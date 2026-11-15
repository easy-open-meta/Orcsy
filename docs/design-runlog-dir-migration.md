# 运行台账目录迁移设计方案

> 版本 V1.0 ｜ 拟制 2026-11-16 ｜ 关联：R1 运行台账（`orcsy/runlog.py`）
> 状态：已实施（S1–S6，分支 `feat/trusted-pipeline-r1`，待合并）
> 实现：`orcsy/runlog.py`（registry / relocate / journal / last_root）、
> `settings_service` 校验与拒绝、`/api/runlog/migration`、看板进度与锁定；
> 测试：`tests/test_runlog_migration.py`（8 组用例）。

---

## 1 目标与约束

用户在看板「设置 → 运行台账」修改 `runlog.dir` 后：

1. 旧目录中的历史 run **自动迁移**到新目录，不需要手工拷贝；
2. 迁移在**后台异步线程**执行，不阻塞调度器与看板；
3. 迁移期间，运行台账设置区的控件（开关 / 目录 / 浏览 / 保存）与任务详情里的 run 回放按钮**全部禁用**，并显示**实时进度**；迁移完成（或失败并明确处置）后才能继续操作；
4. 修改目录时若有任务正在执行，其 run 已经写入旧目录、之后仍会继续产生事件；迁移结束后该 run 必须仍是**一个完整、事件序号连续**的记录，不能分裂成两份；
5. 迁移可中断、可恢复：进程崩溃/断电后重启能继续，不产生重复或不完整目录；
6. 失败不丢数据：旧目录在迁移成功前不被删除。

不变量：

- **I1 单一 root**：任一时刻 `RunStore.root` 唯一，新 run 只写入当前 root。
- **I2 事件连续**：同一 `run_id` 的 `events.jsonl` 中 `seq` 从 1 连续递增。
- **I3 原子可见**：一个 run 目录在目标位置要么完整出现，要么完全不出现。
- **I4 不留半成品**：迁移完成后旧 root 下不再有 run 目录（空项目目录可保留/清理）。
- **I5 只读不阻塞**：迁移期间不阻塞任务执行，仅阻塞台账配置修改与回放读取。

---

## 2 总体流程

```
用户保存新 dir
   │ 校验（绝对路径/非嵌套/不在受管项目内/可写/磁盘余量）
   ▼
AppState：若目标 root == 当前 root → 只更新开关/保留期（现有逻辑）
   │ 否则进入迁移
   ▼
RunStore.relocate(new_root, options)          ← 同步完成「切换 + 记日志」
   ├─ 写迁移日志 <config_dir>/runlog-migration.json（old/new/options/state）
   ├─ self.root = new_root（此后新 run 直接写新目录）
   ├─ 启动后台线程 _migrate(old, new)
   ▼
后台线程（daemon）
   ├─ P1 活跃 run：暂停写入 → 复制 → 重指向 → 恢复写入
   ├─ P2 剩余 run：逐个移动（同盘 rename 优先，跨盘 copy+delete）
   ├─ P3 收尾：删除旧目录、清理空项目目录、状态置 done、删日志
   ▼
前端轮询 /api/runlog/migration → 进度条 → done 后解锁并刷新列表
```

**为什么先把 root 切到新目录再搬旧数据**：迁移期间随时可能有新任务被调度。若等搬完再切，新任务会写进旧目录，等于把「正在搬的源」继续追加内容，永远搬不完；先切 root 后，新 run 天然落在新目录，后台只需处理旧存量与活跃 recorder 的重指向。

---

## 3 模块改造

### 3.1 `orcsy/runlog.py`

新增：

| 成员 | 说明 |
| --- | --- |
| `RunStore._recorders` | `weakref.WeakSet[RunRecorder]` + 锁；`new_run` 注册，`finish` 注销。用弱引用避免异常路径泄漏 |
| `RunStore.configure(enabled, retention_days, max_total_mb)` | 原地更新选项，不重建实例（保留活跃 recorder） |
| `RunStore.relocate(target, *, options, journal_path, background=True)` | 校验 → 切 root → 写日志 → 起线程 |
| `RunStore.migration_status()` | 返回状态快照（供 Web 层） |
| `RunStore.resume_if_needed(journal_path)` | 启动时读取日志；未完成则续跑 |
| `RunRecorder.relocate(new_dir)` | 持锁复制/移动自身目录并改 `self.dir` |
| `RunRecorder.close()` | 幂等结束：注销注册表（`finish` 内部调用） |

迁移状态（模块级，单例）：

