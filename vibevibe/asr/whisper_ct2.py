"""Whisper large-v3-turbo 后端(faster-whisper / CTranslate2),纯 CPU。

它在这个项目里的角色是**对照基线**,不是主力:

  - 中英混说不是它的强项(Whisper 的混说能力是"顺带的",
    Qwen3-ASR 是专门为此训练的)
  - 但它成熟、稳定、没有无限重复那类毛病,适合用来判断
    "Qwen3-ASR 到底强多少"

注:CTranslate2 在 Blackwell(sm_120)显卡上用 int8 会崩
(CUBLAS_STATUS_NOT_SUPPORTED),但这里走的是 CPU 路径,不受影响。
"""

from __future__ import annotations

import time

import numpy as np

from ..config import AsrConfig, WhisperCt2Config, _expand
from .base import AsrBackend, Timing, TranscriptResult

SAMPLE_RATE = 16000


class WhisperCt2Backend(AsrBackend):
    sample_rate = SAMPLE_RATE

    def __init__(self, asr_cfg: AsrConfig, backend_cfg: WhisperCt2Config) -> None:
        self.asr_cfg = asr_cfg
        self.cfg = backend_cfg
        self.model_dir = _expand(backend_cfg.model_dir)
        self._model = None
        self._load_s = 0.0

    @property
    def name(self) -> str:
        return f"whisper_ct2:{self.model_dir.name}:{self.cfg.compute_type}"

    def load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        if not self.model_dir.is_dir():
            raise FileNotFoundError(
                f"模型目录不存在: {self.model_dir}\n请先下载权重。"
            )

        t0 = time.time()
        self._model = WhisperModel(
            str(self.model_dir),
            device="cpu",  # 明确只走 CPU
            compute_type=self.cfg.compute_type,
            cpu_threads=self.cfg.cpu_threads,
            num_workers=self.cfg.num_workers,
        )
        self._load_s = time.time() - t0

    def transcribe(self, pcm: np.ndarray) -> TranscriptResult:
        if self._model is None:
            self.load()

        wav = np.asarray(pcm, dtype=np.float32).reshape(-1)
        timing = Timing(
            audio_duration_s=len(wav) / SAMPLE_RATE,
            load_s=self._load_s,
        )
        t_start = time.time()

        segments, info = self._model.transcribe(
            wav,
            beam_size=self.cfg.beam_size,
            # 留空 = 自动检测语言。中英混说必须走自动。
            language=self.asr_cfg.language or None,
            vad_filter=self.cfg.vad_filter,
        )
        # segments 是惰性生成器,这一行才真正开始算
        parts = [seg.text for seg in segments]

        timing.total_s = time.time() - t_start
        timing.sub_chunks = len(parts) or 1
        text = "".join(parts).strip()

        return TranscriptResult(
            text=text,
            language=getattr(info, "language", "") or "",
            raw_output=text,
            timing=timing,
        )

    def unload(self) -> None:
        self._model = None
