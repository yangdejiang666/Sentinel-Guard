from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import TelemetryRecord, VisionDetection


class DeviceAdapter(ABC):
    """Protocol-neutral contract for future physical device integrations."""

    @abstractmethod
    async def start(self) -> None:
        """Connect and start consuming device data."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop consuming and release resources."""

    @abstractmethod
    async def normalize_telemetry(self, payload: bytes | str | dict) -> TelemetryRecord:
        """Validate and normalize a sensor payload."""

    @abstractmethod
    async def normalize_detection(self, payload: bytes | str | dict) -> VisionDetection:
        """Validate and normalize an AI detection payload."""
