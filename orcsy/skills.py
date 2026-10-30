"""技能库：可复用的 Agent 能力包，集中存放在配置目录下，绝不写入项目目录。

每个技能是一个目录 <配置目录>/skills/<技能名>/，内含 SKILL.md（YAML frontmatter
的 name/description/scope/enabled + Markdown 正文），导入时保留脚本等附属文件。
技能对 Agent 的生效方式：系统提示词只注入「名称+描述」索引，Agent 需要时通过
read_skill 工具读取全文（内容由执行器预载，绕开项目根沙箱，见 tools.py）。

scope 语义：global 对所有项目生效；值为项目名时仅该项目生效（项目专属技能）。
项目删除后遗留的专属 scope 永不匹配任何项目，无害；项目改名由 web 层重映射。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import AppConfig
from .logging_setup import get_logger

log = get_logger("skills")

# 新建技能的命名约束（与 web 层项目名一致：字母数字/下划线/连字符/中文，不以符号开头）
_NAME_RE = re.compile(r"^[\w][\w\-]*$")

# 导入目录名可能带点/空格（第三方技能库），只挡路径穿越与控制字符
_SAFE_COMPONENT_RE = re.compile(r"^[^\\/:\*\?\"<>|\x00-\x1f]{1,80}$")

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

# 常见 AI 工具的用户级技能目录（目录不存在时不出现在扫描结果里）。
# OpenCode 用 xdg 风格路径且目录名为单复数两种约定，都扫
SCAN_SOURCES: list[tuple[str, str]] = [
    ("ZCode", "~/.zcode/skills"),
    ("通用 Agent", "~/.agents/skills"),
    ("Claude Code", "~/.claude/skills"),
    ("Codex", "~/.codex/skills"),
    ("OpenCode", "~/.config/opencode/skill"),
    ("OpenCode", "~/.config/opencode/skills"),
]


@dataclass
class Skill:
    """一个已解析的技能目录；解析失败时 error 非空（容错，不崩掉列表）。"""

    name: str
    description: str
    scope: str            # "global" 或项目名
    enabled: bool
    path: Path            # SKILL.md 路径
    origin: str = "imported"  # created=本程序新建（可硬删除）；imported=外部导入（只能移除）
    error: str = ""


def skills_base(config: AppConfig) -> Path:
    """技能库根目录：与 config.yaml 同级的 skills/（即 文档\\Orcsy\\skills）。"""
    base = config.source_path.parent / "skills" if config.source_path else Path("skills")
    return base.resolve()


def skill_dir(base: Path, name: str) -> Path:
    """把技能名解析为技能库内的目录，拒绝穿越（名字校验 + resolve 包含检查）。"""
    if not _SAFE_COMPONENT_RE.fullmatch(name) or name in (".", ".."):
        raise ValueError(f"技能名不合法：{name!r}")
    d = (base / name).resolve()
    if base.resolve() != d.parent:
        raise ValueError("技能路径越界")
    return d


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """返回 (frontmatter dict, 去掉 frontmatter 后的正文)。"""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
                if isinstance(meta, dict):
                    return meta, parts[2]
            except yaml.YAMLError:
                pass
    return {}, text


def parse_skill_dir(d: Path) -> Skill:
    """解析一个技能目录（目录名即技能名）。"""
    name = d.name
    path = d / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return Skill(name=name, description="", scope="global", enabled=True,
                     path=path, error=f"无法读取 SKILL.md：{e}")
    meta, _ = _parse_frontmatter(text)
    if not meta:
        return Skill(name=name, description="", scope="global", enabled=True,
                     path=path, error="SKILL.md 缺少合法的 frontmatter")
    return Skill(
        name=name,
        description=str(meta.get("description") or "").strip(),
        scope=str(meta.get("scope") or "global").strip() or "global",
        enabled=bool(meta.get("enabled", True)),
        path=path,
        origin="created" if str(meta.get("origin") or "") == "created" else "imported",
    )


def load_skills(base: Path) -> list[Skill]:
    """扫描技能库全部技能（含停用与解析失败的，按名称排序）。"""
    if not base.is_dir():
        return []
    out = [parse_skill_dir(d) for d in sorted(base.iterdir())
           if d.is_dir() and (d / "SKILL.md").is_file()]
    return sorted(out, key=lambda s: s.name.lower())


def skills_for_project(skills: list[Skill], project_name: str) -> list[Skill]:
    """过滤出对某项目生效的技能：启用 且（全局 或 专属该项目）。"""
    return [s for s in skills
            if s.enabled and not s.error
            and (s.scope == "global" or s.scope == project_name)]


def read_skill_body(path: Path) -> str:
    """读取技能正文（去掉 frontmatter），read_skill 工具回传给模型的内容。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    _, body = _parse_frontmatter(text)
    return body.strip()


