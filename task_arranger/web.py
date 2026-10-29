"""Web 可视化看板：看板页面 + JSON API，基于标准库 http.server。

API（均为 GET/POST JSON）：
  GET  /api/state        全量状态：调度器、各项目、任务列表、日志尾部
  POST /api/scan         手动触发一轮扫描（立即返回，后台执行）
  POST /api/task         创建/更新任务  {project, id, title, priority, status, description}
  POST /api/task/delete  删除任务       {project, id}
  POST /api/task/status  修改任务状态   {project, id, status}（blocked→open 重新入队等）

项目管理（可视化配置）：
  POST /api/browse       浏览本地目录    {path}，返回子目录列表（供新建项目选路径）
  POST /api/project      新建/修改项目   {name, path, tasks_dir, guide_file,
                                          max_tasks_per_cycle, max_agent_turns,
                                          allowed_branch, allowed_commands, enabled}
  POST /api/project/delete        删除项目（仅从配置移除，不动磁盘）
  GET/POST /api/guide    读取/保存项目 AGENT.md {project, content?}
  POST /api/config       保存模型/调度/看板配置 {llm:{...}, scheduler:{...}, web:{...}}
                         保存后写回 config.yaml 并热重载调度器
"""

from __future__ import annotations

import json
import re
import threading
import yaml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .agent import ProjectAgent
from .config import AppConfig, LLMConfig, ProjectConfig, SchedulerConfig, TaskStatus, WebConfig
from .logging_setup import get_logger
from .scheduler import Scheduler
from .task_model import Task, parse_task_file, priority_rank, scan_tasks

log = get_logger("web")

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
VALID_STATUS = ("open", "in_progress", "done", "blocked", "cancelled")
_NAME_RE = re.compile(r"^[\w][\w\-]*$")

# 浏览目录时忽略的噪音项
_DIR_IGNORE = {"$Recycle.Bin", "System Volume Information", "Windows", "AppData",
               "node_modules", ".git", ".venv", "__pycache__", ".worktree-arranger"}


def _default_guide(project_name: str) -> str:
    """新项目的 AGENT.md 初始内容（与内置模板一致的精简版）。"""
    return f"""# {project_name} 开发指引

> 由任务编排看板生成，请按项目实际情况修改。Agent 执行任务时会读取本文件作为行为准则。

## 项目简介

- 技术栈：
- 本地运行：
- 测试命令：

## 编码规范

- 遵循项目现有代码风格，不引入新的格式化风格。
- 新增第三方依赖前必须评估必要性；能不引就不引。

## 工作流程要求

1. **先读后改**：动手前先通读相关模块，确认理解了现有实现。
2. **小步提交**：只做当前任务要求的事，不顺手重构无关代码。
3. **必须验证**：修改后运行测试/构建；无法验证时在 notes 里说明。
4. **提交信息**：遵循 Conventional Commits（feat: / fix: / docs: / refactor: / test: / chore:）。

## 禁止事项

- 不要修改 `tasks/` 目录下的任何文件。
- 不要改 CI/CD 配置、依赖锁文件、密钥文件；如任务需要，返回 blocked 并说明。
- 不要在代码里硬编码密钥、密码、内网地址。

## 遇到这些情况直接 blocked

- 任务描述含糊，无法从代码中确定意图。
- 需要人工决策（产品行为、UI 取舍、数据库结构变更）。
- 验证不通过且两次尝试后仍失败。
"""


