"""技能库（导入/新建/项目专属）的端到端自检（自起服务，不依赖运行中的看板）。

覆盖：
1. 新建技能：落盘到配置目录 skills/（不落项目目录），frontmatter 规范，/api/state 可见；
2. 导入本地技能目录：整目录复制（含附属文件），frontmatter name 生效，同名冲突拦截；
3. 适用范围（global / 项目专属）与启停开关，skills_for_project 过滤正确；
4. 系统提示词只注入索引节，read_skill 工具按需读取全文、未知名报可用列表；
5. 技能改名/删除（清掉技能库副本；导入技能原样本在工具目录，重扫可找回）；项目改名后专属技能 scope 自动重映射。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台打不出 ✅ 等字符
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from orcsy.config import load_config
from orcsy.prompts import build_system_prompt
from orcsy.scheduler import Scheduler
from orcsy.skills import (load_skills, parse_skill_dir, skill_dir,
                          skills_for_project)
from orcsy.tools import ToolContext, dispatch_tool
from orcsy.web import AppState, make_server


def make_config(root: Path) -> Path:
    cfg = root / "config.yaml"
    cfg.write_text(
        "llm:\n"
        "  provider: openai\n"
        '  api_key: "sk-test"\n'
        "  base_url: https://api.example.com/v1\n"
        "  model: test-model\n"
        "scheduler:\n"
        "  interval_seconds: 300\n"
        "logging:\n"
        f"  file: {root / 'logs' / 'test.log'}\n",
        encoding="utf-8",
    )
    return cfg


def api(port: int, path: str, body: dict | None = None) -> dict:
    # Windows 本机回环偶发 ConnectionResetError（快速连断竞态），重试两次
    last_err: Exception | None = None
    for _ in range(3):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())
        except ConnectionResetError as e:
            last_err = e
    raise last_err  # type: ignore[misc]


def state_skills(port: int) -> tuple[list[dict], str]:
    s = api(port, "/api/state")
    return s["skills"], s["skills_dir"]


def check_create(root: Path, port: int, skills_dir: Path) -> None:
    # 前置：建一个受管项目，用于项目专属 scope 与「不落项目目录」断言
    proj = root / "proj-a"
    proj.mkdir()
    r = api(port, "/api/project", {"name": "proj-a", "path": str(proj)})
    assert r.get("ok"), r
    before = {p.name for p in proj.iterdir()}

    # 新建全局技能
    r = api(port, "/api/skill", {
        "name": "commit-style", "description": "Conventional Commits 提交规范",
        "scope": "global", "enabled": True,
        "body": "# 提交规范\n\n所有提交信息使用 Conventional Commits。",
    })
    assert r.get("ok"), r
    md = skill_dir(skills_dir, "commit-style") / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    assert text.startswith("---\n") and "name: commit-style" in text, text
    assert "scope: global" in text and "enabled: true" in text, text
    assert "origin: created" in text, "本程序新建的技能应带 created 标记"
    assert "# 提交规范" in text, text

    skills, sd = state_skills(port)
    assert sd == str(skills_dir), sd
    entry = next(s for s in skills if s["name"] == "commit-style")
    assert entry["origin"] == "created", entry

    # 编辑技能：origin 来源标记在整体重写 frontmatter 时保留
    r = api(port, "/api/skill", {"original_name": "commit-style", "name": "commit-style",
                                 "description": "更新后的描述", "scope": "global",
                                 "enabled": True, "body": "新正文内容"})
    assert r.get("ok"), r
    g = api(port, "/api/skill?name=commit-style")
    assert g.get("ok") and g["origin"] == "created", g
    # 不落项目目录：项目目录内容不变
    after = {p.name for p in proj.iterdir()}
    assert after - before == set() or after - before == {"tasks", "AGENT.md"}, after
    assert not (proj / "skills").exists() and not (proj / "SKILL.md").exists(), after
    print("1. 新建技能：落盘技能库、frontmatter 规范、项目目录零写入 ✅")

    # 名字冲突与非法名
    r = api(port, "/api/skill", {"name": "commit-style", "description": "x", "body": "y"})
    assert not r.get("ok") and "已存在" in r.get("message", ""), r
    r = api(port, "/api/skill", {"name": "../escape", "description": "x", "body": "y"})
    assert not r.get("ok"), r
    print("2. 新建技能：同名冲突与路径穿越均被拦截 ✅")


def check_import(root: Path, port: int, skills_dir: Path) -> None:
    # 伪本地技能目录：SKILL.md（带 name/description + 保留自定义键）+ 附属脚本
    local = root / "my-local-skill"
    local.mkdir()
    (local / "SKILL.md").write_text(
        "---\n"
        "name: imported-skill\n"
        "description: 从本地导入的技能\n"
        "license: MIT\n"
        "---\n"
        "# 导入技能\n\n正文内容。\n",
        encoding="utf-8")
    (local / "scripts").mkdir()
    (local / "scripts" / "helper.py").write_text("print('hi')\n", encoding="utf-8")

    r = api(port, "/api/skill/import", {"path": str(local)})
    assert r.get("ok") and r["skill"]["name"] == "imported-skill", r
    dst = skill_dir(skills_dir, "imported-skill")
    assert (dst / "SKILL.md").is_file() and (dst / "scripts" / "helper.py").is_file(), "整目录复制"
    # frontmatter 自定义键保留、scope 缺省为 global
    s = parse_skill_dir(dst)
    assert s.scope == "global" and s.enabled, (s.scope, s.enabled)
    text = (dst / "SKILL.md").read_text(encoding="utf-8")
    assert "license: MIT" in text, text
    # 原目录不受影响
    assert (local / "SKILL.md").is_file(), "原目录不应被移动/删除"

    # 同名再次导入 → 冲突
    r = api(port, "/api/skill/import", {"path": str(local)})
    assert not r.get("ok") and "已存在" in r.get("message", ""), r
    # 缺 SKILL.md 的目录 → 拒绝
    empty = root / "not-a-skill"
    empty.mkdir()
    r = api(port, "/api/skill/import", {"path": str(empty)})
    assert not r.get("ok") and "SKILL.md" in r.get("message", ""), r
    print("3. 导入本地技能：整目录复制、自定义键保留、冲突与缺 SKILL.md 拦截 ✅")


def check_scope_and_enabled(root: Path, port: int, skills_dir: Path) -> None:
    # 专属技能：scope 绑定 proj-a
    r = api(port, "/api/skill", {
        "name": "proj-skill", "description": "仅 proj-a 可见",
        "scope": "proj-a", "enabled": True, "body": "项目专属规范。",
    })
    assert r.get("ok"), r
    all_skills = load_skills(skills_dir)
    in_a = [s.name for s in skills_for_project(all_skills, "proj-a")]
    in_b = [s.name for s in skills_for_project(all_skills, "other-proj")]
    assert "proj-skill" in in_a and "imported-skill" in in_a, in_a
    assert "proj-skill" not in in_b and "imported-skill" in in_b, in_b
    print("4. 适用范围：全局技能全项目生效、专属技能仅绑定项目生效 ✅")

    # 启停开关：停用后 skills_for_project 不再返回
    r = api(port, "/api/skill/enabled", {"name": "proj-skill", "enabled": False})
    assert r.get("ok"), r
    all_skills = load_skills(skills_dir)
    assert "proj-skill" not in [s.name for s in skills_for_project(all_skills, "proj-a")]
    text = (skill_dir(skills_dir, "proj-skill") / "SKILL.md").read_text(encoding="utf-8")
    assert "enabled: false" in text, text
    # 重新启用
    r = api(port, "/api/skill/enabled", {"name": "proj-skill", "enabled": True})
    assert r.get("ok"), r
    print("5. 启停开关：定向改 frontmatter，停用技能退出过滤 ✅")


def check_prompt_and_tool(root: Path, port: int, skills_dir: Path) -> None:
    proj = root / "proj-a"
    from orcsy.config import ProjectConfig

    pc = ProjectConfig(name="proj-a", path=proj)
    all_skills = load_skills(skills_dir)
    active = skills_for_project(all_skills, "proj-a")

    # 带技能：索引节出现，只有名称+描述（不含正文）
    prompt = build_system_prompt(pc, "# 指引", False, skills=active)
    assert "# Skills 技能库" in prompt and "read_skill" in prompt, prompt
    assert "- imported-skill：从本地导入的技能" in prompt, prompt
    assert "# 导入技能" not in prompt, "正文不应进入系统提示词"

    # 不带技能：无技能节
    prompt_plain = build_system_prompt(pc, "# 指引", False)
    assert "# Skills 技能库" not in prompt_plain, prompt_plain

    # read_skill 工具：预载正文后按名读取；未知名给出可用列表
    bodies = {"imported-skill": "导入技能全文内容"}
    ctx = ToolContext(root=proj, skills=bodies)
    out = dispatch_tool(ctx, "read_skill", {"name": "imported-skill"})
    assert out == "导入技能全文内容", out
    out = dispatch_tool(ctx, "read_skill", {"name": "no-such"})
    assert "no-such" in out and "imported-skill" in out, out
    # 未预载技能时友好报错
    out = dispatch_tool(ToolContext(root=proj), "read_skill", {"name": "x"})
    assert "没有可用的技能" in out, out
    print("6. 提示词索引 + read_skill 工具：索引不含正文、按需读取与未知名报错 ✅")


def check_rename_remove_remap(root: Path, port: int, skills_dir: Path) -> None:
    # 技能改名（目录改名）与改名冲突
    api(port, "/api/skill", {"name": "to-rename", "description": "d", "body": "b"})
    r = api(port, "/api/skill", {
        "original_name": "to-rename", "name": "renamed-skill",
        "description": "d2", "scope": "global", "enabled": True, "body": "b2",
    })
    assert r.get("ok"), r
    assert not (skills_dir / "to-rename").exists()
    assert (skill_dir(skills_dir, "renamed-skill") / "SKILL.md").is_file()
    r = api(port, "/api/skill", {
        "original_name": "renamed-skill", "name": "imported-skill",
        "description": "d", "scope": "global", "enabled": True, "body": "b",
    })
    assert not r.get("ok") and "已存在" in r.get("message", ""), r
    print("7. 技能改名：目录改名生效、目标名冲突拦截 ✅")

    # 项目改名 → 专属技能 scope 自动重映射
    r = api(port, "/api/skill", {
        "name": "remap-skill", "description": "随项目改名",
        "scope": "proj-a", "enabled": True, "body": "x",
    })
    assert r.get("ok"), r
    r = api(port, "/api/project", {"name": "proj-b", "path": str(root / "proj-a"),
                                   "original_name": "proj-a"})
    assert r.get("ok"), r
    s = api(port, "/api/skill?name=remap-skill")
    assert s.get("ok") and s["scope"] == "proj-b", s
    print("8. 项目改名：专属技能 scope 自动重映射 ✅")

    # 删除技能：清掉技能库副本，不再有 skills-removed 中转（导入技能可重扫找回）
    r = api(port, "/api/skill/remove", {"names": ["remap-skill", "no-such"]})
    assert r.get("ok") and r["removed"] == ["remap-skill"], r
    assert len(r["skipped"]) == 1 and "不存在" in r["skipped"][0]["reason"], r
    assert not (skills_dir / "remap-skill").exists()
    assert not (skills_dir.parent / "skills-removed").exists(), "不应生成 skills-removed 目录"
    skills, _ = state_skills(port)
    assert "remap-skill" not in [x["name"] for x in skills], skills
    r = api(port, "/api/skill/remove", {"names": []})
    assert not r.get("ok"), r
    # 外部导入技能同样可删：副本删掉，原样本仍在工具目录（重扫可找回，见第 10 项）
    r = api(port, "/api/skill/remove", {"names": ["imported-skill"]})
    assert r.get("ok") and r["removed"] == ["imported-skill"], r
    assert not (skills_dir / "imported-skill").exists()
    print("9. 删除技能：清副本无中转目录、批量与拦截、导入技能同样可删 ✅")


def check_scan_and_batch(root: Path, port: int, skills_dir: Path) -> None:
    """扫描常见 AI 工具技能目录 + 批量导入（同名已存在跳过）。"""
    import os

    # 在假 USERPROFILE 下伪造 ZCode / Claude Code 的技能目录
    home = root / "fake-home"
    zcode = home / ".zcode" / "skills" / "vue-bp"
    claude = home / ".claude" / "skills" / "plain-skill"
    zcode.mkdir(parents=True)
    claude.mkdir(parents=True)
    (zcode / "SKILL.md").write_text(
        "---\nname: scanned-foo\ndescription: ZCode 里的技能\n---\nZCode 技能正文。\n",
        encoding="utf-8")
    (claude / "SKILL.md").write_text(
        "---\ndescription: 无 name 字段的技能\n---\nClaude 技能正文。\n", encoding="utf-8")
    # 同名重复：同来源的变体目录 + 跨来源的重复部署，都应被去重
    dup_same = home / ".zcode" / "skills" / "vue-bp-2"
    dup_cross = home / ".agents" / "skills" / "scanned-foo"
    dup_same.mkdir(parents=True)
    dup_cross.mkdir(parents=True)
    for d in (dup_same, dup_cross):
        (d / "SKILL.md").write_text(
            "---\nname: scanned-foo\ndescription: 重复部署的副本\n---\n重复正文。\n",
            encoding="utf-8")
    # 非技能子目录（缺 SKILL.md）不应出现在扫描结果
    (home / ".zcode" / "skills" / "not-a-skill").mkdir()

    old_home = os.environ.get("USERPROFILE")
    os.environ["USERPROFILE"] = str(home)
    try:
        s = api(port, "/api/skill/scan")
        sources = {x["tool"]: x for x in s["sources"]}
        assert "ZCode" in sources and "Claude Code" in sources, s
        zskills = {k["name"]: k for k in sources["ZCode"]["skills"]}
        assert "scanned-foo" in zskills and "not-a-skill" not in zskills, zskills
        assert zskills["scanned-foo"]["description"] == "ZCode 里的技能", zskills
        assert zskills["scanned-foo"]["exists"] is False
        # 同名去重：同来源变体与跨来源副本都不再出现
        assert sum(1 for k in sources["ZCode"]["skills"] if k["name"] == "scanned-foo") == 1
        agent = sources.get("通用 Agent")
        assert not agent or "scanned-foo" not in {k["name"] for k in agent["skills"]}, agent
        assert "plain-skill" in {k["name"] for k in sources["Claude Code"]["skills"]}, s

        # 批量导入：frontmatter name 优先，无 name 用目录名
        r = api(port, "/api/skill/import-batch",
                {"paths": [str(zcode), str(claude)]})
        assert r.get("ok") and r["imported"] == ["scanned-foo", "plain-skill"], r
        assert (skills_dir / "scanned-foo" / "SKILL.md").is_file()
        assert (skills_dir / "plain-skill" / "SKILL.md").is_file()

        # 重复导入 → 全部跳过不报错
        r = api(port, "/api/skill/import-batch", {"paths": [str(zcode), str(claude)]})
        assert r.get("ok") and r["imported"] == [] and len(r["skipped"]) == 2, r
        assert "已存在" in r["skipped"][0]["reason"], r

        # 扫描标记已导入；空选择报错
        s = api(port, "/api/skill/scan")
        zskills = {k["name"]: k for x in s["sources"] if x["tool"] == "ZCode"
                   for k in x["skills"]}
        assert zskills["scanned-foo"]["exists"] is True, zskills
        r = api(port, "/api/skill/import-batch", {"paths": []})
        assert not r.get("ok"), r
    finally:
        if old_home is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_home
    print("10. 扫描 AI 工具技能：分组发现、同名去重、批量导入、重复跳过、已导入标记 ✅")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="orcsy-skills-"))
    try:
        cfg_path = make_config(root)
        config = load_config(cfg_path, require_api_key=False)
        skills_dir = config.source_path.parent / "skills"

        scheduler = Scheduler(config)
        state = AppState(config, scheduler, log_path=str(root / "logs" / "test.log"))
        assert str(state.skills_dir) == str(skills_dir), state.skills_dir
        server = make_server(state, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        check_create(root, port, skills_dir)
        check_import(root, port, skills_dir)
        check_scope_and_enabled(root, port, skills_dir)
        check_prompt_and_tool(root, port, skills_dir)
        check_rename_remove_remap(root, port, skills_dir)
        check_scan_and_batch(root, port, skills_dir)

        server.shutdown()
        server.server_close()
        print("\n技能库本地自检全部通过 ✅")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
