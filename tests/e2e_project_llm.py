"""端到端：项目专属模型绑定隔离生效（不联网）。

证明两件事：
1. 调度器为绑定项目构造的 Agent 只拿到绑定模型（未绑定项目仍用全局模型）；
2. 绑定项目真实跑一个任务时，LLMClient 收到的配置 model = 绑定值、
   api_key = 继承的全局值（部分覆盖语义在执行链路上成立）。

运行：uv run python tests/e2e_project_llm.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.config import (AppConfig, LLMConfig, LoggingConfig, ProjectConfig,
                          SchedulerConfig, WebConfig)
from orcsy.git_ops import Git
from orcsy.scheduler import Scheduler
from orcsy.web import AppState
import os
os.environ.setdefault('NO_PROXY', '127.0.0.1,localhost')  # 本机回环不走系统代理（环境可能带 http_proxy）

GLOBAL_MODEL = "global-model"
BOUND_MODEL = "bound-model"


def make_git_project(root: Path, name: str) -> ProjectConfig:
    proj = root / name
    proj.mkdir(parents=True)
    (proj / "README.md").write_text("# demo\n", encoding="utf-8")
    g = Git(proj)
    g.run("init", "-q")
    g.run("config", "user.email", "t@t")
    g.run("config", "user.name", "t")
    g.run("add", "-A")
    g.run("commit", "-qm", "init")
    return ProjectConfig(name=name, path=proj)


def make_task(proj: Path) -> None:
    (proj / "tasks").mkdir(exist_ok=True)
    (proj / "tasks" / "P1-demo.md").write_text(
        "---\ntitle: 演示任务\npriority: P1\nstatus: open\n---\n\n无需真实改动。\n",
        encoding="utf-8",
    )
    g = Git(proj)
    g.run("add", "-A")
    g.run("commit", "-qm", "chore: task")


class SpyLLMClient:
    """LLMClient 替身：记录收到的配置关键值，并按脚本直接 finish。"""

    seen: list[dict] = []

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        SpyLLMClient.seen.append({"model": cfg.model, "api_key": cfg.api_key,
                                  "base_url": cfg.base_url})

    def reset(self, system_prompt: str) -> None:
        pass

    def run_task_loop(self, task_prompt, ctx, max_turns=40, on_progress=None):
        import json as _json

        from orcsy.tools import FinishSignal, dispatch_tool

        # 执行器要求 completed 必须有真实文件改动：先写一个文件再收尾
        out = dispatch_tool(ctx, "write_file", _json.dumps(
            {"path": "bound-note.txt", "content": "written by bound model\n"}))
        assert not out.startswith("错误"), f"工具返回错误：{out}"
        raise FinishSignal({
            "action": "completed",
            "summary": "演示：绑定模型处理了本任务",
            "files_changed": ["bound-note.txt"],
            "commit_message": "chore: done by bound model",
            "notes": "",
        })


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-llm-e2e-"))
    try:
        bound = make_git_project(tmp, "proj-bound")
        make_task(bound.path)
        plain = make_git_project(tmp, "proj-plain")
        make_task(plain.path)
        bound.llm = {"model": BOUND_MODEL}  # 只换模型名：api_key/base_url 继承全局

        cfg = AppConfig(
            llm=LLMConfig(api_key="sk-global", base_url="http://localhost:9/v1",
                          model=GLOBAL_MODEL),
            scheduler=SchedulerConfig(run_once=True),
            logging=LoggingConfig(), web=WebConfig(),
            projects=[bound, plain],
            source_path=tmp / "config.yaml",
        )

        # ---- 断言 1：调度器为各项目构造的 Agent 拿到正确的解析结果 ----
        sched = Scheduler(cfg)
        by_name = {a.project.name: a for a in sched.agents}
        assert by_name["proj-bound"].executor.llm.model == BOUND_MODEL, \
            "绑定项目应使用专属模型"
        assert by_name["proj-bound"].executor.llm.api_key == "sk-global", \
            "未覆盖字段应继承全局"
        assert by_name["proj-plain"].executor.llm is cfg.llm, \
            "未绑定项目应原样使用全局配置对象"

        # ---- 断言 2：真实执行链路上 LLMClient 收到绑定模型（Web 侧同源构造） ----
        state = AppState(cfg, sched)
        assert state.agents["proj-bound"].executor.llm.model == BOUND_MODEL

        from orcsy.task_model import parse_task_file
        task = parse_task_file(bound.path / "tasks" / "P1-demo.md")
        executor = state.agents["proj-bound"].executor
        executor.run_task.__globals__["LLMClient"] = SpyLLMClient
        result = executor.run_task(task, guide_content="## 指引\n")
        assert result.outcome == "done", f"任务应完成：{result.notes}"
        assert SpyLLMClient.seen == [
            {"model": BOUND_MODEL, "api_key": "sk-global",
             "base_url": "http://localhost:9/v1"}
        ], f"LLMClient 应收到 绑定模型+继承密钥：{SpyLLMClient.seen}"

        print("绑定项目执行链路使用专属模型、未绑定项目用全局模型 ✅")
        print("\n全部断言通过 ✅")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
