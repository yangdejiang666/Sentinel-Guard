from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from .models import SourceMode, VideoChannel


@dataclass(frozen=True)
class Frame:
    view: str
    device_id: str
    frame_id: str
    jpeg: bytes
    event_time: datetime
    ingest_time: datetime
    source_mode: SourceMode


class VideoStore:
    """Bounded preview frames; video is never silently persisted or uploaded."""
    def __init__(self, stale_after_seconds: int):
        self.stale_after_seconds = stale_after_seconds
        self.frames: dict[str, OrderedDict[str, Frame]] = {"k230": OrderedDict()}

    def put(self, frame: Frame) -> bool:
        channel = self.frames[frame.view]
        existing = channel.get(frame.frame_id)
        if existing:
            if existing.jpeg != frame.jpeg or existing.event_time != frame.event_time or existing.device_id != frame.device_id or existing.source_mode != frame.source_mode:
                raise ValueError("frame_id cannot be reused for different content")
            return False
        last = self.get(frame.view)
        if last and frame.event_time < last.event_time:
            raise ValueError("out-of-order video frame")
        channel[frame.frame_id] = frame
        while len(channel) > 6:
            channel.popitem(last=False)
        return True

    def get(self, view: str, frame_id: str | None = None) -> Frame | None:
        channel = self.frames[view]
        return channel.get(frame_id) if frame_id else next(reversed(channel.values()), None)

    def channels(self, now: datetime, source: SourceMode) -> list[VideoChannel]:
        result = []
        for view in self.frames:
            frame = self.get(view)
            result.append(VideoChannel(
                source_view=view,
                device_id=frame.device_id if frame else "k230-001",
                frame_id=frame.frame_id if frame else None,
                event_time=frame.event_time if frame else None,
                ingest_time=frame.ingest_time if frame else None,
                status=("fresh" if (now - frame.event_time).total_seconds() <= self.stale_after_seconds else "stale") if frame else "missing",
                source_mode=frame.source_mode if frame else source,
                frame_url=f"/api/v1/video/{view}/frame?frame_id={quote(frame.frame_id, safe='')}" if frame else None,
            ))
        return result
