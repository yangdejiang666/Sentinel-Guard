from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasGenerator, AwareDatetime, BaseModel, Field, ConfigDict, field_validator, model_validator
from pydantic.alias_generators import to_camel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    """Accept camelCase/snake_case input; responses consistently use snake_case."""
    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=AliasGenerator(validation_alias=to_camel),
        allow_inf_nan=False,
        str_strip_whitespace=True,
        extra="forbid",
    )


class Quality(str, Enum):
    valid = "valid"
    stale = "stale"
    invalid = "invalid"
    estimated = "estimated"
    simulated = "simulated"


class SourceMode(str, Enum):
    live = "live"
    simulated = "simulated"
    replay = "replay"


class NodeType(str, Enum):
    orange_pi = "Orange Pi 3B"
    k230 = "K230"
    esp32 = "ESP32-S3"
    camera = "Camera"
    actuator = "Actuator"
    simulator = "Simulator"


class BusinessState(str, Enum):
    normal = "NORMAL"
    watch = "WATCH"
    alert = "ALERT"
    danger = "DANGER"


class SystemMode(str, Enum):
    normal = "NORMAL_OPERATION"
    degraded = "DEGRADED"
    offline = "OFFLINE"
    incomplete = "DATA_INCOMPLETE"


class DeviceStatus(str, Enum):
    online = "online"
    delayed = "delayed"
    offline = "offline"
    not_connected = "not_connected"


class Device(Contract):
    device_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    site_id: str = "demo-site"
    display_name: str = Field(min_length=1, max_length=120)
    node_type: NodeType
    status: DeviceStatus = DeviceStatus.not_connected
    protocol: str
    capabilities: list[str]
    firmware_version: str | None = None
    model_version: str | None = None
    last_heartbeat: AwareDatetime | None = None
    last_data_at: AwareDatetime | None = None
    source_mode: SourceMode = SourceMode.live
    health_message: str = "等待数据"


class DeviceRegistration(Contract):
    device_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    node_type: NodeType
    protocol: str = Field(min_length=1, max_length=80)
    capabilities: list[str] = Field(min_length=1, max_length=40)
    firmware_version: str | None = None
    model_version: str | None = None


class TelemetryRecord(Contract):
    event_id: str = Field(default_factory=lambda: f"evt-{uuid4().hex}", min_length=1, max_length=128)
    schema_version: Literal["1.0"] = "1.0"
    site_id: str = "demo-site"
    device_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    node_type: NodeType
    channel: str = Field(min_length=1, max_length=80)
    metric: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    value: float
    unit: str = Field(min_length=1, max_length=40)
    event_time: AwareDatetime
    ingest_time: AwareDatetime = Field(default_factory=utc_now)
    sequence: int = Field(ge=0)
    boot_id: str = Field(default="default", min_length=1, max_length=80)
    quality: Quality = Quality.valid
    quality_reason: str | None = None
    source_mode: SourceMode = SourceMode.live
    model_version: str | None = None
    correlation_id: str = Field(default_factory=lambda: f"corr-{uuid4().hex[:12]}")

    @field_validator("event_time", "ingest_time")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        # Pydantic's allow_inf_nan setting protects JSON parsing, while this
        # explicit check also covers values passed by Python callers.
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("value must be a finite number")
        return value

    @model_validator(mode="after")
    def validate_metric(self) -> "TelemetryRecord":
        # Known metrics use canonical units; new capabilities may add metrics
        # without changing the REST contract or the generic telemetry table.
        ranges = {"temperature": ("°C", -40, 125), "humidity": ("%", 0, 100), "light": ("lux", 0, 200000)}
        if self.metric in ranges:
            unit, low, high = ranges[self.metric]
            if self.unit != unit or not low <= self.value <= high:
                raise ValueError(f"{self.metric} requires {unit} and a value in [{low}, {high}]")
        return self


