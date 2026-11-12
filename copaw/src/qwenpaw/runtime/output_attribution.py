"""Attribute output to guidance present in its actual model request.

This observer never drains steering queues or changes inference scheduling.
Frames belong to individual model calls, not the latest HTTP receipt.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class OutputFrame:
    response_id: str | None
    steer_message_id: str | None = None


_frame: ContextVar[OutputFrame | None] = ContextVar("steer_output_frame", default=None)


@contextmanager
def output_scope(frame: OutputFrame | None):
    token = _frame.set(frame)
    try:
        yield
    finally:
        _frame.reset(token)


def current_frame() -> OutputFrame | None:
    return _frame.get()


def capture_model_input(messages) -> None:
    """Observe native messages after preparation, excluding historical runs."""
    frame = current_frame()
    if frame is None or not frame.response_id:
        return
    latest = None
    for message in messages:
        metadata = getattr(message, "metadata", None) or {}
        if (
            getattr(message, "role", None) == "user"
            and metadata.get("qwenpaw_message_kind")
            in {"user_steering", "edge_cloud_steering"}
            and metadata.get("response_id") == frame.response_id
            and isinstance(metadata.get("sequence"), int)
            and isinstance(metadata.get("message_id"), str)
        ):
            if latest is None or metadata["sequence"] > latest["sequence"]:
                latest = metadata
    frame.steer_message_id = latest["message_id"] if latest is not None else None


def steer_message_id(message) -> str | None:
    return (getattr(message, "metadata", None) or {}).get("steer_message_id")


def tag_output(message, identity: str | None):
    if identity:
        message.metadata = {
            **(getattr(message, "metadata", None) or {}),
            "steer_message_id": identity,
        }
    return message


async def attributed_events(source, frame):
    """Keep a scope within each anext: heartbeat resumes in different tasks."""
    iterator = source.__aiter__()
    try:
        while True:
            with output_scope(frame):
                try:
                    event = await anext(iterator)
                except StopAsyncIteration:
                    return
            yield tag_output(event, frame.steer_message_id)
    finally:
        with output_scope(frame):
            await iterator.aclose()
