#!/usr/bin/env python3
"""录语料的图形界面。

    ./.venv/bin/python bench/record_gui.py

流程:
    选麦克风 → 点「开始录制」→ 出现句子和实时波形 → 念 → 点「下一条」
    → 存盘并跳到下一句 → 循环

用 tkinter 写的,Python 自带,不需要装任何额外的东西。

几个设计上的考虑:
  - 波形是实时画的,能一眼看出有没有削顶、有没有太小、有没有把话说漏
  - 存盘前自动砍掉首尾静音。不砍的话 RTF 会被稀释,跑分数字就不干净了
  - 音频回调线程只往队列里塞数据,画图全在 tkinter 主线程做
    (tkinter 不是线程安全的,在音频线程里画图会随机崩)
"""

from __future__ import annotations

import json
import sys
import time
import tkinter as tk
import tomllib
from collections import deque
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from vibevibe.config import AudioConfig  # noqa: E402
from vibevibe.recorder import Recorder, trim_silence  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent
CORPUS_PATH = BENCH_DIR / "corpus.toml"
DATA_DIR = BENCH_DIR / "data"

# ── 外观 ────────────────────────────────────────────────────────────────
BG = "#16181d"
FG = "#e6e8ec"
DIM = "#8b90a0"
ACCENT = "#5ac8fa"        # 波形和高亮
GOOD = "#4ade80"
WARN = "#fbbf24"
BAD = "#f87171"
PANEL = "#1e2128"

WAVE_SECONDS = 6.0        # 波形窗口显示最近多少秒
WAVE_COLUMNS = 700        # 波形横向像素列数
SUBDIVISIONS = 8          # 每个音频块切成几段画,决定波形细腻程度
REDRAW_MS = 33            # 约 30fps

# 电平判据。峰值高于这个就是削顶,低于这个就是太小。
CLIP_PEAK = 0.98
QUIET_PEAK = 0.06
# 单个样本到这个值就算削顶
CLIP_SAMPLE = 0.99
# 削顶样本占比超过这个就判定为"录废了,建议重录"。
# 千分之一以下属于偶发,听不出来也基本不影响识别;
# 到百分之一就是持续爆音,会实打实吃掉准确率。
CLIP_RATIO_BAD = 0.002


