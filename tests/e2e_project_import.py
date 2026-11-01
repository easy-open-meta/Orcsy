"""导入项目 / 打开文件夹 / 模型思考日志 / 模型连通性测试 的端到端自检（自起服务，不依赖运行中的看板）。

覆盖：
1. 导入现有项目：只新增 tasks/ 与 AGENT.md，AGENT.md 标题带项目名，不动其它文件；
2. /api/project/open-folder：未知项目拦截 + 正常路径调起系统文件管理器；
3. 模型思考内容：OpenAI 兼容响应里的 reasoning_content 写入运行日志（💭 条目），
   Anthropic 响应里的 thinking 块同样处理；
4. /api/llm/test：极短对话验证连通（草稿值优先/缺省回退已存配置/错误透出）。
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import threading
import urllib.error
import os
os.environ.setdefault('NO_PROXY', '127.0.0.1,localhost')  # 本机回环不走系统代理（环境可能带 http_proxy）
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台打不出 ✅ 等字符
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from orcsy.config import LLMConfig, load_config
from orcsy.llm import FinishSignal, LLMClient, finish_response
from orcsy.scheduler import Scheduler
from orcsy.tools import ToolContext
from orcsy.web import AppState, make_server

BASE = "http://127.0.0.1:0"  # 实际端口由 server.server_address 决定


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
    # Windows 本机回环偶发 ConnectionResetError（快速连断竞态）：退避重试 5 次。
    # 注意重试只对幂等 GET 安全；POST（创建类）重试可能重复执行，测试断言需容忍
    last_err: Exception | None = None
    import time as _time
    for attempt in range(5):
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
            _time.sleep(0.4 * (attempt + 1))
    raise last_err  # type: ignore[misc]


class CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def check_import(root: Path, port: int) -> None:
    proj = root / "existing-proj"
    proj.mkdir()
    (proj / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (proj / "README.md").write_text("# existing\n", encoding="utf-8")
    (proj / "package.json").write_text(json.dumps({
        "name": "existing-proj", "version": "1.0.0",
        "scripts": {"test": "jest", "dev": "vite"},
        "devDependencies": {"vite": "^5.0.0"},
    }), encoding="utf-8")
    before = {p.name for p in proj.iterdir()}

    r = api(port, "/api/project", {"name": "imported-proj", "path": str(proj),
                                   "tasks_dir": "tasks", "guide_file": "AGENT.md"})
    assert r.get("ok"), r
    assert set(r["created"]) == {"tasks/", "AGENT.md"}, r["created"]

    after = {p.name for p in proj.iterdir()}
    assert after - before == {"tasks", "AGENT.md"}, after
    assert not any(proj.joinpath("tasks").glob("*.md")), "不应生成示例任务"

    guide = (proj / "AGENT.md").read_text(encoding="utf-8")
    assert guide.startswith("# imported-proj — Development Guide"), guide.splitlines()[0]
    assert "## Workflow" in guide, "应保留模板完整骨架"
    assert "## 禁止事项" not in guide, "模板应已全英文"
    # 技术栈自动填充：package.json → Node.js (Vite)，run/test 命令同步填入
    assert "- Stack: Node.js (Vite)" in guide, [l for l in guide.splitlines() if "Stack" in l]
    assert "- Run: npm run dev" in guide and "- Test: npm test" in guide, guide
    print("1. 导入现有项目：只新增 tasks/ + AGENT.md，标题带项目名，技术栈自动填充 ✅")

    # /api/probe 探测端点（新建项目对话框预填用）
    r = api(port, "/api/probe", {"path": str(proj)})
    assert r.get("ok") and r["stack"] == "Node.js (Vite)", r
    r = api(port, "/api/probe", {"path": str(root / "no-such-dir")})
    assert not r.get("ok"), r
    print("9. /api/probe：技术栈探测端点正常、不存在目录报错 ✅")

    # 用户显式指定的技术栈优先于自动探测；探测不到的字段留空
    proj2 = root / "explicit-proj"
    proj2.mkdir()
    r = api(port, "/api/project", {"name": "explicit-proj", "path": str(proj2),
                                   "tasks_dir": "tasks", "guide_file": "AGENT.md",
                                   "stack": "Custom Monorepo", "run": "make dev",
                                   "test": ""})
    assert r.get("ok"), r
    guide2 = (proj2 / "AGENT.md").read_text(encoding="utf-8")
    assert "- Stack: Custom Monorepo" in guide2 and "- Run: make dev" in guide2, guide2
    assert "- Test:" in guide2 and "- Test: " not in guide2, guide2  # 留空保持占位
    print("10. 显式技术栈字段优先写入 AGENT.md，空字段保持留空 ✅")

    # 再次编辑（original_name）不应重复初始化
    r = api(port, "/api/project", {"name": "imported-proj", "path": str(proj),
                                   "original_name": "imported-proj"})
    assert r.get("ok"), r
    assert r["created"] == [], r["created"]
    print("2. 编辑已有项目：不再重复初始化 ✅")

    # 列表接口可见
    s = api(port, "/api/state")
    assert "imported-proj" in [p["name"] for p in s["projects"]], s["projects"]
    print("3. /api/state 可见导入的项目 ✅")


def check_open_folder(root: Path, port: int) -> None:
    r = api(port, "/api/project/open-folder", {"name": "no-such"})
    assert not r.get("ok") and "未知项目" in r.get("message", ""), r
    print("4. 打开文件夹：未知项目被拦截 ✅")

    import orcsy.web as web_mod

    opened: list[str] = []
    real_startfile = getattr(web_mod.os, "startfile", None)
    web_mod.os.startfile = lambda p: opened.append(str(p))  # type: ignore[attr-defined]
    try:
        r = api(port, "/api/project/open-folder", {"name": "imported-proj"})
    finally:
        if real_startfile is not None:
            web_mod.os.startfile = real_startfile  # type: ignore[attr-defined]
        else:
            del web_mod.os.startfile  # type: ignore[attr-defined]
    assert r.get("ok"), r
    assert len(opened) == 1 and opened[0].endswith("existing-proj"), opened
    print("5. 打开文件夹：正常调起系统文件管理器 ✅")

    # 移除项目：仅移出配置，磁盘文件与 AGENT.md/tasks 全部保留
    r = api(port, "/api/project/delete", {"name": "imported-proj"})
    assert r.get("ok"), r
    s = api(port, "/api/state")
    assert "imported-proj" not in [p["name"] for p in s["projects"]], s["projects"]
    proj = root / "existing-proj"
    assert (proj / "AGENT.md").is_file() and (proj / "app.py").is_file(), "文件不应被删除"
    assert (proj / "tasks").is_dir(), "任务目录不应被删除"
    print("5b. 移除项目：配置移除、磁盘文件全部保留 ✅")


def check_thinking_log(root: Path) -> None:
    cap = CaptureHandler()
    cap.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    llm_logger = logging.getLogger("orcsy.llm")
    llm_logger.setLevel(logging.INFO)
    llm_logger.addHandler(cap)

    calls: list[dict] = []

    def fake_post(self, url, payload, headers):
        calls.append(payload)
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "先看目录结构，再定位登录逻辑。",
                    "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "finish",
                                     "arguments": json.dumps({"action": "completed",
                                                              "summary": "搞定"})},
                    }],
                },
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    real_post = LLMClient._post
    LLMClient._post = fake_post  # type: ignore[method-assign]
    try:
        cfg = LLMConfig(api_key="sk-test", base_url="https://api.example.com/v1",
                        model="test-model")
        client = LLMClient(cfg)
        client.reset("system")
        try:
            client.run_task_loop("做个任务", ToolContext(root=root))
        except FinishSignal as sig:
            resp = finish_response(sig.payload)
        assert resp.action == "completed", resp.action
        # 思考内容不应被回传给服务端（payload 的历史消息里无 reasoning 字段）
        assert "reasoning_content" not in json.dumps(calls[0]), "思考内容不应回传"
    finally:
        LLMClient._post = real_post  # type: ignore[method-assign]
        llm_logger.removeHandler(cap)

    think = [r for r in cap.records if "💭 模型思考" in r and "先看目录结构" in r]
    assert think, cap.records
    print("6. 模型思考内容写入运行日志（💭 条目，可展开） ✅")

    # Anthropic 协议的 thinking 块同样落日志
    cap.records.clear()
    llm_logger.addHandler(cap)

    def fake_post_anthropic(self, url, payload, headers):
        return {
            "content": [
                {"type": "thinking", "thinking": "Anthropic 思考过程。"},
                {"type": "text", "text": ""},
                {"type": "tool_use", "id": "t1", "name": "finish",
                 "input": {"action": "completed", "summary": "搞定"}},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    LLMClient._post = fake_post_anthropic  # type: ignore[method-assign]
    try:
        cfg = LLMConfig(provider="anthropic", api_key="sk-test",
                        base_url="https://api.example.com", model="claude-test")
        client = LLMClient(cfg)
        client.reset("system")
        try:
            client.run_task_loop("做个任务", ToolContext(root=root))
        except FinishSignal:
            pass
    finally:
        LLMClient._post = real_post  # type: ignore[method-assign]
        llm_logger.removeHandler(cap)
    assert any("Anthropic 思考过程" in r for r in cap.records), cap.records
    print("7. Anthropic thinking 块同样写入运行日志 ✅")


def check_thinking_switch() -> None:
    # 开关关闭（默认）：请求不携带任何思考参数
    client = LLMClient(LLMConfig(api_key="sk-test",
                                 base_url="https://api.example.com/v1",
                                 model="test-model"))
    client.reset("system")
    p = client._payload_openai(False)
    assert "enable_thinking" not in p and "thinking" not in p, p

    # OpenAI 兼容 + 开关：enable_thinking 与 thinking 对象都带上
    client_t = LLMClient(LLMConfig(api_key="sk-test",
                                   base_url="https://api.example.com/v1",
                                   model="test-model", enable_thinking=True))
    client_t.reset("system")
    p = client_t._payload_openai(False)
    assert p.get("enable_thinking") is True and p.get("thinking") == {"type": "enabled"}, p

    # Anthropic + 开关：thinking 参数带预算，temperature 必须省略（官方要求固定 1）
    client_a = LLMClient(LLMConfig(provider="anthropic", api_key="sk-test",
                                   base_url="https://api.example.com",
                                   model="claude-test", max_tokens=16384,
                                   temperature=0.2, enable_thinking=True))
    client_a.reset("system")
    client_a.messages.append({"role": "user", "content": "hi"})
    p = client_a._payload_anthropic(False)
    assert p.get("thinking", {}).get("type") == "enabled", p.get("thinking")
    budget = p["thinking"]["budget_tokens"]
    assert 1024 <= budget < p["max_tokens"], (budget, p["max_tokens"])
    assert "temperature" not in p, p
    print("8. 模型思考开关：请求参数按协议正确注入 ✅")


def check_test_model(port: int) -> None:
    """/api/llm/test：极短对话验证连通；草稿值优先、缺省回退已存配置、错误透出。"""
    from orcsy.llm import LLMClient, LLMError

    calls: list[dict] = []
    real_post = LLMClient._post

    def fake_post(self, url, payload, headers):
        calls.append(payload)
        # 连通性探针必须保持小额：max_tokens 限死，单条 user 消息、不带工具
        assert payload["max_tokens"] <= 128, payload.get("max_tokens")
        assert payload["messages"] and payload["messages"][0]["role"] == "user"
        assert "tools" not in payload and "thinking" not in payload
        return {"choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1}}

    LLMClient._post = fake_post  # type: ignore[method-assign]
    try:
        r = api(port, "/api/llm/test", {
            "provider": "openai", "base_url": "https://api.example.com/v1",
            "api_key": "sk-x", "model": "draft-model"})
        assert r.get("ok") and "连接成功" in r["message"] and r["reply"] == "OK", r
        assert calls[-1]["model"] == "draft-model", "草稿值应优先于已存配置"

        r = api(port, "/api/llm/test", {})
        assert r.get("ok"), r
        assert calls[-1]["model"] == "test-model", "缺省应回退已存配置"

        def fake_err(self, url, payload, headers):
            raise LLMError("大模型返回 HTTP 401：Unauthorized")

        LLMClient._post = fake_err  # type: ignore[method-assign]
        r = api(port, "/api/llm/test", {"provider": "openai",
                                        "base_url": "https://api.example.com/v1",
                                        "api_key": "sk-bad", "model": "gpt-x"})
        assert not r.get("ok") and "401" in r["message"], r

        LLMClient._post = fake_post  # type: ignore[method-assign]
        # model 传空按约定回退已存配置（与 /api/models 同模式）
        r = api(port, "/api/llm/test", {"provider": "openai",
                                        "base_url": "https://api.example.com/v1", "model": ""})
        assert r.get("ok") and calls[-1]["model"] == "test-model", r
    finally:
        LLMClient._post = real_post  # type: ignore[method-assign]

    # 参数校验在任何 HTTP 之前拦截（无需假响应）
    r = api(port, "/api/llm/test", {"provider": "openai", "base_url": "ftp://x", "model": "m"})
    assert not r.get("ok") and "http" in r["message"].lower(), r
    print("11. 测试连接：极短对话验证连通、草稿值优先/缺省回退、错误与参数校验透出 ✅")


def check_log_retention(port: int, root: Path) -> None:
    """日志保留天数：看板保存立即生效、写回配置、非法值拦截。"""
    r = api(port, "/api/config", {"logging": {"retention_days": 3}})
    assert r.get("ok"), r
    s = api(port, "/api/state")
    assert s["logging"]["retention_days"] == 3, s.get("logging")
    assert "retention_days: 3" in (root / "config.yaml").read_text(encoding="utf-8")
    r = api(port, "/api/config", {"logging": {"retention_days": 999}})
    assert not r.get("ok") and "0~365" in r.get("message", ""), r
    print("12. 日志保留天数：看板保存立即生效、写回配置、非法值拦截 ✅")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ta-selfcheck-"))
    try:
        cfg_path = make_config(root)
        config = load_config(cfg_path, require_api_key=False)

        scheduler = Scheduler(config)
        state = AppState(config, scheduler, log_path=str(root / "logs" / "test.log"))
        server = make_server(state, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        check_import(root, port)
        check_open_folder(root, port)
        check_thinking_log(root)
        check_thinking_switch()
        check_test_model(port)
        check_log_retention(port, root)

        server.shutdown()
        server.server_close()
        print("\n本地自检全部通过 ✅")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
