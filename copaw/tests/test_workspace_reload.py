"""Reload keeps task control stable and drains only the retiring generation."""

import asyncio
import gc
import json
import weakref
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from qwenpaw.app.multi_agent_manager import MultiAgentManager
from qwenpaw.app.task_tracker import TaskOwnerRetired, TaskTracker
from qwenpaw.app.workspace.workspace import Workspace
from qwenpaw.app.workspace.service_manager import ServiceDescriptor, ServiceManager
from qwenpaw.runtime.steering import SteeringState


class FakeWorkspace:
    set_task_tracker = Workspace.set_task_tracker
    task_tracker = Workspace.task_tracker

    def __init__(self, agent_id="default", generation=0):
        self.agent_id = agent_id
        self.generation = generation
        self._task_tracker = TaskTracker()
        self._start_attempted = False
        self._started = False
        self._manager = None
        self.cron_manager = None
        self.services = {}
        self._service_manager = SimpleNamespace(
            services=self.services,
            get_reusable_services=lambda: dict(self.services),
        )
        self.reused = {}
        self.start_gate = None
        self.start_entered = asyncio.Event()
        self.stopped = asyncio.Event()
        self.stop_calls = []
        self.fail_start = False
        self.chat_manager = SimpleNamespace(get_chat=AsyncMock(return_value=object()))

    async def set_reusable_components(self, components):
        self.reused.update(components)

    async def start(self):
        self._start_attempted = True
        self.start_entered.set()
        if self.start_gate:
            await self.start_gate.wait()
        if self.fail_start:
            raise RuntimeError("candidate failed")
        self._started = True

    def set_manager(self, manager):
        self._manager = manager

    async def stop(self, final=True, *, preserve_reused=False):
        self.stop_calls.append((final, preserve_reused))
        self._started = False
        self.stopped.set()


@pytest.fixture
async def environment(monkeypatch):
    manager = MultiAgentManager()
    old = FakeWorkspace()
    old._started = old._start_attempted = True
    old.set_manager(manager)
    manager.agents["default"] = old
    created = []

    def create(agent_id, workspace_dir):
        candidate = FakeWorkspace(agent_id, len(created) + 1)
        created.append(candidate)
        return candidate

    manager._create_workspace = create
    monkeypatch.setattr(
        "qwenpaw.app.multi_agent_manager.load_config",
        lambda: SimpleNamespace(
            agents=SimpleNamespace(
                profiles={
                    "default": SimpleNamespace(workspace_dir="unused"),
                }
            )
        ),
    )
    yield manager, old, created
    trackers = {ws.task_tracker for ws in [old, *created]}
    tasks = [
        task
        for tracker in trackers
        for task in (await tracker.snapshot_active_tasks()).values()
    ]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await manager.stop_all()


async def start_run(workspace, *, key="chat", run_id="client-run", collaborative=False):
    ready, release = asyncio.Event(), asyncio.Event()
    holder = {}

    async def stream(_):
        tracker = workspace.task_tracker
        if collaborative:
            steering = await tracker.bind_run_control(
                lambda _: SteeringState("response-old", "default", run_id, key)
            )
        else:
            steering = await tracker.bind_steering("response-old", "default", run_id)
        steering.phase = "running"
        holder["steering"] = steering
        ready.set()
        yield 'data: {"object":"response","id":"response-old","status":"in_progress"}\n\n'
        await release.wait()
        holder["finished"] = True
        yield 'data: {"object":"response","id":"response-old","status":"completed"}\n\n'

    queue, _ = await workspace.task_tracker.attach_or_start(
        key, {"run_id": run_id}, stream, owner=workspace
    )
    await asyncio.wait_for(ready.wait(), 3)
    return queue, release, holder