def skill_bodies(skills: list[Skill]) -> dict[str, str]:
    """把技能列表预载为 {名称: 正文}，供 ToolContext.skills 使用。"""
    bodies: dict[str, str] = {}
    for s in skills:
        body = read_skill_body(s.path)
        if body:
            bodies[s.name] = body
    return bodies


def _write_skill_md(d: Path, name: str, description: str, scope: str,
                    enabled: bool, body: str, origin: str = "imported") -> Path:
    meta = {"name": name, "description": description,
            "scope": scope, "enabled": bool(enabled), "origin": origin}
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    path = d / "SKILL.md"
    path.write_text(f"---\n{fm}\n---\n\n{body.strip()}\n", encoding="utf-8")
    return path


def write_skill(base: Path, name: str, description: str, scope: str,
                enabled: bool, body: str) -> Path:
    """新建技能目录并写入规范 SKILL.md，返回 SKILL.md 路径。调用方负责冲突检查。

    本程序新建的技能带 origin: created 标记，允许硬删除。
    """
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"技能名不合法：{name!r}（字母数字/下划线/连字符，且不以符号开头）")
    if not body.strip():
        raise ValueError("技能内容不能为空")
    d = skill_dir(base, name)
    d.mkdir(parents=True, exist_ok=False)
    return _write_skill_md(d, name, description, scope, enabled, body, origin="created")


def update_skill(d: Path, description: str, scope: str,
                 enabled: bool, body: str) -> None:
    """整体重写技能的 SKILL.md（保留目录内其它附属文件与 origin 来源标记）。"""
    if not body.strip():
        raise ValueError("技能内容不能为空")
    _write_skill_md(d, d.name, description, scope, enabled, body,
                    origin=parse_skill_dir(d).origin)


def rename_skill_dir(base: Path, old: str, new: str) -> Path:
    """技能改名 = 目录改名。new 需通过 _NAME_RE（看板输入），且目标不存在。"""
    src = skill_dir(base, old)
    if not src.is_dir():
        raise ValueError(f"技能不存在：{old}")
    if not _NAME_RE.fullmatch(new):
        raise ValueError(f"新技能名不合法：{new!r}（字母数字/下划线/连字符，且不以符号开头）")
    dst = skill_dir(base, new)
    if dst.exists():
        raise ValueError(f"技能名已存在：{new}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    # frontmatter 的 name 字段同步，保持与目录名一致
    md = dst / "SKILL.md"
    try:
        text = md.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if m and re.search(r"^name:", m.group(1), re.MULTILINE):
            front = re.sub(r"^name:.*$", f"name: {new}", m.group(1), flags=re.MULTILINE)
            md.write_text(f"---\n{front}\n---\n{text[m.end():]}", encoding="utf-8")
    except OSError as e:
        log.warning("改名后同步 SKILL.md name 字段失败：%s", e)
    return dst


def set_skill_meta(path: Path, scope: str | None = None,
                   enabled: bool | None = None) -> None:
    """定向修改 SKILL.md frontmatter 的 scope/enabled，保留其它键（导入兼容）。"""
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"SKILL.md 缺少 frontmatter，无法修改：{path}")
    front, rest = m.group(1), text[m.end():]
    for key, value in (("scope", scope), ("enabled", enabled)):
        if value is None:
            continue
        line = yaml.safe_dump({key: value}, allow_unicode=True).strip()
        if re.search(rf"^{key}:", front, re.MULTILINE):
            front = re.sub(rf"^{key}:.*$", line, front, flags=re.MULTILINE)
        else:
            front = front.rstrip("\n") + f"\n{line}"
    path.write_text(f"---\n{front}\n---\n{rest}", encoding="utf-8")


