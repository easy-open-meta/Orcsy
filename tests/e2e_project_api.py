"""项目管理/配置/指引 API 的端到端测试（对运行中的看板发请求）。"""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import os
os.environ.setdefault('NO_PROXY', '127.0.0.1,localhost')  # 本机回环不走系统代理（环境可能带 http_proxy）
import urllib.request

BASE = "http://127.0.0.1:5116"


def api(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def main() -> int:
    os.makedirs(r"D:\tmp\newproj", exist_ok=True)

    r = api("/api/project", {"name": "web-proj", "path": r"D:\tmp\newproj",
                             "tasks_dir": "tasks", "guide_file": "AGENT.md",
                             "max_tasks_per_cycle": 2, "max_agent_turns": 60,
                             "enabled": True})
    assert r["ok"], r
    assert set(r["created"]) == {"tasks/", "AGENT.md"}, r
    print("1. 新建项目 + 自动初始化:", r["message"])

    r = api("/api/project", {"name": "web-proj", "path": r"D:\tmp\newproj"})
    assert not r["ok"] and "已存在" in r["message"], r
    print("2. 重名拦截:", r["message"])

    r = api("/api/project", {"name": "ghost", "path": r"D:\no\such\dir"})
    assert not r["ok"], r
    print("3. 坏路径拦截:", r["message"])

    s = api("/api/state")
    names = [p["name"] for p in s["projects"]]
    assert "web-proj" in names, names
    print("4. state 可见:", names)

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
    assert s["llm"]["api_key"] == "sk-test"
    print("7. 配置保存 + 热重载: interval=120s, parallel=2, key 回显 ok")

    cfg_text = open(r"D:\Repos\agent-task-arrangement-system\run\config.yaml", encoding="utf-8").read()
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
