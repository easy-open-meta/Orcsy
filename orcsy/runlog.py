"""运行台账（RunLog）：以 run 为单位的结构化证据链与回放数据源。

与既有三处留痕的分工（职责不重叠）：
- `append_progress` 写任务文件：给「任务本身」看进度，随任务归档；
- `logs/orcsy.log`：运行排障，按天/大小滚动；
- `logs/usage.jsonl`：token 计量与看板聚合；
- **本模块**：唯一以 `run_id` 为主键、可整体回放的结构化记录——事件流 +
  diff / 测试输出 / 审查结论 / 用量工件。

设计原则（与 usage.py 一致）：
1. **旁路**：任何写盘/序列化失败只记日志，绝不抛给任务执行链路；
2. **不落敏感原文**：所有自由文本先过 masking 规则，事件字段只留摘要；
3. **向后兼容**：不改变任务状态、不改既有 API；旧配置无需新增字段。

目录结构（默认在配置目录下，不污染受管仓库）：

    <data_dir>/runs/<project>/<run_id>/
        meta.json        # 运行元信息（含 outcome / exit_reason）
        events.jsonl     # append-only 事件流，seq 单调递增
        diff.patch       # 本次变更（相对基线，截断上限可配）
        test.log         # 测试类命令输出（追加）
        review.json      # 审查结论
        usage.json       # 本 run 的 token 与角色
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger("runlog")

# 允许的事件类型：拼写错误在写入前暴露，而不是半年后才发现字段名不一致
EVENT_TYPES = frozenset({
    "run_start", "claim", "turn", "model_call", "tool_call", "tool_result",
    "chat", "progress", "review_start", "review_verdict", "commit", "merge",
    "run_end",
})

# 全过程日志（S 后续/④ 回放增强）字段上限：保证单事件净负荷 < MAX_EVENT_DATA_BYTES
CHAT_CONTENT_CAP = 2000
CHAT_REASONING_CAP = 2000
TOOL_RESULT_CAP = 4000

# 允许按名字读取的工件（防止路径穿越与随手读任意文件）
ARTIFACTS = ("meta.json", "events.jsonl", "diff.patch", "test.log",
             "review.json", "usage.json")

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024   # 单工件上限 2MB（超出截断并标注）
MAX_PREVIEW_CHARS = 300                # 事件里的自由文本预览上限
MAX_EVENT_DATA_BYTES = 8 * 1024        # 单事件净负荷上限（超出转为摘要）

# ---- 目录迁移（V1-R1 / runlog.dir）----
SCHEMA_VERSION = 1
STATE_FILENAME = "runlog-state.json"       # 上次实际使用的 root（兼容手工改配置）
JOURNAL_FILENAME = "runlog-migration.json"  # 未完成迁移的续跑依据
_PARTIAL_SUFFIX = ".migrating"
_CONFLICT_DIR = "_conflicts"
MIGRATION_IDLE = "idle"
MIGRATION_RUNNING = "running"
MIGRATION_DONE = "done"
MIGRATION_FAILED = "failed"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# 测试类命令：命中才把输出追加进 test.log，避免把 ls/cat 全部收进来
_TEST_CMD_RE = re.compile(
    r"(?:^|[\s;&|])(?:pytest|py\.test|python\s+-m\s+(?:pytest|unittest)|"
    r"npm\s+(?:run\s+)?test|yarn\s+test|pnpm\s+(?:run\s+)?test|"
    r"go\s+test|cargo\s+test|dotnet\s+test|mvn\s+(?:-\S+\s+)*test|"
    r"gradle\w*\s+test|vitest|jest|playwright\s+test|ctest)",
    re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    """把任意值收敛成可 JSON 序列化的形态（旁路模块不允许因此报错）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > MAX_PREVIEW_CHARS:
            return value[:MAX_PREVIEW_CHARS] + "…"
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in list(value)[:50]]
    return str(value)[:MAX_PREVIEW_CHARS]


