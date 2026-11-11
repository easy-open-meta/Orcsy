"""Review Agent 端到端自检：合并前审查门 + 返工状态机 + 补审 + 双 merge 策略。

用替身 LLM（不联网）驱动完整链路：执行 Agent 与审查 Agent 都脚本化。
运行：uv run python tests/e2e_review.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.config import LLMConfig, ProjectConfig, ReviewConfig
from orcsy.executor import TaskExecutor
from orcsy.git_ops import Git
from orcsy.task_model import parse_task_file, scan_tasks


class ScriptedLLM:
    """脚本化替身：类属性 script 驱动，实例按调用序消费。

    executor 与 review 各自 new 一个 LLMClient——用 call_index 区分哪个客户端：
    测试里给 executor 与 review 注入同一个类，脚本槽位 [0] 给执行、[1] 给审查。
    """

    scripts: dict[str, list] = {}   # role -> [(name, args), ...]
    roles: dict[int, str] = {}      # id(client) -> role
    tool_log: list[tuple[str, str]] = []  # (role, tool_name)

    def __init__(self, cfg) -> None:
        self.role = ScriptedLLM.roles.get(id(self), "task")

    def reset(self, system_prompt: str) -> None:
        self.system_prompt = system_prompt

    def run_task_loop(self, task_prompt, ctx, max_turns=40, on_progress=None):
        from orcsy.tools import FinishSignal, dispatch_tool

        script = ScriptedLLM.scripts[self.role]
        for name, args in script:
            ScriptedLLM.tool_log.append((self.role, name))
            if name == "finish":
                raise FinishSignal(args)
            out = dispatch_tool(ctx, name, json.dumps(args, ensure_ascii=False))
            print(f"  [{self.role}] {name} -> {out[:100].replace(chr(10), ' / ')}")
            assert not out.startswith("错误"), f"[{self.role}] 工具返回错误：{out}"
        raise AssertionError(f"[{self.role}] 脚本执行完仍未 finish")


def make_git_project(root: Path) -> ProjectConfig:
    proj = root / "review-app"
    proj.mkdir(parents=True)
    (proj / "app.py").write_text(
        'def greet(name):\n'
        '    # TODO: 空名字会崩溃\n'
        '    return "Hello, " + name.upper()\n',
        encoding="utf-8")
    (proj / "README.md").write_text("# review-app\n", encoding="utf-8")
    (proj / "tasks").mkdir()
    (proj / "AGENT.md").write_text("## 项目指引\n\n- 修完跑测试。\n", encoding="utf-8")
    g = Git(proj)
    g.init()
    g.run("add", ".")
    g.run("commit", "-m", "init")
    return ProjectConfig(name="review-app", path=proj, tasks_dir="tasks",
                         guide_file="AGENT.md")


def make_task(proj: ProjectConfig) -> object:
    (proj.path / "tasks" / "P1-fix-greet.md").write_text(
        "---\ntitle: 修复 greet 空名字崩溃\npriority: P1\nstatus: open\n---\n\n"
        "## 现象\n空名字崩溃。\n\n## 验收标准\n- [ ] 空名字返回问候语不崩溃\n",
        encoding="utf-8")
    return parse_task_file(proj.path / "tasks" / "P1-fix-greet.md")


TASK_SCRIPT = [
    ("read_file", {"path": "app.py"}),
    ("edit_file", {
        "path": "app.py",
        "old_string": 'def greet(name):\n    # TODO: 空名字会崩溃\n'
                      '    return "Hello, " + name.upper()',
        "new_string": 'def greet(name):\n'
                      '    if not name:\n'
                      '    return "Hello, stranger"\n'
                      '    return "Hello, " + name.upper()',
    }),
    ("finish", {
        "action": "completed",
        "summary": "修复了 greet 空名字崩溃",
        "files_changed": ["app.py"],
        "commit_message": "fix: greet 空名字崩溃",
        "notes": "",
    }),
]


def check_pass_merges_and_archives() -> None:
    """审查通过：先合并后归档；审查节落档；worktree 清理。"""
    with tempfile.TemporaryDirectory() as tmp:
        proj = make_git_project(Path(tmp))
        task = make_task(proj)
        review_cfg = ReviewConfig(
            llm=LLMConfig(api_key="fake-r", base_url="http://localhost:9",
                          model="reviewer-x"))
        task_llm = LLMConfig(api_key="fake", base_url="http://localhost:9", model="worker-y")
        # worktree_isolation=True：本用例断言 WIP 提交（沙盒分支）与 worktree 清理，
        # 依赖「git 项目 + 隔离」完整链路；未开隔离走 _review_non_git（无 WIP 提交）
        ex = TaskExecutor(proj, task_llm, review_cfg=review_cfg,
                          review_llm=review_cfg.llm, worktree_isolation=True)
        ScriptedLLM.scripts = {
            "task": TASK_SCRIPT,
            "review": [("finish", {"action": "completed", "summary": "实现正确，测试通过",
                                   "files_changed": [], "commit_message": "", "notes": ""})],
        }
        _inject(ex, task_llm, review_cfg.llm)
        result = ex.run_task(task, guide_content="## 项目指引\n\n- 修完跑测试。\n")
        assert result.outcome == "done", f"期望 done：{result.notes} / {result.review}"
        assert result.wip_commit, "应有 WIP 提交"
        # 主分支已合并
        assert "stranger" in (proj.path / "app.py").read_text(encoding="utf-8"), \
            "审查通过后主分支应含修复"
        # agent 层回写 → 归档 + 审查节
        from orcsy.agent import ProjectAgent
        agent = ProjectAgent(proj, task_llm, review_cfg=review_cfg)
        agent._write_back_result(task, result)
        archived = proj.path / "tasks" / "done" / task.path.name
        assert archived.exists(), "通过后应归档"
        text = archived.read_text(encoding="utf-8")
        assert "## 审查结果" in text and "通过" in text, "归档文件应含审查通过节"
        assert "reviewer-x" in text, "审查节应含审查模型名"
        # worktree 已清理
        assert not (proj.path / ".worktree-arranger").exists(), "通过后 worktree 应移除"
        assert result.review is not None and result.review.verdict == "pass"
    print("① 审查通过 → 合并 → 归档 + 审查节 ✅")


def check_fail_rework_then_blocked() -> None:
    """审查不通过：不合并不归档，返工 → 二次不通过转 blocked（worktree 保留）。"""
    with tempfile.TemporaryDirectory() as tmp:
        proj = make_git_project(Path(tmp))
        task = make_task(proj)
        review_cfg = ReviewConfig(
            llm=LLMConfig(api_key="f", base_url="http://localhost:9", model="reviewer-x"))
        task_llm = LLMConfig(api_key="f", base_url="http://localhost:9", model="worker-y")
        ex = TaskExecutor(proj, task_llm, review_cfg=review_cfg, review_llm=review_cfg.llm,
                          worktree_isolation=True)
        ScriptedLLM.scripts = {
            "task": TASK_SCRIPT,
            "review": [("finish", {
                "action": "blocked", "summary": "空名字分支缩进错误",
                "files_changed": [], "commit_message": "",
                "notes": "",
                "issues": [{"file": "app.py", "line": 3, "severity": "blocker",
                            "message": "if 分支缺缩进，空名字仍会崩溃"}]}),
        ]}
        _inject(ex, task_llm, review_cfg.llm)
        result = ex.run_task(task, guide_content="指南")
        assert result.outcome == "blocked", result.outcome
        assert result.review.verdict == "fail"
        # agent 层分派（返工/受阻的状态机在 _write_back_result）
        from orcsy.agent import ProjectAgent
        agent = ProjectAgent(proj, task_llm, review_cfg=review_cfg)
        agent._write_back_result(task, result)
        # 主分支未被污染
        assert "stranger" not in (proj.path / "app.py").read_text(encoding="utf-8"), \
            "审查不通过不得合并主分支"
        # WIP 提交保留在分支 + worktree 保留（返工上下文）
        assert (proj.path / ".worktree-arranger" / "app.py").exists(), \
            "worktree 应保留供返工续作"
        g = Git(proj.path / ".worktree-arranger")
        assert g.run("log", "--oneline", "-1").strip(), "分支应有 WIP 提交"
        # 任务文件：pending 清除、状态 open（返工）、审查意见入正文
        t = parse_task_file(task.path)
        assert t.status == "open" and t.review_round == 1 and not t.review_pending, \
            (t.status, t.review_round, t.review_pending)
        body = task.path.read_text(encoding="utf-8")
        assert "## 审查结果" in body and "blocker" in body and "缩进" in body, \
            "审查意见（含 severity/file:line）应入正文"
        assert "requeue_seq" in body, "返工应带插队戳"

        # 第二轮：审查再不通过（round 达默认上限 2）→ blocked
        # 返工轮从分支尖（WIP 提交内容）继续——脚本先读文件自证续作，再补一笔修复
        ScriptedLLM.scripts = {"task": _REWORK_SCRIPT, "review": [_FAIL_REVIEW]}
        agent_result = ex.run_task(task, guide_content="指南")
        assert agent_result.outcome == "blocked"
        agent._write_back_result(task, agent_result)
        t = parse_task_file(task.path)
        assert t.status == "blocked" and t.review_round == 2, (t.status, t.review_round)
        assert "人工裁决" in task.path.read_text(encoding="utf-8")
        # 返工续作证据：worktree 复位到分支尖（WIP 内容仍在）而非被清空重做
        assert "stranger" in (proj.path / ".worktree-arranger" / "app.py").read_text(
            encoding="utf-8"), "返工轮应从 WIP 提交续作（修复内容保留）"
    print("② 审查不通过 → 返工（意见入正文）→ 二次不通过转 blocked ✅")


def check_fail_project_rounds_override() -> None:
    """项目级 review_max_rounds=1：一次不通过即 blocked（项目覆盖生效）。"""
    with tempfile.TemporaryDirectory() as tmp:
        proj = make_git_project(Path(tmp))
        proj.review_max_rounds = 1
        task = make_task(proj)
        review_cfg = ReviewConfig(
            llm=LLMConfig(api_key="f", base_url="http://localhost:9", model="reviewer-x"))
        task_llm = LLMConfig(api_key="f", base_url="http://localhost:9", model="worker-y")
        ex = TaskExecutor(proj, task_llm, review_cfg=review_cfg, review_llm=review_cfg.llm,
                          worktree_isolation=True)
        ScriptedLLM.scripts = {"task": TASK_SCRIPT, "review": [_FAIL_REVIEW]}
        _inject(ex, task_llm, review_cfg.llm)
        result = ex.run_task(task, guide_content="指南")
        assert result.outcome == "blocked"
        from orcsy.agent import ProjectAgent
        agent = ProjectAgent(proj, task_llm, review_cfg=review_cfg)
        agent._write_back_result(task, result)
        t = parse_task_file(task.path)
        assert t.status == "blocked" and t.review_round == 1, (t.status, t.review_round)
    print("③ 项目级 review_max_rounds=1 覆盖生效 ✅")


def check_error_keeps_pending_for_rescan() -> None:
    """审查 error：不合并、pending 保持；下轮补审只跑审查（不重跑执行）。"""
    with tempfile.TemporaryDirectory() as tmp:
        proj = make_git_project(Path(tmp))
        task = make_task(proj)
        review_cfg = ReviewConfig(
            llm=LLMConfig(api_key="f", base_url="http://localhost:9", model="reviewer-x"))
        task_llm = LLMConfig(api_key="f", base_url="http://localhost:9", model="worker-y")
        ex = TaskExecutor(proj, task_llm, review_cfg=review_cfg, review_llm=review_cfg.llm,
                          worktree_isolation=True)
        from orcsy.agent import ProjectAgent
        agent = ProjectAgent(proj, task_llm, review_cfg=review_cfg)
        # 审查脚本非法（cannot_do）→ verdict=error
        ScriptedLLM.scripts = {
            "task": TASK_SCRIPT,
            "review": [("finish", {"action": "cannot_do", "summary": "看不懂"})],
        }
        _inject(ex, task_llm, review_cfg.llm)
        # 生产链路：scan_and_work = run_task + _write_back_result（agent 分派）
        ex.run_task.__globals__["LLMClient"] = ex.run_task.__globals__["LLMClient"]
        result = ex.run_task(task, guide_content="指南")
        assert result.outcome == "blocked" and result.review.verdict == "error"
        agent._write_back_result(task, result)
        t = parse_task_file(task.path)
        assert t.review_pending is True and t.review_round == 1, \
            (t.review_pending, t.review_round)
        assert "stranger" not in (proj.path / "app.py").read_text(encoding="utf-8"), \
            "审查失败不得合并"
        assert "审查失败" in task.path.read_text(encoding="utf-8")

        # 补审：审查通过（脚本槽换 pass）→ 合并归档；执行脚本不触发（只跑审查）
        ScriptedLLM.scripts = {"task": [], "review": [_PASS_REVIEW]}
        _inject_agent(agent, task_llm, review_cfg.llm)
        results = agent._rescan_pending_reviews()
        assert len(results) == 1 and results[0].outcome == "done", results
        assert (proj.path / "tasks" / "done" / task.path.name).exists(), "补审通过后应归档"
        assert "stranger" in (proj.path / "app.py").read_text(encoding="utf-8"), \
            "补审通过后应合并"
        t2 = parse_task_file(proj.path / "tasks" / "done" / task.path.name)
        assert not t2.review_pending and t2.review_round == 1
    print("④ 审查 error → pending 保持 → 补审只跑审查并合并归档 ✅")


def check_recovery_rerun_goes_to_review() -> None:
    """恢复性重跑：审查 error 后重跑，Agent 无新改动直接 completed →
    必须补走审查门（而非误判「无改动 blocked」或抛 UnboundLocalError）。

    回归背景：修复该场景时曾把恢复路径的提前 return 放在 _finish_merge
    闭包定义之前，触发 UnboundLocalError 让任务误标 blocked——本用例锁定。
    """
    with tempfile.TemporaryDirectory() as tmp:
        proj = make_git_project(Path(tmp))
        task = make_task(proj)
        review_cfg = ReviewConfig(
            llm=LLMConfig(api_key="f", base_url="http://localhost:9", model="reviewer-x"))
        task_llm = LLMConfig(api_key="f", base_url="http://localhost:9", model="worker-y")
        ex = TaskExecutor(proj, task_llm, review_cfg=review_cfg, review_llm=review_cfg.llm,
                          worktree_isolation=True)
        # 第一轮：审查失败（verdict=error）→ pending 保持，分支 WIP 保留
        ScriptedLLM.scripts = {"task": TASK_SCRIPT, "review": [_FAIL_REVIEW]}
        _inject(ex, task_llm, review_cfg.llm)
        r1 = ex.run_task(task, guide_content="指南")
        assert r1.outcome == "blocked" and r1.review.verdict == "fail", \
            (r1.outcome, r1.review.verdict)
        from orcsy.agent import ProjectAgent
        ProjectAgent(proj, task_llm, review_cfg=review_cfg)._write_back_result(task, r1)
        t = parse_task_file(task.path)
        assert t.review_round == 1, t.review_round

        # 第二轮（恢复性重跑）：Agent 不改任何文件，验收后直接 completed；
        # 审查这次通过 → 应补审合入并归档，绝不允许 blocked 或异常。
        # 生产链路 scan_and_work 每轮从磁盘重扫任务（review_round 落盘后重新
        # parse），此处同样重扫以保证 task.review_round 反映真实返工轮数
        ScriptedLLM.scripts = {"task": _RECOVERY_SCRIPT, "review": [_PASS_REVIEW]}
        task2 = parse_task_file(task.path)
        assert task2.review_round == 1, task2.review_round
        r2 = ex.run_task(task2, guide_content="指南")
        assert "UnboundLocalError" not in (r2.notes or ""), r2.notes
        assert r2.outcome == "done", f"恢复性重跑应补审通过归档：{r2.notes} / {r2.review}"
        assert r2.review.verdict == "pass", r2.review
        assert r2.wip_commit, "应保留分支 WIP 提交作为合入来源"
        # agent 回写后归档
        ProjectAgent(proj, task_llm, review_cfg=review_cfg)._write_back_result(task, r2)
        assert (proj.path / "tasks" / "done" / task.path.name).exists(), "应归档"
        # 主工作区含成果
        assert "stranger" in (proj.path / "app.py").read_text(encoding="utf-8"), \
            "审查通过后成果应落地主工作区"
    print("④b 恢复性重跑（无新改动）→ 补走审查门 → pass 合入归档 ✅")


def check_kill_recovery() -> None:
    """模拟审查中被强杀：WIP 提交 + pending 在；recover 不重置；补审接续。"""
    with tempfile.TemporaryDirectory() as tmp:
        proj = make_git_project(Path(tmp))
        task = make_task(proj)
        review_cfg = ReviewConfig(
            llm=LLMConfig(api_key="f", base_url="http://localhost:9", model="reviewer-x"))
        task_llm = LLMConfig(api_key="f", base_url="http://localhost:9", model="worker-y")
        ex = TaskExecutor(proj, task_llm, review_cfg=review_cfg, review_llm=review_cfg.llm,
                          worktree_isolation=True)
        # 审查脚本抛 TaskStopped（模拟强杀在审查阶段）
        class _Stop:
            def __init__(self, cfg):
                pass
            def reset(self, sp):
                pass
            def run_task_loop(self, *a, **k):
                from orcsy.tools import TaskStopped
                raise TaskStopped()
        ScriptedLLM.scripts = {"task": TASK_SCRIPT, "review": []}
        ex.run_task.__globals__["LLMClient"] = _make_patchable(ex, task_llm, review_cfg.llm, review_role_stop=True)
        result = ex.run_task(task, guide_content="指南")
        assert result.outcome == "blocked" and result.review.verdict == "error", \
            (result.outcome, result.review.verdict)
        # 现场保全：分支 WIP + pending 标记 + worktree
        assert (proj.path / ".worktree-arranger" / "app.py").exists()
        from orcsy.agent import ProjectAgent, recover_interrupted_tasks
        agent = ProjectAgent(proj, task_llm, review_cfg=review_cfg)
        agent._write_back_result(task, result)
        t = parse_task_file(task.path)
        assert t.review_pending is True, t.review_pending
        reset = recover_interrupted_tasks(proj)
        assert task.id not in reset, "pending 任务不应被启动恢复重置"
    print("⑤ 审查中强杀 → 现场保全 → recover 不重置 ✅")


def check_skip_and_agent_blocked() -> None:
    """未配置审查（默认态）：行为同旧版；Agent 自报 blocked 不进审查门。"""
    with tempfile.TemporaryDirectory() as tmp:
        proj = make_git_project(Path(tmp))
        task = make_task(proj)
        task_llm = LLMConfig(api_key="f", base_url="http://localhost:9", model="worker-y")
        ex = TaskExecutor(proj, task_llm)  # 不传 review_cfg/review_llm
        ScriptedLLM.scripts = {"task": TASK_SCRIPT, "review": []}
        _inject(ex, task_llm, None)
        result = ex.run_task(task, guide_content="指南")
        assert result.outcome == "done"
        assert result.review is not None and result.review.verdict == "skipped"
        assert "审查未执行" in (result.review.summary or "")
        from orcsy.agent import ProjectAgent
        agent = ProjectAgent(proj, task_llm)
        agent._write_back_result(task, result)
        assert (proj.path / "tasks" / "done" / task.path.name).exists()
        assert "stranger" in (proj.path / "app.py").read_text(encoding="utf-8")

        # Agent 自报 blocked：不进审查门（review 为 None，worktree 清理）
        (proj.path / "tasks" / "P2-fail.md").write_text(
            "---\ntitle: 会失败的任务\npriority: P2\nstatus: open\n---\n\n做不了\n",
            encoding="utf-8")
        task2 = parse_task_file(proj.path / "tasks" / "P2-fail.md")
        ScriptedLLM.scripts = {"task": [("finish", {"action": "blocked", "summary": "受阻"})],
                               "review": []}
        r2 = ex.run_task(task2, guide_content="指南")
        assert r2.outcome == "blocked" and r2.review is None
    print("⑥ 默认态跳过审查照常归档；Agent 自报 blocked 不进审查门 ✅")


def check_squash_merge() -> None:
    """返工后通过：squash_merge=True 单提交收敛；False 走 --no-ff。"""
    def _run(squash: bool) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            proj = make_git_project(Path(tmp))
            proj = ProjectConfig(name=proj.name, path=proj.path, tasks_dir="tasks",
                                 guide_file="AGENT.md")
            task = make_task(proj)
            review_cfg = ReviewConfig(
                llm=LLMConfig(api_key="f", base_url="http://localhost:9",
                              model="reviewer-x"), squash_merge=squash)
            task_llm = LLMConfig(api_key="f", base_url="http://localhost:9", model="worker-y")
            # commit_and_merge=True + worktree_isolation=True：本用例断言主分支
            # HEAD 是 squash/--no-ff 合并形态（_merge_and_push 直合主分支）；
            # 默认（不自动 commit）走 stage 落地工作区，不产生主分支提交
            ex = TaskExecutor(proj, task_llm, review_cfg=review_cfg,
                              review_llm=review_cfg.llm, worktree_isolation=True,
                              commit_and_merge=True)
            # 第一轮 fail（产生返工），第二轮 pass（返工脚本补一笔真实修复）
            # 生产链路含 agent 回写（review_round 落盘）——squash 判定读文件
            ScriptedLLM.scripts = {"task": TASK_SCRIPT, "review": [_FAIL_REVIEW]}
            _inject(ex, task_llm, review_cfg.llm)
            r1 = ex.run_task(task, guide_content="指南")
            from orcsy.agent import ProjectAgent
            ProjectAgent(proj, task_llm, review_cfg=review_cfg)._write_back_result(task, r1)
            assert parse_task_file(task.path).review_round == 1, "第一轮 fail 应落 review_round"
            ScriptedLLM.scripts = {"task": _REWORK_SCRIPT, "review": [_PASS_REVIEW]}
            r2 = ex.run_task(task, guide_content="指南")
            assert r2.outcome == "done", r2.notes
            g = Git(proj.path)
            # squash：主分支尖是普通单提交（无 Merge commit，WIP 提交收敛其中）；
            # no-ff：主分支尖是 Merge commit（WIP 提交留在分支侧）
            head = g.run("log", "-1", "--pretty=%s").strip()
            parents = g.run("log", "-1", "--pretty=%P").strip().split()
            print(f"    squash={squash} HEAD: {head[:50]} | parents={len(parents)}")
            return head, parents

    head_s, parents_s = _run(True)
    head_n, parents_n = _run(False)
    # squash：主分支尖是普通单提交（无 Merge commit；WIP 提交收敛其中）
    assert len(parents_s) == 1 and "Merge" not in head_s, (head_s, parents_s)
    # no-ff：主分支尖是 Merge commit（WIP 提交留在分支侧）
    assert len(parents_n) == 2 and "Merge" in head_n, (head_n, parents_n)
    print("⑦ 返工通过后 merge --squash 收敛（默认开）/ --no-ff 保留 ✅")


def check_review_tools_gate() -> None:
    """审查工具面：allow_test_commands 开/关决定 run_command 是否下发。"""
    from orcsy.prompts import build_review_system_prompt
    with tempfile.TemporaryDirectory() as tmp:
        proj = make_git_project(Path(tmp))
        task = make_task(proj)
        review_cfg = ReviewConfig(
            llm=LLMConfig(api_key="f", base_url="http://localhost:9", model="reviewer-x"))
        task_llm = LLMConfig(api_key="f", base_url="http://localhost:9", model="worker-y")

        # 开启：审查脚本里 run_command 可用
        ex = TaskExecutor(proj, task_llm, review_cfg=review_cfg, review_llm=review_cfg.llm)
        ScriptedLLM.scripts = {
            "task": TASK_SCRIPT,
            "review": [("read_file", {"path": "app.py"}),
                       ("finish", {"action": "completed", "summary": "ok"})],
        }
        _inject(ex, task_llm, review_cfg.llm)
        result = ex.run_task(task, guide_content="指南")
        assert result.outcome == "done"
        assert ("review", "read_file") in ScriptedLLM.tool_log, "审查应能读文件"
        assert ("review", "write_file") not in ScriptedLLM.tool_log

        # 关闭：run_command 不在启用集——dispatch 直接拒绝
        from orcsy.tools import ToolContext, dispatch_tool
        ctx = ToolContext(root=proj.path, enabled_tools=frozenset(
            {"list_dir", "read_file", "finish"}))
        out = dispatch_tool(ctx, "run_command", json.dumps({"command": ["git", "status"]}))
        assert out.startswith("错误"), f"禁用 run_command 应被拒：{out[:60]}"
        sp = build_review_system_prompt(proj, proj.path, "指南", allow_test_commands=False)
        assert "run_command" not in sp.split("# 工作环境")[1].split("#")[0] or \
            "不可改动任何文件" not in sp, "关闭时提示词不应宣传测试命令"
    print("⑧ 审查工具面：allow_test_commands 开关与只读约束 ✅")


def check_issues_parsing() -> None:
    """finish.issues 结构化解析与非法回退。"""
    from orcsy.llm import finish_response
    r = finish_response({
        "action": "blocked", "summary": "有问题",
        "issues": [
            {"file": "a.py", "line": 3, "severity": "blocker", "message": "崩"},
            {"file": "b.py", "severity": "major", "message": "缺测试"},
            {"file": "", "severity": "major", "message": "无文件跳过"},
            {"file": "c.py", "severity": "oops", "message": "非法 severity 跳过"},
            "不是对象跳过",
        ]})
    assert len(r.issues) == 2, r.issues
    assert r.issues[0]["line"] == 3 and r.issues[1].get("line") is None
    r2 = finish_response({"action": "blocked", "summary": "s",
                          "issues": "不是数组", "notes": "纯文本问题说明"})
    assert r2.issues == [] and True
    r3 = finish_response({"action": "completed", "summary": "ok"})
    assert r3.issues == []
    print("⑨ finish.issues 结构化解析与兜底 ✅")


_FAIL_REVIEW = ("finish", {
    "action": "blocked", "summary": "实现有问题",
    "files_changed": [], "commit_message": "", "notes": "",
    "issues": [{"file": "app.py", "severity": "major", "message": "缺少边界检查"}]})
_PASS_REVIEW = ("finish", {"action": "completed", "summary": "实现正确",
                           "files_changed": [], "commit_message": "", "notes": ""})

# 返工轮脚本：从分支尖（WIP 内容）续作——读文件自证 + 修缩进 + 重新提交
_REWORK_OLD = ('def greet(name):\n'
               '    if not name:\n'
               '    return "Hello, stranger"\n'
               '    return "Hello, " + name.upper()')
_REWORK_NEW = ('def greet(name):\n'
               '    if not name:\n'
               '        return "Hello, stranger"\n'
               '    return "Hello, " + name.upper()')
_REWORK_SCRIPT = [
    ("read_file", {"path": "app.py"}),
    ("edit_file", {"path": "app.py", "old_string": _REWORK_OLD, "new_string": _REWORK_NEW}),
    ("finish", {"action": "completed", "summary": "按审查意见修复了缩进",
                "files_changed": ["app.py"], "commit_message": "fix: review issues",
                "notes": ""}),
]

# 恢复性重跑脚本：成果已在分支上，验收通过后直接 completed（无新改动、无 files_changed）
_RECOVERY_SCRIPT = [
    ("read_file", {"path": "app.py"}),
    ("finish", {"action": "completed",
                "summary": "成果已完整提交在分支，恢复性重跑验收全部通过",
                "files_changed": [], "commit_message": "", "notes": ""}),
]


def _inject(executor: TaskExecutor, task_llm, review_llm) -> None:
    """把替身注入 executor 的任务循环与审查循环。

    任务循环在 executor.run_task 的全局命名空间取 LLMClient；审查循环在
    orcsy.review 的模块命名空间取——两处都要替换。
    """
    client_cls = _patched_client_factory(task_llm, review_llm)
    executor.run_task.__globals__["LLMClient"] = client_cls
    import orcsy.review as review_mod
    review_mod.LLMClient = client_cls


def _inject_agent(agent, task_llm, review_llm) -> None:
    _inject(agent.executor, task_llm, review_llm)


def _make_patchable(executor, task_llm, review_llm, review_role_stop: bool = False):
    return _patched_client_factory(task_llm, review_llm,
                                   review_role_stop=review_role_stop)


def _patched_client_factory(task_llm, review_llm, review_role_stop: bool = False):
    """按 cfg.model 区分角色：任务模型 → role=task；审查模型 → role=review。"""

    class _Client(ScriptedLLM):
        def __init__(self, cfg) -> None:
            super().__init__(cfg)
            self.role = ("review" if review_llm is not None
                         and cfg.model == review_llm.model else "task")
            self.usage = {"turns": 1, "prompt_tokens": 10, "completion_tokens": 5}
            if review_role_stop and self.role == "review":
                # 强杀模拟：审查客户端一跑就抛 TaskStopped
                self.run_task_loop = self._stop

        def _stop(self, *a, **k):
            from orcsy.tools import TaskStopped
            raise TaskStopped()

    return _Client


def main() -> int:
    check_pass_merges_and_archives()
    check_fail_rework_then_blocked()
    check_fail_project_rounds_override()
    check_error_keeps_pending_for_rescan()
    check_recovery_rerun_goes_to_review()
    check_kill_recovery()
    check_skip_and_agent_blocked()
    check_squash_merge()
    check_review_tools_gate()
    check_issues_parsing()
    print("\nreview 端到端全部自检通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
