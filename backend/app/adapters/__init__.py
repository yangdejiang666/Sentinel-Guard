"""Device adapter boundary.

Real MQTT, HTTP, serial or vendor SDK adapters should normalize their payloads
into the models in backend.app.models and call SentinelHub ingestion methods.
"""

from .base import DeviceAdapter

__all__ = ["DeviceAdapter"]