class RecorderGui:
    def __init__(self, root: tk.Tk, label: str, mic: str) -> None:
        self.root = root
        self.corpus = self._load_corpus()
        self.index = 0
        self.recording = False
        self.record_started_at = 0.0

        # 音频线程 → 界面线程的单向队列。只存 (最小值, 最大值) 对。
        self.wave = deque(maxlen=WAVE_COLUMNS)
        self.pending = deque()
        self.live_peak = 0.0

        self.label_var = tk.StringVar(value=label)
        self.mic_var = tk.StringVar(value=mic)
        self.continuous_var = tk.BooleanVar(value=True)

        self.recorder: Recorder | None = None
        self.manifest: dict = {}

        self._build_ui()
        self._load_manifest()
        self._show_item()
        self._tick()

    # ── 数据 ────────────────────────────────────────────────────────

    def _load_corpus(self) -> list[dict]:
        with open(CORPUS_PATH, "rb") as f:
            return tomllib.load(f)["item"]

    @property
    def out_dir(self) -> Path:
        return DATA_DIR / (self.label_var.get().strip() or "default")

    @property
    def manifest_path(self) -> Path:
        return self.out_dir / "manifest.json"

    def _load_manifest(self) -> None:
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        else:
            self.manifest = {}

    def _save_manifest(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _mic_choices(self) -> list[str]:
        try:
            import sounddevice as sd

            return [""] + [
                d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0
            ]
        except Exception:
            return [""]

    # ── 界面 ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.root.title("vibevibe · 录语料")
        self.root.configure(bg=BG)
        self.root.geometry("900x780")
        self.root.minsize(720, 560)

        # 布局原则:顶栏、波形框、底部按钮**先占位**(side=top/bottom),
        # 句子区最后铺满剩余空间。这样窗口再小也不会把按钮挤出屏幕——
        # Tk 是按 pack 顺序分配空间的,后 pack 的先被牺牲。

        # ttk 的下拉框默认是浅色主题,跟这套深色界面打架,单独调一下
        style = ttk.Style()
        try:
            style.theme_use("clam")
            style.configure(
                "TCombobox", fieldbackground=BG, background=PANEL,
                foreground=FG, arrowcolor=DIM, bordercolor=PANEL,
                lightcolor=PANEL, darkcolor=PANEL)
            self.root.option_add("*TCombobox*Listbox.background", PANEL)
            self.root.option_add("*TCombobox*Listbox.foreground", FG)
            self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        except tk.TclError:
            pass  # 主题不可用就用默认的,不是什么大事

        f_big = tkfont.Font(family="Noto Sans CJK SC", size=19)
        f_mid = tkfont.Font(family="Noto Sans CJK SC", size=12)
        f_small = tkfont.Font(family="Noto Sans CJK SC", size=10)
        f_mono = tkfont.Font(family="monospace", size=10)
        self.f_big, self.f_mid, self.f_small, self.f_mono = f_big, f_mid, f_small, f_mono

        # ── 顶栏:麦克风 / 标签 / 进度 ──
        top = tk.Frame(self.root, bg=PANEL, padx=14, pady=10)
        top.pack(side="top", fill="x")

        # 右侧的进度必须**先** pack,否则左边的控件会把空间吃光、
        # 把它挤成零宽度直接看不见(Tk 按 pack 顺序分配空间)
        self.progress_lbl = tk.Label(top, text="", bg=PANEL, fg=FG, font=f_small)
        self.progress_lbl.pack(side="right", padx=(12, 0))

        tk.Label(top, text="麦克风", bg=PANEL, fg=DIM, font=f_small).pack(side="left")
        self.mic_box = ttk.Combobox(
            top, textvariable=self.mic_var, values=self._mic_choices(),
            width=22, state="readonly")
        self.mic_box.pack(side="left", padx=(6, 16))

        tk.Label(top, text="批次", bg=PANEL, fg=DIM, font=f_small).pack(side="left")
        self.label_entry = tk.Entry(
            top, textvariable=self.label_var, width=9, bg=BG, fg=FG,
            insertbackground=FG, relief="flat", font=f_small)
        self.label_entry.pack(side="left", padx=(6, 0), ipady=3)

        # ── 底栏:按钮(先占位,保证永远可见) ──
        bottom = tk.Frame(self.root, bg=BG, padx=20, pady=14)
        bottom.pack(side="bottom", fill="x")

        # ── 波形框(次优先占位) ──
        wave_frame = tk.Frame(self.root, bg=PANEL, padx=14, pady=12)
        wave_frame.pack(side="bottom", fill="x")

        # ── 中部:句子(最后铺满剩余空间) ──
        mid = tk.Frame(self.root, bg=BG, padx=28, pady=18)
        mid.pack(side="top", fill="both", expand=True)

        self.note_lbl = tk.Label(
            mid, text="", bg=BG, fg=DIM, font=f_small, anchor="w", justify="left")
        self.note_lbl.pack(fill="x")

        self.sentence = tk.Text(
            mid, height=4, bg=BG, fg=FG, font=f_big, relief="flat",
            wrap="word", padx=0, pady=10, insertwidth=0, cursor="arrow",
            highlightthickness=0, borderwidth=0)
        self.sentence.pack(fill="both", expand=True)
        self.sentence.tag_configure("term", foreground=ACCENT)
        self.sentence.configure(state="disabled")

        self.terms_lbl = tk.Label(
            mid, text="", bg=BG, fg=DIM, font=f_small, anchor="w", justify="left")
        self.terms_lbl.pack(fill="x", pady=(4, 0))

        self.canvas = tk.Canvas(
            wave_frame, height=140, bg="#0f1115", highlightthickness=0)
        self.canvas.pack(fill="x")

        meter = tk.Frame(wave_frame, bg=PANEL)
        meter.pack(fill="x", pady=(8, 0))
        self.level_lbl = tk.Label(meter, text="", bg=PANEL, fg=DIM, font=f_mono)
        self.level_lbl.pack(side="left")
        self.timer_lbl = tk.Label(meter, text="", bg=PANEL, fg=DIM, font=f_mono)
        self.timer_lbl.pack(side="right")

        # 状态提示单独占一行。跟按钮挤在一行的话,窗口一窄就被压没了,
        # 而"削顶了""音量太小"这些提示恰恰是最不能漏看的。
        self.status_lbl = tk.Label(
            bottom, text="", bg=BG, fg=DIM, font=f_small, anchor="w")
        self.status_lbl.pack(side="bottom", fill="x", pady=(10, 0))

        btn_row = tk.Frame(bottom, bg=BG)
        btn_row.pack(side="top", fill="x")

        self.main_btn = tk.Button(
            btn_row, text="● 开始录制", command=self.on_main_button,
            bg=ACCENT, fg="#08121a", font=f_mid, relief="flat",
            padx=26, pady=10, activebackground="#7fd8ff", cursor="hand2")
        self.main_btn.pack(side="left")

        for text, cmd in (("重录本条", self.on_redo), ("跳过 ▸", self.on_skip)):
            tk.Button(btn_row, text=text, command=cmd, bg=PANEL, fg=FG,
                      font=f_small, relief="flat", padx=14, pady=8,
                      activebackground="#2a2e37", cursor="hand2"
                      ).pack(side="left", padx=(10, 0))

        tk.Checkbutton(
            btn_row, text="连续模式", variable=self.continuous_var,
            bg=BG, fg=DIM, font=f_small, selectcolor=PANEL, relief="flat",
            activebackground=BG, activeforeground=FG,
        ).pack(side="right")

        # 空格键 = 主按钮,手感跟将来那个小键盘一致
        self.root.bind("<space>", lambda e: self.on_main_button())
        self.root.bind("<Escape>", lambda e: self.on_quit())
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

    # ── 显示当前条目 ────────────────────────────────────────────────

    @property
    def item(self) -> dict | None:
        if 0 <= self.index < len(self.corpus):
            return self.corpus[self.index]
        return None

    def _show_item(self) -> None:
        item = self.item
        self.progress_lbl.config(
            text=f"第 {min(self.index + 1, len(self.corpus))} / {len(self.corpus)} 条"
                 f"   已录 {len(self.manifest)}")

        if item is None:
            self._set_sentence("全部录完了 🎉", [])
            self.note_lbl.config(text="")
            self.terms_lbl.config(
                text=f"文件在 {self.out_dir}  —— 可以关掉窗口了")
            self.main_btn.config(text="完成", bg=GOOD)
            return

        done = item["id"] in self.manifest
        self.note_lbl.config(
            text=f"[{item['id']}]  {item.get('note', '')}"
                 + ("   ✓ 已录过,再录会覆盖" if done else ""))
        self._set_sentence(item["text"], item.get("terms", []))
        terms = item.get("terms", [])
        self.terms_lbl.config(
            text=("必须被识别成英文的词: " + "、".join(terms)) if terms
            else "这条没有技术词(基线用)")
        self.main_btn.config(text="● 开始录制", bg=ACCENT)

    def _set_sentence(self, text: str, terms: list[str]) -> None:
        self.sentence.configure(state="normal")
        self.sentence.delete("1.0", "end")
        self.sentence.insert("1.0", text)
        # 把技术词标成高亮色,念的时候一眼能看见重点在哪
        for term in terms:
            start = "1.0"
            while True:
                pos = self.sentence.search(term, start, stopindex="end", nocase=True)
                if not pos:
                    break
                end = f"{pos}+{len(term)}c"
                self.sentence.tag_add("term", pos, end)
                start = end
        self.sentence.configure(state="disabled")

    # ── 录音 ────────────────────────────────────────────────────────

    def _on_audio_block(self, block: np.ndarray) -> None:
        """在 PortAudio 的音频线程里跑。只做最轻的事:算几个极值塞进队列。"""
        data = block.reshape(-1) if block.ndim == 1 else block.mean(axis=1)
        n = len(data)
        if n == 0:
            return
        step = max(1, n // SUBDIVISIONS)
        for i in range(0, n, step):
            seg = data[i:i + step]
            if len(seg):
                self.pending.append((float(seg.min()), float(seg.max())))

    def _start(self) -> None:
        item = self.item
        if item is None:
            return
        cfg = AudioConfig(input_device=self.mic_var.get().strip())
        self.recorder = Recorder(cfg, on_block=self._on_audio_block)
        try:
            self.recorder.start()
        except Exception as exc:
            messagebox.showerror("打不开麦克风", str(exc))
            self.recorder = None
            return

        self.recording = True
        self.record_started_at = time.time()
        self.wave.clear()
        self.pending.clear()
        self.live_peak = 0.0
        self.main_btn.config(text="■ 下一条", bg=BAD)
        self.mic_box.config(state="disabled")
        self.label_entry.config(state="disabled")
        self.status_lbl.config(text="录音中…… 说完点「下一条」(或按空格)", fg=BAD)

    def _stop_and_save(self) -> bool:
        """停止并存盘。返回是否存成功。"""
        if self.recorder is None:
            return False
        pcm = self.recorder.stop()
        self.recording = False
        self.recorder = None
        self.mic_box.config(state="readonly")
        self.label_entry.config(state="normal")

        item = self.item
        if item is None:
            return False

        if len(pcm) == 0:
            self.status_lbl.config(text="✗ 录音太短或为空,这条没存", fg=BAD)
            return False

        raw_sec = len(pcm) / 16000
        pcm = trim_silence(pcm, 16000)
        trimmed_sec = len(pcm) / 16000

        peak = float(np.abs(pcm).max())
        rms = float(np.sqrt(np.mean(pcm ** 2)))
        # 削顶比例比峰值更能说明问题:峰值只要有一个样本到顶就是 1.0,
        # 看不出是偶发爆音还是持续爆音。比例才分得清。
        clip_ratio = float((np.abs(pcm) >= CLIP_SAMPLE).mean())

        import soundfile as sf

        self.out_dir.mkdir(parents=True, exist_ok=True)
        wav_path = self.out_dir / f"{item['id']}.wav"
        sf.write(str(wav_path), pcm, 16000, subtype="PCM_16")

        self.manifest[item["id"]] = {
            "wav": wav_path.name,
            "text": item["text"],
            "terms": item.get("terms", []),
            "note": item.get("note", ""),
            "duration_sec": round(trimmed_sec, 2),
            "raw_duration_sec": round(raw_sec, 2),
            "peak": round(peak, 4),
            "rms": round(rms, 5),
            "clip_ratio": round(clip_ratio, 5),
            "mic": self.mic_var.get().strip() or "(default)",
        }
        self._save_manifest()

        if clip_ratio > CLIP_RATIO_BAD:
            msg = (f"⚠ 爆音严重:{clip_ratio * 100:.2f}% 的样本削顶 "
                   f"—— 建议调小麦克风增益后「重录本条」")
            color = BAD
        elif peak > CLIP_PEAK:
            msg, color = f"· 有轻微削顶({clip_ratio * 100:.2f}%),尚可接受", WARN
        elif peak < QUIET_PEAK:
            msg, color = f"⚠ 音量太小(峰值 {peak:.2f})——离麦近点或调大增益", WARN
        else:
            msg, color = (f"✓ 已存 {trimmed_sec:.1f}s(裁掉静音 "
                          f"{raw_sec - trimmed_sec:.1f}s) 峰值 {peak:.2f} "
                          f"RMS {rms:.3f}"), GOOD
        self.status_lbl.config(text=msg, fg=color)
        return True

    # ── 按钮 ────────────────────────────────────────────────────────

    def on_main_button(self) -> None:
        if self.item is None:
            self.on_quit()
            return
        if self.recording:
            self._stop_and_save()
            self.index += 1
            self._show_item()
            if self.continuous_var.get() and self.item is not None:
                self._start()
        else:
            self._start()

    def on_redo(self) -> None:
        if self.recording:
            # 丢掉当前这段,重新开始录同一条
            if self.recorder:
                self.recorder.abort()
                self.recorder = None
            self.recording = False
            self.mic_box.config(state="readonly")
            self.label_entry.config(state="normal")
            self.status_lbl.config(text="已丢弃,重新录这一条", fg=WARN)
            self._start()
            return
        # 没在录:退回上一条
        if self.index > 0:
            self.index -= 1
            self._show_item()
            self.status_lbl.config(text="退回上一条", fg=DIM)

    def on_skip(self) -> None:
        if self.recording and self.recorder:
            self.recorder.abort()
            self.recorder = None
            self.recording = False
            self.mic_box.config(state="readonly")
            self.label_entry.config(state="normal")
        self.index += 1
        self._show_item()
        self.status_lbl.config(text="已跳过", fg=DIM)

    def on_quit(self) -> None:
        if self.recording and self.recorder:
            self.recorder.abort()
        self.root.destroy()

    # ── 画波形 ──────────────────────────────────────────────────────

    def _tick(self) -> None:
        # 把音频线程攒下的数据搬进显示缓冲
        moved = 0
        while self.pending and moved < WAVE_COLUMNS:
            lo, hi = self.pending.popleft()
            self.wave.append((lo, hi))
            self.live_peak = max(self.live_peak, abs(lo), abs(hi))
            moved += 1

        self._draw_wave()

        if self.recording:
            elapsed = time.time() - self.record_started_at
            self.timer_lbl.config(text=f"{elapsed:5.1f}s")
            recent = list(self.wave)[-40:]
            inst = max((max(abs(a), abs(b)) for a, b in recent), default=0.0)
            # 正常的 float32 录音是钳在 ±1 的;万一驱动给了非归一化数据,
            # 这里兜一下,免得电平表显示成 4.12 这种看不懂的数字
            inst = min(inst, 1.0)
            if inst > CLIP_PEAK:
                text, color = f"电平 {inst:.2f}  削顶!", BAD
            elif inst < QUIET_PEAK:
                text, color = f"电平 {inst:.2f}  偏小", WARN
            else:
                text, color = f"电平 {inst:.2f}  正常", GOOD
            self.level_lbl.config(text=text, fg=color)

        self.root.after(REDRAW_MS, self._tick)

    def _draw_wave(self) -> None:
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() or 800
        h = c.winfo_height() or 140
        mid = h / 2

        # 中线
        c.create_line(0, mid, w, mid, fill="#2a2e37")
        # 削顶警戒线
        for frac in (CLIP_PEAK, -CLIP_PEAK):
            y = mid - frac * mid * 0.95
            c.create_line(0, y, w, y, fill="#3a2a2a", dash=(3, 4))

        if not self.wave:
            c.create_text(w / 2, mid, text="点「开始录制」后这里会显示实时波形",
                          fill=DIM, font=self.f_small)
            return

        data = list(self.wave)
        n = len(data)
        # 从右往左画,新的数据在右边(像录音软件那样往左滚)
        x_step = w / WAVE_COLUMNS
        x0 = w - n * x_step
        for i, (lo, hi) in enumerate(data):
            x = x0 + i * x_step
            y1 = mid - hi * mid * 0.95
            y2 = mid - lo * mid * 0.95
            clipped = abs(hi) > CLIP_PEAK or abs(lo) > CLIP_PEAK
            c.create_line(x, y1, x, y2,
                          fill=BAD if clipped else ACCENT,
                          width=max(1, int(x_step) + 1))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="vibevibe 录语料(图形界面)")
    parser.add_argument("--label", default="c920", help="批次标签,作为子目录名")
    parser.add_argument("--mic", default="", help="麦克风名字的一部分,留空用系统默认")
    args = parser.parse_args()

    root = tk.Tk()
    RecorderGui(root, label=args.label, mic=args.mic)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
