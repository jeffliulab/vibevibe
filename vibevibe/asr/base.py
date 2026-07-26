"""ASR 后端的统一接口。

三个后端(Qwen3-ASR ONNX / faster-whisper / Qwen3-ASR transformers)
都实现这个接口,所以"换模型"只是改配置文件里的一行,代码不用动。

这也是开发阶段不必等跑分结果的原因——接口先定死,三个实现都写好,
跑分只决定配置里填哪个名字。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Timing:
    """一次转写的耗时分解,给 bench 用。"""

    audio_duration_s: float = 0.0
    total_s: float = 0.0
    load_s: float = 0.0
    mel_s: float = 0.0
    encoder_s: float = 0.0
    prefill_s: float = 0.0
    decode_s: float = 0.0
    tokens_generated: int = 0
    sub_chunks: int = 1

    @property
    def rtf(self) -> float:
        """Real-Time Factor:处理耗时 / 音频时长。小于 1 表示比实时快。"""
        if self.audio_duration_s <= 0:
            return float("nan")
        return self.total_s / self.audio_duration_s


@dataclass
class TranscriptResult:
    text: str
    language: str = ""
    raw_output: str = ""
    timing: Timing = field(default_factory=Timing)
    # 闸门是否触发过(触发了但已被截断挽救的情况)
    guard_note: str = ""


class AsrBackend(ABC):
    """所有 ASR 后端的基类。

    约定:
      - 输入是内存里的 float32 单声道 PCM(取值 -1..1),采样率见 sample_rate。
        **不接受文件路径**——守护进程每次说话都落盘一次是没必要的开销。
      - load() 可以慢(要读几个 G 的权重),但只应发生一次。
      - transcribe() 必须是线程安全的调用方保证:同一时刻只有一个调用。
    """

    sample_rate: int = 16000

    @abstractmethod
    def load(self) -> None:
        """把模型载入内存。重复调用应当是空操作。"""

    @abstractmethod
    def transcribe(self, pcm: np.ndarray) -> TranscriptResult:
        """转写一段 PCM。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """后端标识,写进日志和 bench 结果里。"""

    def unload(self) -> None:
        """释放模型(可选实现)。"""
        return None
