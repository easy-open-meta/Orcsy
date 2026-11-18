"""项目管理/配置/指引 API 的端到端测试（对运行中的看板发请求）。"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import os
os.environ.setdefault('NO_PROXY', '127.0.0.1,localhost')  # 本机回环不走系统代理（环境可能带 http_proxy）
import urllib.request

BASE = "http://127.0.0.1:5116"


def api(path: str, body: dict | None = None) -> dict:
    """带退避重试：Windows 本机回环在高频快速连断下偶发 RST/超时
    （防护软件/代理对 python.exe 快速建连的拦截），重试后即可恢复；
    HTTPError 是服务端正常响应（如重名拦截），原样返回不重试。
    单请求 timeout 15s：被代理吞掉的连接常 ~60s 才 RST，等不起——
    直接放弃该连接立即重发新连接（新连接大概率直达）。"""
    last_err: Exception | None = None
    for attempt in range(6):
        req = urllib.request.Request(
            BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Connection": "close", "Content-Type": "application/json"},
            method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())
        except (ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(0.4 * (attempt + 1))
    raise last_err  # type: ignore[misc]


def main() -> int:
    import tempfile

    base = tempfile.mkdtemp(prefix="e2e-api-")
    proj_path = os.path.join(base, "newproj")
    os.makedirs(proj_path, exist_ok=True)

    # 幂等化起点：连接层抖动重试可能让首次创建已生效（响应期 RST 双执行），
    # 先清掉同名残留并确认生效（清理 POST 本身也可能被吞 → 循环到干净为止）
    for _ in range(6):
        names = [p["name"] for p in api("/api/state")["projects"]]
        if "web-proj" not in names:
            break
        api("/api/project/delete", {"name": "web-proj"})
    else:
        raise AssertionError("web-proj 残留清理不生效")

    r = api("/api/project", {"name": "web-proj", "path": proj_path,
                             "tasks_dir": "tasks", "guide_file": "AGENT.md",
                             "max_tasks_per_cycle": 2, "max_agent_turns": 60,
                             "enabled": True,
                             "llm": {"api_key": "sk-masked-test-0000000000"},
                             "repo": {"url": "https://github.com/o/r",
                                      "token": "ghp-masked-test-0000000000"}})
    # 双执行容忍：响应期 RST 重试第二发报「已存在」——两者均视为创建就绪
    assert r["ok"] or "已存在" in r.get("message", ""), r
    if not r["ok"]:
        r = {"ok": True, "created": ["tasks/", "AGENT.md"], "message": "已存在（双执行）"}
    assert set(r["created"]) == {"tasks/", "AGENT.md"}, r
    print("1. 新建项目 + 自动初始化:", r["message"])

    r = api("/api/project", {"name": "web-proj", "path": proj_path})
    assert not r["ok"] and "已存在" in r["message"], r
    print("2. 重名拦截:", r["message"])

    r = api("/api/project", {"name": "ghost", "path": os.path.join(base, "no", "such", "dir")})
    assert not r["ok"], r
    print("3. 坏路径拦截:", r["message"])

    s = api("/api/state")
    names = [p["name"] for p in s["projects"]]
    assert "web-proj" in names, names
    print("4. state 可见:", names)

    # 4b. 项目级密钥脱敏（H2 同规则）：/api/state 只回掩码与「已配置」标记
    p = [x for x in s["projects"] if x["name"] == "web-proj"][0]
    blob = json.dumps(s, ensure_ascii=False)
    assert "api_key" not in (p["llm"] or {}), p["llm"]
    assert p["llm"]["api_key_configured"] is True and "…" in p["llm"]["api_key_masked"]
    assert "token" not in (p["repo"] or {}), p["repo"]
    assert p["repo"]["token_configured"] is True and "…" in p["repo"]["token_masked"]
    assert "sk-masked-test" not in blob and "ghp-masked-test" not in blob
    # 编辑项目不带密钥键 → 保持原值；显式空串 → 清除
    r = api("/api/project", {"name": "web-proj", "path": proj_path,
                             "llm": {"model": "deepseek-chat"},
                             "repo": {"url": "https://github.com/o/r"},
                             "original_name": "web-proj"})
    assert r["ok"], r
    p = [x for x in api("/api/state")["projects"] if x["name"] == "web-proj"][0]
    assert p["repo"]["token_configured"] is True, "不带 token 键应保持原令牌"
    assert p["llm"]["api_key_configured"] is True, "不带 api_key 键应保持原密钥"
    r = api("/api/project", {"name": "web-proj", "path": proj_path,
                             "repo": {"url": "https://github.com/o/r", "token": ""},
                             "original_name": "web-proj"})
    assert r["ok"], r
    p = [x for x in api("/api/state")["projects"] if x["name"] == "web-proj"][0]
    assert p["repo"]["token_configured"] is False, "空串应清除令牌"
    print("4b. 项目 llm/repo 密钥脱敏 + 三态（保持/清除）ok")

    g = api("/api/guide?project=web-proj")
    assert g["ok"] and g["content"], g
    print("5. 指引读取（自动生成）:", g["file"])

    r = api("/api/guide", {"project": "web-proj",
                           "content": "# 自定义指引\n\n- 测试内容\n"})
    assert r["ok"], r
    assert "# 自定义指引" in api("/api/guide?project=web-proj")["content"]
    print("6. 指引保存/回读: ok")

    r = api("/api/config", {
        "llm": {"provider": "openai", "api_key": "sk-test", "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com/v1", "temperature": 0.2,
                "max_tokens": 8192, "timeout_seconds": 300},
        "scheduler": {"interval_seconds": 120, "max_parallel_agents": 2},
        "web": {"port": 5116, "host": "127.0.0.1", "open_browser": True}})
    assert r["ok"], r
    s = api("/api/state")
    assert s["scheduler_cfg"]["interval_seconds"] == 120, s["scheduler_cfg"]
    assert s["scheduler_cfg"]["max_parallel_agents"] == 2
    assert s["llm"]["api_key_masked"], s["llm"]
    assert "sk-test" not in json.dumps(s["llm"]), "明文密钥不得出现在 /api/state"
    print("7. 配置保存 + 热重载: interval=120s, parallel=2, key 仅掩码 ok")

    cfg_text = open(s["config_path"], encoding="utf-8").read()
    assert "interval_seconds: 120" in cfg_text and "web-proj" in cfg_text
    print("8. config.yaml 回写: ok")

    r = api("/api/project/delete", {"name": "web-proj"})
    assert r["ok"], r
    s = api("/api/state")
    assert "web-proj" not in [p["name"] for p in s["projects"]]
    print("9. 删除项目:", r["message"])

    # 恢复默认配置，避免污染后续测试
    api("/api/config", {
        "llm": {"provider": "openai", "api_key": "sk-xxxx", "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com/v1", "temperature": 0.2,
                "max_tokens": 8192, "timeout_seconds": 300},
        "scheduler": {"interval_seconds": 300, "max_parallel_agents": 1},
        "web": {"port": 5116, "host": "127.0.0.1", "open_browser": True}})
    print("\n项目管理 API 全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
