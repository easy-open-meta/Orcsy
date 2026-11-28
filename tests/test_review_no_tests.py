"""review_no_tests：任务级关闭审查测试 + 审查提示词约束回归。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy import claims
from orcsy.config import ProjectConfig, ReviewConfig
from orcsy.executor import TaskExecutor
from orcsy.prompts import build_review_prompt, build_review_system_prompt
from orcsy.task_model import parse_task_file


def make_task(no_tests: bool = True):
    td = Path(tempfile.mkdtemp(prefix="review-no-tests-"))
    path = td / "P1-doc.md"
    flag = "review_no_tests: true\n" if no_tests else ""
    path.write_text(
        "---\n"
        "title: 文档改动\n"
        "priority: P1\n"
        "status: open\n"
        f"{flag}"
        "---\n\n"
        "只改文档，不需要运行测试。\n",
        encoding="utf-8")
    return parse_task_file(path)


def main() -> int:
    task = make_task(True)
    assert task.review_no_tests is True
    assert make_task(False).review_no_tests is False

    project = ProjectConfig(
        name="p", path=Path(tempfile.mkdtemp(prefix="rp-")),
        guide_file="AGENT.md")
    sys_prompt = build_review_system_prompt(
        project, Path("."), "", allow_test_commands=False)
    assert "测试不是必须项" in sys_prompt

    prompt_no_cmd = build_review_prompt(
        task, "done", "", "", allow_test_commands=False)
    assert "没有命令执行权限" in prompt_no_cmd
    assert "review_no_tests" in prompt_no_cmd

    prompt_cmd = build_review_prompt(
        task, "done", "", "", allow_test_commands=True)
    assert "退出码 5" in prompt_cmd

    # 任务声明 review_no_tests 后，即使全局允许也不给 run_command
    ex = TaskExecutor.__new__(TaskExecutor)
    ex.review_cfg = ReviewConfig(enabled=True, allow_test_commands=True)
    assert ex._review_allow_test_commands(task) is False
    task.review_no_tests = False
    assert ex._review_allow_test_commands(task) is True
    ex.review_cfg = ReviewConfig(enabled=True, allow_test_commands=False)
    assert ex._review_allow_test_commands(task) is False

    # 该字段属于系统键，不参与任务指纹
    assert "review_no_tests" in claims._SYSTEM_KEYS

    # 旧任务文件无该字段时天然兼容
    legacy = make_task(False)
    assert legacy.review_no_tests is False

    print("review_no_tests 任务级开关与提示词约束 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
