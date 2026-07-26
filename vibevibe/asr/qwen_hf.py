"""Qwen3-ASR 官方 PyTorch 路径(transformers / 官方 qwen3-asr 包)。

**这个后端默认跑不起来,而且是故意的。**

它需要 PyTorch(约 2.5GB)和官方的 qwen3-asr 推理包,而本项目的
整条主线是"纯 CPU、不装 PyTorch、不碰 CUDA"。所以这里只做一件事:
在需要的时候给出清清楚楚的安装说明,然后**停下来等人确认**,
绝不自作主张去装几个 G 的依赖。

什么时候才会用到它:
  只有当 0.6B 的准确率不够、而 1.7B 的 ONNX 导出又跑不通时,
  拿它作为准确率上限的参考。它只用于 bench 对照,不进日常听写路径。
"""

from __future__ import annotations

import time

import numpy as np

from ..config import AsrConfig, QwenHfConfig, _expand
from .base import AsrBackend, Timing, TranscriptResult

SAMPLE_RATE = 16000

INSTALL_HINT = """\
这个后端需要额外的重量级依赖,默认没装:

    ./.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
    ./.venv/bin/pip install qwen3-asr

注意:torch 的 CPU 版约 2.5GB。装之前请先确认——
本项目主线不需要它,只有做 1.7B 准确率对照时才用得上。
"""


class QwenHfBackend(AsrBackend):
    sample_rate = SAMPLE_RATE

    def __init__(self, asr_cfg: AsrConfig, backend_cfg: QwenHfConfig) -> None:
        self.asr_cfg = asr_cfg
        self.cfg = backend_cfg
        self._model = None
        self._processor = None
        self._load_s = 0.0

    @property
    def name(self) -> str:
        target = self.cfg.model_dir or self.cfg.model_id
        return f"qwen_hf:{target}:{self.cfg.dtype}"

    def _check_deps(self) -> None:
        missing = []
        try:
            import torch  # noqa: F401
        except ImportError:
            missing.append("torch")
        try:
            import transformers  # noqa: F401
        except ImportError:
            missing.append("transformers")
        if missing:
            raise ImportError(
                f"qwen_hf 后端缺少依赖: {', '.join(missing)}\n\n" + INSTALL_HINT
            )

    def load(self) -> None:
        if self._model is not None:
            return
        self._check_deps()

        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

        target = str(_expand(self.cfg.model_dir)) if self.cfg.model_dir else self.cfg.model_id
        dtype = getattr(torch, self.cfg.dtype)

        t0 = time.time()
        self._processor = AutoProcessor.from_pretrained(target)
        self._model = AutoModelForSpeechSeq2Seq.from_pretrained(
            target, torch_dtype=dtype
        ).to(self.cfg.device).eval()
        self._load_s = time.time() - t0

    def transcribe(self, pcm: np.ndarray) -> TranscriptResult:
        if self._model is None:
            self.load()

        import torch

        wav = np.asarray(pcm, dtype=np.float32).reshape(-1)
        timing = Timing(
            audio_duration_s=len(wav) / SAMPLE_RATE,
            load_s=self._load_s,
        )
        t_start = time.time()

        inputs = self._processor(
            wav, sampling_rate=SAMPLE_RATE, return_tensors="pt"
        ).to(self.cfg.device)
        with torch.no_grad():
            ids = self._model.generate(
                **inputs, max_new_tokens=self.asr_cfg.max_new_tokens
            )
        text = self._processor.batch_decode(ids, skip_special_tokens=True)[0].strip()

        timing.total_s = time.time() - t_start
        timing.tokens_generated = int(ids.shape[-1])

        return TranscriptResult(
            text=text,
            language=self.asr_cfg.language,
            raw_output=text,
            timing=timing,
        )

    def unload(self) -> None:
        self._model = None
        self._processor = None