def resolve_import_name(src: Path) -> str:
    """导入后的技能名：SKILL.md frontmatter 的 name（合法时）优先，否则用目录名。"""
    meta: dict = {}
    try:
        meta, _ = _parse_frontmatter((src / "SKILL.md").read_text(encoding="utf-8"))
    except OSError:
        pass
    name = str(meta.get("name") or "").strip()
    if name and _SAFE_COMPONENT_RE.fullmatch(name) and name not in (".", ".."):
        return name
    return src.name


def import_skill(src: Path, base: Path) -> Skill:
    """把本地技能目录整库复制进技能库（含脚本等附属文件），返回解析结果。

    技能名优先取 SKILL.md frontmatter 的 name（校验合法性），否则用目录名。
    """
    if not src.is_dir():
        raise ValueError(f"目录不存在：{src}")
    if not (src / "SKILL.md").is_file():
        raise ValueError(f"所选目录缺少 SKILL.md，不像技能目录：{src}")
    name = resolve_import_name(src)
    if not _SAFE_COMPONENT_RE.fullmatch(name) or name in (".", ".."):
        raise ValueError(f"技能名不合法：{name!r}，请改用合规目录名或在 SKILL.md 里写 name")
    base.mkdir(parents=True, exist_ok=True)
    dst = skill_dir(base, name)
    if dst.exists():
        raise ValueError(f"技能库中已存在同名技能：{name}")
    try:
        shutil.copytree(src, dst)
    except OSError as e:
        raise ValueError(f"复制技能目录失败：{e}")
    skill = parse_skill_dir(dst)
    log.info("导入技能 %s ← %s", name, src)
    return skill


def delete_skill(base: Path, name: str) -> None:
    """从技能库删除技能目录（含附属文件）。

    外部导入的技能原样本仍在各 AI 工具目录里，重新扫描即可找回；本程序新建
    的技能没有别处副本，删除后不可恢复——调用方负责在确认文案里说清差别。
    """
    d = skill_dir(base, name)
    if not d.is_dir():
        raise ValueError(f"技能不存在：{name}")
    shutil.rmtree(d)
    log.info("删除技能 %s（%s）", name, d)


def scan_source_dir(label: str, base: Path, target: Path | None = None,
                    seen: set[str] | None = None) -> dict | None:
    """扫描单个技能目录集合（直接子目录含 SKILL.md 视为一个技能）。

    条目带 resolve_import_name 预解析的最终技能名；给定 target（本系统技能库
    根目录）时用 exists 标记是否已导入过。seen 为跨来源的去重集合——同名技能
    （导入时本就按名冲突）只显示第一次出现的来源，seen 记录已列出的名字。
    目录不存在或出错返回 None。
    """
    if not base.is_dir():
        return None
    seen = seen if seen is not None else set()
    entries: list[dict] = []
    try:
        children = sorted(base.iterdir(), key=lambda x: x.name.lower())
        for d in children:
            if not (d.is_dir() and (d / "SKILL.md").is_file()):
                continue
            name = resolve_import_name(d)
            if name in seen:
                continue
            s = parse_skill_dir(d)
            exists = False
            if target is not None:
                try:
                    exists = (target / name).exists()
                except OSError:
                    exists = False
            entries.append({
                "name": name,
                "description": s.description,
                "path": str(d),
                "exists": exists,
            })
            seen.add(name)
    except OSError as e:
        log.warning("扫描技能来源 %s 失败（跳过）：%s", base, e)
        return None
    return {"tool": label, "path": str(base), "skills": entries} if entries else None


def scan_sources(target: Path | None = None,
                 seen: set[str] | None = None) -> list[dict]:
    """扫描常见 AI 工具的用户级技能目录，返回可导入的技能清单（按工具分组）。

    同名技能跨来源只保留第一次出现的（SCAN_SOURCES 顺序即优先级）。
    """
    out: list[dict] = []
    seen = seen if seen is not None else set()
    for label, raw in SCAN_SOURCES:
        g = scan_source_dir(label, Path(os.path.expandvars(raw)).expanduser(),
                            target, seen)
        if g:
            out.append(g)
    return out
