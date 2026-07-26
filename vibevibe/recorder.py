"""录音。

用 sounddevice(PortAudio)开一个输入流,把 float32 单声道 PCM 攒在内存里。
系统跑的是 PipeWire,PortAudio 走它的 ALSA 兼容层,不需要额外配置。

设计要点:
  - 录音在后台线程里持续进,主线程随时可以 stop() 拿到结果,
    这样 hold 模式松手的一瞬间就能停,不用等一个大 block 收完
  - 有硬上限(max_record_sec),防止忘记停止把内存吃光
  - 太短的录音直接丢弃,当作误触
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import numpy as np

from .config import AudioConfig
from .i18n import t

log = logging.getLogger(__name__)


class RecorderError(Exception):
    pass


def _resolve_device(spec: str):
    """把配置里的设备写法解析成 sounddevice 能用的值。

    支持三种写法:
      ""      → 系统默认输入设备
      "3"     → 设备索引
      "C920"  → 设备名的子串,大小写不敏感(取第一个匹配到的输入设备)
    """
    import sounddevice as sd

    spec = (spec or "").strip()
    if not spec:
        return None
    if spec.isdigit():
        return int(spec)

    needle = spec.lower()
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and needle in dev["name"].lower():
            return idx
    available = [
        f"[{i}] {d['name']}"
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]
    raise RecorderError(
        f"找不到名字里含 {spec!r} 的输入设备。当前可用的输入设备:\n  "
        + "\n  ".join(available)
    )


class Recorder:
    """一次只录一段。start() / stop() 配对使用。

    on_block 是可选的实时回调,每收到一块音频就调一次,用来画波形/电平表。
    它在 PortAudio 的音频线程里执行,所以**必须又快又不阻塞**——
    里面只能做塞进队列这种事,绝不能画图或读写文件,否则会爆音。
    """

    def __init__(self, cfg: AudioConfig,
                 on_block: Callable[[np.ndarray], None] | None = None) -> None:
        self.cfg = cfg
        self.on_block = on_block
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._frames = 0
        self._max_frames = int(cfg.max_record_sec * cfg.sample_rate)
        self._overflowed = False

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def duration_sec(self) -> float:
        return self._frames / self.cfg.sample_rate

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            log.debug(t("recorder.stream_status"), status)
        with self._lock:
            if self._frames >= self._max_frames:
                self._overflowed = True
                return
            block = indata.copy()
            self._chunks.append(block)
            self._frames += frames
        if self.on_block is not None:
            try:
                self.on_block(block)
            except Exception:
                # 可视化出问题绝不能连累录音本身
                log.debug("on_block 回调出错", exc_info=True)

    def start(self) -> None:
        if self._stream is not None:
            raise RecorderError(t("recorder.already"))
        import sounddevice as sd

        with self._lock:
            self._chunks = []
            self._frames = 0
            self._overflowed = False

        blocksize = max(1, int(self.cfg.block_sec * self.cfg.sample_rate))
        try:
            self._stream = sd.InputStream(
                samplerate=self.cfg.sample_rate,
                channels=self.cfg.channels,
                dtype="float32",
                blocksize=blocksize,
                device=_resolve_device(self.cfg.input_device),
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # PortAudio 的报错通常很难懂,包一层
            self._stream = None
            raise RecorderError(t("recorder.open_failed") % exc) from exc

    def stop(self) -> np.ndarray:
        """停止录音并返回 float32 单声道 PCM。太短则返回空数组。"""
        if self._stream is None:
            raise RecorderError(t("recorder.not_recording"))
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None

        with self._lock:
            chunks = self._chunks
            self._chunks = []

        if not chunks:
            return np.zeros(0, dtype=np.float32)

        pcm = np.concatenate(chunks, axis=0)
        if pcm.ndim > 1:  # 多声道取平均降成单声道
            pcm = pcm.mean(axis=1)
        pcm = pcm.astype(np.float32, copy=False)

        if self._overflowed:
            log.warning(t("recorder.overflow"), self.cfg.max_record_sec)

        if len(pcm) < self.cfg.min_record_sec * self.cfg.sample_rate:
            log.info(t("recorder.too_short"), len(pcm) / self.cfg.sample_rate)
            return np.zeros(0, dtype=np.float32)

        return pcm

    def abort(self) -> None:
        """放弃当前录音,不返回数据。"""
        if self._stream is None:
            return
        try:
            self._stream.abort()
            self._stream.close()
        finally:
            self._stream = None
            with self._lock:
                self._chunks = []
                self._frames = 0


def trim_silence(
    pcm: np.ndarray,
    sample_rate: int,
    threshold_db: float = -30.0,
    margin_sec: float = 0.15,
    frame_sec: float = 0.02,
    noise_percentile: float = 20.0,
) -> np.ndarray:
    """砍掉首尾的静音,两端各留一点余量。

    为什么要做:录的时候难免在开口前和说完后多留一截。这些静音
    会让 RTF(处理耗时 / 音频时长)算出来偏小——音频变长了但
    实际内容没变多,看着像是"更快",其实是把水分算进去了。
    跑分要的是干净数字,所以存盘前先修掉。

    只砍首尾,句子中间的停顿一概保留——那是说话的一部分。

    阈值怎么定(这里踩过坑):一开始只按"相对整段最响处"取相对值,
    结果录音一旦削顶,最响处恒等于 1.0,阈值就被钉死在一个很低的数上,
    而 C920 这种远场麦的底噪高于它,于是一秒都裁不掉。
    现在改成**同时**参考底噪水平(取较安静那 20% 帧的中位数)和峰值,
    两者取较大的那个当阈值,噪声大的麦也能正常工作。
    """
    if len(pcm) == 0:
        return pcm

    frame = max(1, int(frame_sec * sample_rate))
    n_frames = len(pcm) // frame
    if n_frames < 2:
        return pcm

    frames = pcm[:n_frames * frame].reshape(n_frames, frame)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    peak = rms.max()
    if peak <= 0:
        return pcm

    # 相对峰值的阈值:整体音量高低不影响它
    rel_threshold = peak * (10.0 ** (threshold_db / 20.0))
    # 相对底噪的阈值:底噪高的麦(比如远场摄像头麦)靠它才裁得动
    noise_floor = float(np.percentile(rms, noise_percentile))
    noise_threshold = noise_floor * 2.5

    threshold = max(rel_threshold, noise_threshold)
    loud = np.where(rms > threshold)[0]
    if len(loud) == 0:
        return pcm

    margin = int(margin_sec * sample_rate)
    start = max(0, loud[0] * frame - margin)
    end = min(len(pcm), (loud[-1] + 1) * frame + margin)
    return pcm[start:end]


def list_input_devices() -> list[str]:
    """列出所有输入设备,给 CLI 的 devices 命令用。"""
    import sounddevice as sd

    out = []
    default = sd.default.device[0] if sd.default.device else None
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] <= 0:
            continue
        mark = " (默认)" if idx == default else ""
        out.append(
            f"[{idx}] {dev['name']}{mark} "
            f"— {dev['max_input_channels']}ch @ {int(dev['default_samplerate'])}Hz"
        )
    return out
