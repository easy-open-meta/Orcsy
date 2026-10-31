"""Web 可视化看板：看板页面 + JSON API，基于标准库 http.server。

API（均为 GET/POST JSON）：
  GET  /api/state        全量状态：调度器、各项目、任务列表、日志尾部
  POST /api/scan         手动触发一轮扫描（立即返回，后台执行）
  GET  /api/task         单个任务完整内容 ?project=&id=（编辑弹窗用，不截断）
  POST /api/task         创建/更新任务  {project, id, title, priority, status, description}
  POST /api/task/delete  删除任务       {project, id}
  POST /api/task/status  修改任务状态   {project, id, status}（blocked→open 重新入队等）
  POST /api/models       拉取大模型列表 {provider?, base_url?, api_key?}（缺省用已存配置）
  POST /api/llm/test     测试大模型连通性 {provider?, base_url?, api_key?, model?}
                         （一条极短对话；缺省回退已存配置；失败返回 ok:false+原因）

项目管理（可视化配置）：
  POST /api/browse       浏览本地目录    {path}，返回子目录列表（供新建项目选路径）
  POST /api/probe        静态探测技术栈  {path}，返回 stack/run/test（新建项目预填）
  POST /api/mkdir        新建文件夹      {path}（目录选择器内创建，父目录须存在）
  POST /api/project      新建/修改项目   {name, path, tasks_dir, guide_file,
                                          max_tasks_per_cycle, max_agent_turns,
                                          allowed_branch, allowed_commands, enabled,
                                          stack/run/test 可选：写入 AGENT.md 的 Project 节}
  POST /api/project/delete        删除项目（仅从配置移除，不动磁盘）
  POST /api/project/open-folder   用系统文件管理器打开项目根目录 {name}
  GET/POST /api/guide    读取/保存项目 AGENT.md {project, content?}
  POST /api/config       保存模型/调度/看板配置 {llm:{...}, scheduler:{...}, web:{...}}
                         保存后写回 config.yaml 并热重载调度器

技能库（存于配置目录 skills/，绝不写入项目目录）：
  GET  /api/skill          读取单个技能 ?name=（编辑弹窗用：字段+正文）
  POST /api/skill          新建/更新技能 {original_name?, name, description, scope, enabled, body}
  POST /api/skill/delete   （已废弃，由 /api/skill/remove 取代）
  POST /api/skill/remove   删除技能 {names: [..]}（清掉技能库中的副本；导入技能可重新扫描找回，支持批量）
  POST /api/skill/enabled  启用/停用技能 {name, enabled}（定向改 frontmatter）
  POST /api/skill/import   导入本地技能目录 {path}（目录须含 SKILL.md，整目录复制）
  GET  /api/skill/scan     扫描常见 AI 工具的用户级技能目录（ZCode/Claude Code/Codex/OpenCode 等），
                           按工具分组返回可导入清单，exists 标记是否已导入
  POST /api/skill/import-batch  批量导入 {paths: [本地技能目录]}；同名已存在则跳过并记录
  POST /api/skill/open-folder  用系统文件管理器打开技能目录 {name}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import yaml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from . import skills as skills_lib
from .agent import ProjectAgent
from .config import (AppConfig, LLMConfig, ProjectConfig, SchedulerConfig,
                     TaskStatus, WebConfig, normalize_llm_override, resolve_llm)
from .issues import IssueError, normalize_repo, sync_issues
from .llm import LLMError, list_models, test_connection
from .logging_setup import get_logger
from .scheduler import Scheduler
from .skills import Skill
from .task_model import Task, parse_task_file, priority_rank, scan_tasks

log = get_logger("web")

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
VALID_STATUS = ("open", "in_progress", "done", "blocked", "cancelled")
_NAME_RE = re.compile(r"^[\w][\w\-]*$")

# 浏览目录时忽略的噪音项
_DIR_IGNORE = {"$Recycle.Bin", "System Volume Information", "Windows", "AppData",
               "node_modules", ".git", ".venv", "__pycache__", ".worktree-arranger"}


# 新项目/导入项目的 AGENT.md 初始内容：统一走 templates.default_guide（唯一来源）


def read_log_tail(path: str, max_bytes: int = 60000) -> str:
    # 尾部窗口取 60KB：日志里现在有模型思考内容（每轮可达数千字），
    # 窗口太小会只剩最近一两条，看板里看不到执行主线
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


class AppState:
    """Web 层共享的应用状态与操作。"""

    def __init__(self, config: AppConfig, scheduler: Scheduler,
                 log_path: str | None = None) -> None:
        self.config = config
        self.scheduler = scheduler
        # 解析后的日志绝对路径（cmd_web 传入）；不传则退回原始配置值
        # ——config.logging.file 常是相对路径，直接按进程 CWD 读会读错位置
        self.log_path = log_path
        # 技能库根目录（配置文件同级 skills/），运行期不变
        self.skills_dir = skills_lib.skills_base(config)
        self.agents: dict[str, ProjectAgent] = {
            p.name: ProjectAgent(p, resolve_llm(config.llm, p.llm,
                                                context=f"项目 {p.name} 的 llm"),
                                 skills_base=self.skills_dir)
            for p in config.projects
        }

    # ---- 查询 ----

    def full_state(self) -> dict:
        projects = []
        for name, agent in self.agents.items():
            p = agent.project
            open_tasks, errors = scan_tasks(agent.tasks_dir)
            archive_dir = agent.tasks_dir / "done"
            archived = []
            if archive_dir.is_dir():
                for f in sorted(archive_dir.glob("*.md")):
                    archived.append(self._task_dict(parse_task_file(f)))
            # 所有文件（含 in_progress/blocked/cancelled），不只是 open
            all_tasks = []
            if agent.tasks_dir.is_dir():
                for f in sorted(agent.tasks_dir.glob("*.md")):
                    t = parse_task_file(f)
                    all_tasks.append(self._task_dict(t))
            projects.append({
                "name": name,
                "path": str(p.path),
                "enabled": p.enabled,
                "tasks_dir": p.tasks_dir,
                "guide_file": p.guide_file,
                "max_tasks_per_cycle": p.max_tasks_per_cycle,
                "max_agent_turns": p.max_agent_turns,
                "allowed_branch": p.allowed_branch,
                "allowed_commands": p.allowed_commands,
                "llm": p.llm or None,
                "repo": p.repo or None,
                "is_git_repo": agent.executor.is_git_repo,
                "guide_file_path": str(p.path / p.guide_file),
                "guide_exists": (p.path / p.guide_file).exists(),
                "tasks": all_tasks,
                "parse_errors": [{"id": t.id, "path": str(t.path), "error": t.error}
                                 for t in errors],
                "archived": archived[-30:],
            })
        return {
            "version": __version__,
            "config_path": str(self.config.source_path) if self.config.source_path else "",
            "llm": {
                "provider": self.config.llm.provider,
                "model": self.config.llm.model,
                "base_url": self.config.llm.base_url,
                "key_configured": bool(self.config.llm.api_key),
                "api_key": self.config.llm.api_key,       # 设置页回显（本机使用，无外发）
                "temperature": self.config.llm.temperature,
                "max_tokens": self.config.llm.max_tokens,
                "max_context_tokens": self.config.llm.max_context_tokens,
                "enable_thinking": self.config.llm.enable_thinking,
                "timeout_seconds": self.config.llm.timeout_seconds,
            },
            "scheduler": self.scheduler.status(),
            "scheduler_cfg": {
                "interval_seconds": self.config.scheduler.interval_seconds,
                "max_parallel_agents": self.config.scheduler.max_parallel_agents,
            },
            "web_cfg": {
                "host": self.config.web.host,
                "port": self.config.web.port,
                "open_browser": self.config.web.open_browser,
            },
            "logging": {"retention_days": self.config.logging.retention_days},
            "app_autostart": self.app_autostart_status(),
            "skills_dir": str(self.skills_dir),
            "skills": [self._skill_dict(s) for s in skills_lib.load_skills(self.skills_dir)],
            "projects": projects,
            "log_tail": read_log_tail(self.log_path or self.config.logging.file),
        }

    @staticmethod
    def _task_dict(t: Task) -> dict:
        return {
            "id": t.id, "title": t.title, "priority": t.priority,
            "rank": t.rank, "status": t.status, "labels": t.labels,
            "assignee": t.assignee, "error": t.error,
            "file": str(t.path),
            "description": t.description[:2000],
        }

    # ---- 操作 ----

    def trigger_scan(self) -> dict:
        """后台线程触发一轮扫描，立即返回。"""
        if self.scheduler.is_running:
            return {"ok": False, "message": "上一轮扫描尚未结束"}
        threading.Thread(target=self.scheduler.run_cycle,
                         name="manual-scan", daemon=True).start()
        return {"ok": True, "message": "已触发一轮扫描"}

    def _task_path(self, project: str, task_id: str) -> Path:
        agent = self.agents.get(project)
        if agent is None:
            raise ValueError(f"未知项目：{project}")
        if not re.fullmatch(r"[\w][\w\-. ]{0,80}", task_id):
            raise ValueError(f"任务 ID 不合法：{task_id!r}（只允许字母数字_-. 和空格）")
        path = (agent.tasks_dir / f"{task_id}.md").resolve()
        if agent.tasks_dir.resolve() not in path.parents:
            raise ValueError("任务路径越界")
        return path

    def create_or_update_task(self, data: dict) -> dict:
        project = str(data.get("project", ""))
        task_id = str(data.get("id", "")).strip()
        title = str(data.get("title", "")).strip()
        priority = str(data.get("priority", "P2")).strip().upper()
        status = str(data.get("status", "open")).strip().lower()
        description = str(data.get("description", "")).strip()

        if not title:
            raise ValueError("标题不能为空")
        if priority_rank(priority) > 4:
            raise ValueError(f"优先级不合法：{priority}")
        if status not in VALID_STATUS:
            raise ValueError(f"状态不合法：{status}")

        # id 留空 = 自动生成「优先级-标题slug」，与模板的 P1-example-bugfix 命名一致
        if not task_id:
            task_id = self._auto_task_id(project, priority, title)

        path = self._task_path(project, task_id)
        labels = data.get("labels") or []
        labels_line = ", ".join(str(x) for x in labels) if isinstance(labels, list) else ""
        fm = [
            "---",
            f"title: {title}",
            f"priority: {priority}",
            f"status: {status}",
        ]
        if labels_line:
            fm.append(f"labels: [{labels_line}]")
        assignee = str(data.get("assignee", "")).strip()
        if assignee:
            fm.append(f"assignee: {assignee}")
        fm.append("---")
        path.parent.mkdir(parents=True, exist_ok=True)
        # 描述留空用标题兜底：空正文会被解析器判为格式错误任务
        body = description or title
        path.write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
        log.info("Web 创建/更新任务 %s/%s [%s]", project, task_id, priority)
        return {"ok": True, "file": str(path), "id": task_id}

    def _auto_task_id(self, project: str, priority: str, title: str) -> str:
        """从优先级 + 标题生成唯一任务 ID：P2-修复登录超时 / P1-fix-login。"""
        agent = self.agents.get(project)
        if agent is None:
            raise ValueError(f"未知项目：{project}")
        slug = re.sub(r"\s+", "-", title.strip())
        slug = re.sub(r"[^\w\-]", "", slug).strip("-")[:40].strip("-") or "task"
        existing = {p.stem for p in agent.tasks_dir.glob("*.md")} if agent.tasks_dir.is_dir() else set()
        base = f"{priority}-{slug}"
        task_id, n = base, 2
        while task_id in existing:
            task_id = f"{base}-{n}"
            n += 1
        return task_id

    def set_task_status(self, data: dict) -> dict:
        project = str(data.get("project", ""))
        task_id = str(data.get("id", ""))
        status = str(data.get("status", "")).strip().lower()
        if status not in VALID_STATUS:
            raise ValueError(f"状态不合法：{status}")
        path = self._task_path(project, task_id)
        if not path.exists():
            raise ValueError(f"任务不存在：{task_id}")
        text = path.read_text(encoding="utf-8")
        m = FRONTMATTER_RE.match(text)
        if not m:
            raise ValueError("任务文件缺少 frontmatter，无法修改状态")
        front, rest = m.group(1), text[m.end():]
        if re.search(r"^status:", front, re.MULTILINE):
            front = re.sub(r"^status:.*$", f"status: {status}", front, flags=re.MULTILINE)
        else:
            front += f"\nstatus: {status}"
        path.write_text(f"---\n{front}\n---\n{rest}", encoding="utf-8")
        log.info("Web 修改任务状态 %s/%s -> %s", project, task_id, status)
        return {"ok": True}

    def delete_task(self, data: dict) -> dict:
        project = str(data.get("project", ""))
        task_id = str(data.get("id", ""))
        path = self._task_path(project, task_id)
        if not path.exists():
            raise ValueError(f"任务不存在：{task_id}")
        path.unlink()
        log.info("Web 删除任务 %s/%s", project, task_id)
        return {"ok": True}

    def delete_tasks_batch(self, data: dict) -> dict:
        """批量删除任务：{project, ids: [...]}。单条缺失不阻断，返回删除/失败明细。"""
        project = str(data.get("project", "")).strip()
        if not self.agents.get(project):
            raise ValueError(f"项目不存在：{project}")
        ids = data.get("ids")
        if not isinstance(ids, list) or not ids:
            raise ValueError("ids 必须是非空数组")
        if len(ids) > 500:
            raise ValueError("单次最多删除 500 个任务")
        deleted, failed = 0, []
        for raw in ids[:500]:
            task_id = str(raw).strip()
            try:
                path = self._task_path(project, task_id)
                if not path.exists():
                    failed.append(task_id)
                    continue
                path.unlink()
                deleted += 1
            except (ValueError, OSError) as e:
                failed.append(f"{task_id}（{e}）")
        log.info("Web 批量删除任务 %s：删除 %d，失败 %d", project, deleted, len(failed))
        msg = f"已删除 {deleted} 个任务"
        if failed:
            msg += f"，{len(failed)} 个失败：{'、'.join(failed[:5])}{'…' if len(failed) > 5 else ''}"
        return {"ok": True, "deleted": deleted, "failed": failed, "message": msg}

    def clear_tasks(self, data: dict) -> dict:
        """一键清空项目任务：删除 tasks/ 根目录全部 .md（含解析失败文件），
        已归档任务（tasks/done/）不动。"""
        project = str(data.get("project", "")).strip()
        agent = self.agents.get(project)
        if agent is None:
            raise ValueError(f"项目不存在：{project}")
        tasks_dir = agent.tasks_dir
        files = [p for p in tasks_dir.glob("*.md")]
        cleared = 0
        for path in files:
            try:
                path.unlink()
                cleared += 1
            except OSError as e:
                log.warning("清空任务：删除 %s 失败（%s）", path, e)
        log.info("Web 清空项目任务 %s：清除 %d/%d 个任务文件（归档未动）",
                 project, cleared, len(files))
        return {"ok": True, "cleared": cleared,
                "message": f"已清空 {cleared} 个任务（已归档任务不受影响）"}

    def get_task(self, params: dict) -> dict:
        """读取单个任务的完整内容（编辑/查看弹窗用；/api/state 的描述截断到 2000 字）。

        待办在 tasks/ 根目录；已完成任务被归档进 tasks/done/ ——两处都找，
        归档命中时带 archived=true，看板以只读模式打开。
        """
        project = str(params.get("project", ""))
        task_id = str(params.get("id", ""))
        agent = self.agents.get(project)
        if agent is None:
            raise ValueError(f"未知项目：{project}")
        path = self._task_path(project, task_id)  # 复用 ID 合法性与路径越界校验
        archived = False
        if not path.exists():
            archived = True
            path = agent.tasks_dir / "done" / path.name
        if not path.exists():
            raise ValueError(f"任务不存在：{task_id}")
        from .task_model import parse_task_file

        t = parse_task_file(path)
        if t.error:
            return {"ok": True, "id": task_id, "title": t.title, "archived": archived,
                    "priority": "P2", "labels": [], "assignee": "",
                    "description": "", "error": t.error}
        return {"ok": True, "id": t.id, "title": t.title, "priority": t.priority,
                "status": t.status, "labels": t.labels, "assignee": t.assignee,
                "description": t.description, "archived": archived}

    def make_dir(self, data: dict) -> dict:
        """在指定位置创建新文件夹（目录选择器「新建项目」流程用）。"""
        path = str(data.get("path", "")).strip().strip('"')
        if not path:
            raise ValueError("路径不能为空")
        p = Path(path)
        if p.parent == p or not p.parent.is_dir():
            raise ValueError(f"父目录不存在：{p.parent}")
        name = p.name
        if not name or re.search(r'[\\/:*?"<>|]', name):
            raise ValueError(f"文件夹名不合法：{name!r}（不能包含 \\/:*?\"<>|）")
        try:
            p.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            raise ValueError(f"文件夹已存在：{path}")
        except OSError as e:
            raise ValueError(f"创建失败：{e}")
        log.info("Web 创建目录 %s", p)
        return {"ok": True, "path": str(p)}

    def fetch_models(self, data: dict) -> dict:
        """拉取大模型服务的模型列表（请求体缺省时回退到已保存的接入配置）。"""
        llm = self.config.llm
        provider = str(data.get("provider") or llm.provider).strip().lower()
        base_url = str(data.get("base_url") or llm.base_url).strip()
        api_key = str(data.get("api_key") or llm.api_key).strip()
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("API 地址必须以 http(s):// 开头")
        try:
            models = list_models(provider, base_url, api_key, llm.timeout_seconds)
        except LLMError as e:
            return {"ok": False, "message": str(e)}
        if not models:
            return {"ok": False, "message": "服务返回了空模型列表，请检查 API 地址与密钥"}
        # OpenAI 兼容服务通常不带窗口元数据，用内置规格表补全（未命中保持 None）
        from .llm import model_spec

        for m in models:
            if m["context_window"] is None or m["max_output_tokens"] is None:
                spec = model_spec(m["id"])
                if spec:
                    m["context_window"] = m["context_window"] or spec[0]
                    m["max_output_tokens"] = m["max_output_tokens"] or spec[1]
        return {"ok": True, "models": models}

    def test_model(self, data: dict) -> dict:
        """测试大模型连通性（看板「测试连接」按钮）：一条极短对话验证网络/密钥/模型名。

        请求体缺省字段回退到已存配置（与 fetch_models 同模式），看板可用表单
        草稿值在保存前先行测试。
        """
        llm = self.config.llm
        provider = str(data.get("provider") or llm.provider).strip().lower()
        base_url = str(data.get("base_url") or llm.base_url).strip()
        api_key = str(data.get("api_key") or llm.api_key).strip()
        model = str(data.get("model") or llm.model).strip()
        if provider not in ("openai", "anthropic"):
            raise ValueError(f"provider 不合法：{provider}（openai / anthropic）")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("API 地址必须以 http(s):// 开头")
        if not model:
            raise ValueError("model 不能为空")
        try:
            r = test_connection(provider, base_url, api_key, model, llm.timeout_seconds)
        except LLMError as e:
            return {"ok": False, "message": str(e)}
        reply = r["reply"]
        message = f"连接成功（{r['elapsed_seconds']}s）"
        if reply:
            message += f" · 模型回复：{reply}"
        else:
            message += " · 模型未返回正文（推理模型常见，连接本身正常）"
        log.info("Web 测试模型连通：model=%s %.1fs ok", model, r["elapsed_seconds"])
        return {"ok": True, "reply": reply,
                "elapsed_seconds": r["elapsed_seconds"], "message": message}

    # ---- 项目管理 ----

    @staticmethod
    def _safe_project_name(name: str) -> str:
        name = str(name).strip()
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"项目名不合法：{name!r}（字母数字/下划线/连字符，且不以符号开头）")
        return name

    def browse_dir(self, data: dict) -> dict:
        """列出指定目录的子目录，供前端逐级选择本地路径。"""
        raw = str(data.get("path", "")).strip()
        p = Path(raw).resolve() if raw else None

        if p is None or not p.is_dir():
            # 根入口：给出各盘符与用户目录
            roots = []
            import string
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:/")
                if drive.exists():
                    roots.append(str(drive))
            home = Path.home()
            return {"ok": True, "path": "", "parent": None,
                    "dirs": roots + [str(home / "Documents"), str(home / "Desktop")]}

        dirs = []
        try:
            for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if child.is_dir() and child.name not in _DIR_IGNORE and not child.name.startswith("$"):
                    dirs.append(str(child))
                if len(dirs) >= 300:
                    break
        except PermissionError:
            return {"ok": True, "path": str(p), "parent": None, "dirs": [],
                    "note": "无权限读取该目录"}
        parent = str(p.parent) if p.parent != p else None
        return {"ok": True, "path": str(p), "parent": parent, "dirs": dirs}

    def probe_project_api(self, data: dict) -> dict:
        """静态探测目录的技术栈与常用命令（新建项目对话框自动预填用）。"""
        raw = str(data.get("path", "")).strip()
        p = Path(raw).expanduser() if raw else None
        if p is None or not p.is_dir():
            raise ValueError(f"目录不存在：{raw}")
        from .probe import probe_project
        return {"ok": True, **probe_project(p)}

    def _project_payload(self, data: dict, original_name: str | None = None) -> ProjectConfig:
        name = self._safe_project_name(data.get("name", original_name or ""))
        path = Path(str(data.get("path", "")).strip()).expanduser()
        if not path.is_absolute():
            raise ValueError("项目路径必须是绝对路径")
        if not path.is_dir():
            raise ValueError(f"目录不存在：{path}")
        tasks_dir = str(data.get("tasks_dir") or "tasks").strip().replace("\\", "/")
        guide_file = str(data.get("guide_file") or "AGENT.md").strip()
        max_tasks = int(data.get("max_tasks_per_cycle") or 1)
        max_turns = int(data.get("max_agent_turns") or 40)
        if not (1 <= max_tasks <= 50):
            raise ValueError("每轮任务数应为 1~50")
        if not (5 <= max_turns <= 200):
            raise ValueError("工具循环轮数应为 5~200")
        cmds = data.get("allowed_commands")
        allowed = [str(c).strip() for c in cmds if str(c).strip()] if isinstance(cmds, list) else None
        branch = str(data.get("allowed_branch") or "").strip() or None
        # 项目专属模型绑定：字典则清洗+校验（值非法立刻报错），显式 null 或全空=清除绑定
        llm_raw = data.get("llm")
        if llm_raw is not None and not isinstance(llm_raw, dict):
            raise ValueError("llm 绑定必须是键值对字典或 null")
        llm = None
        if isinstance(llm_raw, dict):
            llm = normalize_llm_override(llm_raw) or None
            if llm:
                resolve_llm(self.config.llm, llm, context=f"项目 {name} 的 llm")
        # 远程仓库绑定（Issues 导入用）：null/空=清除；平台由 URL 识别或显式 provider
        repo_raw = data.get("repo")
        if repo_raw is not None and not isinstance(repo_raw, dict):
            raise ValueError("repo 绑定必须是键值对字典或 null")
        repo = None
        if isinstance(repo_raw, dict) and repo_raw:
            try:
                repo = normalize_repo(repo_raw, context=f"项目 {name} 的 repo")
            except IssueError as e:
                raise ValueError(str(e)) from e
        return ProjectConfig(
            name=name, path=path, enabled=bool(data.get("enabled", True)),
            tasks_dir=tasks_dir, guide_file=guide_file,
            max_tasks_per_cycle=max_tasks, max_agent_turns=max_turns,
            allowed_commands=allowed, allowed_branch=branch, llm=llm, repo=repo,
        )

    def _save_config(self, cfg: AppConfig) -> None:
        """把配置对象写回 config.yaml（保留注释做不到，字段顺序固定）。"""
        src = cfg.source_path
        if src is None:
            raise ValueError("配置文件来源未知，无法保存")
        doc = {
            "llm": {
                "provider": cfg.llm.provider, "api_key": cfg.llm.api_key,
                "base_url": cfg.llm.base_url, "model": cfg.llm.model,
                "temperature": cfg.llm.temperature, "max_tokens": cfg.llm.max_tokens,
                "max_context_tokens": cfg.llm.max_context_tokens,
                "enable_thinking": cfg.llm.enable_thinking,
                "timeout_seconds": cfg.llm.timeout_seconds,
            },
            "scheduler": {
                "interval_seconds": cfg.scheduler.interval_seconds,
                "max_parallel_agents": cfg.scheduler.max_parallel_agents,
                "run_once": cfg.scheduler.run_once,
            },
            "logging": {"level": cfg.logging.level, "file": cfg.logging.file,
                        "retention_days": cfg.logging.retention_days},
            "web": {"host": cfg.web.host, "port": cfg.web.port,
                    "open_browser": cfg.web.open_browser},
            "projects": [
                {
                    "name": p.name, "path": str(p.path), "enabled": p.enabled,
                    "tasks_dir": p.tasks_dir, "guide_file": p.guide_file,
                    "max_tasks_per_cycle": p.max_tasks_per_cycle,
                    "max_agent_turns": p.max_agent_turns,
                    **({"allowed_branch": p.allowed_branch} if p.allowed_branch else {}),
                    **({"allowed_commands": p.allowed_commands} if p.allowed_commands else {}),
                    **({"llm": p.llm} if p.llm else {}),
                    **({"repo": p.repo} if p.repo else {}),
                }
                for p in cfg.projects
            ],
        }
        src.write_text(
            "# orcsy 配置（由看板自动保存）\n"
            + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _apply_config(self, new_cfg: AppConfig) -> None:
        self.config = new_cfg
        # agents 覆盖全部项目（含停用的，便于看板展示与编辑）；
        # 调度器只留启用的（reload_config 已按 enabled 过滤）
        self.agents = {p.name: ProjectAgent(p, resolve_llm(new_cfg.llm, p.llm,
                                                           context=f"项目 {p.name} 的 llm"),
                                            skills_base=self.skills_dir)
                       for p in new_cfg.projects}

    def save_project(self, data: dict) -> dict:
        """新建或修改项目并持久化 + 热重载。original_name 用于重命名。"""
        original = data.get("original_name")
        original = str(original).strip() if original else None
        proj = self._project_payload(data, original_name=original)

        names = [p.name for p in self.config.projects]
        if original is None:
            if proj.name in names:
                raise ValueError(f"项目名已存在：{proj.name}")
            self.config.projects.append(proj)
            action = "新建"
        else:
            if original not in names:
                raise ValueError(f"项目不存在：{original}")
            idx = names.index(original)
            if proj.name != original and proj.name in names:
                raise ValueError(f"项目名已存在：{proj.name}")
            self.config.projects[idx] = proj
            action = "修改"
            if proj.name != original:
                # 项目改名：同步重映射专属技能的 scope（悬空 scope 无害但会失效）
                for s in skills_lib.load_skills(self.skills_dir):
                    if s.scope == original and not s.error:
                        try:
                            skills_lib.set_skill_meta(s.path, scope=proj.name)
                        except OSError as e:
                            log.warning("技能 %s 的 scope 重映射失败：%s", s.name, e)

        # 新项目（含导入现有项目）：只初始化任务目录与 AGENT.md，均不覆盖已有；
        # 不再生成示例任务等其它文件，已有代码的项目根目录保持干净
        created = []
        tasks_dir = proj.path / proj.tasks_dir
        if not tasks_dir.exists():
            tasks_dir.mkdir(parents=True, exist_ok=True)
            created.append(f"{proj.tasks_dir}/")
        guide = proj.path / proj.guide_file
        if not guide.exists():
            from .templates import default_guide
            # 请求里带了 stack/run/test 键（新建项目对话框）就按用户填的来，
            # 没带（CLI init / 老调用方）才走自动探测
            if any(k in data for k in ("stack", "run", "test")):
                overrides = {k: str(data.get(k) or "").strip()
                             for k in ("stack", "run", "test")}
            else:
                overrides = None
            guide.write_text(
                default_guide(proj.name, proj.path, overrides=overrides),
                encoding="utf-8")
            created.append(proj.guide_file)

        self.scheduler.reload_config(self.config)
        self._apply_config(self.config)
        self._save_config(self.config)
        log.info("Web %s项目 %s（%s）", action, proj.name, proj.path)
        return {"ok": True, "created": created, "message":
                f"已{action}项目 {proj.name}" + (f"，初始化了 {'、'.join(created)}" if created else "")}

    def sync_project_issues(self, body: dict) -> dict:
        """拉取绑定仓库的 open Issues 导入任务队列（单向；去重含 done/ 归档）。"""
        # 兼容两种键名：项目级端点惯例是 name（与 /api/project/delete 一致）
        name = str(body.get("name") or body.get("project") or "").strip()
        agent = self.agents.get(name)
        if agent is None:
            raise ValueError(f"项目不存在：{name}")
        repo = agent.project.repo
        if not repo:
            raise ValueError(f"项目 {name} 未绑定远程仓库（项目设置里先填仓库 URL）。")
        try:
            result = sync_issues(name, repo, agent.tasks_dir)
        except IssueError as e:
            raise ValueError(str(e)) from e
        imported, skipped = result["imported"], result["skipped"]
        log.info("Web 项目 %s 拉取 Issues：导入 %d，跳过 %d（共 %d）",
                 name, imported, skipped, result["total"])
        return {"ok": True, "imported": imported, "skipped": skipped,
                "message": (f"导入 {imported} 个 Issue 为任务，跳过 {skipped} 个已存在"
                            if imported or skipped else
                            f"仓库没有新的 open Issue（共 {result['total']} 条）")}

    def delete_project(self, data: dict) -> dict:
        name = str(data.get("name", "")).strip()
        names = [p.name for p in self.config.projects]
        if name not in names:
            raise ValueError(f"项目不存在：{name}")
        self.config.projects.pop(names.index(name))
        self.scheduler.reload_config(self.config)
        self._apply_config(self.config)
        self._save_config(self.config)
        log.info("Web 删除项目 %s（仅移出配置，未删磁盘文件）", name)
        return {"ok": True, "message": f"已移除项目 {name}（项目目录未删除）"}

    def open_project_folder(self, data: dict) -> dict:
        """用系统文件管理器打开项目根目录（看板「打开文件夹」按钮）。"""
        name = str(data.get("name", "")).strip()
        agent = self.agents.get(name)
        if agent is None:
            raise ValueError(f"未知项目：{name}")
        path = agent.project.path
        if not path.is_dir():
            raise ValueError(f"项目目录不存在：{path}")
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 — 本机用户主动打开自己的项目目录
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(path)])
        log.info("Web 打开项目文件夹 %s（%s）", name, path)
        return {"ok": True, "path": str(path)}

    # ---- AGENT.md 编辑 ----

    def app_autostart_status(self) -> dict:
        from .autostart import is_enabled

        return {"ok": True, "enabled": is_enabled(),
                "supported": sys.platform == "win32"}

    def set_app_autostart(self, data: dict) -> dict:
        from .autostart import disable, enable

        want = bool(data.get("enabled"))
        changed = enable() if want else disable()
        log.info("Web 开机自启动 -> %s", "开" if want else "关")
        return {"ok": True, "enabled": want,
                "message": "开机自启动已" + ("开启" if want else "关闭")
                if changed else "设置未变化"}

    def get_guide(self, data: dict) -> dict:
        project = str(data.get("project", "")).strip()
        agent = self.agents.get(project)
        if agent is None:
            raise ValueError(f"未知项目：{project}")
        guide_path = agent.project.path / agent.project.guide_file
        content = ""
        if guide_path.exists():
            try:
                content = guide_path.read_text(encoding="utf-8")
            except OSError as e:
                raise ValueError(f"读取指引失败：{e}")
        return {"ok": True, "project": project,
                "file": str(guide_path), "content": content}

    def save_guide(self, data: dict) -> dict:
        project = str(data.get("project", "")).strip()
        content = str(data.get("content", ""))
        agent = self.agents.get(project)
        if agent is None:
            raise ValueError(f"未知项目：{project}")
        if not content.strip():
            raise ValueError("指引内容不能为空")
        guide_path = agent.project.path / agent.project.guide_file
        try:
            guide_path.parent.mkdir(parents=True, exist_ok=True)  # 项目目录可能刚建
            guide_path.write_text(content, encoding="utf-8")
        except OSError as e:
            raise ValueError(f"保存指引失败：{e}")
        log.info("Web 更新 AGENT.md %s（%d 字符）", project, len(content))
        return {"ok": True, "file": str(guide_path)}

    # ---- 技能库（存于配置目录 skills/，不落项目目录） ----

    @staticmethod
    def _skill_dict(s: Skill) -> dict:
        return {"name": s.name, "description": s.description, "scope": s.scope,
                "enabled": s.enabled, "path": str(s.path), "origin": s.origin,
                "error": s.error}

    def get_skill(self, params: dict) -> dict:
        """读取单个技能（编辑弹窗用：解析字段 + 正文全文）。"""
        name = str(params.get("name", "")).strip()
        d = skills_lib.skill_dir(self.skills_dir, name)
        s = skills_lib.parse_skill_dir(d)
        body = "" if s.error else skills_lib.read_skill_body(s.path)
        return {"ok": True, **self._skill_dict(s), "body": body}

    def save_skill(self, data: dict) -> dict:
        """新建或更新技能。original_name 存在表示更新（支持改名 = 目录改名）。"""
        name = str(data.get("name", "")).strip()
        original = str(data.get("original_name", "")).strip() or None
        description = str(data.get("description", "")).strip()
        scope = str(data.get("scope", "")).strip() or "global"
        enabled = bool(data.get("enabled", True))
        body = str(data.get("body", ""))

        if original is None:
            d = skills_lib.skill_dir(self.skills_dir, name)
            if d.exists():
                raise ValueError(f"技能名已存在：{name}")
            path = skills_lib.write_skill(self.skills_dir, name,
                                          description, scope, enabled, body)
            log.info("Web 新建技能 %s（%s）", name, path)
            return {"ok": True, "message": f"已新建技能 {name}"}

        d = skills_lib.skill_dir(self.skills_dir, original)
        if not d.is_dir():
            raise ValueError(f"技能不存在：{original}")
        if name != original:
            d = skills_lib.rename_skill_dir(self.skills_dir, original, name)
        skills_lib.update_skill(d, description, scope, enabled, body)
        log.info("Web 更新技能 %s", name)
        return {"ok": True, "message": f"已保存技能 {name}"}

    def remove_skills_api(self, data: dict) -> dict:
        """删除技能（支持批量）：清掉技能库中的副本。

        外部导入的技能原样本仍在各 AI 工具目录里，重新扫描即可找回；本程序
        新建的技能没有别处副本，删除后不可恢复（确认文案由前端按 origin 区分）。
        """
        names = data.get("names")
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list) or not [n for n in names if str(n).strip()]:
            raise ValueError("未选择要删除的技能")
        removed: list[str] = []
        skipped: list[dict] = []
        for raw in names[:100]:
            name = str(raw).strip()
            try:
                skills_lib.delete_skill(self.skills_dir, name)
                removed.append(name)
            except ValueError as e:
                skipped.append({"name": name, "reason": str(e)})
        if not removed and not skipped:
            raise ValueError("未选择要删除的技能")
        msg = f"已删除 {len(removed)} 个技能"
        if skipped:
            msg += f"，跳过 {len(skipped)} 个"
        log.info("Web 删除技能：%s", removed)
        return {"ok": True, "removed": removed, "skipped": skipped, "message": msg}

    def set_skill_enabled(self, data: dict) -> dict:
        name = str(data.get("name", "")).strip()
        enabled = bool(data.get("enabled", True))
        d = skills_lib.skill_dir(self.skills_dir, name)
        s = skills_lib.parse_skill_dir(d)
        if s.error:
            raise ValueError(f"技能文件解析失败：{s.error}")
        skills_lib.set_skill_meta(s.path, enabled=enabled)
        log.info("Web 技能 %s 启用 -> %s", name, enabled)
        return {"ok": True, "enabled": enabled}

    def import_skill_api(self, data: dict) -> dict:
        """从本地目录导入技能（目录选择器选定路径，整目录复制进技能库）。"""
        raw = str(data.get("path", "")).strip().strip('"')
        if not raw:
            raise ValueError("路径不能为空")
        s = skills_lib.import_skill(Path(raw).expanduser(), self.skills_dir)
        return {"ok": True, "skill": self._skill_dict(s),
                "message": f"已导入技能 {s.name}"}

    def scan_skill_sources(self) -> dict:
        """扫描常见 AI 工具的用户级技能目录（看板「扫描 AI 工具」弹窗）。

        同名技能跨来源只显示一次（共享去重集合）。
        """
        return {"ok": True, "sources": skills_lib.scan_sources(self.skills_dir)}

    def import_skills_batch(self, data: dict) -> dict:
        """批量导入扫描到的技能；同名已存在/无效的跳过并记录，不中断整批。"""
        paths = data.get("paths")
        if not isinstance(paths, list) or not [p for p in paths if str(p).strip()]:
            raise ValueError("未选择要导入的技能")
        imported: list[str] = []
        skipped: list[dict] = []
        for raw in paths[:50]:  # 单批上限保护
            src = Path(str(raw).strip().strip('"')).expanduser()
            try:
                s = skills_lib.import_skill(src, self.skills_dir)
                imported.append(s.name)
            except ValueError as e:
                skipped.append({"path": str(src), "reason": str(e)})
        if not imported and not skipped:
            raise ValueError("未选择要导入的技能")
        msg = f"已导入 {len(imported)} 个技能"
        if skipped:
            msg += f"，跳过 {len(skipped)} 个（已存在或无效）"
        log.info("Web 批量导入技能：导入 %s，跳过 %d 个", imported, len(skipped))
        return {"ok": True, "imported": imported, "skipped": skipped, "message": msg}

    def open_skill_folder(self, data: dict) -> dict:
        """用系统文件管理器打开技能目录（管理附属脚本/参考文件）。"""
        name = str(data.get("name", "")).strip()
        d = skills_lib.skill_dir(self.skills_dir, name)
        if not d.is_dir():
            raise ValueError(f"技能不存在：{name}")
        if sys.platform == "win32":
            os.startfile(d)  # noqa: S606 — 本机用户主动打开自己的技能目录
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(d)])
        return {"ok": True, "path": str(d)}

    # ---- 模型/调度/看板配置 ----

    def save_settings(self, data: dict) -> dict:
        sched = data.get("scheduler") or {}
        web = data.get("web") or {}

        # 分块保存：只带 scheduler/web/logging 的请求不校验/覆盖 llm
        llm = data.get("llm") or {}
        save_llm = "llm" in data
        provider = str(llm.get("provider", self.config.llm.provider)).strip().lower()
        if save_llm and provider not in ("openai", "anthropic"):
            raise ValueError(f"provider 不合法：{provider}（openai / anthropic）")
        model = str(llm.get("model", "")).strip()
        if save_llm and not model:
            raise ValueError("model 不能为空")
        base_url = str(llm.get("base_url", "")).strip()
        if save_llm and not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http(s):// 开头")
        interval = int(sched.get("interval_seconds") or self.config.scheduler.interval_seconds)
        if not (30 <= interval <= 86400):
            raise ValueError("扫描间隔应为 30~86400 秒")
        parallel = int(sched.get("max_parallel_agents") or self.config.scheduler.max_parallel_agents)
        if not (1 <= parallel <= 16):
            raise ValueError("并行 Agent 数应为 1~16")
        port = int(web.get("port") or self.config.web.port)
        if not (1 <= port <= 65535):
            raise ValueError("端口应为 1~65535")

        max_tokens = int(llm.get("max_tokens") or self.config.llm.max_tokens or 8192)
        max_context = int(llm.get("max_context_tokens")
                          or self.config.llm.max_context_tokens or 131072)
        if save_llm:
            if not (256 <= max_tokens <= 200000):
                raise ValueError("最大输出 max_tokens 应为 256~200000")
            if not (4096 <= max_context <= 2_000_000):
                raise ValueError("上下文窗口 max_context_tokens 应为 4096~2000000")
            self.config.llm = LLMConfig(
                provider=provider,
                api_key=str(llm.get("api_key", "")).strip(),
                base_url=base_url, model=model,
                temperature=float(llm.get("temperature", 0.2)),
                max_tokens=max_tokens,
                max_context_tokens=max_context,
                enable_thinking=bool(llm.get("enable_thinking",
                                             self.config.llm.enable_thinking)),
                timeout_seconds=int(llm.get("timeout_seconds") or 300),
            )
        self.config.scheduler = SchedulerConfig(
            interval_seconds=interval, max_parallel_agents=parallel,
            run_once=self.config.scheduler.run_once,
        )
        # host/port 变更需重启生效，其余立即生效
        self.config.web = WebConfig(
            host=str(web.get("host") or self.config.web.host),
            port=port,
            open_browser=bool(web.get("open_browser", self.config.web.open_browser)),
        )
        # 日志保留天数：0 表示关闭自动归档与清理
        log_cfg = data.get("logging") or {}
        if "logging" in data:
            rd = int(log_cfg.get("retention_days", self.config.logging.retention_days))
            if not (0 <= rd <= 365):
                raise ValueError("日志保留天数应为 0~365（0 表示关闭自动归档清理）")
            self.config.logging.retention_days = rd

        self.scheduler.reload_config(self.config)
        self._apply_config(self.config)
        self._save_config(self.config)

        # 新保留天数立即生效：顺手做一次归档/清理
        if "logging" in data:
            from .logging_setup import rotate_and_cleanup

            log_path = (Path(self.log_path) if self.log_path
                        else Path(self.config.logging.file))
            try:
                msg = rotate_and_cleanup(log_path, rd)
                if msg:
                    log.info("日志维护：%s", msg)
            except OSError as e:
                log.warning("日志归档/清理失败：%s", e)

        log.info("Web 保存配置：model=%s interval=%ss", self.config.llm.model, interval)
        return {"ok": True, "message": "配置已保存并生效（看板 host/port 变更下次启动生效）"}


class WebHandler(BaseHTTPRequestHandler):
    state: AppState  # 由 make_server 注入
    # HTTP/1.1：启用 keep-alive（看板 5s 轮询少建连），前提是所有响应都带
    # Content-Length —— _json/_html 均已设置
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----

    def log_message(self, fmt, *args):  # noqa: A002 — 静默默认访问日志
        pass

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self._html()
        elif path == "/api/state":
            self._json(self.state.full_state())
        elif path == "/api/guide":
            try:
                self._json(self.state.get_guide(self._query_params()))
            except (ValueError, OSError) as e:
                self._json({"ok": False, "message": str(e)}, 400)
        elif path == "/api/task":
            try:
                self._json(self.state.get_task(self._query_params()))
            except (ValueError, OSError) as e:
                self._json({"ok": False, "message": str(e)}, 400)
        elif path == "/api/skill":
            try:
                self._json(self.state.get_skill(self._query_params()))
            except (ValueError, OSError) as e:
                self._json({"ok": False, "message": str(e)}, 400)
        elif path == "/api/skill/scan":
            try:
                self._json(self.state.scan_skill_sources())
            except Exception as e:  # noqa: BLE001 — 探针失败也要回 JSON，不能掐断连接
                log.exception("扫描 AI 工具技能目录失败")
                self._json({"ok": False, "message": f"扫描失败：{e}"}, 500)
        elif path == "/api/app-autostart":
            self._json(self.state.app_autostart_status())
        else:
            self._json({"error": "not found"}, 404)

    def _query_params(self) -> dict:
        from urllib.parse import parse_qs
        qs = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in qs.items()}

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_body()
            if path == "/api/scan":
                self._json(self.state.trigger_scan())
            elif path == "/api/task":
                self._json(self.state.create_or_update_task(body))
            elif path == "/api/task/status":
                self._json(self.state.set_task_status(body))
            elif path == "/api/task/delete-batch":
                self._json(self.state.delete_tasks_batch(body))
            elif path == "/api/task/clear":
                self._json(self.state.clear_tasks(body))
            elif path == "/api/task/delete":
                self._json(self.state.delete_task(body))
            elif path == "/api/models":
                try:
                    self._json(self.state.fetch_models(body))
                except (ValueError, OSError) as e:
                    self._json({"ok": False, "message": str(e)}, 400)
            elif path == "/api/llm/test":
                try:
                    self._json(self.state.test_model(body))
                except (ValueError, OSError) as e:
                    self._json({"ok": False, "message": str(e)}, 400)
            elif path == "/api/browse":
                self._json(self.state.browse_dir(body))
            elif path == "/api/probe":
                self._json(self.state.probe_project_api(body))
            elif path == "/api/mkdir":
                try:
                    self._json(self.state.make_dir(body))
                except (ValueError, OSError) as e:
                    self._json({"ok": False, "message": str(e)}, 400)
            elif path == "/api/project":
                self._json(self.state.save_project(body))
            elif path == "/api/project/delete":
                self._json(self.state.delete_project(body))
            elif path == "/api/project/open-folder":
                self._json(self.state.open_project_folder(body))
            elif path == "/api/project/issues-sync":
                self._json(self.state.sync_project_issues(body))
            elif path == "/api/guide":
                self._json(self.state.save_guide(body))
            elif path == "/api/skill":
                self._json(self.state.save_skill(body))
            elif path == "/api/skill/remove":
                self._json(self.state.remove_skills_api(body))
            elif path == "/api/skill/enabled":
                self._json(self.state.set_skill_enabled(body))
            elif path == "/api/skill/import":
                self._json(self.state.import_skill_api(body))
            elif path == "/api/skill/import-batch":
                self._json(self.state.import_skills_batch(body))
            elif path == "/api/skill/open-folder":
                self._json(self.state.open_skill_folder(body))
            elif path == "/api/app-autostart":
                self._json(self.state.set_app_autostart(body))
            elif path == "/api/config":
                self._json(self.state.save_settings(body))
            else:
                self._json({"error": "not found"}, 404)
        except (ValueError, OSError) as e:
            self._json({"ok": False, "message": str(e)}, 400)

    def _html(self) -> None:
        body = _load_page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(state: AppState, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (WebHandler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    # 请求线程随主线程退出：否则看板页面的轮询连接会让进程迟迟退不干净
    server.daemon_threads = True
    return server


def _load_page() -> str:
    """看板页面：编译进包的模块常量（windowed 冻结环境下 _MEI 数据文件
    可能 PermissionError，因此完全不依赖磁盘文件）。"""
    global _PAGE_CACHE
    if _PAGE_CACHE is None:
        from .dashboard_html import PAGE

        _PAGE_CACHE = PAGE
    return _PAGE_CACHE


_PAGE_CACHE: str | None = None


# 看板单页：无外部依赖，5 秒自动刷新 + 手动操作
