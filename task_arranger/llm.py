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
                      max_turns: int = DEFAULT_MAX_TURNS) -> AgentResponse:
        """执行一个任务的完整工具循环，返回结构化结论。"""
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
                result = dispatch_tool(ctx, name, args_json)
                log.debug("工具结果 [%d] %s：%s", turn, name, result[:200].replace("\n", " "))
                self.messages.append(self._tool_result(call_id, result))

        # 轮数耗尽：要求强制收尾
        log.warning("工具循环达到最大轮数 %d，要求强制收尾", max_turns)
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
            {"Authorization": f"Bearer {self.cfg.api_key}"},
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
            {"x-api-key": self.cfg.api_key, "anthropic-version": "2023-06-01"},
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
