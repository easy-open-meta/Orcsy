"""大模型接入与 Agent 工具循环。

两种协议均原生支持工具调用（function calling）：
- openai：OpenAI Chat Completions + tools（覆盖 DeepSeek/GLM/Kimi/通义/Ollama/vLLM 等）
- anthropic：Anthropic Messages API + tools

Agent 循环（run_task_loop）：
  任务提示 → 模型 → （工具调用 → 执行 → 结果回传）* N → 模型调用 finish 工具 → 结构化结论

安全：文件工具被 ToolContext 禁锢在项目根内；命令工具有白名单；循环有轮数上限。
"""

from __future__ import annotations

import json

import httpx

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


def list_models(provider: str, base_url: str, api_key: str,
                timeout_seconds: int = 30) -> list[dict]:
    """拉取大模型服务的模型列表，尽量带上每个模型的上下文/输出上限。

    openai 兼容与 anthropic 都是 GET {base_url}/models；anthropic 响应自带
    context_window / max_output_tokens，openai 兼容服务通常没有（置 None，
    由看板用 model_spec 补全）。
    """
    url = base_url.rstrip("/") + "/models"
    headers = _auth_headers(provider, api_key)
    try:
        resp = httpx.get(url, headers=headers, timeout=min(max(timeout_seconds, 5), 30))
    except httpx.HTTPError as e:
        raise LLMError(f"获取模型列表失败：{e}") from e
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
        use_tools = True

        for turn in range(1, max_turns + 1):
            if self.cfg.provider == "anthropic":
                assistant_msg, tool_calls = self._step_anthropic(use_tools)
            else:
                assistant_msg, tool_calls = self._step_openai(use_tools)

            self.messages.append(assistant_msg)

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
                assistant_msg, tool_calls = self._step_anthropic(True)
            else:
                assistant_msg, tool_calls = self._step_openai(True)
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
        """消息总量逼近模型上下文上限时，把最早的工具结果替换为占位符。

        只动 role=tool 的文本内容——assistant 的 tool_calls 结构原样保留，
        tool_call/tool_result 配对不会被裁断。首条 system 与最后 6 条消息
        不参与裁剪。token 估算取 len//3（中英混合偏保守），用途是防撞墙
        而非精确计费。
        """
        limit = int(getattr(self.cfg, "max_context_tokens", 0) or 0)
        if limit <= 0 or len(self.messages) <= 8:
            return
        total = sum(_est_tokens_msg(m) for m in self.messages)
        if total < int(limit * 0.9):
            return
        target = int(limit * 0.7)
        for i in range(1, max(1, len(self.messages) - 6)):
            if total <= target:
                break
            m = self.messages[i]
            content = m.get("content")
            if m.get("role") == "tool" and isinstance(content, str) and len(content) > 200:
                total -= _est_tokens_msg(m) - len(_TRIMMED_PLACEHOLDER) // 3
                m["content"] = _TRIMMED_PLACEHOLDER
        log.info("上下文接近上限（limit=%d），已裁剪早期工具结果", limit)

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
        if use_tools:
            payload["tools"] = tool_definitions_openai()
        return payload

    def _step_openai(self, use_tools: bool) -> tuple[dict, list[tuple[str, str, str]]]:
        data = self._post(
            self.cfg.base_url.rstrip("/") + "/chat/completions",
            self._payload_openai(use_tools),
            _auth_headers("openai", self.cfg.api_key),
        )
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"OpenAI 响应结构异常：{json.dumps(data, ensure_ascii=False)[:500]}") from e
        content = msg.get("content") or ""
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            tool_calls.append((tc.get("id") or f"call_{len(tool_calls)}",
                               fn.get("name", ""), fn.get("arguments") or "{}"))
        if not content and not tool_calls:
            raise LLMError("大模型返回了空内容")
        return {"role": "assistant", "content": content or None}, tool_calls

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
        if use_tools:
            payload["tools"] = tool_definitions_anthropic()
        return payload

    def _step_anthropic(self, use_tools: bool) -> tuple[dict, list[tuple[str, str, str]]]:
        data = self._post(
            self.cfg.base_url.rstrip("/") + "/messages",
            self._payload_anthropic(use_tools),
            _auth_headers("anthropic", self.cfg.api_key),
        )
        try:
            blocks = data["content"]
        except (KeyError, TypeError) as e:
            raise LLMError(f"Anthropic 响应结构异常：{json.dumps(data, ensure_ascii=False)[:500]}") from e
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        tool_calls = [
            (b.get("id") or f"call_{i}", b.get("name", ""),
             json.dumps(b.get("input") or {}, ensure_ascii=False))
            for i, b in enumerate(blocks) if b.get("type") == "tool_use"
        ]
        if not text and not tool_calls:
            raise LLMError("大模型返回了空内容")
        return {"role": "assistant", "content": text or None}, tool_calls

    # ---- HTTP ----

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False)
        log.debug("LLM 请求 %s model=%s msgs=%d", url, self.cfg.model, len(self.messages))
        try:
            resp = httpx.post(
                url, content=body.encode("utf-8"),
                headers={**headers, "Content-Type": "application/json"},
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
