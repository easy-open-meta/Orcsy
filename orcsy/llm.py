"""大模型接入与 Agent 工具循环。

两种协议均原生支持工具调用（function calling）：
- openai：OpenAI Chat Completions + tools（覆盖 DeepSeek/GLM/Kimi/通义/Ollama/vLLM 等）
- anthropic：Anthropic Messages API + tools

Agent 循环（run_task_loop）：
  任务提示 → 模型 → （工具调用 → 执行 → 结果回传）* N → 模型调用 finish 工具 → 结构化结论

模型的思考内容（OpenAI 兼容的 reasoning_content / Anthropic 的 thinking 块）在
模型返回时原样写入运行日志（💭 前缀条目），看板里可展开查看；不回传给服务端。

安全：文件工具被 ToolContext 禁锢在项目根内；命令工具有白名单；循环有轮数上限。
"""

from __future__ import annotations

import json
import time

import httpx

from . import USER_AGENT
from .config import LLMConfig
from .logging_setup import get_logger
from .tools import (
    FinishSignal,
    ToolContext,
    dispatch_tool,
    tool_definitions_anthropic,
    tool_definitions_openai,
)

log = get_logger("llm")

DEFAULT_MAX_TURNS = 40

# 知名模型的（上下文窗口, 最大输出）兜底规格：OpenAI 兼容服务的 /models
# 列表通常不带窗口元数据，看板选模型时按 id 子串命中补全；顺序即优先级
# （先具体后泛化），数值取官方公开值，用户可在设置页手改。
MODEL_SPECS: list[tuple[str, int, int]] = [
    ("deepseek-reasoner", 65536, 65536),
    ("deepseek-chat", 65536, 8192),
    ("deepseek", 65536, 8192),
    ("kimi-k2", 131072, 8192),
    ("moonshot-v1-128k", 131072, 8192),
    ("moonshot-v1-32k", 32768, 8192),
    ("moonshot-v1-8k", 8192, 4096),
    ("moonshot", 131072, 8192),
    ("glm-4.6", 200000, 128000),
    ("glm-4.5", 131072, 96768),
    ("glm-4", 131072, 4096),
    ("glm", 131072, 8192),
    ("qwen3", 131072, 16384),
    ("qwen", 131072, 8192),
    ("gpt-4.1", 1048576, 32768),
    ("gpt-4o", 128000, 16384),
    ("gpt-4", 8192, 4096),
    ("gpt", 128000, 16384),
    ("gemini", 1048576, 65536),
    ("claude", 200000, 64000),
]


def model_spec(model_id: str) -> tuple[int, int] | None:
    """按 id 子串查内置规格表；未命中返回 None（不瞎猜）。"""
    mid = (model_id or "").lower()
    for key, ctx, out in MODEL_SPECS:
        if key in mid:
            return ctx, out
    return None


