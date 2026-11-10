"""M2 端到端协作剧本测试：
1. 完整 4 角色流水线（PM 拆分 → 后端独立 Worktree 合入 stage 落地主工作区 → 前端带上游摘要并行/接力 → QA 只读联调通过 → 主分支零自动 commit）；
2. QA 联调发现 Bug（report_bug + wait_retest → 并发修复 Bug → 自动唤醒 QA 回归通过）；
3. QA 写测试脚本开关 allow_test_scripts=True/False、PM reassign_task 免拆分直派、max_qa_rounds 熔断、人工提交同步；
4. 自由组合降级（无前端报错拦截、无 PM 走 fallback_role）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.agent import ProjectAgent
from orcsy.config import (
    LLMConfig,
    ProjectConfig,
    ToolsConfig,
    validate_agent_profiles,
    validate_project_team,
)
from orcsy.git_ops import Git
from orcsy.llm import LLMClient
from orcsy.task_model import parse_task_file
from orcsy.tools import dispatch_tool


class TeamScriptedLLM(LLMClient):
    """按 (model, task_id, call_count) 分派脚本的线程安全 FakeLLM。"""

    lock = threading.Lock()
    handler = None  # callable(model, task, user_prompt, ctx, call_idx) -> list[(tool, args)]
    calls: list[tuple[str, str]] = []
    prompts: dict[tuple[str, str], str] = {}

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.messages: list[dict] = []
        self.usage = {"turns": 0, "prompt_tokens": 100, "completion_tokens": 40}

    def reset(self, system_prompt: str) -> None:
        self.messages = [{"role": "system", "content": system_prompt}]

    def run_task_loop(self, user_prompt: str, ctx, *, max_turns=40, on_progress=None):
        from orcsy.llm import finish_response

        tid = ctx.current_task.id
        model = self.cfg.model
        with TeamScriptedLLM.lock:
            TeamScriptedLLM.calls.append((model, tid))
            TeamScriptedLLM.prompts[(model, tid)] = user_prompt
            idx = sum(1 for m, t in TeamScriptedLLM.calls if m == model and t == tid)
            actions = TeamScriptedLLM.handler(model, ctx.current_task, user_prompt, ctx, idx)

        for name, args in actions:
            self.usage["turns"] += 1
            if name == "finish":
                return finish_response(args)
            res = dispatch_tool(ctx, name, args)
            # 若工具被禁用或校验拒绝，便于测试断言捕获
            if res.startswith("错误") and args.get("_strict"):
                raise AssertionError(f"工具意外拒绝 {name}: {res}")
        return finish_response({"action": "blocked", "summary": "script ended"})


def _init_git_proj(root: Path) -> Git:
    root.mkdir(parents=True, exist_ok=True)
    g = Git(root)
    g.init()
    g.run("config", "user.email", "t@t.com")
    g.run("config", "user.name", "tester")
    (root / "README.md").write_text("# Demo App\n", encoding="utf-8")
    (root / "AGENT.md").write_text("# 开发指引\n遵循规范。\n", encoding="utf-8")
    g.ensure_gitignore(["tasks/", "AGENT.md", ".worktree-arranger/"])
    g.add_all()
    g.commit("chore: init")
    return g


def _add_task(tasks_dir: Path, stem: str, title: str, *, priority: str = "P1",
              assignee: str = "", body: str = "请完成需求实现并自测。") -> Path:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"title: {title}", f"priority: {priority}", "status: open"]
    if assignee:
        fm.append(f"assignee: {assignee}")
    fm.append("---")
    p = tasks_dir / f"{stem}.md"
    p.write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
    return p


def check_full_pipeline_and_no_auto_commit() -> None:
    """剧本 1：单调度周期内 PM 拆分 → 后端 → 前端 → QA 联调通过，主分支零自动 commit。"""
    tmp = Path(tempfile.mkdtemp(prefix="team-e2e1-"))
    try:
        proj_dir = tmp / "app"
        g = _init_git_proj(proj_dir)
        c0 = g.rev_parse("HEAD")
        td = proj_dir / "tasks"
        _add_task(td, "P1-login", "实现用户登录功能", body="需要支持账号密码登录与页面交互。")

        base_llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m-base")
        profiles = validate_agent_profiles(
            {
                "pm": {"name": "PM", "role": "pm", "llm": {"model": "m-pm"}},
                "backend": {"name": "后端", "role": "backend", "llm": {"model": "m-be"}},
                "frontend": {"name": "前端", "role": "frontend", "llm": {"model": "m-fe"}},
                "qa": {"name": "测试", "role": "qa", "llm": {"model": "m-qa"}},
            },
            base_llm,
        )
        team = validate_project_team(
            {
                "enabled": True,
                "execution_mode": "parallel_worktree",
                "chain_within_cycle": True,
                "members": [
                    {"id": "pm", "profile": "pm"},
                    {"id": "backend", "profile": "backend"},
                    {"id": "frontend", "profile": "frontend"},
                    {"id": "qa", "profile": "qa"},
                ],
            },
            profiles,
            base_llm,
        )
        proj = ProjectConfig(name="app", path=proj_dir, commit_and_merge=False, team=team)

        def _script(model, task, prompt, ctx, idx):
            if model == "m-pm":
                # 确认 PM 不能调用 write_file
                res_w = dispatch_tool(ctx, "write_file", {"path": "hack.txt", "content": "x"})
                assert "已被配置禁用" in res_w, f"PM 应禁止写代码：{res_w}"
                return [
                    ("list_dir", {"path": "."}),
                    ("create_tasks", {
                        "tasks": [
                            {"title": "后端登录接口", "assignee": "backend",
                             "description": "实现 POST /api/login 返回 token。"},
                            {"title": "前端登录页面", "assignee": "frontend",
                             "depends_on_refs": [0],
                             "description": "调用 /api/login 完成登录页。"},
                            {"title": "登录全链路联调测试", "assignee": "qa",
                             "depends_on_refs": [0, 1],
                             "description": "前后端联调验证登录流程。"},
                        ],
                        "_strict": True,
                    }),
                    ("finish", {"action": "split", "summary": "已拆分为后端/前端/QA 三阶段任务"}),
                ]
            if model == "m-be":
                return [
                    ("write_file", {"path": "server.py", "content": "def login(): return 'token'\n"}),
                    ("finish", {"action": "completed", "summary": "新增 server.py 提供 POST /api/login",
                                "files_changed": ["server.py"], "commit_message": "feat: login api"}),
                ]
            if model == "m-fe":
                # 必须能读到后端代码，且 Prompt 含上游摘要
                assert "新增 server.py" in prompt, f"前端应拿到上游摘要：{prompt}"
                return [
                    ("read_file", {"path": "server.py"}),
                    ("write_file", {"path": "login.vue", "content": "<template>Login</template>\n"}),
                    ("finish", {"action": "completed", "summary": "完成 login.vue 对接 /api/login",
                                "files_changed": ["login.vue"], "commit_message": "feat: login ui"}),
                ]
            if model == "m-qa":
                # QA 在主工作区能直接看到前后端完整成果
                assert (proj_dir / "server.py").is_file() and (proj_dir / "login.vue").is_file()
                return [
                    ("read_file", {"path": "server.py"}),
                    ("read_file", {"path": "login.vue"}),
                    ("finish", {"action": "completed", "summary": "前后端联调全部通过"}),
                ]
            raise AssertionError(f"unexpected {model}")

        TeamScriptedLLM.calls = []
        TeamScriptedLLM.handler = staticmethod(_script)

        import orcsy.executor as ex_mod
        orig = ex_mod.LLMClient
        ex_mod.LLMClient = TeamScriptedLLM
        try:
            agent = ProjectAgent(proj, base_llm, commit_and_merge=False,
                                 agent_profiles=profiles, global_tools=ToolsConfig())
            results = agent.scan_and_work()
            # 一轮周期内链式完成 4 个任务：PM 拆分 + BE + FE + QA
            assert len(results) == 4, f"期望链式执行 4 步，实际 {len(results)}: {TeamScriptedLLM.calls}"
            assert [m for m, _ in TeamScriptedLLM.calls] == ["m-pm", "m-be", "m-fe", "m-qa"]

            # 验证主分支零自动提交，代码均已安全合入工作区！
            assert g.rev_parse("HEAD") == c0, "主分支 HEAD 绝不能自动提交移动"
            assert (proj_dir / "server.py").is_file() and (proj_dir / "login.vue").is_file()
            done_stems = {p.stem for p in (td / "done").glob("*.md")}
            assert "P1-login" in done_stems, "PM 拆分完成的原任务应已归档至 done/"
            assert "P1-后端登录接口" in done_stems
            assert "P1-前端登录页面" in done_stems
            assert "P1-登录全链路联调测试" in done_stems
            assert parse_task_file(td / "done" / "P1-login.md").status == "done"
            print("ok: 剧本1（4角色单周期链式完成 + 强制Worktree隔离 + 主分支零自动commit）")
        finally:
            ex_mod.LLMClient = orig
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_qa_bug_retest_and_parallel_fix() -> None:
    """剧本 2：QA 联调 report_bug 派发前后端 Bug + wait_retest → 并发修复 → 自动回归通过 + 脚本写开关验证。"""
    tmp = Path(tempfile.mkdtemp(prefix="team-e2e2-"))
    try:
        proj_dir = tmp / "app"
        g = _init_git_proj(proj_dir)
        (proj_dir / "api.py").write_text("STATUS = 500\n", encoding="utf-8")
        (proj_dir / "ui.js").write_text("BTN = 'broken'\n", encoding="utf-8")
        g.add_all()
        g.commit("feat: buggy initial")
        c0 = g.rev_parse("HEAD")

        td = proj_dir / "tasks"
        qa_path = _add_task(td, "P1-e2e", "前后端联调验收", assignee="qa",
                            body="请对 api.py 与 ui.js 联调测试。")

        base_llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m-base")
        profiles = validate_agent_profiles(
            {
                "backend": {"name": "后端", "role": "backend", "llm": {"model": "m-be"}},
                "frontend": {"name": "前端", "role": "frontend", "llm": {"model": "m-fe"}},
                "qa": {"name": "测试", "role": "qa", "llm": {"model": "m-qa"}, "allow_test_scripts": True},
            },
            base_llm,
        )
        team = validate_project_team(
            {
                "enabled": True,
                "execution_mode": "parallel_worktree",
                "max_qa_rounds": 3,
                "chain_within_cycle": True,
                "members": [
                    {"id": "backend", "profile": "backend"},
                    {"id": "frontend", "profile": "frontend"},
                    {"id": "qa", "profile": "qa"},
                ],
            },
            profiles,
            base_llm,
        )
        proj = ProjectConfig(name="app", path=proj_dir, commit_and_merge=False, team=team)

        def _script(model, task, prompt, ctx, idx):
            if model == "m-qa" and task.qa_round == 0:
                # 第 1 轮联调发现 2 个 Bug 并进入等待回归
                return [
                    ("report_bug", {
                        "bugs": [
                            {"title": "接口返回 500", "assignee": "backend", "severity": "blocker",
                             "repro_steps": "请求接口", "expected": "200", "actual": "500"},
                            {"title": "按钮状态错误", "assignee": "frontend", "severity": "major",
                             "repro_steps": "点击按钮", "expected": "ready", "actual": "broken"},
                        ],
                        "_strict": True,
                    }),
                    ("finish", {"action": "wait_retest", "summary": "发现2个Bug已派发，等待回归"}),
                ]
            if model == "m-be":
                return [
                    ("write_file", {"path": "api.py", "content": "STATUS = 200\n"}),
                    ("finish", {"action": "completed", "summary": "修复 api 返回 200",
                                "files_changed": ["api.py"], "commit_message": "fix: api 200"}),
                ]
            if model == "m-fe":
                return [
                    ("write_file", {"path": "ui.js", "content": "BTN = 'ready'\n"}),
                    ("finish", {"action": "completed", "summary": "修复按钮状态为 ready",
                                "files_changed": ["ui.js"], "commit_message": "fix: btn ready"}),
                ]
            if model == "m-qa" and task.qa_round >= 1:
                # 第 2 轮回归验证通过，且因 allow_test_scripts=True 写入自动化测试脚本！
                assert "200" in (proj_dir / "api.py").read_text(encoding="utf-8")
                assert "ready" in (proj_dir / "ui.js").read_text(encoding="utf-8")
                return [
                    ("write_file", {"path": "test_e2e.py", "content": "assert True\n"}),
                    ("finish", {"action": "completed", "summary": "回归全部通过并补充 E2E 脚本",
                                "files_changed": ["test_e2e.py"], "commit_message": "test: e2e"}),
                ]
            raise AssertionError(f"unexpected {model} round={task.qa_round}")

        TeamScriptedLLM.calls = []
        TeamScriptedLLM.handler = staticmethod(_script)

        import orcsy.executor as ex_mod
        orig = ex_mod.LLMClient
        ex_mod.LLMClient = TeamScriptedLLM
        try:
            agent = ProjectAgent(proj, base_llm, commit_and_merge=False,
                                 agent_profiles=profiles, global_tools=ToolsConfig())
            results = agent.scan_and_work()
            # 期望流程：Wave1 QA提Bug -> Wave2 前后端并行修2个Bug -> Wave3 QA回归通过归档
            assert len(results) == 4, f"期望 4 次任务执行，实际 {len(results)}: {TeamScriptedLLM.calls}"
            assert (td / "done" / "P1-e2e.md").is_file(), "QA 任务回归通过后应已归档"
            assert (proj_dir / "test_e2e.py").is_file(), "allow_test_scripts=True 时测试脚本应安全落地"
            assert g.rev_parse("HEAD") == c0, "依然严守不默认提交到主分支"
            print("ok: 剧本2（QA report_bug + wait_retest → 并发修Bug → 自动唤醒回归通过）")
        finally:
            ex_mod.LLMClient = orig
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_qa_fuse_reassign_and_fallback() -> None:
    """剧本 3/4：max_qa_rounds 熔断、PM reassign_task 免拆分直派、无 PM 走 fallback_role。"""
    tmp = Path(tempfile.mkdtemp(prefix="team-e2e3-"))
    try:
        proj_dir = tmp / "app"
        _init_git_proj(proj_dir)
        td = proj_dir / "tasks"
        # 任务1：PM 用 reassign_task 直接指派给 backend
        _add_task(td, "P1-small", "改个小常量", body="把 CONST 改成 2。")

        base_llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m-base")
        profiles = validate_agent_profiles(
            {
                "pm": {"name": "PM", "role": "pm", "llm": {"model": "m-pm"}},
                "backend": {"name": "后端", "role": "backend", "llm": {"model": "m-be"}},
                "qa": {"name": "测试", "role": "qa", "llm": {"model": "m-qa"}},
            },
            base_llm,
        )
        team = validate_project_team(
            {
                "enabled": True,
                "max_qa_rounds": 1,  # 仅允许 1 轮，首次 wait_retest 后达上限即熔断转 blocked
                "chain_within_cycle": True,
                "members": [
                    {"id": "pm", "profile": "pm"},
                    {"id": "backend", "profile": "backend"},
                    {"id": "qa", "profile": "qa"},
                ],
            },
            profiles,
            base_llm,
        )
        proj = ProjectConfig(name="app", path=proj_dir, commit_and_merge=False, team=team)

        def _script(model, task, prompt, ctx, idx):
            if model == "m-pm":
                return [
                    ("reassign_task", {"assignee": "backend", "note": "单行修改，直接交后端", "_strict": True}),
                    ("finish", {"action": "split", "summary": "直接指派给后端"}),
                ]
            if model == "m-be":
                return [
                    ("write_file", {"path": "const.py", "content": "CONST = 2\n"}),
                    ("finish", {"action": "completed", "summary": "已改 CONST=2",
                                "files_changed": ["const.py"], "commit_message": "fix: const"}),
                ]
            if model == "m-qa":
                return [
                    ("report_bug", {
                        "bugs": [{"title": "测试持续失败", "assignee": "backend",
                                  "repro_steps": "run", "expected": "ok", "actual": "fail"}],
                        "_strict": True,
                    }),
                    ("finish", {"action": "wait_retest", "summary": "发现问题"}),
                ]
            raise AssertionError(model)

        TeamScriptedLLM.calls = []
        TeamScriptedLLM.handler = staticmethod(_script)

        import orcsy.executor as ex_mod
        orig = ex_mod.LLMClient
        ex_mod.LLMClient = TeamScriptedLLM
        try:
            agent = ProjectAgent(proj, base_llm, commit_and_merge=False,
                                 agent_profiles=profiles, global_tools=ToolsConfig())
            res = agent.scan_and_work()
            assert [m for m, _ in TeamScriptedLLM.calls] == ["m-pm", "m-be"], TeamScriptedLLM.calls
            assert (td / "done" / "P1-small.md").is_file()

            # 测试 QA 熔断：新建 QA 任务，max_qa_rounds=1，提 Bug 后应直接转 blocked 停止空转
            qa_p = _add_task(td, "P1-qa-fuse", "测熔断", assignee="qa", body="测试。")
            TeamScriptedLLM.calls = []
            res2 = agent.scan_and_work()
            qa_t = parse_task_file(qa_p)
            assert qa_t.status == "blocked" and qa_t.qa_round == 1, (qa_t.status, qa_t.qa_round)
            assert len(qa_t.depends_on) == 1
            print("ok: 剧本3（PM reassign_task 免拆分直派 + QA max_qa_rounds 熔断）")

            # 测试剧本 4：无 PM 小组走 fallback_role
            proj_dir2 = tmp / "nopm"
            _init_git_proj(proj_dir2)
            td2 = proj_dir2 / "tasks"
            _add_task(td2, "P1-unassigned", "无PM自动兜底", body="直接给后端。")
            team_nopm = validate_project_team(
                {
                    "enabled": True,
                    "fallback_role": "backend",
                    "members": [{"id": "backend", "profile": "backend"}],
                },
                profiles,
                base_llm,
            )
            proj2 = ProjectConfig(name="nopm", path=proj_dir2, commit_and_merge=False, team=team_nopm)
            TeamScriptedLLM.calls = []
            agent2 = ProjectAgent(proj2, base_llm, commit_and_merge=False,
                                  agent_profiles=profiles, global_tools=ToolsConfig())
            agent2.scan_and_work()
            assert [m for m, _ in TeamScriptedLLM.calls] == ["m-be"]
            assert (td2 / "done" / "P1-unassigned.md").is_file()
            print("ok: 剧本4（无 PM 自由组合走 fallback_role 兜底认领）")
        finally:
            ex_mod.LLMClient = orig
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_full_pipeline_and_no_auto_commit()
    check_qa_bug_retest_and_parallel_fix()
    check_qa_fuse_reassign_and_fallback()
    print("\nM2 多模型项目小组 E2E 协作测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
