from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import BusinessState, Rules, TelemetryRecord, VisionDetection


@dataclass(frozen=True)
class Decision:
    state: BusinessState
    reason: str
    evidence: dict[str, Any]
    trusted: bool


def decide(
    current: BusinessState,
    environment: dict[str, TelemetryRecord],
    detections: list[VisionDetection],
    rules: Rules,
    now: datetime,
    temperature_rise: float | None = None,
) -> Decision:
    """Pure, inspectable fusion rules. Callers supply ONLY fresh usable inputs.

    These are demonstration thresholds, not a certified fire-protection policy.
    Missing inputs never constitute evidence that an alarm has cleared.
    """
    values = {key: record.value for key, record in environment.items()}
    temperature, humidity, light = (values.get(key) for key in ("temperature", "humidity", "light"))
    by_view = {item.source_view: item for item in detections}
    threshold = rules.alert_vision_threshold if current in {BusinessState.alert, BusinessState.danger} else rules.normal_vision_threshold
    fire = max((item.confidence for item in detections if item.target == "fire"), default=0.0)
    smoke = max((item.confidence for item in detections if item.target == "smoke"), default=0.0)
    person = max((item.confidence for item in detections if item.target == "person"), default=0.0)
    warm = temperature is not None and temperature >= rules.alert_temperature
    rapid = temperature_rise is not None and temperature_rise >= rules.rapid_rise_per_minute
    environment_verified = temperature is not None and (temperature >= rules.danger_temperature or (warm and rapid))
    water_mist = humidity is not None and humidity >= rules.high_humidity and not warm and not rapid
    dual_fire = sum(item.target == "fire" and item.confidence >= threshold for item in detections) >= 2
    glare = light is not None and light >= rules.bright_light and not dual_fire and not environment_verified
    confirmed_fire = fire >= threshold and not glare
    effective_smoke = smoke if not water_mist else 0.0
    complete = all(key in environment for key in ("temperature", "humidity", "light")) and {"main", "k230"} <= by_view.keys()

    if confirmed_fire and environment_verified:
        target, reason = BusinessState.danger, "视觉火焰确诊 + 有效温度/升温率验证，满足双条件"
    elif warm or rapid or (fire >= rules.alert_vision_threshold and not glare) or effective_smoke >= threshold or (light is not None and light < rules.dark_light and person >= threshold):
        target, reason = BusinessState.alert, "视觉或环境出现异常，降低视觉阈值并进行多模态复核"
    elif (light is not None and light < rules.dark_light) or (temperature is not None and temperature >= rules.watch_temperature) or effective_smoke >= 0.4 or glare:
        target, reason = BusinessState.watch, "暗光、温度或视觉候选信号触发增强观测"
    else:
        target, reason = BusinessState.normal, "当前有效输入未触发演示预警规则"

    ranks = {state: index for index, state in enumerate(BusinessState)}
    if not complete and ranks[target] < ranks[current]:
        target, reason = current, "关键输入缺失或过期，保持上一风险等级；不能据此宣告恢复正常"
    if not complete:
        reason += "；数据不完整，状态可信度不足"

    evidence: dict[str, Any] = {
        "environment": {key: {
            "value": record.value, "unit": record.unit, "quality": record.quality.value,
            "device_id": record.device_id, "event_id": record.event_id,
            "event_time": record.event_time.isoformat(), "source_mode": record.source_mode.value,
        } for key, record in environment.items()},
        "vision": [{
            "device_id": item.device_id, "source_view": item.source_view,
            "target": item.target, "confidence": item.confidence,
            "event_id": item.event_id, "model_version": item.model_version,
            "event_time": item.event_time.isoformat(), "source_mode": item.source_mode.value,
        } for item in detections],
        "temperature_rise_per_minute": temperature_rise,
        "fire_confidence": fire, "smoke_confidence": smoke,
        "vision_threshold": threshold, "water_mist_excluded": water_mist,
        "reflection_candidate": glare, "environment_verified": environment_verified,
        "state_trusted": complete, "evaluated_at": now.isoformat(),
        "rule_version": rules.version,
    }
    return Decision(target, reason, evidence, complete)
