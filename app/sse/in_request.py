from __future__ import annotations

from collections.abc import AsyncIterator

from starlette.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
KEEPALIVE_INTERVAL_SECONDS = 15
MAX_STREAM_SECONDS = 120


def format_sse_event(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def format_sse_comment(comment: str = "keep-alive") -> str:
    return f": {comment}\n\n"


def sse_response(event_source: AsyncIterator[str]) -> StreamingResponse:
    """Transport for in-request streaming (architecture/04 §SSE): headers only —
    the keep-alive comment cadence and the 120s hard cap are the caller's
    responsibility inside `event_source`. `sse/bridge.py` (Phase 3) wraps this same
    helper for the SSE-over-jobs pattern, which polls `run_events` instead of
    streaming directly from this process.
    """
    return StreamingResponse(event_source, media_type="text/event-stream", headers=SSE_HEADERS)