def _auth_headers(provider: str, api_key: str) -> dict[str, str]:
    """构造认证头；api_key 为空（Ollama/vLLM 等本地服务）时不带认证头，
    避免空 Bearer 值触发 httpx Illegal header value。"""
    if provider == "anthropic":
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        return headers
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _anthropic_base(base_url: str) -> str:
    """归一化 anthropic 兼容地址：自动补 /v1。

    官方（https://api.anthropic.com）与多数兼容服务（如 MiMo 的
    https://token-plan-cn.xiaomimimo.com/anthropic）公布的根地址都不含 /v1，
    而对话端点在其下 /v1/messages——用户照抄官方地址即可，这里统一补齐；
    已以 /v1 结尾的地址不重复补（兼容既有配置与自带 /v1 的代理服务）。
    """
    base = (base_url or "").rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def test_connection(provider: str, base_url: str, api_key: str, model: str,
                    timeout_seconds: int = 300) -> dict:
    """最小对话请求验证大模型接入连通性（看板「测试连接」按钮）。

    只发一条极短 user 消息、max_tokens=64、不带工具与思考参数，把失败面与
    费用压到最小：HTTP 200 即证明 网络可达 + 认证有效 + 模型名存在，回复正文
    仅作展示——推理模型（DeepSeek-R1 等）思考可能耗尽小额 max_tokens 导致
    正文为空，同样算连通。返回 {reply, elapsed_seconds}；失败抛 LLMError。
    """
    provider = provider.strip().lower()
    model = model.strip()
    if provider not in ("openai", "anthropic"):
        raise LLMError(f"provider 不合法：{provider}（openai / anthropic）")
    if not model:
        raise LLMError("model 不能为空")
    client = LLMClient(LLMConfig(
        provider=provider, api_key=api_key, base_url=base_url, model=model,
        timeout_seconds=max(5, min(int(timeout_seconds), 120)),
    ))
    if provider == "anthropic":
        url = _anthropic_base(base_url) + "/messages"
        headers = _auth_headers("anthropic", api_key)
    else:
        url = base_url.rstrip("/") + "/chat/completions"
        headers = _auth_headers("openai", api_key)
    payload = {
        "model": model, "max_tokens": 64,
        "messages": [{"role": "user", "content": "连接测试，请回复：OK"}],
    }
    started = time.monotonic()
    data = client._post(url, payload, headers)
    elapsed = round(time.monotonic() - started, 1)
    if provider == "anthropic":
        reply = "".join(b.get("text", "") for b in data.get("content") or []
                        if isinstance(b, dict) and b.get("type") == "text").strip()
    else:
        try:
            reply = str(data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(
                f"响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from e
    return {"reply": reply[:200], "elapsed_seconds": elapsed}


def list_models(provider: str, base_url: str, api_key: str,
                timeout_seconds: int = 30) -> list[dict]:
    """拉取大模型服务的模型列表，尽量带上每个模型的上下文/输出上限。

    openai 兼容与 anthropic 都是 GET {base_url}/models；anthropic 响应自带
    context_window / max_output_tokens，openai 兼容服务通常没有（置 None，
    由看板用 model_spec 补全）。
    """
    if provider == "anthropic":
        url = _anthropic_base(base_url) + "/models"
    else:
        url = base_url.rstrip("/") + "/models"
    headers = {"User-Agent": USER_AGENT, **_auth_headers(provider, api_key)}
    try:
        resp = httpx.get(url, headers=headers, timeout=min(max(timeout_seconds, 5), 30))
    except httpx.HTTPError as e:
        raise LLMError(f"获取模型列表失败：{e}") from e
    if resp.status_code == 404:
        # 404 ≠ 密钥/网络问题，是该 base_url 下没有 /models 端点——按协议给
        # 可行动的提示（如 MiMo 等 anthropic 兼容面只有 /messages，无列表端点）
        if provider == "anthropic":
            raise LLMError(
                "获取模型列表失败：HTTP 404 —— 该服务的 anthropic 兼容面通常不提供"
                "模型列表端点（仅 /messages 对话）。可直接手填模型名，"
                "或改用其 openai 兼容地址拉取列表。")
        raise LLMError(
            "获取模型列表失败：HTTP 404 —— 该地址下没有 /models 端点，"
            "请检查 base_url（OpenAI 兼容地址通常以 /v1 结尾）。")
    if resp.status_code != 200:
        raise LLMError(f"获取模型列表失败：HTTP {resp.status_code} {resp.text[:200]}")
    try:
        data = resp.json().get("data") or []
    except ValueError as e:
        raise LLMError("获取模型列表失败：响应不是 JSON") from e
    models = []
    for m in data:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        models.append({
            "id": mid,
            "context_window": m.get("context_window"),
            "max_output_tokens": m.get("max_output_tokens"),
        })
    models.sort(key=lambda x: x["id"])
    return models


# 早期工具结果被裁剪后的占位文本
_TRIM_PLACEHOLDER = "（早期工具结果已省略）"
# 滚动压缩参数：最近 N 条消息保持原文；其前的长工具结果占位化
_KEEP_RECENT_MSGS = 12
_TOOL_KEEP_CHARS = 600
# 历史估算 token 软预算：超过即压缩旧工具结果（与模型窗口无关的常驻成本控制）
_SOFT_BUDGET_TOKENS = 24000


def _est_tokens_msg(m: dict) -> int:
    """粗估一条消息的 token 数（len//2，中文偏保守；用于压缩触发判断）。"""
    try:
        return max(1, len(json.dumps(m, ensure_ascii=False)) // 2)
    except (TypeError, ValueError):
        return 1


# 思考内容写入运行日志的单轮上限：思考动辄数千字，不截断会把日志尾
# （看板只读文件尾部）刷满，反而看不见执行主线
_THINKING_LOG_CHARS = 6000


def _clip_thinking(text: str) -> str:
    if len(text) <= _THINKING_LOG_CHARS:
        return text
    return text[:_THINKING_LOG_CHARS] + f"\n…（思考内容超过 {_THINKING_LOG_CHARS} 字，已截断）"


class LLMError(RuntimeError):
    pass


class AgentResponse:
    """Agent 对单个任务的结构化结论（来自 finish 工具）。"""

    def __init__(self, action: str, summary: str, files_changed: list[str],
                 commit_message: str, notes: str) -> None:
        self.action = action              # completed / blocked / cannot_do
        self.summary = summary
        self.files_changed = files_changed
        self.commit_message = commit_message
        self.notes = notes

    @classmethod
    def fallback(cls, reason: str) -> "AgentResponse":
        return cls(action="blocked", summary="", files_changed=[],
                   commit_message="", notes=f"模型未按协议给出结论：{reason}")


def parse_finish_args(args_json: str | dict) -> dict:
    """解析 finish 工具的参数；不是 JSON 对象时返回空 dict。"""
    try:
        payload = json.loads(args_json) if isinstance(args_json, str) else (args_json or {})
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def finish_response(payload: dict) -> AgentResponse:
    """把 finish 参数转成 AgentResponse，协议不完整时返回 blocked 兜底。"""
    action = str(payload.get("action", "")).strip().lower()
    summary = str(payload.get("summary", "")).strip()
    if action not in ("completed", "blocked", "cannot_do") or not summary:
        return AgentResponse.fallback(f"finish 参数不完整：action={action!r}")
    files = payload.get("files_changed") or []
    return AgentResponse(
        action=action,
        summary=summary,
        files_changed=[str(f) for f in files] if isinstance(files, list) else [],
        commit_message=str(payload.get("commit_message", "")).strip(),
        notes=str(payload.get("notes", "")).strip(),
    )


class LLMClient:
    """同一任务的对话客户端：维护完整消息历史（含工具调用轮次）。"""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.messages: list[dict] = []
        # 每轮响应的 usage 累计（两种协议字段不同，这里统一收敛）
        self.usage = {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0}
        # 启用的工具名集合（run_task_loop 从 ToolContext 取）；None = 全量下发
        self._enabled_tools: frozenset[str] | None = None

    def _acc_usage(self, data: dict, provider: str) -> None:
        u = data.get("usage") or {}
        if provider == "anthropic":
            pt = int(u.get("input_tokens") or 0)
            ct = int(u.get("output_tokens") or 0)
        else:
            pt = int(u.get("prompt_tokens") or 0)
            ct = int(u.get("completion_tokens") or 0)
        self.usage["turns"] += 1
        self.usage["prompt_tokens"] += pt
        self.usage["completion_tokens"] += ct

    def reset(self, system_prompt: str) -> None:
        self.messages = [{"role": "system", "content": system_prompt}]

    # ---- 消息体协议适配 ----

    @staticmethod
    def _user(content: str) -> dict:
        return {"role": "user", "content": content}

    @staticmethod
    def _assistant_tool_calls(call_id: str, name: str, args_json: str) -> dict:
        """把一次工具调用表示成 assistant 消息（两种协议统一成 OpenAI 形态存储）。"""
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": args_json},
            }],
        }

    @staticmethod
    def _tool_result(call_id: str, content: str) -> dict:
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    # ---- Agent 循环 ----

    def run_task_loop(self, task_prompt: str, ctx: ToolContext,
                      max_turns: int = DEFAULT_MAX_TURNS,
                      on_progress=None) -> AgentResponse:
        """执行一个任务的完整工具循环，返回结构化结论。

        on_progress(line) 在每次工具调用后触发（finish 除外），供执行器把
        进度落盘到任务文件——进程被强杀时进度不丢。
        """
        self.messages.append(self._user(task_prompt))
        # 工具开关：schema 按启用集过滤下发（dispatch_tool 里还有兜底拦截）
        self._enabled_tools = ctx.enabled_tools
        use_tools = True

        for turn in range(1, max_turns + 1):
            if self.cfg.provider == "anthropic":
                assistant_msg, tool_calls, reasoning = self._step_anthropic(use_tools)
            else:
                assistant_msg, tool_calls, reasoning = self._step_openai(use_tools)

            self.messages.append(assistant_msg)
            # 模型返回了思考内容（DeepSeek-R1/GLM 等的 reasoning_content，
            # Anthropic 的 thinking 块）时写入运行日志，看板可展开查看；
            # 思考内容不回传给服务（多数协议禁止），只本地留档
            if reasoning:
                log.info("💭 模型思考（第 %d 轮）：\n%s", turn, _clip_thinking(reasoning))

            if not tool_calls:
                # 模型直接输出文字而非调用工具：提醒其走 finish 协议
                said = (assistant_msg.get("content") or "").strip()[:200]
                log.debug("第 %d 轮模型未调用工具，输出：%s", turn, said)
                self.messages.append(self._user(
                    "请调用 finish 工具给出任务结论（action/summary 等字段）。"
                    "不要用纯文本回复。"
                ))
                continue

            for call in tool_calls:
                call_id, name, args_json = call
                log.info("工具调用 [%d] %s %s", turn, name,
                         (args_json or "")[:120].replace("\n", " "))
                if name == "finish":
                    raise FinishSignal(self._parse_finish_args(args_json))
                if on_progress is not None:
                    try:
                        on_progress(f"第{turn}轮 · {name} · "
                                    f"{(args_json or '')[:60].strip()}".replace("\n", " "))
                    except Exception:  # noqa: BLE001 进度回调失败不影响执行
                        pass
                result = dispatch_tool(ctx, name, args_json)
                log.debug("工具结果 [%d] %s：%s", turn, name, result[:200].replace("\n", " "))
                self.messages.append(self._tool_result(call_id, result))

            self._trim_context()

        # 轮数耗尽：要求强制收尾
        log.warning("工具循环达到最大轮数 %d，要求强制收尾", max_turns)
        if on_progress is not None:
            try:
                on_progress(f"达到最大轮数 {max_turns}，要求强制收尾")
            except Exception:  # noqa: BLE001
                pass
        self.messages.append(self._user(
            "已达到最大工作轮数。请立即调用 finish 工具：能完成的部分说明完成度，"
            "未完成的部分用 action=blocked 并在 notes 里说明剩余工作。"
        ))
        try:
            if self.cfg.provider == "anthropic":
                assistant_msg, tool_calls, reasoning = self._step_anthropic(True)
            else:
                assistant_msg, tool_calls, reasoning = self._step_openai(True)
            if reasoning:
                log.info("💭 模型思考（收尾轮）：\n%s", _clip_thinking(reasoning))
            self.messages.append(assistant_msg)
            for call_id, name, args_json in tool_calls:
                if name == "finish":
                    return self._finish_response(self._parse_finish_args(args_json))
        except LLMError:
            pass
        return AgentResponse.fallback(f"超过最大轮数 {max_turns} 仍未给出结论")

    @staticmethod
    def _parse_finish_args(args_json: str | dict) -> dict:
        return parse_finish_args(args_json)

    def _trim_context(self) -> None:
        """控制对话历史的常驻成本，避免 token 浪费（每轮工具结果落库后执行）。

        两级策略：
        1. 滚动压缩：历史估算超过软预算（默认 24K tokens，与模型窗口无关——
           历史每轮都要重发，小就是省）时，把「最近 N 条消息之外」的长工具
           结果占位化。这些信息模型已经消费过，保留原文纯属每轮重复计费。
        2. 紧急裁剪：估算逼近模型窗口 90% 时（长任务/小窗口），对剩余旧工具
           结果进一步占位化，防止 400 撞墙。
        system 与最近消息保持原文；tool_call/tool_result 配对不被破坏。
        """
        limit = int(getattr(self.cfg, "max_context_tokens", 0) or 0)
        if len(self.messages) <= _KEEP_RECENT_MSGS + 2:
            return
        total = sum(_est_tokens_msg(m) for m in self.messages)
        soft = min(_SOFT_BUDGET_TOKENS, int(limit * 0.9)) if limit > 0 else _SOFT_BUDGET_TOKENS
        if total > soft:
            target = int(soft * 0.7)
            for i in range(1, len(self.messages) - _KEEP_RECENT_MSGS):
                if total <= target:
                    break
                m = self.messages[i]
                c = m.get("content")
                if m.get("role") == "tool" and isinstance(c, str) \
                        and len(c) > _TOOL_KEEP_CHARS and not c.endswith("已省略）"):
                    total -= _est_tokens_msg(m) - 12
                    m["content"] = _TRIM_PLACEHOLDER
        if limit > 0 and total > int(limit * 0.9):
            for i in range(1, len(self.messages) - 6):
                m = self.messages[i]
                c = m.get("content")
                if m.get("role") == "tool" and isinstance(c, str) \
                        and len(c) > 100 and not c.endswith("已省略）"):
                    total -= _est_tokens_msg(m) - 12
                    m["content"] = _TRIM_PLACEHOLDER
            log.info("上下文逼近模型窗口（limit=%d tokens），已强制裁剪早期工具结果", limit)

    @staticmethod
    def _finish_response(payload: dict) -> AgentResponse:
        return finish_response(payload)

    # ---- OpenAI 兼容协议 ----

    def _payload_openai(self, use_tools: bool) -> dict:
        payload: dict = {
            "model": self.cfg.model,
            "messages": self.messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if self.cfg.enable_thinking:
            # 开关打开时才带上思考参数：Qwen/通义系认 enable_thinking，
            # GLM 系认 thinking 对象；严格校验未知字段的服务（如 OpenAI 官方）
            # 对不支持思考的模型请勿开此开关
            payload["enable_thinking"] = True
            payload["thinking"] = {"type": "enabled"}
        if use_tools:
            payload["tools"] = tool_definitions_openai(self._enabled_tools)
        return payload

    def _step_openai(self, use_tools: bool) -> tuple[dict, list[tuple[str, str, str]], str]:
        data = self._post(
            self.cfg.base_url.rstrip("/") + "/chat/completions",
            self._payload_openai(use_tools),
            _auth_headers("openai", self.cfg.api_key),
        )
        self._acc_usage(data, "openai")
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"OpenAI 响应结构异常：{json.dumps(data, ensure_ascii=False)[:500]}") from e
        content = msg.get("content") or ""
        # 推理模型的思考内容：DeepSeek/GLM 等走 reasoning_content，个别服务叫 reasoning
        reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            tool_calls.append((tc.get("id") or f"call_{len(tool_calls)}",
                               fn.get("name", ""), fn.get("arguments") or "{}"))
        if not content and not tool_calls and not reasoning:
            raise LLMError("大模型返回了空内容")
        return {"role": "assistant", "content": content or None}, tool_calls, reasoning

    # ---- Anthropic 协议 ----

    def _payload_anthropic(self, use_tools: bool) -> dict:
        system_parts = [m["content"] for m in self.messages if m["role"] == "system"]
        rest = [m for m in self.messages if m["role"] != "system"]
        # OpenAI 形态的历史转 Anthropic 形态
        converted: list[dict] = []
        for m in rest:
            if m["role"] == "user":
                converted.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    fn = tc["function"]
                    try:
                        args = json.loads(fn["arguments"])
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    blocks.append({"type": "tool_use", "id": tc["id"],
                                   "name": fn["name"], "input": args})
                if blocks:
                    converted.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                converted.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": m["tool_call_id"],
                                 "content": m["content"]}],
                })
        payload: dict = {
            "model": self.cfg.model,
            "system": "\n\n".join(system_parts),
            "messages": converted,
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
        }
        if self.cfg.enable_thinking:
            # extended thinking 必须显式开启，模型才会返回 thinking 块；
            # 官方约束：budget_tokens ≥ 1024 且 < max_tokens，temperature 固定 1
            # （直接省略 temperature，API 默认即为 1）
            if self.cfg.max_tokens < 2048:
                raise LLMError("开启模型思考需要 max_tokens ≥ 2048（思考预留至少 1024）")
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": max(1024, self.cfg.max_tokens // 2),
            }
            del payload["temperature"]
        if use_tools:
            payload["tools"] = tool_definitions_anthropic(self._enabled_tools)
        return payload

    def _step_anthropic(self, use_tools: bool) -> tuple[dict, list[tuple[str, str, str]], str]:
        data = self._post(
            _anthropic_base(self.cfg.base_url) + "/messages",
            self._payload_anthropic(use_tools),
            _auth_headers("anthropic", self.cfg.api_key),
        )
        self._acc_usage(data, "anthropic")
        try:
            blocks = data["content"]
        except (KeyError, TypeError) as e:
            raise LLMError(f"Anthropic 响应结构异常：{json.dumps(data, ensure_ascii=False)[:500]}") from e
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        # 扩展思考开启时（模型支持的前提下），thinking 块里是模型的思考过程
        reasoning = "".join(b.get("thinking", "") for b in blocks
                            if b.get("type") == "thinking").strip()
        tool_calls = [
            (b.get("id") or f"call_{i}", b.get("name", ""),
             json.dumps(b.get("input") or {}, ensure_ascii=False))
            for i, b in enumerate(blocks) if b.get("type") == "tool_use"
        ]
        if not text and not tool_calls and not reasoning:
            raise LLMError("大模型返回了空内容")
        return {"role": "assistant", "content": text or None}, tool_calls, reasoning

    # ---- HTTP ----

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False)
        log.debug("LLM 请求 %s model=%s msgs=%d", url, self.cfg.model, len(self.messages))
        try:
            resp = httpx.post(
                url, content=body.encode("utf-8"),
                headers={"User-Agent": USER_AGENT, **headers,
                         "Content-Type": "application/json"},
                timeout=self.cfg.timeout_seconds,
            )
        except httpx.HTTPError as e:
            raise LLMError(f"请求大模型失败：{e}") from e
        if resp.status_code != 200:
            raise LLMError(f"大模型返回 HTTP {resp.status_code}：{resp.text[:500]}")
        try:
            return resp.json()
        except ValueError as e:
            raise LLMError(f"大模型返回非 JSON：{resp.text[:500]}") from e
