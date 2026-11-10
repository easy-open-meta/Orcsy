"""任务自主拆分自检：create_tasks 工具（建子任务/关闭原任务/去重/校验）+
finish(action=split) 收尾链路（真实循环：拆分 → split → 原任务 cancelled、
子任务接力）+ edit_file 失败时的「最接近片段」自纠提示。

背景：真实跑批中大任务（预计 2 天工作量）在 20 轮里只啃出骨架还产生模型
自我混淆——与其 blocked 等人工拆，不如让 Agent 早期自主拆分接力。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import LLMConfig, ProjectConfig
from orcsy.executor import TaskExecutor
from orcsy.task_model import parse_task_file
from orcsy.tools import ToolContext, dispatch_tool


def make_task_file(root: Path, stem: str = "P1-big", order: int | None = None) -> object:
    d = root / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{stem}.md"
    lines = ["---", "title: 大任务", "priority: P1", "status: open"]
    if order is not None:
        lines.insert(3, f"order: {order}")
    lines.append("---")
    f.write_text("\n".join(lines) + "\n\n把整个系统写出来。\n", encoding="utf-8")
    return parse_task_file(f)


def tool_ctx(root: Path, task) -> ToolContext:
    return ToolContext(root=root, current_task=task)


def check_create_tasks() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="split-"))
    try:
        task = make_task_file(tmp)
        ctx = tool_ctx(tmp, task)
        r = dispatch_tool(ctx, "create_tasks", {"tasks": [
            {"title": "1 model 层补全",
             "description": "补 images/settings 访问。\n验收：go build ./internal/model",
             "priority": "P1"},
            {"title": "2 auth+setting 包",
             "description": "JWT/中间件/限速 + KV 缓存。\n验收：go test ./internal/..."},
            {"title": "1 model 层补全"},  # 与第一条重名 → 自动 -2
        ]})
        assert "已创建 3 个子任务" in r, r
        d = tmp / "tasks"
        stems = sorted(p.stem for p in d.glob("*.md"))
        assert "P1-1-model-层补全" in stems and "P1-2-authsetting-包" in stems, stems
        assert "P1-1-model-层补全-2" in stems, stems  # 重名避让
        # frontmatter：继承优先级（未给 priority 的第二条）+ order 递增接尾
        t2 = parse_task_file(d / "P1-2-authsetting-包.md")
        assert t2.priority == "P1" and t2.status == "open", (t2.priority, t2.status)
        assert t2.order is not None, "拆分子任务应有手动序"
        t1 = parse_task_file(d / "P1-1-model-层补全.md")
        assert t1.order == t2.order - 1, (t1.order, t2.order)
        # close_current 默认 true：原任务已完成拆分置为 done（磁盘为准）+ 进度留痕
        assert parse_task_file(task.path).status == "done", task.status
        text = task.path.read_text(encoding="utf-8")
        assert "已成功拆分为 3 个子任务" in text
        assert "finish(action=\"split\"" in r
        print("ok: create_tasks（建文件/重名避让/优先级继承/order 接尾/原任务完成留痕）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_validations() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="split-v-"))
    try:
        task = make_task_file(tmp)
        ctx = tool_ctx(tmp, task)
        bad_cases = [
            ({"tasks": []}, "1~10"),
            ({"tasks": [{"title": ""}]}, "title 不能为空"),
            ({"tasks": [{"title": "x", "priority": "P9"}]}, "优先级不合法"),
            ({"tasks": "notalist"}, "数组"),
            ({"tasks": [{"title": "a"}] * 11}, "1~10"),
        ]
        for args, why in bad_cases:
            r = dispatch_tool(ctx, "create_tasks", args)
            assert r.startswith("错误") and why in r, (why, r)
        # close_current=false：原任务状态不动
        r = dispatch_tool(ctx, "create_tasks",
                          {"tasks": [{"title": "solo"}], "close_current": False})
        assert "保持原状态" in r, r
        assert parse_task_file(task.path).status == "open"
        # current_task 未注入 → 拒绝
        r = dispatch_tool(ToolContext(root=tmp), "create_tasks",
                          {"tasks": [{"title": "x"}]})
        assert "无法确定当前任务" in r, r
        print("ok: create_tasks 校验（数量/空标题/非法优先级/不关闭/未注入任务）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_split_end_to_end() -> None:
    """真实循环集成：轮 1 create_tasks（真执行），轮 2 finish(split)。"""
    tmp = Path(tempfile.mkdtemp(prefix="split-e2e-"))
    try:
        proj_dir = tmp / "proj"
        proj_dir.mkdir()
        (proj_dir / "AGENT.md").write_text("# 指引\n", encoding="utf-8")
        from orcsy.task_model import parse_task_file as _p
        tf = proj_dir / "tasks"
        tf.mkdir()
        f = tf / "P1-按设计文档完成整个后端.md"
        f.write_text("---\ntitle: 按设计文档完成整个后端\npriority: P1\nstatus: open\n"
                     "---\n\n照 docs/design.md 全量实现。\n", encoding="utf-8")
        task = _p(f)

        from orcsy.llm import LLMClient
        orig = LLMClient._post
        state = {"calls": 0}

        def fake_post(self, url, payload, headers):
            state["calls"] += 1
            if state["calls"] == 1:
                return {"choices": [{"message": {"role": "assistant", "content": None,
                                                 "tool_calls": [{
                                                     "id": "c1", "type": "function",
                                                     "function": {
                                                         "name": "create_tasks",
                                                         "arguments": json.dumps({
                                                             "tasks": [
                                                                 {"title": "1 model 层",
                                                                  "description": "验收：go build ./internal/model"},
                                                                 {"title": "2 auth+setting",
                                                                  "description": "验收：go test ./internal/..."}]})}}],
                                                 "finish_reason": "tool_calls"}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
            return {"choices": [{"message": {"role": "assistant", "content": None,
                                             "tool_calls": [{
                                                 "id": "c2", "type": "function",
                                                 "function": {
                                                     "name": "finish",
                                                     "arguments": json.dumps({
                                                         "action": "split",
                                                         "summary": "任务过大，已拆分为 2 个子任务接力",
                                                         "notes": "见新建任务文件"})}}],
                                             "finish_reason": "tool_calls"}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 30}}
        LLMClient._post = fake_post
        try:
            # 走 ProjectAgent.scan_and_work：任务状态/结果节写回在这层完成
            from orcsy.agent import ProjectAgent
            proj = ProjectConfig(name="p", path=proj_dir)
            agent = ProjectAgent(proj, LLMConfig(api_key="k",
                                                 base_url="http://localhost:9",
                                                 model="m"))
            results = agent.scan_and_work()
            result = results[0] if results else None
        finally:
            LLMClient._post = orig

        assert result is not None and result.outcome == "done", \
            (result and result.outcome, result and result.notes)
        assert "拆分" in result.summary, result.summary
        # 原任务：done 且自动归档进 tasks/done/，结果节含 summary
        assert not f.exists(), "原任务应已从 tasks/ 根目录移走归档"
        done_f = tf / "done" / f.name
        assert done_f.exists(), "原任务应已归档进 tasks/done/"
        text = done_f.read_text(encoding="utf-8")
        assert "status: done" in text
        assert "## Agent 处理结果" in text and "已拆分为 2 个子任务" in text
        assert "✂ 已成功拆分为 2 个子任务" in text
        # 子任务文件真实存在、open、order 排好
        kids = sorted(p.stem for p in tf.glob("*.md") if p.stem != f.stem)
        assert kids == ["P1-1-model-层", "P1-2-authsetting"], kids
        k1 = parse_task_file(tf / "P1-1-model-层.md")
        assert k1.status == "open" and k1.order is not None
        # 子任务描述里带着验收命令（后续任务可执行）
        assert "go build ./internal/model" in (tf / "P1-1-model-层.md").read_text(
            encoding="utf-8")
        print("ok: 真实循环拆分接力（create_tasks → finish(split) → 原任务 done 归档、子任务 open）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_edit_hint() -> None:
    """edit_file old_string 不存在 → 附最接近片段（tx vs db 记忆漂移场景）。"""
    tmp = Path(tempfile.mkdtemp(prefix="split-hint-"))
    try:
        f = tmp / "users.go"
        f.write_text(
            "package model\n\n"
            "func CreateUserTx(ctx context.Context, tx *sql.Tx, u string) (*User, error) {\n"
            "\tres, err := tx.ExecContext(ctx,\n"
            "\t\t`INSERT INTO users (username) VALUES (?)`, u)\n"
            "\tif err != nil {\n\t\treturn nil, err\n\t}\n"
            "\treturn &User{Username: u}, nil\n}\n", encoding="utf-8")
        ctx = ToolContext(root=tmp)
        # 模型记忆漂移：把 tx 记成 db
        r = dispatch_tool(ctx, "edit_file", {
            "path": "users.go",
            "old_string": "func CreateUser(ctx context.Context, db *sql.DB, u string) (*User, error) {\n\tres, err := db.ExecContext(ctx,",
            "new_string": "func CreateUser(ctx context.Context, db *sql.DB, u string) (*User, error) {\n\tres, err := db.ExecContext(ctx, // fixed"})
        assert "old_string 在文件中不存在" in r, r
        assert "最接近的片段" in r and "tx.ExecContext" in r, r
        assert "相似度" in r and "行" in r, r
        # 文件未被改动
        assert "db.ExecContext" not in f.read_text(encoding="utf-8")
        # multi_edit 同款提示
        r2 = dispatch_tool(ctx, "multi_edit", {"path": "users.go", "edits": [
            {"old_string": "完全不存在的内容 xyzzy", "new_string": "n"}]})
        assert "第 1 条 edit" in r2 and "old_string 在文件中不存在" in r2, r2
        # 完全不相关的 old_string → 无片段也不崩（宁缺毋滥）
        r3 = dispatch_tool(ctx, "edit_file", {
            "path": "users.go", "old_string": "%%完全无关%%\n不存在的行", "new_string": "n"})
        assert "old_string 在文件中不存在" in r3, r3
        # 正常编辑不受影响
        r4 = dispatch_tool(ctx, "edit_file", {
            "path": "users.go",
            "old_string": "tx.ExecContext(ctx,", "new_string": "tx.ExecContext(ctx, // ok"})
        assert r4.startswith("已修改"), r4
        print("ok: edit 失败自纠提示（最接近片段/相似度/文件未动/正常路径不受影响）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_prompts() -> None:
    from orcsy.prompts import build_system_prompt
    proj = ProjectConfig(name="p", path=Path("."))
    prompt = build_system_prompt(proj, "# 指引", False)
    assert "create_tasks" in prompt and "split" in prompt, prompt[:200]
    assert "主动拆分" in prompt, prompt[-600:]
    print("ok: 系统提示词含拆分引导（工具索引/split 动作/时机判断）")


def main() -> int:
    check_create_tasks()
    check_validations()
    check_split_end_to_end()
    check_edit_hint()
    check_prompts()
    print("\n任务自主拆分自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
