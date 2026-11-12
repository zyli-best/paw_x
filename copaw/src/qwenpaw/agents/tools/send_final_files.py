"""Submit selected final deliverables without prematurely emitting attachments."""

import asyncio
import json
import unicodedata
from pathlib import Path
from urllib.parse import unquote

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from ...runtime.final_files import current, snapshot_files, source_path
from ...runtime.tool_registry import tool_descriptor
from .existing_path_guard import resolve_existing_file_path


@tool_descriptor(
    requires_features=("final_files",), requires_sandbox=("file_read",)
)
async def send_final_files_to_user(file_paths: list[str]) -> ToolChunk:
    """提交当前任务的最终文件，支持图片、文档、表格、音视频、代码、数据和压缩包。

    完成生成/修改及必要验证后、最终答复前调用。多个文件一次提交完整清单；
    单个文件也使用数组。仅提交直接满足当前任务交付要求的文件，不发送所有改动、
    草稿、缓存、日志、预览或临时脚本，除非它们本身是要求的产物。
    纯文本任务、仅修改工作区而无文件交付需求、用户要求只保存本地时不调用。

    Args:
        file_paths: 非空本地文件路径数组，必须存在且获准访问。优先使用依据最后一次
            成功保存、复制或移动结果核实的最终文件绝对路径，不沿用旧工作目录路径。
            相对路径从本次默认文件根目录解析，不跟随 Shell 的 cwd；不接受目录、
            通配符、URL、猜测或尚未生成的路径。同名文件须核实目录和最终版本。
            其他 Agent 的文件使用协作工具返回的可访问路径，不猜测其工作区。
            每次提交都替换完整清单，不自动合并历史调用。失败后也须重新提交当前任务
            的全部最终文件，包含之前检查通过且仍需交付的文件，不得只提交修正项，
            不得因之前调用过本工具而省略。委派任务只提交本次委派范围的完整清单；
            调用方回收结果后选择整体任务清单。同一清单内重复路径去重。

    Returns:
        ToolChunk.state 为 SUCCESS 或 ERROR。文本内容为 JSON，成功与失败均包含：
        - status：pending_finalization 表示已登记、等待正常收尾；
          pending_verification 表示已登记、等待独立校验；rejected 表示整批拒绝。
        - attempt_id：当前运行时执行尝试的标识，不是每次调用的新 ID；
          没有运行时上下文时为 null。
        - submitted：本次完整清单是否已登记；true 不表示用户已收到。
        - files：已登记的完整清单；失败时为 []。每项 artifact_id 为快照标识，
          name 为文件名，source_path 为实际读取的源文件绝对路径，media_type 为
          MIME 类型，size 为字节数，sha256 为文件内容摘要。核对 source_path，
          与最终落盘位置不一致时修正路径并全量重提。
        - existing_files：路径检查通过的文件，按绝对路径去重。每项 file_path 为
          原始传入路径，resolved_path 为实际绝对路径；仅表示检查时存在且通过源文件
          检查，不表示已提交，也不保证后续快照成功。失败时这些文件未单独提交。
        - errors：全部路径检查失败项，或快照阶段遇到的失败项；成功时为 []。
          每项 file_path 为原始输入，resolved_path 为实际绝对路径，error 为原因；
          请求级错误或无法解析路径时，对应路径字段为 null。
        - error：本次失败的摘要；成功时为空字符串。
        - retry：required 表示是否需要在原权限和执行预算内修正后重提；
          tool 为本工具名，submission_mode 固定为 replace_all，
          require_complete_file_list 固定为 true，message 为处理指引。
          required=false 不代表已交付；上下文不可用时也为 false，不能反复调用。
        普通执行正常收尾后交付；需要独立校验时，必须先通过校验；
        委派任务先回传调用方，由其选择整体任务最终文件。接收方由运行时决定。
        任一文件失败则整批不生效，返回具体原因；重新提交失败也不会交付旧清单。
        本工具按传入路径读取，不搜索或替换其他目录的同名文件，也不移动文件。
        不生成、修改、合并或压缩文件。不得通过其他工具绕过等待或回传。
    """
    state = current()
    if state is not None:
        async with state.submission_lock:
            return await _submit(file_paths, state)
    return await _submit(file_paths, state)


