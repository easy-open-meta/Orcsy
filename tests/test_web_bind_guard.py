"""非回环绑定必须配置登录密码的守卫自检（安全审计 2026-11-04 M1）。

守卫只关心 host/auth_password 组合：回环地址免密码照旧零打扰；非回环
（0.0.0.0 / 局域网 IP / 无法解析的主机名）必须带密码，否则配置文件加载、
看板保存、CLI --host 覆盖三条入口全部拒绝。

运行：uv run python tests/test_web_bind_guard.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import load_config, validate_web_security


def check_validate_web_security() -> None:
    # 回环 + 空密码：放行（本机零打扰的既有用法）
    for host in ("127.0.0.1", "localhost", "LOCALHOST", "::1", "127.0.0.2"):
        validate_web_security(host, "")
        validate_web_security(host, "pw")
    # 非回环 + 空密码：拒绝（文案可行动，点出 auth_password）
    for host in ("0.0.0.0", "::", "192.168.1.5", "10.0.0.8",
                 "example.com", "garbage host!"):
        try:
            validate_web_security(host, "")
            raise AssertionError(f"非回环 + 空密码应拒绝：{host}")
        except ValueError as e:
            assert "auth_password" in str(e) and host in str(e), (host, e)
        # 同一地址配了密码即放行
        validate_web_security(host, "pw")
    # 空/None host 按默认回环处理
    validate_web_security("", "")
    print("ok: 回环放行 / 非回环无密码拒绝 / 有密码放行 / 垃圾主机名从严")


def check_load_config_guard() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bind-guard-"))
    try:
        cfg = tmp / "config.yaml"
        cfg.write_text("llm: {api_key: sk-x}\n"
                       "web:\n  host: 0.0.0.0\n  port: 5116\n",
                       encoding="utf-8")
        try:
            load_config(cfg)
            raise AssertionError("host=0.0.0.0 + 空密码应拒绝加载")
        except ValueError as e:
            assert "auth_password" in str(e), e
        # 补上密码 → 正常加载
        cfg.write_text("llm: {api_key: sk-x}\n"
                       "web:\n  host: 0.0.0.0\n  auth_password: pw\n",
                       encoding="utf-8")
        c = load_config(cfg)
        assert c.web.host == "0.0.0.0" and c.web.auth_password == "pw"
        # 默认配置（127.0.0.1、无密码）不受守卫影响
        cfg.write_text("llm: {api_key: sk-x}\n", encoding="utf-8")
        c = load_config(cfg)
        assert c.web.host == "127.0.0.1" and c.web.auth_password == ""
        print("ok: load_config 接入守卫（拒绝启动/带密码放行/默认配置零感知）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_validate_web_security()
    check_load_config_guard()
    print("\n非回环绑定守卫全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
