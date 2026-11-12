"""Side-effect-free folding of client events into native message blocks."""

from __future__ import annotations

import json
import logging
import mimetypes
from copy import deepcopy
from datetime import datetime

from agentscope.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    URLSource,
)

logger = logging.getLogger(__name__)


class ClientTranscript:
    """Own only wire copies; never bind session views or modify an agent."""

    def __init__(self, name: str):
        self.name = name
        self.messages: dict[str, dict] = {}
        self.disabled = False

    def record(self, event) -> None:
        if self.disabled:
            return
        try:
            self._record(event.model_dump(mode="json"))
        except Exception:
            # An optional transcript must never interrupt execution. Retain
            # any already recorded messages and stop observing further events.
            self.disabled = True
            logger.warning("Collaboration session recording stopped", exc_info=True)

    def record_user_input(self, message) -> None:
        """A committed user input shares the actual event insertion order."""
        self.messages[message.id] = {"native_user_input": message.model_dump(mode="json")}

    def _record(self, event: dict) -> None:
        kind = event.get("object")
        if kind == "response":
            for message in event.get("output") or []:
                if message.get("id") not in self.messages:
                    self._record(message)
        elif kind == "message":
            message = deepcopy(event)
            previous = self.messages.get(message["id"])
            if previous and not message.get("content"):
                message["content"] = previous["content"]
            message.setdefault(
                "created_at",
                (previous["created_at"] if previous else datetime.now().isoformat()),
            )
            self.messages[message["id"]] = message
        elif kind == "content":
            message = self.messages.get(event.get("msg_id"))
            if message is None:
                return
            blocks = message.setdefault("content", [])
            index = event.get("index") or 0
            while len(blocks) <= index:
                blocks.append({})
            content = deepcopy(event)
            if content.get("delta") and content.get("type") == "text":
                content["text"] = blocks[index].get("text", "") + content.get(
                    "text", ""
                )
            blocks[index] = content

    @staticmethod
    def _media(content: dict, block_id: str) -> DataBlock | None:
        kind = content["type"]
        field = {
            "image": "image_url",
            "audio": "data",
            "video": "video_url",
            "file": "file_url",
        }[kind]
        value = content.get(field)
        if not value:
            return None
        media_type = mimetypes.guess_type(value.split("?", 1)[0])[0]
        if kind == "audio" and content.get("format"):
            audio_format = content["format"]
            media_type = (
                audio_format if "/" in audio_format else f"audio/{audio_format}"
            )
        media_type = (
            media_type
            or {
                "image": "image/jpeg",
                "audio": "audio/mpeg",
                "video": "video/mp4",
                "file": "application/octet-stream",
            }[kind]
        )
        if value.startswith("data:") and ";base64," in value:
            header, data = value.split(";base64,", 1)
            source = Base64Source(media_type=header[5:], data=data)
        elif kind == "audio" and ":" not in value:
            source = Base64Source(media_type=media_type, data=value)
        else:
            source = URLSource(url=value, media_type=media_type)
        return DataBlock(
            id=block_id,
            source=source,
            name=content.get("filename") or content.get("file_name"),
        )

    @staticmethod
    def _tool_output(value, block_id):
        if not isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        # Envelope encodes multimodal tool results as JSON. Restore their
        # native blocks so the existing history renderer can display media.
        try:
            blocks = json.loads(value)
        except ValueError:
            return value
        if not isinstance(blocks, list) or not blocks:
            return value
        result = []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                return value
            if block.get("type") == "text":
                result.append(
                    TextBlock(id=f"{block_id}_{index}", text=block.get("text", ""))
                )
            elif isinstance(block.get("source"), dict):
                result.append(
                    DataBlock(id=f"{block_id}_{index}", source=block["source"])
                )
            else:
                return value
        return result

    def _message(self, message: dict) -> dict | None:
        if "native_user_input" in message:
            return deepcopy(message["native_user_input"])
        blocks = []
        kind = message.get("type")
        for index, content in enumerate(message.get("content") or []):
            block_id = f"{message['id']}_{index}"
            if kind in {"plugin_call", "function_call", "mcp_tool_call"}:
                data = content.get("data") or {}
                arguments = data.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                blocks.append(
                    ToolCallBlock(
                        id=data["call_id"],
                        name=data["name"],
                        input=arguments,
                        state=(
                            "finished"
                            if message.get("status") == "completed"
                            else "pending"
                        ),
                    )
                )
            elif kind in {
                "plugin_call_output",
                "function_call_output",
                "mcp_tool_call_output",
            }:
                data = content.get("data") or {}
                status = message.get("status")
                state = data.get("state") or {
                    "completed": "success",
                    "failed": "error",
                    "cancelled": "interrupted",
                }.get(status, "running")
                blocks.append(
                    ToolResultBlock(
                        id=data["call_id"],
                        name=data["name"],
                        state=state,
                        output=self._tool_output(data.get("output", ""), block_id),
                        metadata={
                            key: data[key]
                            for key in ("mcp_client_key", "mcp_tool_name")
                            if data.get(key)
                        },
                    )
                )
            elif content.get("type") == "text":
                text = content.get("text", "")
                if text:
                    blocks.append(
                        ThinkingBlock(id=block_id, thinking=text)
                        if kind == "reasoning"
                        else TextBlock(id=block_id, text=text)
                    )
            elif content.get("type") in {"image", "audio", "video", "file"}:
                media = self._media(content, block_id)
                if media is not None:
                    blocks.append(media)
            elif content.get("type") == "refusal" and content.get("refusal"):
                blocks.append(TextBlock(id=block_id, text=content["refusal"]))
        if not blocks:
            return None
        return Msg(
            id=message["id"],
            name=self.name,
            role="assistant",
            content=blocks,
            metadata=message.get("metadata") or {},
            created_at=message["created_at"],
        ).model_dump(mode="json")