async def _submit(file_paths, state):
    task_scope = (
        "当前委派任务" if state is not None and state.recipient == "parent"
        else "当前任务"
    )
    complete_list = (
        f"再次调用 send_final_files_to_user 时，必须提供{task_scope}的完整最终文件清单，"
        "包含之前检查通过且仍需交付的文件和修正后的文件；不得只提交修正项，"
        "不得因之前调用过本工具而省略。历史清单不会自动合并。"
    )

    def result(*, existing_files=None, errors=None, error="", retry_required=True):
        rejected = bool(error or errors)
        if error and not errors:
            errors = [{"file_path": None, "resolved_path": None, "error": error}]
        summary = error or ("Final-file submission rejected." if rejected else "")
        if rejected:
            data = {
                "status": "rejected",
                "attempt_id": state.attempt_id if state is not None else None,
                "submitted": False,
                "files": [],
            }
            if retry_required:
                message = (
                    "本次整批未提交，existing_files 仅表示路径检查通过，未单独提交。"
                    "请核对 errors 中的失败项，以及最后一次成功生成、保存、复制或移动的结果，"
                    "必要时查询相关目录，确认最终文件的实际绝对路径。" + complete_list
                )
            else:
                message = "当前请求无法提交最终文件，请按原流程报告不可用，不要反复调用或声称已交付。"
        else:
            data = state.manifest()
            message = "完整清单已登记，等待正常收尾及必要校验后交付或回传。" + complete_list
        data.update({
            "existing_files": existing_files or [],
            "errors": errors or [],
            "error": summary,
            "retry": {
                "required": rejected and retry_required,
                "tool": "send_final_files_to_user",
                "submission_mode": "replace_all",
                "require_complete_file_list": True,
                "message": message,
            },
        })
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR if rejected else ToolResultState.SUCCESS,
            content=[TextBlock(text=json.dumps(data, ensure_ascii=False))],
        )

    if state is None or state.invalid_context or state.finished:
        return result(
            error="Final-file delivery is unavailable in this request.",
            retry_required=False,
        )
    state.submitted = False
    state.files = []
    state.verified = False
    if not isinstance(file_paths, list) or not file_paths:
        return result(error="file_paths must be a nonempty array.")

    def prepare():
        from ...security.tool_guard.guardians.file_guardian import (
            FilePathToolGuardian,
        )

        guardian = FilePathToolGuardian()
        paths, existing_files, errors = [], [], []
        seen = set()
        for raw in file_paths:
            if not isinstance(raw, str) or not raw.strip():
                errors.append(
                    {
                        "file_path": raw,
                        "resolved_path": None,
                        "error": "Expected a nonempty file path.",
                    }
                )
                continue
            checked_path = None
            try:
                # Match the permission layer's canonical path without parsing
                # the shared guard's human-readable diagnostics.
                checked_path = str(source_path(raw))
                normalized = unicodedata.normalize("NFC", unquote(raw))
                resolved = resolve_existing_file_path(
                    normalized,
                    tool_name="send_final_files_to_user",
                    path_argument="file_paths",
                )
                if not isinstance(resolved, ToolChunk):
                    path = Path(resolved).resolve()
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append({
                    "file_path": raw,
                    "resolved_path": checked_path,
                    "error": str(exc),
                })
                continue
            if isinstance(resolved, ToolChunk):
                errors.append(
                    {
                        "file_path": raw,
                        "resolved_path": checked_path,
                        "error": "\n".join(
                            b.text
                            for b in resolved.content
                            if hasattr(b, "text")
                        ),
                    }
                )
                continue
            if guardian.guard(
                "send_final_files_to_user", {"file_paths": [str(path)]}
            ):
                errors.append(
                    {
                        "file_path": raw,
                        "resolved_path": str(path),
                        "error": "Access to sensitive file is blocked.",
                    }
                )
                continue
            if path not in seen:
                seen.add(path)
                paths.append(path)
                existing_files.append({
                    "file_path": raw,
                    "resolved_path": str(path),
                })
        if errors:
            return [], existing_files, errors
        files = []
        for path, checked in zip(paths, existing_files):
            try:
                files.extend(snapshot_files([path], state))
            except (OSError, ValueError) as exc:
                # A source can disappear or change after validation. Associate
                # the failure with it and never activate a partial manifest.
                return [], existing_files, [{**checked, "error": str(exc)}]
        return files, existing_files, []

    files, existing_files, errors = await asyncio.to_thread(prepare)
    if errors:
        return result(existing_files=existing_files, errors=errors)
    state.files = files
    state.submitted = True
    return result(existing_files=existing_files)
