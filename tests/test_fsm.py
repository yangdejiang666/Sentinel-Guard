from datetime import datetime, timedelta, timezone
import asyncio

from backend.app.fsm import decide
from backend.app.hub import SentinelHub
from backend.app.models import BusinessState, NodeType, Rules, TelemetryRecord, VisionDetection


def environment(temperature=23, humidity=45, light=180):
    return {name: TelemetryRecord(
        device_id="esp32-s3-001", node_type=NodeType.esp32, channel="environment",
        metric=name, value=value, unit=unit, event_time=datetime.now(timezone.utc), sequence=1,
    ) for name, value, unit in [("temperature", temperature, "°C"), ("humidity", humidity, "%"), ("light", light, "lux")]}


def vision(view, target="none", confidence=0):
    return VisionDetection(
        device_id="orange-pi-001" if view == "main" else "k230-001",
        camera_id="k230-001", source_view="k230",
        target=target, confidence=confidence, event_time=datetime.now(timezone.utc),
        sequence=1, model_version="test-model-1",
    )


def test_four_states_dynamic_threshold_and_evidence():
    rules, now = Rules(), datetime.now(timezone.utc)
    none = [vision("main"), vision("k230")]
    assert decide(BusinessState.normal, environment(), none, rules, now).state == BusinessState.normal
    assert decide(BusinessState.normal, environment(light=5), none, rules, now).state == BusinessState.watch
    assert decide(BusinessState.normal, environment(temperature=45), none, rules, now).state == BusinessState.alert
    danger = decide(BusinessState.alert, environment(temperature=70),
                    [vision("main", "fire", .9), vision("k230", "fire", .85)], rules, now)
    assert danger.state == BusinessState.danger and danger.trusted
    assert danger.evidence["environment_verified"] is True
    assert danger.evidence["vision_threshold"] == rules.alert_vision_threshold
    assert danger.evidence["rule_version"] == rules.version


def test_fire_without_environment_confirmation_is_not_danger():
    result = decide(BusinessState.normal, environment(), [vision("main", "fire", .99), vision("k230")], Rules(), datetime.now(timezone.utc))
    assert result.state == BusinessState.alert


def test_water_mist_and_reflection_candidates():
    now, rules = datetime.now(timezone.utc), Rules()
    mist = decide(BusinessState.normal, environment(humidity=95), [vision("main", "smoke", .9), vision("k230")], rules, now)
    assert mist.state == BusinessState.normal and mist.evidence["water_mist_excluded"]
    glare = decide(BusinessState.normal, environment(light=20000), [vision("main", "fire", .95), vision("k230")], rules, now)
    assert glare.state == BusinessState.watch and glare.evidence["reflection_candidate"]


def test_missing_inputs_cannot_clear_high_risk():
    result = decide(BusinessState.danger, {"temperature": environment()["temperature"]}, [], Rules(), datetime.now(timezone.utc))
    assert result.state == BusinessState.danger and not result.trusted
    assert "不能据此宣告恢复正常" in result.reason


def test_k230_onboard_inference_is_not_primary_or_independent_vote():
    now = datetime.now(timezone.utc)
    result = decide(BusinessState.danger, environment(), [vision("k230")], Rules(), now)
    assert result.state == BusinessState.danger and not result.trusted
    glare = decide(BusinessState.normal, environment(light=20000),
                   [vision("main", "fire", .95), vision("k230", "fire", .95)], Rules(), now)
    assert glare.state == BusinessState.watch and glare.evidence["reflection_candidate"]


def test_camera_offline_prevents_using_primary_inference(live_settings):
    hub = SentinelHub(live_settings)
    async def scenario():
        record = vision("main")
        record.site_id = "test-site"
        await hub.ingest_detection(record)
        assert not hub._fresh_and_usable(record, datetime.now(timezone.utc))
        camera = vision("k230")
        camera.site_id = "test-site"
        await hub.ingest_detection(camera)
        assert hub._fresh_and_usable(record, datetime.now(timezone.utc))
        hub.devices["k230-001"].last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=30)
        assert not hub._fresh_and_usable(record, datetime.now(timezone.utc))
    asyncio.run(scenario())


def test_recovery_requires_continuous_complete_inputs(live_settings):
    hub = SentinelHub(live_settings)
    async def scenario():
        hub.state = BusinessState.danger
        for record in environment().values():
            record.site_id = "test-site"
            await hub.ingest_telemetry(record)
        for view in ("main", "k230"):
            record = vision(view)
            record.site_id = "test-site"
            await hub.ingest_detection(record)
        await hub.evaluate_state()
        assert hub.state == BusinessState.danger and hub._recovery_target == BusinessState.normal
        hub._recovery_since = datetime.now(timezone.utc) - timedelta(seconds=6)
        await hub.evaluate_state()
        assert hub.state == BusinessState.normal
    asyncio.run(scenario())
