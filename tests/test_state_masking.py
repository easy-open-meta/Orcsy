"""H2 脱敏自检：全量状态不含明文密钥；项目保存三态不误删已存密钥。

安全审计 2026-11-04 H2：/api/state 曾明文回显 llm.api_key 与 update.token
（项目级 llm.api_key / repo.token / repo.password 同样裸奔）。现在只回
掩码 + 「已配置」标记；写侧用三态（键缺失=保持 / 空串=清除 / 非空=替换）
保证「改别的设置保存一次」不会把已配好的密钥抹掉。

运行：uv run python tests/test_state_masking.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import (AppConfig, LLMConfig, ProjectConfig, UpdateConfig,
                          WebConfig)
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, mask_secret

GLOBAL_KEY = "sk-global-1234567890"
UPD_TOKEN = "upd-token-1234567890"
PROJ_KEY = "sk-proj-abcdefghijkl"
PROJ_TOKEN = "proj-token-abcdefgh"
PROJ_PASS = "proj-pass-abcdefgh"
REPO_URL = "https://github.com/o/r"


def check_full_state_masked() -> AppState:
    tmp = Path(tempfile.mkdtemp(prefix="masking-"))
    root = tmp / "proj"
    root.mkdir()
    cfg_file = tmp / "config.yaml"
    cfg_file.write_text("llm: {}\n", encoding="utf-8")
    cfg = AppConfig(
        llm=LLMConfig(api_key=GLOBAL_KEY, base_url="http://localhost:9", model="m"),
        update=UpdateConfig(auto_check=False, token=UPD_TOKEN),
        web=WebConfig(host="127.0.0.1", auth_password=""),
        projects=[ProjectConfig(
            name="p1", path=root,
            llm={"api_key": PROJ_KEY, "model": "m"},
            repo={"url": REPO_URL, "provider": "github", "token": PROJ_TOKEN,
                  "username": "u", "password": PROJ_PASS},
        )],
        source_path=cfg_file,
    )
    state = AppState(cfg, Scheduler(cfg), log_path=str(tmp / "orcsy.log"))
    state._tmp = tmp  # type: ignore[attr-defined]  收尾清理用

    s = state.full_state()
    blob = json.dumps(s, ensure_ascii=False)
    for secret in (GLOBAL_KEY, UPD_TOKEN, PROJ_KEY, PROJ_TOKEN, PROJ_PASS):
        assert secret not in blob, f"明文密钥泄漏到 /api/state：{secret}"
    assert "api_key" not in s["llm"], s["llm"]
    assert s["llm"]["key_configured"] is True
    assert s["llm"]["api_key_masked"] == mask_secret(GLOBAL_KEY), s["llm"]
    assert "token" not in s["update_cfg"], s["update_cfg"]
    assert s["update_cfg"]["token_configured"] is True
    p = s["projects"][0]
    assert "api_key" not in (p["llm"] or {}), p["llm"]
    assert p["llm"]["api_key_configured"] is True
    assert "token" not in (p["repo"] or {}), p["repo"]
    assert p["repo"]["token_configured"] is True
    assert "password" not in (p["repo"] or {}), p["repo"]
    assert p["repo"]["password_configured"] is True

    # 更新状态机里的下载地址带 access_token（私有仓库）同样不得外发；
    # 内部原值必须保留（在线更新要用带令牌的地址下载）
    token_url = ("https://gitee.com/o/r/releases/download/V1/x.zip"
                 f"?access_token={UPD_TOKEN}")
    state._set_update_state({"phase": "available", "download_url": token_url,
                             "sha256_url": token_url + ".sha256",
                             "sha256_value": "a" * 64})
    upd_blob = json.dumps(state.full_state()["update"], ensure_ascii=False)
    assert "access_token" not in upd_blob and UPD_TOKEN not in upd_blob, upd_blob
    assert "access_token=" in state.update_state["download_url"], "内部地址应保留令牌"
    print("ok: full_state 无明文密钥（全局/更新源/项目/下载地址），掩码与标记齐全")
    return state


def check_mask_secret() -> None:
    assert mask_secret("") == ""
    assert mask_secret("   ") == ""
    assert mask_secret("short") == "••••••••"
    assert mask_secret("abcdefghijkl") == "••••••••"  # 12 位仍全打码
    assert mask_secret("abcdefghijklm") == "abcd…jklm"  # 13 位起保留首尾各 4 位
    assert mask_secret("sk-1234567890abcd") == "sk-1…abcd"
    print("ok: mask_secret（空/短值全打码/长值保留首尾各 4 位）")


def check_project_three_state(state: AppState) -> None:
    tmp = state._tmp  # type: ignore[attr-defined]
    root = tmp / "proj"
    try:
        # 键缺失 = 保持原值（看板只拿到掩码，未输入就不带键）
        # （username 由看板正常回传；密码必须与账号成对才保留，同 normalize_repo）
        keep = state._project_payload(
            {"name": "p1", "path": str(root),
             "llm": {"model": "m"}, "repo": {"url": REPO_URL, "username": "u"}},
            original_name="p1")
        assert keep.llm["api_key"] == PROJ_KEY, keep.llm
        assert keep.repo["token"] == PROJ_TOKEN, keep.repo
        assert keep.repo["password"] == PROJ_PASS, keep.repo

        # 空串 = 显式清除
        clear = state._project_payload(
            {"name": "p1", "path": str(root),
             "llm": {"model": "m", "api_key": ""},
             "repo": {"url": REPO_URL, "token": "", "username": "u", "password": ""}},
            original_name="p1")
        assert "api_key" not in (clear.llm or {}), clear.llm
        assert clear.repo["token"] == "", clear.repo
        assert clear.repo["password"] == "", clear.repo

        # 非空 = 替换
        new = state._project_payload(
            {"name": "p1", "path": str(root),
             "repo": {"url": REPO_URL, "username": "u",
                      "token": "new-token-123456"}},
            original_name="p1")
        assert new.repo["token"] == "new-token-123456", new.repo
        assert new.repo["password"] == PROJ_PASS, "未触碰的密码应保持"

        # 新建项目（无原值可继承）：缺密钥键不报错、不产生空密钥
        fresh = state._project_payload(
            {"name": "p2", "path": str(root), "repo": {"url": REPO_URL}})
        assert fresh.repo["token"] == "", fresh.repo
        print("ok: 项目 llm/repo 三态（保持/清除/替换/新建）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_mask_secret()
    state = check_full_state_masked()
    check_project_three_state(state)
    print("\nH2 脱敏与三态全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
