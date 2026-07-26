"""Qwen3-ASR ONNX 后端 —— 纯 CPU,不需要 PyTorch,不碰 CUDA。

流水线(五段):
    PCM → log-mel(librosa)
        → encoder_conv.onnx        3 层 Conv2D,8 倍下采样
        → encoder_transformer.onnx 18 层 Transformer + 投影层(896→1024)
        → 用 numpy 把音频特征填进 prompt 的占位符里
        → decoder_init.onnx        预填充,吐出 logits + KV cache
        → decoder_step.onnx        贪心自回归解码直到 EOS

改造自权重仓库自带的参考实现 onnx_inference.py(Apache 2.0,
见 docs/reference/)。相对参考实现的改动:

  1. 吃内存里的 PCM 而不是文件路径——守护进程每次说话都落盘没必要
  2. 把重复检测做进解码循环,一旦跑飞立刻停,不等撞满 max_new_tokens
  3. 解码循环里查超时
  4. 词嵌入矩阵默认用 mmap 而不是整个读进内存(省 622MB 常驻)
  5. 所有常量走配置,不写死
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from ..config import AsrConfig, GuardConfig, QwenOnnxConfig, _expand
from ..guard import Deadline, GuardTripped, RepetitionGuard
from .base import AsrBackend, Timing, TranscriptResult

# ── 模型固有常量(由权重决定,不是可调项) ─────────────────────────────
# 这些值来自 Qwen3-ASR-0.6B 的模型结构与其 ONNX 导出方式,改了就对不上权重。

SAMPLE_RATE = 16000
N_FFT = 400
HOP_LENGTH = 160
N_MELS = 128
ENCODER_CHUNK_FRAMES = 100  # 编码器一次吃多少帧 mel(= n_window * 2)

# 特殊 token id
AUDIO_START_ID = 151669
AUDIO_END_ID = 151670
AUDIO_PAD_ID = 151676
IM_START_ID = 151644
IM_END_ID = 151645  # EOS
ENDOFTEXT_ID = 151643  # 另一个 EOS
NEWLINE_ID = 198

VOCAB_SIZE = 151936
# 隐藏维随模型大小变(0.6B 是 1024,1.7B 是 2048),加载时从 ONNX 签名读出来,
# 不写死。下面这个只是找不到时的兜底默认值。
DEFAULT_HIDDEN_SIZE = 1024

# 超过这个长度的音频才需要切分。听写场景基本用不到,
# 但录音忘了停的时候它是最后一道防线。
LONG_AUDIO_THRESHOLD_SEC = 45.0
SILENCE_THRESHOLD_DB = -40.0
SILENCE_HOP_SEC = 0.1


# ── mel 频谱(与 Whisper 兼容,纯 numpy + librosa) ───────────────────


def _mel_filters() -> np.ndarray:
    import librosa

    return librosa.filters.mel(
        sr=SAMPLE_RATE,
        n_fft=N_FFT,
        n_mels=N_MELS,
        fmin=0,
        fmax=SAMPLE_RATE // 2,
        norm="slaney",
        htk=False,
    ).astype(np.float32)


def _log_mel(wav: np.ndarray, filters: np.ndarray) -> np.ndarray:
    """[T] 波形 → [128, frames] 对数 mel 频谱。"""
    import librosa

    stft = librosa.stft(
        wav, n_fft=N_FFT, hop_length=HOP_LENGTH,
        window="hann", center=True, pad_mode="reflect",
    )
    power = np.abs(stft) ** 2
    mel = filters @ power
    log_spec = np.log10(np.maximum(mel, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec.astype(np.float32)


def _conv_output_lengths(lengths: np.ndarray) -> np.ndarray:
    """3 次 stride-2 卷积之后的长度。"""
    for _ in range(3):
        lengths = (lengths - 1) // 2 + 1
    return lengths


def _silence_split_points(wav: np.ndarray, target_sec: int) -> list[int]:
    """在静音处切开长音频,返回切分点的采样下标。

    用 RMS 能量找静音,不需要额外的 VAD 模型。
    每段长度范围是 [target/2, target*1.5],切点取最接近目标长度的静音帧。
    """
    import librosa

    min_sec = target_sec // 2
    max_sec = int(target_sec * 1.5)
    total = len(wav)
    if total <= max_sec * SAMPLE_RATE:
        return []

    hop = int(SILENCE_HOP_SEC * SAMPLE_RATE)
    rms = librosa.feature.rms(y=wav, frame_length=hop * 2, hop_length=hop)[0]
    rms_db = librosa.amplitude_to_db(rms, ref=np.max)
    is_silent = rms_db < SILENCE_THRESHOLD_DB

    points: list[int] = []
    cursor = 0
    while cursor + max_sec * SAMPLE_RATE < total:
        start_sec = max(0.0, cursor / SAMPLE_RATE + min_sec)
        end_sec = cursor / SAMPLE_RATE + max_sec
        target_abs = cursor / SAMPLE_RATE + target_sec

        f_start = int(start_sec / SILENCE_HOP_SEC)
        f_end = min(int(end_sec / SILENCE_HOP_SEC), len(is_silent))
        f_target = int(target_abs / SILENCE_HOP_SEC)

        silent = np.where(is_silent[f_start:f_end])[0] + f_start
        if len(silent) > 0:
            best = int(np.argmin(np.abs(silent - f_target)))
            split = int(silent[best] * hop)
        else:
            split = int(target_abs * SAMPLE_RATE)

        split = min(split, total)
        if split <= cursor:  # 防御:切点没往前走就直接放弃切分,避免死循环
            break
        points.append(split)
        cursor = split
    return points


# ── 后端实现 ────────────────────────────────────────────────────────────


class QwenOnnxBackend(AsrBackend):
    sample_rate = SAMPLE_RATE

    def __init__(
        self,
        asr_cfg: AsrConfig,
        backend_cfg: QwenOnnxConfig,
        guard_cfg: GuardConfig,
    ) -> None:
        self.asr_cfg = asr_cfg
        self.cfg = backend_cfg
        self.guard_cfg = guard_cfg
        self._loaded = False
        self._load_s = 0.0

        model_dir = _expand(backend_cfg.model_dir)
        self.model_dir = model_dir
        # 权重仓库的结构是 <repo>/onnx_models/*.onnx + <repo>/tokenizer.json,
        # 但也允许直接把 onnx_dir 指到放 .onnx 的那一层。
        onnx_dir = model_dir / "onnx_models"
        self.onnx_dir = onnx_dir if onnx_dir.is_dir() else model_dir

    @property
    def name(self) -> str:
        return f"qwen_onnx:{self.model_dir.name}:{self.cfg.quantize}"

    # ── 加载 ────────────────────────────────────────────────────────

    def _pick(self, *names: str) -> Path | None:
        """按顺序返回第一个存在的文件(用来兼容带量化后缀的文件名)。"""
        for name in names:
            p = self.onnx_dir / name
            if p.exists():
                return p
        return None

    def _tokenizer_path(self) -> Path:
        for candidate in (self.model_dir / "tokenizer.json", self.onnx_dir / "tokenizer.json"):
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            f"找不到 tokenizer.json(在 {self.model_dir} 和 {self.onnx_dir} 里都没有)。"
            "权重是不是没下全?"
        )

    def load(self) -> None:
        if self._loaded:
            return
        import onnxruntime as ort

        t0 = time.time()

        if not self.onnx_dir.is_dir():
            raise FileNotFoundError(
                f"模型目录不存在: {self.onnx_dir}\n"
                f"请先下载权重到 {self.model_dir}"
            )

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.log_severity_level = 3
        if self.cfg.intra_op_num_threads > 0:
            opts.intra_op_num_threads = self.cfg.intra_op_num_threads
        if self.cfg.inter_op_num_threads > 0:
            opts.inter_op_num_threads = self.cfg.inter_op_num_threads

        # 量化档对应的文件名。不同导出提供的档位不一样:
        # 0.6B 有 int8,1.7B 的社区导出只有 fp32 和 int4。
        # quantize = "none" 表示用不带后缀的 FP32 版本。
        suffix = "" if self.cfg.quantize in ("none", "fp32", "") else f".{self.cfg.quantize}"
        init_name = f"decoder_init{suffix}.onnx"
        step_name = f"decoder_step{suffix}.onnx"
        if not (self.onnx_dir / init_name).exists():
            available = sorted(p.name for p in self.onnx_dir.glob("decoder_init*.onnx"))
            raise FileNotFoundError(
                f"缺少解码器权重 {init_name}(quantize={self.cfg.quantize!r})。"
                f"这个目录里实际有的是: {available or '(一个都没有)'}"
            )

        providers = ["CPUExecutionProvider"]  # 明确只走 CPU,不碰显卡

        # ── 编码器 ──
        # 两种布局,按目录里实际有什么文件判断,不假设:
        #   两段式 —— encoder_conv + encoder_transformer(0.6B 的导出),
        #             mel 要先切成固定长度的块
        #   单文件 —— encoder(1.7B 的导出),整段 mel 一次吃进去
        conv_path = self.onnx_dir / "encoder_conv.onnx"
        single_path = self._pick(f"encoder{suffix}.onnx", "encoder.onnx")
        if conv_path.exists():
            self.encoder_layout = "two_stage"
            self.encoder_conv = ort.InferenceSession(
                str(conv_path), opts, providers=providers)
            self.encoder_transformer = ort.InferenceSession(
                str(self.onnx_dir / "encoder_transformer.onnx"), opts, providers=providers)
        elif single_path is not None:
            self.encoder_single = ort.InferenceSession(
                str(single_path), opts, providers=providers)
            inputs = self.encoder_single.get_inputs()
            shape = list(inputs[0].shape)
            # [1, 128, time] = 整段 mel;[n, 1, 128, T] = 分块后的 mel
            if len(inputs) == 1 and len(shape) == 3 and shape[1] == N_MELS:
                self.encoder_layout = "full_mel"
            elif len(inputs) == 1 and len(shape) == 4:
                self.encoder_layout = "chunked_mel"
            else:
                raise NotImplementedError(
                    "这个单文件编码器的输入签名还没适配过。实际签名是:\n  "
                    + "\n  ".join(f"{i.name}: {i.shape} {i.type}" for i in inputs)
                    + "\n照着它补一个分支——不要靠猜。"
                )
            self.encoder_input_name = inputs[0].name
        else:
            raise FileNotFoundError(
                f"{self.onnx_dir} 里既没有 encoder_conv.onnx 也没有 encoder.onnx,"
                f"认不出这是什么导出格式。目录里的 onnx 文件: "
                f"{sorted(p.name for p in self.onnx_dir.glob('*.onnx'))}"
            )

        # ── 解码器 ──
        self.decoder_init = ort.InferenceSession(
            str(self.onnx_dir / init_name), opts, providers=providers)
        self.decoder_step = ort.InferenceSession(
            str(self.onnx_dir / step_name), opts, providers=providers)

        # 预填充有两种接口,按实际输入名判断:
        #   fused_embeds   —— 吃 input_embeds,音频特征由我们在外面融进词嵌入(0.6B)
        #   ids_and_audio  —— 吃 input_ids + audio_features + audio_offset,
        #                     融合在模型内部做(1.7B)
        init_inputs = {i.name for i in self.decoder_init.get_inputs()}
        if "input_embeds" in init_inputs:
            self.decoder_variant = "fused_embeds"
        elif {"input_ids", "audio_features", "audio_offset"} <= init_inputs:
            self.decoder_variant = "ids_and_audio"
        else:
            raise NotImplementedError(
                "这个 decoder_init 的输入签名还没适配过。实际是: "
                + ", ".join(sorted(init_inputs))
                + "\n照着它补一个分支——不要靠猜。"
            )

        # 隐藏维从 decoder_step 的 input_embeds 形状读,不写死
        # (0.6B 是 1024,1.7B 是 2048)
        self.hidden_size = DEFAULT_HIDDEN_SIZE
        for i in self.decoder_step.get_inputs():
            if i.name == "input_embeds" and len(i.shape) == 3:
                if isinstance(i.shape[-1], int):
                    self.hidden_size = i.shape[-1]
                break

        # ── 词嵌入矩阵 ──
        # dtype 也不能写死:0.6B 那份是 float32,1.7B 那份是 float16。
        # 按 文件大小 ÷ (词表 × 隐藏维) 反推每个元素几个字节——
        # 猜错了不会报错,只会安安静静输出乱码,那种最难查。
        embed_path = self.onnx_dir / "embed_tokens.bin"
        if not embed_path.exists():
            raise FileNotFoundError(f"缺少词嵌入 {embed_path}")
        nbytes = embed_path.stat().st_size
        per_elem = nbytes / (VOCAB_SIZE * self.hidden_size)
        dtype = {4.0: np.float32, 2.0: np.float16}.get(per_elem)
        if dtype is None:
            raise RuntimeError(
                f"认不出词嵌入的数据类型: {embed_path} 有 {nbytes:,} 字节,"
                f"按 词表 {VOCAB_SIZE} × 隐藏维 {self.hidden_size} 算,"
                f"每个元素 {per_elem:.3f} 字节(应该是 4=float32 或 2=float16)"
            )
        self.embed_dtype = dtype
        if self.cfg.mmap_embeddings:
            self.embed_tokens = np.memmap(
                str(embed_path), dtype=dtype, mode="r",
                shape=(VOCAB_SIZE, self.hidden_size))
        else:
            self.embed_tokens = np.fromfile(
                str(embed_path), dtype=dtype).reshape(VOCAB_SIZE, self.hidden_size)

        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(self._tokenizer_path()))
        self.mel_filters = _mel_filters()

        self._load_s = time.time() - t0
        self._loaded = True

    # ── 编码 ────────────────────────────────────────────────────────

    def _chunk_mel(self, mel: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """把 mel 切成固定长度的块并补齐。

        返回 (padded[n_chunks, 1, 128, max_len], 卷积后每块的有效长度)。
        """
        mel_len = mel.shape[1]
        n_chunks = int(np.ceil(mel_len / ENCODER_CHUNK_FRAMES))

        chunk_lens = []
        for i in range(n_chunks):
            start = i * ENCODER_CHUNK_FRAMES
            end = min(start + ENCODER_CHUNK_FRAMES, mel_len)
            chunk_lens.append(end - start)

        max_len = max(chunk_lens)
        padded = np.zeros((n_chunks, 1, N_MELS, max_len), dtype=np.float32)
        cursor = 0
        for i, cl in enumerate(chunk_lens):
            padded[i, 0, :, :cl] = mel[:, cursor:cursor + cl]
            cursor += cl

        return padded, _conv_output_lengths(np.array(chunk_lens))

    def _encode_audio(self, mel: np.ndarray) -> np.ndarray:
        """[128, frames] mel → [N, hidden] 音频特征。"""
        if self.encoder_layout == "full_mel":
            # 整段 mel 一次吃进去,不用切块(1.7B 的导出)
            out = self.encoder_single.run(
                None, {self.encoder_input_name: mel[np.newaxis, :, :]})[0]
            return out[0] if out.ndim == 3 else out

        padded, lens_after_cnn = self._chunk_mel(mel)

        if self.encoder_layout == "two_stage":
            conv_out = self.encoder_conv.run(None, {"padded_mel_chunks": padded})[0]
            # 去掉 padding 再拼起来
            packed = np.concatenate(
                [conv_out[i, :l, :] for i, l in enumerate(lens_after_cnn)], axis=0)
            # 全对全注意力(CPU 上是 eager 模式,ONNX 导出与之一致)
            total = packed.shape[0]
            attn_mask = np.zeros((1, 1, total, total), dtype=np.float32)
            return self.encoder_transformer.run(None, {
                "hidden_states": packed,
                "attention_mask": attn_mask,
            })[0]

        # chunked_mel:单文件编码器,但吃的是切好块的 mel
        out = self.encoder_single.run(None, {self.encoder_input_name: padded})[0]
        if out.ndim == 3:  # [n_chunks, seq, dim] → 去 padding 后拼接
            return np.concatenate(
                [out[i, :l, :] for i, l in enumerate(lens_after_cnn)], axis=0)
        return out

    def _build_prompt(self, n_audio_tokens: int, language: str) -> list[int]:
        enc = lambda s: self.tokenizer.encode(s).ids  # noqa: E731
        ids = [IM_START_ID] + enc("system") + [NEWLINE_ID, IM_END_ID, NEWLINE_ID]
        ids += [IM_START_ID] + enc("user") + [NEWLINE_ID]
        ids += [AUDIO_START_ID] + [AUDIO_PAD_ID] * n_audio_tokens + [AUDIO_END_ID]
        ids += [IM_END_ID, NEWLINE_ID]
        ids += [IM_START_ID] + enc("assistant") + [NEWLINE_ID]
        if language:
            # 只有显式指定语言时才加这段。中英混说要留空走自动检测,
            # 强行指定会把模型钉死在一种语言上,混说就废了。
            ids += enc(f"language {language}<asr_text>")
        return ids

    def _fuse(self, token_ids: list[int], audio_features: np.ndarray) -> np.ndarray:
        ids = np.asarray(token_ids)
        embeds = np.asarray(self.embed_tokens[ids], dtype=np.float32)
        positions = np.where(ids == AUDIO_PAD_ID)[0]
        if len(positions) != audio_features.shape[0]:
            raise RuntimeError(
                f"音频 token 数对不上: 占位符 {len(positions)} 个,"
                f"编码器给了 {audio_features.shape[0]} 个"
            )
        embeds[positions] = audio_features
        return embeds[np.newaxis, :, :]

    # ── 解码 ────────────────────────────────────────────────────────

    def _prefill(
        self,
        prompt_ids: list[int],
        audio_features: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
        """预填充。返回 (logits, KV keys, KV values, 序列长度)。

        两种接口(加载时自省出来的):
          fused_embeds  —— 我们在外面把音频特征填进词嵌入,喂 input_embeds
          ids_and_audio —— 直接喂 token id + 音频特征 + 音频插入位置,
                           融合在模型内部做
        """
        ids = np.asarray(prompt_ids, dtype=np.int64)
        seq_len = len(ids)
        position_ids = np.arange(seq_len, dtype=np.int64).reshape(1, -1)

        if self.decoder_variant == "ids_and_audio":
            positions = np.where(ids == AUDIO_PAD_ID)[0]
            if len(positions) != audio_features.shape[0]:
                raise RuntimeError(
                    f"音频 token 数对不上: 占位符 {len(positions)} 个,"
                    f"编码器给了 {audio_features.shape[0]} 个"
                )
            logits, keys, values = self.decoder_init.run(None, {
                "input_ids": ids.reshape(1, -1),
                "position_ids": position_ids,
                "audio_features": audio_features[np.newaxis].astype(np.float32),
                # 音频特征要插进 token 序列的哪个位置
                "audio_offset": np.array([positions[0]], dtype=np.int64),
            })
        else:
            input_embeds = self._fuse(prompt_ids, audio_features)
            logits, keys, values = self.decoder_init.run(None, {
                "input_embeds": input_embeds,
                "position_ids": position_ids,
            })
        return logits, keys, values, seq_len

    def _decode(
        self,
        prompt_ids: list[int],
        audio_features: np.ndarray,
        deadline: Deadline,
        timing: Timing,
    ) -> tuple[list[int], str]:
        """贪心解码。返回 (token 列表, 闸门说明)。"""
        t0 = time.time()
        logits, keys, values, seq_len = self._prefill(prompt_ids, audio_features)
        timing.prefill_s += time.time() - t0

        t0 = time.time()
        guard = RepetitionGuard(self.guard_cfg)
        generated: list[int] = []
        guard_note = ""

        next_token = int(np.argmax(logits[0, -1, :]))
        generated.append(next_token)
        guard.feed(next_token)
        cur_pos = seq_len

        for _ in range(self.asr_cfg.max_new_tokens - 1):
            if next_token in (IM_END_ID, ENDOFTEXT_ID):
                break
            deadline.check()

            token_embed = np.asarray(
                self.embed_tokens[next_token], dtype=np.float32
            )[np.newaxis, np.newaxis, :]
            logits, keys, values = self.decoder_step.run(None, {
                "input_embeds": token_embed,
                "position_ids": np.array([[cur_pos]], dtype=np.int64),
                "past_keys": keys,
                "past_values": values,
            })
            next_token = int(np.argmax(logits[0, -1, :]))
            generated.append(next_token)
            cur_pos += 1

            if guard.feed(next_token):
                # 陷入重复。把重复的尾巴砍掉,保留前面可能有效的部分,
                # 但要在结果里留下记号,不能假装无事发生。
                guard_note = guard.tripped_reason or "检测到重复"
                generated = generated[: -self.guard_cfg.repeat_ngram_size
                                      * self.guard_cfg.repeat_max_times]
                break

        if generated and generated[-1] in (IM_END_ID, ENDOFTEXT_ID):
            generated = generated[:-1]

        timing.decode_s += time.time() - t0
        timing.tokens_generated += len(generated)
        return generated, guard_note

    def _transcribe_chunk(
        self, wav: np.ndarray, deadline: Deadline, timing: Timing
    ) -> tuple[str, str, str]:
        """返回 (文本, 语言, 闸门说明)。"""
        t0 = time.time()
        mel = _log_mel(wav, self.mel_filters)
        timing.mel_s += time.time() - t0

        t0 = time.time()
        features = self._encode_audio(mel)
        timing.encoder_s += time.time() - t0

        prompt = self._build_prompt(features.shape[0], self.asr_cfg.language)
        tokens, guard_note = self._decode(prompt, features, deadline, timing)
        raw = self.tokenizer.decode(tokens, skip_special_tokens=True)

        # 自动检测语言时,模型会先吐 "language XXX<asr_text>" 再吐正文
        language, text = "", raw
        if "<asr_text>" in raw:
            head, _, tail = raw.partition("<asr_text>")
            if head.startswith("language "):
                language = head[len("language "):].strip()
            text = tail
        elif self.asr_cfg.language:
            language = self.asr_cfg.language

        return text, language, guard_note

    # ── 对外接口 ────────────────────────────────────────────────────

    def transcribe(self, pcm: np.ndarray) -> TranscriptResult:
        if not self._loaded:
            self.load()

        wav = np.asarray(pcm, dtype=np.float32).reshape(-1)
        timing = Timing(
            audio_duration_s=len(wav) / SAMPLE_RATE,
            load_s=self._load_s,
        )
        deadline = Deadline(self.guard_cfg.transcribe_timeout_sec)
        t_start = time.time()

        splits = _silence_split_points(wav, self.asr_cfg.chunk_sec)
        if not splits:
            text, language, note = self._transcribe_chunk(wav, deadline, timing)
        else:
            bounds = [0, *splits, len(wav)]
            timing.sub_chunks = len(bounds) - 1
            parts, notes, language = [], [], ""
            for i in range(len(bounds) - 1):
                t, lang, note = self._transcribe_chunk(
                    wav[bounds[i]:bounds[i + 1]], deadline, timing)
                if t.strip():
                    parts.append(t.strip())
                if note:
                    notes.append(f"第{i + 1}段: {note}")
                language = language or lang
            text = " ".join(parts)
            note = "; ".join(notes)

        timing.total_s = time.time() - t_start
        return TranscriptResult(
            text=text,
            language=language,
            raw_output=text,
            timing=timing,
            guard_note=note,
        )

    def unload(self) -> None:
        for attr in ("encoder_conv", "encoder_transformer", "encoder_single",
                     "decoder_init", "decoder_step", "embed_tokens"):
            if hasattr(self, attr):
                delattr(self, attr)
        self._loaded = False


__all__ = ["QwenOnnxBackend", "GuardTripped"]
