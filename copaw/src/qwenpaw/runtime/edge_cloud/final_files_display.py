"""Defer lower-tier final-file tool display until independent verification."""

from __future__ import annotations

from contextlib import aclosing


class VerifiedFinalFilesDisplay:
    """A wire-only gate; canonical tool results remain available to the agent."""

    def __init__(self, envelope, tool_name: str):
        self.envelope = envelope
        self.tool_name = tool_name
        self.pending = True
        self._calls: set[str] = set()
        self._hidden: set[str] = set()
        self._events: list = []

    async def translate_event(self, event, events):
        call_id = getattr(event, "tool_call_id", None)
        if self.pending and getattr(event, "tool_call_name", None) == self.tool_name:
            if not self._calls:
                # Do not change display bookkeeping for tasks that never
                # submit files. Retain preceding messages in their old order.
                self.envelope.enable_client_message_order()
                for message in self.envelope.response.output:
                    self.envelope._client_message_order.setdefault(
                        message.id, len(self.envelope._client_message_order),
                    )
            self._calls.add(call_id)
        deferred = self.pending and call_id in self._calls
        async with aclosing(events):
            async for output in events:
                if deferred:
                    if output.object == "message" and output.type in {
                        "plugin_call", "plugin_call_output",
                    }:
                        self._hidden.add(output.id)
                        # Assign these messages their visible order on release,
                        # after the verdict, rather than at internal execution.
                        self.envelope._client_message_order.pop(output.id, None)
                    if (
                        output.object == "message" and output.id in self._hidden
                    ) or (
                        output.object == "content"
                        and (
                            getattr(output, "msg_id", None) in self._hidden
                            or (
                                isinstance(getattr(output, "data", None), dict)
                                and output.data.get("name") == self.tool_name
                            )
                        )
                    ):
                        self._events.append(output.model_copy(deep=True))
                        continue
                yield output

    def filter_response(self, response):
        # Heartbeats and the final response must not reveal deferred/discarded
        # calls. Leave Envelope.response and cancellation evidence untouched.
        if not self._hidden:
            return response
        return response.model_copy(update={
            "output": [m for m in response.output if m.id not in self._hidden],
        }, deep=True)

    def release(self):
        self.pending = False
        self._hidden.clear()
        events, self._events = self._events, []
        self._calls.clear()
        for event in events:
            yield self.envelope._tag_seq(event)

    def discard(self):
        self.pending = False
        self._events.clear()
        self._calls.clear()

