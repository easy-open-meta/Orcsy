"""Phase 1 团队记忆自检：架构黑板报注入、批次共享剪贴板、依赖成果概览。"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import ProjectConfig
from orcsy.prompts import (ARCHITECTURE_FILE, ARCHITECTURE_MAX_CHARS,
                           build_role_prompt_extra, build_system_prompt)
from orcsy.session_bus import (SessionBus, batch_id_for, bus_path,
                               cleanup_if_finished, prune_expired)
from orcsy.task_model import parse_task_file, read_dependency_overview
from orcsy.tools import ToolContext, dispatch_tool

ROOT = "T-ROOT"


def _write_task(tasks_dir: Path, task_id: str, *, status: str = "open",
                parent: str = "", body: str = "任务正文", archived: bool = False) -> Path:
    d = tasks_dir / "done" if archived else tasks_dir
    d.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"title: {task_id}", "priority: P2", f"status: {status}"]
    if parent:
        fm.append(f"parent_id: {parent}")
    fm.append("---")
    p = d / f"{task_id}.md"
    p.write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
    return p


def check_architecture_prompt() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="phase1-arch-"))
    try:
        proj = ProjectConfig(name="arch", path=tmp)
        text = "# 架构\n\n- API 层：/api\n- 数据层：sqlite\n"
        prompt = build_system_prompt(proj, "# guide", False, architecture=text)
        assert "项目架构黑板报（ARCHITECTURE.md）" in prompt
        assert "/api" in prompt and "维护约定" in prompt
        assert "英文" in prompt, "黑板报维护约定必须要求英文书写以节省 token"
        absent = build_system_prompt(proj, "# guide", False)
        assert "<architecture>" not in absent
        long_text = "A" * (ARCHITECTURE_MAX_CHARS + 500)
        long_prompt = build_system_prompt(proj, "# guide", False, architecture=long_text)
        assert "已截断" in long_prompt and "A" * (ARCHITECTURE_MAX_CHARS + 1) not in long_prompt

        pm = build_role_prompt_extra("pm", "PM", {"backend": {"name": "后端", "role": "backend"}})
        dev = build_role_prompt_extra("backend", "后端", None)
        assert ARCHITECTURE_FILE in pm and "架构黑板报" in pm
        assert "update_architecture" in pm and "英文" in pm
        assert ARCHITECTURE_FILE in dev and "session_share" in dev
        assert "英文" in dev
        qa = build_role_prompt_extra("qa", "QA", None)
        assert "session_read" in qa and "session_share" in qa

        # 专用工具只能写 ARCHITECTURE.md，PM/开发不需要任意文件写权限
        ctx = ToolContext(root=tmp)
        out = dispatch_tool(ctx, "update_architecture", json.dumps(
            {"mode": "replace", "content": "# 架构\n\n- API：/api"}))
        assert "整体重写" in out, out
        arch = tmp / ARCHITECTURE_FILE
        assert arch.is_file() and "/api" in arch.read_text(encoding="utf-8")
        out = dispatch_tool(ctx, "update_architecture", json.dumps(
            {"mode": "append", "content": "- 新增 /api/users"}))
        assert "追加" in out
        assert "/api/users" in arch.read_text(encoding="utf-8")
        print("ok: 架构黑板报注入/截断/维护工具/角色约定")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_session_bus() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="phase1-bus-"))
    try:
        proj_dir = tmp / "proj"
        tasks = proj_dir / "tasks"
        _write_task(tasks, ROOT, status="done", archived=True)
        _write_task(tasks, "T-A", status="done", parent=ROOT, archived=True)
        _write_task(tasks, "T-B", status="open", parent=ROOT)
        _write_task(tasks, "T-C", status="open")

        proj = ProjectConfig(name="bus", path=proj_dir)
        state = tmp / "state"
        ta = parse_task_file(tasks / "done" / "T-A.md")
        tb = parse_task_file(tasks / "T-B.md")
        tc = parse_task_file(tasks / "T-C.md")
        assert batch_id_for(ta, tasks) == ROOT
        assert batch_id_for(tb, tasks) == ROOT
        assert batch_id_for(tc, tasks) == "T-C"

        bus_a = SessionBus(proj, state, ta, actor="backend")
        bus_b = SessionBus(proj, state, tb, actor="qa")
        assert bus_a.path == bus_b.path
        assert bus_a.batch_id == ROOT
        assert bus_a.share("backend_port", "http://127.0.0.1:5173")["count"] == 1
        bus_b.share("qa_account", "tester/secret")
        assert bus_b.read("backend_port") == "http://127.0.0.1:5173"
        keys = bus_b.keys()
        assert {k["key"] for k in keys} == {"backend_port", "qa_account"}
        assert all("value" not in k for k in keys)
        # 跨批次隔离
        bus_c = SessionBus(proj, state, tc, actor="solo")
        assert bus_c.path != bus_b.path
        assert bus_c.read("backend_port") is None

        # 工具层：share/read 正常；无上下文时报错而不是抛异常
        ctx = ToolContext(root=proj_dir, session_bus=bus_a, current_task=ta)
        out = dispatch_tool(ctx, "session_share", json.dumps(
            {"key": "qa_hint", "value": "先跑 pytest"}))
        assert "已共享 qa_hint" in out, out
        out = dispatch_tool(ctx, "session_read", json.dumps({"key": "qa_hint"}))
        assert out == "先跑 pytest", out
        out = dispatch_tool(ctx, "session_read", json.dumps({"key": ""}))
        assert "qa_hint" in out and "先跑 pytest" not in out, out
        missing = dispatch_tool(ToolContext(root=proj_dir), "session_read",
                                json.dumps({"key": "x"}))
        assert "没有批次共享上下文" in missing, missing

        # 批次未结束：T-B 仍 open → 不清理
        assert cleanup_if_finished(proj, state, tb, tasks_dir=tasks) is False
        assert bus_path(state, "bus", ROOT).is_file()

        # 全部 done → 清理
        _write_task(tasks, "T-B", status="done", parent=ROOT, archived=True)
        (tasks / "T-B.md").unlink()
        tb_done = parse_task_file(tasks / "done" / "T-B.md")
        assert cleanup_if_finished(proj, state, tb_done, tasks_dir=tasks) is True
        assert not bus_path(state, "bus", ROOT).is_file()

        # TTL 兜底：旧文件被 prune
        old = bus_path(state, "bus", "OLD")
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("{}", encoding="utf-8")
        old_time = time.time() - 30 * 24 * 3600
        import os
        os.utime(old, (old_time, old_time))
        assert prune_expired(state) == 1 and not old.exists()
        print("ok: 批次共享（批次解析/跨角色读写/隔离/工具/生命周期/TTL）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_dependency_overview() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="phase1-deps-"))
    try:
        tasks = tmp / "tasks"
        _write_task(tasks, "D-DONE", status="done", archived=True,
                    body="需求正文\n\n## Agent 处理结果\n\n- 新增 GET /api/x\n- 文件：app/x.py")
        _write_task(tasks, "D-OPEN", status="open")
        items = read_dependency_overview(tasks, ["D-DONE", "D-OPEN", "D-MISSING"])
        assert [x["id"] for x in items] == ["D-DONE", "D-OPEN", "D-MISSING"]
        assert items[0]["status"] == "done" and items[0]["archived"] is True
        assert "GET /api/x" in items[0]["summary"] and "新增" in items[0]["summary"]
        assert items[1]["status"] == "open" and items[1]["summary"] == ""
        assert items[2]["missing"] is True and items[2]["title"] == ""
        print("ok: 依赖成果概览（done 摘要 / open 状态 / missing）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_architecture_prompt()
    check_session_bus()
    check_dependency_overview()
    print("\nPhase 1 团队记忆自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
