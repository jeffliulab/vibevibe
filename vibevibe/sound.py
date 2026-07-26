"""提示音。

为什么用声音而不是桌面通知:听写的时候眼睛盯着输入框,不会去看屏幕顶部。
声音不需要你移开视线,也不会遮住任何东西——对讲机就是这么做的。

三个音各自负责一件事:
    start  按下去了,正在录     —— 高一点,清脆
    done   出字了,已经粘好      —— 低一点,收尾感
    error  出问题了,一个字都没粘 —— 明显不同的低音,必须一听就知道不对劲

音是现算的正弦波,不依赖任何音频文件,也不用装 paplay/canberra 之类的东西
(sounddevice 本来就是录音要用的依赖)。所有参数都可配。
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from .config import SoundConfig
from .i18n import t

log = logging.getLogger("vibevibe.sound")

# 提示音的采样率跟录音无关,单独一档就行
SR = 44100
# 淡入淡出时长。不做淡入淡出的话,方波般的起止会有"啪"的爆音,很难听。
FADE_SEC = 0.008


def _tone(freqs: list[float], duration: float, volume: float) -> np.ndarray:
    """合成一小段提示音。freqs 给多个频率就是和弦(听起来更饱满不刺耳)。"""
    n = max(1, int(duration * SR))
    t = np.arange(n) / SR
    wave = np.zeros(n, dtype=np.float32)
    for f in freqs:
        wave += np.sin(2 * np.pi * f * t).astype(np.float32)
    wave /= max(len(freqs), 1)

    # 两端做淡入淡出,消掉爆音
    fade = min(int(FADE_SEC * SR), n // 2)
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        wave[:fade] *= ramp
        wave[-fade:] *= ramp[::-1]

    return (wave * volume).astype(np.float32)


class Player:
    """提示音播放器。

    播放全部在后台线程里做,绝不阻塞录音的开始/结束——
    按下去要立刻开始录,不能等音放完。
    """

    def __init__(self, cfg: SoundConfig) -> None:
        self.cfg = cfg
        self._cache: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()

    def _clip(self, kind: str) -> np.ndarray | None:
        spec = {
            "start": (self.cfg.start_freqs, self.cfg.start_sec),
            "done": (self.cfg.done_freqs, self.cfg.done_sec),
            "error": (self.cfg.error_freqs, self.cfg.error_sec),
        }.get(kind)
        if spec is None:
            return None
        freqs, dur = spec
        if not freqs or dur <= 0:
            return None

        with self._lock:
            if kind not in self._cache:
                self._cache[kind] = _tone(list(freqs), dur, self.cfg.volume)
            return self._cache[kind]

    def _play_blocking(self, clip: np.ndarray) -> None:
        import sounddevice as sd

        device = self.cfg.output_device.strip() or None
        if device is not None and device.isdigit():
            device = int(device)
        sd.play(clip, SR, device=device, blocking=True)

    def play(self, kind: str) -> None:
        """放一个提示音。失败绝不影响听写本身,最多记条日志。"""
        if not self.cfg.enabled:
            return
        clip = self._clip(kind)
        if clip is None:
            return

        def run() -> None:
            try:
                self._play_blocking(clip)
            except Exception as exc:
                # 放不出声是小事,不能让它连累转写
                log.debug(t("sound.play_failed"), kind, exc)

        threading.Thread(target=run, name=f"vibevibe-sound-{kind}", daemon=True).start()
