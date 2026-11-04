"""任务结束提醒：完成/受阻时播提示音 + 弹系统通知（托盘气泡）。

旁路设计（同用量台账）：提醒失败绝不影响任务执行，所有异常都吞掉。
- 系统通知 → 托盘图标气泡（tray.notify_user 跨线程安全；--no-tray/未启动时
  静默跳过）；cancelled（用户在看板手动停止）不提醒——刚操作完不需要提醒。
- 提示音   → winsound 播 Windows 系统音（零资源文件；SND_ASYNC 异步播放，
  不阻塞调度线程）。非 Windows 无 winsound，渠道自动缺席。

开关在 config.yaml 的 notify 节（sound/system，默认全开），由 web 层随
配置加载/保存注入 configure()，看板改完立即生效。
"""

from __future__ import annotations

from .logging_setup import get_logger

log = get_logger("notify")

try:
    import winsound
except ImportError:  # 非 Windows：无提示音渠道，系统通知仍可用
    winsound = None  # type: ignore[assignment]

# 运行时开关（notify 节），由 web 层注入；注入前默认全开（零配置即生效）
_FLAGS: dict[str, bool] = {"sound": True, "system": True}


def configure(cfg) -> None:
    """注入 notify 配置（AppConfig.notify），看板保存后随 _apply_config 即时生效。"""
    _FLAGS["sound"] = bool(getattr(cfg, "sound", True))
    _FLAGS["system"] = bool(getattr(cfg, "system", True))


# 结束状态 → (通知标题, 系统音别名)。cancelled 不在表内 = 不提醒。
# 系统音走用户 Windows 声音方案：完成用「星号」叮一声，受阻用「感叹号」警示。
_ENDINGS: dict[str, tuple[str, str]] = {
    "done": ("✅ 任务完成", "SystemAsterisk"),
    "blocked": ("⚠️ 任务受阻", "SystemExclamation"),
}


def _play_sound(alias: str) -> None:
    if winsound is None:
        return
    winsound.PlaySound(alias, winsound.SND_ALIAS | winsound.SND_ASYNC)


def notify_task_end(project: str, task_id: str, title: str, outcome: str) -> None:
    """任务结束提醒入口（executor 收尾时调用）。任何异常都吞掉。"""
    try:
        ending = _ENDINGS.get(outcome)
        if ending is None:
            return
        head, sound_alias = ending
        message = f"【{project}】{title}"
        if _FLAGS["system"]:
            from . import tray
            tray.notify_user(message, head)
        if _FLAGS["sound"]:
            _play_sound(sound_alias)
        log.info("已发送任务提醒：%s %s（%s）", head, message, task_id)
    except Exception:  # noqa: BLE001 — 提醒是旁路，任何异常都吞掉
        log.warning("任务提醒发送失败（可忽略）", exc_info=True)
