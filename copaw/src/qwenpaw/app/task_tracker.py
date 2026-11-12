# -*- coding: utf-8 -*-
"""Task tracker for background runs: streaming, reconnect, multi-subscriber.

For internal streaming runs, ``run_key`` is typically ``ChatSpec.id``
(chat_id). For externally-managed tasks (registered via
:meth:`TaskTracker.register_external_task`), ``run_key`` is an opaque
identifier chosen by the caller (e.g. a UUID prefixed with ``"ext-"``).
Per run: task, queues, event buffer. Reconnects get buffer replay + new
events. Completed streams are retained briefly for reconnects.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
import weakref
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

_SENTINEL = None
_current_run: ContextVar[Any] = ContextVar("qwenpaw_console_run", default=None)


class RunControlError(ValueError):
    """HTTP-safe error from an optional task control backend."""

    def __init__(self, code: str, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.code, self.status_code = code, status_code


class TaskOwnerRetired(RuntimeError):
    """Retry admission against the current workspace after a reload."""


@dataclass
class _RunState:
    """Per-run state (task, queues, buffer), guarded by tracker lock."""

    task: asyncio.Future
    queues: list[asyncio.Queue] = field(default_factory=list)
    buffer: list[str] = field(default_factory=list)
    start_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None
    steering: Any = None
    control: Any = None
    response_id: str | None = None
    client_run_id: str | None = None
    owner: object | None = None


@dataclass
class _ReplayState:
    """Retain wire data only, without keeping agents or tasks alive."""

    buffer: list[str]
    response_id: str | None
    client_run_id: str | None


class TaskTracker:
    """Per-agent tracker, shared by workspace generations across reloads.

    All mutations to _runs under _lock. Producer broadcasts under lock.
    Subscribers use unbounded per-connection queues; disconnect removes them
    via :meth:`detach_subscriber`.
    """

    def __init__(
        self, *, replay_ttl_seconds: float = 300.0, max_completed_runs: int = 128,
    ) -> None:
        if replay_ttl_seconds < 0 or max_completed_runs < 0:
            raise ValueError("Replay retention limits must be non-negative")
        self._lock = asyncio.Lock()
        self._runs: dict[str, _RunState] = {}
        self._retired_owners: dict[int, weakref.ReferenceType] = {}
        self._completed: dict[tuple[str, str], tuple[float, _ReplayState]] = {}
        self._replay_ttl = replay_ttl_seconds
        self._max_completed = max_completed_runs
        self._global_last_run_at: Optional[datetime] = None
        self._global_last_finish_at: Optional[datetime] = None

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def _owner_retired(self, owner: object | None) -> bool:
        reference = self._retired_owners.get(id(owner))
        return reference is not None and reference() is owner

    async def retire_owner(self, owner: object) -> dict[str, asyncio.Future]:
        """Close admission and capture owned tasks under the same lock."""
        async with self._lock:
            return self._retire_owner(owner)

    def _retire_owner(self, owner):
        """Caller holds lock; manager also publishes the replacement here."""
        key = id(owner)

        def forget(reference):
            if self._retired_owners.get(key) is reference:
                self._retired_owners.pop(key, None)

        self._retired_owners[key] = weakref.ref(owner, forget)
        return self._snapshot_active_tasks(owner)

    def _snapshot_active_tasks(self, owner=None) -> dict[str, asyncio.Future]:
        return {
            key: run.task
            for key, run in self._runs.items()
            if not run.task.done() and (owner is None or run.owner is owner)
        }

    async def snapshot_active_tasks(self, owner=None) -> dict[str, asyncio.Future]:
        async with self._lock:
            return self._snapshot_active_tasks(owner)

    async def wait_tasks_done(self, tasks, timeout: float = 60.0) -> bool:
        """Wait without cancelling task execution on timeout/caller cancel."""
        if not tasks:
            return True
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        return not pending

    async def bind_run_control(self, factory):
        """Register an optional backend for the current producer, once."""
        active = _current_run.get()
        if active is None or active[0] is not self:
            return None
        _, run_key, run = active
        async with self._lock:
            if (self._runs.get(run_key) is not run or run.task.done()
                    or asyncio.current_task() is not run.task):
                return None
            if run.control is None:
                run.control = factory(run_key)
            return run.control

    async def bind_steering(self, response_id: str, agent_id: str, run_id: str):
        """Bind only the Runtime executing inside this tracker's producer."""
        active = _current_run.get()
        if active is None or active[0] is not self:
            return None
        _, run_key, run = active
        async with self._lock:
            if (
                self._runs.get(run_key) is not run or run.task.done()
                or asyncio.current_task() is not run.task
            ):
                return None
            from ..runtime.steering import SteeringState

            if run.steering is None:
                run.steering = SteeringState(response_id, agent_id, run_id, run_key)
            return run.steering

    async def get_chat_runtime(self, run_key: str, agent_id: str) -> dict:
        async with self._lock:
            run = self._runs.get(run_key)
            active = run is not None and not run.task.done()
            steering = (run.control or run.steering) if active else None
            return {
                "agent_id": agent_id,
                "chat_id": run_key,
                "exists": active,
                "status": steering.phase if steering else ("running" if active else "idle"),
                "run_id": steering.run_id if steering else None,
                "can_steer": bool(steering and steering.phase == "running"),
            }

    async def steer(self, run_key: str, run_id: str, message_id: str, text: str):
        from ..runtime.steering import SteeringError

        async with self._lock:
            run = self._runs.get(run_key)
            if run is None or run.task.done():
                raise SteeringError("RUN_NOT_ACTIVE", "Chat has no active execution")
            steering = run.control or run.steering
            if steering is None:
                raise SteeringError("STEER_UNSUPPORTED", "This execution does not support steering")
            if steering.run_id != run_id:
                raise SteeringError("RUN_MISMATCH", "The active execution has changed")
            return steering.enqueue(message_id, text)

    async def get_status(self, run_key: str) -> str:
        """Return ``'idle'`` or ``'running'``."""
        async with self._lock:
            state = self._runs.get(run_key)
        if state is None or state.task.done():
            return "idle"
        return "running"

    async def get_global_status(self) -> dict:
        """Get global agent status summary.

        Returns:
            dict with keys:
                - status: 'idle' | 'running'
                - running_task_count: int
                - last_run_at: Optional[datetime]
                - last_finish_at: Optional[datetime]
        """
        async with self._lock:
            running_count = sum(
                1 for state in self._runs.values() if not state.task.done()
            )
            status = "running" if running_count > 0 else "idle"

            return {
                "status": status,
                "running_task_count": running_count,
                "last_run_at": self._global_last_run_at,
                "last_finish_at": self._global_last_finish_at,
            }

    async def has_active_tasks(self) -> bool:
        """Check if any tasks are currently running.

        Returns:
            bool: True if any tasks are active, False otherwise
        """
        async with self._lock:
            for state in self._runs.values():
                if not state.task.done():
                    return True
            return False

    async def list_active_tasks(self) -> list[str]:
        """List all currently running task keys.

        Returns:
            list[str]: List of active run_keys
        """
        async with self._lock:
            return [
                run_key
                for run_key, state in self._runs.items()
                if not state.task.done()
            ]

    async def wait_all_done(self, timeout: float = 300.0) -> bool:
        """Wait for all active tasks to complete.

        Args:
            timeout: Maximum time to wait in seconds (default: 300s = 5min)

        Returns:
            bool: True if all tasks completed, False if timeout occurred
        """

        async def _wait_loop() -> None:
            while await self.has_active_tasks():
                await asyncio.sleep(0.5)

        try:
            await asyncio.wait_for(_wait_loop(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def register_external_task(self, run_key: str, *, owner=None) -> None:
        """Register an externally-managed task so it is visible to
        :meth:`has_active_tasks` and :meth:`wait_all_done`.

        This is used for tasks managed outside of QwenPaw's own streaming
        pipeline (e.g. background tasks dispatched through a third-party
        scheduler).  The caller **must** call
        :meth:`unregister_external_task` when the task completes.

        Args:
            run_key: Unique identifier for the external task.
        """
        start_time = datetime.now(timezone.utc)
        async with self._lock:
            if run_key in self._runs and not self._runs[run_key].task.done():
                logger.debug(
                    "External task already registered: %s",
                    run_key,
                )
                return
            if self._owner_retired(owner):
                raise TaskOwnerRetired("Workspace was reloaded")
            # Use an unresolved Future as the "task" — it stays not-done
            # until unregister_external_task resolves it.
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._runs[run_key] = _RunState(
                task=future,
                queues=[],
                buffer=[],
                start_time=start_time,
                owner=owner,
            )
            self._global_last_run_at = start_time
            logger.debug("Registered external task: %s", run_key)

    async def unregister_external_task(self, run_key: str) -> None:
        """Mark an externally-managed task as done and remove it.

        Sends the sentinel value to any subscriber queues so their
        :meth:`stream_from_queue` consumers terminate cleanly instead of
        hanging. Idempotent — safe to call even if *run_key* was never
        registered or was already unregistered.

        Args:
            run_key: Unique identifier previously passed to
                :meth:`register_external_task`.
        """
        finish_time = datetime.now(timezone.utc)
        async with self._lock:
            state = self._runs.pop(run_key, None)
            if state is None:
                return
            # Notify any subscriber queues so consumers exit cleanly.
            for q in state.queues:
                q.put_nowait(_SENTINEL)
            if not state.task.done():
                state.task.set_result(None)
            state.finish_time = finish_time
            self._global_last_finish_at = finish_time
            logger.debug("Unregistered external task: %s", run_key)

    def _prune_completed(self) -> None:
        """Called under the tracker lock; never evict an active execution."""
        now = time.monotonic()
        for key, (expires, _) in list(self._completed.items()):
            if expires <= now:
                self._completed.pop(key)
        while len(self._completed) > self._max_completed:
            self._completed.pop(next(iter(self._completed)))

    async def attach(
        self, run_key: str, *, response_id: str | None = None,
        run_id: str | None = None,
    ) -> asyncio.Queue | None:
        """Attach to an existing run.

        Returns a new queue pre-filled with the event buffer, or ``None``
        if no active or retained run matches. Optional identities prevent a
        reconnect from silently attaching to a newer turn in the same chat.
        """
        async with self._lock:
            self._prune_completed()

            def matches(state):
                return ((response_id is None or state.response_id == response_id)
                        and (run_id is None or state.client_run_id == run_id))

            state = self._runs.get(run_key)
            completed = False
            if state is None or state.task.done() or not matches(state):
                state = next((
                    saved for (key, _), (_, saved) in reversed(self._completed.items())
                    if key == run_key and matches(saved)
                ), None)
                completed = True
                if state is None:
                    logger.info(
                        "custom_event replay_miss run_key=%s response_id=%s run_id=%s",
                        run_key, response_id, run_id,
                    )
                    return None
            q: asyncio.Queue = asyncio.Queue()
            for sse in state.buffer:
                q.put_nowait(sse)
            if completed:
                q.put_nowait(_SENTINEL)
            else:
                state.queues.append(q)
            logger.info(
                "custom_event replay_attached run_key=%s response_id=%s run_id=%s "
                "state=%s buffered_frames=%s",
                run_key, state.response_id, state.client_run_id,
                "completed" if completed else "active", len(state.buffer),
            )
            return q

    async def detach_subscriber(
        self,
        run_key: str,
        queue: asyncio.Queue,
    ) -> None:
        """Remove *queue* from *run_key*'s subscriber list.

        Idempotent if the run ended or *queue* was already removed.
        """
        async with self._lock:
            self._prune_completed()
            state = self._runs.get(run_key)
            if state is None:
                return
            try:
                state.queues.remove(queue)
            except ValueError:
                pass

    async def request_stop(self, run_key: str) -> bool:
        """Cancel the run. Returns ``True`` if it was running."""
        logger.debug("[STOP] request_stop called for run_key=%s", run_key)
        async with self._lock:
            state = self._runs.get(run_key)
            logger.debug(
                "[STOP] run_key=%s state=%s done=%s",
                run_key,
                "found" if state else "not_found",
                state.task.done() if state else "N/A",
            )
            if state is None or state.task.done():
                logger.debug(
                    "[STOP] Cannot stop run_key=%s (not running)",
                    run_key,
                )
                return False
            logger.debug(
                "[STOP] Calling task.cancel() for run_key=%s",
                run_key,
            )
            if state.steering is not None:
                state.steering.close()
            if state.control is not None:
                state.control.close()
            state.task.cancel()
            logger.debug("[STOP] task.cancel() called for run_key=%s", run_key)
            return True

    async def attach_or_start(
        self,
        run_key: str,
        payload: Any,
        stream_fn: Callable[..., Coroutine],
        *,
        owner: object | None = None,
    ) -> tuple[asyncio.Queue, bool]:
        """Attach to an existing run or start a new one.

        Returns ``(queue, is_new_run)``.
        """
        async with self._lock:
            self._prune_completed()
            state = self._runs.get(run_key)
            if state is not None and not state.task.done():
                q: asyncio.Queue = asyncio.Queue()
                for sse in state.buffer:
                    q.put_nowait(sse)
                state.queues.append(q)
                return q, False

            if self._owner_retired(owner):
                raise TaskOwnerRetired("Workspace was reloaded")

            my_queue: asyncio.Queue = asyncio.Queue()
            meta = (payload.get("meta") or {}) if isinstance(payload, dict) else {}
            client_run_id = (
                payload.get("run_id") or meta.get("run_id")
                if isinstance(payload, dict)
                else getattr(payload, "run_id", None)
            )
            run = _RunState(
                task=asyncio.Future(),  # placeholder, replaced below
                queues=[my_queue],
                buffer=[],
                client_run_id=client_run_id,
                owner=owner,
            )

            tracker_ref = weakref.ref(self)

            async def _producer() -> None:
                start_time = datetime.now(timezone.utc)
                run_token = _current_run.set((self, run_key, run))

                try:
                    tracker = tracker_ref()
                    if tracker is not None:
                        async with tracker.lock:
                            run.start_time = start_time
                            # pylint: disable=protected-access
                            tracker._global_last_run_at = start_time

                    async for sse in stream_fn(payload):
                        tracker = tracker_ref()
                        if tracker is None:
                            return
                        async with tracker.lock:
                            if run.response_id is None:
                                for line in sse.splitlines():
                                    if not line.startswith("data:"):
                                        continue
                                    try:
                                        event = json.loads(line[5:])
                                        if isinstance(event, dict):
                                            run.response_id = event.get("response_id") or (
                                                event.get("id")
                                                if event.get("object") == "response"
                                                else None
                                            )
                                    except (TypeError, ValueError):
                                        pass
                            run.buffer.append(sse)
                            for q in run.queues:
                                q.put_nowait(sse)
                except asyncio.CancelledError:
                    logger.debug("run cancelled run_key=%s", run_key)
                except Exception:
                    logger.exception("run error run_key=%s", run_key)
                    err_sse = (
                        "data: " f"{json.dumps({'error': 'internal server error'})}\n\n"
                    )
                    tracker = tracker_ref()
                    if tracker is not None:
                        async with tracker.lock:
                            run.buffer.append(err_sse)
                            for q in run.queues:
                                q.put_nowait(err_sse)
                finally:
                    if run.steering is not None:
                        run.steering.close()
                    if run.control is not None:
                        run.control.close()
                    finish_time = datetime.now(timezone.utc)
                    tracker = tracker_ref()
                    if tracker is not None:
                        async with tracker.lock:
                            run.finish_time = finish_time
                            # pylint: disable=protected-access
                            tracker._global_last_finish_at = finish_time
                            for q in run.queues:
                                q.put_nowait(_SENTINEL)
                            run.queues.clear()
                            if tracker._replay_ttl > 0 and tracker._max_completed > 0:
                                replay_key = (run_key, run.response_id or uuid.uuid4().hex)
                                tracker._completed[replay_key] = (
                                    time.monotonic() + tracker._replay_ttl,
                                    _ReplayState(
                                        run.buffer, run.response_id, run.client_run_id
                                    ),
                                )
                                tracker._prune_completed()
                            # pylint: disable=protected-access
                            tracker._runs.pop(
                                run_key,
                                None,
                            )
                    _current_run.reset(run_token)

            producer = _producer()
            try:
                run.task = asyncio.create_task(producer)
            except BaseException:
                producer.close()
                raise
            self._runs[run_key] = run
            return my_queue, True

    async def stream_from_queue(
        self,
        queue: asyncio.Queue,
        run_key: str,
    ) -> AsyncGenerator[str, None]:
        """Yield SSE strings from *queue* until the sentinel ``None``.

        Always detaches *queue* from *run_key* when this stream ends or is
        closed (including client disconnect), so reconnects do not leak queues.
        """
        try:
            while True:
                try:
                    event = await queue.get()
                    if event is _SENTINEL:
                        break
                    yield event
                except asyncio.CancelledError:
                    break
        finally:
            await self.detach_subscriber(run_key, queue)
