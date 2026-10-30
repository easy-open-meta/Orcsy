"""项目 Agent：负责一个项目的完整任务循环。

每轮 scan()：
1. 读取项目的 AGENT.md 指引（缺失时使用默认指引并告警）；
2. 扫描 tasks/ 目录，解析所有任务，按优先级排序；
3. 依次领取前 N 个 open 任务（尊重 assignee），交给 TaskExecutor 执行；
4. 依据执行结果回写任务文件状态（status / result 节），done 的归档到 done/。
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from .config import ProjectConfig
from .executor import TaskExecutor, TaskResult
from .logging_setup import get_logger
from .task_model import Task, _parse_frontmatter, append_progress, scan_tasks, set_task_file_status
from .templates import default_guide

# 回写到任务文件的结果节
_RESULT_HEADING = "## Agent 处理结果"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def recover_interrupted_tasks(project: ProjectConfig) -> list[str]:
    """进程启动时恢复上次意外退出遗留的 in_progress 任务。

    强杀/断电等场景下任务文件停在 in_progress，而扫描只认 open——不处理
    就永远卡死。这里把遗留任务重置为 open 重新入队，并在「执行进度」节
    写入中断说明（最后活动时间取自文件 mtime，即最后一次进度落盘时刻）。
    返回被重置的任务 ID 列表。只在进程启动时调用一次（配置热重载不触发，
    避免误伤正在执行中的任务）。
    """
    tasks_dir = project.path / project.tasks_dir
    if not tasks_dir.is_dir():
        return []
    reset: list[str] = []
    for path in sorted(tasks_dir.glob("*.md")):
        try:
            meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if str(meta.get("status") or "").strip().lower() != "in_progress":
            continue
        try:
            last = _dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            last = "未知时间"
        append_progress(
            path,
            f"**进程意外退出，任务被中断**（最后活动 {last}）。"
            "已重置为待办重新执行；git 项目 worktree 里未提交的改动会在下次执行时重置丢弃。")
        if set_task_file_status(path, "open"):
            reset.append(path.stem)
    return reset


class ProjectAgent:
    """一个项目一个实例；scan() 由调度器周期性调用。"""

    def __init__(self, project: ProjectConfig, llm) -> None:
        self.project = project
        self.executor = TaskExecutor(project, llm)
        self.log = get_logger(f"agent.{project.name}")

    # ---- 对外 ----

    @property
    def tasks_dir(self) -> Path:
        return self.project.path / self.project.tasks_dir

    def scan_and_work(self) -> list[TaskResult]:
        """执行一轮扫描与任务处理，返回本轮的任务结果列表。"""
        log = self.log
        if not self.project.path.is_dir():
            log.error("项目目录不存在：%s，跳过", self.project.path)
            return []

        guide = self._load_guide()
        tasks, errors = scan_tasks(self.tasks_dir)

        if errors:
            for t in errors:
                log.warning("任务文件解析失败 %s：%s", t.id, t.error)
            self._write_back_errors(errors)

        if not tasks:
            log.info("没有可执行的 open 任务")
            return []

        log.info("发现 %d 个 open 任务：%s", len(tasks),
                 ", ".join(f"{t.id}(P{t.rank})" for t in tasks))

        results: list[TaskResult] = []
        budget = max(1, self.project.max_tasks_per_cycle)
        claimed = 0
        for task in tasks:
            if claimed >= budget:
                remaining = len(tasks) - claimed
                log.info("本轮任务额度（%d）已用完，剩余 %d 个任务下轮处理", budget, remaining)
                break
            # assignee 非空时只有匹配的 Agent 能领取（支持一个项目里按角色分派）
            if task.assignee and task.assignee != self.project.name:
                log.debug("任务 %s 指派给 %s，跳过", task.id, task.assignee)
                continue

            self._mark_in_progress(task)
            result = self.executor.run_task(task, guide)
            self._write_back_result(task, result)
            results.append(result)
            claimed += 1

        return results

    # ---- 内部 ----

    def _load_guide(self) -> str:
        guide_path = self.project.path / self.project.guide_file
        try:
            content = guide_path.read_text(encoding="utf-8")
            if content.strip():
                return content
            self.log.warning("指引文件为空：%s，使用内置模板生成", guide_path)
        except OSError:
            self.log.warning("未找到指引文件 %s，使用内置模板生成", guide_path)
        # 与看板新建项目落盘的初始内容同源（templates.default_guide），不留第二份文案
        return default_guide(self.project.name, self.project.path)

    def _set_status(self, path: Path, status: str, result_section: str | None = None) -> None:
        """改写任务文件 frontmatter 的 status，可选追加结果节。"""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            self.log.error("回写任务状态失败（无法读取 %s）：%s", path, e)
            return

        m = _FRONTMATTER_RE.match(text)
        if not m:
            # 没有 frontmatter（不太可能，解析阶段已过滤），直接加结果节
            text = f"---\nstatus: {status}\n---\n\n{text}"
        else:
            front, rest = m.group(1), text[m.end():]
            front_lines = [
                re.sub(r"^status:\s*\S+.*$", f"status: {status}", line, flags=re.MULTILINE)
                for line in [front]
            ][0]
            if f"status:" not in front_lines:
                front_lines = front_lines.rstrip() + f"\nstatus: {status}"
            text = f"---\n{front_lines}\n---\n{rest}"

        if result_section:
            # 结果节固定插在正文最前，旧的先删掉
            body = re.sub(
                rf"{re.escape(_RESULT_HEADING)}.*?(?=\n## |\Z)", "", text, flags=re.DOTALL
            )
            marker = f"\n{_RESULT_HEADING}\n\n{result_section.strip()}\n"
            # 插到 frontmatter 之后
            m2 = _FRONTMATTER_RE.match(body)
            if m2:
                body = body[:m2.end()] + marker.lstrip("\n") + "\n" + body[m2.end():]
            else:
                body = marker + body
            text = body

        try:
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            self.log.error("回写任务状态失败（无法写入 %s）：%s", path, e)

    def _mark_in_progress(self, task: Task) -> None:
        self._set_status(task.path, "in_progress")

    def _write_back_result(self, task: Task, result: TaskResult) -> None:
        lines = [f"- 时间：{_now_str()}",
                 f"- 结果：{result.outcome}"]
        if result.commit:
            lines.append(f"- 提交：{result.commit}{'（已推送）' if result.pushed else ''}")
        if result.turns:
            lines.append(f"- 轮数：{result.turns}（输入 {result.prompt_tokens} / "
                         f"输出 {result.completion_tokens} tokens）")
        if result.summary:
            lines.append(f"- 摘要：{result.summary}")
        if result.files_changed:
            lines.append(f"- 变更文件：{', '.join(result.files_changed)}")
        if result.notes:
            lines.append(f"- 备注：{result.notes.strip()}")
        if result.llm_error:
            lines.append(f"- 异常：{result.llm_error}")

        self._set_status(task.path, result.outcome, result_section="\n".join(lines))

        # done 任务归档到 tasks/done/，保持清单整洁
        if result.outcome == "done":
            done_dir = self.tasks_dir / "done"
            done_dir.mkdir(parents=True, exist_ok=True)
            target = done_dir / task.path.name
            try:
                task.path.replace(target)
                self.log.info("任务 %s 已归档到 %s", task.id, target)
            except OSError as e:
                self.log.warning("归档任务 %s 失败：%s", task.id, e)

    def _write_back_errors(self, errors: list[Task]) -> None:
        """解析失败的任务：把失败原因写回文件头部的 HTML 注释，等人工修复。"""
        for task in errors:
            try:
                text = task.path.read_text(encoding="utf-8")
            except OSError:
                continue
            comment = f"<!-- orcsy: 任务文件解析失败：{task.error} -->\n"
            if "orcsy: 任务文件解析失败" not in text and "task-arranger: 任务文件解析失败" not in text:
                try:
                    task.path.write_text(comment + text, encoding="utf-8")
                except OSError as e:
                    self.log.error("回写解析失败信息到 %s 失败：%s", task.path, e)
