"""Ordinary-run sampling preemption. Never cancel the owning reply task."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from collections.abc import AsyncIterable

from agentscope.middleware import MiddlewareBase


class SteerPreempted(BaseException):
    """Private control flow; SDK/provider Exception retries must not catch it."""


@dataclass
class SamplingPreempted:
    anchor_id: str = ""


@dataclass
class SamplingAttempt:
    sequence: int
    steer_message_id: str | None
    complete: bool = False
    closers: list = field(default_factory=list)
    requests: list = field(default_factory=list)

    def finish(self, status):
        for request in self.requests:
            request["status"] = status

    async def close(self):
        callbacks, self.closers = self.closers, []
        for callback in callbacks:
            await callback()


_normal: ContextVar[object | None] = ContextVar("normal_steer_request", default=None)


def current_normal():
    return _normal.get()


def retain_stream(response):
    """Own the HTTP stream even if the SDK suppresses task cancellation."""
    closed = False

    async def close():
        nonlocal closed
        if not closed:
            closed = True
            callback = getattr(response, "close", None) or getattr(
                response, "aclose", None
            )
            if callback is not None:
                await callback()

    steering = current_normal()
    if steering is not None and steering.attempt is not None:
        steering.attempt.closers.append(close)
    return close


def superseded_response(call_id):
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.tool import ToolResponse

    return ToolResponse(
        id=call_id,
        state=ToolResultState.DENIED,
        content=[TextBlock(text='{"executed":false,"reason":"superseded_by_steer"}')],
        metadata={"steer_superseded": True},
    )


@contextmanager
def normal_scope(steering):
    token = _normal.set(steering)
    try:
        yield
    finally:
        _normal.reset(token)


async def interruptible(operation, steering, sequence):
    """Race only a model wait, with cancellation and waiter cleanup awaited."""
    work = asyncio.ensure_future(operation)
    wake = asyncio.create_task(steering.wait_after(sequence))
    try:
        await asyncio.wait({work, wake}, return_when=asyncio.FIRST_COMPLETED)
        if steering.phase == "closing":
            raise asyncio.CancelledError()
        # A response already delivered in this tick wins. Tool admission still
        # checks the new sequence before any complete call can start.
        if work.done():
            return work.result()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        raise SteerPreempted()
    finally:
        for task in (work, wake):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, wake, return_exceptions=True)


class NormalModelCallMiddleware(MiddlewareBase):
    """Innermost, scoped to one ordinary agent model invocation."""

    def __init__(self, steering):
        self.steering = steering

    async def _wait(self, operation, sequence):
        # Scope actual main-model dispatch only. Scroll and auxiliary model
        # calls before this middleware keep their existing protocol behavior.
        with normal_scope(self.steering):
            return await interruptible(operation, self.steering, sequence)

    async def _close(self, attempt, iterator=None):
        with normal_scope(self.steering):
            try:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    await close()
            finally:
                await attempt.close()

    async def on_model_call(self, agent, input_kwargs, next_handler):
        steering = self.steering
        if steering.prepare_model_call is not None:
            # Approval/redaction preparation is not an interruptible model wait.
            _, prepared = await steering.prepare_model_call(
                (),
                {
                    key: value
                    for key, value in input_kwargs.items()
                    if key != "current_model"
                },
            )
            input_kwargs = {**input_kwargs, **prepared}
        attempt = steering.begin_sampling(input_kwargs["messages"])
        steering.model_name = str(
            getattr(input_kwargs.get("current_model"), "model", "")
        ).lower()
        if steering.phase == "closing":
            raise asyncio.CancelledError()
        if steering.latest_sequence > attempt.sequence:
            raise SteerPreempted()
        try:
            response = await self._wait(
                next_handler(**input_kwargs),
                attempt.sequence,
            )
        except BaseException as exc:
            attempt.finish(
                "steered"
                if isinstance(exc, SteerPreempted)
                else "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            )
            await self._close(attempt)
            raise
        if not isinstance(response, AsyncIterable):
            steering.complete_sampling(attempt, response)
            attempt.finish("completed")
            await self._close(attempt)
            return response

        async def stream():
            iterator = response.__aiter__()
            status = "cancelled"
            try:
                while True:
                    if steering.phase == "closing":
                        raise asyncio.CancelledError()
                    if steering.latest_sequence > attempt.sequence:
                        raise SteerPreempted()
                    try:
                        chunk = await self._wait(anext(iterator), attempt.sequence)
                    except StopAsyncIteration:
                        return
                    if getattr(chunk, "is_last", False):
                        steering.complete_sampling(attempt, chunk)
                        status = "completed"
                    yield chunk
                    if attempt.complete:
                        return
            except SteerPreempted:
                status = "steered"
                raise
            except Exception:
                status = "error"
                raise
            finally:
                attempt.finish(status)
                await self._close(attempt, iterator)

        return stream()
