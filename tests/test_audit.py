"""写操作审计（V1-R5）自检：记录/过滤/脱敏/轮转 + Web 登录/写操作/API Key 留痕。"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orcsy.audit as A
from orcsy.config import (AppConfig, LLMConfig, ProjectConfig, RunLogConfig,
                          UpdateConfig, WebConfig)
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, make_server

PWD = "audit-password"


def check_store_basics() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="audit-store-"))
    try:
        log = A.AuditLog(tmp / "logs" / "audit.jsonl")
        log.record("task.save", actor="user", ip="127.0.0.1",
                   target="demo/T-1", detail={"keys": ["title"]})
        fake_key = "sk-" + "audit" * 6
        log.record("settings.save", actor="api_key:" + "a" * 8, ip="10.0.0.2",
                   target="config", result="error",
                   detail={"note": f"Bearer {fake_key}"})
        entries = log.tail(limit=10)
        assert len(entries) == 2, entries
        assert entries[0]["action"] == "settings.save", entries
        assert entries[0]["result"] == "error"
        raw = (tmp / "logs" / "audit.jsonl").read_text(encoding="utf-8")
        assert fake_key not in raw, "审计日志不得出现密钥原文"
        assert "[REDACTED" in raw
        only = log.tail(action="task")
        assert len(only) == 1 and only[0]["action"] == "task.save"
        only = log.tail(actor="api_key")
        assert len(only) == 1 and only[0]["actor"].startswith("api_key:")
        print("ok: 审计记录/倒序/过滤/脱敏")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_rotation() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="audit-rot-"))
    try:
        log = A.AuditLog(tmp / "audit.jsonl", max_bytes=64 * 1024, backups=3)
        for i in range(400):
            log.record("task.save", actor="user", target=f"p/T-{i}",
                       detail={"pad": "x" * 300})
        assert log.path.exists()
        backups = list(tmp.glob("audit-*.jsonl"))
        assert backups, "超过 64KB 应产生轮转备份"
        assert len(backups) <= 3
        print("ok: 超限轮转与备份上限")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _serve(state: AppState):
    server = make_server(state, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _call(port: int, path: str, *, body: dict | None = None,
          headers: dict | None = None, tries: int = 5) -> tuple[int, dict, dict]:
    h = dict(headers or {})
    data = None
    if body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    last: Exception | None = None
    for _ in range(tries):
        try:
            req = Request(f"http://127.0.0.1:{port}{path}", data=data,
                          headers={**h, "Connection": "close"}, method="POST" if body is not None else "GET")
            with urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)
        except HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8")), dict(e.headers)
        except Exception as e:  # noqa: BLE001 — 回环抖动手工重试
            last = e
            time.sleep(0.2)
    raise last  # type: ignore[misc]


def check_web_audit() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="audit-web-"))
    try:
        proj = tmp / "proj"
        (proj / "tasks").mkdir(parents=True)
        (proj / "AGENT.md").write_text("# 指引\n", encoding="utf-8")
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(
            update=UpdateConfig(auto_check=False),
            llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
            projects=[ProjectConfig(name="proj", path=proj)],
            runlog=RunLogConfig(enabled=True),
            web=WebConfig(host="127.0.0.1", port=5218, open_browser=False,
                          auth_password=PWD, api_keys=["sk-orcsy-audit-key"]),
            source_path=cfg_file)
        A._STORE = None
        state = AppState(cfg, Scheduler(cfg))
        state.LOGIN_FAIL_DELAY = 0.01
        server, port = _serve(state)
        try:
            # 登录失败/成功
            code, _, _ = _call(port, "/api/login", body={"password": "wrong"})
            assert code == 401, code
            code, payload, hdrs = _call(port, "/api/login", body={"password": PWD})
            assert code == 200 and payload.get("token"), (code, payload)
            token = payload["token"]
            auth = {"Authorization": f"Bearer {token}"}

            # 任务创建 + 状态变更
            code, payload, _ = _call(port, "/api/task", headers=auth, body={
                "project": "proj", "id": "T-A1", "title": "审计任务",
                "priority": "P2", "status": "open", "labels": [],
                "assignee": "", "description": "正文", "depends_on": []})
            assert code == 200, (code, payload)
            code, _, _ = _call(port, "/api/task/status", headers=auth,
                               body={"project": "proj", "id": "T-A1",
                                     "status": "blocked"})
            assert code == 200, code

            # 设置保存
            code, _, _ = _call(port, "/api/config", headers=auth,
                               body={"scheduler": {"interval_seconds": 600}})
            assert code == 200, code

            # API Key 写调用：审计主体只能是哈希前缀
            api_key = "sk-orcsy-audit-key"
            code, _, _ = _call(port, "/api/task/status",
                               headers={"X-API-Key": api_key},
                               body={"project": "proj", "id": "T-A1",
                                     "status": "open"})
            assert code == 200, code

            entries = A.get_audit_log().tail(limit=100)
            actions = [(e["action"], e["actor"], e["result"]) for e in entries]
            assert any(a == "auth.login" and r == "fail" for a, _, r in actions), actions
            assert any(a == "auth.login" and r == "ok" for a, _, r in actions), actions
            assert any(a == "task.save" and ac == "user" for a, ac, _ in actions), actions
            assert any(a == "task.status" for a, _, _ in actions), actions
            assert any(a == "settings.save" for a, _, _ in actions), actions
            api_entries = [e for e in entries
                           if e["action"] == "task.status" and e["actor"].startswith("api_key:")]
            assert api_entries, actions
            assert api_key not in json.dumps(entries, ensure_ascii=False)

            # 审计查询接口（需登录）：支持 action 过滤
            code, payload, _ = _call(port, "/api/audit?action=task&limit=20",
                                     headers=auth)
            assert code == 200 and payload.get("entries"), (code, payload)
            assert all("task" in e["action"] for e in payload["entries"])

            raw = A.get_audit_log().path.read_text(encoding="utf-8")
            assert PWD not in raw, "审计日志不得记录登录密码"
            assert api_key not in raw, "审计日志不得记录 API Key 原文"
            print("ok: Web 登录/任务/设置/API Key 审计 + 查询过滤 + 无明文密钥")
        finally:
            server.shutdown()
    finally:
        A._STORE = None
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_store_basics()
    check_rotation()
    check_web_audit()
    print("\n写操作审计自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
