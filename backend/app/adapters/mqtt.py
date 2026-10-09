from __future__ import annotations

import asyncio
import json
import ssl
from datetime import timedelta
from typing import Any

import aiomqtt
from pydantic import ValidationError

from ..config import Settings
from ..hub import SentinelHub, now_utc
from ..models import CommandFeedback, CommandRecord, EventRecord, Heartbeat, Quality, SourceMode, TelemetryRecord, VisionDetection


class MQTTAdapter:
    """Optional LAN adapter. It never runs or publishes in simulation mode."""

    def __init__(self, hub: SentinelHub, settings: Settings):
        self.hub, self.settings = hub, settings
        self.client: aiomqtt.Client | None = None
        self.stopped = False
        self._last_error_event = 0.0

    async def run(self) -> None:
        if self.settings.simulation_mode or not self.settings.mqtt_host:
            return
        while not self.stopped:
            self.hub.mqtt_status = "connecting"
            try:
                tls_context = ssl.create_default_context() if self.settings.mqtt_tls else None
                async with aiomqtt.Client(
                    self.settings.mqtt_host,
                    self.settings.mqtt_port,
                    username=self.settings.mqtt_username or None,
                    password=self.settings.mqtt_password or None,
                    tls_context=tls_context,
                    identifier=f"sentinel-hub-{self.settings.site_id}",
                    keepalive=15,
                    timeout=5,
                    max_queued_incoming_messages=500,
                ) as client:
                    self.client = client
                    self.hub.command_publisher = self.publish_command
                    prefix = f"sentinel/{self.settings.site_id}"
                    for topic in ("telemetry/+", "detections/+", "heartbeat/+", "actuator/feedback/+"):
                        await client.subscribe(f"{prefix}/{topic}", qos=1)
                    self.hub.mqtt_status = "connected"
                    async for message in client.messages:
                        try:
                            await self.route(str(message.topic), message.payload, bool(message.retain))
                        except (ValueError, ValidationError, UnicodeError, json.JSONDecodeError) as exc:
                            await self._audit_rejection(type(exc).__name__)
            except asyncio.CancelledError:
                raise
            except (aiomqtt.MqttError, OSError) as exc:
                self.hub.mqtt_status = "disconnected"
                await self._audit_rejection(f"broker_{type(exc).__name__}")
            finally:
                self.client = None
                self.hub.command_publisher = None
                if not self.stopped:
                    self.hub.mqtt_status = "disconnected"
            if not self.stopped:
                await asyncio.sleep(3)

    async def stop(self) -> None:
        self.stopped = True
        self.hub.command_publisher = None
        self.hub.mqtt_status = "stopped"

    async def _audit_rejection(self, reason: str) -> None:
        stamp = asyncio.get_running_loop().time()
        if stamp - self._last_error_event < 5:
            return
        self._last_error_event = stamp
        await self.hub.add_event(EventRecord(
            site_id=self.settings.site_id,
            event_type="mqtt_rejected",
            severity="warning",
            title="MQTT 通道异常或数据被拒绝",
            message=f"报文未进入安全决策链路：{reason}。未记录凭据或原始敏感报文。",
            quality=Quality.invalid,
            source_mode=SourceMode.live,
        ))

    async def route(self, topic: str, payload: bytes | str | dict, retained: bool = False) -> None:
        if self.settings.simulation_mode:
            raise ValueError("MQTT is disabled in simulation mode")
        if retained:
            raise ValueError("retained safety inputs cannot establish real-time state")
        if isinstance(payload, (bytes, str)) and len(payload) > 65536:
            raise ValueError("MQTT payload exceeds 64 KiB")
        data: dict[str, Any] = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
        if not isinstance(data, dict):
            raise ValueError("MQTT payload must be an object")
        prefix = f"sentinel/{self.settings.site_id}/"
        if not topic.startswith(prefix):
            raise ValueError("topic site does not match")
        suffix = topic[len(prefix):].split("/")
        if len(suffix) == 2 and suffix[0] in {"telemetry", "detections", "heartbeat"}:
            kind, device_id = suffix
        elif len(suffix) == 3 and suffix[:2] == ["actuator", "feedback"]:
            kind, device_id = "feedback", suffix[2]
        else:
            raise ValueError("unsupported MQTT topic")
        models = {"telemetry": TelemetryRecord, "detections": VisionDetection, "heartbeat": Heartbeat, "feedback": CommandFeedback}
        record = models[kind].model_validate(data)
        if record.device_id != device_id or record.site_id != self.settings.site_id:
            raise ValueError("topic device/site differs from payload")
        if kind == "telemetry":
            await self.hub.ingest_telemetry(record)
            await self.hub.evaluate_state()
        elif kind == "detections":
            await self.hub.ingest_detection(record)
            await self.hub.evaluate_state()
        elif kind == "heartbeat":
            await self.hub.ingest_heartbeat(record)
        else:
            await self.hub.apply_command_feedback(record)

    async def publish_command(self, command: CommandRecord) -> bool:
        if self.settings.simulation_mode or not self.settings.allow_actuation or self.client is None:
            return False
        payload = command.model_dump(mode="json")
        payload["expires_at"] = (command.requested_at + timedelta(seconds=self.settings.command_timeout_seconds)).isoformat()
        topic = f"sentinel/{self.settings.site_id}/actuator/command/{command.target_device_id}"
        await self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=False, timeout=3)
        return True
