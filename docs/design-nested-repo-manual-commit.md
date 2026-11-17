# 嵌套 Git 仓库人工把关提交设计

> 状态：已实施（2026-11-17）　|　适用版本：v0.25.0+
> 案例：caresy（父仓库以 gitlink 记录 `caresy-api` / `caresy-web`，无 `.gitmodules`）
> 关联：[嵌套 Git 仓库兼容](design-nested-repo-compat.md)

## 1. 背景与问题

`caresy` 这类项目的父仓库索引里有 mode `160000` 的 gitlink，但没有
`.gitmodules`。任务执行时已回退到主工作区串行模式，Agent 直接修改真实的
`caresy-api/`、`caresy-web/` 文件；审查 Agent 通过
`TaskExecutor._workspace_diff()` 已经能看到子仓库的真实 diff。

但「待人工提交」页仍然只读取父仓库：

- `full_state()` 只调用 `Git(project.path).pending_changes_summary()`；
- 父仓库 `git status` 只显示 `caresy-api` / `caresy-web` 两个 gitlink 目录，
  不显示子仓库里的文件；
- 点开 gitlink 文件无法得到有意义的逐文件 diff；
- `manual_commit()` 只提交父仓库，子仓库的代码改动不会进入版本库；
- 父仓库提交的指针仍指向子仓库旧 HEAD，远端/其他机器都看不到真实修复。

实测 caresy：父仓库只有 3 条改动，而 `caresy-api` 有 11 个改动、
`caresy-web` 有 11 个改动。看板「待人工提交 (3)」会严重低估实际待审内容。

## 2. 目标与范围

### 2.1 本阶段（Phase 1）目标

1. 已登记 gitlink（`git ls-files -s` 中 mode `160000`）的一层嵌套仓库；
2. 看板/项目卡片显示真实待提交文件总数，并按仓库分组；
3. 人工把关页支持子仓库文件 diff、排除、丢弃；
4. 一次提交完成：
   - 先提交所有有改动的子仓库；
   - 最后提交父仓库，自动带上更新后的 gitlink 指针；
5. 可选推送：先推子仓库，全部成功后再推父仓库；任一子仓库推送失败时
   父仓库不推送，避免远端父仓库指向未推送的子仓库 commit；
6. 每条仓库使用同一份 commit message（Phase 1）；返回按仓库拆分的提交结果。

### 2.2 不在本阶段

- 未登记为 gitlink 的嵌套仓库自动 `git add` 纳管；
- 多层嵌套仓库递归提交；
- 每个子仓库单独填写 commit message；
- 子仓库单独选择是否推送；
- 跨仓库事务回滚（Git 无跨仓库事务，采用预检 + 尽力提交 + 明确报告）。

## 3. 数据结构

### 3.1 `pending_git_changes`

保留旧字段，新增 `repos`：

```json
{
  "is_git": true,
  "count": 23,
  "files": [
    "ARCHITECTURE.md",
    "caresy-api/src/.../PasswordMigration.cs",
    "caresy-web/src/api/auth.js"
  ],
  "repos": [
    {
      "repo": "",
      "label": "主仓库",
      "nested": false,
      "count": 1,
      "files": [
        {
          "path": "ARCHITECTURE.md",
          "display": "ARCHITECTURE.md",
          "status": "??",
          "kind": "file"
        }
      ]
    },
    {
      "repo": "caresy-api",
      "label": "caresy-api",
      "nested": true,
      "count": 11,
      "files": [
        {
          "path": "src/EOM.Caresy.Infrastructure/Repositories/PasswordMigration.cs",
          "display": "caresy-api/src/EOM.Caresy.Infrastructure/Repositories/PasswordMigration.cs",
          "status": "??",
          "kind": "file"
        }
      ]
    }
  ],
  "errors": []
}
```

字段约定：

| 字段 | 含义 |
| --- | --- |
| `repo` | `""` = 主仓库；其他值为相对项目根的 gitlink 路径 |
| `label` | 前端展示名；主仓库为「主仓库」，子仓库为相对路径 |
| `files[].path` | 相对**所属仓库根**的路径，用于 diff/discard |
| `files[].display` | 跨仓库展示名，子仓库自动加 `repo/` 前缀 |
| `files[].status` | `git status --porcelain` 的 XY（`??` = 未跟踪） |
| `files[].kind` | `file` 或 `gitlink`（子仓库干净但父仓库指针变化） |
| `count` | 所有仓库的真实文件数之和 |
| `files` | 兼容旧客户端的扁平展示名列表 |

