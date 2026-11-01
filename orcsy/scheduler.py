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
from .config import AppConfig, resolve_llm, resolve_permission_mode, resolve_tools
from .issues import sync_issues
from .logging_setup import anchored_log_path, get_logger, rotate_and_cleanup
from .skills import skills_base

log = get_logger("scheduler")


def _now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Scheduler:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.skills_dir = skills_base(config)
        self.agents = [
            ProjectAgent(p, resolve_llm(config.llm, p.llm,
                                        context=f"项目 {p.name} 的 llm"),
                         skills_base=self.skills_dir,
                         tools=resolve_tools(config.tools, p.tools),
                         permission_mode=resolve_permission_mode(config.permissions, p),
                         permission_timeout_minutes=config.permissions.timeout_minutes)
            for p in config.projects
            if p.enabled
        ]
        if not self.agents:
            log.warning("配置中没有启用的项目")
        self._cycle_lock = threading.Lock()
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
        """用新配置重建 Agent 列表。正在执行的一轮跑完前不重建（锁保护）。"""
        with self._cycle_lock:
            self.config = new_config
            self.skills_dir = skills_base(new_config)
            self.agents = [
                ProjectAgent(p, resolve_llm(new_config.llm, p.llm,
                                            context=f"项目 {p.name} 的 llm"),
                             skills_base=self.skills_dir,
                             tools=resolve_tools(new_config.tools, p.tools),
                             permission_mode=resolve_permission_mode(new_config.permissions, p),
                             permission_timeout_minutes=new_config.permissions.timeout_minutes)
                for p in new_config.projects
                if p.enabled
            ]
        log.info("配置已热重载：%d 个启用项目", len(self.agents))

    # ---- 单轮 ----

    def run_cycle(self) -> dict[str, int]:
        """执行一轮扫描，返回 {项目名: 本轮处理任务数}。

        已有一轮在执行时直接返回空结果（不排队，避免任务重复领取）。
        """
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
                                     self.config.logging.retention_days)
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
