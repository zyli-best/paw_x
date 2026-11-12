"""Fit one timed reminder to the current model, outside durable context."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from agentscope.message import Msg, TextBlock
from agentscope.middleware import MiddlewareBase

from ...config.context import get_current_session_id
from ...constant import PROGRESS_REMINDER_MESSAGE_TAG, QWENPAW_MESSAGE_TAG_KEY
from ...loop.progress_reminder import ProgressReminderController
from .request_hints import is_progress_hint

logger = logging.getLogger(__name__)
_MAX_HINT_TOKENS = 512

# The short versions retain every instruction and boundary; never truncate a
# rule at an arbitrary character/token boundary to fit a smaller model.
_ZH = """<system-reminder>
执行进展检查：本次 reply 累计执行 {elapsed:.1f} 分钟，距上次检查 {since:.1f} 分钟。
已观察到的工具事件：{stats}。仅为事实统计，不等于进展或循环判定，不构成新任务或验收标准变更。
结合原始需求和最近真实结果简短核对，适当加快节奏；本轮故障建议仅供参考：
1. 当前路径仍产生有效结果时继续，不因耗时较长就重启或换方案。
2. 同类失败反复发生、或重复操作没有新信息时，从最近失败的真实原因出发，不换说法重试同一操作；调整关键条件或切换可行路径，执行可验证的下一步。
3. 信息足够就立即行动或交付，不只描述计划；减少解释和重复确认，避免额外扩展，完成必要验证后尽快交付。
4. 不降低验收标准，不跳过必要验证，不突破既有权限和执行限制。
5. 仍有可执行路径就继续；仅在缺少必要输入、权限或外部条件时说明具体阻塞和已尝试的办法。
这不是截止时间，不证明已陷入循环，不要求收尾或缩小范围。核对无异常可直接继续；无需专门回复或长篇自检。
</system-reminder>"""
_EN = """<system-reminder>
Progress check: this reply has executed for {elapsed:.1f} minutes, {since:.1f} since the last check.
Observed tool events: {stats}. Facts only, not a judgment of progress or a loop; no new task or changed acceptance criteria.
Briefly check the original requirements and latest real results; pick up the pace where appropriate. Any failure hint this turn is reference only.
1. Continue a path producing useful results; elapsed time alone is no reason to restart or switch.
2. For recurring failures or repetition without new information, use the true cause of the latest failure, not a reworded retry. Adjust key conditions or choose a viable path and take a verifiable next step.
3. When information suffices, act or deliver now instead of merely planning. Keep output concise, avoid repeated confirmations and extra scope, and deliver promptly after required verification.
4. Do not lower acceptance criteria, skip required verification, or exceed permissions and execution limits.
5. Continue while an actionable path remains. Only when required input, permission or external conditions are missing, state the specific blocker and what you tried.
This is not a deadline, proof of a loop, or a request to wrap up or narrow scope. Continue if nothing is wrong; no separate reply or long self-review is needed.
</system-reminder>"""
_SHORT_ZH = """<system-reminder>
进展检查：累计执行 {elapsed:.1f} 分钟，距上次 {since:.1f} 分钟。工具事件：{stats}。统计不代表进展或循环判定，不新增任务或改变验收。
结合原始需求、最近真实结果及本轮故障建议简短核对，加快推进：有效路径继续，不因耗时重启；同类失败或重复无新信息时，从最近失败的真实原因调整关键条件或换可行路径，不换说法原样重试，并执行可验证的下一步。信息足够就行动或交付，不只计划；减少解释和重复确认，避免扩展，必要验证后及时交付。不降低验收标准、不跳过验证、不越过权限和执行 限制。有可执行路径就继续；真正缺少必要输入、权限或外部条件时才说明具体阻塞和已尝试的办法。这不是截止时间或循环结论，不要求收尾或缩小范围；正常可直接继续，无需回复或长篇自检。
</system-reminder>"""
_SHORT_EN = """<system-reminder>
Progress check: {elapsed:.1f} execution minutes, {since:.1f} since last check. Tool events: {stats}. Facts do not judge progress or loops, add tasks or change acceptance criteria.
Check original requirements, latest real results and any failure hint briefly; pick up the pace. Continue useful paths; time alone does not warrant restarting. For recurring failures or repetition without new information, address the true cause of the latest failure, adjust key conditions or choose a viable path, not a reworded retry; take a verifiable next step. Act or deliver when information suffices, not just plan; keep output concise, avoid repeated confirmations and extra scope, deliver after required verification. Do not lower acceptance criteria, skip verification, or exceed permissions or execution limits. Continue while actionable; only report specific missing input, permission or external conditions and prior attempts when truly blocked. This is not a deadline or loop verdict, nor a request to wrap up or narrow scope. Continue if normal; no reply or long self-review needed.
</system-reminder>"""


class ProgressReminderMiddleware(MiddlewareBase):
    def __init__(self, controller: ProgressReminderController) -> None:
        self.controller = controller
        self._candidate = None
        self._bounded: dict[tuple, Msg | None] = {}
        self._hint_tokens: dict[int, int] = {}

    @staticmethod
    def _decision_key(agent: Any) -> tuple:
        return (
            get_current_session_id() or "default",
            agent.state.reply_id,
            agent.state.cur_iter,
        )

    async def prepare(self, agent: Any) -> None:
        started = time.perf_counter()
        candidate = self.controller.prepare(*self._decision_key(agent))
        if candidate is not self._candidate:
            self._bounded.clear()
            self._hint_tokens.clear()
        self._candidate = candidate
        if candidate is not None:
            logger.info(
                "ProgressReminder prepare_elapsed_ms=%.1f",
                (time.perf_counter() - started) * 1000,
            )

    async def _fit(self, model: Any, language: str) -> Msg | None:
        candidate = self._candidate
        if candidate is None:
            return None
        key = (id(model), language)
        if key in self._bounded:
            return self._bounded[key]
        zh = language == "zh"
        for template, stats in (
            (_ZH if zh else _EN, candidate.stats),
            (_ZH if zh else _EN, "omitted"),
            (_SHORT_ZH if zh else _SHORT_EN, "omitted"),
        ):
            msg = Msg(
                name="progress_reminder",
                role="user",
                content=[
                    TextBlock(
                        type="text",
                        text=template.format(
                            elapsed=candidate.elapsed / 60,
                            since=candidate.since_checkpoint / 60,
                            stats=stats,
                        ),
                    )
                ],
                metadata={
                    QWENPAW_MESSAGE_TAG_KEY: PROGRESS_REMINDER_MESSAGE_TAG,
                    "source": "progress_reminder",
                    "session_id": candidate.key[0],
                    "reply_id": candidate.key[1],
                    "iteration": candidate.key[2],
                    "checkpoint_seq": candidate.checkpoint_seq,
                    "observation_version": candidate.observation_version,
                },
            )
            tokens = await model.count_tokens(messages=[msg], tools=[])
            if tokens <= _MAX_HINT_TOKENS:
                self._bounded[key] = msg
                self._hint_tokens[id(msg)] = tokens
                return msg
        self._bounded[key] = None
        self.controller.record_outcome("omitted", "token_budget")
        return None

    def record_request(self, messages: list) -> None:
        """Called immediately before dispatch, after the final budget check."""
        candidate = self._candidate
        if candidate is None:
            return
        included = next(
            (
                msg
                for msg in messages
                if is_progress_hint(msg)
                and msg.metadata.get("checkpoint_seq")
                == candidate.checkpoint_seq
                and msg.metadata.get("reply_id") == candidate.key[1]
            ),
            None,
        )
        self.controller.record_outcome(
            "included_in_request" if included is not None else "omitted",
            "" if included is not None else "token_budget",
            hint_tokens=self._hint_tokens.get(id(included)),
        )
        if included is not None:
            logger.info(
                "benjamin [progress-reminder] injected session=%s reply=%s "
                "iteration=%s checkpoint_seq=%d elapsed_seconds=%.1f "
                "since_checkpoint_seconds=%.1f hint_tokens=%s content=\n%s",
                *candidate.key,
                candidate.checkpoint_seq,
                candidate.elapsed,
                candidate.since_checkpoint,
                self._hint_tokens.get(id(included)),
                included.get_text_content(),
            )

    async def on_model_call(
        self, agent: Any, input_kwargs: dict[str, Any], next_handler: Callable
    ) -> Any:
        messages = [
            msg
            for msg in input_kwargs["messages"]
            if not is_progress_hint(msg)
        ]
        if self._candidate is not None:
            if self.controller.is_current(
                self._candidate, self._decision_key(agent)
            ):
                try:
                    hint = await self._fit(
                        input_kwargs["current_model"],
                        getattr(agent, "_language", "en"),
                    )
                except Exception:
                    logger.warning(
                        "Progress hint sizing failed", exc_info=True
                    )
                    self.controller.record_outcome("omitted", "count_error")
                    hint = None
                if hint is not None and self.controller.is_current(
                    self._candidate, self._decision_key(agent)
                ):
                    messages.append(hint)
            else:
                self.controller.record_outcome("omitted", "stale")
        input_kwargs["messages"] = messages
        from .budget_middleware import RequestBudgetMiddleware

        if not any(
            isinstance(mw, RequestBudgetMiddleware)
            for mw in getattr(agent, "_model_call_middlewares", [])
        ):
            try:
                self.record_request(messages)
            except Exception:
                logger.warning(
                    "Progress request observation failed", exc_info=True
                )
        try:
            return await next_handler(**input_kwargs)
        except Exception as exc:
            if self._candidate is not None:
                self.controller.record_outcome(
                    "request_failed", type(exc).__name__
                )
            raise
