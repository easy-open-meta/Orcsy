"""嵌套 Git 仓库人工把关提交回归测试。

覆盖 caresy 类项目（父仓库以 gitlink 记录子仓库、无 .gitmodules）：
- 聚合 pending 改动（父仓库 + 各子仓库真实文件数）
- 子仓库文件 diff / 路径穿越拒绝
- 子仓库丢弃改动
- 人工提交：子仓库先 commit，父仓库最后提交更新后的 gitlink 指针
- exclude 按仓库生效，排除文件保留在原工作区
- 子仓库已手动提交、父仓库只有指针变化的场景
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import load_config
from orcsy.git_ops import Git, GitError
from orcsy.scheduler import Scheduler
from orcsy.web import AppState


def _init_repo(path: Path) -> Git:
    path.mkdir(parents=True, exist_ok=True)
    g = Git(path)
    # 显式 init：子仓库位于父仓库内时 Git.init() 的 is_repo() 探测会误判为
    # 已在工作树内而跳过真正建库，测试必须绕过该便捷方法。
    g.run("init")
    g.run("config", "user.email", "nested@example.com")
    g.run("config", "user.name", "nested-tester")
    return g


def _make_env() -> tuple[Path, AppState, Path, Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="nested-commit-"))
    parent = tmp / "caresy"
    parent.mkdir()
    pg = _init_repo(parent)
    (parent / "README.md").write_text("# caresy\n", encoding="utf-8")
    (parent / "AGENT.md").write_text("# Guide\n", encoding="utf-8")
    pg.add_all()
    pg.commit("chore: init parent")

    api = parent / "caresy-api"
    ag = _init_repo(api)
    (api / "src").mkdir()
    (api / "src/feature.py").write_text("x = 1\n", encoding="utf-8")
    ag.add_all()
    ag.commit("chore: init api")

    web = parent / "caresy-web"
    wg = _init_repo(web)
    (web / "src").mkdir()
    (web / "src/app.js").write_text("export const a = 1\n", encoding="utf-8")
    wg.add_all()
    wg.commit("chore: init web")

    # 父仓库登记两个 gitlink（模拟 caresy：有 gitlink、无 .gitmodules）
    pg.run("add", "--", "caresy-api", "caresy-web")
    pg.commit("chore: add nested repos")

    cfg_file = tmp / "config.yaml"
    cfg_file.write_text(
        f"""
llm:
  provider: openai
  api_key: sk-test
  model: test
projects:
  - name: caresy
    path: {parent.as_posix()}
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file, require_api_key=False)
    state = AppState(cfg, Scheduler(cfg))
    return tmp, state, parent, api, web


