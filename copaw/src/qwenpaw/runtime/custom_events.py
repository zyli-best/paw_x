# -*- coding: utf-8 -*-
"""Request-scoped plugin events, independent of model and tool messages."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from concurrent.futures import Future
from contextlib import aclosing
from contextvars import ContextVar
from typing import Any, AsyncIterator

from ..schemas import CustomEvent

logger = logging.getLogger(__name__)

CUSTOM_EVENT_API_VERSION = 1
_current: ContextVar[RequestEvents | None] = ContextVar(
    "qwenpaw_custom_events", default=None,
)


class RequestEvents:
    """One outbox per execution; only the runtime stamps outgoing events.

    Model execution runs in a stable child task so an idle model/tool cannot
    prevent the request task from forwarding plugin events. Lifecycle hooks
    and task-control binding remain in the original request task.
    """

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.envelope = None
        self.pending: deque[CustomEvent] = deque()
        self.ready = asyncio.Event()
        self.closed = False
        self.accepted_count = 0
        self.published_count = 0

    def accept(self, event_type: str, data: Any) -> None:
        if self.closed or self.envelope is None:
            response = getattr(self.envelope, "response", None)
            logger.warning(
                "custom_event rejected type=%s response_id=%s session_id=%s reason=request_closed",
                event_type, getattr(response, "id", None),
                getattr(response, "session_id", None),
            )
            raise RuntimeError("The current request no longer accepts custom events")
        if not isinstance(event_type, str) or not event_type.strip():
            logger.warning(
                "custom_event rejected response_id=%s session_id=%s reason=invalid_type",
                self.envelope.response.id, self.envelope.response.session_id,
            )
            raise ValueError("Custom event type must be a non-empty string")
        # Validate JSON and detach mutable caller-owned data at acceptance.
        payload = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
        self.pending.append(CustomEvent(
            type=event_type, data=payload,
            response_id=self.envelope.response.id,
            session_id=self.envelope.response.session_id,
        ))
        self.accepted_count += 1
        self.ready.set()
        logger.info(
            "custom_event accepted type=%s response_id=%s session_id=%s pending=%s",
            event_type, self.envelope.response.id,
            self.envelope.response.session_id, len(self.pending),
        )

    def drain(self):
        while self.pending:
            yield self.pending.popleft()

    def publish(self, event: Any) -> Any:
        # Envelopes reuse mutable response/message objects across deltas.
        event = event.model_copy(deep=True)
        event.sequence_number = self.envelope.next_sequence_number()
        if isinstance(event, CustomEvent):
            self.published_count += 1
            logger.info(
                "custom_event published type=%s response_id=%s session_id=%s sequence_number=%s",
                event.type, event.response_id, event.session_id, event.sequence_number,
            )
        if event.object == "response" and event.status in (
            "completed", "failed", "cancelled",
        ):
            self.closed = True
            logger.info(
                "custom_event terminal response_id=%s session_id=%s status=%s "
                "sequence_number=%s accepted=%s published=%s pending=%s",
                event.id, event.session_id, event.status, event.sequence_number,
                self.accepted_count, self.published_count, len(self.pending),
            )
        return event

    async def interleave(self, source: AsyncIterator[Any]) -> AsyncIterator[Any]:
        normal: asyncio.Queue = asyncio.Queue(maxsize=1)
        finished = False
        error: BaseException | None = None

        async def produce():
            nonlocal finished, error
            try:
                async with aclosing(source):
                    async for event in source:
                        await normal.put(event.model_copy(deep=True))
                        self.ready.set()
            except BaseException as exc:
                error = exc
            finally:
                finished = True
                self.ready.set()

        producer = asyncio.create_task(produce())
        try:
            while True:
                for event in self.drain():
                    yield event
                if not normal.empty():
                    yield normal.get_nowait()
                    continue
                if finished:
                    if error is not None:
                        raise error
                    return
                self.ready.clear()
                await self.ready.wait()
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)


def _request_events() -> RequestEvents:
    events = _current.get()
    if events is None:
        logger.warning("custom_event rejected reason=no_active_request")
        raise RuntimeError("Custom events require an active Paw request")
    return events


async def emit_custom_event(*, type: str, data: Any) -> None:
    """Accept a JSON event for the current request, or raise if unavailable.

    Acceptance precedes transport delivery. The runtime forwards accepted
    events to the existing SSE buffer before its terminal response.
    """
    events = _request_events()
    if asyncio.get_running_loop() is events.loop:
        events.accept(type, data)
    else:
        await asyncio.wrap_future(_submit_from_thread(events, type, data))


def _submit_from_thread(events, event_type, data) -> Future:
    result: Future = Future()

    def accept():
        if not result.set_running_or_notify_cancel():
            return
        try:
            events.accept(event_type, data)
        except Exception as exc:
            result.set_exception(exc)
        else:
            result.set_result(None)

    events.loop.call_soon_threadsafe(accept)
    return result


def emit_custom_event_sync(*, type: str, data: Any) -> None:
    """Synchronous tool equivalent, including context-preserving worker threads."""
    events = _request_events()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is events.loop:
        events.accept(type, data)
    else:
        _submit_from_thread(events, type, data).result()