@pytest.mark.parametrize("collaborative", [False, True])
async def test_reload_preserves_status_guidance_idempotency_stop_and_replay(
    environment, monkeypatch, collaborative
):
    manager, old, created = environment
    queue, release, holder = await start_run(old, collaborative=collaborative)
    tracker = old.task_tracker
    first, _ = await tracker.steer("chat", "client-run", "a", "first")
    assert await manager.reload_agent("default")
    current = await manager.get_agent("default")
    assert current is created[0] and current.task_tracker is tracker
    assert not old.stopped.is_set()
    assert (await tracker.get_chat_runtime("chat", "default")) == {
        "agent_id": "default",
        "chat_id": "chat",
        "exists": True,
        "status": "running",
        "run_id": "client-run",
        "can_steer": True,
    }
    receipt, duplicate = await current.task_tracker.steer(
        "chat", "client-run", "a", "first"
    )
    assert duplicate and receipt == first
    await current.task_tracker.steer("chat", "client-run", "b", "second")
    assert list(holder["steering"].records) == ["a", "b"]
    assert await current.task_tracker.get_status("chat") == "running"
    assert (await current.task_tracker.get_global_status())["running_task_count"] == 1
    reconnect = await current.task_tracker.attach("chat", run_id="client-run")
    assert reconnect is not None and reconnect.get_nowait().startswith("data:")
    unused = AsyncMock()
    _, is_new = await tracker.attach_or_start("chat", {}, unused, owner=current)
    assert not is_new
    unused.assert_not_called()

    from qwenpaw.app.routers import console

    monkeypatch.setattr(
        console, "get_agent_for_request", lambda _: manager.get_agent("default")
    )
    app = FastAPI()
    app.include_router(console.router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = await client.get(
            "/api/console/chat/status", params={"chat_id": "chat"}
        )
        assert status.json()["run_id"] == "client-run" and status.json()["can_steer"]
        guided = await client.post(
            "/api/console/chat/steer",
            json={
                "chat_id": "chat",
                "run_id": "client-run",
                "message_id": "c",
                "text": "third",
            },
        )
        assert guided.status_code == 202
        stopped = await client.post(
            "/api/console/chat/stop", params={"chat_id": "chat"}
        )
        assert stopped.status_code == 200 and stopped.json()["stopped"]
    await asyncio.wait_for(old.stopped.wait(), 3)
    assert not holder.get("finished") and holder["steering"].phase == "closing"
    assert not (await tracker.get_chat_runtime("chat", "default"))["exists"]
    retained = await current.task_tracker.attach("chat", run_id="client-run")
    assert retained is not None
    assert all(
        not hasattr(record, "owner") for _, record in tracker._completed.values()
    )


async def test_cleanup_ignores_new_generation_and_repeated_reload(environment):
    manager, old, _ = environment
    _, release_old, _ = await start_run(old)
    await manager.reload_agent("default")
    second = manager.agents["default"]
    _, release_new, _ = await start_run(second, key="new-chat", run_id="new-run")
    await manager.reload_agent("default")
    third = manager.agents["default"]
    assert old.task_tracker is second.task_tracker is third.task_tracker
    release_old.set()
    await asyncio.wait_for(old.stopped.wait(), 3)
    assert not second.stopped.is_set()
    assert await third.task_tracker.get_status("new-chat") == "running"
    release_new.set()
    await asyncio.wait_for(second.stopped.wait(), 3)


async def test_timeout_does_not_cancel_tasks_or_close_resources(
    environment, monkeypatch
):
    manager, old, _ = environment
    _, release, _ = await start_run(old)
    original = old.task_tracker.wait_tasks_done
    timed_out = asyncio.Event()
    calls = 0

    async def wait(tasks, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            timed_out.set()
            return False
        return await original(tasks, timeout)

    monkeypatch.setattr(old.task_tracker, "wait_tasks_done", wait)
    await manager.reload_agent("default")
    await timed_out.wait()
    assert not old.stopped.is_set()
    assert await old.task_tracker.get_status("chat") == "running"
    release.set()
    await asyncio.wait_for(old.stopped.wait(), 3)


async def test_failed_or_cancelled_candidate_leaves_old_execution_live(environment):
    manager, old, created = environment
    _, release, _ = await start_run(old)
    factory = manager._create_workspace

    def fail(*args, **kwargs):
        candidate = factory(*args, **kwargs)
        candidate.fail_start = True
        return candidate

    manager._create_workspace = fail
    assert not await manager.reload_agent("default")
    assert manager.agents["default"] is old and not old.stopped.is_set()
    assert created[0].stop_calls == [(True, True)]

    def delayed(*args, **kwargs):
        candidate = factory(*args, **kwargs)
        candidate.start_gate = asyncio.Event()
        return candidate

    manager._create_workspace = delayed
    task = asyncio.create_task(manager.reload_agent("default"))
    while len(created) < 2:
        await asyncio.sleep(0)
    await created[1].start_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created[1].stop_calls == [(True, True)]
    assert manager.agents["default"] is old and not old.stopped.is_set()
    await old.task_tracker.steer("chat", "client-run", "still-live", "change")
    release.set()


async def test_two_simultaneous_reloads_keep_one_tracker(environment):
    manager, old, created = environment
    _, release, _ = await start_run(old)
    assert await asyncio.gather(
        manager.reload_agent("default"), manager.reload_agent("default")
    ) == [True, True]
    assert all(ws.task_tracker is old.task_tracker for ws in created)
    assert manager.agents["default"] is created[-1]
    release.set()
    await asyncio.wait_for(old.stopped.wait(), 3)


async def test_retired_owner_blocks_late_start_but_allows_existing_attachment(
    environment,
):
    _, old, _ = environment
    _, release, _ = await start_run(old)
    tracker = old.task_tracker
    snapshot = await tracker.retire_owner(old)
    assert list(snapshot) == ["chat"]
    _, is_new = await tracker.attach_or_start("chat", {}, AsyncMock(), owner=old)
    assert not is_new
    with pytest.raises(TaskOwnerRetired):
        await tracker.attach_or_start("late", {}, AsyncMock(), owner=old)
    with pytest.raises(TaskOwnerRetired):
        await tracker.register_external_task("late-external", owner=old)
    assert not await tracker.wait_tasks_done(list(snapshot.values()), timeout=0.01)
    assert all(not task.cancelled() for task in snapshot.values())
    release.set()


async def test_late_http_request_retries_on_current_workspace(environment, monkeypatch):
    manager, old, _ = environment
    from qwenpaw.app.routers import console

    entered, proceed = asyncio.Event(), asyncio.Event()
    calls = []

    def wire_channel(ws):
        async def stream(_):
            calls.append(ws.generation)
            yield 'data: {"object":"response","status":"completed"}\n\n'

        return SimpleNamespace(
            resolve_session_id=lambda **_: "session", stream_one=stream
        )

    old_channel = wire_channel(old)

    async def get_old_channel(_):
        entered.set()
        await proceed.wait()
        return old_channel

    old.channel_manager = SimpleNamespace(get_channel=get_old_channel)
    chat = SimpleNamespace(id="chat", name="unrelated")
    old.chat_manager.get_or_create_chat = AsyncMock(return_value=chat)
    factory = manager._create_workspace

    def create(*args, **kwargs):
        current = factory(*args, **kwargs)
        current.channel_manager = SimpleNamespace(
            get_channel=AsyncMock(return_value=wire_channel(current))
        )
        current.chat_manager = old.chat_manager
        return current

    manager._create_workspace = create
    monkeypatch.setattr(
        console, "get_agent_for_request", lambda _: manager.get_agent("default")
    )
    request = asyncio.create_task(
        console.post_console_chat(
            {"session_id": "session", "run_id": "run", "input": []}, object()
        )
    )
    await entered.wait()
    await manager.reload_agent("default")
    proceed.set()
    response = await asyncio.wait_for(request, 3)
    events = [item async for item in response.body_iterator]
    assert events and calls == [1]


async def test_owner_references_are_weak_and_agents_remain_isolated():
    tracker = TaskTracker()
    owner = FakeWorkspace()
    reference = weakref.ref(owner)
    await tracker.retire_owner(owner)
    del owner
    gc.collect()
    assert reference() is None and not tracker._retired_owners
    first, second = FakeWorkspace("a"), FakeWorkspace("b")
    _, release, _ = await start_run(first)
    assert await second.task_tracker.get_status("chat") == "idle"
    release.set()
    assert await first.task_tracker.wait_all_done(3)


async def test_failed_candidate_closes_own_resources_but_not_reused_services():
    ws = SimpleNamespace(agent_id="default")
    services = ServiceManager(ws)
    reused, own = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())
    for name, instance in [("reused", reused), ("own", own)]:
        services.register(
            ServiceDescriptor(name=name, stop_method="close", reusable=True)
        )
        services.services[name] = instance
    services.reused_services.add("reused")
    await services.stop_all(final=True, preserve_reused=True)
    reused.close.assert_not_awaited()
    own.close.assert_awaited_once()


async def test_tracker_cannot_change_after_start_has_begun():
    ws = FakeWorkspace()
    ws._start_attempted = True
    with pytest.raises(RuntimeError, match="Cannot replace"):
        ws.set_task_tracker(TaskTracker())


async def test_cancel_after_start_before_publish_disposes_candidate(environment):
    manager, old, created = environment
    factory = manager._create_workspace

    def create(*args, **kwargs):
        candidate = factory(*args, **kwargs)
        candidate.start_gate = asyncio.Event()
        return candidate

    manager._create_workspace = create
    task = asyncio.create_task(manager.reload_agent("default"))
    while not created:
        await asyncio.sleep(0)
    candidate = created[0]
    await candidate.start_entered.wait()
    async with manager._lock:
        candidate.start_gate.set()
        while not candidate._started:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert candidate.stop_calls == [(True, True)]
    assert manager.agents["default"] is old and not old.stopped.is_set()
    assert not old.task_tracker._owner_retired(old)


async def test_agent_removed_while_candidate_builds_is_not_resurrected(environment):
    manager, old, created = environment
    factory = manager._create_workspace

    def create(*args, **kwargs):
        candidate = factory(*args, **kwargs)
        candidate.start_gate = asyncio.Event()
        return candidate

    manager._create_workspace = create
    task = asyncio.create_task(manager.reload_agent("default"))
    while not created:
        await asyncio.sleep(0)
    await created[0].start_entered.wait()
    assert await manager.stop_agent("default")
    created[0].start_gate.set()
    assert await task is False
    assert "default" not in manager.agents
    assert created[0].stop_calls == [(True, True)]


async def test_shutdown_releases_retired_instances_and_uncommitted_candidate(
    environment,
):
    manager, old, created = environment
    _, release, _ = await start_run(old)
    await manager.reload_agent("default")
    current = manager.agents["default"]
    factory = manager._create_workspace

    def create(*args, **kwargs):
        candidate = factory(*args, **kwargs)
        candidate.start_gate = asyncio.Event()
        return candidate

    manager._create_workspace = create
    reload = asyncio.create_task(manager.reload_agent("default"))
    while len(created) < 2:
        await asyncio.sleep(0)
    await created[-1].start_entered.wait()
    await manager.stop_all()
    with pytest.raises(asyncio.CancelledError):
        await reload
    assert old.stopped.is_set() and current.stopped.is_set()
    assert created[-1].stop_calls == [(True, True)]
    assert not manager._retired_instances and not manager._cleanup_tasks
    release.set()


@pytest.mark.parametrize("collaborative", [False, True])
async def test_real_runtime_after_reload_keeps_session_upload_and_output_attribution(
    environment,
    monkeypatch,
    tmp_path,
    collaborative,
):
    from test_edge_cloud import Model, call, route, text
    from test_steering_output import runtime_setup
    from qwenpaw.observability.session_delta_redactor import (
        redact_session_delta_payload,
    )

    manager, old, _ = environment
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingModel(Model):
        async def __call__(self, messages, **kwargs):
            response = await super().__call__(messages, **kwargs)
            if len(self.requests) == 1:
                entered.set()
                await release.wait()
            return response

    cloud = BlockingModel(
        "cloud",
        (
            [call("submit_route_decision", route())]
            if collaborative
            else [text("old"), text("corrected")]
        ),
    )
    device = Model("device", [text("corrected")])
    ctx, runtime, tracker, session, uploads = runtime_setup(
        monkeypatch,
        tmp_path,
        cloud,
        device,
        collaboration=collaborative,
        tracing=True,
        recording=True,
    )
    for key, value in vars(ctx.workspace).items():
        if key != "task_tracker":
            setattr(old, key, value)
    old._task_tracker = tracker
    ctx.workspace = runtime.workspace = old
    output = []

    async def stream(_):
        async for event in runtime.run(ctx.request):
            output.append(event.model_copy(deep=True))
            yield str(event)

    queue, _ = await tracker.attach_or_start("chat", {}, stream, owner=old)

    async def consume():
        return [event async for event in tracker.stream_from_queue(queue, "chat")]

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 3)
    await manager.reload_agent("default")
    current = manager.agents["default"]
    assert current.task_tracker is tracker and not old.stopped.is_set()
    receipt, duplicate = await current.task_tracker.steer(
        "chat", "client-run", "guide", "after reload correction"
    )
    assert not duplicate and receipt["run_id"] == "client-run"
    release.set()
    await asyncio.wait_for(consumer, 5)
    await asyncio.wait_for(old.stopped.wait(), 3)
    assert getattr(ctx, "error", None) is None
    assert session.saved and len(uploads) == 1
    assert "after reload correction" in json.dumps(session.saved)
    assert "after reload correction" in json.dumps(uploads[0])
    final = next(e for e in reversed(output) if e.object == "response")
    assert final.status == "completed"
    answer = next(
        m
        for m in final.output
        if any("corrected" in getattr(c, "text", "") for c in m.content)
    )
    assert answer.metadata["steer_message_id"] == "guide"
    redacted = redact_session_delta_payload(
        uploads[0],
        sensitive_words={
            "patterns": [{"type": "custom", "keyword": "after reload correction"}]
        },
    )
    assert "after reload correction" not in json.dumps(redacted)


