"""提示词构建：把 AGENT.md 指引与任务协议注入系统提示词。

工作模型分两种情况：
- 项目是 git 仓库：Agent 在独立 worktree（worktrees/<name>）里改代码，主仓库
  由本系统负责提交合并，避免 Agent 直接碰主工作区造成混乱。
- 项目不是 git 仓库：Agent 直接在项目目录里改，靠任务目录做简单隔离。
"""

from __future__ import annotations

from pathlib import Path

from .config import ProjectConfig
from .logging_setup import get_logger
from .skills import Skill

log = get_logger("prompts")

OUTPUT_CONTRACT_HEADER = """\
## 工作方式与结束协议（必须严格遵守）

"""

_FINISH_PROTOCOL = """\

结束任务时**必须调用 finish 工具**，参数字段：

- action："completed"（已完成且按指引验证通过）| "blocked"（尝试后受阻，notes 写清原因）
  | "cannot_do"（超出能力或需人工决策，notes 说明原因）
- summary：一句话说明做了什么或为何受阻
- files_changed：相对项目根的变更文件路径数组；未改动则空数组
- commit_message：Conventional Commits 风格提交信息（如 fix: ... / feat: ...）；
  action 非 completed 时留空
- notes：补充说明、未尽事项；无则留空

不要用纯文本回复结论；不要自己执行 git commit / git push（系统统一处理）。
"""


def _output_contract(enabled: frozenset[str] | None = None) -> str:
    """按启用工具集动态生成工作方式与资源约定。

    enabled=None（全启用）时文案覆盖全部 13 个工具；被禁用的工具不出现在
    提示词里——模型反复尝试调用一个 schema 里不存在的工具纯属浪费轮次。
    """
    def on(name: str) -> bool:
        return enabled is None or name in enabled

    clauses: list[str] = []
    explore = [n for n in ("list_dir", "find_files", "read_file", "grep") if on(n)]
    if explore:
        clauses.append(f"{' / '.join(explore)} 了解项目")
    modify = [n for n in ("write_file", "edit_file", "multi_edit",
                          "delete_file", "move_file") if on(n)]
    if modify:
        clauses.append(f"{' / '.join(modify)} 修改代码")
    if on("run_command"):
        clauses.append("run_command 运行测试与构建")
    if on("web_fetch"):
        clauses.append("web_fetch 查阅公网文档资料")
    work_line = ("你通过工具完成一切工作：" + "，".join(clauses) + "。") if clauses else (
        "你当前没有可用的工作工具：直接调用 finish（action=cannot_do）说明受阻原因。")

    bullets: list[str] = []
    if on("grep") and on("read_file"):
        bullets.append(
            "- 大文件先用 grep 定位行号，再用 read_file 的 offset/limit 只读需要的区段；\n"
            "  禁止为了\"看看结构\"整读大文件，禁止重复读取未变化的内容。")
    if on("list_dir") or on("find_files"):
        bullets.append("- list_dir / find_files 用于初次了解结构与定位文件，不要反复调用。")
    if on("run_command"):
        bullets.append(
            "- run_command 优先窄范围执行（跑单个测试文件、加 tail/--quiet 等参数）；\n"
            "  超长输出会被截断，需要更多信息时收窄命令而不是重跑。")
    if on("web_fetch"):
        bullets.append("- web_fetch 只取正文文本，单次结果可能较长：优先抓权威文档页，"
                       "不要连续抓取大量页面。")
    resources = ("## 资源约定（上下文成本控制，必须遵守）\n\n"
                 "对话历史每一轮都会整体重发，冗长的工具结果会按轮次重复计费：\n\n"
                 + "\n".join(bullets) + "\n") if bullets else ""

    return (OUTPUT_CONTRACT_HEADER + work_line + _FINISH_PROTOCOL
            + ("\n" + resources if resources else ""))


def _read_text(path: Path, max_chars: int) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _skills_section(skills: list[Skill] | None,
                    enabled: frozenset[str] | None = None) -> str:
    """技能索引节：只列名称+描述，全文由 Agent 按需用 read_skill 读取。

    read_skill 被禁用时返回空——索引发下去模型也读不到全文，纯占上下文。
    """
    if not skills:
        return ""
    if enabled is not None and "read_skill" not in enabled:
        return ""
    lines = "\n".join(f"- {s.name}：{s.description or '（无描述）'}" for s in skills)
    return (
        f"# Skills 技能库\n\n"
        f"以下技能适用于当前项目。任务内容与某技能相关时，先调用 read_skill 工具读取其全文，"
        f"再严格按技能内容执行；与任务无关的技能不要读取。\n\n"
        f"{lines}\n\n"
    )


def _permissions_section(mode: str | None) -> str:
    """人工审批提示节：模式非 full_access 时告知模型操作可能被拒/超时。"""
    if not mode or mode == "full_access":
        return ""
    from .approvals import MODE_LABELS
    if mode == "ask_on_change":
        scope = "所有文件变更与命令执行（write_file / edit_file / multi_edit / delete_file / move_file / run_command）"
    else:
        scope = "高风险操作（delete_file / move_file / run_command）"
    return (
        f"## 人工审批\n\n"
        f"本项目启用了「{MODE_LABELS.get(mode, mode)}」访问权限：{scope}"
        f"会挂起等待用户在看板批准，可能被拒绝，也可能因超时被自动拒绝。\n"
        f"被拒绝后不要原样重试同一操作；调整方案继续，确实无法推进时调用 "
        f"finish（action=blocked）说明受阻原因。\n"
    )


def build_system_prompt(
    project: ProjectConfig,
    guide_content: str,
    is_git_repo: bool,
    guide_max_chars: int = 24000,
    skills: list[Skill] | None = None,
    tools: frozenset[str] | None = None,
    permission_mode: str | None = None,
) -> str:
    """组装系统提示词：角色 → 项目指引 → 技能索引 → 工作环境 → 输出协议。

    tools：启用的工具名集合（含 finish）；None = 全启用，文案与旧版一致。
    permission_mode：访问权限模式；非 full_access 时追加人工审批提示节。
    """
    guide = guide_content.strip()
    if len(guide) > guide_max_chars:
        log_safe = guide[:guide_max_chars]
        guide = log_safe + f"\n\n（指引过长，已截断至前 {guide_max_chars} 字符）"

    if is_git_repo:
        workspace = (
            f"## 工作环境\n\n"
            f"你在一个独立的 git worktree 中工作（项目主工作区：{project.path}，"
            f"对你是只读参考，不要修改）。\n"
            f"你的所有工具操作的相对路径都以当前 worktree 为根，不要触碰其他路径。\n"
        )
    else:
        workspace = (
            f"## 工作环境\n\n"
            f"项目根目录：{project.path}\n"
            f"你的所有工具操作的相对路径都以该目录为根，不要触碰其他路径。\n"
            f"该项目不是 git 仓库，不需要考虑版本控制。\n"
        )

    return (
        f"# 角色\n\n"
        f"你是「{project.name}」项目的开发 Agent，负责按任务清单完成功能开发与 bug 修复。\n\n"
        f"# Development Guide (AGENT.md)\n\n"
        f"以下是该项目的开发指引，是你在本项目工作的最高行为准则，必须遵守：\n\n"
        f"<agent_guide>\n{guide}\n</agent_guide>\n\n"
        f"{_skills_section(skills, tools)}"
        f"{workspace}\n"
        f"{_permissions_section(permission_mode)}\n"
        f"{_output_contract(tools)}"
    )
