# -*- coding: utf-8 -*-
"""MultiAgentManager: Manages multiple agent workspaces with lazy loading.

Provides centralized management for multiple Workspace objects,
including lazy loading, lifecycle management, and hot reloading.
"""

import asyncio
import logging
import time
from typing import Dict, Set

from qwenpaw.exceptions import (
    ConfigurationException,
)

from .workspace import Workspace
from ..config.utils import load_config

logger = logging.getLogger(__name__)


class MultiAgentManager:
    """Manages multiple agent workspaces.

    Features:
    - Lazy loading: Workspaces are created only when first requested
    - Lifecycle management: Start, stop, reload workspaces
    - Thread-safe: Uses async lock for concurrent access
    - Hot reload: Reload individual workspaces without affecting others
    - Parallel startup: Multiple agents start concurrently via
      fine-grained locking (lock released during slow workspace init)
    """

    def __init__(self):
        """Initialize multi-agent manager."""
        self.agents: Dict[str, Workspace] = {}
        self._lock = asyncio.Lock()
        self._pending_starts: Dict[str, asyncio.Event] = {}
        self._cleanup_tasks: Set[asyncio.Task] = set()
        self._retired_instances: dict[int, Workspace] = {}
        self._reload_locks: dict[str, asyncio.Lock] = {}
        self._reload_tasks: set[asyncio.Task] = set()
        self._reload_candidates: dict[asyncio.Task, Workspace] = {}
        self._stopping = False
        logger.debug("MultiAgentManager initialized")

    def _create_workspace(
        self,
        agent_id: str,
        workspace_dir: str,
    ) -> Workspace:
        """Factory method for workspace creation.

        Overridden by WorkspaceRegistry.
        """
        return Workspace(agent_id=agent_id, workspace_dir=workspace_dir)

    async def get_agent(self, agent_id: str) -> Workspace:
        """Get agent workspace by ID (lazy loading with dedup).

        If workspace doesn't exist in memory, it will be created and started.
        Multiple concurrent callers for the same agent_id are coordinated:
        the first caller creates the workspace while others wait.

        The lock is only held briefly for dict checks/mutations, not during
        the slow workspace startup, allowing parallel agent initialization.

        Args:
            agent_id: Agent ID to retrieve

        Returns:
            Workspace: The requested workspace instance

        Raises:
            ConfigurationException: If agent ID not found in configuration
        """
        if self._stopping:
            raise RuntimeError("Agent manager is shutting down")
        # Fast path: already loaded (no lock)
        if agent_id in self.agents:
            logger.debug(f"Returning cached agent: {agent_id}")
            return self.agents[agent_id]

        should_start = False
        event = None
        agent_ref = None

        async with self._lock:
            # Re-check under lock
            if agent_id in self.agents:
                logger.debug(f"Returning cached agent: {agent_id}")
                return self.agents[agent_id]

            if agent_id in self._pending_starts:
                # Another task is already starting this agent; wait for it
                event = self._pending_starts[agent_id]
            else:
                # We are the first caller — validate config and claim startup
                config = load_config()
                if agent_id not in config.agents.profiles:
                    raise ConfigurationException(
                        config_key="agent",
                        message=(
                            f"Agent '{agent_id}' not found in configuration. "
                            f"Available agents: "
                            f"{list(config.agents.profiles.keys())}"
                        ),
                    )
                agent_ref = config.agents.profiles[agent_id]
                event = asyncio.Event()
                self._pending_starts[agent_id] = event
                should_start = True

        if not should_start:
            # Wait for the in-progress startup to finish
            await event.wait()
            if agent_id in self.agents:
                logger.debug(f"Returning cached agent: {agent_id}")
                return self.agents[agent_id]
            raise ConfigurationException(
                config_key="agent",
                message=f"Agent '{agent_id}' failed to initialize",
            )

        # We are the starter — create outside the lock for parallelism
        t0 = time.perf_counter()
        logger.debug(f"Creating new workspace: {agent_id}")
        instance = self._create_workspace(
            agent_id=agent_id,
            workspace_dir=agent_ref.workspace_dir,
        )

        try:
            await instance.start()
            instance.set_manager(self)

            async with self._lock:
                self.agents[agent_id] = instance

            elapsed = time.perf_counter() - t0
            logger.debug(
                f"Workspace created and started: {agent_id} "
                f"({elapsed:.3f}s)",
            )

            # Fire workspace_created hooks so plugins can provision
            # skills / config into the newly created workspace.
            await self._fire_workspace_created_hooks(
                {
                    "agent_id": agent_id,
                    "workspace_dir": str(agent_ref.workspace_dir),
                },
            )

            return instance
        except Exception as e:
            logger.error(f"Failed to start workspace {agent_id}: {e}")
            raise
        finally:
            # Always clean up pending state and signal waiters
            # This handles cancellation (CancelledError) and all other cases
            async with self._lock:
                self._pending_starts.pop(agent_id, None)
            event.set()

    @staticmethod
    async def _fire_workspace_created_hooks(workspace_info: dict) -> None:
        """Invoke all registered workspace_created hooks.

        Supports both sync and async callbacks:
        - Async callbacks are awaited directly.
        - Sync callbacks are offloaded to a thread via
          ``asyncio.to_thread`` so they never block the event loop.

        Errors in individual hooks are logged but do not prevent
        subsequent hooks from running.

        Args:
            workspace_info: Dict with at least ``agent_id`` and
                ``workspace_dir`` keys.
        """
        try:
            from ..plugins.registry import PluginRegistry

            hooks = PluginRegistry().get_workspace_created_hooks()
        except Exception:
            # Plugin system not initialised yet — nothing to do.
            return

        for hook in hooks:
            try:
                callback = hook.callback
                if asyncio.iscoroutinefunction(callback):
                    await callback(workspace_info)
                else:
                    result = await asyncio.to_thread(callback, workspace_info)
                    if asyncio.iscoroutine(result) or hasattr(
                        result,
                        "__await__",
                    ):
                        await result
            except Exception as exc:
                logger.error(
                    f"Error in workspace_created hook "
                    f"'{hook.hook_name}' for plugin "
                    f"'{hook.plugin_id}': {exc}",
                    exc_info=True,
                )

    async def _graceful_stop_old_instance(
        self,
        old_instance: Workspace,
        agent_id: str,
        active_tasks: dict[str, asyncio.Future] | None = None,
    ) -> None:
        """Gracefully stop old instance after checking for active tasks.

        If active tasks exist, schedule delayed cleanup in background.
        Otherwise, stop immediately.

        Args:
            old_instance: The old workspace instance to stop
            agent_id: Agent ID for logging
        """
        if active_tasks is None:
            active_tasks = await old_instance.task_tracker.retire_owner(old_instance)
        self._retired_instances[id(old_instance)] = old_instance

        async def delayed_cleanup():
            while not await old_instance.task_tracker.wait_tasks_done(
                list(active_tasks.values()),
                timeout=60.0,
            ):
                logger.warning(
                    "Tasks still active for old instance %s; keeping its resources",
                    agent_id,
                )
            while True:
                try:
                    await old_instance.stop(final=False)
                except Exception:
                    logger.exception(
                        "Old workspace cleanup failed for %s; will retry", agent_id
                    )
                    await asyncio.sleep(60.0)
                else:
                    self._retired_instances.pop(id(old_instance), None)
                    return

        cleanup_task = asyncio.create_task(delayed_cleanup())
        self._cleanup_tasks.add(cleanup_task)

        def on_done(task):
            self._cleanup_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        cleanup_task.add_done_callback(on_done)
        if not active_tasks:
            # Keep cleanup alive even if the reload caller is cancelled.
            await asyncio.shield(cleanup_task)

    async def stop_agent(self, agent_id: str) -> bool:
        """Stop a specific agent instance.

        Args:
            agent_id: Agent ID to stop

        Returns:
            bool: True if agent was stopped, False if not running
        """
        async with self._lock:
            if agent_id not in self.agents:
                logger.warning(f"Agent not running: {agent_id}")
                return False

            instance = self.agents[agent_id]
            await instance.stop()
            del self.agents[agent_id]
            logger.info(f"Agent stopped and removed: {agent_id}")
            return True

    async def reload_agent(self, agent_id: str) -> bool:
        """Serialize reloads of one agent while other agents stay available."""
        task = asyncio.current_task()
        self._reload_tasks.add(task)
        try:
            async with self._reload_locks.setdefault(agent_id, asyncio.Lock()):
                if self._stopping:
                    return False
                return await self._reload_agent(agent_id)
        finally:
            try:
                candidate = self._reload_candidates.pop(task, None)
                if candidate is not None:
                    await candidate.stop(preserve_reused=True)
            finally:
                self._reload_tasks.discard(task)

    async def _reload_agent(self, agent_id: str) -> bool:
        """Reload a specific agent instance with zero-downtime.

        This method performs a seamless reload by:
        1. Creating and fully starting a new workspace instance (no lock)
        2. Atomically replacing the old instance with the new one (with lock)
        3. Gracefully stopping the old instance (no lock):
           - If active tasks exist: schedule delayed cleanup in background
           - If no active tasks: stop immediately

        The lock is only held during the atomic swap to minimize blocking
        time for other agent operations.

        This ensures that:
        - New requests are immediately handled by the new instance
        - Ongoing SSE/streaming tasks continue uninterrupted
        - Other agents remain accessible during reload
        - The manager returns quickly without waiting for old tasks
        - Old instance is automatically cleaned up after tasks complete

        Args:
            agent_id: Agent ID to reload

        Returns:
            bool: True if agent was reloaded, False if not running
        """
        # Step 1: Check if agent exists (quick check with lock)
        async with self._lock:
            if agent_id not in self.agents:
                logger.debug(
                    f"Agent not running, will be loaded on next " f"request: {agent_id}",
                )
                return False
            old_instance = self.agents[agent_id]

        logger.info(f"Reloading agent (zero-downtime): {agent_id}")

        # Step 2: Load configuration (outside lock)
        config = load_config()
        if agent_id not in config.agents.profiles:
            logger.error(
                f"Agent '{agent_id}' not found in configuration " f"during reload",
            )
            return False

        agent_ref = config.agents.profiles[agent_id]

        # Step 3: Create and start new workspace instance (outside lock)
        # This is the slow part, but doesn't block other agents
        logger.info(f"Creating new workspace instance: {agent_id}")
        new_instance = self._create_workspace(
            agent_id=agent_id,
            workspace_dir=agent_ref.workspace_dir,
        )
        self._reload_candidates[asyncio.current_task()] = new_instance
        new_instance.cron_start_paused = True

        # Step 3.5: Set reusable components from old instance (if any)
        async with self._lock:
            old_instance = self.agents.get(agent_id)

        if old_instance:
            new_instance.set_task_tracker(old_instance.task_tracker)
            # Get all reusable services from old instance's ServiceManager
            # pylint: disable=protected-access
            reusable = old_instance._service_manager.get_reusable_services()
            # pylint: enable=protected-access

            if reusable:
                await new_instance.set_reusable_components(reusable)
                logger.info(
                    f"Set reusable components for {agent_id}: " f"{list(reusable.keys())}",
                )

        try:
            await new_instance.start()
            new_instance.set_manager(self)  # Set manager reference
            logger.info(f"New workspace instance started: {agent_id}")
        except BaseException as e:
            logger.exception(
                f"Failed to start new workspace instance for {agent_id}: {e}",
            )
            # Old instance is still running and serving requests
            if not isinstance(e, Exception):
                raise
            return False

        # Step 4: Atomic swap (minimal lock time)
        # From this point, reload is considered successful
        async with self._lock:
            # Double-check agent still exists
            if (
                self._stopping
                or old_instance is None
                or self.agents.get(agent_id) is not old_instance
            ):
                logger.warning(
                    f"Agent {agent_id} was removed during reload, "
                    f"stopping new instance",
                )
                return False

            # Swap instances atomically
            old_instance = self.agents[agent_id]
            async with old_instance.task_tracker.lock:
                old_cron = old_instance.cron_manager
                new_cron = new_instance.cron_manager
                if new_cron is not None:
                    new_cron.handoff_from(old_cron)
                # No await between closing old admission and publishing new.
                active_tasks = old_instance.task_tracker._retire_owner(old_instance)
                if old_cron is not None:
                    active_tasks.update(old_cron.snapshot_active_tasks())
                self.agents[agent_id] = new_instance
                self._retired_instances[id(old_instance)] = old_instance
                self._reload_candidates.pop(asyncio.current_task(), None)
            logger.info(f"Workspace instance replaced: {agent_id}")

        # Step 5: Gracefully stop old instance (outside lock)
        # Delegates to helper method to avoid too-many-statements
        try:
            old_watcher = old_instance._service_manager.services.get("agent_config_watcher")
            if old_watcher is not None:
                try:
                    await old_watcher.stop()
                except Exception:
                    logger.warning("Failed to stop old config watcher", exc_info=True)
        finally:
            await self._graceful_stop_old_instance(old_instance, agent_id, active_tasks)

        return True

    async def cancel_all_cleanup_tasks(self) -> None:
        """Cancel and await all pending delayed cleanup tasks.

        This ensures that any in-progress background cleanups are either
        completed or cleanly cancelled before the manager is torn down.
        Called by stop_all() during shutdown.
        """
        if not self._cleanup_tasks:
            return

        logger.info(
            f"Cancelling {len(self._cleanup_tasks)} pending cleanup "
            f"task(s)...",
        )
        tasks = list(self._cleanup_tasks)
        self._cleanup_tasks.clear()

        for task in tasks:
            if not task.done():
                task.cancel()

        # Await completion of all tasks, collecting exceptions
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("All cleanup tasks cancelled/completed")

    async def stop_all(self):
        """Stop all agent instances concurrently.

        Called during application shutdown to clean up resources.
        Cancels any pending delayed cleanup tasks and stops all
        agents in parallel via asyncio.gather.
        """
        logger.info(
            f"Stopping all agents ({len(self.agents)} running)...",
        )

        self._stopping = True
        reloads = [
            task for task in self._reload_tasks if task is not asyncio.current_task()
        ]
        for task in reloads:
            task.cancel()
        await asyncio.gather(*reloads, return_exceptions=True)
        await self.cancel_all_cleanup_tasks()
        for retired in list(self._retired_instances.values()):
            try:
                await retired.stop(final=False)
            except Exception:
                logger.exception("Failed to stop retired workspace during shutdown")
        self._retired_instances.clear()

        async def _stop_one(agent_id: str, instance: Workspace):
            try:
                await instance.stop()
                logger.debug(f"Agent stopped: {agent_id}")
            except Exception as e:
                logger.error(
                    f"Error stopping agent {agent_id}: {e}",
                )

        await asyncio.gather(
            *(_stop_one(aid, inst) for aid, inst in self.agents.items()),
        )

        self.agents.clear()
        logger.info("All agents stopped")

    def list_loaded_agents(self) -> list[str]:
        """List currently loaded agent IDs.

        Returns:
            list[str]: List of loaded agent IDs
        """
        return list(self.agents.keys())

    def is_agent_loaded(self, agent_id: str) -> bool:
        """Check if agent is currently loaded.

        Args:
            agent_id: Agent ID to check

        Returns:
            bool: True if agent is loaded and running
        """
        return agent_id in self.agents

    async def preload_agent(self, agent_id: str) -> bool:
        """Preload an agent instance during startup.

        Args:
            agent_id: Agent ID to preload

        Returns:
            bool: True if successfully preloaded, False if failed
        """
        try:
            await self.get_agent(agent_id)
            logger.info(f"Successfully preloaded agent: {agent_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to preload agent {agent_id}: {e}")
            return False

    async def start_all_configured_agents(self) -> dict[str, bool]:
        """Start all enabled agents defined in configuration concurrently.

        Only agents with enabled=True will be started.
        Disabled agents are skipped to save resources.

        Agents are started truly in parallel: get_agent() only holds the
        manager lock briefly for dict checks, releasing it during the slow
        workspace initialization.

        Returns:
            dict[str, bool]: Mapping of agent_id to success status
        """
        config = load_config()
        # Filter only enabled agents
        enabled_agents = {
            agent_id: ref
            for agent_id, ref in config.agents.profiles.items()
            if getattr(ref, "enabled", True)
        }
        agent_ids = list(enabled_agents.keys())

        if not agent_ids:
            logger.warning("No enabled agents configured in config")
            return {}

        total_agents = len(config.agents.profiles)
        disabled_count = total_agents - len(agent_ids)
        logger.debug(
            f"Starting {len(agent_ids)} enabled agent(s) "
            f"({disabled_count} disabled)",
        )

        async def start_single_agent(agent_id: str) -> tuple[str, bool]:
            """Start a single agent with error handling."""
            try:
                logger.debug(f"Starting agent: {agent_id}")
                await self.get_agent(agent_id)
                logger.debug(f"Agent started successfully: {agent_id}")
                return (agent_id, True)
            except Exception as e:
                logger.error(
                    f"Failed to start agent {agent_id}: {e}. "
                    f"Continuing with other agents...",
                )
                return (agent_id, False)

        # Truly parallel: get_agent releases lock during workspace startup
        results = await asyncio.gather(
            *[start_single_agent(agent_id) for agent_id in agent_ids],
            return_exceptions=False,
        )

        # Build result mapping
        result_map = dict(results)
        success_count = sum(1 for success in result_map.values() if success)
        logger.info(
            f"Agent startup complete: {success_count}/{len(agent_ids)} "
            f"agents started successfully, {disabled_count} disabled",
        )

        return result_map

    def __repr__(self) -> str:
        """String representation of manager."""
        loaded = list(self.agents.keys())
        return f"MultiAgentManager(loaded_agents={loaded})"
