from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.app.api import create_app
from backend.app.hub import SentinelHub
from backend.app.models import CommandRecord, EventRecord, StateTransition


def _live_telemetry(event_id: str, *, site_id: str = "test-site", source_mode: str = "live", sequence: int = 1) -> dict:
    return {
        "event_id": event_id,
        "site_id": site_id,
        "device_id": "esp32-s3-001",
        "node_type": "ESP32-S3",
        "channel": "environment",
        "metric": "temperature",
        "value": 24.0,
        "unit": "°C",
        "event_time": datetime.now(timezone.utc).isoformat(),
        "ingest_time": datetime.now(timezone.utc).isoformat(),
        "sequence": sequence,
        "boot_id": "regression-boot",
        "quality": "valid",
        "source_mode": source_mode,
    }


def test_heartbeat_rejects_old_future_and_wrong_source(live_client: TestClient, device_headers: dict[str, str]) -> None:
    now = datetime.now(timezone.utc)
    payload = {"site_id": "test-site", "device_id": "esp32-s3-001", "event_time": now.isoformat(), "source_mode": "live"}
    assert live_client.post("/api/v1/ingest/heartbeat", json=payload, headers=device_headers).status_code == 202
    assert live_client.post("/api/v1/ingest/heartbeat", json=payload, headers=device_headers).status_code == 422
    old = {**payload, "event_time": (now - timedelta(seconds=20)).isoformat()}
    assert live_client.post("/api/v1/ingest/heartbeat", json=old, headers=device_headers).status_code == 422
    future = {**payload, "event_time": (now + timedelta(seconds=20)).isoformat()}
    assert live_client.post("/api/v1/ingest/heartbeat", json=future, headers=device_headers).status_code == 422
    replay = {**payload, "source_mode": "replay"}
    assert live_client.post("/api/v1/ingest/heartbeat", json=replay, headers=device_headers).status_code == 422


def test_delayed_heartbeat_is_not_falsely_reported_as_fresh(live_client: TestClient, device_headers: dict[str, str]) -> None:
    payload = {"site_id": "test-site", "device_id": "esp32-s3-001", "event_time": (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat(), "source_mode": "live"}
    assert live_client.post("/api/v1/ingest/heartbeat", json=payload, headers=device_headers).status_code == 202
    device = next(item for item in live_client.get("/api/v1/devices").json() if item["device_id"] == "esp32-s3-001")
    assert device["status"] == "delayed"


def test_history_and_audit_endpoints_are_scoped_to_site_and_source(live_settings) -> None:
    hub = SentinelHub(live_settings)
    hub.db.insert_telemetry({**_live_telemetry("foreign-site"), "site_id": "other-site"})
    hub.db.insert_telemetry({**_live_telemetry("foreign-source", sequence=2), "source_mode": "simulated"})
    foreign_event = EventRecord(
        event_id="foreign-event",
        site_id="other-site",
        event_type="test",
        title="foreign",
        message="foreign",
        source_mode="live",
    )
    hub.db.insert_json("events", "event_id", foreign_event.event_id, foreign_event.model_dump(mode="json"), "occurred_at", foreign_event.occurred_at)
    foreign_transition = StateTransition(site_id="other-site", from_state="NORMAL", to_state="WATCH", reason="foreign", source_mode="live")
    hub.db.insert_transition(foreign_transition.model_dump(mode="json"), foreign_transition.occurred_at)
    foreign_command = CommandRecord(command_id="foreign-command", request_id="foreign-request", site_id="other-site", command_type="fan_on", reason="foreign", requested_by="test", source_mode="live")
    hub.db.insert_json("commands", "command_id", foreign_command.command_id, foreign_command.model_dump(mode="json"), "requested_at", foreign_command.requested_at)

    with TestClient(create_app(live_settings)) as client:
        assert client.get("/api/v1/telemetry/history").json() == []
        assert client.get("/api/v1/events").json() == []
        assert client.get("/api/v1/state/transitions").json() == []
        assert client.get("/api/v1/commands").json() == []
        # The optional source selector cannot bypass the site boundary.
        rows = client.get("/api/v1/telemetry/history", params={"source_mode": "simulated"}).json()
        assert [row["event_id"] for row in rows] == ["foreign-source"]
        assert client.post("/api/v1/events/foreign-event/acknowledge", headers={"X-Control-Token": "control-secret"}).status_code == 404


def test_request_id_cannot_reuse_record_from_another_source_pipeline(live_settings) -> None:
    hub = SentinelHub(live_settings)
    command = CommandRecord(
        command_id="sim-command",
        request_id="shared-request",
        site_id="test-site",
        command_type="fan_on",
        reason="simulated request",
        requested_by="simulator",
        status="completed",
        source_mode="simulated",
    )
    hub.db.insert_json("commands", "command_id", command.command_id, command.model_dump(mode="json"), "requested_at", command.requested_at)
    hub.db.remember_request("shared-request", command.command_id)
    with TestClient(create_app(live_settings)) as client:
        response = client.post(
            "/api/v1/commands",
            json={"command_type": "fan_on", "reason": "simulated request", "requested_by": "simulator", "request_id": "shared-request"},
            headers={"X-Control-Token": "control-secret"},
        )
    assert response.status_code == 409
    assert "different site/source" in response.json()["detail"]


def test_restart_preserves_last_risk_without_marking_it_trusted(live_settings) -> None:
    first = SentinelHub(live_settings)
    transition = StateTransition(site_id="test-site", from_state="NORMAL", to_state="DANGER", reason="persisted danger", source_mode="live")
    first.db.insert_transition(transition.model_dump(mode="json"), transition.occurred_at)
    second = SentinelHub(live_settings)
    snapshot = second.overview()
    assert snapshot.current_state.value == "DANGER"
    assert snapshot.state_trusted is False
    assert snapshot.state_evidence["restart_pending"] is True


def test_automatic_live_actuation_requires_trusted_fresh_inputs(live_settings) -> None:
    settings = replace(live_settings, allow_actuation=True, auto_actuation=True)
    hub = SentinelHub(settings)
    calls: list[str] = []

    async def publisher(_command):
        calls.append("published")
        return True

    hub.command_publisher = publisher

    async def scenario():
        command = await hub.create_command("fan_on", "automatic test", "fsm-agent", "automatic-untrusted", automatic=True)
        assert command.status == "failed"
        assert "输入不完整" in (command.execution_message or "")
        assert calls == []

    asyncio.run(scenario())