```python
{
  "state": "idle|scanning|running|done|failed",
  "old_root": "...", "new_root": "...",
  "total_runs": 12, "done_runs": 3,
  "total_bytes": 104857600, "done_bytes": 31457280,
  "current": "demo/20261116-101500-T-1-a1b2",
  "percent": 30.0, "started_at": "...", "finished_at": "",
  "error": ""
}
```

### 3.2 `orcsy/executor.py`

无需改动：`RunRecorder` 内部完成目录重指向。仅确认 `run_task` 收尾仍调用 `finish()`（幂等、内部注销）。

### 3.3 `orcsy/web.py` / `settings_service.py`

- `settings_service.save_settings`：
  - 迁移进行中且请求包含 `runlog` → `ValueError("运行台账正在迁移，请等待完成后再修改")`（400）；
  - 目录变化不再走 `init_run_store(force=True)`，改为交给 `AppState.apply_runlog_config(new_cfg)`。
- `AppState.apply_runlog_config(cfg)`：
  - 目标 root 与当前 root 相同 → `store.configure(...)`；
  - 不同 → 校验后 `store.relocate(...)`。
- `_apply_config` 中对 `init_run_store` 的调用替换为上述方法。
- 新增 `GET /api/runlog/migration`（需登录）：返回 3.1 的状态。
- `full_state` 附带同一状态（供任务详情页禁用按钮）。

### 3.4 前端

`SettingsView.vue`：

- 新增 `runlogBusy = computed(() => ['scanning','running'].includes(mig.state))`；
- 开关、保留天数、占用上限、目录输入、浏览、保存按钮全部 `:disabled="runlogBusy"`；
- 迁移中显示：`NProgress` + `正在迁移 3/12 个 run · 30/100 MB · 当前 demo/2026...`；
- 迁移中每 1s 轮询 `/api/runlog/migration`，结束时清除定时器并 `refresh(true)`；
- 失败：显示错误、提供「重试迁移」（以当前配置再次触发同向迁移）与「改回原目录」（触发反向迁移）；失败态允许修改目录，避免卡死。

`Dialogs.vue`（任务详情）：

- `runlogBusy` 时禁用「刷新」「run 行展开」与工件按钮，显示「运行台账迁移中，暂不可回放」；
- 轮询沿用 `store.state.runlog_migration`（现有全量轮询）即可，不额外加请求。

`api.ts`：新增 `runlogMigration()` 与 `AppStateT.runlog_migration` 类型。

---

## 4 关键算法

### 4.1 活跃 run 的合并（核心）

活跃 run 指迁移开始时 `RunStore._recorders` 中尚未 `finish` 的 recorder。

```
for rec in snapshot(recorders):
    with rec.lock:                       # 暂停该 run 的事件/工件写入
        old_dir = rec.dir
        if not old_dir.exists(): continue
        new_dir = target/rec.project/rec.run_id
        copy_run(old_dir, new_dir, merge=True)   # 目录已存在时按规则合并
        rec.dir = new_dir                # 重指向；seq/_meta 仍在内存中继续
    # 释放锁后，后续事件直接 append 到新目录，seq 连续
```

要点：

- `rec._seq` 在内存里，复制后继续 append，天然连续；
- `meta.json` 会随复制带过去，`finish()` 时原子重写，含最终 outcome；
- `test.log` 是 append 工件，先复制再追加，不丢前文；
- `diff.patch / usage.json / review.json` 通常在收尾阶段才写，重指向后直接落新目录；
- 若复制时目标已存在（崩溃恢复/重试），走 4.3 的合并规则。

### 4.2 非活跃 run 的移动

```
for (project, run_id) in scan(old_root):
    src, dst = old/proj/run_id, new/proj/run_id
    if not src.exists(): continue
    if dst.exists(): merge_run(src, dst)
    else:
        try: os.replace(src, dst)          # 同盘原子改名（快）
        except OSError:                    # 跨盘 / 文件被占用
            copy_run(src, dst, merge=False)  # 先复制到 dst.migrating 再改名
            shutil.rmtree(src)
    progress.done += size(src)
```

`copy_run` 统一流程：复制到 `<run_id>.migrating` → `os.replace` 到最终名 → 删源。保证 I3。

### 4.3 合并规则（同 run_id 两边都存在）

| 文件 | 规则 |
| --- | --- |
| `events.jsonl` | 两边逐行解析，按 `seq` 取并集（相同 seq 取目标侧）；按 seq 排序重写 |
| `meta.json` | 取 `finished_at` 较新者；都未完成取目标侧（其事件更全） |
| `test.log` | 两边内容拼接（去重空行） |
| `diff.patch` / `review.json` / `usage.json` | 取 mtime 较新者 |
| `.migrating` 残留 | 直接删除后重做该 run |