class VisionDetection(Contract):
    id: int | None = None
    event_id: str = Field(default_factory=lambda: f"det-{uuid4().hex[:12]}")
    schema_version: Literal["1.0"] = "1.0"
    site_id: str = "demo-site"
    device_id: str
    camera_id: str
    # "main" is read-only compatibility for immutable historical records.
    # New ingestion rejects it; both processors use camera_id/source_view K230.
    source_view: Literal["main", "k230"]
    target: Literal["fire", "smoke", "person", "none"]
    confidence: float = Field(ge=0, le=1)
    bbox: list[float] = Field(default_factory=lambda: [0.25, 0.2, 0.5, 0.55], min_length=4, max_length=4)
    event_time: AwareDatetime
    ingest_time: AwareDatetime = Field(default_factory=utc_now)
    sequence: int = Field(default=0, ge=0)
    boot_id: str = "default"
    quality: Quality = Quality.valid
    quality_reason: str | None = None
    source_mode: SourceMode = SourceMode.live
    model_version: str = Field(min_length=1, max_length=120)
    frame_id: str | None = Field(default=None, max_length=128)
    correlation_id: str = Field(default_factory=lambda: f"corr-{uuid4().hex[:12]}")

    @field_validator("bbox")
    @classmethod
    def normalized_bbox(cls, value: list[float]) -> list[float]:
        x, y, width, height = value
        if not all(0 <= component <= 1 for component in value) or x + width > 1.001 or y + height > 1.001:
            raise ValueError("bbox must be normalized [x,y,width,height] within the frame")
        return value

    @field_validator("event_time", "ingest_time")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class Heartbeat(Contract):
    site_id: str = "demo-site"
    device_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    event_time: AwareDatetime
    source_mode: SourceMode = SourceMode.live
    boot_id: str = "default"


class VideoChannel(Contract):
    source_view: Literal["k230"]
    device_id: str
    frame_id: str | None = None
    event_time: AwareDatetime | None = None
    ingest_time: AwareDatetime | None = None
    status: Literal["missing", "fresh", "stale"] = "missing"
    source_mode: SourceMode = SourceMode.live
    frame_url: str | None = None


class CommandFeedback(Contract):
    site_id: str = "demo-site"
    command_id: str = Field(min_length=1, max_length=128)
    device_id: str = "actuator-001"
    status: Literal["acknowledged", "completed", "failed"]
    event_time: AwareDatetime
    execution_message: str = Field(min_length=1, max_length=500)
    source_mode: SourceMode = SourceMode.live


class Acknowledgement(Contract):
    acknowledged_by: str = Field(default="operator", min_length=1, max_length=80)


class EventAction(Contract):
    action: Literal["claim", "note", "submit_review", "close", "reopen"]
    actor: str = Field(min_length=1, max_length=80, pattern=r".*\S.*")
    note: str = Field(min_length=1, max_length=1000, pattern=r".*\S.*")
    evidence_ref: str = Field(default="", max_length=500)
    expected_revision: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=128)


class StateTransition(Contract):
    id: int | None = None
    site_id: str = "demo-site"
    from_state: BusinessState
    to_state: BusinessState
    reason: str
    rule_version: str = "demo-fsm-1.0"
    evidence: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)
    correlation_id: str = Field(default_factory=lambda: f"corr-{uuid4().hex[:12]}")
    source_mode: SourceMode = SourceMode.live
    vision_threshold: float = 0.72


class EventRecord(Contract):
    id: int | None = None
    event_id: str = Field(default_factory=lambda: f"event-{uuid4().hex[:12]}")
    site_id: str = "demo-site"
    event_type: str
    severity: Literal["info", "warning", "critical"] = "info"
    title: str
    message: str
    source_device_id: str | None = None
    quality: Quality = Quality.valid
    source_mode: SourceMode = SourceMode.live
    occurred_at: datetime = Field(default_factory=utc_now)
    acknowledged: bool = False
    acknowledged_by: str | None = None
    acknowledged_at: AwareDatetime | None = None
    correlation_id: str = Field(default_factory=lambda: f"corr-{uuid4().hex[:12]}")
    payload: dict[str, Any] = Field(default_factory=dict)
    handling_status: Literal["open", "in_progress", "pending_review", "closed"] = "open"
    assignee: str | None = None
    revision: int = 0
    handling_history: list[dict[str, Any]] = Field(default_factory=list)


