"""项目 Git 编排服务（从 orcsy/web.AppState 逐字拆分，Stage 3 绞杀者重构）。

职责：人工把关提交（含 stage 分支复位）、单文件 diff 预览、未提交改动丢弃、
一键 Git 初始化与新项目 Git 引导。纯业务函数：显式传入 `agents`/`ProjectConfig`，
不感知 HTTP；错误抛出顺序与消息文本与拆分前逐字一致。

日志沿用 `orcsy.web` 流（get_logger("web")）保持运维侧日志身份不变。
"""

from __future__ import annotations

from pathlib import Path

from .git_ops import Git, GitError
from .logging_setup import get_logger
from .worktree import WORKTREE_DIR

log = get_logger("web")


def _repo_key(value: object) -> str:
    """规范化仓库标识：'' / '.' = 主仓库；其余为相对项目根的 gitlink 路径。"""
    key = str(value or "").strip().replace("\\", "/").strip("/")
    return "" if key in ("", ".") else key


def discover_nested_repos(project_path: Path) -> dict[str, Path]:
    """发现项目内已登记的一层嵌套 Git 仓库（gitlink，mode 160000）。

    只认父仓库索引里的 gitlink，不扫描任意子目录，避免把 vendor/node_modules
    里偶然存在的 .git 误当成受管子仓库。路径 resolve 后必须仍在项目根内，
    防止 symlink 指向项目外。
    """
    base = Path(project_path).resolve()
    try:
        rels = Git(base).gitlinks()
    except GitError as e:
        log.debug("读取 gitlink 失败（按普通仓库处理）：%s", e)
        return {}
    repos: dict[str, Path] = {}
    for rel in rels:
        key = _repo_key(rel)
        if not key:
            continue
        candidate = (base / key).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            log.warning("忽略项目根之外的 gitlink：%s", rel)
            continue
        if candidate.is_dir() and (candidate / ".git").exists():
            repos[key] = candidate
    return repos


def _resolve_repo(agent, repo: object) -> tuple[str, Path]:
    """把 API 传入的 repo 解析成受管仓库路径；未知/越界仓库一律拒绝。"""
    key = _repo_key(repo)
    if not key:
        return "", Path(agent.project.path)
    nested = discover_nested_repos(agent.project.path)
    target = nested.get(key)
    if target is None:
        available = "、".join(sorted(nested)) or "无"
        raise ValueError(f"未知嵌套仓库：{repo}（可用：{available}）")
    return key, target


def _normalise_excludes(raw: object) -> dict[str, list[str]]:
    """兼容旧 exclude 列表与新「按仓库分组」对象。

    - 旧：["a", "b"] -> {"": ["a", "b"]}
    - 新：{"": ["a"], "caresy-api": ["tmp/x"]}
    """
    if isinstance(raw, dict):
        out: dict[str, list[str]] = {}
        for k, v in raw.items():
            if not isinstance(v, (list, tuple, set)):
                continue
            vals = [str(x).strip().strip('"') for x in v if str(x).strip()]
            if vals:
                out[_repo_key(k)] = vals
        return out
    if isinstance(raw, (list, tuple, set)):
        vals = [str(x).strip().strip('"') for x in raw if str(x).strip()]
        return {"": vals} if vals else {}
    return {}


def _included_entries(git: Git, excludes: list[str]) -> list[dict]:
    """当前仓库中未被 exclude 命中的 status 条目。"""
    exclude_set = set(excludes)
    return [e for e in git.status_entries()
            if not Git.is_path_excluded(e["paths"], exclude_set)]


def _entry_dict(repo: str, entry: dict) -> dict:
    path = str(entry.get("display") or "")
    return {
        "path": path,
        "display": f"{repo}/{path}" if repo else path,
        "status": str(entry.get("xy") or ""),
        "kind": "file",
    }


