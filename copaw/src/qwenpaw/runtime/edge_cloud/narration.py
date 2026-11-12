"""Readable display summaries derived from validated runtime decisions."""

from .roles import AGENT_NAMES


def _brief(text: str, limit: int = 280) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[:limit] + "…"


def router_summary(route, *, requested_target: str) -> str:
    target = AGENT_NAMES[route.target_agent]
    lines = [f"已选择 {target} 处理本次请求。"]
    reason = route.reason.strip() or {
        "device_text": "简单文本任务，可以由端侧直接处理。",
        "device_tools": "基础文件任务，符合端侧工具范围与轮数限制。",
        "edge_skill": "已确认一个现有 Skill 可以独立完成任务。",
        "complex_or_negative": "该任务需要云端 Agent 处理。",
    }[route.task_type]
    if requested_target != route.target_agent:
        lines.append(
            f"原因：原建议交给 {AGENT_NAMES[requested_target]}，"
            "运行时检查未满足执行条件，已调整为云端处理。"
        )
        lines.append(f"原路由理由：{_brief(reason)}")
    else:
        lines.append(f"原因：{_brief(reason)}")
    return "\n".join(lines)


def verifier_summary(
    verdict,
    *,
    executed_by: str,
    files_submitted: bool,
) -> str:
    lines = [f"已检查 {AGENT_NAMES[executed_by]} 的执行结果。"]
    if verdict.passed:
        lines.extend(
            [
                "校验结论：通过。",
                "原因：VerifyAgent 判定执行过程和结果满足任务要求。",
                f"本次接受 {AGENT_NAMES[executed_by]} 的执行结果。",
            ]
        )
        if files_submitted:
            lines.append("已提交的文件清单通过校验，正常收尾后按原流程交付。")
    else:
        lines.append("校验结论：未通过。")
        reason = verdict.failure_reason or "校验模型判定尚未满足任务要求。"
        lines.append(f"原因：{_brief(reason)}")
        if verdict.missing_requirements:
            lines.append("待补充的要求：" + _brief("；".join(verdict.missing_requirements)))
        lines.append("接下来交给 CloudAgent 继续处理未完成的部分。")
    return "\n".join(lines)
