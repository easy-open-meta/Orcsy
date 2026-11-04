"""合成 Orcsy 内置提示音（纯标准库，可重复生成）。

产物（写入 orcsy/templates/sounds/，随 orcsy.spec 的 templates 目录打包）：
- ding-dong.wav   完成提醒：「叮-咚」上扬双音（高→低，清亮短促）
- blocked.wav     受阻提醒：低沉双音（同构但音高低、更长，听感偏警示）

音色设计：正弦基频 + 12% 二次谐波，指数衰减包络，音间 60ms 间隙；
22050 Hz / 16 bit / 单声道。每个音约 0.28s，整段不到 1s。
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SR = 22050  # 采样率
HARMONIC = 0.12  # 二次谐波占比（给纯音一点「木质感」）
GAP_S = 0.06  # 音与音之间的静音
NOTE_S = 0.28  # 单音时长
TAIL_S = 0.10  # 末尾收音余量


def _tone(freq: float, seconds: float) -> list[float]:
    """一个带指数衰减包络的单音采样。"""
    n = int(SR * seconds)
    samples: list[float] = []
    for i in range(n):
        t = i / SR
        # 衰减快起慢收：前 5ms 线性起振防爆音，之后指数衰减
        attack = min(1.0, t / 0.005)
        env = attack * math.exp(-t / 0.09)
        v = math.sin(2 * math.pi * freq * t) \
            + HARMONIC * math.sin(2 * math.pi * freq * 2 * t)
        samples.append(v * env)
    return samples


def _render(notes: list[tuple[float, float]], out: Path) -> None:
    """按 (频率, 时长) 序列渲染并写 16bit 单声道 wav。"""
    pcm: list[float] = []
    for i, (freq, seconds) in enumerate(notes):
        if i:
            pcm.extend([0.0] * int(SR * GAP_S))
        pcm.extend(_tone(freq, seconds))
    pcm.extend([0.0] * int(SR * TAIL_S))
    peak = max(abs(v) for v in pcm) or 1.0
    scale = 0.75 / peak  # 留 25% 余量，避免削波
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = b"".join(
            struct.pack("<h", int(v * scale * 32767)) for v in pcm)
        w.writeframes(frames)
    print(f"已生成 {out}（{out.stat().st_size // 1024} KB）")


def main() -> None:
    base = Path(__file__).resolve().parents[1] / "orcsy" / "templates" / "sounds"
    # 完成：叮（高）→ 咚（低），经典的门铃上扬感
    _render([(1174.7, NOTE_S), (784.0, NOTE_S)], base / "ding-dong.wav")
    # 受阻：同构下行、音高低一截、拖长些——听得出来「没成」而不是「成了」
    _render([(523.3, 0.34), (349.2, 0.40)], base / "blocked.wav")


if __name__ == "__main__":
    main()