async def cron(monkeypatch, paused=False):
    from qwenpaw.app.crons.manager import CronManager

    monkeypatch.setattr(
        "qwenpaw.app.crons.manager.get_heartbeat_config",
        lambda _: SimpleNamespace(enabled=False),
    )
    monkeypatch.setattr("qwenpaw.app.crons.manager.get_dream_cron", lambda _: None)
    repository = SimpleNamespace(
        load=AsyncMock(return_value=SimpleNamespace(jobs=[])),
        prune_orphan_history=AsyncMock(),
    )
    value = CronManager(
        repo=repository,
        workspace=SimpleNamespace(cron_start_paused=paused),
        channel_manager=None,
    )
    await value.start()
    return value


async def test_cron_handoff_does_not_repeat_consumed_once_job_or_cancel_running_work(
    monkeypatch,
):
    from apscheduler.triggers.date import DateTrigger

    old, new = await cron(monkeypatch), await cron(monkeypatch, True)
    entered, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    count = 0

    async def work():
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        done.set()

    trigger = DateTrigger(
        run_date=datetime.now(timezone.utc) - timedelta(milliseconds=1)
    )
    old._scheduler.add_job(work, trigger, id="once")
    old._schedules["once"] = {"schedule": "unchanged"}
    new._scheduler.add_job(work, trigger, id="once")
    new._schedules["once"] = {"schedule": "unchanged"}
    try:
        await asyncio.wait_for(entered.wait(), 3)
        pending = old.snapshot_active_tasks()
        assert pending
        new.handoff_from(old)
        assert new._scheduler.get_job("once") is None
        assert old._scheduler_executor._instances is new._scheduler_executor._instances
        assert not any(task.cancelled() for task in pending.values())
        release.set()
        assert await TaskTracker().wait_tasks_done(list(pending.values()), 3)
        assert done.is_set() and count == 1
    finally:
        release.set()
        await old.stop()
        await new.stop()
        await asyncio.sleep(0)


async def test_cron_snapshot_includes_submitted_callback_before_it_starts(monkeypatch):
    from apscheduler.triggers.date import DateTrigger

    value = await cron(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        await release.wait()

    value._scheduler.add_job(
        work,
        DateTrigger(run_date=datetime.now(timezone.utc) - timedelta(milliseconds=1)),
        id="pending",
    )
    value._scheduler._process_jobs()
    tasks = value.snapshot_active_tasks()
    try:
        assert tasks and not entered.is_set()
        release.set()
        assert await TaskTracker().wait_tasks_done(list(tasks.values()), 3)
    finally:
        release.set()
        await value.stop()
        await asyncio.sleep(0)


async def test_task_creation_failure_does_not_leave_a_running_placeholder(monkeypatch):
    tracker = TaskTracker()

    async def stream(_):
        yield "data: {}\n\n"

    def fail(_):
        raise RuntimeError("cannot create task")

    monkeypatch.setattr("qwenpaw.app.task_tracker.asyncio.create_task", fail)
    with pytest.raises(RuntimeError, match="cannot create"):
        await tracker.attach_or_start("chat", {}, stream)
    assert await tracker.get_status("chat") == "idle"
    assert not tracker._runs
