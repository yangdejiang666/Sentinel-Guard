from __future__ import annotations

import asyncio
import json
import math
import re
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .config import Settings, settings
from .db import Database
from .fsm import decide
from .video import Frame, VideoStore
from .models import (
    BusinessState,
    CommandFeedback,
    CommandRecord,
    Device,
    DeviceRegistration,
    DeviceStatus,
    EventRecord,
    Heartbeat,
    NodeType,
    Overview,
    Quality,
    Rules,
    SourceMode,
    StateTransition,
    SystemMode,
    TelemetryRecord,
    VisionDetection,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class RealtimeHub:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._clients.discard(queue)

    async def publish(self, message: dict[str, Any]) -> None:
        for queue in tuple(self._clients):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync_required"})
            queue.put_nowait(message)


class SentinelHub:
    def __init__(self, config: Settings | None = None) -> None:
        self.settings = config or settings
        self.db = Database(self.settings.database_path)
        self.realtime = RealtimeHub()
        self.video = VideoStore(self.settings.stale_after_seconds)
        self.state = BusinessState.normal
        self.state_since = now_utc()
        self.state_reason = "系统启动，等待有效数据"
        self.latest: dict[str, TelemetryRecord] = {}
        self.latest_by_source: dict[tuple[str, str], TelemetryRecord] = {}
        self._latest_detections: dict[str, VisionDetection] = {}
        self._temperature_window: deque[TelemetryRecord] = deque(maxlen=120)
        self.devices: dict[str, Device] = {}
        self.detections: list[VisionDetection] = []
        self._started_at = now_utc()
        self._last_transition: StateTransition | None = None
        self._last_sequences: dict[tuple[str, str, str], int] = {}
        self._last_event_times: dict[tuple[str, str, str], datetime] = {}
        self._state_evidence: dict[str, Any] = {}
        self._state_trusted = False
        self._recovery_target: BusinessState | None = None
        self._recovery_since: datetime | None = None
        self._decision_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._simulation_tick = 0
        self._sim_sequences: defaultdict[tuple[str, str], int] = defaultdict(int)
        self.mqtt_status = "disabled-in-simulation" if self.settings.simulation_mode else ("configured" if self.settings.mqtt_host else "disabled")
        self.command_publisher: Callable[[CommandRecord], Awaitable[bool]] | None = None
        self.local_command_publisher: Callable[[CommandRecord], Awaitable[bool]] | None = None
        self.stream_id = f"stream-{uuid4().hex[:12]}"
        self.stream_sequence = 0
        self.rules = Rules()
        self._initialize_devices()
        self._restore_state()

    def _restore_state(self) -> None:
        rows = self.db.list_json("transitions", 1, site_id=self.settings.site_id, source_mode=self.source_mode.value)
        if rows:
            previous = StateTransition.model_validate(rows[0])
            self.state, self.state_since = previous.to_state, previous.occurred_at
            self.state_reason = f"服务重启，保留上次 {self.state.value} 风险等级，等待新鲜数据复核"
            self._last_transition = previous
            self._state_evidence = {"previous_transition": previous.model_dump(mode="json"), "state_trusted": False, "restart_pending": True}

    @property
    def source_mode(self) -> SourceMode:
        return SourceMode.simulated if self.settings.simulation_mode else SourceMode.live

    async def _publish(self, message: dict[str, Any]) -> None:
        """Publish an event with a monotonic stream sequence for replay/debugging."""
        self.stream_sequence += 1
        envelope = {**message, "stream_id": self.stream_id, "stream_sequence": self.stream_sequence}
        await self.realtime.publish(envelope)

    def _initialize_devices(self) -> None:
        offline = DeviceStatus.not_connected
        source = self.source_mode
        definitions = [
            ("orange-pi-001", "Orange Pi 主控", NodeType.orange_pi, "local", ["fsm", "api", "gpio", "video-decode", "vision-detection", "heartbeat"]),
            ("k230-001", "K230 视觉从节点", NodeType.k230, "MQTT", ["vision-detection", "video-k230", "heartbeat"]),
            ("esp32-s3-001", "ESP32-S3 环境网关", NodeType.esp32, "MQTT", ["temperature", "humidity", "light", "heartbeat"]),
            ("camera-main-001", "主摄像头 MF937", NodeType.camera, "USB / local stream", ["video-main", "vision-metadata"]),
            ("actuator-001", "声光与继电器执行组", NodeType.actuator, "GPIO / isolated relay", ["alarm", "fan", "execution-feedback"]),
        ]
        for device_id, name, node, protocol, capabilities in definitions:
            self.devices[device_id] = Device(
                device_id=device_id,
                site_id=self.settings.site_id,
                display_name=name,
                node_type=node,
                status=offline,
                protocol=protocol,
                capabilities=capabilities,
                firmware_version="sim-1.0" if self.settings.simulation_mode else None,
                model_version="demo-vision-1.0" if self.settings.simulation_mode and node in {NodeType.k230, NodeType.orange_pi} else None,
                source_mode=source,
                health_message="等待模拟数据" if self.settings.simulation_mode else "等待真实设备接入",
            )
        for payload in self.db.list_json("devices", 1000):
            device = Device.model_validate(payload)
            if device.site_id == self.settings.site_id:
                self.devices[device.device_id] = device.model_copy(update={
                    "status": DeviceStatus.not_connected, "last_heartbeat": None,
                    "source_mode": self.source_mode, "health_message": "服务重启，等待新心跳",
                })

    async def register_device(self, request: DeviceRegistration) -> Device:
        if request.device_id in self.devices:
            raise ValueError("device_id already registered; existing devices are not overwritten")
        device = Device(**request.model_dump(), site_id=self.settings.site_id, source_mode=self.source_mode)
        self.devices[device.device_id] = device
        self.db.save_device(device.model_dump(mode="json"), now_utc())
        await self._publish({"type": "device_registered", "data": device.model_dump(mode="json")})
        return device

    def _validate_source(self, device_id: str, source: SourceMode, site_id: str | None = None) -> Device:
        if device_id not in self.devices:
            raise ValueError(f"Unknown device: {device_id}")
        if site_id is not None and site_id != self.settings.site_id:
            raise ValueError(f"Unknown site: {site_id}")
        if source != self.source_mode:
            raise ValueError(f"source_mode must be {self.source_mode.value}; source pipelines cannot be mixed")
        return self.devices[device_id]

    def _set_device_heartbeat(
        self,
        device_id: str,
        data_at: datetime | None = None,
        source_mode: SourceMode | None = None,
    ) -> None:
        device = self.devices[device_id]
        self.devices[device_id] = device.model_copy(update={
            "status": DeviceStatus.online,
            "last_heartbeat": now_utc(),
            "last_data_at": data_at or device.last_data_at,
            "source_mode": source_mode or device.source_mode,
            "health_message": "数据流正常" if data_at else "心跳正常",
        })

    def _timestamp_quality(
        self,
        source_mode: SourceMode,
        current_quality: Quality,
        event_time: datetime,
        now: datetime,
    ) -> tuple[Quality, str | None]:
        skew = (event_time - now).total_seconds()
        age = (now - event_time).total_seconds()
        if current_quality == Quality.invalid:
            return Quality.invalid, "source_marked_invalid"
        if not self.settings.simulation_mode and current_quality == Quality.simulated:
            return Quality.invalid, "simulated_quality_in_live_mode"
        if skew > self.settings.clock_tolerance_seconds:
            return Quality.invalid, f"event_time_is_{skew:.1f}s_in_the_future"
        if age > self.settings.stale_after_seconds or current_quality == Quality.stale:
            return Quality.stale, f"event_age_{age:.1f}s_exceeds_limit"
        if current_quality == Quality.estimated:
            return Quality.estimated, "source_marked_estimated"
        if source_mode == SourceMode.simulated:
            return Quality.simulated, "simulation_mode"
        return current_quality, None

    def _ordering_reason(
        self,
        key: tuple[str, str, str],
        sequence: int,
        event_time: datetime,
    ) -> str | None:
        if key not in self._last_sequences:
            cursor = self.db.get_cursor(json.dumps(key))
            if cursor:
                self._last_sequences[key], self._last_event_times[key] = cursor
        previous_sequence = self._last_sequences.get(key)
        previous_time = self._last_event_times.get(key)
        if previous_sequence is not None and sequence <= previous_sequence:
            return f"out_of_order_sequence_{sequence}_after_{previous_sequence}"
        if previous_time is not None and event_time < previous_time:
            return "out_of_order_event_time"
        return None

    def _remember_ordering(self, key: tuple[str, str, str], sequence: int, event_time: datetime) -> None:
        self._last_sequences[key] = sequence
        self._last_event_times[key] = event_time
        self.db.save_cursor(json.dumps(key), sequence, event_time)

    def _fresh_and_usable(self, record: TelemetryRecord | VisionDetection, now: datetime) -> bool:
        age = (now - record.event_time).total_seconds()
        return (
            -self.settings.clock_tolerance_seconds <= age <= self.settings.stale_after_seconds
            and record.quality == (Quality.simulated if self.settings.simulation_mode else Quality.valid)
            and record.source_mode == self.source_mode
            and self._freshness(self.devices[record.device_id], now).status == DeviceStatus.online
        )

    async def ingest_telemetry(self, record: TelemetryRecord) -> TelemetryRecord:
        now = now_utc()
        device = self._validate_source(record.device_id, record.source_mode, record.site_id)
        if record.node_type != device.node_type or record.metric not in device.capabilities:
            raise ValueError("node_type/metric does not match registered device capabilities")
        if self.db.get_telemetry_event(record.event_id):
            duplicate = record.model_copy(update={
                "ingest_time": now,
                "quality": Quality.invalid,
                "quality_reason": "duplicate_event_id_ignored",
            })
            await self._publish({"type": "quality_issue", "data": duplicate.model_dump(mode="json")})
            return duplicate
        key = (record.device_id, f"{record.site_id}:{record.source_mode.value}:{record.metric}", record.boot_id)
        ordering_reason = self._ordering_reason(key, record.sequence, record.event_time)
        quality, quality_reason = self._timestamp_quality(record.source_mode, record.quality, record.event_time, now)
        if ordering_reason:
            quality, quality_reason = Quality.invalid, ordering_reason
        record = record.model_copy(update={
            "ingest_time": now,
            "quality": quality,
            "quality_reason": quality_reason or record.quality_reason,
        })
        self.db.insert_telemetry(record.model_dump(mode="json"))
        if quality != Quality.invalid and not ordering_reason:
            self._remember_ordering(key, record.sequence, record.event_time)
            current = self.latest_by_source.get((record.device_id, record.metric))
            if current is None or (quality in {Quality.valid, Quality.simulated} and record.event_time >= current.event_time):
                self.latest_by_source[(record.device_id, record.metric)] = record
            previous = self.latest.get(record.metric)
            if previous is None or (quality in {Quality.valid, Quality.simulated} and record.event_time >= previous.event_time):
                self.latest[record.metric] = record
            if quality in {Quality.valid, Quality.simulated, Quality.estimated}:
                self._set_device_heartbeat(record.device_id, record.event_time, record.source_mode)
            if record.metric == "temperature" and quality in {Quality.valid, Quality.simulated}:
                self._temperature_window.append(record)
        await self._publish({"type": "telemetry", "data": record.model_dump(mode="json")})
        return record

    async def ingest_detection(self, detection: VisionDetection) -> VisionDetection:
        now = now_utc()
        device = self._validate_source(detection.device_id, detection.source_mode, detection.site_id)
        if "vision-detection" not in device.capabilities or detection.camera_id not in self.devices:
            raise ValueError("vision capability/camera_id not registered")
        if detection.source_view == "k230" and device.node_type != NodeType.k230:
            raise ValueError("k230 view must originate from K230")
        if detection.source_view == "main" and device.node_type != NodeType.orange_pi:
            raise ValueError("main view must originate from Orange Pi")
        expected_camera = "camera-main-001" if detection.source_view == "main" else "k230-001"
        if detection.camera_id != expected_camera:
            raise ValueError("camera_id does not match the registered source view")
        if self.db.get_json_event("detections", detection.event_id):
            duplicate = detection.model_copy(update={
                "ingest_time": now,
                "quality": Quality.invalid,
                "quality_reason": "duplicate_event_id_ignored",
            })
            await self._publish({"type": "quality_issue", "data": duplicate.model_dump(mode="json")})
            return duplicate
        key = (detection.device_id, f"{detection.site_id}:{detection.source_mode.value}:vision-{detection.source_view}", detection.boot_id)
        ordering_reason = self._ordering_reason(key, detection.sequence, detection.event_time)
        quality, quality_reason = self._timestamp_quality(detection.source_mode, detection.quality, detection.event_time, now)
        if ordering_reason:
            quality, quality_reason = Quality.invalid, ordering_reason
        detection = detection.model_copy(update={
            "ingest_time": now,
            "quality": quality,
            "quality_reason": quality_reason or detection.quality_reason,
        })
        self.db.insert_json("detections", "event_id", detection.event_id, detection.model_dump(mode="json"), "event_time", detection.event_time)
        if quality != Quality.invalid and not ordering_reason:
            self._remember_ordering(key, detection.sequence, detection.event_time)
            self.detections.append(detection)
            self.detections = self.detections[-50:]
            if quality in {Quality.valid, Quality.simulated}:
                previous = self._latest_detections.get(detection.source_view)
                if previous is None or detection.event_time >= previous.event_time:
                    self._latest_detections[detection.source_view] = detection
                self._set_device_heartbeat(detection.device_id, detection.event_time, detection.source_mode)
                self.devices[detection.device_id] = self.devices[detection.device_id].model_copy(update={"model_version": detection.model_version})
        await self._publish({"type": "detection", "data": detection.model_dump(mode="json")})
        if detection.quality not in {Quality.valid, Quality.simulated} or detection.target == "none":
            return detection
        event = EventRecord(
            site_id=self.settings.site_id,
            event_type="vision_detection",
            severity="warning" if detection.target in {"fire", "smoke"} else "info",
            title=f"{detection.source_view.upper()} 视角检测到{detection.target}",
            message=f"置信度 {detection.confidence:.0%} · 模型 {detection.model_version}",
            source_device_id=detection.device_id,
            quality=detection.quality,
            source_mode=detection.source_mode,
            occurred_at=detection.event_time,
            correlation_id=detection.correlation_id,
            payload=detection.model_dump(mode="json"),
        )
        await self.add_event(event)
        return detection

    async def ingest_heartbeat(self, heartbeat: Heartbeat) -> None:
        device = self._validate_source(heartbeat.device_id, heartbeat.source_mode, heartbeat.site_id)
        now = now_utc()
        age = (now - heartbeat.event_time).total_seconds()
        if age < -self.settings.clock_tolerance_seconds:
            raise ValueError("heartbeat event_time is too far in the future")
        if age > self.settings.heartbeat_timeout_seconds:
            raise ValueError("heartbeat event_time is too old to establish liveness")
        if device.source_mode != heartbeat.source_mode:
            raise ValueError("heartbeat source_mode does not match the registered pipeline")
        key = (heartbeat.device_id, f"{heartbeat.site_id}:{heartbeat.source_mode.value}:heartbeat", heartbeat.boot_id)
        previous = self._last_event_times.get(key)
        if previous is None:
            cursor = self.db.get_cursor(json.dumps(key))
            previous = cursor[1] if cursor else None
        if previous is not None and heartbeat.event_time <= previous:
            raise ValueError("duplicate or out-of-order heartbeat cannot refresh liveness")
        self._remember_ordering(key, 0, heartbeat.event_time)
        self._set_device_heartbeat(heartbeat.device_id, heartbeat.event_time, heartbeat.source_mode)
        # An old-but-acceptable heartbeat is delayed, not a new fresh sample.
        self.devices[heartbeat.device_id] = self.devices[heartbeat.device_id].model_copy(update={
            "last_heartbeat": min(heartbeat.event_time, now),
        })
        self.db.save_device(self.devices[heartbeat.device_id].model_dump(mode="json"), now)
        await self._publish({
            "type": "heartbeat",
            "data": heartbeat.model_dump(mode="json"),
        })

    async def apply_command_feedback(self, feedback: CommandFeedback) -> CommandRecord:
        async with self._command_lock:
            self._validate_source(feedback.device_id, feedback.source_mode, feedback.site_id)
            payload = self.db.get_json_event("commands", feedback.command_id)
            if payload is None:
                raise ValueError(f"Unknown command: {feedback.command_id}")
            command = CommandRecord.model_validate(payload)
            if feedback.device_id != command.target_device_id or feedback.site_id != command.site_id:
                raise ValueError("feedback device/site does not match command target")
            if feedback.source_mode != command.source_mode:
                raise ValueError("feedback source_mode does not match command")
            now = now_utc()
            if (feedback.event_time - now).total_seconds() > self.settings.clock_tolerance_seconds:
                raise ValueError("feedback event_time is too far in the future")
            if feedback.event_time < command.requested_at:
                raise ValueError("feedback event_time predates the command")
            last_at = datetime.fromisoformat(command.status_history[-1]["at"]) if command.status_history else command.requested_at
            if feedback.event_time < last_at:
                raise ValueError("feedback event_time predates the latest command status")
            if feedback.status == command.status:
                return command
            if command.status not in {"sent", "acknowledged"}:
                raise ValueError("feedback is not allowed for an unsent or terminal command")
            if (now - command.requested_at).total_seconds() > self.settings.command_timeout_seconds:
                self.db.mark_command_timeout(command.command_id, now)
                raise ValueError("late feedback cannot complete an expired command")
            update = self.db.update_command_feedback(feedback.model_dump(mode="json"))
            if update is None:
                raise ValueError(f"Unknown command: {feedback.command_id}")
            record = CommandRecord.model_validate(update)
            await self._publish({"type": "command_feedback", "data": record.model_dump(mode="json")})
            return record

    async def add_event(self, event: EventRecord) -> EventRecord:
        self.db.insert_json("events", "event_id", event.event_id, event.model_dump(mode="json"), "occurred_at", event.occurred_at)
        await self._publish({"type": "event", "data": event.model_dump(mode="json")})
        return event

    async def add_transition(self, new_state: BusinessState, reason: str, evidence: dict[str, Any]) -> StateTransition | None:
        if new_state == self.state:
            return None
        transition = StateTransition(
            site_id=self.settings.site_id,
            from_state=self.state,
            to_state=new_state,
            reason=reason,
            evidence=evidence,
            rule_version=self.rules.version,
            occurred_at=now_utc(),
            source_mode=self.source_mode,
            vision_threshold=(self.rules.alert_vision_threshold if new_state in {BusinessState.alert, BusinessState.danger} else self.rules.normal_vision_threshold),
        )
        transition.id = self.db.insert_transition(transition.model_dump(mode="json"), transition.occurred_at)
        self.state = new_state
        self.state_since = transition.occurred_at
        self.state_reason = reason
        self._state_evidence = evidence
        self._last_transition = transition
        await self._publish({"type": "state_transition", "data": transition.model_dump(mode="json")})
        await self.add_event(EventRecord(
            site_id=self.settings.site_id,
            event_type="state_transition",
            severity="critical" if new_state == BusinessState.danger else "warning" if new_state == BusinessState.alert else "info",
            title=f"状态变更：{new_state.value}",
            message=reason,
            quality=Quality.simulated if self.settings.simulation_mode else Quality.valid,
            source_mode=self.source_mode,
            occurred_at=transition.occurred_at,
            correlation_id=transition.correlation_id,
            payload=transition.model_dump(mode="json"),
        ))
        if new_state == BusinessState.danger:
            await self.create_command(
                "alarm_on",
                "FSM 达到 DANGER，申请声光报警（演示策略）",
                "fsm-agent",
                request_id=f"{transition.correlation_id}-alarm",
                automatic=True,
            )
            await self.create_command(
                "fan_on",
                "FSM 达到 DANGER，申请演示执行器动作；不代表真实消防处置策略",
                "fsm-agent",
                request_id=f"{transition.correlation_id}-fan",
                automatic=True,
            )
        return transition

    async def acknowledge_event(self, event_id: str, operator: str = "operator") -> EventRecord | None:
        existing = self.db.get_json_event("events", event_id)
        if not existing or existing.get("site_id", "demo-site") != self.settings.site_id or existing.get("source_mode", "live") != self.source_mode.value:
            return None
        payload = self.db.update_event_acknowledgement(event_id, True, operator)
        if payload is None:
            return None
        event = EventRecord.model_validate(payload)
        await self._publish({"type": "event_acknowledged", "data": event.model_dump(mode="json")})
        return event

    async def evaluate_state(self) -> None:
        async with self._decision_lock:
            now = now_utc()
            environment = {
                metric: record for metric in ("temperature", "humidity", "light")
                if (record := self.latest_by_source.get(("esp32-s3-001", metric))) is not None
                and self._fresh_and_usable(record, now)
            }
            detections = [
                item for item in self._latest_detections.values()
                if self._fresh_and_usable(item, now)
            ]
            temperature_rise = None
            current_temp = environment.get("temperature")
            temps = [item for item in self._temperature_window if current_temp is not None and item.device_id == current_temp.device_id and item.boot_id == current_temp.boot_id and 0 <= (now - item.event_time).total_seconds() <= 60]
            if len(temps) >= 2:
                first, last = temps[0], temps[-1]
                seconds = (last.event_time - first.event_time).total_seconds()
                if seconds >= 3:
                    temperature_rise = (last.value - first.value) / seconds * 60
            decision = decide(self.state, environment, detections, self.rules, now, temperature_rise)
            target = decision.state
            current_rank = list(BusinessState).index(self.state)
            target_rank = list(BusinessState).index(target)
            if target_rank < current_rank:
                if self._recovery_target != target:
                    self._recovery_target, self._recovery_since = target, now
                    target = self.state
                elif self._recovery_since and (now - self._recovery_since).total_seconds() >= self.rules.recovery_hold_seconds:
                    self._recovery_target = None
                    self._recovery_since = None
                else:
                    target = self.state
            else:
                self._recovery_target = None
                self._recovery_since = None
            self._state_trusted = decision.trusted and bool(environment)
            self._state_evidence = {**decision.evidence, "state_trusted": self._state_trusted, "recovery_target": self._recovery_target.value if self._recovery_target else None}
            self.state_reason = decision.reason
            if self._recovery_target:
                self.state_reason += f"；恢复持续观察中（{self.rules.recovery_hold_seconds:g}s）"
            await self.add_transition(target, self.state_reason, self._state_evidence)

    async def create_command(
        self,
        command_type: str,
        reason: str,
        requested_by: str,
        request_id: str | None = None,
        automatic: bool = False,
    ) -> CommandRecord:
        async with self._command_lock:
            if request_id:
                existing = self.db.command_for_request(request_id)
                if existing:
                    if existing.get("site_id", "demo-site") != self.settings.site_id or existing.get("source_mode", "live") != self.source_mode.value:
                        raise ValueError("request_id belongs to a different site/source pipeline")
                    if existing["command_type"] != command_type or existing["reason"] != reason or existing["requested_by"] != requested_by:
                        raise ValueError("request_id already used for a different command")
                    return CommandRecord.model_validate(existing)
            source_mode = self.source_mode
            requested_at = now_utc()
            if self.settings.simulation_mode:
                status, message = "completed", "仅完成模拟命令回执；未驱动物理硬件"
            elif not self.settings.allow_actuation:
                status, message = "failed", "真实执行被安全开关禁止；未驱动物理硬件"
            elif automatic and not self.settings.auto_actuation:
                status, message = "failed", "自动真实执行未启用；保留决策请求，不驱动硬件"
            elif automatic and not self._state_trusted:
                status, message = "failed", "关键输入不完整或过期，自动真实执行被禁止；等待完整复核"
            else:
                status, message = "accepted", "命令已登记，等待执行器适配器接入"
            record = CommandRecord(
                request_id=request_id,
                site_id=self.settings.site_id,
                command_type=command_type,
                reason=reason,
                requested_by=requested_by,
                status=status,
                requested_at=requested_at,
                acknowledged_at=requested_at if status == "completed" else None,
                completed_at=requested_at if status == "completed" else None,
                execution_message=message,
                source_mode=source_mode,
                status_history=[{"status": status, "at": requested_at.isoformat()}],
            )
            record.id = self.db.insert_json("commands", "command_id", record.command_id, record.model_dump(mode="json"), "requested_at", record.requested_at)
            if request_id:
                self.db.remember_request(request_id, record.command_id)
            publisher = self.local_command_publisher or self.command_publisher
            if status == "accepted" and publisher is not None:
                try:
                    sent = await publisher(record)
                except Exception as exc:
                    sent = False
                    record = record.model_copy(update={"status": "failed", "execution_message": f"命令发布失败：{type(exc).__name__}"})
                if sent:
                    sent_at = now_utc()
                    record = record.model_copy(update={"status": "sent", "execution_message": "命令已发布至执行器通道，等待硬件回执", "status_history": [*record.status_history, {"status": "sent", "at": sent_at.isoformat()}]})
                else:
                    record = record.model_copy(update={"status": "failed", "execution_message": "执行通道未接受命令；未取得硬件完成回执", "status_history": [*record.status_history, {"status": "failed", "at": now_utc().isoformat()}]})
                self.db.save_command(record.model_dump(mode="json"))
            await self._publish({"type": "command", "data": record.model_dump(mode="json")})
            await self.add_event(EventRecord(
                site_id=self.settings.site_id,
                event_type="control_command",
                severity="warning",
                title=f"控制请求：{command_type}",
                message=record.execution_message or reason,
                source_device_id="actuator-001",
                quality=Quality.simulated if self.settings.simulation_mode else Quality.valid,
                source_mode=record.source_mode,
                occurred_at=record.requested_at,
                correlation_id=record.command_id,
                payload=record.model_dump(mode="json"),
            ))
            return record

    def _freshness(self, device: Device, now: datetime) -> Device:
        heartbeat = device.last_heartbeat
        if heartbeat is None:
            return device.model_copy(update={
                "status": DeviceStatus.not_connected,
                "health_message": "等待模拟数据" if self.settings.simulation_mode else "等待真实设备接入",
            })
        age = max(0.0, (now - heartbeat).total_seconds())
        status = DeviceStatus.online if age <= self.settings.stale_after_seconds else DeviceStatus.delayed if age <= self.settings.heartbeat_timeout_seconds else DeviceStatus.offline
        return device.model_copy(update={
            "status": status,
            "health_message": "通信正常" if status == DeviceStatus.online else f"心跳延迟 {age:.0f}s" if status == DeviceStatus.delayed else f"心跳已中断 {age:.0f}s",
        })

    def overview(self) -> Overview:
        now = now_utc()
        devices = [self._freshness(device, now) for device in self.devices.values()]
        offline = [device for device in devices if device.status in {DeviceStatus.offline, DeviceStatus.not_connected}]
        missing_environment = any(
            ("esp32-s3-001", metric) not in self.latest_by_source or not self._fresh_and_usable(self.latest_by_source[("esp32-s3-001", metric)], now)
            for metric in ("temperature", "humidity", "light")
        )
        missing_vision = any(view not in self._latest_detections or not self._fresh_and_usable(self._latest_detections[view], now) for view in ("main", "k230"))
        delayed = [device for device in devices if device.status == DeviceStatus.delayed]
        if offline or delayed:
            mode, message = SystemMode.degraded, f"{len(offline)} 个节点离线/未连接，{len(delayed)} 个节点心跳延迟；当前业务状态不代表数据完整"
        elif missing_environment or missing_vision:
            mode, message = SystemMode.incomplete, "环境或双路视觉数据缺失/过期；不能视为完整现场结论"
        else:
            mode, message = SystemMode.normal, "所有已登记数据源在线"
        scope = {"site_id": self.settings.site_id, "source_mode": self.source_mode.value}
        events = [EventRecord.model_validate(row) for row in self.db.list_json("events", 20, **scope)]
        transitions = [StateTransition.model_validate(row) for row in self.db.list_json("transitions", 20, **scope)]
        commands = [CommandRecord.model_validate(row) for row in self.db.list_json("commands", 10, **scope)]
        recent = [event for event in events if not event.acknowledged][:10]
        effective_telemetry: list[TelemetryRecord] = []
        for item in self.latest_by_source.values():
            age = max(0.0, (now - item.event_time).total_seconds())
            quality = Quality.stale if age > self.settings.stale_after_seconds else item.quality
            effective_telemetry.append(item.model_copy(update={"quality": quality}))
        return Overview(
            app_name=self.settings.app_name,
            site_id=self.settings.site_id,
            simulation_mode=self.settings.simulation_mode,
            current_state=self.state,
            state_since=self.state_since,
            state_reason=self.state_reason,
            state_trusted=self._state_trusted and not offline and not delayed and not missing_environment and not missing_vision,
            system_mode=mode,
            system_message=message,
            generated_at=now,
            stream_id=self.stream_id,
            stream_sequence=self.stream_sequence,
            rules=self.rules,
            vision_threshold=(self.rules.alert_vision_threshold if self.state in {BusinessState.alert, BusinessState.danger} else self.rules.normal_vision_threshold),
            devices=devices,
            latest_telemetry=effective_telemetry,
            latest_detections=[item.model_copy(update={"quality": Quality.stale if (now - item.event_time).total_seconds() > self.settings.stale_after_seconds else item.quality}) for item in self._latest_detections.values()],
            active_events=recent,
            recent_transitions=transitions,
            latest_commands=commands,
            fallback_active=(not self.settings.simulation_mode and (self.mqtt_status not in {"connected", "disabled"} or any(device.status != DeviceStatus.online for device in devices if device.node_type in {NodeType.esp32, NodeType.k230}))),
            state_evidence=self._state_evidence,
            mqtt_status=self.mqtt_status,
            local_fallback_ready=not self.settings.simulation_mode and self.local_command_publisher is not None,
            video_channels=self.video.channels(now, self.source_mode),
        )

    async def simulate_once(self) -> None:
        if not self.settings.simulation_mode:
            return
        now = now_utc()
        seconds = (now - self._started_at).total_seconds()
        cycle = seconds % 48
        heat_phase = 26 <= cycle < 36
        alert_phase = 20 <= cycle < 26
        temp = 22.8 + (22.0 if alert_phase else 48.0 if heat_phase else 0.8 * math.sin(seconds / 11))
        humidity = 48 + 3.2 * math.sin(seconds / 17)
        light = 10.0 if 8 <= cycle < 20 else 180.0 + 20 * math.sin(seconds / 9)
        for metric, value, unit in (("temperature", temp, "°C"), ("humidity", humidity, "%"), ("light", light, "lux")):
            device_id = "esp32-s3-001"
            self._sim_sequences[(device_id, metric)] += 1
            await self.ingest_telemetry(TelemetryRecord(
                device_id=device_id,
                site_id=self.settings.site_id,
                node_type=NodeType.esp32,
                channel="environment",
                metric=metric,
                value=round(value, 2),
                unit=unit,
                event_time=now,
                sequence=self._sim_sequences[(device_id, metric)],
                boot_id=self.stream_id,
                quality=Quality.simulated,
                source_mode=SourceMode.simulated,
            ))
        # Simulated heartbeats demonstrate all-node health without claiming hardware exists.
        for device_id in ("orange-pi-001", "k230-001", "camera-main-001", "actuator-001"):
            self._set_device_heartbeat(device_id)
        self._simulation_tick += 1
        detection_due = self._simulation_tick % 2 == 1
        if detection_due:
            target = "fire" if heat_phase else "smoke" if alert_phase else "person" if 12 <= cycle < 19 else "none"
            confidence = (0.88 + 0.04 * math.sin(seconds)) if heat_phase else (0.68 if alert_phase else 0.0 if target == "none" else 0.81)
            if True:
                await self.ingest_detection(VisionDetection(
                    device_id="k230-001",
                    site_id=self.settings.site_id,
                    camera_id="k230-001",
                    source_view="k230",
                    target=target,
                    confidence=round(confidence, 3),
                    bbox=[0.35, 0.22, 0.2, 0.38],
                    event_time=now,
                    quality=Quality.simulated,
                    source_mode=SourceMode.simulated,
                    model_version="demo-vision-1.0",
                    sequence=self._simulation_tick,
                    boot_id=self.stream_id,
                ))
                if True:
                    # Mirror an intermittent main-camera inference for the dashboard.
                    await self.ingest_detection(VisionDetection(
                        device_id="orange-pi-001",
                        site_id=self.settings.site_id,
                        camera_id="camera-main-001",
                        source_view="main",
                        target=target,
                        confidence=round(max(0.0, confidence - 0.08), 3),
                        bbox=[0.44, 0.24, 0.18, 0.36],
                        event_time=now,
                        quality=Quality.simulated,
                        source_mode=SourceMode.simulated,
                        model_version="demo-vision-1.0",
                        sequence=self._simulation_tick,
                        boot_id=self.stream_id,
                    ))
        await self.evaluate_state()
        await self._publish({"type": "health", "data": [d.model_dump(mode="json") for d in self.overview().devices]})

    async def simulator_loop(self) -> None:
        while True:
            try:
                await self.simulate_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.add_event(EventRecord(
                    site_id=self.settings.site_id,
                    event_type="simulator_error",
                    severity="warning",
                    title="模拟数据源异常",
                    message=str(exc),
                    quality=Quality.invalid,
                    source_mode=SourceMode.simulated,
                ))
            await asyncio.sleep(max(0.2, self.settings.simulation_interval_seconds))


    async def ingest_frame(self, frame: Frame) -> bool:
        device = self._validate_source(frame.device_id, frame.source_mode)
        if frame.view not in {"main", "k230"} or f"video-{frame.view}" not in device.capabilities:
            raise ValueError("video view does not match registered device capability")
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", frame.frame_id):
            raise ValueError("invalid frame_id")
        quality, _ = self._timestamp_quality(frame.source_mode, Quality.valid, frame.event_time, frame.ingest_time)
        if quality not in {Quality.valid, Quality.simulated}:
            raise ValueError("video frame timestamp is invalid or stale")
        inserted = self.video.put(frame)
        if inserted:
            self._set_device_heartbeat(frame.device_id, frame.event_time, frame.source_mode)
            await self._publish({"type": "video", "data": [item.model_dump(mode="json") for item in self.video.channels(now_utc(), self.source_mode)]})
        return inserted

    async def expire_commands(self) -> None:
        async with self._command_lock:
            now = now_utc()
            for payload in self.db.list_json("commands", 1000):
                command = CommandRecord.model_validate(payload)
                if command.site_id != self.settings.site_id or command.source_mode != self.source_mode or command.status not in {"accepted", "sent", "acknowledged"}:
                    continue
                if (now - command.requested_at).total_seconds() > self.settings.command_timeout_seconds:
                    updated = self.db.mark_command_timeout(command.command_id, now)
                    await self._publish({"type": "command_feedback", "data": updated})
                    await self.add_event(EventRecord(site_id=self.settings.site_id, event_type="command_timeout", severity="warning", title="执行器回执超时", message="命令未在时限内完成；不会假定硬件已动作", correlation_id=command.command_id, source_mode=self.source_mode, quality=Quality.simulated if self.settings.simulation_mode else Quality.valid, payload=updated or {}))

    async def monitor_loop(self) -> None:
        while True:
            await self.evaluate_state()
            await self.expire_commands()
            await self._publish({"type": "snapshot", "data": self.overview().model_dump(mode="json")})
            await asyncio.sleep(1)