class SafetySignal(Contract):
    """External edge inference or safety-system finding, not an actuator command."""
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    site_id: str = Field(min_length=1, max_length=80)
    source_device_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    category: Literal[
        "fire", "smoke", "person", "ppe_missing", "restricted_area",
        "temperature", "humidity", "light", "equipment", "other",
    ]
    severity: Literal["info", "warning", "critical"]
    title: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=500)
    occurred_at: AwareDatetime
    source_mode: SourceMode
    quality: Quality
    correlation_id: str = Field(min_length=1, max_length=128)
    rule_id: str = Field(min_length=1, max_length=80)
    model_version: str | None = Field(default=None, max_length=80)
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class CommandRequest(Contract):
    command_type: Literal["alarm_on", "alarm_off", "fan_on", "fan_off", "reset"]
    reason: str = Field(min_length=1, max_length=240)
    requested_by: str = Field(default="operator", min_length=1, max_length=80)
    request_id: str = Field(default_factory=lambda: f"request-{uuid4().hex}", min_length=1, max_length=128)


class CommandRecord(Contract):
    id: int | None = None
    command_id: str = Field(default_factory=lambda: f"cmd-{uuid4().hex[:12]}")
    request_id: str | None = None
    site_id: str = "demo-site"
    command_type: str
    reason: str
    requested_by: str
    status: Literal["accepted", "sent", "acknowledged", "completed", "timeout", "failed"] = "accepted"
    requested_at: datetime = Field(default_factory=utc_now)
    acknowledged_at: datetime | None = None
    completed_at: datetime | None = None
    execution_message: str | None = None
    source_mode: SourceMode = SourceMode.live
    correlation_id: str | None = None
    status_history: list[dict[str, Any]] = Field(default_factory=list)
    target_device_id: str = "actuator-001"


class Rules(Contract):
    version: str = "demo-fsm-1.0"
    watch_temperature: float = Field(default=34, ge=-40, le=120)
    alert_temperature: float = Field(default=42, ge=-40, le=120)
    danger_temperature: float = Field(default=65, ge=-40, le=125)
    dark_light: float = Field(default=18, ge=0, le=200000)
    normal_vision_threshold: float = Field(default=0.72, ge=0.1, le=1)
    alert_vision_threshold: float = Field(default=0.60, ge=0.1, le=1)
    rapid_rise_per_minute: float = Field(default=12, gt=0)
    high_humidity: float = Field(default=85, ge=0, le=100)
    bright_light: float = Field(default=10000, ge=0, le=200000)
    recovery_hold_seconds: float = Field(default=5, ge=0, le=120)

    @model_validator(mode="after")
    def ordered_thresholds(self) -> "Rules":
        if not self.watch_temperature < self.alert_temperature < self.danger_temperature:
            raise ValueError("temperature thresholds must be ordered WATCH < ALERT < DANGER")
        if self.alert_vision_threshold > self.normal_vision_threshold:
            raise ValueError("alert vision threshold cannot exceed normal threshold")
        return self


class Overview(Contract):
    app_name: str
    site_id: str
    simulation_mode: bool
    current_state: BusinessState
    state_since: datetime
    state_reason: str
    state_trusted: bool = False
    system_mode: SystemMode
    system_message: str
    generated_at: datetime
    stream_id: str
    stream_sequence: int
    rules: Rules
    vision_threshold: float
    devices: list[Device]
    latest_telemetry: list[TelemetryRecord]
    latest_detections: list[VisionDetection]
    active_events: list[EventRecord]
    recent_transitions: list[StateTransition]
    latest_commands: list[CommandRecord]
    fallback_active: bool = False
    state_evidence: dict[str, Any] = Field(default_factory=dict)
    mqtt_status: str = "disabled"
    local_fallback_ready: bool = False
    video_channels: list[VideoChannel] = Field(default_factory=list)


class ConfigResponse(Contract):
    stale_after_seconds: int
    heartbeat_timeout_seconds: int
    supported_states: list[BusinessState]
    supported_quality: list[Quality]
    supported_source_modes: list[SourceMode]
    simulation_mode: bool
    control_token_required: bool = False
    rules: Rules
    server_time: AwareDatetime = Field(default_factory=utc_now)
    site_id: str = "demo-site"
    device_token_required: bool = False
    allow_actuation: bool = False
    auto_actuation: bool = False
    command_timeout_seconds: int = 10
    mqtt_topics: dict[str, str] = Field(default_factory=dict)

