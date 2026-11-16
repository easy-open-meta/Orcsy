"""预算硬闸：任务级 token/成本上限 + 项目当日 token 上限。

设计要点（见 docs/plan-v1-trusted-pipeline.md §4.5）：

- **任务级**：单任务 prompt+completion 总量、折算成本超限 → 立即停止本次执行
  （抛 `BudgetExceeded`，由 executor 收敛为 blocked + block_reason=budget_exceeded），
  不新增任务状态值，避免破坏看板与旧任务兼容。
- **项目日窗**：按本地自然日累计每个项目的 tokens；达到上限后调度器不再领取
  新任务（软停），正在跑的任务允许收尾。
- **计量来源**：`LLMClient._acc_usage` 每次模型调用后上报；子代理共享同一
  tracker（子代理异常吞掉也没关系，tracker.exceeded 会让父循环在下次请求前
  停止）。
- 成本只在配置了 `prices[model]` 时计算；未配置单价只做 token 限制。
- 状态文件 `<配置目录>/state/budget.json`，与租约共用 `state/` 目录；
  读改写使用进程锁 + 跨进程文件锁 + 原子替换。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from .claims import _file_lock, claims_state_dir
from .config import BudgetConfig
from .logging_setup import get_logger

log = get_logger("budget")

SCHEMA_VERSION = 1
STATE_FILENAME = "budget.json"


class BudgetExceeded(RuntimeError):
    """预算超限：scope = task | project；usage 为当时累计值。"""

    def __init__(self, message: str, *, scope: str = "task",
                 usage: dict | None = None) -> None:
        super().__init__(message)
        self.scope = scope
        self.usage = usage or {}


class BudgetStore:
    """项目当日 token 累计（跨任务/跨进程持久化）。"""

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / STATE_FILENAME
        self.lock_path = self.path.with_suffix(".lock")
        self._tlock = threading.RLock()

    def _load_unlocked(self) -> dict:
        if not self.path.is_file():
            return {"schema_version": SCHEMA_VERSION, "projects": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema_version", SCHEMA_VERSION)
        projects = data.get("projects")
        data["projects"] = projects if isinstance(projects, dict) else {}
        return data

    def _save_unlocked(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def add(self, project: str, prompt_tokens: int, completion_tokens: int,
            cost_cny: float = 0.0) -> dict:
        today = self._today()
        with self._tlock, _file_lock(self.lock_path):
            data = self._load_unlocked()
            rec = data["projects"].get(str(project)) or {}
            if str(rec.get("date") or "") != today:
                rec = {"date": today, "prompt_tokens": 0,
                       "completion_tokens": 0, "cost_cny": 0.0}
            rec["prompt_tokens"] = int(rec.get("prompt_tokens") or 0) + int(prompt_tokens or 0)
            rec["completion_tokens"] = int(rec.get("completion_tokens") or 0) + int(completion_tokens or 0)
            rec["cost_cny"] = round(float(rec.get("cost_cny") or 0.0)
                                    + float(cost_cny or 0.0), 8)
            data["projects"][str(project)] = rec
            self._save_unlocked(data)
            return dict(rec)

    def project_day(self, project: str) -> dict:
        today = self._today()
        with self._tlock, _file_lock(self.lock_path):
            rec = self._load_unlocked()["projects"].get(str(project)) or {}
        if str(rec.get("date") or "") != today:
            return {"date": today, "prompt_tokens": 0,
                    "completion_tokens": 0, "cost_cny": 0.0,
                    "total_tokens": 0}
        out = dict(rec)
        out["total_tokens"] = (int(rec.get("prompt_tokens") or 0)
                               + int(rec.get("completion_tokens") or 0))
        return out

    def snapshot(self) -> dict[str, dict]:
        """所有项目今日用量（看板展示；过期日期视为 0）。"""
        today = self._today()
        with self._tlock, _file_lock(self.lock_path):
            projects = dict(self._load_unlocked()["projects"])
        out: dict[str, dict] = {}
        for name, rec in projects.items():
            if str(rec.get("date") or "") != today:
                continue
            out[name] = {
                "date": today,
                "prompt_tokens": int(rec.get("prompt_tokens") or 0),
                "completion_tokens": int(rec.get("completion_tokens") or 0),
                "cost_cny": round(float(rec.get("cost_cny") or 0.0), 6),
                "total_tokens": (int(rec.get("prompt_tokens") or 0)
                                 + int(rec.get("completion_tokens") or 0)),
            }
        return out


class BudgetTracker:
    """一次任务执行的预算计量与闸门（子代理共享同一实例）。"""

    def __init__(self, project: str, task_id: str, *,
                 config: BudgetConfig | None = None,
                 store: BudgetStore | None = None,
                 enforce_task: bool = True) -> None:
        self.project = str(project)
        self.task_id = str(task_id)
        self.cfg = config or get_budget_config()
        self.store = store or get_budget_store()
        self.enforce_task = bool(enforce_task)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_cny = 0.0
        self.exceeded = False
        self._lock = threading.Lock()

    # ---- 计量 ----

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def _price_for(self, model: str) -> dict[str, float] | None:
        prices = self.cfg.prices or {}
        price = prices.get(str(model or ""))
        if price is None:
            price = prices.get(str(model or "").lower())
        return price if isinstance(price, dict) else None

    def _cost(self, model: str, prompt_tokens: int,
              completion_tokens: int) -> float:
        price = self._price_for(model)
        if not price:
            return 0.0
        return ((int(prompt_tokens or 0) / 1_000_000)
                * float(price.get("prompt") or 0)
                + (int(completion_tokens or 0) / 1_000_000)
                * float(price.get("completion") or 0))

    def record(self, model: str, prompt_tokens: int,
               completion_tokens: int) -> None:
        """记录一次模型调用；超限时持久化后抛 BudgetExceeded。"""
        pt = max(0, int(prompt_tokens or 0))
        ct = max(0, int(completion_tokens or 0))
        cost = self._cost(model, pt, ct)
        with self._lock:
            self.prompt_tokens += pt
            self.completion_tokens += ct
            self.cost_cny = round(self.cost_cny + cost, 8)
            snapshot = self._usage_snapshot()
        try:
            self.store.add(self.project, pt, ct, cost)
        except Exception:  # noqa: BLE001 — 台账写失败不阻断任务（限额内仍可跑）
            log.warning("预算用量写盘失败（忽略）：项目 %s", self.project)
        self._check_limits(snapshot)

    def _usage_snapshot(self) -> dict:
        return {"task_id": self.task_id, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens, "cost_cny": self.cost_cny}

    def _check_limits(self, snapshot: dict) -> None:
        cfg = self.cfg
        if not cfg.enabled:
            return
        if self.enforce_task:
            if cfg.max_tokens_per_task > 0 and self.total_tokens >= cfg.max_tokens_per_task:
                self.exceeded = True
                raise BudgetExceeded(
                    f"任务 token 预算超限：{self.total_tokens} / "
                    f"{cfg.max_tokens_per_task}", scope="task", usage=snapshot)
            if cfg.max_cost_per_task_cny > 0 and self.cost_cny >= cfg.max_cost_per_task_cny:
                self.exceeded = True
                raise BudgetExceeded(
                    f"任务成本预算超限：{self.cost_cny:.4f} / "
                    f"{cfg.max_cost_per_task_cny} 元", scope="task", usage=snapshot)
        if cfg.max_tokens_per_project_per_day > 0:
            try:
                day = self.store.project_day(self.project)
            except Exception:  # noqa: BLE001
                return
            if int(day.get("total_tokens") or 0) >= cfg.max_tokens_per_project_per_day:
                self.exceeded = True
                raise BudgetExceeded(
                    f"项目今日 token 预算已用尽：{day['total_tokens']} / "
                    f"{cfg.max_tokens_per_project_per_day}", scope="project",
                    usage={**snapshot, "project_today": day})

    def precheck(self) -> None:
        """模型请求前检查：已超限（含子代理触发）则不再发请求。"""
        cfg = self.cfg
        if not cfg.enabled:
            return
        if self.exceeded:
            raise BudgetExceeded("预算已超限，停止本次执行", scope="task",
                                 usage=self._usage_snapshot())
        if (self.enforce_task and cfg.max_tokens_per_task > 0
                and self.total_tokens >= cfg.max_tokens_per_task):
            self.exceeded = True
            raise BudgetExceeded(
                f"任务 token 预算超限：{self.total_tokens} / "
                f"{cfg.max_tokens_per_task}", scope="task",
                usage=self._usage_snapshot())
        if (self.enforce_task and cfg.max_cost_per_task_cny > 0
                and self.cost_cny >= cfg.max_cost_per_task_cny):
            self.exceeded = True
            raise BudgetExceeded(
                f"任务成本预算超限：{self.cost_cny:.4f} / "
                f"{cfg.max_cost_per_task_cny} 元", scope="task",
                usage=self._usage_snapshot())
        if cfg.max_tokens_per_project_per_day > 0:
            try:
                day = self.store.project_day(self.project)
            except Exception:  # noqa: BLE001
                return
            if int(day.get("total_tokens") or 0) >= cfg.max_tokens_per_project_per_day:
                self.exceeded = True
                raise BudgetExceeded(
                    f"项目今日 token 预算已用尽：{day['total_tokens']} / "
                    f"{cfg.max_tokens_per_project_per_day}", scope="project",
                    usage={**self._usage_snapshot(), "project_today": day})

    def status(self) -> dict:
        return {"usage": self._usage_snapshot(),
                "limits": {"max_tokens_per_task": self.cfg.max_tokens_per_task,
                           "max_cost_per_task_cny": self.cfg.max_cost_per_task_cny,
                           "max_tokens_per_project_per_day":
                               self.cfg.max_tokens_per_project_per_day},
                "exceeded": self.exceeded}


# ---- 模块级配置与单例（与 usage/runlog 同风格） ----

_CONFIG = BudgetConfig()
_STORE: BudgetStore | None = None
_STORE_LOCK = threading.Lock()


def configure(cfg: BudgetConfig | None) -> None:
    global _CONFIG
    _CONFIG = cfg or BudgetConfig()


def get_budget_config() -> BudgetConfig:
    return _CONFIG


def init_budget_store(config: object = None, *, state_dir: Path | str | None = None,
                      force: bool = False) -> BudgetStore:
    global _STORE
    target = Path(state_dir) if state_dir else claims_state_dir(config)
    with _STORE_LOCK:
        if _STORE is not None and not force and _STORE.state_dir == target:
            return _STORE
        _STORE = BudgetStore(target)
        return _STORE


def get_budget_store() -> BudgetStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = BudgetStore(claims_state_dir(None))
        return _STORE


def project_budget_exhausted(project: str) -> tuple[bool, dict]:
    """项目当日预算是否用尽（调度器软停判断）。"""
    cfg = get_budget_config()
    if not cfg.enabled or cfg.max_tokens_per_project_per_day <= 0:
        return False, {}
    try:
        day = get_budget_store().project_day(project)
    except Exception:  # noqa: BLE001 — 计量不可用时不阻塞调度
        return False, {}
    return (int(day.get("total_tokens") or 0)
            >= cfg.max_tokens_per_project_per_day), day


__all__ = [
    "BudgetExceeded", "BudgetStore", "BudgetTracker",
    "configure", "get_budget_config", "init_budget_store",
    "get_budget_store", "project_budget_exhausted",
]