def read_log_tail(path: str, max_bytes: int = 20000) -> str:
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

    def __init__(self, config: AppConfig, scheduler: Scheduler) -> None:
        self.config = config
        self.scheduler = scheduler
        self.agents: dict[str, ProjectAgent] = {
            a.project.name: a for a in scheduler.agents
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
                "is_git_repo": agent.executor.is_git_repo,
                "guide_file": str(p.path / p.guide_file),
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
            "projects": projects,
            "log_tail": read_log_tail(self.config.logging.file),
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
        path.write_text("\n".join(fm) + "\n\n" + description + "\n", encoding="utf-8")
        log.info("Web 创建/更新任务 %s/%s [%s]", project, task_id, priority)
        return {"ok": True, "file": str(path)}

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
        return ProjectConfig(
            name=name, path=path, enabled=bool(data.get("enabled", True)),
            tasks_dir=tasks_dir, guide_file=guide_file,
            max_tasks_per_cycle=max_tasks, max_agent_turns=max_turns,
            allowed_commands=allowed, allowed_branch=branch,
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
                "timeout_seconds": cfg.llm.timeout_seconds,
            },
            "scheduler": {
                "interval_seconds": cfg.scheduler.interval_seconds,
                "max_parallel_agents": cfg.scheduler.max_parallel_agents,
                "run_once": cfg.scheduler.run_once,
            },
            "logging": {"level": cfg.logging.level, "file": cfg.logging.file},
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
                }
                for p in cfg.projects
            ],
        }
        src.write_text(
            "# task-arranger 配置（由看板自动保存）\n"
            + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _apply_config(self, new_cfg: AppConfig) -> None:
        self.config = new_cfg
        self.agents = {a.project.name: a for a in self.scheduler.agents}

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

        # 新项目：初始化任务目录与 AGENT.md（已存在则不动）
        created = []
        tasks_dir = proj.path / proj.tasks_dir
        if not tasks_dir.exists():
            tasks_dir.mkdir(parents=True, exist_ok=True)
            created.append(f"{proj.tasks_dir}/")
        guide = proj.path / proj.guide_file
        if not guide.exists():
            guide.write_text(_default_guide(proj.name), encoding="utf-8")
            created.append(proj.guide_file)

        self.scheduler.reload_config(self.config)
        self._apply_config(self.config)
        self._save_config(self.config)
        log.info("Web %s项目 %s（%s）", action, proj.name, proj.path)
        return {"ok": True, "created": created, "message":
                f"已{action}项目 {proj.name}" + (f"，初始化了 {'、'.join(created)}" if created else "")}

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

    # ---- AGENT.md 编辑 ----

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

    # ---- 模型/调度/看板配置 ----

    def save_settings(self, data: dict) -> dict:
        llm = data.get("llm") or {}
        sched = data.get("scheduler") or {}
        web = data.get("web") or {}

        provider = str(llm.get("provider", self.config.llm.provider)).strip().lower()
        if provider not in ("openai", "anthropic"):
            raise ValueError(f"provider 不合法：{provider}（openai / anthropic）")
        model = str(llm.get("model", "")).strip()
        if not model:
            raise ValueError("model 不能为空")
        base_url = str(llm.get("base_url", "")).strip()
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http(s):// 开头")
        interval = int(sched.get("interval_seconds") or 300)
        if not (30 <= interval <= 86400):
            raise ValueError("扫描间隔应为 30~86400 秒")
        parallel = int(sched.get("max_parallel_agents") or 1)
        if not (1 <= parallel <= 16):
            raise ValueError("并行 Agent 数应为 1~16")
        port = int(web.get("port") or self.config.web.port)
        if not (1 <= port <= 65535):
            raise ValueError("端口应为 1~65535")

        self.config.llm = LLMConfig(
            provider=provider,
            api_key=str(llm.get("api_key", "")).strip(),
            base_url=base_url, model=model,
            temperature=float(llm.get("temperature", 0.2)),
            max_tokens=int(llm.get("max_tokens") or 8192),
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

        self.scheduler.reload_config(self.config)
        self._apply_config(self.config)
        self._save_config(self.config)
        log.info("Web 保存配置：model=%s interval=%ss", self.config.llm.model, interval)
        return {"ok": True, "message": "配置已保存并生效（看板 host/port 变更下次启动生效）"}


class WebHandler(BaseHTTPRequestHandler):
    state: AppState  # 由 make_server 注入

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
            elif path == "/api/task/delete":
                self._json(self.state.delete_task(body))
            elif path == "/api/browse":
                self._json(self.state.browse_dir(body))
            elif path == "/api/project":
                self._json(self.state.save_project(body))
            elif path == "/api/project/delete":
                self._json(self.state.delete_project(body))
            elif path == "/api/guide":
                self._json(self.state.save_guide(body))
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