def pending_changes(agent) -> dict:
    """聚合主仓库 + 已登记嵌套仓库的未提交改动（人工把关提交页数据源）。"""
    base = Path(agent.project.path)
    parent_git = Git(base)
    if not parent_git.is_repo():
        return {"is_git": False, "count": 0, "files": [], "repos": []}
    try:
        parent_entries = parent_git.status_entries()
    except GitError as e:
        log.warning("读取项目 %s 主仓库 status 失败：%s", agent.project.name, e)
        return {"is_git": True, "count": 0, "files": [], "repos": [],
                "errors": [f"主仓库：{e}"]}

    nested = discover_nested_repos(base)
    nested_groups: list[dict] = []
    active_nested: set[str] = set()
    errors: list[str] = []
    for rel in sorted(nested, key=str.lower):
        child = Git(nested[rel])
        try:
            entries = child.status_entries()
        except GitError as e:
            errors.append(f"{rel}：{e}")
            continue
        if not entries:
            continue
        active_nested.add(rel)
        files = [_entry_dict(rel, e) for e in entries]
        nested_groups.append({
            "repo": rel, "label": rel, "nested": True,
            "count": len(files), "files": files,
        })

    parent_files: list[dict] = []
    for e in parent_entries:
        norm_paths = [str(Path(p).as_posix()).strip("/") for p in e.get("paths") or []]
        # 子仓库有真实改动时，父仓库那条 `M sub` 只是「脏子仓库」，
        # 由上面的子仓库分组展示；不重复计入，也不展示无意义的目录 diff。
        if any(p in active_nested for p in norm_paths):
            continue
        kind = "gitlink" if any(p in nested for p in norm_paths) else "file"
        item = _entry_dict("", e)
        item["kind"] = kind
        parent_files.append(item)

    repos: list[dict] = []
    if parent_files:
        repos.append({"repo": "", "label": "主仓库", "nested": False,
                      "count": len(parent_files), "files": parent_files})
    repos.extend(nested_groups)
    flat = [f["display"] for group in repos for f in group["files"]]
    return {
        "is_git": True,
        "count": len(flat),
        "files": flat[:2000],
        "repos": repos,
        **({"errors": errors} if errors else {}),
    }


