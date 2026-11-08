"""LocalDebugConfig 配置自检：调试配置解析、白名单格式校验、项目级合并规则与硬禁区拦截。

运行：uv run python tests/test_local_debug_config.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

import yaml
from orcsy.config import (LocalDebugConfig, ProjectConfig, load_config,
                          resolve_debug_hosts, validate_allowed_hosts)
from orcsy.hosts import check_host_allowed, is_ip_in_forbidden_ranges


def check_validate_allowed_hosts() -> None:
    # 正常列表
    hosts = validate_allowed_hosts(["localhost", "127.0.0.1", "192.168.1.50", "::1"])
    assert "localhost" in hosts
    assert "127.0.0.1" in hosts
    assert "192.168.1.50" in hosts
    assert "::1" in hosts

    # 空列表或 None
    assert validate_allowed_hosts(None) == []
    assert validate_allowed_hosts([]) == []

    # 非法输入校验
    try:
        validate_allowed_hosts("not-a-list")
        assert False, "应拒绝非列表输入"
    except ValueError:
        pass

    # 包含协议头
    try:
        validate_allowed_hosts(["http://localhost"])
        assert False, "应拒绝带协议头的条目"
    except ValueError as e:
        assert "协议头" in str(e)

    # 包含斜杠（如 CIDR）
    try:
        validate_allowed_hosts(["192.168.1.0/24"])
        assert False, "应拒绝斜杠或 CIDR 网段"
    except ValueError as e:
        assert "斜杠" in str(e)

    # 包含通配符
    try:
        validate_allowed_hosts(["*.example.com"])
        assert False, "应拒绝通配符"
    except ValueError as e:
        assert "通配符" in str(e)

    # 包含端口号
    try:
        validate_allowed_hosts(["localhost:8080"])
        assert False, "应拒绝带端口号"
    except ValueError as e:
        assert "端口号" in str(e)

    print("validate_allowed_hosts 校验语义 ✅")


def check_resolve_debug_hosts() -> None:
    global_cfg = LocalDebugConfig(allowed_hosts=["localhost", "127.0.0.1"])

    # 项目未覆盖：原样返回全局
    assert resolve_debug_hosts(global_cfg, None) == ["localhost", "127.0.0.1"]
    assert resolve_debug_hosts(global_cfg, []) == ["localhost", "127.0.0.1"]

    # 项目级追加：并集且只增不减
    combined = resolve_debug_hosts(global_cfg, ["192.168.1.100", "localhost"])
    assert combined == ["localhost", "127.0.0.1", "192.168.1.100"]
    print("resolve_debug_hosts 合并（只增不减）语义 ✅")


def check_host_security_and_forbidden_ranges() -> None:
    allowed = ["localhost", "127.0.0.1", "169.254.169.254", "0.0.0.0"]

    # 回环地址正常放行
    ok, err = check_host_allowed("127.0.0.1", allowed)
    assert ok is True and not err

    ok, err = check_host_allowed("localhost", allowed)
    assert ok is True and not err

    # 不在白名单的主机直接拒绝
    ok, err = check_host_allowed("192.168.1.99", allowed)
    assert ok is False and "不在允许的调试白名单中" in err

    # 硬禁区：即使人为人为写进 allowed 列表，也必须被拦截
    ok, err = check_host_allowed("169.254.169.254", allowed)
    assert ok is False and "硬禁区" in err or "禁区" in err

    ok, err = check_host_allowed("0.0.0.0", allowed)
    assert ok is False and ("硬禁区" in err or "禁区" in err)

    print("check_host_allowed 硬禁区拦截与白名单校验 ✅")


def check_load_config_local_debug() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write("""
llm:
  provider: openai
  api_key: sk-test
  base_url: https://api.openai.com/v1
  model: gpt-4o

local_debug:
  browser: edge
  browser_executable: ""
  headless: false
  allowed_hosts:
    - localhost
    - 127.0.0.1
    - test.lan
  navigation_timeout_seconds: 45
  service_ready_timeout_seconds: 90
  max_services: 2
  artifacts_retention_days: 14

projects:
  - name: demo-web
    path: .
    debug_allowed_hosts:
      - 192.168.1.88
""")
        cfg_path = Path(f.name)

    try:
        cfg = load_config(cfg_path)
        assert cfg.local_debug.browser == "edge"
        assert cfg.local_debug.headless is False
        assert cfg.local_debug.allowed_hosts == ["localhost", "127.0.0.1", "test.lan"]
        assert cfg.local_debug.navigation_timeout_seconds == 45
        assert cfg.local_debug.service_ready_timeout_seconds == 90
        assert cfg.local_debug.max_services == 2
        assert cfg.local_debug.artifacts_retention_days == 14

        proj = cfg.projects[0]
        assert proj.debug_allowed_hosts == ["192.168.1.88"]
        effective = resolve_debug_hosts(cfg.local_debug, proj.debug_allowed_hosts)
        assert effective == ["localhost", "127.0.0.1", "test.lan", "192.168.1.88"]
        print("load_config local_debug & debug_allowed_hosts 解析 ✅")
    finally:
        cfg_path.unlink(missing_ok=True)


def main() -> None:
    check_validate_allowed_hosts()
    check_resolve_debug_hosts()
    check_host_security_and_forbidden_ranges()
    check_load_config_local_debug()
    print("\nAll local_debug config tests passed! ✨")


if __name__ == "__main__":
    main()
