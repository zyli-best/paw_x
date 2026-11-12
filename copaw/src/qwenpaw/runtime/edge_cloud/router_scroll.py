"""Reuse the existing Scroll pipeline for one isolated routing decision."""

from __future__ import annotations

from copy import deepcopy

from agentscope.agent import Agent
from agentscope.message import TextBlock, ToolResultBlock
from agentscope.tool import FunctionTool, ToolBase, ToolResponse

from ...agents.context.budget_middleware import RequestBudgetMiddleware
from ...agents.context.scroll.cap_middleware import ToolResultCapMiddleware
from ...agents.context.scroll.manager import ScrollContextManager
from ...agents.context.scroll.recall_tool import make_recall_history
from .roles import AGENT_NAMES


class _RouterRecall(ToolBase):
    """Keep the existing recall permission check and use the router's scope."""

    name = "recall_history"
    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, original, recall):
        super().__init__()
        self.original, self.recall = original, recall
        self.description = recall.description
        self.input_schema = recall.input_schema

    async def check_permissions(self, *args, **kwargs):
        return await self.original.check_permissions(*args, **kwargs)

    async def __call__(self, **kwargs):
        return await self.recall(**kwargs)


class RouterScrollContext:
    """Adapt the decision loop to Scroll without changing the host agent."""

    @classmethod
    def for_agent(cls, host, model, recall, *, session_id, agent_id, flow_id):
        source = getattr(host, "_context_manager", None)
        if not isinstance(source, ScrollContextManager) or source._history.closed:
            return None
        return cls(host, model, recall, source, session_id, agent_id, flow_id)

    def __init__(self, host, model, recall, source, session_id, agent_id, flow_id):
        # Separate bookkeeping and archive scope: decision turns must not become
        # the host's session history or change its Scroll eviction index.
        scope = f"{session_id}:router:{flow_id}"
        owner = f"{agent_id}:router:{flow_id}"
        history = source._history
        self.view = Agent(
            name=AGENT_NAMES["router"],
            system_prompt="",
            model=model,
            context_config=deepcopy(host.context_config),
        )
        running = getattr(getattr(host, "_agent_config", None), "running", None)
        context = getattr(running, "light_context_config", None)
        compact = getattr(context, "context_compact_config", None)
        self.enabled = getattr(compact, "enabled", True)
        capped_results = {}
        self.manager = ScrollContextManager(
            history=history,
            session_id=scope,
            agent_id=owner,
            capped_results=capped_results,
            summarize_unheadlined=source._summarize_unheadlined,
            summarize_timeout_s=source._summarize_timeout_s,
        )
        self.cap = ToolResultCapMiddleware(
            history=history,
            model=model,
            session_id=scope,
            agent_id=owner,
            token_cap=getattr(
                getattr(context, "scroll_config", None), "tool_output_token_cap", 3000,
            ),
            capped_results=capped_results,
        )
        self.budget = RequestBudgetMiddleware(
            self.manager, compression_enabled=self.enabled,
        )
        self.recall_tool = _RouterRecall(recall, FunctionTool(make_recall_history(
            history_db_path=str(history.path), session_id=scope, agent_id=owner,
        )))
        self._seen_results = set()

    async def prepare(self, messages, tools):
        # All mutations belong to this decision. Preserve the exact system
        # message and initial payload instead of regenerating either of them.
        system = messages[0]
        self.view._system_prompt = system.get_text_content() or ""
        self.view.state.context = deepcopy(messages[1:])
        for message in self.view.state.context:
            for block in message.content:
                if not isinstance(block, ToolResultBlock) or block.id in self._seen_results:
                    continue
                output = block.output
                if isinstance(output, str):
                    output = [TextBlock(text=output)]
                response = ToolResponse(content=output or [], state=block.state)

                async def result(**kwargs):
                    yield response

                async for capped in self.cap.on_acting(
                    self.view, {"tool_call": block}, result,
                ):
                    block.output = capped.content
                self._seen_results.add(block.id)

        async def prepare_input():
            return {"messages": [system, *self.view.state.context], "tools": tools}

        if self.enabled:
            await self.manager.compress(self.view, prepare_input=prepare_input)

        async def checked(**kwargs):
            return kwargs["messages"]

        return await self.budget.on_model_call(
            self.view,
            {"current_model": self.view.model, **(await prepare_input())},
            checked,
        )
