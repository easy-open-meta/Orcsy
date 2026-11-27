"""GitLab / GitHub 工单型 Provider 共用的 issue 文本与评论命令解析。"""

from __future__ import annotations

import json


def _parse_approval_command(body: str) -> tuple[str, str]:
    """解析评论首行的 /approve 或 /reject；返回 (decision, comment)。"""
    text = str(body or "").strip()
    if not text:
        return "", ""
    first, _, rest = text.partition("\n")
    parts = first.strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    comment = parts[1].strip() if len(parts) > 1 else ""
    if command in ("/approve", "/lgtm"):
        return "approved", comment
    if command in ("/reject", "/deny"):
        return "rejected", comment
    return "", ""


def _issue_title(manifest: dict) -> str:
    return (f"[Orcsy 审批] {manifest.get('project', '')}/"
            f"{manifest.get('task_id', '')} · "
            f"{manifest.get('risk', {}).get('level', '')}")


def _issue_body(manifest: dict) -> str:
    risk = manifest.get("risk") or {}
    emails = ", ".join(manifest.get("allowed_approvers") or [])
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    return "\n".join([
        "## Orcsy 合入前风险审批",
        "",
        f"- 请求：`{manifest.get('request_id', '')}`",
        f"- 项目 / 任务：{manifest.get('project', '')} / "
        f"{manifest.get('task_id', '')}",
        f"- 标题：{manifest.get('title', '')}",
        f"- 风险等级：**{risk.get('level', '')}**",
        f"- Commit：`{manifest.get('commit', '')}`",
        f"- Digest：`{manifest.get('digest', '')}`",
        f"- 需要会签：{manifest.get('required_approvals', 0)} 人",
        f"- 允许审批 email：{emails or '（未配置）'}",
        "",
        "审批方式：在本 issue 评论 `/approve` 或 `/reject 原因`。",
        "只有 Orcsy 配置中登记的 email 对应账号会计入 quorum；"
        "未登记账号的评论会被忽略。",
        "",
        "```json",
        payload,
        "```",
    ])


__all__ = ["_issue_body", "_issue_title", "_parse_approval_command"]
