"""调度器：按配置周期驱动各项目的 Agent 扫描任务。

多 Agent 支持：
- 串行模式（max_parallel_agents=1）：一轮里逐个项目执行，稳定省资源；
- 并行模式（max_parallel_agents>1）：用线程池同时驱动多个项目的 Agent，
  每个项目仍是一个独立 Agent，互不干扰（各自有独立的 worktree 与对话历史）。

线程安全：run_cycle() 由周期线程与 Web 手动触发共用，全局互斥（同一时刻只有
一轮扫描在执行）；运行状态（is_running / last_cycle 等）供 Web 看板查询。
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .agent import ProjectAgent, recover_interrupted_tasks
from .config import (
    AppConfig,
    resolve_commands,
    resolve_commit_and_merge,
    resolve_llm,
    resolve_permission_mode,
    resolve_tools,
)
from .issues import sync_issues
from .logging_setup import anchored_log_path, get_logger, rotate_and_cleanup
from .mirrors import mirror_runtime_for
from .skills import ensure_builtin_skills, skills_base
from .usage import init_usage_store

log = get_logger("scheduler")


def _now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Scheduler:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.skills_dir = skills_base(config)
        # 内置技能播种/升级（幂等）：老用户拿到新内置技能，未改动副本随模板刷新
        seeded = ensure_builtin_skills(self.skills_dir)
        if seeded:
            log.info("已播种/升级内置技能：%s", ", ".join(seeded))
        # 用量台账按配置锚定路径（executor 记录、看板聚合共用同一单例）
        init_usage_store(config)
        # 镜像源运行时（测速结果边车与 config.yaml 同目录）：executor 的
        # speedtest_mirrors 工具与 env-setup 技能动态优选段共用
        self.mirror_runtime = mirror_runtime_for(config)
        self.agents = [
            ProjectAgent(p, resolve_llm(config.llm, p.llm,
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
            if p.enabled
        ]
        if not self.agents:
            log.warning("配置中没有启用的项目")
        self._cycle_lock = threading.Lock()
        # Agent 列表重建标记锁：reload_config 快速切换 config 并标记 stale，
        # 实际重建由后台线程 / run_cycle 开头消费（绝不阻塞 HTTP 保存请求）
        self._agents_lock = threading.Lock()
        self._agents_stale = False
        self._stop_event = threading.Event()
        # 定时拉取 Issues：独立后台线程（拉取是网络 I/O，不占用调度周期锁）。
        # _issue_timers：项目名 → 下次应拉取的 time.monotonic() 时刻；
        # issue_pull_status：项目名 → 最近一次拉取结果（供看板展示）。
        self._issue_stop_event = threading.Event()
        self._issue_timers: dict[str, float] = {}
        self.issue_pull_status: dict[str, dict] = {}
        self.is_running = False          # 当前是否有一轮扫描在执行
        self.last_cycle_stats: dict[str, int] = {}
        self.last_cycle_at: str = ""
        self.next_cycle_at: str = ""
        self.cycles_done: int = 0
        self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        """启动恢复：上次进程意外退出遗留的 in_progress 任务重置为待办。

        只在进程启动时执行一次（本构造函数不会被热重载走到），正在执行中
        的任务不受影响。
        """
        reset: list[str] = []
        for agent in self.agents:
            try:
                ids = recover_interrupted_tasks(agent.project)
            except Exception:  # noqa: BLE001 — 恢复失败不能阻止启动
                agent.log.exception("中断任务恢复检查失败")
                continue
            if ids:
                agent.log.warning("检测到 %d 个被中断的任务，已重置为待办：%s",
                                  len(ids), ", ".join(ids))
                reset.append(f"{agent.project.name}[{len(ids)}]")
        if reset:
            log.warning("上次运行意外退出，重置中断任务待重跑：%s", "; ".join(reset))

    # ---- 状态查询（供 Web 看板） ----

    def status(self) -> dict:
        return {
            "running": self.is_running,
            "cycles_done": self.cycles_done,
            "last_cycle_at": self.last_cycle_at,
            "next_cycle_at": self.next_cycle_at,
            "interval_seconds": max(30, self.config.scheduler.interval_seconds),
            "last_cycle_stats": self.last_cycle_stats,
        }

    # ---- 配置热重载（Web 修改配置/项目后调用） ----

    def reload_config(self, new_config: AppConfig) -> None:
        """用新配置重建 Agent 列表。正在执行的一轮跑完前不重建（后台兜底）。

        锁语义：本方法绝不能阻塞 HTTP 保存请求——任务可能一跑数小时，若与
        run_cycle 抢同一把 _cycle_lock，看板「保存设置」会一直 loading。
        因此 config/mirror_runtime 先行原子切换（保存即时生效的新值供
        下轮读取），Agent 列表重建交给后台线程拿锁执行；拿不到锁（一轮
        执行中）也无需重试——run_cycle 每轮结束后的下一次 reload（或
        _sync_agents_if_stale 兜底）都会用 self.config 最新值重建。
        """
        with self._agents_lock:
            self.config = new_config
            self.skills_dir = skills_base(new_config)
            self.mirror_runtime = mirror_runtime_for(new_config)
            self._agents_stale = True
        t = threading.Thread(target=self._rebuild_agents_background,
                             name="reload-agents", daemon=True)
        t.start()
        log.info("配置已接收，Agent 列表后台重建中")

    def _rebuild_agents_background(self) -> None:
        """后台重建 Agent 列表：只在空闲（无执行轮）时拿锁换名单。"""
        if not self._cycle_lock.acquire(blocking=False):
            return  # 一轮执行中：等下轮触发前由 _sync_agents_if_stale 补建
        try:
            self._rebuild_agents_locked()
        finally:
            self._cycle_lock.release()

    def _rebuild_agents_locked(self) -> None:
        """持有 _cycle_lock 的实际重建逻辑（run_cycle 结束前也调用以消费最新配置）。"""
        cfg = self.config
        ensure_builtin_skills(self.skills_dir)
        self.agents = [
            ProjectAgent(p, resolve_llm(cfg.llm, p.llm,
                                        context=f"项目 {p.name} 的 llm"),
                         skills_base=self.skills_dir,
                         tools=resolve_tools(cfg.tools, p.tools),
                         permission_mode=resolve_permission_mode(cfg.permissions, p),
                         permission_timeout_minutes=cfg.permissions.timeout_minutes,
                         allowed_commands=resolve_commands(cfg.commands, p),
                         extra_env=cfg.env.vars or None,
                         mirror_runtime=self.mirror_runtime,
                         path_autofix=cfg.env.path_autofix,
                         review_cfg=cfg.review,
                         local_debug=cfg.local_debug,
                         commit_and_merge=resolve_commit_and_merge(cfg.git, p),
                         agent_profiles=cfg.agent_profiles,
                         global_tools=cfg.tools)
            for p in cfg.projects
            if p.enabled
        ]
        with self._agents_lock:
            self._agents_stale = False
        log.info("配置已热重载：%d 个启用项目", len(self.agents))

    def _sync_agents_if_stale(self) -> None:
        """run_cycle 每轮开始前调用：配置有变更且未重建时，先重建再扫描。"""
        with self._agents_lock:
            stale = self._agents_stale
        if stale and self._cycle_lock.acquire(blocking=False):
            try:
                self._rebuild_agents_locked()
            finally:
                self._cycle_lock.release()

    # ---- 单轮 ----

    def run_cycle(self) -> dict[str, int]:
        """执行一轮扫描，返回 {项目名: 本轮处理任务数}。

        已有一轮在执行时直接返回空结果（不排队，避免任务重复领取）。
        """
        # 配置有变更时先在本轮开始前重建 Agent 名单（此刻必无执行轮，锁必可得）
        self._sync_agents_if_stale()
        if not self._cycle_lock.acquire(blocking=False):
            log.warning("上一轮扫描尚未结束，跳过本轮触发")
            return {}
        self.is_running = True
        try:
            log.info("========== 调度周期开始 ==========")
            self._rotate_logs_if_due()
            stats: dict[str, int] = {}
            max_parallel = max(1, self.config.scheduler.max_parallel_agents)

            if max_parallel == 1 or len(self.agents) == 1:
                for agent in self.agents:
                    stats[agent.project.name] = self._run_agent(agent)
            else:
                stats = self._run_parallel(max_parallel)

            self.last_cycle_stats = stats
            self.last_cycle_at = _now_str()
            self.cycles_done += 1
            log.info("========== 调度周期结束：%s ==========", stats or "无任务处理")
            return stats
        finally:
            self.is_running = False
            self._cycle_lock.release()

    def _rotate_logs_if_due(self) -> None:
        """每轮顺手检查日志归档/清理：内部按创建时间自判是否到期，代价一次 stat。"""
        if not self.config.source_path:  # 直接构造 AppConfig 的测试场景，跳过
            return
        try:
            msg = rotate_and_cleanup(anchored_log_path(self.config),
                                     self.config.logging.retention_days,
                                     self.config.logging.max_total_mb)
        except Exception:  # noqa: BLE001 — 日志维护失败不影响调度
            log.exception("日志归档/清理失败")
            return
        if msg:
            log.info("日志维护：%s", msg)

    def _run_agent(self, agent: ProjectAgent) -> int:
        try:
            results = agent.scan_and_work()
            return len(results)
        except Exception:  # noqa: BLE001 — 单项目异常不能拖垮整体调度
            agent.log.exception("项目 Agent 本轮执行异常")
            return 0

    def _run_parallel(self, max_parallel: int) -> dict[str, int]:
        stats: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = {pool.submit(self._run_agent, agent): agent for agent in self.agents}
            for future in as_completed(futures):
                agent = futures[future]
                try:
                    stats[agent.project.name] = future.result()
                except Exception:  # noqa: BLE001
                    agent.log.exception("并行执行异常")
                    stats[agent.project.name] = 0
        return stats

    # ---- 持续循环（后台线程模式，供 Web 服务调用） ----

    def run_forever_background(self) -> threading.Thread:
        t = threading.Thread(target=self._loop, name="scheduler-loop", daemon=True)
        t.start()
        return t

    def _loop(self) -> None:
        interval = max(30, self.config.scheduler.interval_seconds)
        log.info("调度循环启动：每 %d 秒扫描 %d 个项目", interval, len(self.agents))
        while not self._stop_event.is_set():
            self.run_cycle()
            # 分片等待，期间响应配置变更（间隔变短能提前醒来）与 stop
            waited = 0.0
            interval = max(30, self.config.scheduler.interval_seconds)
            while waited < interval and not self._stop_event.is_set():
                step = min(1.0, interval - waited)
                self._stop_event.wait(step)
                waited += step
                interval = max(30, self.config.scheduler.interval_seconds)
            if not self._stop_event.is_set():
                self.next_cycle_at = _dt.datetime.now().strftime("%H:%M:%S")
        log.info("调度循环已退出")

    def stop(self) -> None:
        self._stop_event.set()
        self._issue_stop_event.set()

    # ---- 定时拉取远程 Issues（独立后台线程） ----

    def run_issues_forever_background(self) -> threading.Thread:
        t = threading.Thread(target=self._issues_loop, name="issue-pull-loop", daemon=True)
        t.start()
        return t

    def _issues_loop(self) -> None:
        """按各项目 repo.auto_pull_interval 定时拉取 open Issues 入任务队列。

        分片等待 + 每次唤醒遍历到期项目（首次见到立即拉一次）；单项目失败只
        记状态不中断循环。reload_config 原子替换 agents 列表，这里每次醒来
        重读，间隔变更无需重启即生效。
        """
        log.info("Issue 自动拉取循环启动")
        while not self._issue_stop_event.is_set():
            self._pull_due_issues()
            waited = 0.0
            while waited < 10 and not self._issue_stop_event.is_set():
                self._issue_stop_event.wait(1.0)
                waited += 1.0
        log.info("Issue 自动拉取循环已退出")

    def _pull_due_issues(self) -> None:
        now = time.monotonic()
        for agent in list(self.agents):
            repo = agent.project.repo
            interval = (repo or {}).get("auto_pull_interval") or 0
            if not interval:
                continue
            name = agent.project.name
            due = self._issue_timers.get(name, now)  # 首次见到：立即拉取
            if now < due:
                continue
            try:
                result = sync_issues(name, repo, agent.tasks_dir)
            except Exception:  # noqa: BLE001 — 网络失败不能拖垮自动拉取循环
                agent.log.exception("自动拉取 Issues 失败")
                self.issue_pull_status[name] = {
                    "interval": interval, "last_error": "拉取失败（详见日志）",
                    "imported": 0, "skipped": 0,
                }
            else:
                agent.log.info("自动拉取 Issues：导入 %d，跳过 %d（共 %d）",
                               result["imported"], result["skipped"], result["total"])
                self.issue_pull_status[name] = {
                    "interval": interval,
                    "imported": result["imported"], "skipped": result["skipped"],
                    "total": result["total"], "last_error": "",
                }
            self._issue_timers[name] = time.monotonic() + interval

    # ---- 前台阻塞模式（CLI run 命令） ----

    def run_forever(self) -> None:
        log.info(
            "进入持续调度：每 %d 秒扫描 %d 个项目（Ctrl+C 退出）",
            max(30, self.config.scheduler.interval_seconds), len(self.agents),
        )
        self.run_issues_forever_background()
        try:
            self._loop()
        except KeyboardInterrupt:
            log.info("收到退出信号，调度器停止")
