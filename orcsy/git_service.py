"""项目 Git 编排服务（从 orcsy/web.AppState 逐字拆分，Stage 3 绞杀者重构）。

职责：人工把关提交（含 stage 分支复位）、单文件 diff 预览、未提交改动丢弃、
一键 Git 初始化与新项目 Git 引导。纯业务函数：显式传入 `agents`/`ProjectConfig`，
不感知 HTTP；错误抛出顺序与消息文本与拆分前逐字一致。

日志沿用 `orcsy.web` 流（get_logger("web")）保持运维侧日志身份不变。
"""

from __future__ import annotations

from .git_ops import Git, GitError
from .logging_setup import get_logger
from .worktree import WORKTREE_DIR

log = get_logger("web")


def manual_commit(agents: dict, data: dict) -> dict:
    """人工把关确认：将主工作区已落地的未提交变更在主分支执行 git commit（可选推送）。

    exclude：本次不提交的文件名列表（如临时测试产物），这些文件保留在工作区。
    """
    name = str(data.get("name") or data.get("project") or "").strip()
    message = str(data.get("message") or "").strip()
    push = bool(data.get("push", False))
    exclude_raw = data.get("exclude")
    exclude = ([str(x) for x in exclude_raw] if isinstance(exclude_raw, list) else [])
    agent = agents.get(name)
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


def file_diff(agents: dict, data: dict) -> dict:
    """人工把关提交页：查看单个文件在主工作区的未提交改动 diff。

    新文件（未跟踪）返回全文预览；已修改文件返回 git diff 文本。
    附带 truncated/total_chars/lines 供前端提示与「下载补丁」使用。
    """
    name = str(data.get("name") or data.get("project") or "").strip()
    file_path = str(data.get("file") or "").strip()
    agent = agents.get(name)
    if agent is None:
        raise ValueError(f"未知项目：{name}")
    p = agent.project
    if not agent.executor.probe_git_repo():
        raise ValueError(f"项目 {name} 不是 Git 仓库，无法查看 diff")
    g = Git(p.path)
    info = g.file_diff(file_path)
    return {"ok": True, "file": file_path, **info}


def discard_files(agents: dict, data: dict) -> dict:
    """丢弃指定文件的未提交改动（人工把关提交页「删除/还原」按钮）。

    未跟踪文件/目录直接物理删除；已跟踪文件还原到 HEAD（撤销本次改动）。
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
    "discard_files",
    "file_diff",
    "git_init",
    "manual_commit",
]
