# 嵌套 Git 仓库兼容（gitlink / 未注册 submodule）设计

> 落地：2026-11-17　|　触发案例：caresy（父仓库以 gitlink 记录 `caresy-api` / `caresy-web`，无 `.gitmodules`）

## 1. 问题

`caresy` 这类仓库的父仓库索引里有两个 mode `160000` 的 gitlink，但没有
`.gitmodules`。`git worktree add` 检出后：

- `caresy-api/`、`caresy-web/` 是**空目录**，Agent 读不到源码；
- Agent 自己 `git clone` 本地子仓库后，改动只存在于 worktree 里的克隆；
- 父仓库 `git commit` 只记录 gitlink 指针，`merge`/`stage land` 不会带上
  子仓库文件内容；
- 结果：任务显示完成、审查通过，但主工作区的子仓库代码没有变化（P0 修复
  实际丢失，下游 QA 重新报同一个 Bug）。

## 2. 兼容策略：检测即回退主工作区模式

### 2.1 检测

`orcsy/git_ops.py::Git.gitlinks()`：

```
git ls-files -s -z
→ mode=160000 的路径（子模块或未注册嵌套仓库）
```

`TaskExecutor.__init__` 在确认 `is_git_repo` 后探测一次：

- `self.gitlinks = [...]`
- 非空时强制：
  - `worktree_isolation = False`
  - `per_task_worktree = False`
  - 同步 `worktree_mgr` 的两个开关
- 记录 warning：说明回退原因与「需在各子仓库内提交」。

### 2.2 执行

回退后任务走现有「工作区模式」：

- `ToolContext.root = project.path`，Agent 直接编辑真实的
  `caresy-api/...` / `caresy-web/...` 文件；
- 不再创建/合并 worktree，不会出现空目录与 gitlink 合并丢失；
- `ProjectAgent` 的并行判定额外要求 `not executor.gitlinks`，因此这类项目
  **串行**推进，避免多个任务同时改同一子仓库；
- 系统提示词追加「嵌套 Git 仓库」节：说明执行模式、不要自行 commit/push、
  最终由人工在子仓库内提交。

### 2.3 审查与台账

父仓库 `git diff` 只显示 gitlink 指针，不显示子仓库文件。新增
`TaskExecutor._workspace_diff()`：

```
父仓库 git diff HEAD
+ 每个 gitlink 子仓库:
    git status --short
    git diff
```

用于：

- 工作区模式审查 prompt 的 diff 字段（审查 Agent 能看到真实改动）；
- RunLog `diff.patch` 工件；
- 前端「运行记录」回放。

任务成功收尾时追加进度提示：

> 📦 嵌套 Git 仓库改动已保留在主工作区（caresy-api、caresy-web）；
> Orcsy 不自动提交子仓库，请在对应目录内分别 git add/commit。

## 3. 人工提交顺序

1. 进入每个有改动的子仓库，`git add` + `git commit`（子仓库自己的历史）；
2. 回到父仓库，此时 gitlink 指针变化会出现在待提交列表；
3. 通过看板「待人工提交」页提交父仓库指针（或手动 `git add caresy-api`）。

Orcsy 不自动提交子仓库，也不自动 push；这与「主分支最终提交人工把关」的
安全边界一致。

## 4. 边界与后续

| 项 | 当前行为 |
| --- | --- |
| 并行 | 含 gitlink 的项目强制串行（`can_parallel=False`） |
| 隔离 | 不使用 worktree；任务直接改主工作区子仓库 |
| 自动提交 | 不自动提交子仓库/父仓库；人工按上面顺序提交 |
| 审查 | 使用父仓库 + 子仓库合并 diff，审查 Agent 可读真实文件 |
| 推送 | 子仓库与父仓库分别推送（如果配置了远端） |

后续可选增强：

- 为真正配置了 `.gitmodules` 的 submodule 提供 `git submodule update --init`
  的 worktree 物化；
- 在看板「待人工提交」页集中展示子仓库改动并代为提交；
- 嵌套仓库的 worktree 级提交/回灌（当前不做，风险高于收益）。

## 5. 验收

`tests/test_nested_repo_compat.py`：

- 构造父仓库 + gitlink 嵌套仓库；
- `TaskExecutor.gitlinks == ["caresy-api"]`；
- `worktree_isolation=False`、`per_task_worktree=False`；
- 提示词包含「嵌套 Git 仓库」与回退说明；
- 子仓库文件改动出现在 `_workspace_diff()` 中（父仓库 diff 看不到）。

回归：`test_parallel_writeback`、`e2e_project_team`、`e2e_fake_llm`。