def check_pending_and_diff() -> None:
    tmp, state, parent, api, web = _make_env()
    try:
        (api / "src/feature.py").write_text("x = 2\n", encoding="utf-8")
        (api / "src/new.py").write_text("print('new')\n", encoding="utf-8")
        (web / "src/app.js").write_text("export const a = 2\n", encoding="utf-8")
        (parent / "ARCHITECTURE.md").write_text("# Arch\n", encoding="utf-8")

        project = state.full_state()["projects"][0]
        pending = project["pending_git_changes"]
        assert pending["count"] == 4, pending
        labels = {r["label"]: r for r in pending["repos"]}
        assert set(labels) == {"主仓库", "caresy-api", "caresy-web"}, labels
        assert labels["主仓库"]["count"] == 1
        assert labels["caresy-api"]["count"] == 2
        api_files = labels["caresy-api"]["files"]
        assert any(f["path"] == "src/feature.py" for f in api_files), api_files
        assert any(f["display"] == "caresy-api/src/new.py" for f in api_files), api_files
        # 父仓库不再重复显示 gitlink 目录行
        assert all(f["path"] not in ("caresy-api", "caresy-web")
                   for f in labels["主仓库"]["files"]), labels["主仓库"]

        diff = state.project_file_diff({
            "name": "caresy", "repo": "caresy-api", "file": "src/feature.py"})
        assert diff["ok"] and "x = 2" in diff["diff"], diff
        assert diff["repo"] == "caresy-api"
        new_diff = state.project_file_diff({
            "name": "caresy", "repo": "caresy-api", "file": "src/new.py"})
        assert "新增文件" in new_diff["diff"], new_diff
        parent_diff = state.project_file_diff({
            "name": "caresy", "file": "ARCHITECTURE.md"})
        assert "新增文件" in parent_diff["diff"], parent_diff

        for repo in ("../outside", "no-such-repo"):
            try:
                state.project_file_diff({
                    "name": "caresy", "repo": repo, "file": "src/feature.py"})
                raise AssertionError(f"非法 repo 应被拒绝：{repo}")
            except (ValueError, GitError):
                pass
        try:
            state.project_file_diff({
                "name": "caresy", "repo": "caresy-api", "file": "../README.md"})
            raise AssertionError("子仓库路径穿越应被拒绝")
        except (ValueError, GitError):
            pass
        print("ok: pending 聚合子仓库文件 / 子仓库 diff / 路径穿越拒绝")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_discard_nested() -> None:
    tmp, state, _parent, api, _web = _make_env()
    try:
        (api / "tmp.log").write_text("temp\n", encoding="utf-8")
        res = state.discard_project_files({
            "name": "caresy", "repo": "caresy-api", "files": ["tmp.log"]})
        assert res["ok"] and res["deleted"] == ["tmp.log"], res
        assert not (api / "tmp.log").exists(), "子仓库未跟踪文件应被删除"

        (api / "src/feature.py").write_text("x = 9\n", encoding="utf-8")
        res2 = state.discard_project_files({
            "name": "caresy", "repo": "caresy-api", "files": ["src/feature.py"]})
        assert res2["ok"] and res2["restored"] == ["src/feature.py"], res2
        assert (api / "src/feature.py").read_text(encoding="utf-8") == "x = 1\n"
        # 主仓库不能直接丢弃 gitlink 指针
        try:
            state.discard_project_files({
                "name": "caresy", "files": ["caresy-api"]})
            raise AssertionError("主仓库 gitlink 指针不应允许 discard")
        except ValueError as e:
            assert "子仓库" in str(e), e
        print("ok: 子仓库 discard（删除未跟踪 / 还原已跟踪 / 主仓库指针保护）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_manual_commit_nested() -> None:
    tmp, state, parent, api, web = _make_env()
    try:
        (api / "src/feature.py").write_text("x = 2\n", encoding="utf-8")
        (api / "tmp.log").write_text("temp\n", encoding="utf-8")
        (web / "src/app.js").write_text("export const a = 2\n", encoding="utf-8")
        (parent / "ARCHITECTURE.md").write_text("# Arch\n", encoding="utf-8")
        (parent / "root.tmp").write_text("temp\n", encoding="utf-8")

        res = state.manual_commit_project({
            "name": "caresy",
            "message": "feat: 嵌套仓库人工提交",
            "push": False,
            "exclude": {
                "": ["root.tmp"],
                "caresy-api": ["tmp.log"],
            },
        })
        assert res["ok"] and res["committed"], res
        repos = {r["label"]: r for r in res["repos"]}
        assert set(repos) == {"主仓库", "caresy-api", "caresy-web"}, repos
        assert repos["caresy-api"]["commit"], repos
        assert repos["caresy-web"]["commit"], repos
        assert res["commit"] == repos["主仓库"]["commit"]
        assert "caresy-api/tmp.log" in res["skipped"], res["skipped"]
        assert "root.tmp" in res["skipped"], res["skipped"]
        assert (api / "tmp.log").is_file(), "排除的子仓库文件必须保留"
        assert (parent / "root.tmp").is_file(), "排除的父仓库文件必须保留"

        # 父仓库 gitlink 指针必须指向子仓库新 HEAD
        parent_pointer = Git(parent).run("rev-parse", "HEAD:caresy-api")
        child_head = Git(api).rev_parse("HEAD")
        assert parent_pointer == child_head, (parent_pointer, child_head)
        web_pointer = Git(parent).run("rev-parse", "HEAD:caresy-web")
        assert web_pointer == Git(web).rev_parse("HEAD")
        # 子仓库提交内容真实存在
        assert "feat: 嵌套仓库人工提交" in Git(api).run("log", "-1", "--pretty=%s")
        assert "feat: 嵌套仓库人工提交" in Git(web).run("log", "-1", "--pretty=%s")
        print("ok: 子仓库先提交 / 父仓库指针最后更新 / exclude 按仓库生效")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_pointer_only() -> None:
    tmp, state, parent, api, _web = _make_env()
    try:
        # 用户在外部手动提交子仓库：子仓库干净，父仓库只剩 gitlink 指针变化
        (api / "manual.py").write_text("print('manual')\n", encoding="utf-8")
        child = Git(api)
        child.add_all()
        child.commit("feat: manual child commit")

        pending = state.full_state()["projects"][0]["pending_git_changes"]
        main = next(r for r in pending["repos"] if r["repo"] == "")
        pointer = next(f for f in main["files"] if f["path"] == "caresy-api")
        assert pointer["kind"] == "gitlink", pointer
        diff = state.project_file_diff({"name": "caresy", "file": "caresy-api"})
        assert "子仓库指针" in diff["diff"], diff
        assert child.rev_parse("HEAD") in diff["diff"], diff

        res = state.manual_commit_project({
            "name": "caresy", "message": "chore: 提交子仓库指针", "push": False})
        assert res["ok"] and Git(parent).run("rev-parse", "HEAD:caresy-api") \
            == child.rev_parse("HEAD")
        print("ok: 子仓库已手动提交时，父仓库指针项可预览/提交")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_push_order() -> None:
    """push=true：子仓库先推、父仓库最后推；子仓库推送失败则父仓库不推送。"""
    tmp, state, parent, api, web = _make_env()
    try:
        remotes = tmp / "remotes"
        remotes.mkdir()
        bare: dict[str, Path] = {}
        branches: dict[str, str] = {}
        for key, repo in (("parent", parent), ("api", api), ("web", web)):
            b = remotes / f"{key}.git"
            b.mkdir()
            Git(b).run("init", "--bare")
            g = Git(repo)
            branch = g.current_branch()
            g.run("remote", "add", "origin", str(b))
            g.run("push", "-u", "origin", branch)
            bare[key] = b
            branches[key] = branch

        (api / "src/feature.py").write_text("x = 3\n", encoding="utf-8")
        (web / "src/app.js").write_text("export const a = 3\n", encoding="utf-8")
        (parent / "ARCHITECTURE.md").write_text("# Arch\n", encoding="utf-8")
        res = state.manual_commit_project({
            "name": "caresy", "message": "feat: push all repos", "push": True})
        assert res["ok"] and res["pushed"], res
        assert all(r["pushed"] for r in res["repos"]), res["repos"]
        for key, repo in (("parent", parent), ("api", api), ("web", web)):
            local = Git(repo).rev_parse("HEAD")
            remote = Git(bare[key]).run("rev-parse", f"refs/heads/{branches[key]}")
            assert local == remote, (key, local, remote)

        # 子仓库推送失败：父仓库本地提交保留，但远端父仓库不得前进
        before_parent_remote = Git(bare["parent"]).run(
            "rev-parse", f"refs/heads/{branches['parent']}")
        (api / "src/feature.py").write_text("x = 4\n", encoding="utf-8")
        child = Git(api)
        child.run("remote", "set-url", "origin", str(tmp / "missing-remote.git"))
        res2 = state.manual_commit_project({
            "name": "caresy", "message": "feat: child push failure", "push": True})
        repos = {r["label"]: r for r in res2["repos"]}
        assert repos["caresy-api"]["pushed"] is False, repos
        assert "子仓库" in repos["主仓库"]["push_note"], repos
        assert "跳过父仓库" in repos["主仓库"]["push_note"], repos
        assert repos["主仓库"]["commit"], "父仓库本地提交应保留"
        after_parent_remote = Git(bare["parent"]).run(
            "rev-parse", f"refs/heads/{branches['parent']}")
        assert before_parent_remote == after_parent_remote, "父仓库远端不应推进"
        print("ok: push 顺序（子先父后 / 子推送失败时父仓库不推送）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_gitignore_false_positive() -> None:
    """.gitignore 裸目录规则误伤大小写不同的源码目录时提交仍成功。

    caresy 真实案例：.gitignore 的 `data/`（Docker 运行时目录）在 Windows
    大小写不敏感匹配下命中 `src/EOM.Caresy.Infrastructure/Data/`，其内已跟踪
    文件（SeedData.cs）被 git 判为「父目录被忽略」，`git add -A -- <pathspec>`
    整体拒绝。人工提交必须回退 -f 强制暂存，不能整体失败。
    """
    tmp, state, _parent, api, _web = _make_env()
    try:
        # 复刻 caresy 历史：文件先被跟踪，之后才出现误伤的 ignore 规则
        data_dir = api / "src" / "EOM.Caresy.Infrastructure" / "Data"
        data_dir.mkdir(parents=True)
        (data_dir / "SeedData.cs").write_text("seed = 1\n", encoding="utf-8")
        child = Git(api)
        child.add_all()
        child.commit("chore: init seed")
        (api / ".gitignore").write_text("data/\n", encoding="utf-8")
        child.add_all()
        child.commit("chore: add gitignore")
        # 已跟踪文件修改后，add pathspec 因父目录命中 ignore 规则整体拒绝
        (data_dir / "SeedData.cs").write_text("seed = 2\n", encoding="utf-8")
        (api / "src/feature.py").write_text("x = 5\n", encoding="utf-8")

        res = state.manual_commit_project({
            "name": "caresy", "message": "fix: seed data", "push": False})
        repos = {r["label"]: r for r in res["repos"]}
        assert repos["caresy-api"]["commit"], res["repos"]
        head = Git(api).run("rev-parse", "HEAD:src/EOM.Caresy.Infrastructure/Data/SeedData.cs")
        assert head, "SeedData.cs 应被提交"
        assert "seed = 2" in Git(api).run("show", f"{head}"), "应提交最新内容"
        print("ok: gitignore 误伤 Data 目录时回退 -f 暂存，提交不整体失败")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_pending_and_diff()
    check_discard_nested()
    check_manual_commit_nested()
    check_pointer_only()
    check_push_order()
    check_gitignore_false_positive()
    print("\n嵌套仓库人工把关提交全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
