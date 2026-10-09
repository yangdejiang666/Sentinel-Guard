from datetime import datetime, timedelta, timezone

import pytest


def telemetry(event_id="telemetry-a", sequence=1, **updates):
    return {
        "event_id": event_id, "site_id": "test-site", "device_id": "esp32-s3-001",
        "node_type": "ESP32-S3", "channel": "environment", "metric": "temperature",
        "value": 24, "unit": "°C", "event_time": datetime.now(timezone.utc).isoformat(),
        "sequence": sequence, "boot_id": "boot-a", "quality": "valid", "source_mode": "live",
        **updates,
    }


def detection(event_id="detection-a", sequence=1, **updates):
    return {
        "event_id": event_id, "site_id": "test-site", "device_id": "k230-001",
        "camera_id": "k230-001", "source_view": "k230", "target": "none",
        "confidence": 0, "bbox": [0.2, 0.2, 0.4, 0.4],
        "event_time": datetime.now(timezone.utc).isoformat(), "sequence": sequence,
        "boot_id": "vision-boot", "quality": "valid", "source_mode": "live",
        "model_version": "test-model-1", **updates,
    }


def test_duplicates_ordering_and_boot_reset(live_client, device_headers):
    first = telemetry(sequence=10)
    post = lambda payload: live_client.post("/api/v1/ingest/telemetry", json=payload, headers=device_headers)
    assert post(first).json()["quality"] == "valid"
    duplicate = post(first)
    assert duplicate.json()["quality"] == "invalid"
    assert duplicate.json()["quality_reason"] == "duplicate_event_id_ignored"
    out_of_order = post(telemetry("old-seq", 9, value=99))
    assert out_of_order.json()["quality"] == "invalid"
    assert "out_of_order_sequence" in out_of_order.json()["quality_reason"]
    reboot = post(telemetry("reboot", 1, boot_id="boot-b", value=25))
    assert reboot.json()["quality"] == "valid"
    latest = live_client.get("/api/v1/telemetry/latest").json()
    assert latest[0]["event_id"] == "reboot" and latest[0]["value"] == 25


@pytest.mark.parametrize("quality,age,expected", [
    ("valid", 8, "stale"), ("valid", -10, "invalid"),
    ("estimated", 0, "estimated"), ("invalid", 0, "invalid"),
    ("stale", 0, "stale"), ("simulated", 0, "invalid"),
])
def test_unusable_inputs_do_not_replace_valid_latest(live_client, device_headers, quality, age, expected):
    post = lambda payload: live_client.post("/api/v1/ingest/telemetry", json=payload, headers=device_headers)
    assert post(telemetry()).json()["quality"] == "valid"
    result = post(telemetry("unusable", 1, boot_id="other-boot", value=90, quality=quality,
                            event_time=(datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()))
    assert result.status_code == 202
    assert result.json()["quality"] == expected
    assert live_client.get("/api/v1/telemetry/latest").json()[0]["value"] == 24


@pytest.mark.parametrize("updates", [
    {"unit": "%"}, {"value": 200}, {"device_id": "unknown-board"},
    {"source_mode": "simulated"}, {"source_mode": "replay"}, {"site_id": "wrong-site"},
    {"node_type": "K230"}, {"metric": "unregistered_metric"}, {"unexpected_field": True},
    {"event_time": "2026-10-08T12:00:00"},
])
def test_contract_and_source_validation(live_client, device_headers, updates):
    assert live_client.post("/api/v1/ingest/telemetry", json=telemetry(**updates), headers=device_headers).status_code == 422


def test_live_ingest_requires_device_token(live_client):
    assert live_client.post("/api/v1/ingest/telemetry", json=telemetry()).status_code == 401


def test_camelcase_contract_and_server_owned_ingest_time(live_client, device_headers):
    body = {
        "eventId": "camel-case", "siteId": "test-site", "deviceId": "esp32-s3-001",
        "nodeType": "ESP32-S3", "channel": "environment", "metric": "humidity",
        "value": 45, "unit": "%", "eventTime": datetime.now(timezone.utc).isoformat(),
        "ingestTime": "2000-01-01T00:00:00Z", "sequence": 1, "bootId": "boot",
        "quality": "valid", "sourceMode": "live",
    }
    response = live_client.post("/api/v1/ingest/telemetry", json=body, headers=device_headers)
    assert response.status_code == 202
    assert response.json()["event_id"] == "camel-case"
    assert not response.json()["ingest_time"].startswith("2000-")


@pytest.mark.parametrize("updates", [
    {"model_version": ""}, {"bbox": [0.8, 0.8, 0.4, 0.4]},
    {"camera_id": "camera-main-001"}, {"source_view": "main"}, {"confidence": 1.1},
])
def test_vision_metadata_is_strict(live_client, device_headers, updates):
    assert live_client.post("/api/v1/ingest/detections", json=detection(**updates), headers=device_headers).status_code == 422


def test_none_proves_visual_liveness_and_stale_detection_cannot_replace_it(live_client, device_headers):
    post = lambda payload: live_client.post("/api/v1/ingest/detections", json=payload, headers=device_headers)
    missing = detection()
    missing.pop("model_version")
    assert post(missing).status_code == 422
    assert post(detection()).status_code == 202
    stale = post(detection("stale-fire", 1, boot_id="another", target="fire", confidence=1,
                           event_time=(datetime.now(timezone.utc) - timedelta(seconds=8)).isoformat()))
    assert stale.json()["quality"] == "stale"
    overview = live_client.get("/api/v1/overview").json()
    assert overview["latest_detections"][0]["target"] == "none"
    assert overview["current_state"] != "DANGER"


def test_new_sensor_can_be_registered_without_changing_frontend(live_client, control_headers, device_headers):
    registration = {
        "device_id": "gas-sensor-001", "display_name": "扩展气体节点", "node_type": "ESP32-S3",
        "protocol": "HTTP", "capabilities": ["co_ppm", "heartbeat"],
    }
    assert live_client.post("/api/v1/devices", json=registration).status_code == 401
    assert live_client.post("/api/v1/devices", json=registration, headers=control_headers).status_code == 201
    assert live_client.post("/api/v1/devices", json=registration, headers=control_headers).status_code == 409
    result = live_client.post("/api/v1/ingest/telemetry", json=telemetry(
        device_id="gas-sensor-001", metric="co_ppm", value=3.2, unit="ppm"), headers=device_headers)
    assert result.status_code == 202
    assert any(row["metric"] == "co_ppm" for row in live_client.get("/api/v1/telemetry/latest").json())