### 4.4 崩溃恢复

- 日志文件 `<config_dir>/runlog-migration.json` 在切 root **之前**写入，包含 `old_root / new_root / options / state`；
- 启动时 `init_run_store` 检测到日志且 `state != done` → 后台续跑：
  1. 重新扫描 `old_root`（已搬走的自然不存在）；
  2. 处理目标侧 `.migrating` 残留；
  3. 完成后删日志。
- 若 `old_root` 不存在或为空 → 直接 `done`。
- 若日志损坏 → 标记 `failed`，保留旧目录，前端提示人工处理（不自动删任何数据）。

### 4.5 进度与限流

- 迁移前扫描所有 run 目录统计 `total_runs / total_bytes`（一次 rglob，可接受）；
- 每完成一个 run 更新 `done_runs / done_bytes`；大 run 内部按文件追加字节；
- 状态更新节流 200ms，避免频繁写内存；
- 迁移期间禁用 `prune()`（避免把正在迁移的 run 删掉）；`done` 后下一次正常 prune。

---

## 5 校验与冲突策略

保存新目录时依次校验（`settings_service` + `RunStore` 双重）：

1. 绝对路径、无换行/空字符（已有）；
2. 目标不是已存在的文件（已有）；
3. 目标与当前 root 相同 → 不迁移，只更新选项；
4. **禁止嵌套**：`new` 在 `old` 内或 `old` 在 `new` 内 → 拒绝（否则递归/自搬运）；
5. **禁止落在受管项目目录内**：`new` 等于或在任 `cfg.projects[].path` 之下 → 拒绝（台账不应污染代码仓库，与默认设计一致）；
6. 可写探针：`mkdir` + 写临时文件再删；
7. 磁盘余量：跨盘且 `free < total_bytes * 1.1` → 拒绝并提示所需空间；
8. 迁移进行中再次修改 → 拒绝（400）。

同一路径已存在同名 run：

- 属于活跃 run → 按 4.3 合并（正常路径）；
- 属于历史 run 且两边事件不同 → 按 4.3 合并；无法解析的行保留在目标侧；
- run_id 撞车但内容完全不相关 → 事件仍按 `seq` 取并集（同 seq 取目标侧），工件取更新/更完整的一份；不静默丢数据，也不额外生成冲突目录。

---

## 6 接口

`GET /api/runlog/migration`：

```json
{
  "ok": true,
  "migration": {
    "state": "running",
    "old_root": "C:\\...\\runs",
    "new_root": "D:\\orcsy-runs",
    "total_runs": 12, "done_runs": 3,
    "total_bytes": 104857600, "done_bytes": 31457280,
    "current": "demo/20261116-101500-T-1-a1b2",
    "percent": 23.4,
    "started_at": "2026-11-16T10:15:00+08:00",
    "finished_at": "", "error": ""
  }
}
```

`POST /api/runlog/migration/retry`：`failed` 状态下以同一 `old→new` 重新执行；其他状态返回 400。

`save_settings` 在迁移中携带 `runlog` 键 → 400，message 明确。

---

## 7 并发与锁顺序

统一锁顺序（避免死锁）：

```
RunStore._mig_lock  →  RunStore._lock  →  RunRecorder._lock
```

- `relocate` 持 `_mig_lock` 完成「校验 + 切 root + 写日志 + 起线程」，立即返回；
- 后台线程在移动每个 run 时取 `_lock` 更新注册表快照；
- 活跃 run 处理时再取对应 `rec._lock`；
- `RunRecorder.event/artifact` 只取自身 `_lock`，不与 store 锁嵌套；
- `get_run_store()` / `new_run()` 在迁移中正常工作（root 已切换）。

---

## 8 前端交互细节

| 状态 | 设置区控件 | 进度展示 | 任务详情回放 |
| --- | --- | --- | --- |
| idle | 可用 | 无 | 可用 |
| scanning / running | **全部禁用** | 进度条 + 计数 + 字节 + 当前 run | 禁用，提示迁移中 |
| done | 恢复可用 | 一次性成功提示后隐藏 | 恢复可用，自动刷新 |
| failed | 允许改目录（便于回退） | 错误信息 + 「重试迁移」「改回原目录」 | 禁用，提示迁移失败 |

- 进度轮询 1s，仅 `scanning/running` 时启动；组件卸载/切分区时清理定时器；
- 迁移完成/失败用 `window.$message` 提示一次（用状态迁移去重，避免轮询重复弹）；
- 移动端布局沿用现有单列栅格，进度条与按钮同宽。

