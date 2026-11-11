"""Web 可视化看板：看板页面 + JSON API，基于标准库 http.server。

API（均为 GET/POST JSON）：
  GET  /api/state        全量状态：调度器、各项目、任务列表、日志尾部
  POST /api/scan         手动触发一轮扫描（立即返回，后台执行）
  GET  /api/task         单个任务完整内容 ?project=&id=（编辑弹窗用，不截断）
  POST /api/task         创建/更新任务  {project, id, title, priority, status, description}
  POST /api/task/delete  删除任务       {project, id}
  POST /api/task/status  修改任务状态   {project, id, status}（blocked→open 重新入队等）
  POST /api/task/reorder 看板拖拽排序落盘 {project, ids:[拖拽后的完整任务 id 顺序]}
                         （重写 frontmatter 的 order: N；仅影响显示，Agent 仍按优先级领取）
  POST /api/task/stop    强制停止执行中的任务 {project, id}（工具边界收尾为 cancelled，可重新入队）
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

镜像源管理（内置+用户添加源，HTTP 测速选最快；详见 orcsy/mirrors.py）：
  POST /api/mirror/test   测速 {category?}（缺省测全部类别），结果存边车并返回
  POST /api/mirror/apply  把代理类最快源写进 env.vars（GOPROXY/PIP_INDEX_URL/
                          npm_config_registry），需先有测速成功的结果

登录鉴权（web.auth_password 非空时，除白名单外所有页面/API 需先登录）：
  GET  /api/auth         查询鉴权状态 {enabled, authed}（凭会话 Cookie 判定）
  POST /api/login        登录 {password}；成功下发 HttpOnly 会话 Cookie（7 天有效）
  POST /api/logout       退出登录；服务端注销当前会话并清除 Cookie

程序自更新（零 UAC 静默换名安装，详见 orcsy/updater.py）：
  POST /api/update/check   检查 Gitee Release 是否有新版本
  POST /api/update/apply   在线更新：下载 zip 更新包后走本地安装引擎
  POST /api/update/install 手动导入本地 zip 更新包 {path}，后台线程静默安装
                           （校验后立即返回；成功自动重启，出错写安装日志并自动打开）

程序手动重启（看护脚本等本进程退出后拉起当前 exe，仅 Windows 安装版）：
  POST /api/app-restart    重启程序：看板右上角「重启」按钮 / 托盘菜单「重启程序」

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
import shutil
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
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from . import __version__
from . import skills as skills_lib
from .agent import ProjectAgent
from .approvals import get_manager, MUTATING_TOOLS, RISKY_TOOLS
from .auth import hash_password, is_password_hashed, verify_password
from .concurrency import configure_limiter, snapshot_pools
from .config import (BUILT_IN_ROLES, ROLE_PROMPT_TEMPLATES, AgentProfileConfig, AppConfig, CommandsConfig,
                     EnvConfig, GitConfig, LLMConfig,
                     LOCAL_DEBUG_BROWSERS, LocalDebugConfig, MaskingConfig,
                     MAX_MASKING_PATTERNS, MAX_MASKING_PATTERN_LEN,
                     MirrorsConfig, NotifyConfig, PermissionsConfig,
                     ProjectConfig, ProjectTeamConfig, ReviewConfig, SchedulerConfig,
                     SubagentConfig,
                     TaskStatus, TeamMemberConfig, ToolsConfig, UpdateConfig, WebConfig,
                     assert_review_model_distinct, normalize_llm_override,
                     normalize_review_llm, resolve_commands, resolve_commit_and_merge, resolve_llm,
                     resolve_permission_mode, resolve_project_team_members, resolve_review_llm,
                     resolve_review_rounds, resolve_tools, resolve_worktree_isolation,
                     validate_agent_profiles, validate_allowed_hosts,
                     validate_env_vars, validate_masking_patterns, validate_permission_mode,
                     validate_project_team,
                     validate_web_security, review_enabled_for)
from .config import _coerce_llm as _coerce_llm_cfg
from .executor import WORKTREE_DIR
from .git_ops import Git, GitError
from .issues import IssueError, normalize_repo, sync_issues
from . import notify
from . import masking
from . import updater
from .llm import LLMError, list_models, test_connection
from .logging_setup import get_logger
from .mirrors import (CATEGORIES, CATEGORY_KEYS, mirror_runtime_for,
                      validate_mirrors)
from .scheduler import Scheduler
from .skills import Skill
from .task_model import (Task, _next_requeue_seq, collect_task_statuses,
                         detect_dependency_cycle, evaluate_task_dependencies,
                         parse_task_file, priority_rank, scan_tasks,
                         set_task_file_order)
from .tools import (TOOLS, TOOL_GROUPS, DEFAULT_OFF_TOOLS, DEFAULT_ALLOWED_COMMANDS,
                    COMMAND_CATEGORIES, TOOL_CATEGORIES,
                    validate_commands_map, validate_tools_map)
from .usage import get_usage_store, init_usage_store

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


def mask_secret(value: str) -> str:
    """设置页展示用的掩码：保留首尾各 4 位可辨认，其余打码；短值/空值全打码。

    密钥/令牌绝不随 /api/state 明文外发（安全审计 2026-11-04 H2）：已配置的
    事实 + 首尾可辨认片段足够设置页提示「留空保持不变」。
    """
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 12:
        return "••••••••"
    return f"{v[:4]}…{v[-4:]}"


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

    # /api/login 限速参数（L1）：同 IP 滑窗内失败达上限即锁定，锁定期间直接
    # 拒绝不做密码比对。类属性便于测试缩短窗口；不采信 X-Forwarded-For（本
    # 工具部署形态是直连，该头可伪造）
    LOGIN_WINDOW = 600.0      # 失败计数滑窗：10 分钟
    LOGIN_MAX_FAILS = 5       # 窗口内允许的失败次数
    LOGIN_LOCKOUT = 900.0     # 触发后的锁定时长：15 分钟
    LOGIN_FAIL_DELAY = 0.8    # 每次失败/锁定响应的延迟：抬高暴力破解成本

    def __init__(self, config: AppConfig, scheduler: Scheduler,
                 log_path: str | None = None) -> None:
        self.config = config
        self.scheduler = scheduler
        # 解析后的日志绝对路径（cmd_web 传入）；不传则退回原始配置值
        # ——config.logging.file 常是相对路径，直接按进程 CWD 读会读错位置
        self.log_path = log_path
        # 技能库根目录（配置文件同级 skills/），运行期不变
        self.skills_dir = skills_lib.skills_base(config)
        # 用量台账锚定到配置目录（Scheduler 若已初始化则同路径幂等）
        init_usage_store(config)
        # 镜像源运行时（测速边车与 config.yaml 同目录；与 Scheduler 各持一份，
        # 数据经边车文件天然共享）
        self.mirror_runtime = mirror_runtime_for(config)
        # 任务结束提醒开关（模块级旁路，同镜像源运行时随配置注入）
        notify.configure(config.notify)
        # 数据脱敏规则（模块级旁路，同 notify 随配置注入；看板保存后即时生效）
        masking.configure(config.masking)
        # 模型规格缓存（model id → context_window/max_output_tokens）：看板拉取
        # 模型列表时更新；保存配置时用模型上报值钳制/校验输出与上下文上限——
        # 模型服务端的限制才是真上限。内存态，重启后由下次拉取重建
        self._model_meta: dict[str, dict] = {}
        self.agents: dict[str, ProjectAgent] = {
            p.name: ProjectAgent(p, resolve_llm(config.llm, p.llm,
                                                context=f"项目 {p.name} 的 llm"),
                                 skills_base=self.skills_dir,
                                 tools=resolve_tools(config.tools, p.tools),
                                 permission_mode=resolve_permission_mode(config.permissions, p),
                                 permission_timeout_minutes=config.permissions.timeout_minutes,
                                 allowed_commands=resolve_commands(config.commands, p),
                                 extra_env=config.env.vars or None,
                                 mirror_runtime=self.mirror_runtime,
                                 path_autofix=config.env.path_autofix,
                                 review_cfg=config.review,
                                 local_debug=config.local_debug,
                                 commit_and_merge=resolve_commit_and_merge(config.git, p),
                                 agent_profiles=config.agent_profiles,
                                 global_tools=config.tools)
            for p in config.projects
        }
        # 程序自更新状态机：idle / checking / latest / available / downloading / restarting / error
        self.update_state: dict = {"phase": "idle"}
        # 登录会话：token -> 过期时间戳（内存态，进程重启即全部失效）
        self._auth_tokens: dict[str, float] = {}
        self._auth_lock = threading.Lock()
        # 登录限速（L1）：ip -> 滑窗内失败时间戳 / 锁定到期时间戳。
        # 内存态，重启清零可接受
        self._login_failures: dict[str, list[float]] = {}
        self._login_locked_until: dict[str, float] = {}
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

    def login(self, password: str, client_ip: str = "") -> dict:
        """校验登录密码；成功颁发会话 token（由 handler 负责下发 Cookie）。

        限速（L1）：同 IP 滑窗内失败达 LOGIN_MAX_FAILS 次即锁定 LOGIN_LOCKOUT
        秒，锁定期间直接拒绝、不做密码比对；登录成功清零该 IP 的失败计数。
        """
        if not self.auth_enabled:
            return {"ok": True, "token": None}
        now = time.time()
        with self._auth_lock:
            locked_until = self._login_locked_until.get(client_ip, 0.0)
            if locked_until and now >= locked_until:  # 锁定期满：恢复尝试
                self._login_locked_until.pop(client_ip, None)
                locked_until = 0.0
            if not locked_until:
                stamps = [t for t in self._login_failures.get(client_ip, [])
                          if now - t < self.LOGIN_WINDOW]
                if len(stamps) >= self.LOGIN_MAX_FAILS:
                    # 滑窗内失败达上限：锁定 LOGIN_LOCKOUT（独立于滑窗——
                    # 不是等 10 分钟窗口过期就自动解锁），期满重新计数
                    locked_until = now + self.LOGIN_LOCKOUT
                    self._login_locked_until[client_ip] = locked_until
                    self._login_failures[client_ip] = []
                else:
                    self._login_failures[client_ip] = stamps
        if locked_until > now:
            time.sleep(self.LOGIN_FAIL_DELAY)  # 与失败延迟一致：锁定期间也抬高探测成本
            return {"ok": False,
                    "message": "尝试次数过多，账号已临时锁定，请 15 分钟后再试"}
        if not verify_password(str(password or ""), self.config.web.auth_password):
            time.sleep(self.LOGIN_FAIL_DELAY)  # 失败延迟：抬高暴力破解成本
            with self._auth_lock:
                self._login_failures.setdefault(client_ip, []).append(now)
            return {"ok": False, "message": "密码错误"}
        token = secrets.token_urlsafe(32)
        with self._auth_lock:
            self._login_failures.pop(client_ip, None)  # 成功即清零失败计数
            self._login_locked_until.pop(client_ip, None)
            self._prune_sessions()
            self._auth_tokens[token] = time.time() + SESSION_TTL
        return {"ok": True, "token": token}

    def _clear_login_throttle(self) -> None:
        """清空登录限速状态（密码变更/测试重置用）：旧失败不应继续锁新密码。"""
        with self._auth_lock:
            self._login_failures.clear()
            self._login_locked_until.clear()

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
        """密码变更/清除后注销全部会话：旧 Cookie 立即失效，需用新密码重登。

        顺带清空登录限速状态：旧密码时期的失败/锁定不应妨碍新密码登录。
        """
        with self._auth_lock:
            self._auth_tokens.clear()
            self._login_failures.clear()
            self._login_locked_until.clear()

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

    # ---- 强制停止 ----

    def stop_task(self, data: dict) -> dict:
        """强制停止执行中的任务：置位停止信号，循环在工具边界收尾为 cancelled。"""
        from .task_control import get_task_controls

        project = str(data.get("project") or "")
        task_id = str(data.get("id") or "").strip()
        if project not in self.agents:
            raise ValueError(f"未知项目：{project}")
        if not task_id:
            raise ValueError("缺少任务 id")
        if not get_task_controls().stop(project, task_id):
            raise ValueError("该任务当前不在执行中（可能已结束）")
        log.info("Web 强制停止任务 %s/%s", project, task_id)
        return {"ok": True,
                "message": "已发送停止信号，任务将在当前工具调用边界中止并标记为已取消"}

    # ---- 镜像源测速与应用 ----

    def mirror_test(self, data: dict) -> dict:
        """测速镜像源（可只测单类别），结果落边车并返回。

        HTTP 直连探测（绕过系统代理），全量约 20 个源并发测，最坏 ~15s——
        ThreadingHTTPServer 下不阻塞其它请求；前端按钮转 loading。
        """
        category = str((data or {}).get("category") or "").strip() or None
        if category and category not in CATEGORY_KEYS:
            raise ValueError(f"未知镜像类别：{category}（可用：{', '.join(CATEGORY_KEYS)}）")
        results = self.mirror_runtime.run_speedtest(category)
        ok = sum(1 for v in results.values() if v is not None)
        log.info("Web 镜像测速%s：%d/%d 可达",
                 f"（{category}）" if category else "", ok, len(results))
        return {"ok": True, "results": results,
                "speeds": self.mirror_runtime.load_speeds(),
                "message": f"测速完成：{ok}/{len(results)} 个源可达"}

    def mirror_apply(self, data: dict) -> dict:
        """把代理类（GOPROXY/pip/npm）最快源写进 env.vars（覆盖同名旧值）。"""
        fastest = self.mirror_runtime.apply_fastest()
        if not fastest:
            raise ValueError("没有可用的测速结果：请先点「测速」，成功后再应用最快源")
        merged = {**self.config.env.vars, **fastest}
        self.save_settings({"env": {"vars": merged}})
        log.info("Web 应用最快镜像源到 env.vars：%s", fastest)
        return {"ok": True, "applied": fastest,
                "message": "已写入 Agent 子进程环境变量："
                           + "，".join(f"{k}={v}" for k, v in fastest.items())}

    # ---- 查询 ----

    def full_state(self) -> dict:
        projects = []
        for name, agent in self.agents.items():
            p = agent.project
            open_tasks, errors = scan_tasks(agent.tasks_dir)
            all_statuses, done_ids = collect_task_statuses(agent.tasks_dir)
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
                    dep_info = None if t.error else evaluate_task_dependencies(t, all_statuses, done_ids)
                    all_tasks.append(self._task_dict(t, dep_info))
            is_git = agent.executor.probe_git_repo()
            pending_git = Git(p.path).pending_changes_summary() if is_git else {"is_git": False, "count": 0, "files": []}
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
                # 项目级密钥同样只出掩码（H2 同规则）：项目专属模型 api_key 与
                # 仓库 token/密码都随 /api/state 外发，不能给明文
                "llm": self._project_llm_public(p.llm),
                # 项目级审查配置：原始覆盖值（review_enabled/review_llm/review_max_rounds）
                # + 生效值（开关/模型掩码/返工上限），前端展示「跟随全局」态用
                "review_enabled": p.review_enabled,
                "review_llm": self._project_llm_public(p.review_llm),
                "review_max_rounds": p.review_max_rounds,
                "review": self._project_review_public(p),
                "debug_allowed_hosts": p.debug_allowed_hosts or None,
                "commit_and_merge": p.commit_and_merge,
                "commit_and_merge_effective": resolve_commit_and_merge(self.config.git, p),
                "worktree_isolation_effective": resolve_worktree_isolation(self.config.git, p),
                "pending_git_changes": pending_git,
                "team": self._team_public(p.team),
                "team_enabled": bool(p.team and p.team.enabled),
                "team_members_effective": self._team_members_effective(p),
                "active_roles": dict(getattr(agent, "active_roles", {}) or {}),
                "repo": self._repo_public(p.repo),
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
                # 实时探测而非会话缓存：用户可能事后手动 init git，5s 轮询
                # 也能看到最新状态（探测带 .git 存在性预判，开销可忽略）
                "is_git_repo": is_git,
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
                # 密钥只出掩码不出明文（H2）：看板设置页用它提示「已配置，留空保持」
                "api_key_masked": mask_secret(self.config.llm.api_key),
                "temperature": self.config.llm.temperature,
                "max_tokens": self.config.llm.max_tokens,
                "max_context_tokens": self.config.llm.max_context_tokens,
                "enable_thinking": self.config.llm.enable_thinking,
                "timeout_seconds": self.config.llm.timeout_seconds,
            },
            # 全局审查配置：模型掩码同规则（api_key 不出明文）；model_configured
            # = 审查未启用默认态判定（设置页中性提示「配置一个不同模型即开启」）
            "review_cfg": {
                "enabled": self.config.review.enabled,
                "model_configured": self.config.review.llm is not None,
                "provider": self.config.review.llm.provider if self.config.review.llm else "openai",
                "model": self.config.review.llm.model if self.config.review.llm else "",
                "base_url": self.config.review.llm.base_url if self.config.review.llm else "",
                "api_key_masked": mask_secret(self.config.review.llm.api_key) if self.config.review.llm else "",
                "max_turns": self.config.review.max_turns,
                "max_review_rounds": self.config.review.max_review_rounds,
                "allow_test_commands": self.config.review.allow_test_commands,
                "squash_merge": self.config.review.squash_merge,
            },
            "scheduler": self.scheduler.status(),
            "update": self._healed_update_state(),
            # 上次升级结果：一次性曝光给前端弹窗（读后即清，避免轮询反复弹）
            "update_result": self._pop_update_result(),
            "scheduler_cfg": {
                "interval_seconds": self.config.scheduler.interval_seconds,
                "max_parallel_agents": self.config.scheduler.max_parallel_agents,
            },
            # 子代理并发预算：max_parallel/adaptive 是配置值，pools 是运行时
            # 实际生效值（自适应撞过 429 时 limit 会低于 max_parallel）
            "subagent_cfg": {
                "max_parallel": self.config.subagent.max_parallel,
                "adaptive": self.config.subagent.adaptive,
                "pools": snapshot_pools(),
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
                # 令牌只出掩码不出明文（H2），与 llm.api_key 同规则
                "token_masked": mask_secret(self.config.update.token),
                "token_configured": bool(self.config.update.token),
                "auto_check": self.config.update.auto_check,
            },
            "notify_cfg": {
                "sound": self.config.notify.sound,
                "system": self.config.notify.system,
            },
            # 数据脱敏设置（custom_patterns 是用户自填正则，非密钥，直接回显）
            "masking_cfg": {
                "enabled": self.config.masking.enabled,
                "enhanced_rules": self.config.masking.enhanced_rules,
                "custom_patterns": list(self.config.masking.custom_patterns),
            },
            "logging": {"retention_days": self.config.logging.retention_days,
                        "max_total_mb": self.config.logging.max_total_mb},
            # 工具开关：全局生效 map + 工具目录（名称/描述取自 TOOLS 注册表）。
            # finish 也进目录——前端把它渲染成「常开且禁用」的展示行，
            # validate_tools_map 会剥掉任何 finish 条目，配置语义不受影响
            "tools_cfg": dict(self.config.tools.enabled),
            "tools_catalog": [
                {
                    "name": name,
                    "description": desc,
                    "group": next((g for g, members in TOOL_GROUPS.items() if name in members), None),
                    "category": next((c["key"] for c in TOOL_CATEGORIES if name in c["tools"]), None),
                    "is_mutating": name in MUTATING_TOOLS,
                    "is_risky": name in RISKY_TOOLS,
                    "default_off": name in DEFAULT_OFF_TOOLS,
                }
                for name, (desc, _, _) in TOOLS.items()
            ],
            "tool_groups": {k: list(v) for k, v in TOOL_GROUPS.items()},
            "tool_categories": TOOL_CATEGORIES,
            "default_off_tools": sorted(DEFAULT_OFF_TOOLS),
            # 本地调试配置（Chrome/Edge 与后台服务）
            "local_debug_cfg": {
                "browser": self.config.local_debug.browser,
                "browser_executable": self.config.local_debug.browser_executable,
                "headless": self.config.local_debug.headless,
                "allowed_hosts": list(self.config.local_debug.allowed_hosts),
                "navigation_timeout_seconds": self.config.local_debug.navigation_timeout_seconds,
                "service_ready_timeout_seconds": self.config.local_debug.service_ready_timeout_seconds,
                "max_services": self.config.local_debug.max_services,
                "artifacts_retention_days": self.config.local_debug.artifacts_retention_days,
            },
            # Git 提交与合并配置（默认关闭，改动直接保留在工作区）
            "git_cfg": {
                "commit_and_merge": self.config.git.commit_and_merge,
            },
            # 访问权限：全局配置 + 当前挂起的审批请求（看板 5s 轮询刷新，
            # 顶栏「待审批」徽标与审批面板都读这里）
            "permissions_cfg": {
                "mode": self.config.permissions.mode,
                "timeout_minutes": self.config.permissions.timeout_minutes,
            },
            "approvals": get_manager().pending(),
            # 命令白名单开关：全局生效 map + 默认白名单目录（逐命令 NSwitch 用）
            "commands_cfg": dict(self.config.commands.enabled),
            "commands_catalog": list(DEFAULT_ALLOWED_COMMANDS),
            "command_categories": COMMAND_CATEGORIES,
            # Agent 子进程自定义环境变量（代理/国内镜像），设置页可视化编辑
            "env_cfg": dict(self.config.env.vars),
            # PATH 自动修复开关（env 节的 path_autofix）：env_cfg 本体保持
            # vars dict 结构（前端 Object.entries 直接消费），开关另用顶级键
            "env_path_autofix": self.config.env.path_autofix,
            # 镜像源管理：类别表 + 各类别源清单（内置在前、含启用态）+ 用户配置
            # + 测速结果边车（设置页「镜像源管理与测速选源」卡片全量数据）
            "mirror_categories": [
                {"key": c.key, "label": c.label, "kind": c.kind, "env_var": c.env_var}
                for c in CATEGORIES
            ],
            "mirrors_catalog": self.mirror_runtime.sources(),
            "mirrors_cfg": {
                "custom": {k: list(v) for k, v in self.config.mirrors.custom.items()},
                "disabled": list(self.config.mirrors.disabled),
            },
            "mirror_speeds": self.mirror_runtime.load_speeds(),
            # 模型用量：总体 + 各项目 + 按角色 + 近 12 周逐日 + 最近记录
            "usage": get_usage_store().summary(days=84),
            # 项目小组：全局内置角色元信息 + 用户自定义预设池（api_key 仅出掩码）
            "built_in_roles": BUILT_IN_ROLES,
            "role_prompt_templates": ROLE_PROMPT_TEMPLATES,
            "agent_profiles": {
                pid: self._profile_public(prof)
                for pid, prof in self.config.agent_profiles.items()
            },
            "app_autostart": self.app_autostart_status(),
            "skills_dir": str(self.skills_dir),
            "skills": [self._skill_dict(s) for s in skills_lib.load_skills(self.skills_dir)],
            "projects": projects,
            "log_tail": read_log_tail(self.log_path or self.config.logging.file),
        }

    @staticmethod
    def _project_llm_public(llm: dict | None) -> dict | None:
        """项目专属模型绑定的可外发形态：api_key 只出掩码与「已配置」标记（H2 同规则）。"""
        if not llm:
            return None
        out = {k: v for k, v in llm.items() if k != "api_key"}
        out["api_key_masked"] = mask_secret(str(llm.get("api_key") or ""))
        out["api_key_configured"] = bool(llm.get("api_key"))
        return out

    def _project_review_public(self, p: ProjectConfig) -> dict:
        """项目审查门的生效状态块（看板项目卡片/设置页展示用）。

        enabled_effective：项目覆盖后的开关；model_configured/effective_model：
        两级解析后的审查模型（api_key 只出掩码）；effective_max_rounds：两级
        解析后的返工上限；warning：互斥冲突提示（运行时守卫同源判定）。
        """
        cfg = self.config.review
        eff_llm = resolve_review_llm(cfg, p.review_llm,
                                     context=f"项目 {p.name} 的 review_llm")
        warning = ""
        if eff_llm is not None and review_enabled_for(cfg, p):
            try:
                assert_review_model_distinct(
                    resolve_llm(self.config.llm, p.llm,
                                context=f"项目 {p.name} 的 llm"),
                    eff_llm, context=f"项目 {p.name} 的 review")
            except ValueError as e:
                warning = str(e)
        return {
            "enabled_effective": review_enabled_for(cfg, p),
            "model_configured": eff_llm is not None,
            "effective_model": eff_llm.model if eff_llm else "",
            "api_key_masked": mask_secret(str(eff_llm.api_key)) if eff_llm else "",
            "effective_max_rounds": resolve_review_rounds(cfg, p),
            "warning": warning,
        }

    @staticmethod
    def _repo_public(repo: dict | None) -> dict | None:
        """项目仓库绑定的可外发形态：token/密码只出掩码与「已配置」标记（H2 同规则）。"""
        if not repo:
            return None
        out = {k: v for k, v in repo.items() if k not in ("token", "password")}
        out["token_masked"] = mask_secret(str(repo.get("token") or ""))
        out["token_configured"] = bool(repo.get("token"))
        out["password_masked"] = mask_secret(str(repo.get("password") or ""))
        out["password_configured"] = bool(repo.get("password"))
        return out

    @classmethod
    def _profile_public(cls, prof: AgentProfileConfig) -> dict:
        """全局 Agent 角色预设的可外发形态（api_key 脱敏）。"""
        return {
            "id": prof.id,
            "name": prof.name,
            "role": prof.role,
            "description": prof.description,
            "llm": cls._project_llm_public(prof.llm),
            "tools": prof.tools or None,
            "max_turns": prof.max_turns,
            "produces_code": prof.produces_code,
            "allow_test_scripts": prof.allow_test_scripts,
            "extra_prompt": prof.extra_prompt,
        }

    @classmethod
    def _team_public(cls, team: ProjectTeamConfig | None) -> dict | None:
        """项目小组编排的可外发形态（成员 api_key 脱敏）。"""
        if team is None:
            return None
        return {
            "enabled": team.enabled,
            "execution_mode": team.execution_mode,
            "max_qa_rounds": team.max_qa_rounds,
            "max_split_depth": team.max_split_depth,
            "chain_within_cycle": team.chain_within_cycle,
            "max_chain_steps_per_cycle": team.max_chain_steps_per_cycle,
            "fallback_role": team.fallback_role,
            "members": [
                {
                    "id": m.id,
                    "profile": m.profile,
                    "enabled": m.enabled,
                    "name": m.name,
                    "role": m.role,
                    "description": m.description,
                    "llm": cls._project_llm_public(m.llm),
                    "tools": m.tools or None,
                    "max_turns": m.max_turns,
                    "produces_code": m.produces_code,
                    "allow_test_scripts": m.allow_test_scripts,
                    "extra_prompt": m.extra_prompt,
                }
                for m in team.members
            ],
        }

    def _team_members_effective(self, p: ProjectConfig) -> list[dict]:
        """项目小组已启用成员的生效摘要列表（供看板展示角色徽标与实际模型）。"""
        if not p.team or not p.team.enabled:
            return []
        resolved = resolve_project_team_members(
            p, self.config.agent_profiles, self.config.llm, self.config.tools
        )
        return [
            {
                "id": m["id"],
                "role": m["role"],
                "name": m["name"],
                "icon": m["icon"],
                "description": m["description"],
                "model": m["llm"].model,
                "provider": m["llm"].provider,
                "produces_code": m["produces_code"],
                "allow_test_scripts": m["allow_test_scripts"],
                "max_turns": m["max_turns"],
            }
            for m in resolved.values()
        ]

    @staticmethod
    def _task_dict(t: Task, dep_info: dict | None = None) -> dict:
        out = {
            "id": t.id, "title": t.title, "priority": t.priority,
            "rank": t.rank, "status": t.status, "labels": t.labels,
            "assignee": t.assignee, "error": t.error,
            "file": str(t.path),
            "description": t.description[:2000],
            # 看板拖拽排序的手动序号；null = 未手动排过（显示回退状态→优先级）
            "order": t.order,
            # 审查门状态：任务卡片「待审查」徽标 + 审查轮数
            "review_pending": t.review_pending,
            "review_round": t.review_round,
            # 项目小组协同与 DAG 依赖字段
            "depends_on": list(t.depends_on),
            "parent_id": t.parent_id,
            "split_depth": t.split_depth,
            "qa_round": t.qa_round,
            "created_by": t.created_by,
        }
        if dep_info is not None:
            out["dep_state"] = dep_info["dep_state"]
            out["blocking_deps"] = dep_info["blocking_deps"]
            out["dep_done_count"] = dep_info["done_count"]
            out["dep_total_count"] = dep_info["total_deps"]
        else:
            out["dep_state"] = "ready"
            out["blocking_deps"] = []
            out["dep_done_count"] = len(t.depends_on)
            out["dep_total_count"] = len(t.depends_on)
        return out

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

        # 保留已有任务的协同元字段（编辑任务不得静默丢失 parent_id/split_depth/qa_round 等）
        old = parse_task_file(path) if path.exists() else None
        if old is not None and old.error:
            old = None

        # 前置依赖：请求带 depends_on 键则以请求为准（空列表=清除），否则沿用原值
        if "depends_on" in data:
            deps_raw = data.get("depends_on") or []
            if isinstance(deps_raw, str):
                deps_raw = [x for x in re.split(r"[,，\s]+", deps_raw) if x]
            if not isinstance(deps_raw, list):
                raise ValueError("depends_on 必须是任务 ID 列表")
            depends_on: list[str] = []
            for d in deps_raw:
                d = str(d).strip()
                if d and d not in depends_on:
                    depends_on.append(d)
        else:
            depends_on = list(old.depends_on) if old else []
        if task_id in depends_on:
            raise ValueError("任务不能依赖自身")
        if depends_on:
            agent = self.agents.get(project)
            known = {p.stem for p in agent.tasks_dir.glob("*.md")} if agent and agent.tasks_dir.is_dir() else set()
            done_dir = agent.tasks_dir / "done" if agent else None
            if done_dir and done_dir.is_dir():
                known |= {p.stem for p in done_dir.glob("*.md")}
            missing = [d for d in depends_on if d not in known]
            if missing:
                raise ValueError(f"前置任务不存在：{', '.join(missing)}")
            cyc = detect_dependency_cycle(agent.tasks_dir, extra_edges={task_id: depends_on})
            if cyc:
                raise ValueError(f"前置依赖存在循环：{' -> '.join(cyc)}")

        assignee = str(data.get("assignee") or "").strip()
        agent_cfg = self.agents.get(project)
        if (assignee and agent_cfg is not None and agent_cfg.team_enabled
                and assignee != project and assignee not in agent_cfg.members):
            raise ValueError(
                f"指派角色 {assignee!r} 不在本项目小组内，可用角色："
                f"{', '.join(agent_cfg.members) or '（无）'}")

        title_yaml = (json.dumps(title, ensure_ascii=False)
                      if any(ch in title for ch in (":", "#", "[", "]", "{", "}", '"', "'"))
                      else title)
        fm = [
            "---",
            f"title: {title_yaml}",
            f"priority: {priority}",
            f"status: {status}",
        ]
        if labels_line:
            fm.append(f"labels: [{labels_line}]")
        if assignee:
            fm.append(f"assignee: {assignee}")
        if depends_on:
            fm.append("depends_on: [" + ", ".join(depends_on) + "]")
        if old is not None:
            if old.parent_id:
                fm.append(f"parent_id: {old.parent_id}")
            if old.split_depth:
                fm.append(f"split_depth: {old.split_depth}")
            if old.qa_round:
                fm.append(f"qa_round: {old.qa_round}")
            if old.created_by:
                fm.append(f"created_by: {old.created_by}")
        else:
            fm.append("created_by: user")
        # 新建任务排到手动序末尾（已有 order 的最大值 +1）：看板里新建的
        # 任务接在已排序列后面，而不是插队到最前；更新已有任务不动 order
        if not path.exists():
            fm.append(f"order: {self._next_task_order(project)}")
        elif old is not None and old.order is not None:
            fm.append(f"order: {old.order}")
        fm.append("---")
        path.parent.mkdir(parents=True, exist_ok=True)
        # 描述留空用标题兜底：空正文会被解析器判为格式错误任务
        body = description or title
        path.write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
        log.info("Web 创建/更新任务 %s/%s [%s]", project, task_id, priority)
        return {"ok": True, "file": str(path), "id": task_id}

    def _next_task_order(self, project: str) -> int:
        """新建任务的手动序号：项目内已有 order 的最大值 +1（无则 1）。"""
        agent = self.agents.get(project)
        if agent is None or not agent.tasks_dir.is_dir():
            return 1
        orders = []
        for f in agent.tasks_dir.glob("*.md"):
            t = parse_task_file(f)
            if t.order is not None:
                orders.append(t.order)
        return max(orders) + 1 if orders else 1

    def reorder_tasks(self, data: dict) -> dict:
        """看板拖拽排序落盘：按给定 id 顺序重写 frontmatter 的 order（1..N）。

        只写发生变化的文件；ids 里不存在的任务跳过（并发删除等竞态 tolerable）。
        仅影响看板显示顺序，Agent 领取仍按优先级（scan_tasks 不受影响）。
        """
        project = str(data.get("project") or "")
        ids = data.get("ids")
        if project not in self.agents:
            raise ValueError(f"未知项目：{project}")
        if not isinstance(ids, list) or not ids:
            raise ValueError("ids 必须是非空数组（拖拽后的完整任务顺序）")
        agent = self.agents[project]
        updated = 0
        for i, tid in enumerate(ids, 1):
            tid = str(tid).strip()
            if not tid:
                continue
            path = agent.tasks_dir / f"{tid}.md"
            if not path.is_file():
                continue  # 并发删除/改名：跳过，不让整次排序失败
            t = parse_task_file(path)
            if t.error:
                continue  # 解析失败的文件不碰，避免把脏 frontmatter 写得更脏
            if t.order != i:
                if set_task_file_order(path, i):
                    updated += 1
        log.info("Web 拖拽排序 %s：%d 个任务重编号（%d 个文件落盘）",
                 project, len(ids), updated)
        return {"ok": True, "updated": updated}

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
        # 受阻重入队插队戳：→ open 时递增（下一轮扫描优先领取），离开 open 清除
        from .task_model import _set_frontmatter_field
        if status == "open":
            _set_frontmatter_field(path, None, "requeue_seq",
                                   str(_next_requeue_seq(path.parent)))
        else:
            _set_frontmatter_field(path, None, "requeue_seq", None)
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
                    "depends_on": [], "description": "", "error": t.error}
        return {"ok": True, "id": t.id, "title": t.title, "priority": t.priority,
                "status": t.status, "labels": t.labels, "assignee": t.assignee,
                "depends_on": list(t.depends_on), "parent_id": t.parent_id,
                "qa_round": t.qa_round,
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
        # 缓存模型规格：保存配置时按模型上报值钳制/校验 max_tokens 与上下文窗口
        for m in models:
            if m["context_window"] is not None or m["max_output_tokens"] is not None:
                self._model_meta[str(m["id"])] = m
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

    def test_browser_api(self, data: dict) -> dict:
        """测试 Chrome / Edge 浏览器连通性与版本探测。"""
        engine = str(data.get("browser") or self.config.local_debug.browser or "auto").strip().lower()
        custom_path = str(data.get("browser_executable") or self.config.local_debug.browser_executable or "").strip()
        from .browser.discover import find_browser, launch_browser, terminate_browser
        import httpx
        try:
            exe_path, found_engine = find_browser(engine, custom_path)
            proc, port, ws_url, profile_dir = launch_browser(exe_path, headless=True)
            version_info = "未知版本"
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=3.0)
                if resp.status_code == 200:
                    v_data = resp.json()
                    version_info = v_data.get("Browser", v_data.get("product", "未知版本"))
            except Exception:
                pass
            finally:
                terminate_browser(proc, ws_url=ws_url, profile_dir=profile_dir)
            return {
                "ok": True,
                "engine": found_engine,
                "path": exe_path,
                "version": version_info,
                "message": f"探测成功：{found_engine} ({version_info})",
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "message": f"浏览器探测失败：{e}"}

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
        # 盘符根（pathlib 里 p.parent == p）的上一级是「盘符列表」根入口（path=""），
        # 返回 "" 而非 None——None 会让前端把「上一级」按钮禁掉，用户回不到盘符列表
        parent = "" if p.parent == p else str(p.parent)
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
        # 不能用 `or 40` 兜底：0 是非法值该报错，且 -1 是合法语义不能被吞
        raw_turns = data.get("max_agent_turns")
        max_turns = 40 if raw_turns is None else int(raw_turns)
        if not (1 <= max_tasks <= 50):
            raise ValueError("每轮任务数应为 1~50")
        # -1 = 不设轮数上限（由强制停止/finish/审批兜底）
        if max_turns != -1 and not (5 <= max_turns <= 200):
            raise ValueError("工具循环轮数应为 5~200，或 -1 表示不设上限")
        cmds = data.get("allowed_commands")
        allowed = [str(c).strip() for c in cmds if str(c).strip()] if isinstance(cmds, list) else None
        branch = str(data.get("allowed_branch") or "").strip() or None
        # 编辑现有项目时取原配置：密钥类字段的「三态」合并需要它（H2 脱敏配套）
        existing = (next((p for p in self.config.projects if p.name == original_name), None)
                    if original_name else None)
        # 项目专属模型绑定：字典则清洗+校验（值非法立刻报错），显式 null 或全空=清除绑定。
        # api_key 三态：键缺失=保持原值（看板只拿到掩码，未输入就不带键）；带键
        # （含空串）=按新值处理，空串即清除绑定回「继承全局」
        llm_raw = data.get("llm")
        if llm_raw is not None and not isinstance(llm_raw, dict):
            raise ValueError("llm 绑定必须是键值对字典或 null")
        llm = None
        if isinstance(llm_raw, dict):
            llm_data = dict(llm_raw)
            if existing is not None and existing.llm and "api_key" not in llm_data:
                llm_data["api_key"] = existing.llm.get("api_key", "")
            llm = normalize_llm_override(llm_data) or None
            if llm:
                resolve_llm(self.config.llm, llm, context=f"项目 {name} 的 llm")
        # 远程仓库绑定（Issues 导入用）：null/空=清除；平台由 URL 识别或显式 provider。
        # token/password 同三态：键缺失=保持原值；带键（含空串）=新值/清除
        repo_raw = data.get("repo")
        if repo_raw is not None and not isinstance(repo_raw, dict):
            raise ValueError("repo 绑定必须是键值对字典或 null")
        repo = None
        if isinstance(repo_raw, dict) and repo_raw:
            repo_data = dict(repo_raw)
            if existing is not None and existing.repo:
                for key in ("token", "password"):
                    if key not in repo_data and existing.repo.get(key):
                        repo_data[key] = existing.repo[key]
            try:
                repo = normalize_repo(repo_data, context=f"项目 {name} 的 repo")
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
        # 项目级审查配置：开关（三态 null=跟随全局）、模型绑定（部分覆盖全局
        # review.llm；api_key 三态同 llm）、返工次数（null=跟随全局，1~10）
        review_enabled_raw = data.get("review_enabled")
        if review_enabled_raw is not None and not isinstance(review_enabled_raw, bool):
            raise ValueError("review_enabled 必须是布尔值或 null（跟随全局）")
        review_llm_raw = data.get("review_llm")
        if review_llm_raw is not None and not isinstance(review_llm_raw, dict):
            raise ValueError("review_llm 绑定必须是键值对字典或 null")
        review_llm = None
        if isinstance(review_llm_raw, dict):
            review_llm_data = dict(review_llm_raw)
            # api_key 三态（H2 配套）：键缺失=保持原值；带键（含空串）=新值/清除
            if (existing is not None and existing.review_llm
                    and "api_key" not in review_llm_data):
                review_llm_data["api_key"] = existing.review_llm.get("api_key", "")
            review_llm = normalize_review_llm(review_llm_data) or None
            if review_llm:
                # 显式 null/全空=清除绑定（回继承全局），同 llm 绑定语义
                resolve_review_llm(self.config.review, review_llm,
                                   context=f"项目 {name} 的 review_llm")
        review_rounds_raw = data.get("review_max_rounds")
        review_rounds = None
        if review_rounds_raw is not None:
            try:
                review_rounds = int(review_rounds_raw)
            except (TypeError, ValueError) as e:
                raise ValueError(f"review_max_rounds 必须是整数：{e}") from e
            if not (review_rounds == -1 or 1 <= review_rounds <= 10):
                raise ValueError(f"review_max_rounds 应为 -1（不设上限）或 1~10（当前：{review_rounds}）")
        debug_hosts_raw = data.get("debug_allowed_hosts")
        debug_hosts = (
            validate_allowed_hosts(debug_hosts_raw, context=f"项目 {name} 的 debug_allowed_hosts")
            if debug_hosts_raw is not None else None
        )
        cam_raw = data.get("commit_and_merge")
        commit_and_merge = bool(cam_raw) if cam_raw is not None else None

        # 项目小组编排：null / 缺失 = 沿用单兵模式；成员 llm.api_key 三态同其它密钥字段
        # （键缺失=保持原值；带键含空串=新值/清除）
        team = None
        if "team" in data and data.get("team") is not None:
            team_raw = data.get("team")
            if not isinstance(team_raw, dict):
                raise ValueError("team 配置必须是字典或 null")
            team_data = dict(team_raw)
            members_in = team_data.get("members") or []
            if not isinstance(members_in, list):
                raise ValueError("team.members 必须是列表")
            old_members = {m.id: m for m in (existing.team.members if existing and existing.team else [])}
            merged_members = []
            for m in members_in:
                if not isinstance(m, dict):
                    raise ValueError("team.members 每项必须是字典")
                m2 = dict(m)
                old = old_members.get(str(m2.get("id") or "").strip())
                m_llm = m2.get("llm")
                if isinstance(m_llm, dict) and old is not None and old.llm and "api_key" not in m_llm:
                    m_llm = dict(m_llm)
                    m_llm["api_key"] = old.llm.get("api_key", "")
                    m2["llm"] = m_llm
                merged_members.append(m2)
            team_data["members"] = merged_members
            team = validate_project_team(
                team_data, self.config.agent_profiles, self.config.llm,
                context=f"项目 {name} 的 team")
        elif existing is not None and "team" not in data:
            # 兼容未携带 team 键的旧调用方（如 toggleProject 回传遗漏）：保持原值不清空
            team = existing.team

        draft = ProjectConfig(
            name=name, path=path, enabled=bool(data.get("enabled", True)),
            tasks_dir=tasks_dir, guide_file=guide_file,
            max_tasks_per_cycle=max_tasks, max_agent_turns=max_turns,
            allowed_commands=allowed, allowed_branch=branch, llm=llm, repo=repo,
            tools=tools, permission_mode=perm,
            review_enabled=review_enabled_raw,
            review_llm=review_llm,
            review_max_rounds=review_rounds,
            debug_allowed_hosts=debug_hosts,
            commit_and_merge=commit_and_merge,
            team=team,
        )
        # 审查模型互斥校验（R4）：配置了审查模型且开关生效时，与该项目生效
        # 任务模型同名即拒绝保存（400，前端红字提示同一文案）
        eff_review_llm = resolve_review_llm(self.config.review, review_llm,
                                            context=f"项目 {name} 的 review_llm")
        if eff_review_llm is not None and review_enabled_for(self.config.review, draft):
            assert_review_model_distinct(
                resolve_llm(self.config.llm, llm, context=f"项目 {name} 的 llm"),
                eff_review_llm, context=f"项目 {name} 的 review")
        return draft

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
                        "retention_days": cfg.logging.retention_days,
                        "max_total_mb": cfg.logging.max_total_mb},
            "web": {"host": cfg.web.host, "port": cfg.web.port,
                    "open_browser": cfg.web.open_browser,
                    "auth_password": cfg.web.auth_password},
            # 全局工具开关：只落盘显式关闭/开启的项（空 dict = 全默认启用）
            "tools": dict(cfg.tools.enabled),
            # 命令白名单开关：只落盘显式禁用/启用的项（空 dict = 默认全放行）
            "commands": dict(cfg.commands.enabled),
            # Agent 子进程环境变量（代理/镜像）：整体落盘（空 dict = 不注入）；
            # path_autofix 必须一起写回——漏写会在看板任意保存时静默重置为关
            #（同 update 节漏写的教训）
            "env": {"vars": dict(cfg.env.vars),
                    "path_autofix": cfg.env.path_autofix},
            # 镜像源管理：用户添加源 + 禁用清单（测速结果在边车 JSON，不落这里）
            "mirrors": {"custom": {k: list(v) for k, v in cfg.mirrors.custom.items()},
                        "disabled": list(cfg.mirrors.disabled)},
            "permissions": {"mode": cfg.permissions.mode,
                            "timeout_minutes": cfg.permissions.timeout_minutes},
            # 自更新源必须一起写回：此前漏写，看板任意一次保存都会把
            # 用户填的 update.repo/token 从 config.yaml 里静默抹掉
            "update": {"repo": cfg.update.repo, "token": cfg.update.token,
                       "auto_check": cfg.update.auto_check},
            # 任务结束提醒开关必须一起写回：漏写会在看板任意保存时静默
            # 重置为默认全开（同 update 节漏写的教训）
            "notify": {"sound": cfg.notify.sound, "system": cfg.notify.system},
            # 数据脱敏配置整体写回（同 update/notify 教训：漏写会被看板任意
            # 保存静默重置回默认）
            "masking": {
                "enabled": cfg.masking.enabled,
                "enhanced_rules": cfg.masking.enhanced_rules,
                "custom_patterns": list(cfg.masking.custom_patterns),
            },
            # 任务审查（Review Agent）配置整体写回：漏写会在看板任意保存时
            # 静默重置（llm=None = 审查未启用默认态；同 update/notify 教训）
            "review": {
                "enabled": cfg.review.enabled,
                "llm": ({"provider": cfg.review.llm.provider,
                         "api_key": cfg.review.llm.api_key,
                         "base_url": cfg.review.llm.base_url,
                         "model": cfg.review.llm.model,
                         "temperature": cfg.review.llm.temperature,
                         "max_tokens": cfg.review.llm.max_tokens,
                         "max_context_tokens": cfg.review.llm.max_context_tokens,
                         "enable_thinking": cfg.review.llm.enable_thinking,
                         "timeout_seconds": cfg.review.llm.timeout_seconds}
                        if cfg.review.llm else None),
                "max_turns": cfg.review.max_turns,
                "max_review_rounds": cfg.review.max_review_rounds,
                "allow_test_commands": cfg.review.allow_test_commands,
                "squash_merge": cfg.review.squash_merge,
            },
            # 子代理并发预算（漏写会被看板任意保存静默重置回默认 4/自适应开）
            "subagent": {
                "max_parallel": cfg.subagent.max_parallel,
                "adaptive": cfg.subagent.adaptive,
            },
            # 本地调试配置整体写回
            "local_debug": {
                "browser": cfg.local_debug.browser,
                "browser_executable": cfg.local_debug.browser_executable,
                "headless": cfg.local_debug.headless,
                "allowed_hosts": list(cfg.local_debug.allowed_hosts),
                "navigation_timeout_seconds": cfg.local_debug.navigation_timeout_seconds,
                "service_ready_timeout_seconds": cfg.local_debug.service_ready_timeout_seconds,
                "max_services": cfg.local_debug.max_services,
                "artifacts_retention_days": cfg.local_debug.artifacts_retention_days,
            },
            # Git 自动化工作流配置
            "git": {
                "commit_and_merge": cfg.git.commit_and_merge,
            },
            # 全局 Agent 角色预设池整体写回（漏写会在看板任意保存时静默丢失）
            "agent_profiles": {
                pid: self._profile_to_yaml(prof) for pid, prof in cfg.agent_profiles.items()
            },
            "projects": [
                {
                    "name": p.name, "path": str(p.path), "enabled": p.enabled,
                    "tasks_dir": p.tasks_dir, "guide_file": p.guide_file,
                    "max_tasks_per_cycle": p.max_tasks_per_cycle,
                    "max_agent_turns": p.max_agent_turns,
                    **({"allowed_branch": p.allowed_branch} if p.allowed_branch else {}),
                    **({"allowed_commands": p.allowed_commands} if p.allowed_commands else {}),
                    **({"llm": p.llm} if p.llm else {}),
                    **({"review_enabled": p.review_enabled}
                       if p.review_enabled is not None else {}),
                    **({"review_llm": p.review_llm} if p.review_llm else {}),
                    **({"review_max_rounds": p.review_max_rounds}
                       if p.review_max_rounds is not None else {}),
                    **({"debug_allowed_hosts": p.debug_allowed_hosts}
                       if p.debug_allowed_hosts is not None else {}),
                    **({"commit_and_merge": p.commit_and_merge}
                       if p.commit_and_merge is not None else {}),
                    **({"repo": p.repo} if p.repo else {}),
                    **({"tools": p.tools} if p.tools else {}),
                    **({"permission_mode": p.permission_mode} if p.permission_mode else {}),
                    **({"team": self._team_to_yaml(p.team)} if p.team else {}),
                }
                for p in cfg.projects
            ],
        }
        src.write_text(
            "# orcsy 配置（由看板自动保存）\n"
            + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    @staticmethod
    def _profile_to_yaml(prof: AgentProfileConfig) -> dict:
        d: dict = {"name": prof.name, "role": prof.role}
        if prof.description:
            d["description"] = prof.description
        if prof.llm:
            d["llm"] = dict(prof.llm)
        if prof.tools:
            d["tools"] = dict(prof.tools)
        if prof.max_turns is not None:
            d["max_turns"] = prof.max_turns
        if prof.produces_code is not None:
            d["produces_code"] = prof.produces_code
        if prof.allow_test_scripts:
            d["allow_test_scripts"] = True
        if prof.extra_prompt:
            d["extra_prompt"] = prof.extra_prompt
        return d

    @staticmethod
    def _team_to_yaml(team: ProjectTeamConfig) -> dict:
        members = []
        for m in team.members:
            md: dict = {"id": m.id, "enabled": m.enabled}
            if m.profile:
                md["profile"] = m.profile
            if m.name:
                md["name"] = m.name
            if m.role:
                md["role"] = m.role
            if m.description:
                md["description"] = m.description
            if m.llm:
                md["llm"] = dict(m.llm)
            if m.tools:
                md["tools"] = dict(m.tools)
            if m.max_turns is not None:
                md["max_turns"] = m.max_turns
            if m.produces_code is not None:
                md["produces_code"] = m.produces_code
            if m.allow_test_scripts is not None:
                md["allow_test_scripts"] = m.allow_test_scripts
            if m.extra_prompt:
                md["extra_prompt"] = m.extra_prompt
            members.append(md)
        out: dict = {
            "enabled": team.enabled,
            "execution_mode": team.execution_mode,
            "max_qa_rounds": team.max_qa_rounds,
            "max_split_depth": team.max_split_depth,
            "chain_within_cycle": team.chain_within_cycle,
            "max_chain_steps_per_cycle": team.max_chain_steps_per_cycle,
        }
        if team.fallback_role:
            out["fallback_role"] = team.fallback_role
        out["members"] = members
        return out

    def _apply_config(self, new_cfg: AppConfig) -> None:
        self.config = new_cfg
        # 镜像源运行时随配置重建（custom/disabled 可能变了；测速边车路径不变）
        self.mirror_runtime = mirror_runtime_for(new_cfg)
        # 任务结束提醒开关随配置刷新（看板保存后立即生效，无需重启）
        notify.configure(new_cfg.notify)
        masking.configure(new_cfg.masking)
        # agents 覆盖全部项目（含停用的，便于看板展示与编辑）；
        # 调度器只留启用的（reload_config 已按 enabled 过滤）
        self.agents = {p.name: ProjectAgent(p, resolve_llm(new_cfg.llm, p.llm,
                                                           context=f"项目 {p.name} 的 llm"),
                                            skills_base=self.skills_dir,
                                            tools=resolve_tools(new_cfg.tools, p.tools),
                                            permission_mode=resolve_permission_mode(
                                                new_cfg.permissions, p),
                                            permission_timeout_minutes=new_cfg.permissions.timeout_minutes,
                                            allowed_commands=resolve_commands(new_cfg.commands, p),
                                            extra_env=new_cfg.env.vars or None,
                                            mirror_runtime=self.mirror_runtime,
                                            path_autofix=new_cfg.env.path_autofix,
                                            review_cfg=new_cfg.review,
                                            local_debug=new_cfg.local_debug,
                                            commit_and_merge=resolve_commit_and_merge(new_cfg.git, p),
                                            agent_profiles=new_cfg.agent_profiles,
                                            global_tools=new_cfg.tools)
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

        # 新项目/导入项目：git 就绪——非仓库则 git init，是仓库则补齐 .gitignore
        # （任务目录 / AGENT.md / worktree 不入版本库）。已有条目与用户内容一律
        # 不动；git 不可用等失败只记日志，不阻断建项。
        git_notes: list[str] = []
        if original is None and proj.path.is_dir():
            git = Git(proj.path)
            try:
                if not git.is_repo():
                    git.init()
                    git_notes.append("已初始化 git 仓库")
                added = git.ensure_gitignore([
                    f"{proj.tasks_dir.strip('/')}/", proj.guide_file,
                    f"{WORKTREE_DIR}/",
                ])
                if added:
                    git_notes.append(f".gitignore 已忽略 {'、'.join(added)}")
                if not git.has_commits():
                    git.add_all()
                    git.commit("chore: 初始化项目仓库")
                    git_notes.append("已创建初始提交")
            except (GitError, OSError) as e:
                log.warning("项目 %s git 初始化/忽略规则失败：%s", proj.name, e)

        self.scheduler.reload_config(self.config)
        self._apply_config(self.config)
        self._save_config(self.config)
        log.info("Web %s项目 %s（%s）", action, proj.name, proj.path)
        return {"ok": True, "created": created, "git": git_notes, "message":
                f"已{action}项目 {proj.name}"
                + (f"，初始化了 {'、'.join(created)}" if created else "")
                + (f"；{'；'.join(git_notes)}" if git_notes else "")}

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

    def manual_commit_project(self, data: dict) -> dict:
        """人工把关确认：将主工作区已落地的未提交变更在主分支执行 git commit（可选推送）。

        exclude：本次不提交的文件名列表（如临时测试产物），这些文件保留在工作区。
        """
        name = str(data.get("name") or data.get("project") or "").strip()
        message = str(data.get("message") or "").strip()
        push = bool(data.get("push", False))
        exclude_raw = data.get("exclude")
        exclude = ([str(x) for x in exclude_raw] if isinstance(exclude_raw, list) else [])
        agent = self.agents.get(name)
        if agent is None:
            raise ValueError(f"未知项目：{name}")
        p = agent.project
        if not agent.executor.probe_git_repo():
            raise ValueError(f"项目 {name} 不是 Git 仓库，无法提交")

        g = Git(p.path)
        res = g.commit_and_push_manual(message, push=push, branch=p.allowed_branch,
                                       exclude=exclude)
        # 人工提交完成，主工作区恢复干净，重置 stage 分支到最新 HEAD
        stage_branch = f"orcsy/stage/{p.name}"
        if g.branch_exists(stage_branch):
            g.ensure_stage_branch(stage_branch, g.current_branch(),
                                  stage_worktree=p.path / WORKTREE_DIR / "_stage")
        skipped = res.get("skipped") or []
        log.info("Web 人工确认提交项目 %s：%s（commit=%s, push=%s, 排除 %d 个文件）",
                 name, message, res.get("commit"), res.get("pushed"), len(skipped))
        msg = f"已成功提交至主分支（{res.get('commit')}）" + ("并推送到远程仓库" if res.get("pushed") else "")
        if skipped:
            msg += f"；已排除 {len(skipped)} 个文件未提交（保留在工作区）：{'、'.join(skipped[:10])}"
            if len(skipped) > 10:
                msg += " 等"
        if res.get("push_note"):
            msg += f"（推送警告：{res.get('push_note')}）"
        return {"ok": True, **res, "message": msg}

    def project_file_diff(self, data: dict) -> dict:
        """人工把关提交页：查看单个文件在主工作区的未提交改动 diff。

        新文件（未跟踪）返回全文预览；已修改文件返回 git diff 文本。
        附带 truncated/total_chars/lines 供前端提示与「下载补丁」使用。
        """
        name = str(data.get("name") or data.get("project") or "").strip()
        file_path = str(data.get("file") or "").strip()
        agent = self.agents.get(name)
        if agent is None:
            raise ValueError(f"未知项目：{name}")
        p = agent.project
        if not agent.executor.probe_git_repo():
            raise ValueError(f"项目 {name} 不是 Git 仓库，无法查看 diff")
        g = Git(p.path)
        info = g.file_diff(file_path)
        return {"ok": True, "file": file_path, **info}

    def discard_project_files(self, data: dict) -> dict:
        """丢弃指定文件的未提交改动（人工把关提交页「删除/还原」按钮）。

        未跟踪文件/目录直接物理删除；已跟踪文件还原到 HEAD（撤销本次改动）。
        """
        name = str(data.get("name") or data.get("project") or "").strip()
        files_raw = data.get("files")
        files = [str(x) for x in files_raw] if isinstance(files_raw, list) else []
        agent = self.agents.get(name)
        if agent is None:
            raise ValueError(f"未知项目：{name}")
        p = agent.project
        if not agent.executor.probe_git_repo():
            raise ValueError(f"项目 {name} 不是 Git 仓库，无法丢弃改动")
        g = Git(p.path)
        res = g.discard_changes(files)
        parts = []
        if res["deleted"]:
            parts.append(f"已删除 {len(res['deleted'])} 项")
        if res["restored"]:
            parts.append(f"已还原 {len(res['restored'])} 个文件")
        if res["skipped"]:
            parts.append(f"{len(res['skipped'])} 项跳过")
        log.info("Web 丢弃项目 %s 未提交改动：删除 %d / 还原 %d / 跳过 %d",
                 name, len(res["deleted"]), len(res["restored"]), len(res["skipped"]))
        return {"ok": True, **res, "message": "；".join(parts) or "没有可丢弃的改动"}

    def git_init_project(self, data: dict) -> dict:
        """为非 Git 目录一键初始化 Git 仓库并创建基础提交与忽略规则（开启小组 Worktree 必备）。"""
        name = str(data.get("name") or data.get("project") or "").strip()
        agent = self.agents.get(name)
        if agent is None:
            raise ValueError(f"未知项目：{name}")
        p = agent.project
        if not p.path.is_dir():
            raise ValueError(f"项目目录不存在：{p.path}")
        g = Git(p.path)
        if g.is_repo():
            return {"ok": True, "message": "该目录已是 Git 仓库"}
        g.init()
        g.ensure_gitignore([
            f"{p.tasks_dir.strip('/')}/",
            p.guide_file,
            f"{WORKTREE_DIR}/",
        ])
        g.add_all()
        g.commit("chore: 初始化项目仓库")
        agent.executor.probe_git_repo()
        log.info("Web 为项目 %s 初始化 Git 仓库", name)
        return {"ok": True, "message": f"已成功为项目 {name} 初始化 Git 仓库"}

    # ---- AGENT.md 编辑 ----

    # ---- 程序自更新 ----

    def _set_update_state(self, state: dict) -> None:
        """记录更新状态机快照；带时间戳供超时自愈判断。"""
        self.update_state = {**state, "ts": time.time()}

    @staticmethod
    def _strip_url_token(url: str) -> str:
        """去掉下载地址里的 access_token 查询参数（H2 同类：令牌不随状态外发）。

        私有仓库的附件下载地址带 ?access_token=...（Gitee 仅支持查询串鉴权），
        内部状态保留原值供下载，但 /api/state 只返回脱敏后的地址。
        """
        if not url:
            return url
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() != "access_token"]
        return urlunsplit(parts._replace(query=urlencode(query)))

    def _healed_update_state(self) -> dict:
        """读侧超时自愈：进行中状态超过 120 秒（如静默安装被外部打断、进程
        异常残留）时转为 error，避免看板按钮永久卡在「安装中」。
        附带安装日志尾部，静默失败可查；下载地址里的令牌脱敏后再外发。"""
        upd = dict(self.update_state)
        for key in ("download_url", "sha256_url"):
            if upd.get(key):
                upd[key] = self._strip_url_token(str(upd[key]))
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
            self._cleanup_download_dir(package)  # 失败重试会重新下载，不留残包
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
        dest: Path | None = None
        try:
            self._set_update_state({"phase": "downloading", "percent": 0})
            # 下载落私有临时目录（mkdtemp 随机名 + 用户级 %TEMP% ACL）：替换
            # 共享目录固定文件名，消除可预测路径的本地 TOCTOU 窗口（M2）。
            # 成功路径进程直接退出没机会清，残留由开头的超龄清理兜底回收
            updater.prune_stale_download_dirs()
            dest = Path(tempfile.mkdtemp(prefix="orcsy-update-"))
            name = self.update_state.get("asset_name") or "orcsy-update-download.zip"
            # 完整性校验（M2）：优先 API digest 字段给的哈希值；否则下载同名
            # .sha256 附件解析。两者皆无 = 无校验依据，拒绝在线更新——宁可这
            # 一版不能在线更新，不可无校验提权执行（手动导入不受影响）
            expected = str(self.update_state.get("sha256_value") or "")
            if not expected:
                sha_url = str(self.update_state.get("sha256_url") or "")
                if sha_url:
                    expected = updater.fetch_expected_sha256(sha_url, dest)
            if not expected:
                raise updater.UpdateError(
                    "本次 Release 未提供 sha256 校验文件，在线更新已拒绝；"
                    "请用设置页「选择安装包」手动导入更新包，或联系发布方补齐"
                    ".sha256 校验文件。")
            path = updater.download_installer(
                dl_url, dest, progress_cb=lambda pct: self.update_state.update(
                    {"phase": "downloading", "percent": pct}),
                filename=name, expected_sha256=expected)
            return self._start_install_thread(path)
        except updater.UpdateError as e:
            if dest is not None:
                shutil.rmtree(dest, ignore_errors=True)  # 失败不残留半截包
            self._set_update_state({"phase": "error", "message": str(e)})
            raise ValueError(str(e)) from e

    @staticmethod
    def _cleanup_download_dir(package: Path) -> None:
        """清理在线更新下载的私有临时目录（orcsy-update- 前缀才动，手动导入不涉及）。"""
        try:
            parent = package.parent
            if parent.name.startswith("orcsy-update-"):
                shutil.rmtree(parent, ignore_errors=True)
        except OSError:
            pass

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

    def restart_app(self) -> dict:
        """手动重启程序（看板按钮/托盘菜单）：看护脚本等本进程退出后拉起新进程。

        仅 Windows 安装版支持（源码/Docker 由 updater.plan_restart 给出指引）。
        先置更新状态 restarting（让前端遮罩与「重启等待」探测复用自更新同一套
        UI 流程），再后台线程启动看护并退出本进程——启动看护必须在本进程退出前
        完成，而退出会打断 HTTP 响应，所以响应先行、线程收尾。
        """
        try:
            bat, _exe = updater.plan_restart()
        except updater.UpdateError as e:
            raise ValueError(str(e)) from e
        self._reject_if_update_running()
        log.info("Web 请求重启程序（看护脚本 %s）", bat.name)
        self._set_update_state({"phase": "restarting"})
        threading.Thread(target=self._restart_worker, args=(bat,),
                         daemon=True, name="app-restart").start()
        return {"ok": True, "phase": "restarting",
                "message": "正在重启，应用将短暂离线并在数秒后自动回来"}

    def _restart_worker(self, bat: Path) -> None:
        """后台重启线程：启动看护 → 收尾 → 立即退出本进程（看护拉起新进程）。"""
        try:
            updater.start_watchdog(bat)
        except Exception:  # noqa: BLE001 — 启动失败时如实报错，程序继续运行
            log.exception("重启看护脚本启动失败")
            self._set_update_state({"phase": "error",
                                    "message": "重启失败：看护脚本启动出错，程序未重启"})
            return
        self._exit_for_restart()

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
        # 模型上限钳制提示（save_llm 分支填充；分块保存时保持为空）
        clamped: list[str] = []
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
            # 输出/上下文上限优先跟随模型上报规格（看板拉取模型列表时缓存在
            # _model_meta）：超出自动钳到模型值——模型服务端的限制才是真上限，
            # 写死的 20 万/200 万只是没拉过模型列表时的兜底
            spec = self._model_meta.get(model) or {}
            out_cap = spec.get("max_output_tokens")
            ctx_cap = spec.get("context_window")
            if isinstance(out_cap, int) and out_cap > 0 and max_tokens > out_cap:
                max_tokens = out_cap
                clamped.append(f"max_tokens 已按模型上限调整为 {out_cap}")
            if isinstance(ctx_cap, int) and ctx_cap > 0 and max_context > ctx_cap:
                max_context = ctx_cap
                clamped.append(f"上下文窗口已按模型上限调整为 {ctx_cap}")
            tokens_cap = out_cap if isinstance(out_cap, int) and out_cap > 0 else 200000
            if not (256 <= max_tokens <= tokens_cap):
                raise ValueError(f"最大输出 max_tokens 应为 256~{tokens_cap}")
            ctx_cap_eff = ctx_cap if isinstance(ctx_cap, int) and ctx_cap > 0 else 2_000_000
            if not (4096 <= max_context <= ctx_cap_eff):
                raise ValueError(f"上下文窗口 max_context_tokens 应为 4096~{ctx_cap_eff}")
            # 超时 ≤0（如 -1）归一为 -1 = 不超时（挂起靠「强制停止」打断）；
            # 不能用 `or` 兜底——0 是显式意图该归一，而不是被当成「未填」回退 300
            raw_ts = llm.get("timeout_seconds")
            ts = 300 if raw_ts is None else int(raw_ts)
            if ts <= 0:
                ts = -1
            # api_key 三态（H2 脱敏的配套）：键缺失=保持原值（看板不再回显明文，
            # 未输入新密钥时前端不带该键——若按旧的「缺失=清空」语义，改任意
            # 设置保存一次就会把已配好的密钥抹掉）；空字符串=清除；非空=换新密钥
            api_key = self.config.llm.api_key
            if "api_key" in llm:
                api_key = str(llm.get("api_key") or "").strip()
            self.config.llm = LLMConfig(
                provider=provider,
                api_key=api_key,
                base_url=base_url, model=model,
                temperature=float(llm.get("temperature", 0.2)),
                max_tokens=max_tokens,
                max_context_tokens=max_context,
                enable_thinking=bool(llm.get("enable_thinking",
                                             self.config.llm.enable_thinking)),
                timeout_seconds=ts,
            )
        self.config.scheduler = SchedulerConfig(
            interval_seconds=interval, max_parallel_agents=parallel,
            run_once=self.config.scheduler.run_once,
        )
        # host/port 变更需重启生效，其余立即生效
        # auth_password 三态：键缺失=保持原值；空字符串=清除（关闭鉴权）；非空=设置新密码（密文存储）
        auth_password = self.config.web.auth_password
        if "auth_password" in web:
            new_pwd = str(web.get("auth_password") or "").strip()
            if not new_pwd:
                if auth_password:
                    auth_password = ""
                    self._invalidate_sessions()
            elif not verify_password(new_pwd, auth_password):
                auth_password = hash_password(new_pwd)
                self._invalidate_sessions()  # 密码变更后旧会话全部作废
        elif auth_password and not is_password_hashed(auth_password):
            auth_password = hash_password(auth_password)
        cfg_host = str(web.get("host") or self.config.web.host)
        # 非回环绑定必须带登录密码（M1）：host 与密码都已定案后校验，
        # 不合规抛 ValueError → 400 透传看板，配置不落盘
        validate_web_security(cfg_host, auth_password, context="看板保存的 web 配置")
        self.config.web = WebConfig(
            host=cfg_host,
            port=port,
            open_browser=bool(web.get("open_browser", self.config.web.open_browser)),
            auth_password=auth_password,
        )
        # 日志保留天数：0 表示关闭自动归档与清理；体积上限同理（0 = 不限制）。
        # 都不能用 `or` 兜底——0 是显式关闭意图，不是「未填」
        log_cfg = data.get("logging") or {}
        if "logging" in data:
            rd = int(log_cfg.get("retention_days", self.config.logging.retention_days))
            if not (0 <= rd <= 365):
                raise ValueError("日志保留天数应为 0~365（0 表示关闭自动归档清理）")
            self.config.logging.retention_days = rd
            raw_mb = log_cfg.get("max_total_mb")
            mb = (self.config.logging.max_total_mb if raw_mb is None
                  else int(raw_mb))
            if not (0 <= mb <= 100000):
                raise ValueError("日志总占用上限应为 0~100000 MB（0 表示不限制）")
            self.config.logging.max_total_mb = mb

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

        # 任务结束提醒开关：分块保存，只带 notify 的请求不触碰其它配置
        if "notify" in data:
            not_cfg = data.get("notify") or {}
            self.config.notify = NotifyConfig(
                sound=bool(not_cfg.get("sound", self.config.notify.sound)),
                system=bool(not_cfg.get("system", self.config.notify.system)),
            )
            notify.configure(self.config.notify)

        # 数据脱敏：分块保存，只带 masking 的请求不触碰其它配置；自定义正则
        # 校验（条数/长度/可编译）400 透传看板
        if "masking" in data:
            m_cfg = data.get("masking") or {}
            self.config.masking = MaskingConfig(
                enabled=bool(m_cfg.get("enabled", self.config.masking.enabled)),
                enhanced_rules=bool(m_cfg.get("enhanced_rules",
                                              self.config.masking.enhanced_rules)),
                custom_patterns=validate_masking_patterns(
                    m_cfg.get("custom_patterns",
                              self.config.masking.custom_patterns),
                    context="masking.custom_patterns"),
            )
            masking.configure(self.config.masking)

        # 命令白名单开关：分块保存；键必须是默认白名单成员（400 透传看板）
        if "commands" in data:
            self.config.commands = CommandsConfig(
                enabled=validate_commands_map(data.get("commands"), context="commands"))

        # Agent 子进程环境变量（代理/国内镜像）：分块保存，键名与保留变量校验。
        # 兼容 {vars:{...}} 与平铺 {...} 两种前端/手写形态；path_autofix 键缺失
        # =保持原值（mirror_apply 等只带 vars 的保存不得静默重置开关），显式
        # 提供才更新——平铺形态键全部按环境变量处理，与 config.py 加载侧同规则
        if "env" in data:
            raw_env = data.get("env")
            if isinstance(raw_env, dict):
                env_map = raw_env.get("vars") if "vars" in raw_env else raw_env
            else:
                env_map = raw_env
            path_autofix = self.config.env.path_autofix
            if (isinstance(raw_env, dict) and "vars" in raw_env
                    and "path_autofix" in raw_env):
                path_autofix = bool(raw_env.get("path_autofix"))
            self.config.env = EnvConfig(
                vars=validate_env_vars(env_map, context="env.vars"),
                path_autofix=path_autofix)

        # 镜像源管理：分块保存（整块替换语义）；类别/URL 校验 400 透传看板
        if "mirrors" in data:
            mirrors_clean = validate_mirrors(data.get("mirrors"), context="mirrors")
            self.config.mirrors = MirrorsConfig(
                custom=mirrors_clean["custom"], disabled=mirrors_clean["disabled"])

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

        # 任务审查（Review Agent）：分块保存；模型三态 api_key 同 llm 全局节；
        # 保存后全项目互斥复检（全局审查模型换了，可能与任何项目的任务模型撞名）。
        # 先校验后赋值：复检失败抛 400 时配置保持原值（防半写）
        if "review" in data:
            rev = data.get("review") or {}
            rev_llm_raw = rev.get("llm")
            if rev_llm_raw is not None and not isinstance(rev_llm_raw, dict):
                raise ValueError("review.llm 必须是键值对字典或 null（null=清除绑定）")
            rev_llm = None
            if isinstance(rev_llm_raw, dict) and rev_llm_raw:
                rev_data = dict(rev_llm_raw)
                # api_key 三态（H2 配套）：键缺失=保持原值；空串=清除；非空=换新
                if ("api_key" not in rev_data and self.config.review.llm):
                    rev_data["api_key"] = self.config.review.llm.api_key
                rev_llm = _coerce_llm_cfg(rev_data, require_api_key=False,
                                          context="review.llm")
            rev_turns = int(rev.get("max_turns", self.config.review.max_turns))
            if not (rev_turns == -1 or 1 <= rev_turns <= 100):
                raise ValueError(f"review.max_turns 应为 -1（不设上限）或 1~100（当前：{rev_turns}）")
            rev_rounds = int(rev.get("max_review_rounds",
                                     self.config.review.max_review_rounds))
            if not (rev_rounds == -1 or 1 <= rev_rounds <= 10):
                raise ValueError(f"review.max_review_rounds 应为 -1（不设上限）或 1~10（当前：{rev_rounds}）")
            new_review = ReviewConfig(
                enabled=bool(rev.get("enabled", self.config.review.enabled)),
                llm=rev_llm,
                max_turns=rev_turns,
                max_review_rounds=rev_rounds,
                allow_test_commands=bool(rev.get(
                    "allow_test_commands", self.config.review.allow_test_commands)),
                squash_merge=bool(rev.get("squash_merge", self.config.review.squash_merge)),
            )
            # 全项目互斥复检：审查模型与任一启用审查项目的生效任务模型撞名即拒绝
            for p in self.config.projects:
                eff_review = resolve_review_llm(new_review, p.review_llm,
                                                context=f"项目 {p.name} 的 review_llm")
                if eff_review is not None and review_enabled_for(new_review, p):
                    assert_review_model_distinct(
                        resolve_llm(self.config.llm, p.llm,
                                    context=f"项目 {p.name} 的 llm"),
                        eff_review, context=f"项目 {p.name} 的 review")
            self.config.review = new_review  # 复检全过才落盘

        # 子代理并发预算：分块保存，只带 subagent 的请求生效；
        # 改完立刻同步进程级配额池（无需重启/等下一轮调度）
        if "subagent" in data:
            sub_raw = data.get("subagent") or {}
            try:
                sub_max = int(sub_raw.get("max_parallel",
                                          self.config.subagent.max_parallel))
            except (TypeError, ValueError):
                raise ValueError("子代理并发上限必须是整数") from None
            if not (1 <= sub_max <= 16):
                raise ValueError("子代理并发上限应为 1~16")
            self.config.subagent = SubagentConfig(
                max_parallel=sub_max,
                adaptive=bool(sub_raw.get("adaptive",
                                          self.config.subagent.adaptive)),
            )
            configure_limiter(self.config.subagent.max_parallel,
                              self.config.subagent.adaptive)

        # 本地调试配置：分块保存，只带 local_debug 的请求生效
        if "local_debug" in data:
            dbg_raw = data.get("local_debug") or {}
            dbg_browser = str(dbg_raw.get("browser", self.config.local_debug.browser)).strip().lower()
            if dbg_browser not in LOCAL_DEBUG_BROWSERS:
                raise ValueError(f"local_debug.browser 只能是 {' / '.join(LOCAL_DEBUG_BROWSERS)}（当前：{dbg_browser}）")
            dbg_nav = int(dbg_raw.get("navigation_timeout_seconds", self.config.local_debug.navigation_timeout_seconds))
            if not (5 <= dbg_nav <= 300):
                raise ValueError("页面导航超时应为 5~300 秒")
            dbg_srv = int(dbg_raw.get("service_ready_timeout_seconds", self.config.local_debug.service_ready_timeout_seconds))
            if not (5 <= dbg_srv <= 600):
                raise ValueError("服务就绪超时应为 5~600 秒")
            dbg_max = int(dbg_raw.get("max_services", self.config.local_debug.max_services))
            if not (1 <= dbg_max <= 10):
                raise ValueError("单任务最大后台服务数应为 1~10")
            dbg_ret = int(dbg_raw.get("artifacts_retention_days", self.config.local_debug.artifacts_retention_days))
            if not (0 <= dbg_ret <= 365):
                raise ValueError("工件保留天数应为 0~365 天")

            hosts_raw = dbg_raw.get("allowed_hosts")
            if hosts_raw is not None:
                dbg_hosts = validate_allowed_hosts(hosts_raw, context="local_debug.allowed_hosts")
            else:
                dbg_hosts = self.config.local_debug.allowed_hosts

            self.config.local_debug = LocalDebugConfig(
                browser=dbg_browser,
                browser_executable=str(dbg_raw.get("browser_executable", self.config.local_debug.browser_executable) or "").strip(),
                headless=bool(dbg_raw.get("headless", self.config.local_debug.headless)),
                allowed_hosts=dbg_hosts,
                navigation_timeout_seconds=dbg_nav,
                service_ready_timeout_seconds=dbg_srv,
                max_services=dbg_max,
                artifacts_retention_days=dbg_ret,
            )

        # Git 自动化工作流配置（任务完成后的提交与合并开关）
        if "git" in data:
            git_raw = data.get("git") or {}
            self.config.git = GitConfig(
                commit_and_merge=bool(git_raw.get("commit_and_merge", self.config.git.commit_and_merge)),
            )

        # 全局 Agent 角色预设池（分块保存；llm.api_key 三态脱敏保护）
        if "agent_profiles" in data:
            profs_in = data.get("agent_profiles") or {}
            if not isinstance(profs_in, dict):
                raise ValueError("agent_profiles 必须是字典")
            cleaned_profs: dict = {}
            for pid, item in profs_in.items():
                if not isinstance(item, dict):
                    raise ValueError(f"agent_profiles.{pid} 必须是字典")
                p_item = dict(item)
                old_prof = self.config.agent_profiles.get(pid)
                m_llm = p_item.get("llm")
                if isinstance(m_llm, dict) and old_prof and old_prof.llm and "api_key" not in m_llm:
                    m_llm = dict(m_llm)
                    m_llm["api_key"] = old_prof.llm.get("api_key", "")
                    p_item["llm"] = m_llm
                cleaned_profs[pid] = p_item
            new_profs = validate_agent_profiles(cleaned_profs, self.config.llm, context="agent_profiles")
            # 校验删除安全性：若已有启用的小组成员引用该预设，拒绝删除以防配置悬空
            for p in self.config.projects:
                if p.team:
                    for m in p.team.members:
                        if m.profile and m.profile not in new_profs:
                            raise ValueError(
                                f"预设 '{m.profile}' 正被项目 '{p.name}' 的小组成员 '{m.id}' 引用，"
                                f"请先在项目设置中解绑该预设后再删除。"
                            )
            self.config.agent_profiles = new_profs

        self.scheduler.reload_config(self.config)
        self._apply_config(self.config)
        self._save_config(self.config)

        # 新保留天数立即生效：顺手做一次归档/清理
        if "logging" in data:
            from .logging_setup import rotate_and_cleanup

            log_path = (Path(self.log_path) if self.log_path
                        else Path(self.config.logging.file))
            try:
                msg = rotate_and_cleanup(log_path, rd,
                                         self.config.logging.max_total_mb)
                if msg:
                    log.info("日志维护：%s", msg)
            except OSError as e:
                log.warning("日志归档/清理失败：%s", e)

        log.info("Web 保存配置：model=%s interval=%ss", self.config.llm.model, interval)
        note = f"（{'；'.join(clamped)}）" if clamped else ""
        return {"ok": True,
                "message": f"配置已保存并生效（看板 host/port 变更下次启动生效）{note}"}


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
        # client_address[0] 是真实对端 IP（直连部署，无反代）；不采信
        # X-Forwarded-For——可伪造，限速会失去意义
        result = self.state.login(str(body.get("password") or ""),
                                  client_ip=self.client_address[0])
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
        elif path == "/api/artifact":
            try:
                self._serve_artifact(self._query_params())
            except Exception as e:
                self._json({"ok": False, "message": str(e)}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def _serve_artifact(self, query: dict) -> None:
        p_raw = str(query.get("path") or "").strip()
        if not p_raw:
            return self._json({"error": "缺少 path 参数"}, 400)
        p = Path(p_raw).resolve()
        if not p.is_file():
            return self._json({"error": "工件文件不存在"}, 404)
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            return self._json({"error": "仅支持预览图片工件"}, 400)
        data = p.read_bytes()
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

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
            elif path == "/api/task/reorder":
                self._json(self.state.reorder_tasks(body))
            elif path == "/api/task/stop":
                self._json(self.state.stop_task(body))
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
            elif path == "/api/debug/browser-test":
                try:
                    self._json(self.state.test_browser_api(body))
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
            elif path == "/api/project/commit":
                try:
                    self._json(self.state.manual_commit_project(body))
                except (ValueError, GitError, OSError) as e:
                    self._json({"ok": False, "message": str(e)}, 400)
            elif path == "/api/project/diff":
                try:
                    self._json(self.state.project_file_diff(body))
                except (ValueError, GitError, OSError) as e:
                    self._json({"ok": False, "message": str(e)}, 400)
            elif path == "/api/project/discard":
                try:
                    self._json(self.state.discard_project_files(body))
                except (ValueError, GitError, OSError) as e:
                    self._json({"ok": False, "message": str(e)}, 400)
            elif path == "/api/project/git-init":
                try:
                    self._json(self.state.git_init_project(body))
                except (ValueError, GitError, OSError) as e:
                    self._json({"ok": False, "message": str(e)}, 400)
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
            elif path == "/api/app-restart":
                self._json(self.state.restart_app())
            elif path == "/api/approval":
                self._json(self.state.approval_respond(body))
            elif path == "/api/mirror/test":
                self._json(self.state.mirror_test(body))
            elif path == "/api/mirror/apply":
                self._json(self.state.mirror_apply(body))
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
