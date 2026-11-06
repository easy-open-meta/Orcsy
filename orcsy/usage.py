"""模型用量台账：每个任务结束追加一条记录，看板展示各项目与总体用量。

记录内容：时间、项目、任务 ID、结果、轮数、输入/输出 tokens、模型名。
落盘为 JSONL（<配置目录>/logs/usage.jsonl，追加写、崩溃安全）；内存缓存
一份供 full_state 每 5s 轮询零成本聚合，无需反复读盘。条目数超上限时截断
最旧记录（内存与磁盘同步），防止无限增长。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("usage")

# 内存与落盘的条目上限：超出截断最旧记录（单条约 150B，2 万条约 3MB 封顶）
_MAX_ENTRIES = 20000


def usage_store_path(config: object = None) -> Path:
    """台账路径：与 orcsy.log / install.log 同目录（配置目录/logs/）。

    无配置时退回 %TEMP%（自检/测试兜底，风格同 updater.install_log_path）。
    """
    src = getattr(config, "source_path", None) if config is not None else None
    if src:
        return Path(src).parent / "logs" / "usage.jsonl"
    return Path(tempfile.gettempdir()) / "orcsy-usage.jsonl"


class UsageStore:
    """一个进程一份；record 线程安全（调度线程写、Web 线程读）。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._load()

    # ---- 读写 ----

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return  # 首次运行无台账属正常
        entries: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue  # 半行/脏行跳过，不让台账损坏拖垮看板
            if isinstance(e, dict):
                entries.append(e)
        self._entries = entries[-_MAX_ENTRIES:]

    def record(self, project: str, task_id: str, outcome: str, turns: int,
               prompt_tokens: int, completion_tokens: int,
               model: str = "", kind: str = "task") -> dict:
        """追加一条用量记录；写盘失败只记日志，绝不影响任务本身。

        kind：task（执行 Agent，默认）/ review（审查 Agent）——审查的 token
        消耗单列可核算；聚合视图对缺省 kind 兼容（旧记录视为 task）。
        """
        entry = {
            "ts": time.time(),
            "at": time.strftime("%Y-%m-%d %H:%M"),
            "project": str(project),
            "task_id": str(task_id),
            "outcome": str(outcome),
            "turns": int(turns or 0),
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "model": str(model or ""),
            "kind": str(kind or "task"),
        }
        with self._lock:
            self._entries.append(entry)
            overflow = len(self._entries) - _MAX_ENTRIES
            if overflow > 0:
                self._entries = self._entries[overflow:]
                self._rewrite_disk()
            else:
                self._append_disk(entry)
        return entry

    def _append_disk(self, entry: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("用量记录写盘失败（不影响任务）：%s", e)

    def _rewrite_disk(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                for e in self._entries:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("用量台账截断重写失败（不影响任务）：%s", e)

    # ---- 聚合（full_state 用） ----

    def summary(self, days: int = 14) -> dict:
        """总体 + 各项目 + 近 N 天逐日 + 最近记录（倒序）。"""
        with self._lock:
            entries = list(self._entries)

        def blank() -> dict:
            return {"tasks": 0, "turns": 0, "prompt_tokens": 0, "completion_tokens": 0}

        total = blank()
        projects: dict[str, dict] = {}
        for e in entries:
            total["tasks"] += 1
            total["turns"] += int(e.get("turns") or 0)
            total["prompt_tokens"] += int(e.get("prompt_tokens") or 0)
            total["completion_tokens"] += int(e.get("completion_tokens") or 0)
            p = projects.setdefault(e.get("project") or "?", blank())
            p["tasks"] += 1
            p["turns"] += int(e.get("turns") or 0)
            p["prompt_tokens"] += int(e.get("prompt_tokens") or 0)
            p["completion_tokens"] += int(e.get("completion_tokens") or 0)
            at = e.get("at") or ""
            if at > p.get("last_at", ""):
                p["last_at"] = at

        proj_list = [{"name": name, **agg} for name, agg in projects.items()]
        # 按总 tokens 降序，用量大户排前面
        proj_list.sort(key=lambda x: x["prompt_tokens"] + x["completion_tokens"],
                       reverse=True)

        # 近 N 天逐日（含零值天，趋势图横轴稳定）
        day_map: dict[str, dict] = {}
        now = time.localtime()
        for offset in range(days - 1, -1, -1):
            day = time.strftime("%Y-%m-%d",
                                time.localtime(time.mktime(now) - offset * 86400))
            day_map[day] = {"date": day, **blank()}
        for e in entries:
            day = time.strftime("%Y-%m-%d", time.localtime(float(e.get("ts") or 0)))
            if day in day_map:
                d = day_map[day]
                d["tasks"] += 1
                d["turns"] += int(e.get("turns") or 0)
                d["prompt_tokens"] += int(e.get("prompt_tokens") or 0)
                d["completion_tokens"] += int(e.get("completion_tokens") or 0)

        return {
            "total": total,
            "projects": proj_list,
            "days": list(day_map.values()),
            "recent": list(reversed(entries[-20:])),
        }


# 进程级单例：executor（记录方）与 AppState（展示方）共用，
# 免跨 Scheduler/Agent/Executor 构造器透传（同 approvals.get_manager 模式）
_STORE: UsageStore | None = None
_STORE_LOCK = threading.Lock()


def init_usage_store(config: object) -> UsageStore:
    """按配置锚定台账路径（Scheduler/AppState 启动时调用，幂等）。"""
    global _STORE
    path = usage_store_path(config)
    with _STORE_LOCK:
        if _STORE is None or _STORE.path != path:
            _STORE = UsageStore(path)
        return _STORE


def get_usage_store() -> UsageStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = UsageStore(usage_store_path(None))
        return _STORE
