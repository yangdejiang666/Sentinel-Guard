from __future__ import annotations

from fastapi.testclient import TestClient
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from backend.app.db import Database
from backend.app.models import EventRecord


def test_dashboard_health_config_and_security_headers(sim_client: TestClient) -> None:
    health = sim_client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["simulation_mode"] is True

    dashboard = sim_client.get("/")
    assert dashboard.status_code == 200
    assert "智卫 Sentinel" in dashboard.text
    assert "frame-ancestors 'none'" in dashboard.headers["content-security-policy"]
    assert dashboard.headers["x-content-type-options"] == "nosniff"
    assert sim_client.get("/static/app.js").status_code == 200

    config = sim_client.get("/api/v1/config")
    assert config.status_code == 200
    assert config.json()["site_id"] == "test-site"
    assert config.json()["allow_actuation"] is False
    assert "telemetry" in config.json()["mqtt_topics"]

def test_safety_signal_ingest_is_auditable_idempotent_and_does_not_actuate(sim_client: TestClient) -> None:
    signal = {
        "event_id": "safety-signal-1", "site_id": "test-site",
        "source_device_id": "orange-pi-001", "category": "temperature",
        "severity": "warning", "title": "温度异常趋势", "message": "需要现场复核",
        "occurred_at": datetime.now(timezone.utc).isoformat(), "source_mode": "simulated",
        "quality": "simulated", "correlation_id": "corr-temp-1",
        "rule_id": "trend-v1", "model_version": "orange-pi-model-v1",
        "confidence": 0.84, "evidence": {"telemetry_event_ids": ["sensor-1"]},
    }
    with sim_client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        response = sim_client.post("/api/v1/ingest/anomalies", json=signal)
        assert response.status_code == 202
        event = response.json()
        assert websocket.receive_json()["type"] == "event"
        assert event["event_type"] == "anomaly"
        assert event["payload"]["evidence"] == signal["evidence"]
        assert sim_client.post("/api/v1/ingest/anomalies", json=signal).json()["event_id"] == event["event_id"]
        assert sim_client.get("/api/v1/events/safety-signal-1").json()["payload"]["rule_id"] == "trend-v1"
    assert sim_client.get("/api/v1/commands").json() == []
    assert sim_client.post("/api/v1/ingest/anomalies", json={**signal, "message": "改变内容"}).status_code == 422
    acknowledged = sim_client.post(
        "/api/v1/events/safety-signal-1/acknowledge", json={"acknowledged_by": "test-operator"}
    ).json()
    replay = sim_client.post("/api/v1/ingest/anomalies", json=signal).json()
    assert replay == acknowledged
    alert = {**signal, "event_id": "industrial-1", "category": "ppe_missing", "severity": "critical"}
    assert sim_client.post("/api/v1/ingest/industrial-alerts", json=alert).json()["event_type"] == "industrial_alert"
    assert sim_client.get("/api/v1/events?limit=10").json()[0]["event_id"] == "industrial-1"


def test_safety_signal_rejects_untrusted_sources_and_stale_data(sim_client: TestClient) -> None:
    signal = {
        "event_id": "safety-invalid-1", "site_id": "test-site",
        "source_device_id": "orange-pi-001", "category": "fire",
        "severity": "critical", "title": "火情", "message": "待复核",
        "occurred_at": datetime.now(timezone.utc).isoformat(), "source_mode": "simulated",
        "quality": "simulated", "correlation_id": "corr-fire-1", "rule_id": "rule-1",
    }
    for changed in (
        {"site_id": "another-site"}, {"source_device_id": "unknown-device"},
        {"source_mode": "live"}, {"quality": "estimated"},
        {"confidence": 1.1}, {"category": "unrecognized"},
        {"occurred_at": "2020-01-01T00:00:00Z"},
    ):
        assert sim_client.post("/api/v1/ingest/anomalies", json={**signal, **changed}).status_code == 422
    assert sim_client.get("/api/v1/events/safety-invalid-1").status_code == 404


@pytest.mark.parametrize("endpoint", ["anomalies", "industrial-alerts"])
def test_live_safety_signal_requires_token_and_valid_quality(live_client, device_headers, endpoint) -> None:
    signal = {
        "event_id": "live-safety-1", "site_id": "test-site",
        "source_device_id": "orange-pi-001", "category": "equipment",
        "severity": "critical", "title": "设备异常", "message": "待人工复核",
        "occurred_at": datetime.now(timezone.utc).isoformat(), "source_mode": "live",
        "quality": "valid", "correlation_id": "corr-equipment-1", "rule_id": "equipment-v1",
    }
    path = f"/api/v1/ingest/{endpoint}"
    assert live_client.post(path, json=signal).status_code == 401
    for changed in (
        {"quality": "simulated"}, {"quality": "invalid"}, {"source_mode": "replay"},
        {"occurred_at": "2099-01-01T00:00:00Z"},
    ):
        assert live_client.post(path, json={**signal, **changed}, headers=device_headers).status_code == 422
    before = live_client.get("/api/v1/overview").json()["current_state"]
    assert live_client.post(path, json=signal, headers=device_headers).status_code == 202
    assert live_client.get("/api/v1/overview").json()["current_state"] == before
    assert live_client.get("/api/v1/commands").json() == []


