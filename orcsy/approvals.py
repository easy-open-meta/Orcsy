"""人工审批引擎：访问权限模式下，敏感工具调用挂起等待看板批准。

三档访问权限（全局 permissions.mode，项目 permission_mode 可覆盖）：
- ask_on_change 变更前询问：一切变更类操作（写/改/删/移/执行命令）先问；
- autonomous    自主模式：仅高风险操作（删/移/执行命令）先问；
- full_access   完全访问：不问（默认，与历史行为一致）。

机制：任务线程在 dispatch_tool 前调用 ApprovalManager.request() 阻塞在
threading.Event 上；看板经 full_state 看到 pending 列表，POST /api/approval
批准/拒绝后唤醒。超时（permissions.timeout_minutes，默认 30）自动拒绝，
无人值守时任务不会永久挂起。审批状态只存内存——进程重启后挂起请求随
任务线程一起消失（任务停在 in_progress，由启动恢复逻辑重置回 open）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field

from .logging_setup import get_logger

log = get_logger("approvals")

# 三档模式的规范值（config 校验与 UI 选项同源）
PERMISSION_MODES = ("ask_on_change", "autonomous", "full_access")

MODE_LABELS = {
    "ask_on_change": "变更前询问",
    "autonomous": "自主模式",
    "full_access": "完全访问",
}

# 变更类操作（ask_on_change 询问）：改文件 + 执行命令
MUTATING_TOOLS = frozenset({
    "write_file", "edit_file", "multi_edit",
    "delete_file", "move_file", "run_command",
})

# 高风险操作（autonomous 也询问）：破坏性文件操作 + 执行命令
RISKY_TOOLS = frozenset({"delete_file", "move_file", "run_command"})

# 审批请求 args 预览的截断长度（看板卡片展示用）
_ARGS_PREVIEW_CHARS = 300


def requires_approval(mode: str, tool: str) -> bool:
    """按模式判定一个工具调用是否需要人工审批。

    只读类（list_dir/read_file/grep/find_files/read_skill/web_fetch）与
    协议工具 finish 在任何模式下都不询问。
    """
    if mode == "full_access":
        return False
    if mode == "ask_on_change":
        return tool in MUTATING_TOOLS
    if mode == "autonomous":
        return tool in RISKY_TOOLS
    return False  # 未知模式按最宽松处理（校验层已挡住非法值）


@dataclass
class ApprovalRequest:
    """一条挂起的审批请求。"""

    id: str
    project: str
    task_id: str
    tool: str
    args_preview: str
    mode: str
    created_at: float = field(default_factory=time.time)
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    always: bool = False
    responded: bool = False


@dataclass
class ApprovalOutcome:
    """request() 的返回：批准与否 + 是否「本任务内始终允许」+ 是否超时。"""

    approved: bool
    always: bool = False
    timed_out: bool = False


def args_preview(args: dict | str, limit: int = _ARGS_PREVIEW_CHARS) -> str:
    """把工具参数压成单行安全预览（审批卡片与进度行共用）。"""
    try:
        text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(args)
    text = " ".join(text.split())  # 压平换行，卡片里单行展示
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


class ApprovalManager:
    """进程级审批注册表：任务线程挂起等待，看板线程响应唤醒。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, ApprovalRequest] = {}

    def request(self, project: str, task_id: str, tool: str,
                args: dict | str, mode: str, timeout_seconds: float) -> ApprovalOutcome:
        """登记一条审批请求并阻塞等待响应；超时自动拒绝。"""
        req = ApprovalRequest(id=uuid.uuid4().hex, project=project, task_id=task_id,
                              tool=tool, args_preview=args_preview(args), mode=mode)
        with self._lock:
            self._pending[req.id] = req
        log.info("等待人工审批 [%s] %s · %s %s", project, task_id, tool, req.args_preview[:80])
        try:
            resolved = req.event.wait(timeout=max(1.0, timeout_seconds))
        finally:
            with self._lock:
                self._pending.pop(req.id, None)
        if not resolved:
            log.warning("审批超时自动拒绝 [%s] %s · %s", project, task_id, tool)
            return ApprovalOutcome(approved=False, timed_out=True)
        return ApprovalOutcome(approved=req.approved, always=req.always)

    def respond(self, request_id: str, approved: bool, always: bool = False) -> bool:
        """看板响应一条审批；未知/已处理的 id 返回 False。"""
        with self._lock:
            req = self._pending.get(request_id)
            if req is None or req.responded:
                return False
            req.approved = bool(approved)
            req.always = bool(always) and bool(approved)  # 「始终允许」只在批准时有意义
            req.responded = True
        log.info("审批%s：%s · %s（%s）", "通过" if approved else "拒绝",
                 req.project, req.tool, req.task_id)
        req.event.set()
        return True

    def pending(self) -> list[dict]:
        """当前挂起的审批列表（full_state 曝光给看板）。"""
        now = time.time()
        with self._lock:
            items = list(self._pending.values())
        return [{
            "id": r.id, "project": r.project, "task_id": r.task_id,
            "tool": r.tool, "args_preview": r.args_preview, "mode": r.mode,
            "created_at": r.created_at,
            "waited_seconds": int(now - r.created_at),
        } for r in items]

    def clear(self) -> None:
        """丢弃全部挂起请求（测试用）。"""
        with self._lock:
            items = list(self._pending.values())
            self._pending.clear()
        for r in items:
            r.event.set()


# 进程级单例：executor（请求方）与 AppState（曝光/响应方）共用，
# 免去跨 Scheduler/Agent/Executor 构造器长链透传
_MANAGER: ApprovalManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> ApprovalManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = ApprovalManager()
        return _MANAGER