父仓库中与「有改动的子仓库」对应的 gitlink 条目不再重复展示；子仓库提交后
父仓库指针由提交流程自动暂存。

### 3.2 仓库发现

`git_service.discover_nested_repos(project_path)`：

1. `Git(project_path).gitlinks()` 读取 mode `160000` 路径；
2. 对每个路径做 `resolve()`，要求仍在项目根目录内（拒绝 symlink 逃逸）；
3. 要求目录存在且包含 `.git`（目录或 gitfile）；
4. 返回 `{相对路径: 绝对路径}`。

只读取 gitlink，不递归扫描任意子目录，避免误把 vendor/node_modules 里的
`.git` 当成受管子仓库。

## 4. API 变更

### 4.1 单文件 diff

```http
POST /api/project/diff
{
  "name": "caresy",
  "repo": "caresy-api",
  "file": "src/EOM.Caresy.Infrastructure/Repositories/PasswordMigration.cs"
}
```

- `repo` 可省略，缺省 `""`（主仓库），兼容旧调用；
- 后端只接受当前项目发现出来的仓库集合中的 `repo`；
- `file` 仍由 `Git.file_diff()` 做绝对路径/`..` 穿越拦截；
- 主仓库 gitlink 指针项返回「子仓库指针」文本摘要，而不是尝试读取目录。

### 4.2 丢弃改动

```http
POST /api/project/discard
{
  "name": "caresy",
  "repo": "caresy-api",
  "files": ["tmp/output.log"]
}
```

- `repo` 缺省主仓库；
- 子仓库内未跟踪文件物理删除、已跟踪文件还原到该子仓库 HEAD；
- 主仓库 gitlink 指针项禁止通过此接口丢弃，避免误伤子仓库工作区。

### 4.3 人工提交

```http
POST /api/project/commit
{
  "name": "caresy",
  "message": "feat: 完成患者 CRUD 与前端联调",
  "push": true,
  "exclude": {
    "": ["ARCHITECTURE.md"],
    "caresy-api": ["tmp/output.log"],
    "caresy-web": ["dist/"]
  }
}
```

`exclude` 兼容旧格式：

- 旧格式 `["a", "b"]` 等价于 `{"": ["a", "b"]}`（仅主仓库）；
- 新格式按仓库分组；每条仓库只匹配自己仓库内的路径。

提交结果：

```json
{
  "ok": true,
  "committed": true,
  "commit": "父仓库 hash",
  "pushed": true,
  "repos": [
    {
      "repo": "caresy-api",
      "label": "caresy-api",
      "commit": "abc1234",
      "pushed": true,
      "push_note": "",
      "skipped": ["caresy-api/tmp/output.log"]
    },
    {
      "repo": "caresy-web",
      "label": "caresy-web",
      "commit": "def5678",
      "pushed": true,
      "push_note": "",
      "skipped": []
    },
    {
      "repo": "",
      "label": "主仓库",
      "commit": "123abcd",
      "pushed": true,
      "push_note": "",
      "skipped": []
    }
  ],
  "skipped": ["caresy-api/tmp/output.log"],
  "message": "已成功提交 3 个仓库：caresy-api abc1234、caresy-web def5678、主仓库 123abcd，并已推送"
}
```

## 5. 提交编排

### 5.1 顺序

```
1. 预检：发现仓库 + 读取 status；没有有效改动直接报错
2. 按 exclude 过滤每个仓库的条目
3. 子仓库按路径排序，逐个 commit（当前分支，push=False）
4. 父仓库最后 commit：暂存父仓库普通改动 + 更新后的 gitlink 指针
5. 若 push=true：
   a. 逐个推送有提交的子仓库
   b. 任一子仓库推送失败或未配置 origin → 跳过父仓库推送，记录 push_note
   c. 全部成功 → 推送父仓库（沿用父仓库 allowed_branch / 当前分支）
```

### 5.2 关键实现点

- 子仓库提交使用各自当前分支，不把父仓库的 `allowed_branch` 强加给子仓库；
- 父仓库 `commit_and_push_manual()` 会先 `git reset` 再按包含路径 `git add`，
  因此不会把用户手工暂存的排除文件带入提交；
- 父仓库提交时 `caresy-api` 路径是 gitlink，`git add -A -- caresy-api`
  会记录子仓库新 HEAD 的指针（即使没有 `.gitmodules`）；
- 子仓库全部改动都被排除时跳过该子仓库；父仓库若只剩「脏 gitlink」
  但没有实际指针变化，跳过父仓库，不制造空提交；
