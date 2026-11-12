"""Hard-budget-only verification inputs with durable evidence delivery tracking."""

from __future__ import annotations

import copy
import json

from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock

from ...agents.context.request_budget import budget_for_model
from ...exceptions import ConfigurationException
from .schemas import VerifierDecision


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _text(output):
    if isinstance(output, str):
        return output
    if all(getattr(part, "type", None) == "text" for part in output or []):
        return "\n".join(part.text for part in output or [])
    return _json([part.model_dump(mode="json") for part in output or []])


def _object(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def _is_steering(message):
    return (message.metadata or {}).get("qwenpaw_message_kind") == "edge_cloud_steering"


def _ranges(ranges):
    merged = []
    for start, end in sorted((start, end) for start, end in ranges if start < end):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _remaining(wanted, delivered):
    pending = list(wanted)
    for lo, hi in delivered:
        pending = [(a, b) for start, end in pending
                   for a, b in ((start, min(end, lo)), (max(start, hi), end)) if a < b]
    return pending


class VerifierContext:
    """One flow's local view; saved evidence and delivered evidence are distinct."""

    def __init__(self, history):
        self.history = history
        self.active = False
        self.payloads = {}
        self.raw_payloads = {}
        self.delivered_payloads = set()
        self.delivered_blocks = set()
        self.roots = {}
        self.required = {}
        self.provided = set()
        self.requested = {}
        self.delivered = {}
        self.catalogs = {}
        self.unavailable = []
        self.tool_messages = {}
        self.archived_batches = {}

    def _need(self, seq, path, label, *, complete=False, provided=False):
        key = (seq, path)
        self.required[key] = (label, complete)
        if provided:
            self.provided.add(key)

    def _page(self, block):
        if block.name != "recall_history" or getattr(block.state, "value", block.state) != "success":
            return None
        page = _object(_text(block.output))
        if page and page.get("page_type") == "edge_cloud_history":
            return page
        return None

    def _observe(self, messages):
        for message in messages:
            if _is_steering(message):
                continue
            if message.role == "user" and message.id not in self.payloads:
                payload = _object(message.get_text_content())
                if payload and not (message.metadata or {}).get("edge_cloud_history_ref"):
                    self.payloads[message.id] = payload
                    self.raw_payloads[message.id] = message.get_text_content()
                    for index, record in enumerate(payload.get("tool_results", [])):
                        if isinstance(record, dict):
                            self._index_refs(record.get("text_evidence", []), f"调用 {index + 1} 的截断前文本")
                            self._index_refs(record.get("result"), f"调用 {index + 1} 的返回")
            for block in message.content:
                if not isinstance(block, ToolResultBlock):
                    continue
                page = self._page(block)
                if page:
                    key = (page["seq"], page["json_path"])
                    start, end = page["offset"], page["offset"] + len(page["content"])
                    self.requested[key] = _ranges([*self.requested.get(key, []), (start, end)])

    def _index_refs(self, value, label):
        if isinstance(value, list):
            for item in value:
                self._index_refs(item, label)
        elif isinstance(value, dict):
            ref = value.get("history_context", value)
            if value.get("available") is False and "source" in value and "notice" in value:
                self.unavailable.append({"evidence": label, "available": False})
            elif isinstance(ref, dict) and isinstance(ref.get("seq"), int) and isinstance(ref.get("recall"), dict):
                if self.history is not None and ref["seq"] in self.history.verifier_seqs:
                    self._need(ref["seq"], ref["recall"].get("json_path", ""), label)
                else:
                    self.unavailable.append({"evidence": label, "available": False})
            else:
                for item in value.values():
                    self._index_refs(item, label)

    def _provided(self, seq, path, *, complete, has_body, into=None):
        provided = self.provided if into is None else into
        for key, (_label, needs_complete) in self.required.items():
            number, wanted = key
            if number != seq:
                continue
            if complete and (wanted == path or wanted.startswith(path + "/") or not path):
                provided.add(key)
            elif has_body and not needs_complete and (path == wanted or path.startswith(wanted + "/")):
                provided.add(key)

    def _delivery_state(self, messages):
        """Project what this exact input provides without committing delivery."""
        provided = set(self.provided)
        delivered = dict(self.delivered)
        for message in messages:
            if message.role == "user":
                catalog = self.catalogs.get(message.id)
                payload = _object(message.get_text_content())
                if catalog and payload:
                    seq, _rows = catalog
                    for index in range(len(payload.get("tool_results", []))):
                        self._provided(seq, f"/calls/{index}", complete=True, has_body=True, into=provided)
            for block in message.content:
                if not isinstance(block, ToolResultBlock):
                    continue
                page = self._page(block)
                if page:
                    key = (page["seq"], page["json_path"])
                    start, end = page["offset"], page["offset"] + len(page["content"])
                    spans = _ranges([*delivered.get(key, []), (start, end)])
                    delivered[key] = spans
                    total = page["total_chars"]
                    complete = not _remaining([(0, total)], spans)
                    self._provided(*key, complete=complete, has_body=bool(page["content"]) or total == 0, into=provided)
                elif not block.metadata.get("edge_cloud_history_ref"):
                    identity = (message.id, block.id)
                    if identity in self.roots:
                        self._provided(*self.roots[identity], complete=True, has_body=True, into=provided)
        return provided, delivered

    def record_delivery(self, messages):
        """Called only after a model request completes, never when a tool returns."""
        self.provided, self.delivered = self._delivery_state(messages)
        for message in messages:
            if message.role == "user" and message.id in self.payloads and not message.metadata.get("edge_cloud_history_ref"):
                self.delivered_payloads.add(message.id)
            if any(isinstance(block, (ToolCallBlock, ToolResultBlock)) for block in message.content):
                self.tool_messages.setdefault(message.id, message.model_dump(mode="json"))
            for block in message.content:
                if isinstance(block, ToolResultBlock) and not block.metadata.get("edge_cloud_history_ref"):
                    self.delivered_blocks.add((message.id, block.id))

    def _pending(self, provided=None, delivered=None):
        provided = self.provided if provided is None else provided
        delivered = self.delivered if delivered is None else delivered
        items = list(self.unavailable)
        for (seq, path), (label, _complete) in self.required.items():
            if (seq, path) not in provided:
                items.append({"evidence": label, **self.history.reference(seq, path=path)})
        for (seq, path), wanted in self.requested.items():
            for start, end in _remaining(wanted, delivered.get((seq, path), [])):
                ref = self.history.reference(seq, path=path)
                ref["recall"].update(offset=start, max_chars=min(4000, end - start))
                items.append({"until_offset": end, **ref})
        return items

    def validate_decision(self, decision):
        if VerifierDecision.model_validate(decision).passed and self._pending():
            raise ValueError("Verify 尚有未提供的必要证据或未完成的回读页面，不能判定通过。")

    def _state(self, messages):
        if not self.active:
            return
        provided, delivered = self._delivery_state(messages)
        pending = self._pending(provided, delivered)
        for message in messages:
            if message.role != "user":
                continue
            payload = _object(message.get_text_content())
            if payload is not None:
                self._refresh_references(payload, provided)
                state = {
                    "pending_count": len(pending), "next_reads": pending[:2],
                    "notice": "本请求正文之外仍待提供的证据见 next_reads。索引不代表正文已提供；已提供不等于已核验。",
                }
                payload["verification_reads"] = state
                text = _json(payload)
                if text != message.get_text_content():
                    message.content = [TextBlock(text=text)]
                break

    def _refresh_references(self, value, provided):
        if isinstance(value, list):
            for item in value:
                self._refresh_references(item, provided)
        elif isinstance(value, dict):
            ref = value.get("history_context")
            if "delivery" in value and isinstance(ref, dict):
                key = (ref.get("seq"), (ref.get("recall") or {}).get("json_path", ""))
                value["delivery"] = "provided" if key in provided else "pending"
            for key, item in value.items():
                if key != "verification_reads":
                    self._refresh_references(item, provided)

    def _reference(self, seq, path, label, *, provided=False, complete=False):
        self._need(seq, path, label, provided=provided, complete=complete)
        return {"history_context": self.history.reference(seq, path=path),
                "delivery": "provided" if provided else "pending"}

    def _compact_payload(self, message):
        original = self.payloads.get(message.id)
        if original is None or message.metadata.get("edge_cloud_history_ref"):
            return
        seq = self.history.snapshot(self.raw_payloads[message.id], stage="verifier")
        provided = message.id in self.delivered_payloads
        payload = copy.deepcopy(original)
        for index, record in enumerate(payload.get("tool_results", [])):
            if not isinstance(record, dict) or len(_json(record)) <= 400:
                continue
            path = f"/tool_results/{index}"
            entry = {}
            for field, value in record.items():
                if field == "text_evidence" or len(_json(value)) <= 256:
                    entry[field] = value
                else:
                    pointer = path + "/" + field.replace("~", "~0").replace("/", "~1")
                    entry[field] = self._reference(seq, pointer, f"调用 {index + 1} 的 {field}", provided=provided)
                    if field == "arguments" and isinstance(value, dict):
                        entry[field].update({key: value[key] for key in (
                            "file_path", "path", "source_path", "destination_path", "src", "dst",
                            "start_line", "end_line", "offset", "limit", "mode", "skill",
                        ) if key in value})
            entry["record_context"] = self.history.reference(seq, path=path)
            payload["tool_results"][index] = entry
        if len(_json(payload.get("final_answer", ""))) > 256:
            payload["final_answer"] = self._reference(seq, "/final_answer", "执行者最终回答", provided=provided)
        skill = payload.get("skill")
        if isinstance(skill, dict) and len(_json(skill.get("files", ""))) > 256:
            skill["files"] = self._reference(seq, "/skill/files", "执行时 Skill 文件目录", provided=provided)
        payload["history_context"] = {
            **self.history.reference(seq),
            "notice": "完整校验输入已保存；用 recall_history 按 json_path 分页读取原文。索引不表示正文已提供。",
        }
        message.content = [TextBlock(text=_json(payload))]
        message.metadata = {**message.metadata, "edge_cloud_history_ref": True}

    def _fold(self, message, block):
        if block.metadata.get("edge_cloud_history_ref"):
            return
        identity = (message.id, block.id)
        page = self._page(block)
        if page:
            seq, path = page["seq"], page["json_path"]
            ref = self.history.reference(seq, path=path)
            ref["recall"]["offset"] = page["offset"]
        else:
            if identity not in self.roots:
                self.roots[identity] = (self.history.snapshot(_text(block.output), stage="verifier"), "")
            seq, path = self.roots[identity]
            provided = identity in self.delivered_blocks
            self._need(seq, path, block.name + " 返回正文", provided=provided, complete=block.name == "Skill")
            ref = self.history.reference(seq, path=path)
        block.output = [TextBlock(text=_json({
            "history_context": ref,
            "delivery": "provided" if identity in self.delivered_blocks else "pending",
            "notice": "原文仍可回读；未提供的正文继续待核验。",
        }))]
        block.metadata["edge_cloud_history_ref"] = True

    @staticmethod
    def _set_page(block, page, text):
        page = {**page, "content": text}
        end = page["offset"] + len(text)
        page["complete"] = end >= page["total_chars"]
        page["next_offset"] = None if page["complete"] else end
        block.output = [TextBlock(text=_json(page))]

    def _archive_delivered_pairs(self, messages):
        """Retire complete old read pairs only after their actual delivery.

        Raw calls and the pages actually provided stay in the archive; pending
        ranges continue to reference the original evidence, not this archive.
        """
        retired = set()
        for index in range(len(messages) - 1):
            call, result = messages[index:index + 2]
            calls = [block for block in call.content if isinstance(block, ToolCallBlock)]
            results = [block for block in result.content if isinstance(block, ToolResultBlock)]
            if not calls or not results or {block.id for block in calls} != {block.id for block in results}:
                continue
            if not all((result.id, block.id) in self.delivered_blocks for block in results):
                continue
            if call.id not in self.tool_messages or result.id not in self.tool_messages:
                continue
            self.archived_batches.setdefault(call.id, [self.tool_messages[call.id], self.tool_messages[result.id]])
            retired.update((call.id, result.id))
        if not retired:
            return
        seq = self.history.snapshot(_json({"read_batches": list(self.archived_batches.values())}), stage="verifier")
        messages[:] = [message for message in messages
                       if message.id not in retired and not message.metadata.get("verifier_read_archive")]
        # A text observation has no dangling tool results. The original protocol
        # pairs can be recalled intact, including failed reads and their arguments.
        messages.insert(2, Msg(name="verifier_history", role="assistant", content=[TextBlock(text=_json({
            "provided_read_batches": len(self.archived_batches),
            "history_context": self.history.reference(seq),
            "notice": "已提供给模型的旧读取记录已归档；必要原文仍可回读。未提供的证据范围见 verification_reads。",
        }))], metadata={"verifier_read_archive": True}))

    async def prepare(self, model, messages, tools):
        budget = budget_for_model(model)
        if budget is None:
            raise ConfigurationException("无法读取 Verify 模型的上下文长度或最大输出。", config_key="model.context_budget")
        self._observe(messages)

        if self.history is None:
            tokens = await model.count_tokens(messages=messages, tools=tools)
            if budget.exceeded(tokens):
                raise budget.error(model, tokens, "校验历史库不可用，无法安全归档超长证据，本次请求未发送。")
            return messages

        async def fits(items):
            self._state(items)
            return not budget.exceeded(await model.count_tokens(messages=items, tools=tools))

        # No ratio trigger: at equality, or above 80% but within C - O, leave
        # the input intact. Only delivery-state refreshes survive prior compaction.
        if not self.active and not budget.exceeded(await model.count_tokens(messages=messages, tools=tools)):
            return messages
        incoming = messages
        messages = copy.deepcopy(messages)
        if await fits(messages):
            return incoming if messages == incoming else messages
        self.active = True
        observations = [(message, block) for message in messages for block in message.content
                        if isinstance(block, ToolResultBlock)]
        for message, block in observations:
            if (message.id, block.id) in self.delivered_blocks:
                self._fold(message, block)
                if await fits(messages):
                    return messages
        for message in messages:
            if message.role == "user":
                self._compact_payload(message)
                if await fits(messages):
                    return messages

        # Retain every new call/result pair. All new recall pages share the
        # available space; none is silently classified as an old observation.
        pages = []
        for message, block in observations:
            page = self._page(block)
            if page and (message.id, block.id) not in self.delivered_blocks:
                pages.append((block, page))
                self._set_page(block, page, "")
            else:
                self._fold(message, block)

        # A large directory is paged independently from result bodies. The
        # exact original task/conditions and all call statuses remain durable.
        if not await fits(messages):
            self._archive_delivered_pairs(messages)
        if not await fits(messages):
            for message in messages:
                payload = _object(message.get_text_content()) if message.role == "user" and not _is_steering(message) else None
                skill = payload.get("skill") if payload else None
                if not isinstance(skill, dict) or not isinstance(skill.get("instructions"), str):
                    continue
                if len(skill["instructions"]) <= 256 or "history_context" not in payload:
                    continue
                seq = payload["history_context"]["seq"]
                skill["instructions"] = self._reference(
                    seq, "/skill/instructions", "执行时 Skill 正文", complete=True,
                    provided=message.id in self.delivered_payloads,
                )
                message.content = [TextBlock(text=_json(payload))]
                if await fits(messages):
                    break
        if not await fits(messages):
            for message in messages:
                payload = _object(message.get_text_content()) if message.role == "user" and not _is_steering(message) else None
                rows = payload.get("tool_results") if payload else None
                if not isinstance(rows, list) or not rows:
                    continue
                if message.id not in self.catalogs:
                    seq = self.history.snapshot(_json({"calls": rows}), stage="verifier")
                    self.catalogs[message.id] = (seq, rows)
                    for index in range(len(rows)):
                        self._need(seq, f"/calls/{index}", f"工具调用目录第 {index + 1} 条", complete=True,
                                   provided=message.id in self.delivered_payloads)
                seq, rows = self.catalogs[message.id]
                lo, hi = 0, len(rows)
                while lo < hi:
                    middle = (lo + hi + 1) // 2
                    self._catalog_page(message, payload, seq, rows, middle)
                    if await fits(messages):
                        lo = middle
                    else:
                        hi = middle - 1
                self._catalog_page(message, payload, seq, rows, lo)
                if await fits(messages):
                    break
        if not await fits(messages):
            tokens = await model.count_tokens(messages=messages, tools=tools)
            raise budget.error(model, tokens, "校验任务、条件或必要证据索引仍超过预算，本次请求未发送。")
        if pages:
            scale = max(len(page["content"]) for _block, page in pages)
            lo, hi = 0, scale
            while lo < hi:
                middle = (lo + hi + 1) // 2
                for block, page in pages:
                    self._set_page(block, page, page["content"][:len(page["content"]) * middle // scale])
                if await fits(messages):
                    lo = middle
                else:
                    hi = middle - 1
            for block, page in pages:
                self._set_page(block, page, page["content"][:len(page["content"]) * lo // scale] if scale else "")
        if not await fits(messages):
            tokens = await model.count_tokens(messages=messages, tools=tools)
            raise budget.error(model, tokens, "校验输入仍超过预算，本次请求未发送。")
        return messages

    def _catalog_page(self, message, payload, seq, rows, count):
        unseen = [index for index in range(count, len(rows)) if (seq, f"/calls/{index}") not in self.provided]
        next_index = unseen[0] if unseen else None
        payload["tool_results"] = rows[:count]
        payload["tool_catalog"] = {
            "total": len(rows), "shown": count, "remaining": len(unseen),
            "next_index": next_index,
            "history_context": self.history.reference(seq, path=f"/calls/{next_index}" if unseen else "/calls"),
        }
        message.content = [TextBlock(text=_json(payload))]
