"""In-process event bus for SSE real-time updates."""
from __future__ import annotations

import asyncio
from typing import Any

_event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)


def publish(event_type: str, payload: Any):
    try:
        _event_queue.put_nowait({"type": event_type, "payload": payload})
    except asyncio.QueueFull:
        pass


def queue() -> asyncio.Queue:
    return _event_queue