- 任一仓库 commit 失败：立即停止后续提交，把「此前已提交的仓库 + 失败原因」
  放进错误消息，不回滚已产生的 commit；
- 推送失败不回滚本地 commit，结果中保留 `push_note`。

### 5.3 推送一致性

父仓库的 gitlink 指针指向子仓库 commit。若子仓库未推送成功（含未配置
origin）而父仓库推送成功，远端父仓库会引用一个远端子仓库不存在的 commit。
因此：

```
子仓库未全部推送成功 => 父仓库 commit 保留本地，但父仓库 push 跳过
```

结果消息明确提示「子仓库未全部推送成功，已跳过父仓库推送」。

## 6. 前端

### 6.1 人工把关提交页

`CommitView.vue` 按仓库分组：

```
改动文件（23）

▾ 主仓库 (1)
    ?? ARCHITECTURE.md

▾ caresy-api (11)
    ?? src/.../PasswordMigration.cs
    M  src/.../CrudEndpointTests.cs

▾ caresy-web (11)
    M  src/api/auth.js
    ...
```

- 文件唯一键：`repo + "\0" + path`，避免不同仓库同名文件冲突；
- 右侧 diff 标题显示 `repo / path`；
- 排除/删除按所属仓库分别调用 API；
- 底部一条 commit message + 一个 push 开关；
- 提交结果按仓库展示 commit hash / pushed / push_note；
- 主仓库 gitlink 指针项不提供「删除」按钮。

### 6.2 项目卡片 / Dashboard

- 「待人工提交 (N)」的 N 改为所有仓库的真实文件总数；
- tooltip 汇总：

```
主仓库 1 · caresy-api 11 · caresy-web 11
```

## 7. 安全与边界

| 风险 | 处理 |
| --- | --- |
| repo 路径穿越 | repo 必须在 gitlink 发现集合内；resolve 后仍在项目根内 |
| file 路径穿越 | 继续使用 `Git.file_diff()` / `discard_changes()` 的路径校验 |
| 误删子仓库指针 | 主仓库 gitlink 项不提供 discard；API 拒绝 |
| 子仓库未初始化 | 发现阶段要求 `.git` 存在，否则不纳入并在 `errors` 提示 |
| 子仓库推送失败 | 父仓库不推送 |
| 跨仓库无事务 | 预检 + 立即停止 + 部分成功报告；不回滚已有 commit |
| 运行中任务并发改子仓库 | 现有工作区模式串行；人工提交仍建议在无任务执行时进行 |

## 8. 测试

新增 `tests/test_nested_repo_manual_commit.py`：

1. 构造父仓库 + `caresy-api` / `caresy-web` 两个 gitlink 子仓库；
2. `full_state().pending_git_changes`：
   - `repos` 含主仓库与两个子仓库；
   - 子仓库文件带 `repo` 前缀；
   - `count` = 各仓库文件数之和；
3. 子仓库 tracked 修改与未跟踪新文件的 diff；
4. 非法 `repo`、`file` 穿越拒绝；
5. 子仓库 discard（删除未跟踪 / 还原已跟踪）；
6. 人工提交：
   - 子仓库先 commit，父仓库最后 commit；
   - 父仓库 `HEAD:caresy-api` == 子仓库新 HEAD；
   - 子仓库与父仓库工作区状态符合预期；
   - `exclude` 按仓库生效，排除文件留在原仓库工作区；
   - 返回 `repos` 明细与扁平 `skipped`；
7. 指针-only 场景：子仓库已手动提交、父仓库只有 gitlink 指针变化时，
   主仓库组出现 `kind=gitlink` 条目，可提交父仓库指针。

回归：`test_team_web_api`（旧 exclude 列表 / 单仓库提交消息）、
`test_nested_repo_compat`、`test_mobile_remote_access`、`e2e_auth`。

## 9. 实施清单

- [x] 设计文档存档
- [x] `git_ops.py`：公开 status entries / exclude 匹配 / 指针摘要
- [x] `git_service.py`：仓库发现、聚合 pending、repo-aware diff/discard、编排提交
- [x] `web.py`：full_state 使用聚合结果
- [x] `api.ts` / `CommitView.vue`：仓库分组、repo 参数、提交结果
- [x] `ProjectsView.vue` / `DashboardView.vue`：数量与 tooltip
- [x] `tests/test_nested_repo_manual_commit.py` + 回归
- [x] 用户指南 / README / 官网同步
