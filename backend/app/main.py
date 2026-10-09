"""Stable ASGI entrypoint; application factory lives in api.py."""

from .api import app, create_app

__all__ = ["app", "create_app"]

