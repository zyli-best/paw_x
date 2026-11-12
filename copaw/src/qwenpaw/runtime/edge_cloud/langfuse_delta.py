"""Build an upload-only transcript, independently of local session storage."""

from __future__ import annotations

import logging

from ..session_views import display_message_metadata
from .client_transcript import ClientTranscript
from .roles import AGENT_NAMES

logger = logging.getLogger(__name__)


def create_upload_recorder(ctx):
    """Optional observability setup must not affect collaborative execution."""
    try:
        from ...observability.session_delta_langfuse import (
            is_session_delta_langfuse_enabled,
        )

        if is_session_delta_langfuse_enabled():
            return LangfuseDeltaRecorder(ctx.agent.name)
    except Exception:
        logger.debug("Collaboration upload recorder unavailable", exc_info=True)
    return None


class LangfuseDeltaRecorder(ClientTranscript):
    """Fold only this request's emitted events, after existing display gates."""

    def snapshot(self, ctx) -> list[dict]:
        delta = [message.model_dump(mode="json") for message in ctx.input_msgs]
        active_agent = ""
        for message in self.messages.values():
            name = message.get("name")
            if name in AGENT_NAMES.values():
                active_agent = name
            converted = self._message(message)
            if converted is None:
                continue
            if active_agent and converted.get("role") != "user":
                converted["metadata"]["model_ref"] = active_agent
            delta.append(converted)
        for message in delta:
            message["metadata"] = display_message_metadata(
                message.get("metadata") or {}
            )
        return delta

    def attach(self, ctx) -> None:
        """Attach a fresh export after final-file delivery and before saving.

        Failure leaves the existing native delta available as a fallback. No
        agent state, request messages, session views or outgoing events change.
        """
        upload = ctx.extras.get("edge_cloud_upload")
        if (
            not isinstance(upload, dict)
            or upload.get("protocol") != "edge_cloud_trace.v2"
        ):
            return
        try:
            delta = self.snapshot(ctx)
        except Exception:
            logger.warning("Collaboration upload transcript unavailable", exc_info=True)
            return
        upload["delta"] = delta
        upload["delta_count"] = len(delta)
