"""测试密码密文存储 (PBKDF2-SHA256) 与终端快捷重置命令 (orcsy reset-password / passwd)。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import yaml

from orcsy.auth import hash_password, is_password_hashed, verify_password, update_config_auth_password
from orcsy.config import load_config
from orcsy.__main__ import cmd_reset_password


def test_hash_and_verify():
    pwd = "Admin-Secret_2026!"
    hashed = hash_password(pwd)
    assert is_password_hashed(hashed)
    assert hashed.startswith("pbkdf2:sha256:100000$")
    assert verify_password(pwd, hashed)
    assert not verify_password("wrong-password", hashed)
    assert not verify_password("", hashed)
    # 重复 hash 幂等
    assert hash_password(hashed) == hashed
    # 空密码返回空
    assert hash_password("") == ""
    # 向后兼容明文密码比对
    assert verify_password("legacy_plain", "legacy_plain")
    assert not verify_password("wrong", "legacy_plain")
    print("ok: hash_password 与 verify_password 密文比对及向后兼容明文比对")


def test_update_config_auth_password():
    with tempfile.TemporaryDirectory() as tmp:
        cfg_file = Path(tmp) / "config.yaml"
        # 初始含明文密码与注释的配置
        cfg_file.write_text("""# 系统配置
web:
  host: "127.0.0.1"
  port: 5116
  auth_password: "old-plain-password" # 这是旧密码注释
llm:
  model: deepseek-chat
""", encoding="utf-8")

        # 1. 重置为新密码
        new_pwd = "MyNewPassword#888"
        hashed = update_config_auth_password(cfg_file, new_pwd)
        assert is_password_hashed(hashed)
        
        # 验证文件内容已变成密文，且明文绝未残留
        content = cfg_file.read_text(encoding="utf-8")
        assert f'auth_password: "{hashed}"' in content
        assert new_pwd not in content
        assert "old-plain-password" not in content
        assert "# 系统配置" in content  # 注释保留
        assert "# 这是旧密码注释" in content

        # 校验 load_config 能正常读取并校验
        loaded = load_config(cfg_file, require_api_key=False)
        assert loaded.web.auth_password == hashed
        assert verify_password(new_pwd, loaded.web.auth_password)

        # 2. 清空密码
        cleared = update_config_auth_password(cfg_file, "")
        assert cleared == ""
        content_cleared = cfg_file.read_text(encoding="utf-8")
        assert 'auth_password: ""' in content_cleared
        loaded_cleared = load_config(cfg_file, require_api_key=False)
        assert loaded_cleared.web.auth_password == ""
    print("ok: update_config_auth_password 密文落盘、明文清除与注释保留")


def test_cli_cmd_reset_password():
    with tempfile.TemporaryDirectory() as tmp:
        cfg_file = Path(tmp) / "config.yaml"
        cfg_file.write_text("""web:
  host: "127.0.0.1"
  port: 5116
  auth_password: "init"
""", encoding="utf-8")

        # 1. 终端命令行指定参数重置
        args = argparse.Namespace(
            command="reset-password",
            config=str(cfg_file),
            password="cli-new-password",
            flag_password=None,
            clear=False,
        )
        rc = cmd_reset_password(args)
        assert rc == 0
        cfg = load_config(cfg_file, require_api_key=False)
        assert verify_password("cli-new-password", cfg.web.auth_password)
        assert is_password_hashed(cfg.web.auth_password)

        # 2. 命令行 --clear 清除密码
        args_clear = argparse.Namespace(
            command="reset-password",
            config=str(cfg_file),
            password=None,
            flag_password=None,
            clear=True,
        )
        rc_clear = cmd_reset_password(args_clear)
        assert rc_clear == 0
        cfg_clear = load_config(cfg_file, require_api_key=False)
        assert cfg_clear.web.auth_password == ""

        # 3. 非回环地址绑定（0.0.0.0）试图清空密码 → 必须被安全拦截
        cfg_file.write_text("""web:
  host: "0.0.0.0"
  port: 5116
  auth_password: "must-have-pass"
""", encoding="utf-8")
        rc_bad = cmd_reset_password(args_clear)
        assert rc_bad == 1  # 拦截拒绝
    print("ok: cmd_reset_password CLI 重置、清除与非回环地址安全守卫")


def main() -> int:
    test_hash_and_verify()
    test_update_config_auth_password()
    test_cli_cmd_reset_password()
    print("\n全部密码密文与重置命令测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
