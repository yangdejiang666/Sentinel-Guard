from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("SENTINEL_APP_NAME", "智卫 Sentinel")
    site_id: str = os.getenv("SENTINEL_SITE_ID", "demo-site")
    environment: str = os.getenv("SENTINEL_ENV", "development")
    simulation_mode: bool = os.getenv("SIMULATION_MODE", "true").lower() not in {"0", "false", "no"}
    database_path: str = os.getenv("SENTINEL_DATABASE", str(ROOT_DIR / "data" / "sentinel.sqlite3"))
    stale_after_seconds: int = int(os.getenv("SENTINEL_STALE_AFTER_SECONDS", "5"))
    heartbeat_timeout_seconds: int = int(os.getenv("SENTINEL_HEARTBEAT_TIMEOUT_SECONDS", "8"))
    simulation_interval_seconds: float = float(os.getenv("SENTINEL_SIMULATION_INTERVAL", "1"))
    control_token: str = os.getenv("SENTINEL_CONTROL_TOKEN", "")
    device_token: str = os.getenv("SENTINEL_DEVICE_TOKEN", "")
    clock_tolerance_seconds: int = int(os.getenv("SENTINEL_CLOCK_TOLERANCE_SECONDS", "5"))
    mqtt_host: str = os.getenv("SENTINEL_MQTT_HOST", "")
    mqtt_port: int = int(os.getenv("SENTINEL_MQTT_PORT", "1883"))
    mqtt_username: str = os.getenv("SENTINEL_MQTT_USERNAME", "")
    mqtt_password: str = os.getenv("SENTINEL_MQTT_PASSWORD", "")
    mqtt_tls: bool = os.getenv("SENTINEL_MQTT_TLS", "false").lower() in {"1", "true", "yes"}
    command_timeout_seconds: int = int(os.getenv("SENTINEL_COMMAND_TIMEOUT_SECONDS", "10"))
    allow_actuation: bool = os.getenv("SENTINEL_ALLOW_ACTUATION", "false").lower() in {"1", "true", "yes"}
    auto_actuation: bool = os.getenv("SENTINEL_AUTO_ACTUATION", "false").lower() in {"1", "true", "yes"}

    def __post_init__(self) -> None:
        if self.stale_after_seconds < 1 or self.heartbeat_timeout_seconds <= self.stale_after_seconds:
            raise ValueError("heartbeat timeout must exceed positive stale_after_seconds")
        if self.simulation_interval_seconds < 0.2:
            raise ValueError("simulation interval must be at least 0.2 seconds")
        if not 1 <= self.mqtt_port <= 65535 or self.command_timeout_seconds < 1:
            raise ValueError("invalid MQTT port or command timeout")
        if not self.simulation_mode and (not self.control_token or not self.device_token):
            raise ValueError("live mode requires SENTINEL_CONTROL_TOKEN and SENTINEL_DEVICE_TOKEN")
        if self.auto_actuation and not self.allow_actuation:
            raise ValueError("automatic physical actuation requires SENTINEL_ALLOW_ACTUATION=true")
        if self.clock_tolerance_seconds < 0 or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", self.site_id):
            raise ValueError("invalid clock tolerance or site id")


settings = Settings()
