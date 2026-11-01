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
                                          tools 可选：项目级工具开关覆盖 {工具名:bool}，
                                          null=跟随全局,
                                          permission_mode 可选：项目级访问权限覆盖
                                          （ask_on_change/autonomous/full_access），
                                          null=跟随全局,
                                          stack/run/test 可选：写入 AGENT.md 的 Project 节}
  POST /api/project/delete        删除项目（仅从配置移除，不动磁盘）
  POST /api/project/open-folder   用系统文件管理器打开项目根目录 {name}
  GET/POST /api/guide    读取/保存项目 AGENT.md {project, content?}
  POST /api/config       保存模型/调度/看板配置 {llm:{...}, scheduler:{...}, web:{...},
                         tools:{工具名:bool}, permissions:{mode, timeout_minutes}}
                         （分块保存；tools 为全局 Agent 工具开关，permissions 为
                         访问权限模式；非法值报 400）
                         保存后写回 config.yaml 并热重载调度器

人工审批（访问权限模式为 ask_on_change / autonomous 时，敏感工具调用挂起等待）：
  POST /api/approval     响应一条挂起的审批 {id, approved, always?}
                         （always=true 且批准时，该工具在本任务内不再询问；
                         挂起列表经 GET /api/state 的 approvals 字段曝光；
                         超时 permissions.timeout_minutes 自动拒绝）

登录鉴权（web.auth_password 非空时，除白名单外所有页面/API 需先登录）：
  GET  /api/auth         查询鉴权状态 {enabled, authed}（凭会话 Cookie 判定）
  POST /api/login        登录 {password}；成功下发 HttpOnly 会话 Cookie（7 天有效）
  POST /api/logout       退出登录；服务端注销当前会话并清除 Cookie

程序自更新（零 UAC 静默换名安装，详见 orcsy/updater.py）：
  POST /api/update/check   检查 Gitee Release 是否有新版本
  POST /api/update/apply   在线更新：下载 zip 更新包后走本地安装引擎
  POST /api/update/install 手动导入本地 zip 更新包 {path}，后台线程静默安装
                           （校验后立即返回；成功自动重启，出错写安装日志并自动打开）

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
import hmac
import os
import secrets
import tempfile
import time
import re
import subprocess
import sys
import threading
import yaml
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from . import skills as skills_lib
from .agent import ProjectAgent
from .approvals import get_manager
from .config import (AppConfig, LLMConfig, PermissionsConfig, ProjectConfig,
                     SchedulerConfig, TaskStatus, ToolsConfig, UpdateConfig,
                     WebConfig, normalize_llm_override, resolve_llm,
                     resolve_permission_mode, resolve_tools,
                     validate_permission_mode)
from .issues import IssueError, normalize_repo, sync_issues
from . import updater
from .llm import LLMError, list_models, test_connection
from .logging_setup import get_logger
from .scheduler import Scheduler
from .skills import Skill
from .task_model import Task, parse_task_file, priority_rank, scan_tasks
from .tools import TOOLS, validate_tools_map

log = get_logger("web")

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
VALID_STATUS = ("open", "in_progress", "done", "blocked", "cancelled")
_NAME_RE = re.compile(r"^[\w][\w\-]*$")

