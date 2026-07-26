"""ASR 后端。三个实现共用 base.AsrBackend 接口,换模型只改配置。"""

from .base import AsrBackend, Timing, TranscriptResult
from .registry import BACKENDS, build_backend

__all__ = ["AsrBackend", "Timing", "TranscriptResult", "BACKENDS", "build_backend"]
