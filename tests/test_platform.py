from __future__ import annotations

from fastapi.testclient import TestClient


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
    assert len(payload["devices"]) == 5
    assert payload["local_fallback_ready"] is False
    assert {item["source_view"] for item in payload["video_channels"]} == {"main", "k230"}


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

