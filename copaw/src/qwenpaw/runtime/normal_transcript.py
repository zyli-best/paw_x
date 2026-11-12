"""Ordinary interrupted output: display persistence, never model input."""

from collections import OrderedDict

from agentscope.message import Msg, TextBlock, ThinkingBlock

from .session_views import SessionContextViews, make_context_view


def project_interruptions(steering, context):
    groups = OrderedDict()
    ids = {m.get("id") for m in context}
    for record in steering.interruptions:
        message = record["message"]
        metadata = {
            **(message.metadata or {}),
            "qwenpaw_message_kind": "steer_interrupted_output",
            "status": str(getattr(message.status, "value", message.status)),
        }
        blocks = []
        for content in message.content:
            text = getattr(content, "text", None)
            if text is None:
                import json

                text = json.dumps(getattr(content, "data", {}), ensure_ascii=False)
            blocks.append(
                ThinkingBlock(thinking=text)
                if message.type == "reasoning"
                else TextBlock(text=text)
            )
        native = Msg(
            id=message.id,
            name="assistant",
            role="assistant",
            content=blocks,
            metadata=metadata,
        )
        anchor = record["anchor_id"]
        if anchor not in ids:
            anchor = next(
                (
                    m["id"]
                    for m in reversed(context)
                    if m.get("id") in steering.original_ids
                ),
                "",
            )
        groups.setdefault(anchor, []).append(native.model_dump(mode="json"))
    views = SessionContextViews()
    positions = {m.get("id"): i for i, m in enumerate(context)}
    for anchor, display in sorted(
        groups.items(), key=lambda item: positions.get(item[0], -1)
    ):
        views.history.append(
            make_context_view([], display, view_id=display[0]["id"], after_id=anchor)
        )
    return views.project(context)