def manual_commit(agents: dict, data: dict) -> dict:
    """人工把关确认：按仓库提交主工作区已落地的未提交变更（可选推送）。

    嵌套 Git 仓库（gitlink）按以下顺序处理，保证父仓库指针指向子仓库新 HEAD：

    1. 先提交所有有改动的子仓库（各自当前分支，本地 commit）；
    2. 最后提交父仓库，自动暂存更新后的 gitlink 指针；
    3. push=True 时先推子仓库；任一子仓库推送失败则跳过父仓库推送，
       避免远端父仓库引用一个子仓库远端不存在的 commit。

    exclude 兼容旧列表（仅主仓库）与新对象（按仓库分组），排除文件保留在各仓库
    工作区，不删除、不回滚。
    """
    name = str(data.get("name") or data.get("project") or "").strip()
    message = str(data.get("message") or "").strip()
    push = bool(data.get("push", False))
    excludes = _normalise_excludes(data.get("exclude"))
    agent = agents.get(name)
    if agent is None:
        raise ValueError(f"未知项目：{name}")
    p = agent.project
    if not agent.executor.probe_git_repo():
        raise ValueError(f"项目 {name} 不是 Git 仓库，无法提交")

    base = Path(p.path)
    parent_git = Git(base)
    nested = discover_nested_repos(base)
    results: list[dict] = []

    def _partial_note() -> str:
        if not results:
            return ""
        return "；此前已提交：" + "、".join(
            f"{r['label']} {r['commit']}" for r in results)

    # ---- 1. 子仓库先提交（按路径排序，保证结果稳定可复现）----
    for rel in sorted(nested, key=str.lower):
        child_path = nested[rel]
        child = Git(child_path)
        child_excludes = excludes.get(rel, [])
        try:
            included = _included_entries(child, child_excludes)
        except GitError as e:
            raise GitError(f"读取【{rel}】改动失败：{e}{_partial_note()}") from e
        if not included:
            continue
        try:
            res = child.commit_and_push_manual(message, push=False,
                                               exclude=child_excludes)
        except GitError as e:
            raise GitError(f"提交【{rel}】失败：{e}{_partial_note()}") from e
        results.append({
            "repo": rel, "label": rel,
            "commit": res.get("commit", ""),
            "branch": res.get("branch", ""),
            "pushed": False, "push_note": "",
            "skipped": [f"{rel}/{x}" for x in (res.get("skipped") or [])],
        })

    # ---- 2. 父仓库最后提交（包含更新后的 gitlink 指针）----
    parent_excludes = excludes.get("", [])
    parent_result: dict | None = None
    try:
        parent_included = _included_entries(parent_git, parent_excludes)
    except GitError as e:
        raise GitError(f"读取主仓库改动失败：{e}{_partial_note()}") from e
    if parent_included:
        try:
            res = parent_git.commit_and_push_manual(
                message, push=False, branch=p.allowed_branch,
                exclude=parent_excludes)
        except GitError as e:
            text = str(e)
            if "nothing to commit" in text or "没有可提交的改动" in text:
                # 子仓库改动全被排除时，父仓库可能只剩「脏 gitlink」而没有
                # 实际指针变化；不制造空提交，直接跳过父仓库。
                log.info("Web 人工提交 %s：父仓库无实际可提交改动，跳过", name)
            else:
                raise GitError(f"提交【主仓库】失败：{e}{_partial_note()}") from e
        else:
            parent_result = {
                "repo": "", "label": "主仓库",
                "commit": res.get("commit", ""),
                "branch": res.get("branch", ""),
                "pushed": False, "push_note": "",
                "skipped": list(res.get("skipped") or []),
            }
            results.append(parent_result)

    if not results:
        raise GitError("没有可提交的改动（或所有改动都被排除）")

    # ---- 3. 推送：子仓库全部成功后才推父仓库 ----
    if push:
        nested_incomplete = False
        for item in results:
            if item["repo"] == "":
                continue
            child = Git(nested[item["repo"]])
            if not child.has_remote("origin"):
                item["push_note"] = "未配置 origin，已跳过推送"
                # 父仓库的 gitlink 会指向这个仅存在于本地的子仓库 commit；
                # 远端父仓库无法还原子仓库内容，因此父仓库也不推送。
                nested_incomplete = True
                continue
            try:
                child.push("origin", item["branch"] or child.current_branch())
                item["pushed"] = True
            except GitError as e:
                item["push_note"] = str(e)
                nested_incomplete = True
        if parent_result is not None:
            if nested_incomplete:
                parent_result["push_note"] = "子仓库未全部推送成功，已跳过父仓库推送"
            elif not parent_git.has_remote("origin"):
                parent_result["push_note"] = "未配置 origin，已跳过推送"
            else:
                try:
                    parent_git.push(
                        "origin", p.allowed_branch or parent_git.current_branch())
                    parent_result["pushed"] = True
                except GitError as e:
                    parent_result["push_note"] = str(e)

    # 人工提交完成，主工作区恢复干净，重置 stage 分支到最新 HEAD
    if parent_result is not None:
        stage_branch = f"orcsy/stage/{p.name}"
        if parent_git.branch_exists(stage_branch):
            parent_git.ensure_stage_branch(
                stage_branch, parent_git.current_branch(),
                stage_worktree=base / WORKTREE_DIR / "_stage")

    skipped = [x for r in results for x in r["skipped"]]
    pushed = any(r["pushed"] for r in results)
    push_notes = [f"{r['label']}：{r['push_note']}"
                  for r in results if r.get("push_note")]
    overall_commit = (parent_result["commit"] if parent_result is not None
                      else results[-1]["commit"])
    overall_branch = (parent_result["branch"] if parent_result is not None
                      else results[-1]["branch"])

    log.info("Web 人工确认提交项目 %s：%s（仓库 %d 个，commits=%s, push=%s, 排除 %d 个文件）",
             name, message, len(results),
             "、".join(f"{r['label']}:{r['commit']}" for r in results),
             pushed, len(skipped))

    if len(results) == 1 and results[0]["repo"] == "":
        msg = (f"已成功提交至主分支（{overall_commit}）"
               + ("并推送到远程仓库" if pushed else ""))
        if skipped:
            msg += f"；已排除 {len(skipped)} 个文件未提交（保留在工作区）：{'、'.join(skipped[:10])}"
            if len(skipped) > 10:
                msg += " 等"
        if push_notes:
            msg += f"（推送警告：{'；'.join(push_notes)}）"
    else:
        msg = (f"已成功提交 {len(results)} 个仓库："
               + "、".join(f"{r['label']} {r['commit']}" for r in results)
               + ("，并已推送" if pushed else ""))
        if skipped:
            msg += f"；已排除 {len(skipped)} 个文件未提交（保留在工作区）：{'、'.join(skipped[:10])}"
            if len(skipped) > 10:
                msg += " 等"
        if push_notes:
            msg += f"（推送警告：{'；'.join(push_notes)}）"

    return {
        "ok": True,
        "committed": True,
        "commit": overall_commit,
        "branch": overall_branch,
        "pushed": pushed,
        "push_note": "；".join(push_notes),
        "skipped": skipped,
        "repos": results,
        "message": msg,
    }