---

## 9 测试与验收

### 9.1 单元/集成（新增 `tests/test_runlog_migration.py`）

| 用例 | 断言 |
| --- | --- |
| 历史 run 迁移 | 新 root 可被 `list_runs/load_run/read_artifact` 读到；旧目录不存在 |
| 活跃 run 合并 | 迁移前写 2 条事件 → `relocate` → 再写 2 条 → `finish`；新目录事件 seq = 1..N 连续，旧目录消失，`meta.outcome` 正确 |
| 跨盘 fallback | 猴子补丁让 `os.replace` 抛 OSError；仍完成且内容一致 |
| 崩溃恢复 | 手工留下 `.migrating` 与部分已迁移 run + 日志；`resume_if_needed` 后续跑完成，无重复 |
| 同 run_id 合并 | 两侧 events 各含不同 seq；合并后 seq 连续且无丢行 |
| 校验 | 嵌套目录 / 指向文件 / 受管项目内 / 相对路径 / 迁移中再改 → 全部拒绝 |
| 配置与热切换 | `save_settings` 改目录 → 返回后立即 `store.root == new`；新 run 落新目录 |
| HTTP | 迁移中 `GET /api/runlog/migration` 返回 running；`save_settings` 400 |

测试用 `relocate(..., background=False)`（或 `wait=True`）保证确定性，不依赖线程调度。

### 9.2 手工验收

1. 准备两个磁盘目录与若干历史 run；
2. 在看板改目录 → 观察进度条、控件禁用、任务详情回放禁用；
3. 迁移中启动一个任务（可用假模型长任务）→ 完成后确认该 run 只有一个目录、事件连续；
4. 迁移完成后刷新列表，所有历史 run 可回放；
5. 迁移中强杀进程 → 重启后自动续跑并完成。

---

## 10 实施步骤

| 步骤 | 内容 | 预估 |
| --- | --- | --- |
| S1 | `RunStore` 注册表 / configure / 状态 / 日志、`RunRecorder.relocate/close` | 0.5 天 |
| S2 | `copy_run / merge_run / move` 与进度、同盘 rename + 跨盘 fallback、崩溃恢复 | 1 天 |
| S3 | `AppState.apply_runlog_config` + settings_service 校验/拒绝 + Web 状态接口 | 0.5 天 |
| S4 | SettingsView 进度/禁用/轮询；Dialogs 禁用；重新 build + gen_page | 0.5 天 |
| S5 | 测试（含 HTTP/崩溃恢复）+ 手工浏览器验证 | 0.5 天 |
| S6 | 文档更新（本方案标记已实施、roadmap 备注） | 0.2 天 |

合计约 3 人日。

---

## 11 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 迁移大目录耗时长 | 用户以为卡死 | 进度条 + 当前 run + 字节数；后台线程不阻塞任务 |
| 跨盘复制中断 | 半成品目录 | `.migrating` 后缀 + 原子改名 + 启动清理 |
| 活跃 run 正写文件 | Windows 无法 rename/复制不一致 | 先取 recorder 锁暂停写入，再复制并重指向 |
| 目标盘空间不足 | 复制失败 | 迁移前估算并拒绝；失败保留旧数据 |
| 用户连续改目录 | 迁移叠迁移 | 全局单迁移锁，迁移中拒绝 |
| 迁移中调度器 prune | 误删 | 迁移期间禁用 prune |
| 崩溃后配置已指向新目录但数据未搬完 | 看板暂时看不到部分 run | 日志驱动续跑；UI 显示迁移中 |
| 目录落在项目内 | 污染仓库/触发 git 变更 | 保存时校验拒绝 |

---

## 12 明确不做

- 不做多实例/多进程同时迁移（单实例产品假设）；
- 不做自动回滚（提供「改回原目录」触发反向迁移即可）；
- 不做跨机器/网络存储的断点续传优化（按文件级重试）；
- 不迁移旧目录中非本模块产生的文件（只处理 `runs/<project>/<run_id>/` 结构，其余原样保留并提示）。

---

## 13 兼容与无缝升级

### 13.1 升级 / 降级矩阵

