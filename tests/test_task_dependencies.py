"""M1 单元测试：DAG 依赖引擎、create_tasks 依赖/角色校验、按任务 Worktree 沙盒隔离与增量落地主工作区（不自动 commit）。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import LLMConfig, ProjectConfig
from orcsy.executor import TaskExecutor
from orcsy.git_ops import Git
from orcsy.llm import LLMClient
from orcsy.task_model import (
    append_task_dependencies,
    collect_task_statuses,
    detect_dependency_cycle,
    evaluate_task_dependencies,
    increment_task_file_qa_round,
    parse_task_file,
    read_done_summaries,
    set_task_file_assignee,
    set_task_file_dependencies,
)
from orcsy.tools import ToolContext, dispatch_tool


def _write_task(tasks_dir: Path, stem: str, title: str, *,
                status: str = "open", priority: str = "P1",
                assignee: str = "", depends_on: list[str] | None = None,
                body: str = "任务内容说明。") -> Path:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"title: {title}", f"priority: {priority}", f"status: {status}"]
    if assignee:
        fm.append(f"assignee: {assignee}")
    if depends_on:
        fm.append("depends_on: [" + ", ".join(depends_on) + "]")
    fm.append("---")
    p = tasks_dir / f"{stem}.md"
    p.write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
    return p


def check_parse_and_mutators() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="m1-parse-"))
    try:
        td = tmp / "tasks"
        p = _write_task(td, "P1-ui", "前端页", assignee="frontend", depends_on=["P1-api"])
        t = parse_task_file(p)
        assert t.assignee == "frontend"
        assert t.depends_on == ["P1-api"]
        assert t.parent_id == "" and t.split_depth == 0 and t.qa_round == 0

        # 多行 YAML depends_on 也能宽容解析
        p2 = td / "P1-multi.md"
        p2.write_text(
            "---\ntitle: 多行依赖\npriority: P1\nstatus: open\ndepends_on:\n  - a\n  - b\n  - a\nparent_id: P0-root\nsplit_depth: 1\nqa_round: 2\ncreated_by: pm\n---\n\n内容\n",
            encoding="utf-8",
        )
        t2 = parse_task_file(p2)
        assert t2.depends_on == ["a", "b"], t2.depends_on
        assert t2.parent_id == "P0-root" and t2.split_depth == 1 and t2.qa_round == 2
        assert t2.created_by == "pm"

        # mutators
        assert set_task_file_assignee(p, "backend")
        assert parse_task_file(p).assignee == "backend"
        assert set_task_file_assignee(p, None)
        assert parse_task_file(p).assignee == ""

        merged = append_task_dependencies(p, ["P1-api", "P1-db"])
        assert merged == ["P1-api", "P1-db"]
        assert parse_task_file(p).depends_on == ["P1-api", "P1-db"]

        # 多行 depends_on 覆盖改写干净
        assert set_task_file_dependencies(p2, ["x", "y"])
        assert parse_task_file(p2).depends_on == ["x", "y"]
        assert set_task_file_dependencies(p2, [])
        assert parse_task_file(p2).depends_on == []

        assert increment_task_file_qa_round(p) == 1
        assert increment_task_file_qa_round(p) == 2
        assert parse_task_file(p).qa_round == 2
        print("ok: task_model 新字段解析与 mutators")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_dependency_evaluation_and_cycles() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="m1-dag-"))
    try:
        td = tmp / "tasks"
        done_dir = td / "done"
        _write_task(done_dir, "P1-db", "建表", status="done",
                    body="需求说明\n\n## Agent 处理结果\n\n- 状态：已完成\n- 结论：已建 users 表\n")
        p_api = _write_task(td, "P1-api", "写接口", status="open", depends_on=["P1-db"])
        p_ui = _write_task(td, "P1-ui", "写界面", status="open", depends_on=["P1-db", "P1-api"])
        p_blk = _write_task(td, "P1-blk", "受阻任务", status="blocked")
        p_wait_blk = _write_task(td, "P1-down", "下游受阻", status="open", depends_on=["P1-blk"])

        all_statuses, done_ids = collect_task_statuses(td)
        assert "P1-db" in done_ids

        ev_api = evaluate_task_dependencies(parse_task_file(p_api), all_statuses, done_ids)
        assert ev_api["dep_state"] == "ready" and ev_api["done_count"] == 1

        ev_ui = evaluate_task_dependencies(parse_task_file(p_ui), all_statuses, done_ids)
        assert ev_ui["dep_state"] == "waiting"
        assert ev_ui["blocking_deps"] == [{"id": "P1-api", "status": "open"}]

        ev_down = evaluate_task_dependencies(parse_task_file(p_wait_blk), all_statuses, done_ids)
        assert ev_down["dep_state"] == "upstream_blocked"

        assert detect_dependency_cycle(td) is None
        cyc = detect_dependency_cycle(td, extra_edges={"P1-db": ["P1-ui"]})
        assert cyc is not None and len(cyc) >= 3, cyc

        sums = read_done_summaries(td, ["P1-db"])
        assert len(sums) == 1 and "已建 users 表" in sums[0]["summary"]
        print("ok: DAG 依赖状态评估 / 环检测 / 上游摘要读取")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_create_tasks_team_and_refs() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="m1-create-"))
    try:
        td = tmp / "tasks"
        root_p = _write_task(td, "P1-req", "实现登录功能", assignee="pm")
        root_t = parse_task_file(root_p)

        team_roles = {
            "pm": {"name": "PM", "role": "pm", "produces_code": False},
            "backend": {"name": "后端", "role": "backend", "produces_code": True},
            "frontend": {"name": "前端", "role": "frontend", "produces_code": True},
            "qa": {"name": "测试", "role": "qa", "produces_code": False},
        }
        ctx = ToolContext(root=tmp, current_task=root_t, team_roles=team_roles,
                          current_role="pm", max_split_depth=1)

        # 错误校验：未指定 assignee（不写半成品）
        r_bad = dispatch_tool(ctx, "create_tasks", {"tasks": [
            {"title": "缺角色任务"},
        ]})
        assert r_bad.startswith("错误") and "assignee" in r_bad
        assert not (td / "P1-缺角色任务.md").exists()

        # 错误校验：不存在角色
        r_bad2 = dispatch_tool(ctx, "create_tasks", {"tasks": [
            {"title": "错角色", "assignee": "ios"},
        ]})
        assert r_bad2.startswith("错误") and "ios" in r_bad2

        # 错误校验：批内环依赖
        r_cyc = dispatch_tool(ctx, "create_tasks", {"tasks": [
            {"title": "任务A", "assignee": "backend", "depends_on_refs": [1]},
            {"title": "任务B", "assignee": "frontend", "depends_on_refs": [0]},
        ]})
        assert r_cyc.startswith("错误") and "循环" in r_cyc
        assert not (td / "P1-任务A.md").exists()

        # 正常拆分：含批内下标依赖 + 角色 + 标签
        r_ok = dispatch_tool(ctx, "create_tasks", {"tasks": [
            {"title": "后端登录接口", "assignee": "backend", "labels": ["api"]},
            {"title": "前端登录界面", "assignee": "frontend", "depends_on_refs": [0]},
            {"title": "前后端联调验证", "assignee": "qa", "depends_on_refs": [0, 1]},
        ]})
        assert "已创建 3 个子任务" in r_ok, r_ok
        t_fe = parse_task_file(td / "P1-前端登录界面.md")
        t_qa = parse_task_file(td / "P1-前后端联调验证.md")
        assert t_fe.assignee == "frontend" and t_fe.depends_on == ["P1-后端登录接口"]
        assert t_fe.parent_id == "P1-req" and t_fe.split_depth == 1 and t_fe.created_by == "pm"
        assert t_qa.depends_on == ["P1-后端登录接口", "P1-前端登录界面"]

        # 递归深度拦截：split_depth=1 的任务禁止 PM 再拆分
        ctx_deep = ToolContext(root=tmp, current_task=t_fe, team_roles=team_roles,
                               current_role="pm", max_split_depth=1)
        r_deep = dispatch_tool(ctx_deep, "create_tasks", {"tasks": [
            {"title": "套娃", "assignee": "frontend"},
        ]})
        assert r_deep.startswith("错误") and "拆分深度" in r_deep
        print("ok: create_tasks 小组模式校验 / depends_on_refs / 防递归拆分")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _ScriptedLLM(LLMClient):
    """按任务切换响应序列的轻量 FakeLLM。"""

    scripts: dict[str, list[tuple[str, dict]]] = {}
    last_prompts: dict[str, str] = {}

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.messages: list[dict] = []
        self.usage = {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self._step = 0
        self._key = "default"

    def reset(self, system_prompt: str) -> None:
        self.messages = [{"role": "system", "content": system_prompt}]

    def run_task_loop(self, user_prompt: str, ctx, *, max_turns=40, on_progress=None):
        from orcsy.llm import finish_response
        tid = ctx.current_task.id
        self._key = tid
        _ScriptedLLM.last_prompts[tid] = user_prompt
        seq = list(_ScriptedLLM.scripts.get(tid, []))
        for name, args in seq:
            self.usage["turns"] += 1
            if name == "finish":
                return finish_response(args)
            dispatch_tool(ctx, name, args)
        return finish_response({"action": "blocked", "summary": "script exhausted"})


def check_worktree_isolation_land_without_commit() -> None:
    """验证小组模式：强制 Worktree 隔离开发 + 合入 stage 并落地主工作区（零自动 commit）+ 下游可见前置代码。"""
    tmp = Path(tempfile.mkdtemp(prefix="m1-wt-"))
    try:
        proj_dir = tmp / "proj"
        proj_dir.mkdir()
        g = Git(proj_dir)
        g.init()
        g.run("config", "user.email", "t@t.com")
        g.run("config", "user.name", "test")
        (proj_dir / "README.md").write_text("# Demo\n", encoding="utf-8")
        (proj_dir / "AGENT.md").write_text("# Guide\n", encoding="utf-8")
        g.ensure_gitignore(["tasks/", "AGENT.md", ".worktree-arranger/"])
        g.add_all()
        g.commit("chore: init")
        c0 = g.rev_parse("HEAD")

        td = proj_dir / "tasks"
        p_be = _write_task(td, "P1-be", "写后端接口", assignee="backend")
        p_fe = _write_task(td, "P1-fe", "写前端页面", assignee="frontend", depends_on=["P1-be"])
        p_qa = _write_task(td, "P1-qa", "只读联调", assignee="qa", depends_on=["P1-be", "P1-fe"])

        proj = ProjectConfig(name="demo", path=proj_dir)
        llm = LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake-m")

        _ScriptedLLM.scripts = {
            "P1-be": [
                ("write_file", {"path": "backend.py", "content": "def login(): return {'ok': True}\n"}),
                ("finish", {"action": "completed", "summary": "实现 POST /login",
                            "files_changed": ["backend.py"], "commit_message": "feat: add backend.py"}),
            ],
            "P1-fe": [
                ("read_file", {"path": "backend.py"}),  # 验证 T2 在自己独立 worktree 内能读到 T1 刚写的 backend.py
                ("write_file", {"path": "frontend.ts", "content": "export const api = '/login';\n"}),
                ("finish", {"action": "completed", "summary": "对接后端接口完成登录页",
                            "files_changed": ["frontend.ts"], "commit_message": "feat: add frontend.ts"}),
            ],
            "P1-qa": [
                ("read_file", {"path": "backend.py"}),
                ("read_file", {"path": "frontend.ts"}),
                ("finish", {"action": "completed", "summary": "联调验证通过",
                            "files_changed": [], "commit_message": ""}),
            ],
        }

        import orcsy.executor as ex_mod
        orig_llm = ex_mod.LLMClient
        ex_mod.LLMClient = _ScriptedLLM
        try:
            # 1) 后端执行（强制 worktree_isolation=True, commit_and_merge=False, per_task_worktree=True）
            ex_be = TaskExecutor(
                proj, llm, commit_and_merge=False,
                worktree_isolation=True, per_task_worktree=True, produces_code=True,
                team_role="backend",
            )
            res_be = ex_be.run_task(parse_task_file(p_be), "# Guide")
            assert res_be.outcome == "done", res_be.notes
            assert res_be.commit == "" and not res_be.pushed, "不默认提交也不推送"
            assert g.rev_parse("HEAD") == c0, "主分支 HEAD 绝不能自动移动"
            assert (proj_dir / "backend.py").is_file(), "增量已安全落地主分支工作区"
            assert not (proj_dir / ".worktree-arranger" / "P1-be").exists(), "临时 task worktree 应已清理"

            # 模拟 agent 归档前置任务（供上游摘要透传读取）
            (td / "done").mkdir(exist_ok=True)
            p_be.write_text(p_be.read_text(encoding="utf-8") + "\n## Agent 处理结果\n\n- 结论：实现 POST /login\n", encoding="utf-8")
            p_be.replace(td / "done" / p_be.name)

            # 2) 前端执行：从 stage 拉取 worktree，必能读到 backend.py，且 Prompt 含上游摘要
            ex_fe = TaskExecutor(
                proj, llm, commit_and_merge=False,
                worktree_isolation=True, per_task_worktree=True, produces_code=True,
                team_role="frontend",
            )
            res_fe = ex_fe.run_task(parse_task_file(p_fe), "# Guide")
            assert res_fe.outcome == "done", res_fe.notes
            assert "实现 POST /login" in _ScriptedLLM.last_prompts["P1-fe"], "应注入上游前置摘要"
            assert g.rev_parse("HEAD") == c0, "主分支 HEAD 仍停在 C0"
            assert (proj_dir / "backend.py").is_file() and (proj_dir / "frontend.ts").is_file()

            # 3) QA 只读执行：produces_code=False，免建 worktree、无改动不判 blocked
            ex_qa = TaskExecutor(
                proj, llm, commit_and_merge=False,
                worktree_isolation=True, per_task_worktree=True, produces_code=False,
                team_role="qa",
            )
            res_qa = ex_qa.run_task(parse_task_file(p_qa), "# Guide")
            assert res_qa.outcome == "done", f"只读 QA 应通过：{res_qa.notes}"

            # 4) 验证 pending_changes_summary 与人工 commit 后 stage 同步
            pending = g.pending_changes_summary()
            assert pending["count"] >= 2, pending
            manual = g.commit_and_push_manual("feat: complete login feature")
            assert manual["committed"] and g.rev_parse("HEAD") != c0
            assert not g.is_dirty()

            # 下次确保 stage 同步到人工新提交 HEAD
            new_stage = g.ensure_stage_branch(
                f"orcsy/stage/{proj.name}", g.current_branch(),
                stage_worktree=proj_dir / ".worktree-arranger" / "_stage",
            )
            assert new_stage == g.rev_parse("HEAD"), "人工提交后 stage 应同步到新 HEAD"
            print("ok: 强制 Worktree 沙盒隔离 / stage 增量落地主工作区（零自动 commit）/ 人工提交同步")
        finally:
            ex_mod.LLMClient = orig_llm
    finally:
        # 清理 git worktree 锁之后再删除临时目录
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_parse_and_mutators()
    check_dependency_evaluation_and_cycles()
    check_create_tasks_team_and_refs()
    check_worktree_isolation_land_without_commit()
    print("\nM1 DAG 依赖与 Worktree 隔离不自动提交自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
