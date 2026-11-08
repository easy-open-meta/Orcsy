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
from .tools import _path_key

log = get_logger("prompts")

OUTPUT_CONTRACT_HEADER = """\
## 工作方式与结束协议（必须严格遵守）

"""

_FINISH_PROTOCOL = """\

结束任务时**必须调用 finish 工具**，参数字段：

- action："completed"（已完成且按指引验证通过）| "blocked"（尝试后受阻，notes 写清原因）
  | "cannot_do"（超出能力或需人工决策，notes 说明原因）| "split"（任务过大，已用
  create_tasks 拆分为子任务并交接，summary 写拆成了哪几块）
- summary：一句话说明做了什么或为何受阻
- files_changed：相对项目根的变更文件路径数组；未改动则空数组
- commit_message：Conventional Commits 风格提交信息（如 fix: ... / feat: ...）；
  action 非 completed 时留空
- notes：补充说明、未尽事项；无则留空

不要用纯文本回复结论；不要自己执行 git commit / git push（系统统一处理）。
"""


def _output_contract(enabled: frozenset[str] | None = None) -> str:
    """按启用工具集动态生成工作方式与资源约定。

    enabled=None（全启用）时文案覆盖全部 14 个工具；被禁用的工具不出现在
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
    if on("speedtest_mirrors"):
        clauses.append("speedtest_mirrors 测镜像源延迟选最快的下载")
    if on("create_tasks"):
        clauses.append("create_tasks 在任务过大时自主拆分子任务")
    if on("dev_server_start"):
        clauses.append("dev_server_* 管理后台开发服务")
    if on("http_request"):
        clauses.append("http_request 调试本地或内网 API")
    if on("browser_navigate"):
        clauses.append("browser_* 操作本地浏览器调试 Web 页面")
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
    if on("speedtest_mirrors"):
        bullets.append("- speedtest_mirrors 结果会持久化：技能正文的「镜像源测速优选」段"
                       "已有较新数据时直接用，不要重复测速。")
    if on("create_tasks"):
        bullets.append(
            "- 任务体量远超单轮预算时**主动拆分**，不要硬撑到轮数耗尽：执行早期"
            "（读完需求、动手前）就判断——预计 15+ 轮、要新写大量文件、或验证清单"
            "明显做不完，即用 create_tasks 拆成可独立验证的子任务（每个带明确验收"
            "命令），再 finish(action=split)。拆分前把已验证的现状写进子任务描述，"
            "避免后续任务重做或误判已有文件损坏。")
    if on("dev_server_start") or on("browser_navigate") or on("http_request"):
        bullets.append(
            "- 本地调试流程：先用 dev_server_start 启动被测服务（命令中使用 {PORT} 自动分配端口并等待就绪），"
            "再用 browser_navigate 访问页面或 http_request 调接口验证；操作页面前先用 browser_snapshot "
            "获取元素 ref，发生异常及时查看 browser_console / dev_server_logs；任务交付前调用 dev_server_stop 释放服务。")
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


def _tools_section(tool_map: dict | None) -> str:
    """本机工具节：只列不在系统默认 PATH、靠启动扫描注入会话的命令。

    本来就在 PATH 里的命令不列（模型直接用即可，列了纯占上下文）；
    空地图整节约省略。
    """
    if not tool_map:
        return ""
    extra_keys = {_path_key(d) for d in (tool_map.get("extra_dirs") or [])}
    injected = []
    for name, abs_path in sorted((tool_map.get("tools") or {}).items()):
        if _path_key(str(Path(abs_path).parent)) in extra_keys:
            injected.append(f"- {name} → {abs_path}")
    if not injected:
        return ""
    lines = "\n".join(injected)
    return (
        f"## 本机工具\n\n"
        f"以下命令已安装但不在系统默认 PATH，其目录已自动注入你的会话 PATH，"
        f"用裸名直接调用即可（不要自己拼绝对路径）：\n\n"
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
    tool_map: dict | None = None,
) -> str:
    """组装系统提示词：角色 → 项目指引 → 技能索引 → 工作环境 → 输出协议。

    tools：启用的工具名集合（含 finish）；None = 全启用，文案与旧版一致。
    permission_mode：访问权限模式；非 full_access 时追加人工审批提示节。
    tool_map：任务启动扫描的本机工具地图（tools + extra_dirs），非空时追加
    「本机工具」节——模型无需浪费轮数自己探测命令在哪。
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
        f"{_tools_section(tool_map)}"
        f"{_permissions_section(permission_mode)}\n"
        f"{_output_contract(tools)}"
    )


# ---- 审查 Agent（Review Agent）提示词：独立模型、只读核查、通过/不通过 ----

