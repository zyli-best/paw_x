"""Request-only history views for ordinary steering; never mutate AgentState."""

from copy import deepcopy

from agentscope.message import Msg, TextBlock

REPLAY_KEY = "qwenpaw_reasoning_replay"


def project_messages(messages, *, responses=False):
    output, answered, called = [], set(), set()
    for message in messages:
        if message.role != "assistant" or not isinstance(message.content, list):
            output.append(message)
            continue
        segment = []

        def flush():
            if not segment:
                return
            blocks = list(segment)
            if not responses and all(b.type == "thinking" for b in blocks):
                blocks.append(TextBlock(text=""))
            role = message.role
            if message.name == "system" and all(
                b.type not in {"thinking", "tool_call"} for b in blocks
            ):
                # A coordinator notification is not a model generation with
                # missing mandatory DeepSeek reasoning.
                role = "user"
            output.append(
                message.model_copy(update={"content": blocks, "role": role}, deep=True)
            )
            segment.clear()

        for block in message.content:
            if block.type == "tool_result":
                flush()
                if block.id in answered or block.id not in called:
                    # A background completion is a notification; its call
                    # already has the coordinator's offload receipt as result.
                    result = (
                        [TextBlock(text=block.output)]
                        if isinstance(block.output, str)
                        else list(block.output)
                    )
                    output.append(
                        Msg(
                            name="system",
                            role="user",
                            content=[
                                TextBlock(
                                    text=f"Background result for {block.id} follows."
                                ),
                                *result,
                            ],
                            metadata=deepcopy(message.metadata),
                        )
                    )
                else:
                    output.append(
                        message.model_copy(update={"content": [block]}, deep=True)
                    )
                    answered.add(block.id)
            else:
                if block.type == "tool_call":
                    called.add(block.id)
                if block.type == "thinking" and segment:
                    flush()
                segment.append(block)
        flush()
    return output


def restore_reasoning_items(wire, messages):
    """Replay completed Responses reasoning, including opaque encrypted state."""
    replay = {}
    for message in messages:
        for block in message.content or []:
            raw = getattr(block, REPLAY_KEY, None)
            if raw:
                summary = " ".join(
                    part.get("text", "")
                    for item in raw
                    for part in item.get("summary", [])
                    if part.get("text")
                )
                if summary != block.thinking:
                    # Redaction/compaction may change the visible summary.
                    # Never reintroduce the unredacted copy from replay data.
                    raise ValueError(
                        "Completed Responses reasoning was modified; its original replay state cannot be reused"
                    )
                replay[raw[0]["id"]] = raw
    output = []
    for item in wire:
        raw = replay.get(item.get("id")) if item.get("type") == "reasoning" else None
        output.extend(deepcopy(raw) if raw else [item])
    return output


def export_view(value):
    """Recovery-only replay data is never a Langfuse/session-delta field."""
    if isinstance(value, dict):
        return {
            k: export_view(v)
            for k, v in value.items()
            if k
            not in {REPLAY_KEY, "encrypted_content", "qwenpaw_session_context_view"}
        }
    if isinstance(value, (list, tuple)):
        return [export_view(v) for v in value]
    return value
