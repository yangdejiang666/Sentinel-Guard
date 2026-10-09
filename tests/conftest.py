from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from backend.app.api import create_app
from backend.app.config import Settings


def make_settings(tmp_path, *, simulation_mode: bool, **overrides) -> Settings:
    values = {
        "app_name": "智卫 Sentinel Test",
        "site_id": "test-site",
        "environment": "test",
        "simulation_mode": simulation_mode,
        "database_path": str(tmp_path / ("sim.sqlite3" if simulation_mode else "live.sqlite3")),
        "stale_after_seconds": 5,
        "heartbeat_timeout_seconds": 8,
        "simulation_interval_seconds": 60,
        "clock_tolerance_seconds": 2,
        "control_token": "" if simulation_mode else "control-secret",
        "device_token": "" if simulation_mode else "device-secret",
        "mqtt_host": "",
        "mqtt_port": 1883,
        "mqtt_username": "",
        "mqtt_password": "",
        "mqtt_tls": False,
        "command_timeout_seconds": 10,
        "allow_actuation": False,
        "auto_actuation": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def sim_settings(tmp_path) -> Settings:
    return make_settings(tmp_path, simulation_mode=True)


@pytest.fixture
def live_settings(tmp_path) -> Settings:
    return make_settings(tmp_path, simulation_mode=False)


@pytest.fixture
def sim_client(sim_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(sim_settings)) as client:
        yield client


@pytest.fixture
def live_client(live_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(live_settings)) as client:
        yield client


@pytest.fixture
def device_headers() -> dict[str, str]:
    return {"X-Device-Token": "device-secret"}


@pytest.fixture
def control_headers() -> dict[str, str]:
    return {"X-Control-Token": "control-secret"}
