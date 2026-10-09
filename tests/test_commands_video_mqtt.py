import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.adapters.mqtt import MQTTAdapter
from backend.app.hub import SentinelHub
from backend.app.models import CommandFeedback
from backend.app.video import Frame, VideoStore
from backend.app.models import SourceMode


def test_live_control_token_and_default_safe_failure(live_client, control_headers):
    body = {"command_type": "fan_on", "reason": "operator test", "request_id": "live-safe"}
    assert live_client.post("/api/v1/commands", json=body).status_code == 401
    result = live_client.post("/api/v1/commands", json=body, headers=control_headers)
    assert result.status_code == 201 and result.json()["status"] == "failed"
    assert "安全开关禁止" in result.json()["execution_message"]
    assert "未驱动物理硬件" in result.json()["execution_message"]


def test_sent_feedback_terminal_guard_and_timeout(live_settings):
    hub = SentinelHub(replace(live_settings, allow_actuation=True, command_timeout_seconds=1))
    async def scenario():
        async def publish(_): return True
        hub.command_publisher = publish
        command = await hub.create_command("alarm_on", "feedback test", "pytest", "feedback-request")
        assert command.status == "sent" and command.completed_at is None
        def feedback(status, **updates):
            return CommandFeedback(site_id="test-site", command_id=command.command_id,
                device_id="actuator-001", status=status, event_time=datetime.now(timezone.utc),
                execution_message="hardware feedback", source_mode="live", **updates)
        assert (await hub.apply_command_feedback(feedback("acknowledged"))).status == "acknowledged"
        assert (await hub.apply_command_feedback(feedback("completed"))).status == "completed"
        with pytest.raises(ValueError, match="terminal"):
            await hub.apply_command_feedback(feedback("failed"))
        expiring = await hub.create_command("fan_on", "timeout test", "pytest", "timeout-request")
        payload = hub.db.get_json_event("commands", expiring.command_id)
        old = datetime.now(timezone.utc) - timedelta(seconds=3)
        payload["requested_at"] = old.isoformat()
        payload["status_history"] = [{"status": "sent", "at": old.isoformat()}]
        hub.db.save_command(payload)
        await hub.expire_commands()
        assert hub.db.get_json_event("commands", expiring.command_id)["status"] == "timeout"
        with pytest.raises(ValueError, match="terminal"):
            await hub.apply_command_feedback(CommandFeedback(site_id="test-site", command_id=expiring.command_id,
                device_id="actuator-001", status="completed", event_time=datetime.now(timezone.utc),
                execution_message="late hardware feedback", source_mode="live"))
    asyncio.run(scenario())


def test_video_identity_content_order_and_cache(live_client, device_headers):
    now = datetime.now(timezone.utc)
    params = {"device_id": "k230-001", "frame_id": "frame-1", "event_time": now.isoformat(), "source_mode": "live"}
    headers = {**device_headers, "Content-Type": "image/jpeg"}
    jpeg = b"\xff\xd8\xff\xd9"
    post = lambda content=jpeg, query=params, hdr=headers: live_client.post("/api/v1/ingest/video/k230", params=query, content=content, headers=hdr)
    assert post().json()["inserted"] is True
    assert post().json()["inserted"] is False
    assert post(b"\xff\xd8\x01\xff\xd9").status_code == 422
    assert post(query={**params, "frame_id": "older", "event_time": (now - timedelta(seconds=1)).isoformat()}).status_code == 422
    assert post(hdr=device_headers).status_code == 415
    assert post(b"not-a-jpeg", query={**params, "frame_id": "bad"}).status_code == 422
    frame = live_client.get("/api/v1/video/k230/frame", params={"frame_id": "frame-1"})
    assert frame.status_code == 200 and frame.content == jpeg
    assert frame.headers["cache-control"].startswith("no-store")
    channels = live_client.get("/api/v1/video/channels").json()
    assert next(item for item in channels if item["source_view"] == "k230")["status"] == "fresh"


def test_video_store_is_bounded_and_marks_stale():
    store = VideoStore(5)
    now = datetime.now(timezone.utc)
    for i in range(10):
        store.put(Frame("k230", "k230-001", str(i), b"test", now + timedelta(milliseconds=i), now, SourceMode.live))
    assert len(store.frames["k230"]) == 6 and store.get("k230").frame_id == "9"
    assert store.channels(now + timedelta(seconds=6), SourceMode.live)[0].status == "stale"


def test_mqtt_topic_binding_retained_and_simulation_guard(live_settings, sim_settings):
    hub = SentinelHub(live_settings)
    adapter = MQTTAdapter(hub, live_settings)
    payload = {"event_id": "mqtt-1", "site_id": "test-site", "device_id": "esp32-s3-001",
        "node_type": "ESP32-S3", "channel": "environment", "metric": "temperature", "value": 24.2,
        "unit": "°C", "event_time": datetime.now(timezone.utc).isoformat(), "sequence": 1,
        "boot_id": "mqtt-boot", "quality": "valid", "source_mode": "live"}
    async def scenario():
        await adapter.route("sentinel/test-site/telemetry/esp32-s3-001", payload)
        assert hub.latest["temperature"].event_id == "mqtt-1"
        with pytest.raises(ValueError, match="retained"):
            await adapter.route("sentinel/test-site/telemetry/esp32-s3-001", payload, retained=True)
        with pytest.raises(ValueError, match="differs"):
            await adapter.route("sentinel/test-site/telemetry/k230-001", payload)
        with pytest.raises(ValueError, match="site"):
            await adapter.route("sentinel/other-site/telemetry/esp32-s3-001", payload)
        with pytest.raises(ValueError, match="simulation"):
            await MQTTAdapter(SentinelHub(sim_settings), sim_settings).route("sentinel/test-site/telemetry/esp32-s3-001", payload)
    asyncio.run(scenario())