def file_diff(agents: dict, data: dict) -> dict:
    """人工把关提交页：查看指定仓库单个文件的未提交改动 diff。

    repo 缺省为主仓库；子仓库必须是项目 gitlink 发现集合中的路径。
    主仓库的 gitlink 指针项返回指针摘要（父仓库旧 SHA vs 子仓库当前 HEAD）。
    """
    name = str(data.get("name") or data.get("project") or "").strip()
    file_path = str(data.get("file") or "").strip()
    agent = agents.get(name)
    if agent is None:
        raise ValueError(f"未知项目：{name}")
    p = agent.project
    if not agent.executor.probe_git_repo():
        raise ValueError(f"项目 {name} 不是 Git 仓库，无法查看 diff")
    repo_key, target = _resolve_repo(agent, data.get("repo"))
    g = Git(target)
    if repo_key == "":
        norm_file = str(Path(file_path.replace("\\", "/")).as_posix()).strip("/")
        if norm_file in discover_nested_repos(p.path):
            info = g.gitlink_diff(norm_file)
        else:
            info = g.file_diff(file_path)
    else:
        info = g.file_diff(file_path)
    return {"ok": True, "repo": repo_key, "file": file_path, **info}


def discard_files(agents: dict, data: dict) -> dict:
    """丢弃指定仓库文件的未提交改动（人工把关提交页「删除/还原」按钮）。

    未跟踪文件/目录直接物理删除；已跟踪文件还原到所属仓库 HEAD。
    主仓库 gitlink 指针项禁止丢弃，避免误伤子仓库工作区。
    """
    name = str(data.get("name") or data.get("project") or "").strip()
    files_raw = data.get("files")
    files = [str(x) for x in files_raw] if isinstance(files_raw, list) else []
    agent = agents.get(name)
    if agent is None:
        raise ValueError(f"未知项目：{name}")
    p = agent.project
    if not agent.executor.probe_git_repo():
        raise ValueError(f"项目 {name} 不是 Git 仓库，无法丢弃改动")
    repo_key, target = _resolve_repo(agent, data.get("repo"))
    if repo_key == "":
        nested = discover_nested_repos(p.path)
        for f in files:
            norm = str(Path(str(f).replace("\\", "/")).as_posix()).strip("/")
            matched = next(
                (rel for rel in nested
                 if norm == rel or norm.startswith(rel + "/")), "")
            if matched:
                raise ValueError(
                    f"{norm} 属于子仓库 {matched} 的指针/内容，"
                    "请到对应子仓库分组内丢弃，不能从主仓库删除")
    g = Git(target)
    res = g.discard_changes(files)
    parts = []
    if res["deleted"]:
        parts.append(f"已删除 {len(res['deleted'])} 项")
    if res["restored"]:
        parts.append(f"已还原 {len(res['restored'])} 个文件")
    if res["skipped"]:
        parts.append(f"{len(res['skipped'])} 项跳过")
    log.info("Web 丢弃项目 %s[%s] 未提交改动：删除 %d / 还原 %d / 跳过 %d",
             name, repo_key or "主仓库", len(res["deleted"]),
             len(res["restored"]), len(res["skipped"]))
    return {"ok": True, "repo": repo_key, **res,
            "message": "；".join(parts) or "没有可丢弃的改动"}


def git_init(agents: dict, data: dict) -> dict:
    """为非 Git 目录一键初始化 Git 仓库并创建基础提交与忽略规则（开启小组 Worktree 必备）。"""
    name = str(data.get("name") or data.get("project") or "").strip()
    agent = agents.get(name)
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


def bootstrap_project_git(proj) -> list[str]:
    """新项目/导入项目的 git 引导（save_project 内联块逐字拆分）。

    非 git 目录则 git init；是仓库则补齐 .gitignore（任务目录 / AGENT.md /
    worktree 不入版本库）。已有条目与用户内容一律不动；git 不可用等失败
    只记日志、不阻断建项。返回给人看的 git_notes 列表。
    """
    git_notes: list[str] = []
    if proj.path.is_dir():
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
    return git_notes


__all__ = [
    "bootstrap_project_git",
    "discover_nested_repos",
    "discard_files",
    "file_diff",
    "git_init",
    "manual_commit",
    "pending_changes",
]