# 登录会话：Cookie 名与有效期（秒）；会话仅存内存，进程重启后需重新登录
AUTH_COOKIE = "orcsy_session"
SESSION_TTL = 7 * 24 * 3600

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
                                 skills_base=self.skills_dir,
                                 tools=resolve_tools(config.tools, p.tools),
                                 permission_mode=resolve_permission_mode(config.permissions, p),
                                 permission_timeout_minutes=config.permissions.timeout_minutes)
            for p in config.projects
        }
        # 程序自更新状态机：idle / checking / latest / available / downloading / restarting / error
        self.update_state: dict = {"phase": "idle"}
        # 登录会话：token -> 过期时间戳（内存态，进程重启即全部失效）
        self._auth_tokens: dict[str, float] = {}
        self._auth_lock = threading.Lock()
        # 安装日志：<配置目录>/logs/install.log，升级全程留痕（仅出错时自动打开）
        self.install_log = updater.install_log_path(config)
        # 上次升级结果（看护脚本写、启动时消费）；一次曝光后清空
        self.update_result: dict | None = None
        self._consume_update_result()
        if config.update.auto_check and config.update.repo:
            threading.Thread(target=self._auto_update_check, daemon=True,
                             name="update-check").start()

    def _auto_update_check(self) -> None:
        """启动后静默检查一次更新：任何失败都只记日志，不打扰使用。"""
        try:
            self.update_check({})
        except Exception:  # noqa: BLE001 — 自检失败不影响启动
            log.info("启动自动检查更新失败（可忽略，可在设置页手动重试）")

    # ---- 登录鉴权 ----

    @property
    def auth_enabled(self) -> bool:
        """配置了登录密码即开启鉴权；留空 = 关闭（本机使用零打扰）。"""
        return bool(self.config.web.auth_password)

    def login(self, password: str) -> dict:
        """校验登录密码；成功颁发会话 token（由 handler 负责下发 Cookie）。"""
        if not self.auth_enabled:
            return {"ok": True, "token": None}
        if not hmac.compare_digest(str(password or ""), self.config.web.auth_password):
            time.sleep(0.8)  # 失败延迟：抬高暴力破解成本
            return {"ok": False, "message": "密码错误"}
        token = secrets.token_urlsafe(32)
        with self._auth_lock:
            self._prune_sessions()
            self._auth_tokens[token] = time.time() + SESSION_TTL
        return {"ok": True, "token": token}

    def logout(self, token: str | None) -> dict:
        with self._auth_lock:
            self._auth_tokens.pop(token or "", None)
        return {"ok": True}

    def token_valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._auth_lock:
            expiry = self._auth_tokens.get(token)
            if expiry is None:
                return False
            if expiry < time.time():
                del self._auth_tokens[token]
                return False
            return True

    def auth_status(self, token: str | None) -> dict:
        enabled = self.auth_enabled
        return {"ok": True, "enabled": enabled,
                "authed": (not enabled) or self.token_valid(token)}

    def _prune_sessions(self) -> None:
        """清掉过期会话（调用方须持有 _auth_lock）。"""
        now = time.time()
        self._auth_tokens = {t: e for t, e in self._auth_tokens.items() if e >= now}

    def _invalidate_sessions(self) -> None:
        """密码变更/清除后注销全部会话：旧 Cookie 立即失效，需用新密码重登。"""
        with self._auth_lock:
            self._auth_tokens.clear()

    # ---- 人工审批 ----

    def approval_respond(self, data: dict) -> dict:
        """看板响应一条挂起的审批请求（批准/拒绝，可带「本任务内始终允许」）。"""
        rid = str(data.get("id") or "").strip()
        if not rid:
            raise ValueError("缺少审批请求 id")
        ok = get_manager().respond(rid, bool(data.get("approved")),
                                   always=bool(data.get("always")))
        if not ok:
            raise ValueError("审批请求不存在或已处理（可能已被响应或超时自动拒绝）")
        return {"ok": True}

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
                # 项目级工具开关覆盖；null = 跟随全局（前端显示「跟随全局」态）
                "tools": p.tools or None,
                # 项目级访问权限覆盖（原始值）+ 当前生效值（前端展示用）
                "permission_mode": p.permission_mode,
                "permission_mode_effective": resolve_permission_mode(
                    self.config.permissions, p),
                # 定时拉取 Issues：repo.auto_pull_interval 启用时附最近一次自动拉取状态
                "issue_auto_pull": (
                    {**(self.scheduler.issue_pull_status.get(name) or {}),
                     "interval": p.repo["auto_pull_interval"]}
                    if (p.repo or {}).get("auto_pull_interval") else None
                ),
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
            "update": self._healed_update_state(),
            # 上次升级结果：一次性曝光给前端弹窗（读后即清，避免轮询反复弹）
            "update_result": self._pop_update_result(),
            "scheduler_cfg": {
                "interval_seconds": self.config.scheduler.interval_seconds,
                "max_parallel_agents": self.config.scheduler.max_parallel_agents,
            },
            "web_cfg": {
                "host": self.config.web.host,
                "port": self.config.web.port,
                "open_browser": self.config.web.open_browser,
                # 只回显开关状态，绝不回显密码本身
                "auth_enabled": self.auth_enabled,
            },
            "update_cfg": {
                "repo": self.config.update.repo,
                "token": self.config.update.token,
                "auto_check": self.config.update.auto_check,
            },
            "logging": {"retention_days": self.config.logging.retention_days},
            # 工具开关：全局生效 map + 工具目录（名称/描述取自 TOOLS 注册表）。
            # finish 也进目录——前端把它渲染成「常开且禁用」的展示行，
            # validate_tools_map 会剥掉任何 finish 条目，配置语义不受影响
            "tools_cfg": dict(self.config.tools.enabled),
            "tools_catalog": [
                {"name": name, "description": desc}
                for name, (desc, _, _) in TOOLS.items()
            ],
            # 访问权限：全局配置 + 当前挂起的审批请求（看板 5s 轮询刷新，
            # 顶栏「待审批」徽标与审批面板都读这里）
            "permissions_cfg": {
                "mode": self.config.permissions.mode,
                "timeout_minutes": self.config.permissions.timeout_minutes,
            },
            "approvals": get_manager().pending(),
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
        """列出指定目录的子目录与文件，供前端逐级选择本地路径/文件。

        dirs 恒返回（目录选择器用）；files 仅在选择文件模式时有意义，
        由前端按需展示。两者合计上限 300 项。
        """
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
                    "dirs": roots + [str(home / "Documents"), str(home / "Desktop")],
                    "files": []}

        dirs, files = [], []
        try:
            for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if child.is_dir():
                    if child.name not in _DIR_IGNORE and not child.name.startswith("$"):
                        dirs.append(str(child))
                else:
                    files.append(str(child))
                if len(dirs) + len(files) >= 300:
                    break
        except PermissionError:
            return {"ok": True, "path": str(p), "parent": None, "dirs": [],
                    "files": [], "note": "无权限读取该目录"}
        parent = str(p.parent) if p.parent != p else None
        return {"ok": True, "path": str(p), "parent": parent, "dirs": dirs, "files": files}

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
        # 项目级工具开关覆盖：字典则校验（未知工具名/禁 finish/非 bool 立刻报错），
        # 显式 null 或空字典=清除覆盖（跟随全局）
        tools_raw = data.get("tools")
        if tools_raw is not None and not isinstance(tools_raw, dict):
            raise ValueError("tools 覆盖必须是「工具名: true/false」字典或 null")
        tools = validate_tools_map(tools_raw, context=f"项目 {name} 的 tools") or None
        # 项目级访问权限模式覆盖：合法枚举或 null/缺失（跟随全局）
        perm_raw = data.get("permission_mode")
        perm = (validate_permission_mode(perm_raw,
                                         context=f"项目 {name} 的 permission_mode")
                if perm_raw else None)
        return ProjectConfig(
            name=name, path=path, enabled=bool(data.get("enabled", True)),
            tasks_dir=tasks_dir, guide_file=guide_file,
            max_tasks_per_cycle=max_tasks, max_agent_turns=max_turns,
            allowed_commands=allowed, allowed_branch=branch, llm=llm, repo=repo,
            tools=tools, permission_mode=perm,
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
                    "open_browser": cfg.web.open_browser,
                    "auth_password": cfg.web.auth_password},
            # 全局工具开关：只落盘显式关闭/开启的项（空 dict = 全默认启用）
            "tools": dict(cfg.tools.enabled),
            "permissions": {"mode": cfg.permissions.mode,
                            "timeout_minutes": cfg.permissions.timeout_minutes},
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
                    **({"tools": p.tools} if p.tools else {}),
                    **({"permission_mode": p.permission_mode} if p.permission_mode else {}),
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
                                            skills_base=self.skills_dir,
                                            tools=resolve_tools(new_cfg.tools, p.tools),
                                            permission_mode=resolve_permission_mode(
                                                new_cfg.permissions, p),
                                            permission_timeout_minutes=new_cfg.permissions.timeout_minutes)
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

    # ---- 程序自更新 ----

    def _set_update_state(self, state: dict) -> None:
        """记录更新状态机快照；带时间戳供超时自愈判断。"""
        self.update_state = {**state, "ts": time.time()}

    def _healed_update_state(self) -> dict:
        """读侧超时自愈：进行中状态超过 120 秒（如静默安装被外部打断、进程
        异常残留）时转为 error，避免看板按钮永久卡在「安装中」。
        附带安装日志尾部，静默失败可查。"""
        upd = dict(self.update_state)
        if upd.get("phase") in ("downloading", "applying", "restarting") \
                and time.time() - upd.get("ts", 0) > 120:
            tail = updater.read_install_log(self.install_log)[-800:]
            upd = {"phase": "error",
                   "message": "更新流程超时未完成——请重试，或使用设置页的手动导入安装包",
                   "install_log_tail": tail}
        return upd

    def _pop_update_result(self) -> dict | None:
        res, self.update_result = self.update_result, None
        return res

    def _consume_update_result(self) -> None:
        """启动时消费上次升级的结果标记（仅 Windows 安装版）。

        成功静默（不打开日志、不打扰）；失败如实弹出并自动打开安装日志文档。
        看护脚本可能在拉起新版后才补写结果，因此标记允许存在少量延迟。
        """
        if not updater.can_apply():
            return  # 源码/Docker 运行不参与换名安装，不消费标记
        res = updater.read_update_result()
        if res is None:
            return
        updater.clear_update_result()
        if res.get("ok"):
            updater.append_install_log(self.install_log, "上次升级成功，新版本已启动")
            return
        reason = str(res.get("reason") or "unknown")
        msg = updater.result_message(reason)
        updater.append_install_log(self.install_log, f"上次升级未完成：{msg}")
        updater.open_install_log(self.install_log)  # 出错才自动打开日志文档
        self.update_result = {"ok": False, "message": msg}
        self._set_update_state({"phase": "error", "message": msg,
                                "install_log_tail":
                                    updater.read_install_log(self.install_log)})

    def update_check(self, data: dict) -> dict:
        """检查 Gitee Release 是否有新版本；结果写入 update_state 供头部展示。"""
        self._set_update_state({"phase": "checking"})
        try:
            info = updater.check_update(
                {"repo": self.config.update.repo, "token": self.config.update.token},
                current_version=__version__)
        except updater.UpdateError as e:
            self._set_update_state({"phase": "error", "message": str(e)})
            raise ValueError(str(e)) from e
        phase = "available" if info["has_update"] else "latest"
        self._set_update_state({"phase": phase, **info})
        log.info("检查更新：当前 %s，最新 %s%s", __version__, info["latest"],
                 "（可更新）" if info["has_update"] else "（已是最新）")
        return {"ok": True, "phase": phase, **info}

    def _reject_if_update_running(self) -> None:
        """防重入：更新周期进行中（下载/安装/重启，120 秒内）拒绝再次触发，
        避免重复点击反复重置状态时间戳、导致看板永久卡「安装中」。"""
        st = self.update_state
        if st.get("phase") in ("downloading", "applying", "restarting") \
                and time.time() - st.get("ts", 0) < 120:
            raise ValueError("已有一次更新正在进行（正在静默安装），请稍候再试；"
                             "若长时间未完成请刷新页面后重试")

    def _start_install_thread(self, package: Path) -> dict:
        """校验通过后启动后台安装线程并立即返回（可写目录零 UAC；系统目录提权兜底）。

        响应送达后线程开始安装：目录可写走换名落位；不可写（如 Program Files）
        走提权兜底——安装中会弹一次 UAC 授权窗口，message 里提前告知。
        成功则旧进程退出、看护拉起新版，前端遮罩等待 /api/state 回归后自动
        刷新。失败在线程里写日志、弹窗并自动打开安装日志（成功不打开）。
        """
        self._set_update_state({"phase": "applying", "asset_name": package.name,
                                "target_version":
                                    updater.detect_installer_version(package.name)})
        # 提前探测可写性只为提示语准确；实际分流在 run_local_install 里做
        elevated = not updater.is_writable(Path(sys.executable).parent)
        threading.Thread(target=self._install_worker, args=(package,),
                         daemon=True, name="update-install").start()
        log.info("Web 触发静默升级：更新包 %s（后台线程%s）", package.name,
                 "提权安装，将弹一次 UAC" if elevated else "换名落位，零 UAC")
        message = ("已开始安装：安装目录需要管理员权限，稍后将弹出一次 UAC 授权窗口，"
                   "请点「是」继续（取消则不更新，程序照常运行）" if elevated else
                   "已开始静默升级，应用将短暂离线并自动重启")
        return {"ok": True, "phase": "applying", "elevated": elevated,
                "target": updater.detect_installer_version(package.name),
                "message": message}

    def _install_worker(self, package: Path) -> None:
        """后台安装线程：换名落位 → 成功退出进程由看护重启；失败弹窗+开日志。"""
        try:
            updater.run_local_install(package, Path(sys.executable).parent,
                                      self.install_log)
        except updater.UpdateError as e:
            # 要求：错误绝不静默吞掉——写安装日志 + 自动打开日志 + 前端弹窗
            updater.append_install_log(self.install_log, f"静默升级失败：{e}")
            updater.open_install_log(self.install_log)  # 仅出错时打开
            self._set_update_state({
                "phase": "error", "message": str(e),
                "install_log_tail": updater.read_install_log(self.install_log)})
            return
        self._set_update_state({"phase": "restarting", "asset_name": package.name})
        # 换名落位已完成：收尾后退出本进程，看护脚本等端口释放即拉起新版。
        # 不走 atexit：os._exit 前手动停调度/收子进程；托盘图标随进程消失。
        self._exit_for_restart()

    def _exit_for_restart(self) -> None:
        try:
            self.scheduler.stop()
        except Exception:  # noqa: BLE001 — 退出路径不因清理失败而留下活进程
            pass
        try:
            from .tools import kill_running_children

            kill_running_children()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)  # 让在途 HTTP 响应发完
        os._exit(0)

    def update_apply(self, data: dict) -> dict:
        """应用更新（在线）：下载 check_update 确认的 zip 更新包后走本地安装引擎。

        成功返回 applying——下载完成即转后台安装线程，进程随后由看护重启。
        """
        self._reject_if_update_running()
        if not updater.can_apply():
            raise ValueError(updater.apply_gate_message())
        dl_url = self.update_state.get("download_url")
        if not dl_url:
            raise ValueError("请先检查更新（在线更新需要先确认可用的新版本）")
        try:
            self._set_update_state({"phase": "downloading", "percent": 0})
            dest = Path(tempfile.gettempdir())
            name = self.update_state.get("asset_name") or "orcsy-update-download.zip"
            path = updater.download_installer(
                dl_url, dest, progress_cb=lambda pct: self.update_state.update(
                    {"phase": "downloading", "percent": pct}),
                filename=name)
            return self._start_install_thread(path)
        except updater.UpdateError as e:
            self._set_update_state({"phase": "error", "message": str(e)})
            raise ValueError(str(e)) from e

    def update_install(self, data: dict) -> dict:
        """手动导入本地 zip 更新包：校验后后台线程静默安装（零 UAC）。

        同版本覆盖、升/降级均允许（确认弹窗已展示版本对照）；装回当前程序
        所在目录。成功自动重启并刷新页面；出错写安装日志并自动打开日志文档。
        """
        self._reject_if_update_running()
        if not updater.can_apply():
            raise ValueError(updater.apply_gate_message())
        raw = str(data.get("path") or "").strip()
        if not raw:
            raise ValueError("请先选择更新包文件（orcsy-update-*.zip）")
        package = Path(raw)
        try:
            info = updater.validate_package(package)
        except updater.UpdateError as e:
            raise ValueError(str(e)) from e
        log.info("Web 手动导入更新包：%s（版本 %s，同/异版本均允许）",
                 package.name, info["version"] or "未知")
        return self._start_install_thread(package)

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
        # auth_password 三态：键缺失=保持原值；空字符串=清除（关闭鉴权）；非空=设置新密码
        auth_password = self.config.web.auth_password
        if "auth_password" in web:
            new_pwd = str(web.get("auth_password") or "")
            if new_pwd != auth_password:
                auth_password = new_pwd
                self._invalidate_sessions()  # 密码变更后旧会话全部作废
        self.config.web = WebConfig(
            host=str(web.get("host") or self.config.web.host),
            port=port,
            open_browser=bool(web.get("open_browser", self.config.web.open_browser)),
            auth_password=auth_password,
        )
        # 日志保留天数：0 表示关闭自动归档与清理
        log_cfg = data.get("logging") or {}
        if "logging" in data:
            rd = int(log_cfg.get("retention_days", self.config.logging.retention_days))
            if not (0 <= rd <= 365):
                raise ValueError("日志保留天数应为 0~365（0 表示关闭自动归档清理）")
            self.config.logging.retention_days = rd

        # 软件更新源：分块保存，只带 update 的请求不触碰其它配置
        if "update" in data:
            upd = data.get("update") or {}
            repo = str(upd.get("repo") or "").strip()
            if repo and not repo.startswith(("http://", "https://")):
                raise ValueError("更新源仓库 URL 必须以 http(s):// 开头")
            self.config.update = UpdateConfig(
                repo=repo,
                token=str(upd.get("token", self.config.update.token)),
                auto_check=bool(upd.get("auto_check", self.config.update.auto_check)),
            )

        # Agent 工具开关：分块保存，只带 tools 的请求生效；
        # 未知工具名/禁用 finish/非布尔值由 validate_tools_map 直接报错（400 透传看板）
        if "tools" in data:
            self.config.tools = ToolsConfig(
                enabled=validate_tools_map(data.get("tools"), context="tools"))

        # 访问权限：分块保存；模式枚举与超时范围校验（400 透传看板）
        if "permissions" in data:
            perm = data.get("permissions") or {}
            mode = validate_permission_mode(
                perm.get("mode", self.config.permissions.mode))
            # 不能用 `or` 兜底：0 是非法值该报错，而不是被当成「未填」回退现值
            raw_timeout = perm.get("timeout_minutes")
            if raw_timeout is None:
                timeout = self.config.permissions.timeout_minutes
            else:
                try:
                    timeout = int(raw_timeout)
                except (TypeError, ValueError):
                    raise ValueError("审批超时必须是整数（分钟）") from None
            if not (1 <= timeout <= 1440):
                raise ValueError("审批超时应为 1~1440 分钟")
            self.config.permissions = PermissionsConfig(mode=mode, timeout_minutes=timeout)

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

    def _json(self, obj: dict, code: int = 200,
              extra_headers: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra_headers or ():
            self.send_header(name, value)
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

    # ---- 鉴权守卫 ----

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar: SimpleCookie = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 — Cookie 头畸形按未登录处理
            return None
        morsel = jar.get(AUTH_COOKIE)
        return morsel.value if morsel else None

    def _authorized(self) -> bool:
        return (not self.state.auth_enabled
                or self.state.token_valid(self._cookie_token()))

    def _deny(self) -> None:
        self._json({"ok": False, "unauthorized": True,
                    "message": "未登录或会话已过期"}, 401)

    def _handle_login(self, body: dict) -> None:
        result = self.state.login(str(body.get("password") or ""))
        if not result.get("ok"):
            return self._json(
                {"ok": False, "message": result.get("message", "密码错误")}, 401)
        token = result.get("token")
        if token:
            cookie = (f"{AUTH_COOKIE}={token}; HttpOnly; Path=/; "
                      f"Max-Age={SESSION_TTL}; SameSite=Lax")
            self._json({"ok": True}, extra_headers=[("Set-Cookie", cookie)])
        else:
            self._json({"ok": True})  # 鉴权未开启：无需会话

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        # 鉴权守卫：SPA 壳（/、/index.html）与 /api/auth 放行，登录界面由页内接管
        if path not in ("/", "/index.html", "/api/auth") and not self._authorized():
            return self._deny()
        if path == "/" or path == "/index.html":
            self._html()
        elif path == "/api/auth":
            self._json(self.state.auth_status(self._cookie_token()))
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
            # 先读净请求体再鉴权：401 拒掉时若留下未读的 body，keep-alive
            # 连接上的下一个请求会把残留字节当请求行解析（协议失步 → 连接重置）
            body = self._read_body()
            # 鉴权守卫：仅登录接口放行（401 走 _json，keep-alive 的 Content-Length 不缺）
            if path != "/api/login" and not self._authorized():
                return self._deny()
            if path == "/api/login":
                self._handle_login(body)
            elif path == "/api/logout":
                clear = f"{AUTH_COOKIE}=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax"
                self._json(self.state.logout(self._cookie_token()),
                           extra_headers=[("Set-Cookie", clear)])
            elif path == "/api/scan":
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
            elif path == "/api/approval":
                self._json(self.state.approval_respond(body))
            elif path == "/api/update/check":
                self._json(self.state.update_check(body))
            elif path == "/api/update/apply":
                self._json(self.state.update_apply(body))
            elif path == "/api/update/install":
                self._json(self.state.update_install(body))
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
