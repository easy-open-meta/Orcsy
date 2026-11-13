"""看板设置保存服务（从 orcsy/web.AppState.save_settings 逐字拆分，阶段 3.5）。

职责：全局配置的分块保存与校验——llm/scheduler/web/logging/update/tools/
notify/masking/commands/env/mirrors/permissions/review/subagent/local_debug/git/
agent_profiles 各节的校验规则、api_key 三态语义、模型上限钳制与互斥复检，
以及保存后的 reload_config → _apply_config → _save_config 热重载编排。

绞杀者第一刀：函数体逐字搬移（`self.` → `state.`），显式接收 AppState；
后续再评估收窄为「配置变更事务」式窄接口。校验错误顺序与消息文本与
拆分前逐字一致。日志沿用 `orcsy.web` 流。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import masking, notify
from .auth import hash_password, is_password_hashed, verify_password
from .concurrency import configure_limiter
from .config import (
    LOCAL_DEBUG_BROWSERS,
    CommandsConfig,
    EnvConfig,
    GitConfig,
    LLMConfig,
    LocalDebugConfig,
    MaskingConfig,
    MirrorsConfig,
    NotifyConfig,
    PermissionsConfig,
    ReviewConfig,
    SchedulerConfig,
    SubagentConfig,
    ToolsConfig,
    UpdateConfig,
    WebConfig,
    assert_review_model_distinct,
    resolve_llm,
    resolve_review_llm,
    review_enabled_for,
    validate_agent_profiles,
    validate_allowed_hosts,
    validate_commands_map,
    validate_env_vars,
    validate_masking_patterns,
    validate_mirrors,
    validate_permission_mode,
    validate_tools_map,
    validate_web_security,
)
from .config import (
    _coerce_llm as _coerce_llm_cfg,
)
from .logging_setup import get_logger

if TYPE_CHECKING:
    from .web import AppState

log = get_logger("web")


def save_settings(state: AppState, data: dict) -> dict:
    sched = data.get("scheduler") or {}
    web = data.get("web") or {}

    # 分块保存：只带 scheduler/web/logging 的请求不校验/覆盖 llm
    llm = data.get("llm") or {}
    save_llm = "llm" in data
    # 模型上限钳制提示（save_llm 分支填充；分块保存时保持为空）
    clamped: list[str] = []
    provider = str(llm.get("provider", state.config.llm.provider)).strip().lower()
    if save_llm and provider not in ("openai", "anthropic"):
        raise ValueError(f"provider 不合法：{provider}（openai / anthropic）")
    model = str(llm.get("model", "")).strip()
    if save_llm and not model:
        raise ValueError("model 不能为空")
    base_url = str(llm.get("base_url", "")).strip()
    if save_llm and not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url 必须以 http(s):// 开头")
    interval = int(sched.get("interval_seconds") or state.config.scheduler.interval_seconds)
    if not (30 <= interval <= 86400):
        raise ValueError("扫描间隔应为 30~86400 秒")
    parallel = int(sched.get("max_parallel_agents") or state.config.scheduler.max_parallel_agents)
    if not (1 <= parallel <= 16):
        raise ValueError("并行 Agent 数应为 1~16")
    port = int(web.get("port") or state.config.web.port)
    if not (1 <= port <= 65535):
        raise ValueError("端口应为 1~65535")

    max_tokens = int(llm.get("max_tokens") or state.config.llm.max_tokens or 8192)
    max_context = int(llm.get("max_context_tokens")
                      or state.config.llm.max_context_tokens or 131072)
    if save_llm:
        # 输出/上下文上限优先跟随模型上报规格（看板拉取模型列表时缓存在
        # _model_meta）：超出自动钳到模型值——模型服务端的限制才是真上限，
        # 写死的 20 万/200 万只是没拉过模型列表时的兜底
        spec = state._model_meta.get(model) or {}
        out_cap = spec.get("max_output_tokens")
        ctx_cap = spec.get("context_window")
        if isinstance(out_cap, int) and out_cap > 0 and max_tokens > out_cap:
            max_tokens = out_cap
            clamped.append(f"max_tokens 已按模型上限调整为 {out_cap}")
        if isinstance(ctx_cap, int) and ctx_cap > 0 and max_context > ctx_cap:
            max_context = ctx_cap
            clamped.append(f"上下文窗口已按模型上限调整为 {ctx_cap}")
        tokens_cap = out_cap if isinstance(out_cap, int) and out_cap > 0 else 200000
        if not (256 <= max_tokens <= tokens_cap):
            raise ValueError(f"最大输出 max_tokens 应为 256~{tokens_cap}")
        ctx_cap_eff = ctx_cap if isinstance(ctx_cap, int) and ctx_cap > 0 else 2_000_000
        if not (4096 <= max_context <= ctx_cap_eff):
            raise ValueError(f"上下文窗口 max_context_tokens 应为 4096~{ctx_cap_eff}")
        # 超时 ≤0（如 -1）归一为 -1 = 不超时（挂起靠「强制停止」打断）；
        # 不能用 `or` 兜底——0 是显式意图该归一，而不是被当成「未填」回退 300
        raw_ts = llm.get("timeout_seconds")
        ts = 300 if raw_ts is None else int(raw_ts)
        if ts <= 0:
            ts = -1
        # api_key 三态（H2 脱敏的配套）：键缺失=保持原值（看板不再回显明文，
        # 未输入新密钥时前端不带该键——若按旧的「缺失=清空」语义，改任意
        # 设置保存一次就会把已配好的密钥抹掉）；空字符串=清除；非空=换新密钥
        api_key = state.config.llm.api_key
        if "api_key" in llm:
            api_key = str(llm.get("api_key") or "").strip()
        state.config.llm = LLMConfig(
            provider=provider,
            api_key=api_key,
            base_url=base_url, model=model,
            temperature=float(llm.get("temperature", 0.2)),
            max_tokens=max_tokens,
            max_context_tokens=max_context,
            enable_thinking=bool(llm.get("enable_thinking",
                                         state.config.llm.enable_thinking)),
            timeout_seconds=ts,
        )
    state.config.scheduler = SchedulerConfig(
        interval_seconds=interval, max_parallel_agents=parallel,
        run_once=state.config.scheduler.run_once,
    )
    # host/port 变更需重启生效，其余立即生效
    # auth_password 三态：键缺失=保持原值；空字符串=清除（关闭鉴权）；非空=设置新密码（密文存储）
    auth_password = state.config.web.auth_password
    if "auth_password" in web:
        new_pwd = str(web.get("auth_password") or "").strip()
        if not new_pwd:
            if auth_password:
                auth_password = ""
                state._invalidate_sessions()
        elif not verify_password(new_pwd, auth_password):
            auth_password = hash_password(new_pwd)
            state._invalidate_sessions()  # 密码变更后旧会话全部作废
    elif auth_password and not is_password_hashed(auth_password):
        auth_password = hash_password(auth_password)

    # api_keys 三态：键缺失=保持原值；显式提供=按新值更新（列表或逗号分隔串）；留空=清空
    api_keys = list(state.config.web.api_keys)
    if "api_keys" in web:
        raw_keys = web.get("api_keys")
        if isinstance(raw_keys, list):
            api_keys = [str(k).strip() for k in raw_keys if str(k).strip()]
        elif isinstance(raw_keys, str):
            api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        elif raw_keys is None:
            api_keys = []

    cors_origins = list(state.config.web.cors_origins)
    if "cors_origins" in web:
        raw_cors = web.get("cors_origins")
        if isinstance(raw_cors, list):
            cors_origins = [str(o).strip() for o in raw_cors if str(o).strip()] or ["*"]
        elif isinstance(raw_cors, str):
            cors_origins = [o.strip() for o in raw_cors.split(",") if o.strip()] or ["*"]

    cfg_host = str(web.get("host") or state.config.web.host)
    # 非回环绑定必须带登录密码或 API Key（M1）：host 与密码都已定案后校验，
    # 不合规抛 ValueError → 400 透传看板，配置不落盘
    validate_web_security(cfg_host, auth_password, context="看板保存的 web 配置", api_keys=api_keys)
    state.config.web = WebConfig(
        host=cfg_host,
        port=port,
        open_browser=bool(web.get("open_browser", state.config.web.open_browser)),
        auth_password=auth_password,
        api_keys=api_keys,
        cors_origins=cors_origins,
    )
    # 日志保留天数：0 表示关闭自动归档与清理；体积上限同理（0 = 不限制）。
    # 都不能用 `or` 兜底——0 是显式关闭意图，不是「未填」
    log_cfg = data.get("logging") or {}
    if "logging" in data:
        rd = int(log_cfg.get("retention_days", state.config.logging.retention_days))
        if not (0 <= rd <= 365):
            raise ValueError("日志保留天数应为 0~365（0 表示关闭自动归档清理）")
        state.config.logging.retention_days = rd
        raw_mb = log_cfg.get("max_total_mb")
        mb = (state.config.logging.max_total_mb if raw_mb is None
              else int(raw_mb))
        if not (0 <= mb <= 100000):
            raise ValueError("日志总占用上限应为 0~100000 MB（0 表示不限制）")
        state.config.logging.max_total_mb = mb

    # 软件更新源：分块保存，只带 update 的请求不触碰其它配置
    if "update" in data:
        upd = data.get("update") or {}
        repo = str(upd.get("repo") or "").strip()
        if repo and not repo.startswith(("http://", "https://")):
            raise ValueError("更新源仓库 URL 必须以 http(s):// 开头")
        state.config.update = UpdateConfig(
            repo=repo,
            token=str(upd.get("token", state.config.update.token)),
            auto_check=bool(upd.get("auto_check", state.config.update.auto_check)),
        )

    # Agent 工具开关：分块保存，只带 tools 的请求生效；
    # 未知工具名/禁用 finish/非布尔值由 validate_tools_map 直接报错（400 透传看板）
    if "tools" in data:
        state.config.tools = ToolsConfig(
            enabled=validate_tools_map(data.get("tools"), context="tools"))

    # 任务结束提醒开关：分块保存，只带 notify 的请求不触碰其它配置
    if "notify" in data:
        not_cfg = data.get("notify") or {}
        state.config.notify = NotifyConfig(
            sound=bool(not_cfg.get("sound", state.config.notify.sound)),
            system=bool(not_cfg.get("system", state.config.notify.system)),
        )
        notify.configure(state.config.notify)

    # 数据脱敏：分块保存，只带 masking 的请求不触碰其它配置；自定义正则
    # 校验（条数/长度/可编译）400 透传看板
    if "masking" in data:
        m_cfg = data.get("masking") or {}
        state.config.masking = MaskingConfig(
            enabled=bool(m_cfg.get("enabled", state.config.masking.enabled)),
            enhanced_rules=bool(m_cfg.get("enhanced_rules",
                                          state.config.masking.enhanced_rules)),
            custom_patterns=validate_masking_patterns(
                m_cfg.get("custom_patterns",
                          state.config.masking.custom_patterns),
                context="masking.custom_patterns"),
        )
        masking.configure(state.config.masking)

    # 命令白名单开关：分块保存；键必须是默认白名单成员（400 透传看板）
    if "commands" in data:
        state.config.commands = CommandsConfig(
            enabled=validate_commands_map(data.get("commands"), context="commands"))

    # Agent 子进程环境变量（代理/国内镜像）：分块保存，键名与保留变量校验。
    # 兼容 {vars:{...}} 与平铺 {...} 两种前端/手写形态；path_autofix 键缺失
    # =保持原值（mirror_apply 等只带 vars 的保存不得静默重置开关），显式
    # 提供才更新——平铺形态键全部按环境变量处理，与 config.py 加载侧同规则
    if "env" in data:
        raw_env = data.get("env")
        if isinstance(raw_env, dict):
            env_map = raw_env.get("vars") if "vars" in raw_env else raw_env
        else:
            env_map = raw_env
        path_autofix = state.config.env.path_autofix
        if (isinstance(raw_env, dict) and "vars" in raw_env
                and "path_autofix" in raw_env):
            path_autofix = bool(raw_env.get("path_autofix"))
        state.config.env = EnvConfig(
            vars=validate_env_vars(env_map, context="env.vars"),
            path_autofix=path_autofix)

    # 镜像源管理：分块保存（整块替换语义）；类别/URL 校验 400 透传看板
    if "mirrors" in data:
        mirrors_clean = validate_mirrors(data.get("mirrors"), context="mirrors")
        state.config.mirrors = MirrorsConfig(
            custom=mirrors_clean["custom"], disabled=mirrors_clean["disabled"])

    # 访问权限：分块保存；模式枚举与超时范围校验（400 透传看板）
    if "permissions" in data:
        perm = data.get("permissions") or {}
        mode = validate_permission_mode(
            perm.get("mode", state.config.permissions.mode))
        # 不能用 `or` 兜底：0 是非法值该报错，而不是被当成「未填」回退现值
        raw_timeout = perm.get("timeout_minutes")
        if raw_timeout is None:
            timeout = state.config.permissions.timeout_minutes
        else:
            try:
                timeout = int(raw_timeout)
            except (TypeError, ValueError):
                raise ValueError("审批超时必须是整数（分钟）") from None
        if not (1 <= timeout <= 1440):
            raise ValueError("审批超时应为 1~1440 分钟")
        state.config.permissions = PermissionsConfig(mode=mode, timeout_minutes=timeout)

    # 任务审查（Review Agent）：分块保存；模型三态 api_key 同 llm 全局节；
    # 保存后全项目互斥复检（全局审查模型换了，可能与任何项目的任务模型撞名）。
    # 先校验后赋值：复检失败抛 400 时配置保持原值（防半写）
    if "review" in data:
        rev = data.get("review") or {}
        rev_llm_raw = rev.get("llm")
        if rev_llm_raw is not None and not isinstance(rev_llm_raw, dict):
            raise ValueError("review.llm 必须是键值对字典或 null（null=清除绑定）")
        rev_llm = None
        if isinstance(rev_llm_raw, dict) and rev_llm_raw:
            rev_data = dict(rev_llm_raw)
            # api_key 三态（H2 配套）：键缺失=保持原值；空串=清除；非空=换新
            if ("api_key" not in rev_data and state.config.review.llm):
                rev_data["api_key"] = state.config.review.llm.api_key
            rev_llm = _coerce_llm_cfg(rev_data, require_api_key=False,
                                      context="review.llm")
        rev_turns = int(rev.get("max_turns", state.config.review.max_turns))
        if not (rev_turns == -1 or 1 <= rev_turns <= 100):
            raise ValueError(f"review.max_turns 应为 -1（不设上限）或 1~100（当前：{rev_turns}）")
        rev_rounds = int(rev.get("max_review_rounds",
                                 state.config.review.max_review_rounds))
        if not (rev_rounds == -1 or 1 <= rev_rounds <= 10):
            raise ValueError(f"review.max_review_rounds 应为 -1（不设上限）或 1~10（当前：{rev_rounds}）")
        new_review = ReviewConfig(
            enabled=bool(rev.get("enabled", state.config.review.enabled)),
            llm=rev_llm,
            max_turns=rev_turns,
            max_review_rounds=rev_rounds,
            allow_test_commands=bool(rev.get(
                "allow_test_commands", state.config.review.allow_test_commands)),
            squash_merge=bool(rev.get("squash_merge", state.config.review.squash_merge)),
        )
        # 全项目互斥复检：审查模型与任一启用审查项目的生效任务模型撞名即拒绝
        for p in state.config.projects:
            eff_review = resolve_review_llm(new_review, p.review_llm,
                                            context=f"项目 {p.name} 的 review_llm")
            if eff_review is not None and review_enabled_for(new_review, p):
                assert_review_model_distinct(
                    resolve_llm(state.config.llm, p.llm,
                                context=f"项目 {p.name} 的 llm"),
                    eff_review, context=f"项目 {p.name} 的 review")
        state.config.review = new_review  # 复检全过才落盘

    # 子代理并发预算：分块保存，只带 subagent 的请求生效；
    # 改完立刻同步进程级配额池（无需重启/等下一轮调度）
    if "subagent" in data:
        sub_raw = data.get("subagent") or {}
        try:
            sub_max = int(sub_raw.get("max_parallel",
                                      state.config.subagent.max_parallel))
        except (TypeError, ValueError):
            raise ValueError("子代理并发上限必须是整数") from None
        if not (1 <= sub_max <= 16):
            raise ValueError("子代理并发上限应为 1~16")
        state.config.subagent = SubagentConfig(
            max_parallel=sub_max,
            adaptive=bool(sub_raw.get("adaptive",
                                      state.config.subagent.adaptive)),
        )
        configure_limiter(state.config.subagent.max_parallel,
                          state.config.subagent.adaptive)

    # 本地调试配置：分块保存，只带 local_debug 的请求生效
    if "local_debug" in data:
        dbg_raw = data.get("local_debug") or {}
        dbg_browser = str(dbg_raw.get("browser", state.config.local_debug.browser)).strip().lower()
        if dbg_browser not in LOCAL_DEBUG_BROWSERS:
            raise ValueError(f"local_debug.browser 只能是 {' / '.join(LOCAL_DEBUG_BROWSERS)}（当前：{dbg_browser}）")
        dbg_nav = int(dbg_raw.get("navigation_timeout_seconds", state.config.local_debug.navigation_timeout_seconds))
        if not (5 <= dbg_nav <= 300):
            raise ValueError("页面导航超时应为 5~300 秒")
        dbg_srv = int(dbg_raw.get("service_ready_timeout_seconds", state.config.local_debug.service_ready_timeout_seconds))
        if not (5 <= dbg_srv <= 600):
            raise ValueError("服务就绪超时应为 5~600 秒")
        dbg_max = int(dbg_raw.get("max_services", state.config.local_debug.max_services))
        if not (1 <= dbg_max <= 10):
            raise ValueError("单任务最大后台服务数应为 1~10")
        dbg_ret = int(dbg_raw.get("artifacts_retention_days", state.config.local_debug.artifacts_retention_days))
        if not (0 <= dbg_ret <= 365):
            raise ValueError("工件保留天数应为 0~365 天")

        hosts_raw = dbg_raw.get("allowed_hosts")
        if hosts_raw is not None:
            dbg_hosts = validate_allowed_hosts(hosts_raw, context="local_debug.allowed_hosts")
        else:
            dbg_hosts = state.config.local_debug.allowed_hosts

        state.config.local_debug = LocalDebugConfig(
            browser=dbg_browser,
            browser_executable=str(dbg_raw.get("browser_executable", state.config.local_debug.browser_executable) or "").strip(),
            headless=bool(dbg_raw.get("headless", state.config.local_debug.headless)),
            allowed_hosts=dbg_hosts,
            navigation_timeout_seconds=dbg_nav,
            service_ready_timeout_seconds=dbg_srv,
            max_services=dbg_max,
            artifacts_retention_days=dbg_ret,
        )

    # Git 自动化工作流配置（任务完成后的提交与合并开关）
    if "git" in data:
        git_raw = data.get("git") or {}
        state.config.git = GitConfig(
            commit_and_merge=bool(git_raw.get("commit_and_merge", state.config.git.commit_and_merge)),
        )

    # 全局 Agent 角色预设池（分块保存；llm.api_key 三态脱敏保护）
    if "agent_profiles" in data:
        profs_in = data.get("agent_profiles") or {}
        if not isinstance(profs_in, dict):
            raise ValueError("agent_profiles 必须是字典")
        cleaned_profs: dict = {}
        for pid, item in profs_in.items():
            if not isinstance(item, dict):
                raise ValueError(f"agent_profiles.{pid} 必须是字典")
            p_item = dict(item)
            old_prof = state.config.agent_profiles.get(pid)
            m_llm = p_item.get("llm")
            if isinstance(m_llm, dict) and old_prof and old_prof.llm and "api_key" not in m_llm:
                m_llm = dict(m_llm)
                m_llm["api_key"] = old_prof.llm.get("api_key", "")
                p_item["llm"] = m_llm
            cleaned_profs[pid] = p_item
        new_profs = validate_agent_profiles(cleaned_profs, state.config.llm, context="agent_profiles")
        # 校验删除安全性：若已有启用的小组成员引用该预设，拒绝删除以防配置悬空
        for p in state.config.projects:
            if p.team:
                for m in p.team.members:
                    if m.profile and m.profile not in new_profs:
                        raise ValueError(
                            f"预设 '{m.profile}' 正被项目 '{p.name}' 的小组成员 '{m.id}' 引用，"
                            f"请先在项目设置中解绑该预设后再删除。"
                        )
        state.config.agent_profiles = new_profs

    state.scheduler.reload_config(state.config)
    state._apply_config(state.config)
    state._save_config(state.config)

    # 新保留天数立即生效：顺手做一次归档/清理
    if "logging" in data:
        from .logging_setup import rotate_and_cleanup

        log_path = (Path(state.log_path) if state.log_path
                    else Path(state.config.logging.file))
        try:
            msg = rotate_and_cleanup(log_path, rd,
                                     state.config.logging.max_total_mb)
            if msg:
                log.info("日志维护：%s", msg)
        except OSError as e:
            log.warning("日志归档/清理失败：%s", e)

    log.info("Web 保存配置：model=%s interval=%ss", state.config.llm.model, interval)
    note = f"（{'；'.join(clamped)}）" if clamped else ""
    return {"ok": True,
            "message": f"配置已保存并生效（看板 host/port 变更下次启动生效）{note}"}


__all__ = [
    "save_settings",
]
