"""登录限速自检（安全审计 2026-11-04 L1）。

语义：同 IP 在 LOGIN_WINDOW 滑窗内失败达 LOGIN_MAX_FAILS 次 → 锁定
LOGIN_LOCKOUT 秒。锁定期独立于滑窗（不是「窗口一过就自动解锁」），
锁定期满重新计数；登录成功清空该 IP 的失败计数；不同 IP 互不影响。

运行：uv run python tests/test_login_throttle.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import AppConfig, LLMConfig, UpdateConfig, WebConfig
from orcsy.scheduler import Scheduler
from orcsy.web import AppState

PWD = "correct-password"


def make_state() -> AppState:
    cfg = AppConfig(
        llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
        update=UpdateConfig(auto_check=False),
        web=WebConfig(host="127.0.0.1", auth_password=PWD),
    )
    state = AppState(cfg, Scheduler(cfg), log_path=str(Path(tempfile.gettempdir()) / "t.log"))
    # 缩短节奏便于测试（类属性的实例覆盖）
    state.LOGIN_FAIL_DELAY = 0.01
    state.LOGIN_WINDOW = 0.5
    state.LOGIN_MAX_FAILS = 2
    state.LOGIN_LOCKOUT = 2.0
    return state


def check_lockout() -> None:
    state = make_state()
    try:
        assert state.login("wrong", "1.2.3.4")["ok"] is False
        assert state.login("wrong", "1.2.3.4")["ok"] is False
        # 达上限：即使密码正确也拒绝
        r = state.login(PWD, "1.2.3.4")
        assert r["ok"] is False and "尝试次数过多" in r["message"], r
        # 其它 IP 不受影响
        assert state.login(PWD, "5.6.7.8")["ok"] is True
        # 锁定期独立于滑窗：窗口（0.5s）已过，锁定（2s）仍在
        time.sleep(0.6)
        r = state.login(PWD, "1.2.3.4")
        assert r["ok"] is False, "锁定不应随失败滑窗过期自动解除"
        # 锁定期满：恢复
        time.sleep(1.5)
        assert state.login(PWD, "1.2.3.4")["ok"] is True, "锁定期满应允许登录"
        print("ok: 达阈值锁定 / 锁定期独立于滑窗 / 期满恢复 / 其它 IP 不受影响")
    finally:
        state._clear_login_throttle()


def check_success_resets() -> None:
    state = make_state()
    try:
        assert state.login("wrong", "9.9.9.9")["ok"] is False
        assert state.login(PWD, "9.9.9.9")["ok"] is True
        # 成功即清零：再失败一次不该立刻触发锁定（阈值 2）
        assert state.login("wrong", "9.9.9.9")["ok"] is False
        assert state.login(PWD, "9.9.9.9")["ok"] is True
        # 密码变更清空限速：改密后旧锁定不阻塞新密码
        assert state.login("wrong", "8.8.8.8")["ok"] is False
        assert state.login("wrong", "8.8.8.8")["ok"] is False
        assert state.login(PWD, "8.8.8.8")["ok"] is False
        state._invalidate_sessions()
        assert state.login(PWD, "8.8.8.8")["ok"] is True, "改密后应解除旧锁定"
        print("ok: 成功登录清零计数 / 改密清空限速状态")
    finally:
        state._clear_login_throttle()


def check_auth_disabled() -> None:
    cfg = AppConfig(
        llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
        update=UpdateConfig(auto_check=False),
        web=WebConfig(host="127.0.0.1", auth_password=""),
    )
    state = AppState(cfg, Scheduler(cfg))
    try:
        r = state.login("anything", "1.2.3.4")
        assert r == {"ok": True, "token": None}, r
        print("ok: 未启用鉴权时不限速（本机零打扰）")
    finally:
        state._clear_login_throttle()


def main() -> int:
    check_lockout()
    check_success_resets()
    check_auth_disabled()
    print("\n登录限速全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
