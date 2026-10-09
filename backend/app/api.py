from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import ROOT_DIR, Settings, settings
from .hub import SentinelHub, now_utc
from .models import (
    Acknowledgement,
    BusinessState,
    CommandFeedback,
    CommandRecord,
    CommandRequest,
    ConfigResponse,
    Device,
    DeviceRegistration,
    EventRecord,
    EventAction,
    SafetySignal,
    Heartbeat,
    Overview,
    Quality,
    SourceMode,
    StateTransition,
    TelemetryRecord,
    VisionDetection,
    VideoChannel,
)
from .video import Frame


MAX_JPEG_BYTES = 2 * 1024 * 1024


def _require_token(provided: str | None, expected: str, label: str) -> None:
    if expected and not secrets.compare_digest(provided or "", expected):
        raise HTTPException(status_code=401, detail=f"{label} token required")


def _aware(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail=f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _topics(site_id: str) -> dict[str, str]:
    prefix = f"sentinel/{site_id}"
    return {
        "telemetry": f"{prefix}/telemetry/{{device_id}}",
        "detections": f"{prefix}/detections/{{device_id}}",
        "heartbeat": f"{prefix}/heartbeat/{{device_id}}",
        "command": f"{prefix}/actuator/command/{{device_id}}",
        "feedback": f"{prefix}/actuator/feedback/{{device_id}}",
    }


def create_app(config: Settings | None = None) -> FastAPI:
    app_settings = config or settings
    hub = SentinelHub(app_settings)
    static_dir = ROOT_DIR / "frontend"
    mqtt_adapter = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal mqtt_adapter
        tasks: list[asyncio.Task] = []
        if app_settings.simulation_mode:
            await hub.simulate_once()
            tasks.append(asyncio.create_task(hub.simulator_loop(), name="sentinel-simulator"))
        elif app_settings.mqtt_host:
            from .adapters.mqtt import MQTTAdapter

            mqtt_adapter = MQTTAdapter(hub, app_settings)
            tasks.append(asyncio.create_task(mqtt_adapter.run(), name="sentinel-mqtt"))
        tasks.append(asyncio.create_task(hub.monitor_loop(), name="sentinel-monitor"))
        try:
            yield
        finally:
            if mqtt_adapter is not None:
                await mqtt_adapter.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="智卫 Sentinel API",
        version="0.2.0",
        description="本地优先、多模态、硬件可扩展的边缘安全监测平台。模拟数据不代表真实现场结论。",
        lifespan=lifespan,
    )
    app.state.hub = hub
    app.state.settings = app_settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Control-Token", "X-Device-Token"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        # Revalidate console HTML and assets so an existing browser does not
        # silently keep a previous camera layout after a local deployment.
        if response.headers.get("content-type", "").split(";")[0] in {
            "text/html", "text/css", "text/javascript", "application/javascript",
        } and not request.url.path.startswith(("/api/", "/docs", "/redoc")):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        if not request.url.path.startswith("/docs") and not request.url.path.startswith("/openapi"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'",
            )
        return response

    @app.get("/api/v1/health")
    async def health() -> dict:
        database_ok = hub.db.check()
        return {
            "status": "ok" if database_ok else "degraded",
            "app": app_settings.app_name,
            "environment": app_settings.environment,
            "simulation_mode": app_settings.simulation_mode,
            "database": "connected" if database_ok else "unavailable",
            "mqtt_status": hub.mqtt_status,
            "server_time": now_utc().isoformat(),
        }

    @app.get("/api/v1/overview", response_model=Overview)
    async def overview() -> Overview:
        return hub.overview()

    @app.get("/api/v1/devices", response_model=list[Device])
    async def devices() -> list[Device]:
        return hub.overview().devices

    @app.post("/api/v1/devices", response_model=Device, status_code=201)
    async def register_device(record: DeviceRegistration, x_control_token: str | None = Header(default=None)) -> Device:
        _require_token(x_control_token, app_settings.control_token, "control")
        try:
            return await hub.register_device(record)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/telemetry/latest", response_model=list[TelemetryRecord])
    async def telemetry_latest() -> list[TelemetryRecord]:
        return hub.overview().latest_telemetry

    @app.get("/api/v1/telemetry/history", response_model=list[TelemetryRecord])
    async def telemetry_history(
        metric: str | None = None,
        device_id: str | None = None,
        device_id_camel: str | None = Query(default=None, alias="deviceId"),
        start: str | None = None,
        end: str | None = None,
        source_mode: SourceMode | None = None,
        limit: int = Query(default=120, ge=1, le=1000),
    ) -> list[TelemetryRecord]:
        start_at, end_at = _aware(start, "start"), _aware(end, "end")
        if start_at and end_at and start_at > end_at:
            raise HTTPException(status_code=422, detail="start must not be after end")
        rows = hub.db.list_telemetry(metric, device_id or device_id_camel, limit, start_at, end_at,
                                     site_id=app_settings.site_id, source_mode=(source_mode or hub.source_mode).value)
        return [TelemetryRecord.model_validate(row) for row in rows]

    @app.get("/api/v1/detections", response_model=list[VisionDetection])
    async def detections(limit: int = Query(default=50, ge=1, le=500), source_mode: SourceMode | None = None) -> list[VisionDetection]:
        return [VisionDetection.model_validate(row) for row in hub.db.list_json("detections", limit, site_id=app_settings.site_id, source_mode=(source_mode or hub.source_mode).value)]

    @app.get("/api/v1/events", response_model=list[EventRecord])
    async def events(limit: int = Query(default=100, ge=1, le=1000), source_mode: SourceMode | None = None) -> list[EventRecord]:
        return [EventRecord.model_validate(row) for row in hub.db.list_json("events", limit, site_id=app_settings.site_id, source_mode=(source_mode or hub.source_mode).value)]

    @app.post("/api/v1/ingest/anomalies", response_model=EventRecord, status_code=202)
    async def ingest_anomaly(record: SafetySignal, x_device_token: str | None = Header(default=None)) -> EventRecord:
        _require_token(x_device_token, app_settings.device_token, "device")
        try:
            return await hub.ingest_safety_signal(record, "anomaly")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/events/search")
    async def search_events(
        q: str = Query(default="", max_length=120),
        severity: str = Query(default="all", pattern="^(all|info|warning|critical)$"),
        acknowledgement: str = Query(default="all", pattern="^(all|pending|acknowledged)$"),
        kind: str = Query(default="all", pattern="^(all|anomaly|industrial_alert|other)$"),
        handling: str = Query(default="all", pattern="^(all|open|in_progress|pending_review|closed)$"),
        sort: str = Query(default="recent", pattern="^(recent|priority)$"),
        page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
    ) -> dict:
        result = hub.db.search_events(site_id=app_settings.site_id, source_mode=hub.source_mode.value,
                                     q=q.strip(), severity=severity, acknowledgement=acknowledgement,
                                     kind=kind, handling=handling, sort=sort, page=page, page_size=page_size)
        result["items"] = [EventRecord.model_validate(item).model_dump(mode="json") for item in result["items"]]
        return result

    @app.post("/api/v1/events/{event_id}/actions", response_model=EventRecord)
    async def event_action(event_id: str, action: EventAction,
                           x_control_token: str | None = Header(default=None)) -> EventRecord:
        _require_token(x_control_token, app_settings.control_token, "control")
        try:
            result = hub.db.act_on_event(event_id, action.model_dump(mode="json"),
                                         app_settings.site_id, hub.source_mode.value)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Event not found")
        record, changed = result
        event = EventRecord.model_validate(record)
        if changed:
            await hub._publish({"type": "event_updated", "data": event.model_dump(mode="json")})
        return event

    @app.post("/api/v1/ingest/industrial-alerts", response_model=EventRecord, status_code=202)
    async def ingest_industrial_alert(record: SafetySignal, x_device_token: str | None = Header(default=None)) -> EventRecord:
        _require_token(x_device_token, app_settings.device_token, "device")
        try:
            return await hub.ingest_safety_signal(record, "industrial_alert")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/events/{event_id}/acknowledge", response_model=EventRecord)
    async def acknowledge_event(
        event_id: str,
        acknowledgement: Acknowledgement | None = Body(default=None),
        x_control_token: str | None = Header(default=None),
    ) -> EventRecord:
        _require_token(x_control_token, app_settings.control_token, "control")
        event = await hub.acknowledge_event(event_id, acknowledgement.acknowledged_by if acknowledgement else "operator")
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        return event

    @app.get("/api/v1/events/{event_id}", response_model=EventRecord)
    async def event_detail(event_id: str) -> EventRecord:
        record = hub.db.get_json_event("events", event_id)
        if not record or record.get("site_id") != app_settings.site_id or record.get("source_mode") != hub.source_mode.value:
            raise HTTPException(status_code=404, detail="Event not found")
        return EventRecord.model_validate(record)

    @app.get("/api/v1/state/transitions", response_model=list[StateTransition])
    async def transitions(limit: int = Query(default=100, ge=1, le=1000), source_mode: SourceMode | None = None) -> list[StateTransition]:
        return [StateTransition.model_validate(row) for row in hub.db.list_json("transitions", limit, site_id=app_settings.site_id, source_mode=(source_mode or hub.source_mode).value)]

    @app.get("/api/v1/commands", response_model=list[CommandRecord])
    async def commands(limit: int = Query(default=100, ge=1, le=1000), source_mode: SourceMode | None = None) -> list[CommandRecord]:
        return [CommandRecord.model_validate(row) for row in hub.db.list_json("commands", limit, site_id=app_settings.site_id, source_mode=(source_mode or hub.source_mode).value)]

    @app.post("/api/v1/commands", response_model=CommandRecord, status_code=201)
    async def create_command(request: CommandRequest, x_control_token: str | None = Header(default=None)) -> CommandRecord:
        _require_token(x_control_token, app_settings.control_token, "control")
        try:
            return await hub.create_command(request.command_type, request.reason, request.requested_by, request.request_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/ingest/telemetry", response_model=TelemetryRecord, status_code=202)
    async def ingest_telemetry(record: TelemetryRecord, x_device_token: str | None = Header(default=None)) -> TelemetryRecord:
        _require_token(x_device_token, app_settings.device_token, "device")
        try:
            result = await hub.ingest_telemetry(record)
            await hub.evaluate_state()
            return result
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/ingest/detections", response_model=VisionDetection, status_code=202)
    async def ingest_detection(record: VisionDetection, x_device_token: str | None = Header(default=None)) -> VisionDetection:
        _require_token(x_device_token, app_settings.device_token, "device")
        try:
            result = await hub.ingest_detection(record)
            await hub.evaluate_state()
            return result
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/ingest/heartbeat", status_code=202)
    async def ingest_heartbeat(record: Heartbeat, x_device_token: str | None = Header(default=None)) -> dict:
        _require_token(x_device_token, app_settings.device_token, "device")
        try:
            await hub.ingest_heartbeat(record)
            return {"accepted": True, "server_time": now_utc().isoformat()}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/ingest/command-feedback", response_model=CommandRecord, status_code=202)
    async def command_feedback(record: CommandFeedback, x_device_token: str | None = Header(default=None)) -> CommandRecord:
        _require_token(x_device_token, app_settings.device_token, "device")
        try:
            return await hub.apply_command_feedback(record)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/ingest/video/{view}", status_code=202)
    async def ingest_video(
        view: str,
        request: Request,
        device_id: str,
        frame_id: str,
        event_time: str,
        source_mode: SourceMode,
        x_device_token: str | None = Header(default=None),
    ) -> dict:
        _require_token(x_device_token, app_settings.device_token, "device")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {"image/jpeg", "image/jpg"}:
            raise HTTPException(status_code=415, detail="video preview endpoint accepts image/jpeg only")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_JPEG_BYTES:
                raise HTTPException(status_code=413, detail="JPEG frame exceeds 2 MiB")
            chunks.append(chunk)
        jpeg = b"".join(chunks)
        if len(jpeg) < 4 or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
            raise HTTPException(status_code=422, detail="invalid JPEG signature")
        occurred_at = _aware(event_time, "event_time")
        if occurred_at is None:
            raise HTTPException(status_code=422, detail="event_time is required")
        try:
            inserted = await hub.ingest_frame(Frame(view, device_id, frame_id, jpeg, occurred_at, now_utc(), source_mode))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"accepted": True, "inserted": inserted, "frame_id": frame_id}

    @app.get("/api/v1/video/channels", response_model=list[VideoChannel])
    async def video_channels() -> list[VideoChannel]:
        return hub.video.channels(now_utc(), hub.source_mode)

    @app.get("/api/v1/video/{view}/frame")
    async def video_frame(view: str, frame_id: str | None = None) -> Response:
        if view != "k230":
            raise HTTPException(status_code=404, detail="Unknown video channel")
        frame = hub.video.get(view, frame_id)
        if frame is None:
            raise HTTPException(status_code=404, detail="Frame not found")
        return Response(
            frame.jpeg,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Frame-Id": frame.frame_id,
                "X-Event-Time": frame.event_time.isoformat(),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/config", response_model=ConfigResponse)
    async def platform_config() -> ConfigResponse:
        return ConfigResponse(
            stale_after_seconds=app_settings.stale_after_seconds,
            heartbeat_timeout_seconds=app_settings.heartbeat_timeout_seconds,
            supported_states=list(BusinessState),
            supported_quality=list(Quality),
            supported_source_modes=list(SourceMode),
            simulation_mode=app_settings.simulation_mode,
            control_token_required=bool(app_settings.control_token),
            device_token_required=bool(app_settings.device_token),
            rules=hub.rules,
            site_id=app_settings.site_id,
            allow_actuation=app_settings.allow_actuation,
            auto_actuation=app_settings.auto_actuation,
            command_timeout_seconds=app_settings.command_timeout_seconds,
            mqtt_topics=_topics(app_settings.site_id),
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = await hub.realtime.subscribe()
        try:
            await websocket.send_json({
                "type": "snapshot",
                "stream_id": hub.stream_id,
                "stream_sequence": hub.stream_sequence,
                "data": hub.overview().model_dump(mode="json"),
            })
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                    if message.get("type") == "resync_required":
                        message = {
                            "type": "snapshot",
                            "stream_id": hub.stream_id,
                            "stream_sequence": hub.stream_sequence,
                            "data": hub.overview().model_dump(mode="json"),
                        }
                    await websocket.send_json(message)
                except TimeoutError:
                    await websocket.send_json({"type": "ping", "server_time": now_utc().isoformat()})
        except WebSocketDisconnect:
            pass
        finally:
            hub.realtime.unsubscribe(queue)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found")
        candidate = (static_dir / path).resolve()
        if candidate.is_file() and static_dir.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()

