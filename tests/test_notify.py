"""任务结束提醒自检：完成/受阻触发提示音+系统通知，cancelled 不打扰，开关独立生效。

背景：任务结束提醒是旁路功能（notify.py），挂在 executor 收尾——
①done/blocked 触发托盘气泡 + Windows 系统音；②cancelled（用户在看板手动停止）
不提醒；③notify.sound/system 开关各自独立；④托盘未启动（--no-tray）时系统
通知静默跳过不报错、提示音照常；⑤气泡发送抛异常只吞掉不影响调用方；
⑥notify 节解析：缺省全开、显式关闭生效。
全部用替身对象，不真弹气泡、不真发声。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台打不出 ✅ 等字符
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy import notify as notify_mod
from orcsy import tray as tray_mod
from orcsy.config import NotifyConfig, load_config


class FakeIcon:
    """替身托盘图标：记录 notify 调用，可选主动抛异常模拟气泡失败。"""

    def __init__(self, raise_on_notify: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.raise_on_notify = raise_on_notify

    def notify(self, message, title):
        if self.raise_on_notify:
            raise RuntimeError("气泡炸了")
        self.calls.append((title, message))


class FakeWinsound:
    """替身 winsound：只记录 PlaySound 的目标（文件路径或系统音别名），不发声。"""

    SND_ALIAS = 0x10000
    SND_ASYNC = 0x0001
    SND_FILENAME = 0x20000

    def __init__(self):
        self.played: list[str] = []

    def PlaySound(self, target, flags):
        self.played.append(target)


def _fake_env(fake_icon):
    """装上替身（托盘 holder + winsound），返回还原函数。"""
    orig_holder = dict(tray_mod._ICON_HOLDER)
    orig_ws = notify_mod.winsound
    fake_ws = FakeWinsound()
    if fake_icon is not None:
        tray_mod._ICON_HOLDER["icon"] = fake_icon
    else:
        tray_mod._ICON_HOLDER.pop("icon", None)
    notify_mod.winsound = fake_ws
    notify_mod.configure(NotifyConfig(sound=True, system=True))

    def restore():
        tray_mod._ICON_HOLDER.clear()
        tray_mod._ICON_HOLDER.update(orig_holder)
        notify_mod.winsound = orig_ws
        notify_mod.configure(NotifyConfig(sound=True, system=True))

    return fake_ws, restore


def _expected_builtin(outcome: str) -> str:
    """内置音的绝对路径字符串（源码运行时直接存在）。"""
    return str(notify_mod.SOUNDS_DIR / ("ding-dong.wav" if outcome == "done"
                                        else "blocked.wav"))


def check_done_blocked_notify() -> None:
    """done/blocked 触发气泡+内置提示音；cancelled 什么都不发。"""
    icon = FakeIcon()
    ws, restore = _fake_env(icon)
    try:
        notify_mod.notify_task_end("demo", "T-1", "登录页改造", "done")
        assert icon.calls == [("✅ 任务完成", "【demo】登录页改造")], icon.calls
        assert ws.played == [_expected_builtin("done")], ws.played

        notify_mod.notify_task_end("demo", "T-2", "支付接口联调", "blocked")
        assert icon.calls[-1] == ("⚠️ 任务受阻", "【demo】支付接口联调"), icon.calls
        assert ws.played == [_expected_builtin("done"), _expected_builtin("blocked")], ws.played

        # cancelled（用户手动停止）不提醒——刚操作完不需要打扰
        notify_mod.notify_task_end("demo", "T-3", "手动停", "cancelled")
        assert len(icon.calls) == 2 and len(ws.played) == 2
    finally:
        restore()
    print("ok: done/blocked 提醒、cancelled 静默")


def check_builtin_sound_fallback() -> None:
    """内置音频文件缺失时回退 Windows 系统音（老版本包/裁剪场景不哑火）。"""
    from unittest.mock import patch

    icon = FakeIcon()
    ws, restore = _fake_env(icon)
    try:
        with patch.object(notify_mod, "SOUNDS_DIR", Path(tempfile.gettempdir()) / "no-such-dir"):
            notify_mod.notify_task_end("demo", "T-1", "无内置音", "done")
        assert ws.played == ["SystemAsterisk"], ws.played
    finally:
        restore()
    print("ok: 内置音缺失回退系统音")


def check_flags_independent() -> None:
    """sound/system 开关各自独立：关掉一个只停一个渠道。"""
    icon = FakeIcon()
    ws, restore = _fake_env(icon)
    try:
        notify_mod.configure(NotifyConfig(sound=False, system=True))
        notify_mod.notify_task_end("demo", "T-1", "只气泡", "done")
        assert len(icon.calls) == 1 and ws.played == []

        notify_mod.configure(NotifyConfig(sound=True, system=False))
        notify_mod.notify_task_end("demo", "T-2", "只声音", "done")
        assert len(icon.calls) == 1 and ws.played == [_expected_builtin("done")]
    finally:
        restore()
    print("ok: sound/system 开关独立生效")


def check_no_tray_icon() -> None:
    """托盘未启动（--no-tray）：系统通知静默跳过，提示音照常，不报错。"""
    ws, restore = _fake_env(None)
    try:
        notify_mod.notify_task_end("demo", "T-1", "无托盘", "done")
        assert ws.played == [_expected_builtin("done")], ws.played
    finally:
        restore()
    print("ok: 无托盘不报错，提示音照常")


def check_bubble_failure_swallowed() -> None:
    """气泡发送抛异常只吞掉：不影响调用方，提示音仍执行。"""
    icon = FakeIcon(raise_on_notify=True)
    ws, restore = _fake_env(icon)
    try:
        notify_mod.notify_task_end("demo", "T-1", "气泡失败", "done")
        assert ws.played == [_expected_builtin("done")], ws.played
    finally:
        restore()
    print("ok: 气泡失败被吞掉，旁路语义成立")


def check_notify_config_parse() -> None:
    """notify 节解析：缺省全开；显式关闭生效；非法形态报错。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.yaml"
        base = "llm: {provider: openai, api_key: sk-x, model: m}\n"

        p.write_text(base, encoding="utf-8")  # 缺 notify 节 = 全开（零配置即生效）
        cfg = load_config(p)
        assert cfg.notify == NotifyConfig(sound=True, system=True), cfg.notify

        p.write_text(base + "notify: {sound: false, system: true}\n", encoding="utf-8")
        cfg = load_config(p)
        assert cfg.notify == NotifyConfig(sound=False, system=True), cfg.notify

        p.write_text(base + "notify: 只写了个字符串\n", encoding="utf-8")
        try:
            load_config(p)
        except ValueError as e:
            assert "notify" in str(e), e
        else:
            raise AssertionError("notify 非字典应报 ValueError")
    print("ok: notify 节解析（缺省全开/显式关闭/非法报错）")


def main() -> int:
    check_done_blocked_notify()
    check_flags_independent()
    check_no_tray_icon()
    check_bubble_failure_swallowed()
    check_builtin_sound_fallback()
    check_notify_config_parse()
    print("\n任务结束提醒自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