| 场景 | 行为 | 是否无缝 |
| --- | --- | --- |
| 0.24（无台账能力）→ 新版本 | 首次启动记录 `last_root = configured`，不迁移；新建 runs 目录并开始记录 | ✅ |
| 新版本 → 新版本（已有 run 数据） | 目录结构、事件字段不变，直接读取 | ✅ |
| 旧配置无 `runlog` 节 | 全部字段取默认值；看板保存时补全写回，不影响其它节 | ✅ |
| 已配置自定义 `dir` | 启动时 `last_root == configured`，不触发迁移 | ✅ |
| 看板改目录 | 异步迁移 + 进度 + UI 锁定（本方案主线） | ✅ |
| 手工改 `config.yaml` 后重启 | `last_root != configured` → 后台自动迁移（13.3 情况 C） | ✅ |
| 迁移中崩溃 / 断电 | journal 续跑，不重复、不丢数据 | ✅ |
| 同时升级运行中的进程 | 需重启进程；`in_progress` 任务按现有启动恢复逻辑重新排队，已有 run 记录不受影响 | ⚠️ 需重启 |
| 降级到旧版本 | 旧版本不认识 `runlog` 节，run 文件保留但可能被旧版保存配置时写丢该节 | ⚠️ 见 13.5 |

### 13.2 `last_root` 状态文件

位置：与 `config.yaml` 同目录的 `runlog-state.json`（不放进 `runs/`，避免随目录迁移）。

```json
{
  "schema_version": 1,
  "last_root": "C:/Users/xxx/AppData/Roaming/Orcsy/runs",
  "updated_at": "2026-11-16T10:00:00+08:00"
}
```

- 每次成功切换 root（迁移完成、或首次初始化）后原子重写；
- 迁移 journal 与状态文件都带 `schema_version`；读到不认识的版本 → `failed` 并提示，**不移动/删除任何数据**；
- 状态文件缺失（首次升级）→ 直接采用当前配置目录并写入，不触发迁移。

### 13.3 启动判定

```
configured = runs_root(cfg)          # runlog.dir；空 = 配置文件同级 runs/
state      = read_runlog_state()      # 缺失 = None
last       = state.last_root

A. last 不存在                     → 采用 configured，写入 state，不迁移
B. last == configured              → 正常启动
C. last 存在且 != configured：
     C1. last 目录仍存在且非空      → 后台迁移 last → configured（同一套进度/journal/UI）
     C2. last 目录不存在或为空      → 只采用 configured，写告警日志，不迁移
D. config 中缺少整个 runlog 节      → 采用 last（保留数据优先），不回退默认目录
```

D 的用意：旧版本保存配置可能整体丢掉 `runlog` 节。若此时盲目回退默认目录，会把用户自定义目录里的历史记录「藏起来」。只有配置里**存在 `runlog.dir` 且为空**时，才视为用户明确选择默认目录并执行回迁。

### 13.4 幂等与续跑

- 迁移日志 `<config_dir>/runlog-migration.json` 在切换 root 前写入，包含 `schema_version / old_root / new_root / options / state`；
- 启动时发现未完成日志 → 必须走 `resume_if_needed`：重扫旧目录、清理 `.migrating`、跳过已迁移 run、按 `seq` 合并同名 run；
- 迁移完成：更新 `last_root` → 删除 journal → 状态置 `done`；
- 重复执行（用户重试、崩溃重启）结果一致，不产生重复目录。

### 13.5 降级边界（重要，需在用户文档中明示）

运行台账是 0.25 起的新能力。**旧版本不认识 `runlog` 配置节**：

1. 降级后 run 数据文件不会被删除，仍保留在原目录；
2. 但旧版本保存任意设置时，`runlog` 节（含自定义 `dir`）可能被旧版配置写回逻辑丢弃；
3. 因此：**降级期间不要在看板或 `config.yaml` 中修改运行台账相关设置**，降级前备份 `config.yaml` 与台账目录；
4. 升级回新版本后：
   - 若配置里仍有 `runlog.dir`，按 13.3 自动接回；
   - 若该节已被旧版本写丢，程序会优先采用 `runlog-state.json` 里的 `last_root`；
   - 若两者都丢失，把台账目录手动补回 `runlog.dir` 即可（可触发迁移）。
5. 升级/降级均需重启进程；重启会按现有逻辑把 `in_progress` 任务重新入队，已完成的 run 记录不受影响。

### 13.6 兼容性验收用例

1. 无 `runlog` 配置 + 已有 runs 目录 → 不迁移、可读、写入 state；
2. `last_root=A`、配置改 B → 启动自动迁移，完成后 A 清空、state 更新为 B；
3. `last_root=A`、配置整节缺失 → 采用 A，不搬；
4. 迁移中断后重启 → 续跑完成，无重复 run；
5. 手工把 `schema_version` 改成未知值 → `failed`，数据原样保留。
