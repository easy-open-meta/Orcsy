"""任务结束提醒：完成/受阻时播提示音 + 弹系统通知（托盘气泡）。

旁路设计（同用量台账）：提醒失败绝不影响任务执行，所有异常都吞掉。
- 系统通知 → 托盘图标气泡（tray.notify_user 跨线程安全；--no-tray/未启动时
  静默跳过）；cancelled（用户在看板手动停止）不提醒——刚操作完不需要提醒。
- 提示音   → winsound 播内置「叮咚」音（orcsy/templates/sounds/ 下的 wav，
  随程序分发、不依赖用户的声音方案设置；SND_ASYNC 异步播放不阻塞调度线程；
  音频缺失时回退 Windows 系统音）。非 Windows 无 winsound，渠道自动缺席。

开关在 config.yaml 的 notify 节（sound/system，默认全开），由 web 层随
配置加载/保存注入 configure()，看板改完立即生效。
"""

from __future__ import annotations

from pathlib import Path

from .logging_setup import get_logger
from .templates import TEMPLATES_DIR

log = get_logger("notify")

try:
    import winsound
except ImportError:  # 非 Windows：无提示音渠道，系统通知仍可用
    winsound = None  # type: ignore[assignment]

# 运行时开关（notify 节），由 web 层注入；注入前默认全开（零配置即生效）
_FLAGS: dict[str, bool] = {"sound": True, "system": True}

# 内置提示音目录：源码运行在包内 templates/sounds/；冻结后 TEMPLATES_DIR
# 已由入口脚本指向 _MEIPASS，音频随 orcsy.spec 的 templates 数据一并打包
SOUNDS_DIR = TEMPLATES_DIR / "sounds"


def configure(cfg) -> None:
    """注入 notify 配置（AppConfig.notify），看板保存后随 _apply_config 即时生效。"""
    _FLAGS["sound"] = bool(getattr(cfg, "sound", True))
    _FLAGS["system"] = bool(getattr(cfg, "system", True))


# 结束状态 → (通知标题, 内置音频名, 系统音别名)。cancelled 不在表内 = 不提醒。
# 内置音在 templates/sounds/（随程序打包分发，不随用户系统声音方案变化）；
# 音频文件缺失（老版本包/被裁剪）时回退系统音——完成「星号」、受阻「感叹号」。
_ENDINGS: dict[str, tuple[str, str, str]] = {
    "done": ("✅ 任务完成", "ding-dong.wav", "SystemAsterisk"),
    "blocked": ("⚠️ 任务受阻", "blocked.wav", "SystemExclamation"),
}


def _play_sound(sound_file: str, fallback_alias: str) -> None:
    if winsound is None:
        return
    path = SOUNDS_DIR / sound_file
    if path.is_file():
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
    else:
        log.debug("内置提示音缺失：%s，回退系统音", path)
        winsound.PlaySound(fallback_alias, winsound.SND_ALIAS | winsound.SND_ASYNC)


def notify_task_end(project: str, task_id: str, title: str, outcome: str) -> None:
    """任务结束提醒入口（executor 收尾时调用）。任何异常都吞掉。"""
    try:
        ending = _ENDINGS.get(outcome)
        if ending is None:
            return
        head, sound_file, sound_alias = ending
        message = f"【{project}】{title}"
        if _FLAGS["system"]:
            from . import tray
            tray.notify_user(message, head)
        if _FLAGS["sound"]:
            _play_sound(sound_file, sound_alias)
        log.info("已发送任务提醒：%s %s（%s）", head, message, task_id)
    except Exception:  # noqa: BLE001 — 提醒是旁路，任何异常都吞掉
        log.warning("任务提醒发送失败（可忽略）", exc_info=True)


__all__ = [
    "SOUNDS_DIR",
    "configure",
    "notify_task_end",
]

