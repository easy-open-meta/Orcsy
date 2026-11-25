"""项目任务归档（评估⑤）：点时间快照推送到指定存储目标。

对齐路线图评估⑤「快照分发（合规/审计），避免与 git 双轨」：归档是单向快照，
恢复 = 手动下载后回灌（后续再设计）。内容：tasks/*.md + tasks/done/*.md
+ AGENT.md + 项目记忆文档（ARCHITECTURE.md / PROJECT_NOTES.md /
LONG_TERM_MEMORY.md），可选 runs/<项目> 运行台账。归档先落本地 archive/
并 prune，远端推送走 `orcsy.storage` 统一 provider。
"""

from __future__ import annotations

import pathlib
import tarfile
import time

from .tools_memory import ARCHITECTURE_FILE, LONG_TERM_FILE, NOTES_FILE

ARCHIVE_GLOB = "orcsy-project-*.tar.gz"
# 项目长期记忆：随任务快照一起打包，换机/回灌时不丢项目经验
MEMORY_FILES = (ARCHITECTURE_FILE, NOTES_FILE, LONG_TERM_FILE)


def _safe_project_name(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)


def archive_project(project, *, data_dir: pathlib.Path,
                    include_runs: bool = False, keep_last: int = 10) -> pathlib.Path:
    """打包一个项目的任务快照到 data_dir/archive，返回归档路径。

    参数 project 为 ProjectConfig 的鸭类型（含 .name/.path/.tasks_dir/.guide_file）。
    """
    project_name = getattr(project, "name", "") or "project"
    proj_root = pathlib.Path(getattr(project, "path", "") or "")
    tasks_dir = str(getattr(project, "tasks_dir", "tasks") or "tasks")
    guide_file = str(getattr(project, "guide_file", "AGENT.md") or "AGENT.md")

    dest_dir = pathlib.Path(data_dir) / "archive"
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_project_name(project_name)
    dest = dest_dir / f"orcsy-project-{safe_name}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"

    tasks_root = proj_root / tasks_dir
    with tarfile.open(dest, "w:gz") as tf:
        if tasks_root.is_dir():
            for md in sorted(tasks_root.glob("*.md")):
                tf.add(md, arcname=f"tasks/{md.name}")
            done_root = tasks_root / "done"
            if done_root.is_dir():
                for md in sorted(done_root.glob("*.md")):
                    tf.add(md, arcname=f"tasks/done/{md.name}")
        guide = proj_root / guide_file
        if guide.is_file():
            tf.add(guide, arcname=guide.name)
        for memory_name in MEMORY_FILES:
            memory = proj_root / memory_name
            if memory.is_file():
                tf.add(memory, arcname=memory.name)
        if include_runs:
            runs_root = pathlib.Path(data_dir) / "runs" / project_name
            if runs_root.is_dir():
                for run_dir in sorted(runs_root.iterdir()):
                    if not run_dir.is_dir():
                        continue
                    for artifact in sorted(run_dir.glob("*")):
                        if artifact.is_file() and ".lock" not in artifact.name:
                            rel = artifact.relative_to(runs_root)
                            tf.add(artifact, arcname=f"runs/{rel.as_posix()}")

    try:
        dest.chmod(0o600)
    except OSError:
        pass
    prune_archives(dest_dir, keep_last, project_name=project_name)
    return dest


def prune_archives(dest_dir: pathlib.Path, keep_last: int,
                   *, project_name: str | None = None) -> None:
    """按项目裁剪本地归档保留数（project_name 置空则裁剪全部）。"""
    if keep_last <= 0:
        keep_last = 1
    dest_dir = pathlib.Path(dest_dir)
    if project_name:
        glob = f"orcsy-project-{_safe_project_name(project_name)}-*.tar.gz"
    else:
        glob = ARCHIVE_GLOB
    archives = sorted(dest_dir.glob(glob), key=lambda p: p.stat().st_mtime)
    for old in archives[:-keep_last] if len(archives) > keep_last else []:
        try:
            old.unlink()
        except OSError:
            pass


def archive_prefix(project_name: str) -> str:
    """某项目归档文件名前缀（用于远端裁剪过滤）。"""
    return f"orcsy-project-{_safe_project_name(project_name)}-"


def push_archive(archive: pathlib.Path, provider, *, keep_last: int | None = None,
                 prefix: str = "") -> str:
    """把项目归档推送到 provider；对象键用归档文件名，返回 provider 句柄。

    keep_last + prefix 非空时，推送后裁剪远端旧归档（失败静默）。
    """
    archive = pathlib.Path(archive)
    handle = provider.put(archive.name, archive.read_bytes(),
                          content_type="application/gzip")
    if keep_last is not None and prefix:
        from .storage import prune_remote
        prune_remote(provider, prefix=prefix, keep_last=keep_last)
    return handle


__all__ = ["archive_project", "prune_archives", "push_archive", "archive_prefix",
           "ARCHIVE_GLOB"]