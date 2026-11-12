"""Persist only emitted collaboration events using native session blocks."""

from __future__ import annotations

import logging

from ..session_views import context_views, make_context_view
from .client_transcript import ClientTranscript

logger = logging.getLogger(__name__)


class SessionProcessRecorder(ClientTranscript):
    """Bind an opt-in save view without editing the running agent."""

    def __init__(self, ctx):
        super().__init__(ctx.agent.name)
        self.ctx = ctx
        self.baseline_ids = {message.id for message in ctx.agent.state.context}
        self.views = context_views(ctx)
        self.views.current = self._view

    def _view(self, context: list[dict]):
        display = []
        for message in self.messages.values():
            try:
                converted = self._message(message)
                if converted is not None:
                    display.append(converted)
            except Exception:
                # Do not replace canonical context with an incomplete view.
                logger.warning("Collaboration session conversion failed", exc_info=True)
                return None
        if not display:
            return None
        inputs = [message.model_dump(mode="json") for message in self.ctx.input_msgs]
        input_ids = {message["id"] for message in inputs}
        start = next(
            (
                index
                for index, message in enumerate(context)
                if message.get("id") not in self.baseline_ids
                or message.get("id") in input_ids
            ),
            len(context),
        )
        return make_context_view(
            context[start:],
            [*inputs, *display],
            view_id=self.views.view_id,
            after_id=context[start - 1].get("id", "") if start else "",
        )