_REVIEW_FINISH_PROTOCOL = """\

结束审查时**必须调用 finish 工具**给出结论：

- action："completed"（审查通过）| "blocked"（审查不通过，发现问题）
- summary：一句话审查结论
- issues（action=blocked 时提供）：结构化问题数组，每条：
  {"file": "相对根的文件路径", "line": 行号（可省）, "severity": "blocker|major|minor", "message": "问题与建议"}
  severity 含义：blocker=不满足需求或阻断验收；major=功能缺陷/明显质量问题；minor=次要问题
- notes：补充说明（可留空）；不要用纯文本回复结论，不要自己执行 git 操作
"""


def build_review_system_prompt(project: ProjectConfig, work_root: Path,
                               guide_content: str,
                               allow_test_commands: bool = True,
                               skill_bodies: dict[str, str] | None = None,
                               guide_max_chars: int = 12000) -> str:
    """组装审查 agent 系统提示词：角色 → 指引（精简）→ 工作环境 → 结论协议。

    与任务 agent 的系统提示词刻意不同源：审查角色、只读约束、issues 上报
    协议、无技能索引节（技能按需由 read_skill 读取，正文已预载）。
    """
    guide = (guide_content or "").strip()
    if len(guide) > guide_max_chars:
        guide = guide[:guide_max_chars] + f"\n\n（指引过长，已截断至前 {guide_max_chars} 字符）"

    tools_line = ("read_file / grep / list_dir / find_files 探查代码"
                  + ("；run_command 只跑测试与构建验证（不可改动任何文件）"
                     if allow_test_commands else ""))
    return (
        f"# 角色\n\n"
        f"你是「{project.name}」项目的**独立审查 Agent**，与完成任务的开发 Agent 不是同一个模型。"
        f"你的职责是在代码合并前进站核查：开发 Agent 声称完成的任务是否真的完成、"
        f"实现是否满足需求与验收标准、有无明显缺陷。你不信任任何自述，一切以代码为准。\n\n"
        f"# Development Guide (AGENT.md)（验收参照）\n\n"
        f"<agent_guide>\n{guide}\n</agent_guide>\n\n"
        f"# 工作环境与约束\n\n"
        f"- 审查根目录：{work_root}（你的所有工具操作的相对路径都以它为根）。\n"
        f"- **只读审查**：禁止修改任何文件、禁止执行任何 git 写操作"
        f"（commit/merge/push 由系统在审查通过后统一处理）。\n"
        f"- 你通过工具完成一切工作：{tools_line}。\n"
        f"- 上下文成本控制：大文件先 grep 定位，再用 read_file 的 offset/limit 读区段，"
        f"不要整读大文件，不要重复读取。\n\n"
        f"{_REVIEW_FINISH_PROTOCOL}"
    )


def build_review_prompt(task: Task, agent_summary: str, agent_notes: str,
                        diff_text: str, allow_test_commands: bool = True,
                        diff_max_chars: int = 80000) -> str:
    """组装审查任务提示词：任务原文 + Agent 自述 + 改动 diff + 审查要求。"""
    diff = (diff_text or "").strip()
    if len(diff) > diff_max_chars:
        diff = diff[:diff_max_chars] + "\n\n（diff 过长已截断；请用 read_file 自行查看可疑文件全文）"
    verify_line = (
        "- 若任务声称验证通过：**必须实际运行相应测试/构建命令**复核，不能只信自述。"
        if allow_test_commands else
        "- 你没有命令执行权限：重点做代码级核对（实现是否存在、逻辑是否对应需求）。")
    return (
        f"# 待审查任务\n\n"
        f"- 任务 ID：{task.id}\n"
        f"- 标题：{task.title}\n"
        f"- 优先级：{task.priority}\n\n"
        f"## 任务内容（需求与验收标准）\n\n{task.description}\n\n"
        f"## 开发 Agent 的自述\n\n"
        f"- 摘要：{agent_summary or '（无）'}\n"
        f"- 备注：{agent_notes or '（无）'}\n\n"
        + (f"## 本次改动（git diff，相对主分支的累计改动）\n\n```diff\n{diff}\n```\n\n"
           if diff else "## 本次改动\n\n（无 diff：非 git 项目，请直接探查项目目录中的实现）\n\n")
        + f"## 审查要求\n\n"
        f"1. 对照任务内容逐条核对：需求是否都实现？验收标准是否满足？\n"
        f"2. 用 read_file/grep 抽查关键实现的真实性——防止「声称完成实则未做/只做了一半」。\n"
        f"3. {verify_line}\n"
        f"4. 只报实质问题（不满足需求、功能缺陷、明显质量问题）；代码风格与个人偏好不算问题。\n"
        f"5. 全部通过则 finish(action=completed)；发现任何实质问题则 finish(action=blocked) "
        f"并用 issues 逐条列出（宁缺勿滥：不通过的代价是整个任务返工）。\n"
    )