def redact_text(text: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    """先截断再脱敏：台账任何自由文本都不得出现密钥原文。

    每次新建 MaskingSession 是有意为之：会话内部维护「原文 ↔ 占位符」映射，
    台账是长期落盘模块，不能把原文缓存在内存里。占位符由内容哈希派生，
    多次调用结果稳定。
    """
    raw = str(text or "")
    if len(raw) > limit:
        raw = raw[:limit] + "…"
    try:
        from .masking import MaskingSession
        return MaskingSession().mask_text(raw)[0]
    except Exception:  # noqa: BLE001 — 脱敏失败时宁可不写全文
        return raw


def _safe_component(name: str) -> str:
    """把项目名/任务 ID 收敛成单层目录名，同时尽量保留可读性。"""
    raw = str(name or "unknown").strip() or "unknown"
    safe = _SAFE_NAME_RE.sub("_", raw)[:60].strip("._-") or "x"
    if safe != raw:
        safe += "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
    return safe


def runs_root(config: object = None) -> Path:
    """台账根目录：`runlog.dir` 优先，否则与 usage.jsonl 同级的 `runs/`。"""
    section = getattr(config, "runlog", None) if config is not None else None
    custom = str(getattr(section, "dir", "") or "").strip()
    if custom:
        return Path(custom).expanduser()
    src = getattr(config, "source_path", None) if config is not None else None
    if src:
        return Path(src).parent / "runs"
    return Path(tempfile.gettempdir()) / "orcsy-runs"


def state_paths(config: object = None) -> tuple[Path | None, Path | None]:
    """状态文件与迁移日志的路径（均放在配置文件同级，不随台账目录迁移）。"""
    src = getattr(config, "source_path", None) if config is not None else None
    if not src:
        return None, None
    base = Path(src).parent
    return base / STATE_FILENAME, base / JOURNAL_FILENAME


def _same_path(a: Path | str, b: Path | str) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return str(a) == str(b)


class RunRecorder:
    """单次 run 的写入句柄：事件、工件、元信息。线程安全、失败静默。"""

    def __init__(self, store: "RunStore", project: str, task_id: str, *,
                 role: str = "", agent_name: str = "", model: str = "",
                 fingerprint: str = "") -> None:
        self.store = store
        self.project = str(project)
        self.task_id = str(task_id)
        self.role = str(role or "")
        self.agent_name = str(agent_name or "")
        self.model = str(model or "")
        self.fingerprint = str(fingerprint or "")
        self.started_at = _now_iso()
        self.run_id = store.new_run_id(self.task_id)
        self.dir = store.run_dir(self.project, self.run_id)
        self._lock = threading.Lock()
        self._seq = 0
        self._event_count = 0
        self._finished = False
        self._meta: dict = {
            "run_id": self.run_id,
            "project": self.project,
            "task_id": self.task_id,
            "role": self.role,
            "agent_name": self.agent_name,
            "model": self.model,
            "fingerprint": self.fingerprint,
            "started_at": self.started_at,
            "finished_at": "",
            "outcome": "",
            "exit_reason": "",
            "event_count": 0,
            "review_verdict": "",
            "commit": "",
            "tokens": {},
        }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._write_meta()
        except OSError as e:  # 旁路：建目录失败也不能影响任务
            log.warning("运行台账目录创建失败（忽略）：%s", e)
        self.event("run_start", role=self.role, agent_name=self.agent_name,
                   model=self.model, fingerprint=self.fingerprint)

    # ---- 事件 ----

    def event(self, kind: str, **data: Any) -> None:
        if kind not in EVENT_TYPES:
            log.warning("未知运行事件类型已忽略：%s", kind)
            return
        if not self.store.enabled:
            return
        payload = _json_safe(data)
        try:
            line = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            line = "{}"
        if len(line.encode("utf-8")) > MAX_EVENT_DATA_BYTES:
            payload = {"truncated": True, "digest": hashlib.sha256(line.encode()).hexdigest()[:16]}
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": _now_iso(),
                "type": kind,
                "actor": self._actor(),
                "data": payload,
            }
            try:
                with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as e:
                log.warning("运行事件写入失败（忽略）：%s", e)
                return
            self._event_count += 1
            if self._event_count % 25 == 0:
                self._meta["event_count"] = self._event_count
                self._write_meta()

    def _actor(self) -> str:
        if self.role and self.agent_name:
            return f"{self.role}/{self.agent_name}"
        return self.role or self.agent_name or "agent"

    # ---- 语义化便捷写入（统一脱敏，调用方不必关心字段规则） ----

    def progress(self, line: str) -> None:
        self.event("progress", line=redact_text(line, 240))

    def model_event(self, model: str, prompt_tokens: int, completion_tokens: int,
                    *, turn: int = 0, duration_ms: int = 0) -> None:
        self.event("model_call", model=str(model or ""),
                   prompt_tokens=int(prompt_tokens or 0),
                   completion_tokens=int(completion_tokens or 0),
                   turn=int(turn or 0), duration_ms=int(duration_ms or 0))

    def chat_event(self, *, turn: int, content: str = "", reasoning: str = "",
                   tool_calls: list[dict] | None = None) -> None:
        """记录一轮模型消息（正文/思考/计划调用，各字段截断+脱敏）。"""
        self.event("chat", turn=int(turn or 0),
                   content=redact_text(content or "", CHAT_CONTENT_CAP),
                   reasoning=redact_text(reasoning or "", CHAT_REASONING_CAP),
                   tool_calls=tool_calls or [])

    def tool_result_event(self, name: str, *, ok: bool, result: str) -> None:
        """记录一次工具执行的结果文本（截断+脱敏）。"""
        self.event("tool_result", name=str(name or ""), ok=bool(ok),
                   result=redact_text(result or "", TOOL_RESULT_CAP))

    def tool_event(self, name: str, *, ok: bool, ms: int = 0,
                   preview: str = "", intercepted: bool = False) -> None:
        self.event("tool_call", name=str(name or ""), ok=bool(ok),
                   ms=int(ms or 0), intercepted=bool(intercepted),
                   preview=redact_text(preview, 160))

    # ---- 工件 ----

    def artifact(self, name: str, content: str | bytes, *, append: bool = False) -> None:
        """写入工件；内容超限截断。名字必须在白名单内。"""
        if name not in ARTIFACTS or name in ("meta.json", "events.jsonl"):
            return
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content or b"")
        if not data:
            return
        clipped = len(data) > MAX_ARTIFACT_BYTES
        if clipped:
            data = data[:MAX_ARTIFACT_BYTES] + (
                "\n…（工件超过 2MB，已截断）\n").encode("utf-8")
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                mode = "ab" if append else "wb"
                with open(self.dir / name, mode) as f:
                    f.write(data)
            except OSError as e:
                log.warning("运行工件写入失败（忽略）%s：%s", name, e)

    def record_command_output(self, command: str, output: str) -> None:
        """测试类命令输出追加进 test.log（普通命令不入，避免污染证据）。"""
        if not _TEST_CMD_RE.search(str(command or "")):
            return
        self.artifact("test.log", f"\n$ {redact_text(command, 500)}\n{output}\n",
                      append=True)

    # ---- 目录迁移（V1-R1）----

    def relocate(self, new_dir: Path) -> None:
        """把本 run 已写入的内容迁到新目录并重指向（迁移线程调用）。

        调用方须先持 self._lock（RunStore._migrate 负责），这里不再加锁——
        否则会与 event/artifact 的写入锁互相等待。
        """
        new_dir = Path(new_dir)
        if _same_path(self.dir, new_dir):
            return
        old_dir = self.dir
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        if old_dir.is_dir():
            if new_dir.exists():
                _merge_run_dirs(old_dir, new_dir)
            else:
                _move_dir(old_dir, new_dir)
        else:
            new_dir.mkdir(parents=True, exist_ok=True)
        self.dir = new_dir

    def close(self) -> None:
        """结束并注销活跃登记（幂等；异常路径也可调用）。"""
        try:
            self.store._unregister_recorder(self)
        except Exception:  # noqa: BLE001 — 注销失败不影响任务
            pass

    # ---- 元信息与收尾 ----

    def update_meta(self, **fields: Any) -> None:
        with self._lock:
            self._meta.update({k: _json_safe(v) for k, v in fields.items()})
            self._meta["event_count"] = self._event_count
            self._write_meta()

    def finish(self, outcome: str, exit_reason: str = "", **extra: Any) -> None:
        if self._finished:
            return
        self._finished = True
        # 先写 run_end 事件再收元信息：meta.event_count 必须与事件流实际行数一致
        self.event("run_end", outcome=outcome, exit_reason=exit_reason, **extra)
        with self._lock:
            self._meta.update({
                "finished_at": _now_iso(),
                "outcome": str(outcome or ""),
                "exit_reason": str(exit_reason or ""),
                **{k: _json_safe(v) for k, v in extra.items()},
                "event_count": self._event_count,
            })
            self._write_meta()
        self.close()

    def _write_meta(self) -> None:
        """原子写 meta.json（进程被强杀也不会留下半截 JSON）。"""
        tmp = self.dir / "meta.json.tmp"
        try:
            tmp.write_text(json.dumps(self._meta, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.dir / "meta.json")
        except OSError as e:
            log.warning("运行 meta 写入失败（忽略）：%s", e)


class RunStore:
    """一个进程一份；负责创建 run、查询回放、保留期清理与目录迁移。"""

    def __init__(self, root: Path | str, *, enabled: bool = True,
                 retention_days: int = 30, max_total_mb: int = 500,
                 state_path: Path | str | None = None,
                 journal_path: Path | str | None = None) -> None:
        self.root = Path(root)
        self.enabled = bool(enabled)
        self.retention_days = max(0, int(retention_days or 0))
        self.max_total_mb = max(0, int(max_total_mb or 0))
        self.state_path = Path(state_path) if state_path else None
        self.journal_path = Path(journal_path) if journal_path else None
        self._lock = threading.Lock()
        self._mig_lock = threading.RLock()
        self._last_prune = 0.0
        self._recorders: weakref.WeakSet = weakref.WeakSet()
        self._recorders_lock = threading.Lock()
        self._migration: dict = {
            "state": MIGRATION_IDLE, "old_root": "", "new_root": "",
            "total_runs": 0, "done_runs": 0, "total_bytes": 0,
            "done_bytes": 0, "current": "", "percent": 0.0,
            "started_at": "", "finished_at": "", "error": "",
        }
        self._migration_thread: threading.Thread | None = None
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log.warning("运行台账根目录创建失败（忽略）：%s", e)
            self.prune()

    # ---- 路径 ----

    def _project_dir(self, project: str) -> Path:
        return self.root / _safe_component(project)

    def run_dir(self, project: str, run_id: str) -> Path:
        if not _RUN_ID_RE.match(str(run_id or "")):
            raise ValueError(f"非法 run_id：{run_id!r}")
        return self._project_dir(project) / str(run_id)

    @staticmethod
    def new_run_id(task_id: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"{stamp}-{_safe_component(task_id)[:24]}-{secrets.token_hex(3)}"

    # ---- 创建 ----

    def new_run(self, project: str, task_id: str, **kwargs: Any) -> RunRecorder | None:
        if not self.enabled:
            return None
        self.maybe_prune()
        try:
            rec = RunRecorder(self, project, task_id, **kwargs)
            self._register_recorder(rec)
            return rec
        except Exception:  # noqa: BLE001 — 台账绝不能阻断任务
            log.warning("运行台账初始化失败（忽略）", exc_info=True)
            return None

    # ---- 配置与活跃 run 登记 ----

    def configure(self, *, enabled: bool | None = None,
                  retention_days: int | None = None,
                  max_total_mb: int | None = None) -> None:
        """原地更新选项（保留实例与活跃 recorder，供看板保存热生效）。"""
        with self._mig_lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if retention_days is not None:
                self.retention_days = max(0, int(retention_days or 0))
            if max_total_mb is not None:
                self.max_total_mb = max(0, int(max_total_mb or 0))
            if self.enabled:
                try:
                    self.root.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
            self.write_state()

    def set_paths(self, state_path: Path | str | None,
                  journal_path: Path | str | None) -> None:
        self.state_path = Path(state_path) if state_path else None
        self.journal_path = Path(journal_path) if journal_path else None

    def _register_recorder(self, rec: RunRecorder) -> None:
        with self._recorders_lock:
            self._recorders.add(rec)

    def _unregister_recorder(self, rec: RunRecorder) -> None:
        with self._recorders_lock:
            self._recorders.discard(rec)

    def _snapshot_recorders(self) -> list[RunRecorder]:
        with self._recorders_lock:
            return [r for r in list(self._recorders) if not r._finished]

    # ---- 状态文件（last_root） ----

    def read_state(self) -> dict:
        if self.state_path is None or not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_state(self) -> None:
        if self.state_path is None:
            return
        payload = {
            "schema_version": SCHEMA_VERSION,
            "last_root": str(self.root),
            "updated_at": _now_iso(),
        }
        _atomic_json(self.state_path, payload)

    # ---- 目录迁移 ----

    def is_migrating(self) -> bool:
        with self._mig_lock:
            return self._migration["state"] == MIGRATION_RUNNING

    def migration_status(self) -> dict:
        with self._mig_lock:
            return dict(self._migration)

    def adopt_root(self, root: Path | str) -> None:
        """直接采用一个已存在的 root（配置节缺失时的兼容路径，不迁移）。"""
        with self._mig_lock:
            self.root = Path(root)
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            self.write_state()

    def relocate(self, target: Path | str, *, enabled: bool | None = None,
                 retention_days: int | None = None,
                 max_total_mb: int | None = None,
                 journal_path: Path | str | None = None,
                 state_path: Path | str | None = None,
                 background: bool = True) -> dict:
        """切换 root 并后台迁移旧数据。校验/磁盘检查失败抛异常。"""
        target = Path(target)
        with self._mig_lock:
            if self._migration["state"] == MIGRATION_RUNNING:
                raise RuntimeError("运行台账正在迁移，请等待完成")
            old = Path(self.root)
            if _same_path(old, target):
                if journal_path is not None:
                    self.journal_path = Path(journal_path)
                if state_path is not None:
                    self.state_path = Path(state_path)
                self.configure(enabled=enabled, retention_days=retention_days,
                               max_total_mb=max_total_mb)
                return self.migration_status()
            if _is_nested(old, target):
                raise ValueError("新旧台账目录不能互相包含（会递归迁移）")
            # 目标可写探针（存在但不是目录、或没有写权限时尽快失败）
            try:
                target.mkdir(parents=True, exist_ok=True)
                probe = target / f".probe-{secrets.token_hex(4)}"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as e:
                raise ValueError(f"运行台账目录不可写：{target}（{e}）") from e
            if journal_path is not None:
                self.journal_path = Path(journal_path)
            if state_path is not None:
                self.state_path = Path(state_path)
            if enabled is not None:
                self.enabled = bool(enabled)
            if retention_days is not None:
                self.retention_days = max(0, int(retention_days or 0))
            if max_total_mb is not None:
                self.max_total_mb = max(0, int(max_total_mb or 0))
            # 先写 journal 再切 root：崩溃后按日志续跑
            self._write_journal({
                "schema_version": SCHEMA_VERSION,
                "old_root": str(old),
                "new_root": str(target),
                "state": MIGRATION_RUNNING,
                "started_at": _now_iso(),
            })
            self.root = target
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            self.write_state()
            self._migration = {
                "state": MIGRATION_RUNNING, "old_root": str(old),
                "new_root": str(target), "total_runs": 0, "done_runs": 0,
                "total_bytes": 0, "done_bytes": 0, "current": "",
                "percent": 0.0, "started_at": _now_iso(),
                "finished_at": "", "error": "",
            }
            if background:
                self._migration_thread = threading.Thread(
                    target=self._migrate, args=(old, target),
                    name="orcsy-runlog-migrate", daemon=True)
                self._migration_thread.start()
                return dict(self._migration)
            self._migrate(old, target)
            return self.migration_status()

    def retry_migration(self, *, background: bool = True) -> dict:
        """失败后按 journal 记录的原方向重试。"""
        with self._mig_lock:
            journal = self._read_journal()
            if self._migration["state"] != MIGRATION_FAILED:
                raise ValueError("当前没有失败的迁移任务")
            old, new = journal.get("old_root"), journal.get("new_root")
            if not old or not new:
                raise ValueError("迁移日志缺失，无法重试")
            # 失败后 root 已指向新目录；重试以 journal 的 old 为源
            self.root = Path(str(old))
            self._migration["state"] = MIGRATION_IDLE
        return self.relocate(new, journal_path=self.journal_path,
                             state_path=self.state_path,
                             background=background)

    def resume_if_needed(self, *, background: bool = True) -> bool:
        """启动时读取 journal：未完成则续跑。返回是否启动了迁移。"""
        journal = self._read_journal()
        if not journal or journal.get("state") == MIGRATION_DONE:
            return False
        if int(journal.get("schema_version") or 0) != SCHEMA_VERSION:
            self._update_migration(state=MIGRATION_FAILED,
                                   error="迁移日志版本不受支持，请手动处理")
            return False
        old = Path(str(journal.get("old_root") or ""))
        new = Path(str(journal.get("new_root") or ""))
        if not str(old) or not str(new):
            self._update_migration(state=MIGRATION_FAILED,
                                   error="迁移日志缺少目录信息")
            return False
        with self._mig_lock:
            if self._migration["state"] == MIGRATION_RUNNING:
                return False
            self.root = new
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            self.write_state()
            self._migration.update({
                "state": MIGRATION_RUNNING, "old_root": str(old),
                "new_root": str(new), "started_at": _now_iso(),
                "finished_at": "", "error": "", "percent": 0.0,
                "done_runs": 0, "done_bytes": 0, "current": "",
            })
            if background:
                self._migration_thread = threading.Thread(
                    target=self._migrate, args=(old, new),
                    name="orcsy-runlog-migrate", daemon=True)
                self._migration_thread.start()
                return True
            self._migrate(old, new)
            return True

    # ---- 迁移实现 ----

    def _migrate(self, old: Path, new: Path) -> None:
        try:
            # 上次中断可能留下目标侧半成品：先清理再搬（源侧由 _scan_runs 清）
            _clean_partials(new)
            runs = self._scan_runs(old)
            total_bytes = sum(r["size"] for r in runs)
            self._update_migration(state=MIGRATION_RUNNING,
                                   total_runs=len(runs),
                                   total_bytes=total_bytes,
                                   current="", error="")
            free = _free_bytes(new)
            if free and free < total_bytes * 1.1 and not _same_volume(old, new):
                raise OSError(
                    f"目标目录剩余空间不足：需要约 {total_bytes * 1.1 / 1048576:.1f} MB，"
                    f"当前 {free / 1048576:.1f} MB")

            # P1 活跃 run：暂停写入 → 复制 → 重指向（seq 在内存中继续）
            for rec in self._snapshot_recorders():
                with rec._lock:
                    if rec._finished or not rec.dir.is_dir():
                        continue
                    if not _is_under(rec.dir, old):
                        continue
                    new_dir = new / rec.dir.parent.name / rec.dir.name
                    try:
                        rec.relocate(new_dir)
                        self._update_migration(
                            done_runs=self._migration["done_runs"] + 1,
                            done_bytes=self._migration["done_bytes"]
                            + _dir_size(new_dir),
                            current=f"{rec.project}/{rec.run_id}")
                    except OSError as e:
                        log.warning("活跃 run 迁移失败（任务继续执行）：%s", e)

            # P2 其余 run（含迁移期间刚结束、已转入旧目录的）
            for run in self._scan_runs(old):
                src, project, run_id = run["path"], run["project"], run["run_id"]
                dst = new / project / run_id
                self._update_migration(current=f"{project}/{run_id}")
                try:
                    if dst.exists():
                        _merge_run_dirs(src, dst)
                    else:
                        _move_dir(src, dst)
                except OSError as e:
                    # 单个 run 失败不终止整体：保留源目录，记录后继续
                    log.warning("run 迁移失败（保留原目录）：%s：%s", src, e)
                    self._migration["error"] = f"{src.name}: {e}"
                    continue
                self._update_migration(
                    done_runs=self._migration["done_runs"] + 1,
                    done_bytes=self._migration["done_bytes"] + run["size"])
                _test_delay()

            _remove_empty_project_dirs(old)
            self._update_migration(state=MIGRATION_DONE, percent=100.0,
                                   current="", finished_at=_now_iso())
            self.write_state()
            self._delete_journal()
        except Exception as e:  # noqa: BLE001 — 迁移失败保留数据，状态可查
            log.exception("运行台账目录迁移失败")
            self._update_migration(state=MIGRATION_FAILED, error=str(e),
                                   finished_at=_now_iso())
            self._write_journal({
                "schema_version": SCHEMA_VERSION,
                "old_root": str(old), "new_root": str(new),
                "state": MIGRATION_FAILED, "error": str(e),
                "started_at": self._migration.get("started_at") or _now_iso(),
            })

    @staticmethod
    def _scan_runs(root: Path) -> list[dict]:
        out: list[dict] = []
        if not root.is_dir():
            return out
        for pdir in sorted(p for p in root.iterdir() if p.is_dir()):
            if pdir.name == _CONFLICT_DIR:
                continue
            for rdir in sorted(p for p in pdir.iterdir() if p.is_dir()):
                if rdir.name.endswith(_PARTIAL_SUFFIX):
                    shutil.rmtree(rdir, ignore_errors=True)
                    continue
                out.append({"project": pdir.name, "run_id": rdir.name,
                            "path": rdir, "size": _dir_size(rdir)})
        return out

    def _update_migration(self, **fields: Any) -> None:
        with self._mig_lock:
            self._migration.update(fields)
            total_r = int(self._migration.get("total_runs") or 0)
            done_r = int(self._migration.get("done_runs") or 0)
            total_b = int(self._migration.get("total_bytes") or 0)
            done_b = int(self._migration.get("done_bytes") or 0)
            if total_b > 0:
                pct = done_b / total_b * 100
            elif total_r > 0:
                pct = done_r / total_r * 100
            else:
                pct = 0.0
            if self._migration.get("state") == MIGRATION_DONE:
                self._migration["percent"] = 100.0
            else:
                self._migration["percent"] = round(
                    min(99.9, max(0.0, pct)), 1)

    # ---- journal ----

    def _read_journal(self) -> dict:
        if self.journal_path is None or not self.journal_path.is_file():
            return {}
        try:
            data = json.loads(self.journal_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_journal(self, payload: dict) -> None:
        if self.journal_path is None:
            return
        _atomic_json(self.journal_path, payload)

    def _delete_journal(self) -> None:
        if self.journal_path is None:
            return
        try:
            self.journal_path.unlink(missing_ok=True)
        except OSError:
            pass


    # ---- 查询 ----

    def _iter_meta(self, project: str | None = None):
        if not self.root.is_dir():
            return
        project_dirs = ([self._project_dir(project)] if project
                        else sorted(p for p in self.root.iterdir() if p.is_dir()))
        for pdir in project_dirs:
            if not pdir.is_dir():
                continue
            for rdir in sorted((p for p in pdir.iterdir() if p.is_dir()), reverse=True):
                meta_path = rdir / "meta.json"
                if not meta_path.is_file():
                    continue
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(meta, dict):
                    meta.setdefault("run_id", rdir.name)
                    yield meta, rdir

    def list_runs(self, project: str | None = None, task_id: str = "",
                  limit: int = 50) -> list[dict]:
        if not self.enabled:
            return []
        out: list[dict] = []
        want = str(task_id or "").strip()
        for meta, rdir in self._iter_meta(project):
            if want and str(meta.get("task_id") or "") != want:
                continue
            out.append({
                "run_id": meta.get("run_id") or rdir.name,
                "project": meta.get("project") or "",
                "task_id": meta.get("task_id") or "",
                "role": meta.get("role") or "",
                "agent_name": meta.get("agent_name") or "",
                "model": meta.get("model") or "",
                "started_at": meta.get("started_at") or "",
                "finished_at": meta.get("finished_at") or "",
                "outcome": meta.get("outcome") or "",
                "exit_reason": meta.get("exit_reason") or "",
                "event_count": int(meta.get("event_count") or 0),
                "review_verdict": meta.get("review_verdict") or "",
                "commit": meta.get("commit") or "",
                "tokens": meta.get("tokens") or {},
            })
        out.sort(key=lambda r: (r["started_at"], r["run_id"]), reverse=True)
        return out[:max(1, min(int(limit or 50), 500))]

    def load_run(self, project: str, run_id: str, *, offset: int = 0,
                 limit: int = 2000) -> dict:
        rdir = self.run_dir(project, run_id)
        meta_path = rdir / "meta.json"
        if not meta_path.is_file():
            raise FileNotFoundError("运行记录不存在")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"运行记录损坏：{e}") from e
        events: list[dict] = []
        total = 0
        ev_path = rdir / "events.jsonl"
        if ev_path.is_file():
            try:
                with open(ev_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        total += 1
                        if total <= max(0, int(offset)):
                            continue
                        if len(events) >= max(1, min(int(limit or 2000), 5000)):
                            continue
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                pass
        artifacts = []
        for name in ARTIFACTS:
            p = rdir / name
            if p.is_file():
                try:
                    artifacts.append({"name": name, "size": p.stat().st_size})
                except OSError:
                    pass
        return {"meta": meta, "events": events, "event_total": total,
                "offset": int(offset), "artifacts": artifacts}

    def read_artifact(self, project: str, run_id: str, name: str) -> tuple[bytes, str]:
        if name not in ARTIFACTS:
            raise ValueError("工件名不在白名单内")
        p = self.run_dir(project, run_id) / name
        if not p.is_file():
            raise FileNotFoundError("工件不存在")
        mime = "application/json" if name.endswith(".json") else \
               ("text/x-diff" if name.endswith(".patch") else "text/plain")
        return p.read_bytes(), mime

    # ---- 保留期清理 ----

    def maybe_prune(self) -> None:
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        self.prune()

    def prune(self) -> None:
        """按保留天数与总占用清理最旧的 run；迁移期间不清理。"""
        if not self.enabled or not self.root.is_dir() or self.is_migrating():
            return
        cutoff = (time.time() - self.retention_days * 86400
                  if self.retention_days > 0 else 0)
        runs: list[tuple[float, Path]] = []
        for pdir in self.root.iterdir():
            if not pdir.is_dir():
                continue
            for rdir in pdir.iterdir():
                if not rdir.is_dir():
                    continue
                try:
                    mtime = rdir.stat().st_mtime
                except OSError:
                    continue
                if cutoff and mtime < cutoff:
                    shutil.rmtree(rdir, ignore_errors=True)
                    continue
                runs.append((mtime, rdir))
        if self.max_total_mb <= 0:
            return
        limit = self.max_total_mb * 1024 * 1024
        total = _dir_size(self.root)
        for _mtime, rdir in sorted(runs):          # 最旧的先删
            if total <= limit:
                break
            size = _dir_size(rdir)
            shutil.rmtree(rdir, ignore_errors=True)
            total -= size


def _test_delay() -> None:
    """测试钩子：每个 run 迁移后延迟（仅用于手工/端到端观察进度条）。"""
    try:
        delay = float(os.environ.get("ORCSY_TEST_MIGRATE_DELAY", "0") or 0)
    except ValueError:
        return
    if delay > 0:
        time.sleep(min(delay, 5.0))


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


# ---- 迁移辅助（文件系统级） ----

def _atomic_json(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("台账状态文件写入失败（忽略）：%s：%s", path, e)


def _is_under(child: Path | str, parent: Path | str) -> bool:
    try:
        c = Path(child).expanduser().resolve()
        p = Path(parent).expanduser().resolve()
    except OSError:
        return False
    return c == p or p in c.parents


def _is_nested(a: Path | str, b: Path | str) -> bool:
    return _is_under(a, b) or _is_under(b, a)


def _same_volume(a: Path, b: Path) -> bool:
    try:
        da = os.stat(Path(a).exists() and a or a.parent).st_dev
        db = os.stat(Path(b).exists() and b or b.parent).st_dev
        return da == db
    except OSError:
        return False


def _free_bytes(path: Path) -> int:
    try:
        return int(shutil.disk_usage(str(path)).free)
    except OSError:
        return 0


def _clean_partials(root: Path) -> None:
    """清理目标侧上次中断遗留的 `<run_id>.migrating` 目录。"""
    if not root.is_dir():
        return
    try:
        for pdir in root.iterdir():
            if not pdir.is_dir():
                continue
            for child in list(pdir.iterdir()):
                if child.is_dir() and child.name.endswith(_PARTIAL_SUFFIX):
                    shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass


def _remove_empty_project_dirs(old: Path) -> None:
    if not old.is_dir():
        return
    try:
        for pdir in list(old.iterdir()):
            if not pdir.is_dir():
                continue
            if pdir.name == _CONFLICT_DIR:
                continue
            try:
                for child in list(pdir.iterdir()):
                    if child.is_dir() and child.name.endswith(_PARTIAL_SUFFIX):
                        shutil.rmtree(child, ignore_errors=True)
                if not any(pdir.iterdir()):
                    pdir.rmdir()
            except OSError:
                pass
    except OSError:
        pass


def _move_dir(src: Path, dst: Path) -> None:
    """把一个 run 目录原子地放到目标位置；跨盘/占用时退化为复制+改名。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        _merge_run_dirs(src, dst)
        return
    try:
        os.replace(src, dst)
        return
    except OSError:
        pass
    partial = dst.with_name(dst.name + _PARTIAL_SUFFIX)
    shutil.rmtree(partial, ignore_errors=True)
    try:
        shutil.copytree(src, partial)
        os.replace(partial, dst)
    except OSError:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    shutil.rmtree(src, ignore_errors=True)


def _merge_run_dirs(src: Path, dst: Path) -> None:
    """同一 run_id 两边都存在：事件按 seq 合并，工件取更完整/更新的一份。"""
    dst.mkdir(parents=True, exist_ok=True)
    src_ev, dst_ev = src / "events.jsonl", dst / "events.jsonl"
    if src_ev.is_file() or dst_ev.is_file():
        _merge_events(src_ev, dst_ev)
    # meta：取事件更多 / 已结束的一份（没有则保留目标侧）
    src_meta = _read_json(src / "meta.json")
    dst_meta = _read_json(dst / "meta.json")
    meta = dst_meta or src_meta
    if src_meta:
        if int(src_meta.get("event_count") or 0) > int(meta.get("event_count") or 0):
            meta = src_meta
        elif src_meta.get("finished_at") and not meta.get("finished_at"):
            meta = src_meta
    if meta:
        _atomic_json(dst / "meta.json", meta)
    for name in ARTIFACTS:
        if name in ("meta.json", "events.jsonl"):
            continue
        s, d = src / name, dst / name
        if not s.is_file():
            continue
        if not d.is_file():
            shutil.copy2(s, d)
            continue
        if name == "test.log":
            if s.stat().st_size > d.stat().st_size:
                shutil.copy2(s, d)
        elif s.stat().st_mtime > d.stat().st_mtime:
            shutil.copy2(s, d)
    shutil.rmtree(src, ignore_errors=True)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _merge_events(src: Path, dst: Path) -> None:
    """events.jsonl 按 seq 取并集（目标侧优先），排序后原子重写。"""
    entries: dict[int, dict] = {}
    for path, prefer in ((src, False), (dst, True)):
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(item, dict):
                        continue
                    seq = int(item.get("seq") or 0)
                    if seq <= 0:
                        continue
                    if prefer or seq not in entries:
                        entries[seq] = item
        except OSError:
            continue
    if not entries:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for seq in sorted(entries):
            f.write(json.dumps(entries[seq], ensure_ascii=False) + "\n")
    os.replace(tmp, dst)


# ---- 模块级单例（与 usage.py 同风格，便于 Web/调度共用） ----

_STORE: RunStore | None = None
_STORE_LOCK = threading.Lock()


def init_run_store(config: object = None, *, force: bool = False) -> RunStore:
    """按配置初始化进程级台账（幂等）。

    启动时额外处理两件事（见设计文档 13.3）：
    1. 有未完成的迁移日志 → 后台续跑；
    2. 配置里的目录与 `runlog-state.json` 的 last_root 不一致 → 自动迁移
       （配置整节缺失时优先保留 last_root，防止旧版本写丢配置导致数据错位）。
    """
    global _STORE
    section = getattr(config, "runlog", None) if config is not None else None
    target = runs_root(config)
    sp, jp = state_paths(config)
    with _STORE_LOCK:
        if _STORE is not None and not force and _same_path(_STORE.root, target):
            _STORE.set_paths(sp, jp)
            _STORE.configure(
                enabled=bool(getattr(section, "enabled", True)),
                retention_days=int(getattr(section, "retention_days", 30) or 0),
                max_total_mb=int(getattr(section, "max_total_mb", 500) or 0),
            )
            return _STORE
        _STORE = RunStore(
            target,
            enabled=bool(getattr(section, "enabled", True)),
            retention_days=int(getattr(section, "retention_days", 30) or 0),
            max_total_mb=int(getattr(section, "max_total_mb", 500) or 0),
            state_path=sp, journal_path=jp,
        )
        store = _STORE
    _boot_store(store, section)
    return store


def _boot_store(store: RunStore, section: object) -> None:
    """启动期：先续跑未完成迁移，再比较 last_root 与配置目录。"""
    try:
        if store.resume_if_needed(background=True):
            return
        state = store.read_state()
        last = str(state.get("last_root") or "").strip()
        explicit = bool(getattr(section, "dir_explicit", False))
        if not last:
            store.write_state()
            return
        last_path = Path(last)
        if not explicit:
            # 配置里没有 runlog 节：保留 last_root，不回退默认目录
            if not _same_path(last_path, store.root) and last_path.is_dir():
                store.adopt_root(last_path)
                # 同步回配置对象：否则看板任意保存会把「无 dir」写回，
                # 下次启动又被判定为回默认，触发反向迁移
                try:
                    section.dir = str(last_path)
                    section.dir_explicit = True
                except Exception:  # noqa: BLE001 — 配置对象只读/缺失时忽略
                    pass
            else:
                store.write_state()
            return
        if _same_path(last_path, store.root):
            store.write_state()
            return
        if last_path.is_dir() and any(last_path.iterdir()):
            # relocate 以「当前 root」为源：启动时当前 root 已是配置目标，
            # 这里先把 last_root 临时作为源，再切回配置目录并迁移。
            configured = store.root
            store.root = last_path
            store.relocate(
                configured,
                enabled=bool(getattr(section, "enabled", True)),
                retention_days=int(getattr(section, "retention_days", 30) or 0),
                max_total_mb=int(getattr(section, "max_total_mb", 500) or 0),
                background=True,
            )
        else:
            store.write_state()
    except Exception:  # noqa: BLE001 — 启动期迁移失败不阻断程序
        log.exception("运行台账启动迁移检查失败（数据保留在原目录）")
        store._update_migration(state=MIGRATION_FAILED,
                                error="启动迁移检查失败，请查日志")


def apply_runlog_config(config: object) -> dict:
    """看板保存 runlog 配置：同目录只更新选项，不同目录触发异步迁移。"""
    store = get_run_store()
    section = getattr(config, "runlog", None)
    target = runs_root(config)
    sp, jp = state_paths(config)
    store.set_paths(sp, jp)
    opts = {
        "enabled": bool(getattr(section, "enabled", True)),
        "retention_days": int(getattr(section, "retention_days", 30) or 0),
        "max_total_mb": int(getattr(section, "max_total_mb", 500) or 0),
    }
    if _same_path(store.root, target):
        store.configure(**opts)
        return store.migration_status()
    return store.relocate(target, journal_path=jp, state_path=sp,
                          background=True, **opts)


def get_run_store() -> RunStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = RunStore(Path(tempfile.gettempdir()) / "orcsy-runs")
        return _STORE
