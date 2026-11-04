"""环境引导（env-setup）能力自检：内置技能播种与模板升级、白名单覆盖包管理器
与镜像直下所需 shell/下载工具、审批模式覆盖安装命令、技能经系统提示词注入
Agent、注册表现读用户 PATH（setx 后免重启可见）。

风格与 test_commands_toggle.py 一致：main() + assert，直跑无 pytest。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.approvals import requires_approval
from orcsy.config import AppConfig, LLMConfig, ProjectConfig
from orcsy.executor import TaskExecutor
from orcsy.prompts import build_system_prompt
from orcsy.scheduler import Scheduler
from orcsy.skills import ensure_builtin_skills, load_skills, read_skill_body, skills_base
from orcsy.tools import DEFAULT_ALLOWED_COMMANDS, ToolContext, dispatch_tool
from orcsy.web import AppState


def check_seed() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="envseed-"))
    try:
        base = tmp / "skills"
        seeded = ensure_builtin_skills(base)
        assert seeded == ["env-setup"], seeded
        f = base / "env-setup" / "SKILL.md"
        assert f.is_file()
        # 播种同时写边车哈希（后续模板升级判定「未被用户改动」的依据）
        assert (base / "env-setup" / ".builtin-sha256").is_file()
        body = read_skill_body(f)
        assert "winget" in body and "action=blocked" in body, body[:200]
        # 幂等：二次播种不再写；用户改动不被覆盖
        f.write_text("---\nname: env-setup\ndescription: x\n---\n\n用户改过的正文\n",
                     encoding="utf-8")
        assert ensure_builtin_skills(base) == []
        assert "用户改过的正文" in f.read_text(encoding="utf-8")
        # Scheduler 初始化触发播种（老配置无 skills 目录也能拿到）
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(llm=LLMConfig(api_key="k", base_url="http://localhost:9",
                                      model="m"),
                        source_path=cfg_file)
        Scheduler(cfg)
        assert (skills_base(cfg) / "env-setup" / "SKILL.md").is_file()
        skills = load_skills(skills_base(cfg))
        assert any(s.name == "env-setup" and not s.error for s in skills)
        print("ok: 内置技能播种（幂等/边车哈希/不覆盖用户改动/Scheduler 初始化触发）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_builtin_upgrade() -> None:
    """模板修订后：未改动副本自动升级、用户改过的绝不覆盖、legacy 播种可迁移。"""
    tmp = Path(tempfile.mkdtemp(prefix="envupg-"))
    try:
        import orcsy.skills as S
        tpl_root = tmp / "templates" / "skills" / "env-setup"
        tpl_root.mkdir(parents=True)
        tpl = tpl_root / "SKILL.md"
        tpl.write_text("---\nname: env-setup\n---\n\n模板 v1\n", encoding="utf-8")
        base = tmp / "skills"
        orig_dir, orig_legacy = S.TEMPLATES_DIR, S._LEGACY_BUILTIN_HASHES
        S.TEMPLATES_DIR = tmp / "templates"
        try:
            assert S.ensure_builtin_skills(base) == ["env-setup"]
            md = base / "env-setup" / "SKILL.md"
            assert "模板 v1" in md.read_text(encoding="utf-8")
            assert S.ensure_builtin_skills(base) == []  # 已最新：不动
            # 模板修订 + 副本未改动 → 同步升级，边车哈希刷新
            tpl.write_text("---\nname: env-setup\n---\n\n模板 v2\n", encoding="utf-8")
            assert S.ensure_builtin_skills(base) == ["env-setup"]
            assert "模板 v2" in md.read_text(encoding="utf-8")
            # 用户改动后模板再修订 → 永不覆盖
            md.write_text("---\nname: env-setup\n---\n\n用户改过 v2\n", encoding="utf-8")
            tpl.write_text("---\nname: env-setup\n---\n\n模板 v3\n", encoding="utf-8")
            assert S.ensure_builtin_skills(base) == []
            assert "用户改过 v2" in md.read_text(encoding="utf-8")
            # legacy 迁移：边车机制上线前的播种（无边车文件），内容命中历史
            # 模板哈希 = 未改动 → 升级并补写边车
            legacy_text = "---\nname: env-setup\n---\n\n旧内置版本\n"
            S._LEGACY_BUILTIN_HASHES = {"env-setup": (S._text_sha256(legacy_text),)}
            d2 = tmp / "skills2" / "env-setup"
            d2.mkdir(parents=True)
            (d2 / "SKILL.md").write_text(legacy_text, encoding="utf-8")
            assert S.ensure_builtin_skills(d2.parent) == ["env-setup"]
            assert "模板 v3" in (d2 / "SKILL.md").read_text(encoding="utf-8")
            assert (d2 / ".builtin-sha256").is_file()
            # legacy 哈希不命中（用户改过旧版）→ 不覆盖
            d3 = tmp / "skills3" / "env-setup"
            d3.mkdir(parents=True)
            (d3 / "SKILL.md").write_text("改过的旧版", encoding="utf-8")
            assert S.ensure_builtin_skills(d3.parent) == []
        finally:
            S.TEMPLATES_DIR, S._LEGACY_BUILTIN_HASHES = orig_dir, orig_legacy
        # 真实模板：镜像直下章节齐备，且 legacy 迁移表登记了旧版哈希
        real = (S.TEMPLATES_DIR / "skills" / "env-setup" / "SKILL.md").read_text(
            encoding="utf-8")
        for needle in ("golang.google.cn", "mirrors.aliyun.com/golang",
                       "npmmirror.com/mirrors/node",
                       "mirrors.tuna.tsinghua.edu.cn/Adoptium", "goproxy.cn",
                       "SetEnvironmentVariable", "Expand-Archive", "-TimeoutSec 3",
                       "LOCALAPPDATA", "ProgressPreference", "审批", "probe-before-install",
                       "speedtest_mirrors", "镜像源测速优选"):
            assert needle in real, f"真实模板缺关键内容：{needle}"
        assert S._LEGACY_BUILTIN_HASHES.get("env-setup"), "legacy 迁移表应有旧版哈希"
        print("ok: 内置技能升级（未改动副本随模板刷新/用户改动不覆盖/legacy 迁移/真实模板镜像章节）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_whitelist_and_approval() -> None:
    # 包管理器在默认白名单（通配符 winget* 覆盖 winget）
    assert "winget*" in DEFAULT_ALLOWED_COMMANDS and "choco" in DEFAULT_ALLOWED_COMMANDS
    # 镜像直下流程（技能阶段二B）所需的 shell/下载/解压/PATH 注册工具
    for c in ("powershell", "pwsh", "curl", "tar", "setx"):
        assert c in DEFAULT_ALLOWED_COMMANDS, c
    # POSIX 只读探测/统计（模型高频误用被拦的 wc/head/tail/grep 等已放行）；
    # 能写文件或执行任意命令的坚决不放
    for c in ("ls", "cat", "wc", "head", "tail", "grep", "sort", "uniq",
              "cut", "tr", "diff", "stat", "touch", "sha256sum"):
        assert c in DEFAULT_ALLOWED_COMMANDS, c
    for c in ("sed", "awk", "rm", "mv", "cp", "xargs", "bash", "sh", "env",
              "find", "chmod"):
        assert c not in DEFAULT_ALLOWED_COMMANDS, c
    tmp = Path(tempfile.mkdtemp(prefix="envwl-"))
    try:
        ctx = ToolContext(root=tmp)  # None = 默认全表
        # FileNotFoundError 分支（机器上没有 winget 也返回指引而非崩溃）
        r = dispatch_tool(ctx, "run_command", {"command": ["winget", "install", "x"]})
        assert r.startswith(("退出码", "错误：命令")), r[:80]
        # powershell 真跑一条（阶段二B 的探测/下载/注册 PATH 全靠它）
        if os.name == "nt":
            r = dispatch_tool(ctx, "run_command", {"command": [
                "powershell", "-NoProfile", "-Command", "1+1"]})
            assert r.startswith("退出码 0") and "2" in r, r
        # 安装命令受审批约束：autonomous/ask_on_change 都因 run_command 询问
        assert requires_approval("autonomous", "run_command") is True
        assert requires_approval("ask_on_change", "run_command") is True
        assert requires_approval("full_access", "run_command") is False
        print("ok: 包管理器/镜像直下工具白名单 + powershell 可用 + 安装命令受审批约束")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_registry_path_merge() -> None:
    """注册表现读用户 PATH：setx 注册的新目录免重启即进子进程环境并可解析新命令。"""
    import orcsy.tools as T
    if os.name == "nt":
        entries = T._registry_path_entries()
        assert isinstance(entries, list) and entries, "Windows 注册表 PATH 不应为空"
        assert all("%" not in e for e in entries), "REG_EXPAND_SZ 应已展开"
    tmp = Path(tempfile.mkdtemp(prefix="envreg-"))
    try:
        # 受控验证：假装注册表里多了一个新装工具目录
        fake = tmp / "freshdir"
        fake.mkdir()
        tool = fake / "zzfresh-tool.exe" if os.name == "nt" else fake / "zzfresh-tool"
        tool.write_text("x", encoding="utf-8")  # 非 0 字节（跳过执行别名陷阱）
        orig = T._registry_path_entries
        T._registry_path_entries = lambda: [str(fake)]
        try:
            assert str(fake) in T._tool_path_dirs()
            # 不变量：_tool_path_dirs 返回的目录绝不与进程 PATH 重复（去重）
            proc_entries = [p for p in os.environ.get("PATH", "").split(os.pathsep)
                            if p.strip()]
            keys = {T._path_key(p) for p in proc_entries}
            T._registry_path_entries = lambda: list(proc_entries)
            assert not (keys & {T._path_key(d) for d in T._tool_path_dirs()}), \
                "已在进程 PATH 的目录不应重复追加"
            T._registry_path_entries = lambda: [str(fake)]
            env = T._augmented_env()
            assert str(fake) in env["PATH"].split(os.pathsep), "新注册目录应进子进程 PATH"
            resolved = T._resolve_new_install(["zzfresh-tool", "run"])
            assert resolved[0] == str(tool), resolved
        finally:
            T._registry_path_entries = orig
        print("ok: 注册表现读用户 PATH（新目录免重启可见/去重/新命令解析）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_skill_injection() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="envinj-"))
    try:
        base = tmp / "skills"
        ensure_builtin_skills(base)
        skills = [s for s in load_skills(base) if s.name == "env-setup"]
        proj = ProjectConfig(name="p", path=tmp)
        llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m")
        # 系统提示词注入技能索引（Agent 据此按需 read_skill）
        prompt = build_system_prompt(proj, "# 指引", False, skills=skills)
        assert "env-setup" in prompt and "环境引导" in prompt, prompt[:300]
        # 执行器把技能正文预载进 ToolContext：read_skill 可读全文
        ex = TaskExecutor(proj, llm)
        from orcsy.skills import skill_bodies
        bodies = skill_bodies(skills)
        assert "env-setup" in bodies
        ctx = ToolContext(root=tmp, skills=bodies)
        out = dispatch_tool(ctx, "read_skill", {"name": "env-setup"})
        assert "winget" in out and "scoop" in out, out[:120]
        print("ok: 技能索引入提示词 + read_skill 全文可用")

        # 新装工具可发现性：绝对路径按 basename 过白名单（go.exe ≡ go）；
        # 新装目录自动解析；扩充 PATH 进子进程环境
        from orcsy.tools import _augmented_env, _resolve_new_install, _tool_path_dirs
        exe_dir = tmp / "Go" / "bin"
        exe_dir.mkdir(parents=True)
        fake_go = exe_dir / "go.exe"
        fake_go.write_text("", encoding="utf-8")
        # 把目录塞进探测表尾部做受控测试（_TOOL_PATH_DIRS 本身不可变的兜底验证
        # 由 _resolve_new_install 的目录参数化保证）
        import orcsy.tools as T
        orig_dirs = T._TOOL_PATH_DIRS
        T._TOOL_PATH_DIRS = orig_dirs + (str(exe_dir),)
        try:
            resolved = _resolve_new_install(["go", "version"])
            assert resolved[0].lower().endswith("go.exe"), resolved
            ctx = ToolContext(root=tmp, allowed_commands=["go"])
            # 绝对路径调用：basename go.exe → go 过白名单，自动解析到安装目录
            r = dispatch_tool(ctx, "run_command",
                              {"command": [str(fake_go), "version"]})
            assert "不在白名单" not in r, r
            env = _augmented_env()
            assert str(exe_dir) in env["PATH"], "扩充 PATH 应含新装目录"
            assert _tool_path_dirs(), "真实机器应至少探测到一个工具目录"
        finally:
            T._TOOL_PATH_DIRS = orig_dirs
        print("ok: 新装工具可发现（绝对路径 basename 过白名单/目录解析/扩充 PATH）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_seed()
    check_builtin_upgrade()
    check_whitelist_and_approval()
    check_registry_path_merge()
    check_skill_injection()
    print("\n环境引导能力自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