def test_event_detail_does_not_leak_other_sites_or_modes(sim_client) -> None:
    hub = sim_client.app.state.hub
    for event_id, site_id, source_mode in (
        ("other-site", "another-site", "simulated"), ("other-mode", "test-site", "live"),
    ):
        event = EventRecord(event_id=event_id, site_id=site_id, source_mode=source_mode,
                            event_type="industrial_alert", severity="critical",
                            title="受限事件", message="受限证据")
        hub.db.insert_safety_event(event.model_dump(mode="json"))
        assert sim_client.get(f"/api/v1/events/{event_id}").status_code == 404
        assert sim_client.post(f"/api/v1/events/{event_id}/acknowledge").status_code == 404


def test_safety_event_atomic_deduplication_across_connections(tmp_path) -> None:
    path = str(tmp_path / "atomic-events.sqlite3")
    stores = [Database(path), Database(path)]
    event = EventRecord(event_id="atomic-safety", event_type="anomaly", severity="warning",
                        title="异常点", message="待复核").model_dump(mode="json")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda store: store.insert_safety_event(event), stores))
    assert sum(inserted for _, inserted in results) == 1
    assert results[0][0] == results[1][0]
    assert len(stores[0].list_json("events")) == 1
    with pytest.raises(ValueError, match="conflicts"):
        stores[1].insert_safety_event({**event, "message": "不同内容"})

def test_overview_contains_traceable_simulated_data(sim_client: TestClient) -> None:
    response = sim_client.get("/api/v1/overview")
    assert response.status_code == 200
    payload = response.json()
    assert payload["simulation_mode"] is True
    assert payload["current_state"] in {"NORMAL", "WATCH", "ALERT", "DANGER"}
    assert {item["metric"] for item in payload["latest_telemetry"]} >= {"temperature", "humidity", "light"}
    assert all(item["source_mode"] == "simulated" for item in payload["latest_telemetry"])
    assert all(item["quality"] == "simulated" for item in payload["latest_telemetry"])
    assert all(item["event_id"] and item["event_time"] and item["boot_id"] for item in payload["latest_telemetry"])
    assert len(payload["devices"]) == 4
    assert payload["local_fallback_ready"] is False
    assert {item["source_view"] for item in payload["video_channels"]} == {"k230"}
    assert {item["device_id"] for item in payload["latest_detections"]} == {"k230-001", "orange-pi-001"}
    assert {item["camera_id"] for item in payload["latest_detections"]} == {"k230-001"}


def test_retired_camera_routes_reject_new_data(live_client, device_headers, control_headers):
    detection = {
        "site_id": "test-site", "device_id": "orange-pi-001", "camera_id": "camera-main-001",
        "source_view": "main", "target": "none", "confidence": 0,
        "event_time": datetime.now(timezone.utc).isoformat(), "sequence": 1,
        "model_version": "test-primary",
    }
    assert live_client.post("/api/v1/ingest/detections", json=detection, headers=device_headers).status_code == 422
    assert live_client.get("/api/v1/video/main/frame").status_code == 404
    assert live_client.post("/api/v1/devices", headers=control_headers, json={
        "device_id": "extra-camera", "display_name": "Second camera", "node_type": "K230",
        "capabilities": ["video-k230"], "protocol": "HTTP",
    }).status_code == 409


def test_history_shape_and_time_range_validation(sim_client: TestClient) -> None:
    response = sim_client.get("/api/v1/telemetry/history", params={"metric": "temperature", "limit": 20})
    assert response.status_code == 200
    rows = response.json()
    assert rows
    assert all(row["metric"] == "temperature" for row in rows)
    assert all(row["event_id"] and row["event_time"] and row["ingest_time"] for row in rows)

    invalid_range = sim_client.get(
        "/api/v1/telemetry/history",
        params={"start": "2026-10-08T10:01:00Z", "end": "2026-10-08T10:00:00Z"},
    )
    assert invalid_range.status_code == 422


def test_simulated_command_is_idempotent_and_never_claims_physical_action(sim_client: TestClient) -> None:
    body = {
        "command_type": "fan_on",
        "reason": "automated test",
        "requested_by": "pytest",
        "request_id": "same-request",
    }
    first = sim_client.post("/api/v1/commands", json=body)
    second = sim_client.post("/api/v1/commands", json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["command_id"] == second.json()["command_id"]
    assert first.json()["status"] == "completed"
    assert first.json()["source_mode"] == "simulated"
    assert "未驱动物理硬件" in first.json()["execution_message"]

    conflict = sim_client.post("/api/v1/commands", json={**body, "command_type": "alarm_on"})
    assert conflict.status_code == 409


def test_reserved_ingest_rejects_unknown_device(sim_client: TestClient) -> None:
    response = sim_client.post(
        "/api/v1/ingest/telemetry",
        json={
            "device_id": "unknown-board",
            "node_type": "ESP32-S3",
            "channel": "environment",
            "metric": "temperature",
            "value": 20,
            "unit": "°C",
            "event_time": "2026-10-08T10:00:00Z",
            "sequence": 1,
            "source_mode": "simulated",
        },
    )
    assert response.status_code == 422


def test_websocket_starts_with_authoritative_snapshot(sim_client: TestClient) -> None:
    with sim_client.websocket_connect("/ws") as websocket:
        message = websocket.receive_json()
        assert message["type"] == "snapshot"
        assert message["stream_id"]
        assert isinstance(message["stream_sequence"], int)
        assert message["data"]["current_state"] in {"NORMAL", "WATCH", "ALERT", "DANGER"}

