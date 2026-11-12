"""看板访问密码哈希加密、密文存储与验证管理。

使用工业标准的 PBKDF2-HMAC-SHA256 算法对密码进行加盐哈希（标准库实现，零外部 C 依赖）：
- 随机生成 16 字节（32 位十六进制）Salt 盐值；
- 100,000 次哈希迭代（平衡安全性与瞬时登录性能，抗彩虹表与 GPU 离线暴力破解）；
- 密文落盘格式：`pbkdf2:sha256:100000$<salt_hex>$<hash_hex>`。

兼容性设计：
- 识别旧版明文密码（非 pbkdf2 开头）：若用户历史 config.yaml 为明文，以恒定时间安全兼容校验；
- 密码更新、首次保存或终端命令行重置时，一律转为安全密文落盘；
- 终端重置支持快捷重设与清除。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
import secrets
from typing import Any

import yaml

PBKDF2_PREFIX = "pbkdf2:sha256:100000$"
DEFAULT_ITERATIONS = 100000


def hash_password(password: str) -> str:
    """对明文密码进行 PBKDF2-HMAC-SHA256 加盐哈希加密，返回标准密文字符串。

    空字符串原样返回（表示未设置密码，免密模式）。已经是密文格式的直接返回，防止二次哈希。
    """
    if not password:
        return ""
    if is_password_hashed(password):
        return password
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        DEFAULT_ITERATIONS,
    ).hex()
    return f"{PBKDF2_PREFIX}{salt}${digest}"


def is_password_hashed(value: str) -> bool:
    """判断字符串是否为 PBKDF2 标准密文存储格式。"""
    if not value or not isinstance(value, str):
        return False
    return value.startswith("pbkdf2:sha256:")


def verify_password(password: str, stored_hash: str) -> bool:
    """安全校验输入的明文密码是否与已存储的密码凭证匹配（恒定时间比较防时序攻击）。

    支持：
    - PBKDF2-HMAC-SHA256 密文比对；
    - 兼容历史配置中未迁移的明文密码。
    """
    if not stored_hash:
        return True  # 未设置密码时视为免密
    stored = str(stored_hash).strip()
    if is_password_hashed(stored):
        try:
            # 格式：pbkdf2:sha256:<iterations>$<salt>$<hash>
            rest = stored[len("pbkdf2:sha256:"):]
            parts = rest.split("$")
            if len(parts) == 3:
                iterations = int(parts[0])
                salt = parts[1]
                expected_hash = parts[2]
            elif len(parts) == 2:
                iterations = DEFAULT_ITERATIONS
                salt = parts[0]
                expected_hash = parts[1]
            else:
                return False
            actual_hash = hashlib.pbkdf2_hmac(
                "sha256",
                str(password or "").encode("utf-8"),
                salt.encode("ascii"),
                iterations,
            ).hex()
            return hmac.compare_digest(actual_hash, expected_hash)
        except Exception:
            return False

    # 向后兼容：旧版历史配置中的明文密码（恒定时间比较）
    return hmac.compare_digest(str(password or ""), stored)


def update_config_auth_password(cfg_path: Path, new_password: str) -> str:
    """将新密码（明文或清空意图）加密并安全写回 config.yaml，尽量保留注释与原有格式。

    返回写入配置文件的密文字符串（清空时返回 ""）。
    """
    hashed = hash_password(new_password.strip()) if new_password and new_password.strip() else ""
    val_repr = f'"{hashed}"' if hashed else '""'

    text = cfg_path.read_text(encoding="utf-8")
    pattern = r"(^[ \t]*auth_password:\s*)(?:\"[^\"]*\"|'[^']*'|[^#\r\n]*)([ \t]*#.*)?$"

    def _repl(m: re.Match) -> str:
        comment = m.group(2) or ""
        return f"{m.group(1)}{val_repr}{comment}"

    if re.search(pattern, text, flags=re.MULTILINE):
        # 命中已有键：精确替换值，保护行尾注释与其余缩进格式
        new_text = re.sub(pattern, _repl, text, count=1, flags=re.MULTILINE)
    elif re.search(r"^web:\s*$", text, flags=re.MULTILINE):
        # web: 节存在但未列出 auth_password：在 web: 首行后插入
        new_text = re.sub(r"(^web:\s*$)", rf"\1\n  auth_password: {val_repr}", text, count=1, flags=re.MULTILINE)
    else:
        # 兜底回退：yaml 字典修改后 dump
        data: dict[str, Any] = yaml.safe_load(text) or {}
        if not isinstance(data.get("web"), dict):
            data["web"] = {}
        data["web"]["auth_password"] = hashed
        new_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    cfg_path.write_text(new_text, encoding="utf-8")
    return hashed


__all__ = [
    "PBKDF2_PREFIX",
    "DEFAULT_ITERATIONS",
    "hash_password",
    "is_password_hashed",
    "verify_password",
    "update_config_password",
]

