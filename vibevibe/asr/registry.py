"""后端注册表。配置里的 backend 名字 → 具体实现。

加了新后端只要在这里登记一行,其余代码不用动。
"""

from __future__ import annotations

from ..config import Config
from .base import AsrBackend

BACKENDS = ("qwen_onnx", "qwen_onnx_1p7b", "whisper_ct2", "qwen_hf")


def build_backend(cfg: Config, backend: str | None = None) -> AsrBackend:
    """按配置造一个 ASR 后端(此时还没加载权重,load() 才加载)。"""
    name = backend or cfg.asr.backend

    if name == "qwen_onnx":
        from .qwen_onnx import QwenOnnxBackend

        return QwenOnnxBackend(cfg.asr, cfg.asr.qwen_onnx, cfg.guard)

    if name == "qwen_onnx_1p7b":
        from .qwen_onnx import QwenOnnxBackend

        return QwenOnnxBackend(cfg.asr, cfg.asr.qwen_onnx_1p7b, cfg.guard)

    if name == "whisper_ct2":
        from .whisper_ct2 import WhisperCt2Backend

        return WhisperCt2Backend(cfg.asr, cfg.asr.whisper_ct2)

    if name == "qwen_hf":
        from .qwen_hf import QwenHfBackend

        return QwenHfBackend(cfg.asr, cfg.asr.qwen_hf)

    raise ValueError(
        f"未知的 ASR 后端 {name!r}。可选: {', '.join(BACKENDS)}"
    )
