#!/usr/bin/env python3
"""录测试语料。

用法:
    # 先看看有哪些麦
    ./.venv/bin/python bench/record.py --list

    # 用 C920 摄像头麦录一轮
    ./.venv/bin/python bench/record.py --mic C920 --label c920

    # 换个近场麦再录一轮,用来量化麦克风的影响
    ./.venv/bin/python bench/record.py --mic ALC1220 --label onboard

    # 只补录某几条
    ./.venv/bin/python bench/record.py --mic C920 --label c920 --only mix_git,zh_long

录出来的东西放在 bench/data/<label>/,每条一个 wav,
外加一份 manifest.json 记着参考文本和关键词。

这一步几乎不占 CPU(就是录音和写文件),可以随时做。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from vibevibe.config import AudioConfig  # noqa: E402
from vibevibe.recorder import Recorder, list_input_devices  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent
CORPUS_PATH = BENCH_DIR / "corpus.toml"
DATA_DIR = BENCH_DIR / "data"


def load_corpus() -> list[dict]:
    with open(CORPUS_PATH, "rb") as f:
        return tomllib.load(f)["item"]


# record_one 的两个特殊返回值,跟"录到的音频"区分开
SKIP = object()
QUIT = object()


def record_one(recorder: Recorder, item: dict):
    """交互式录一条。返回 PCM、SKIP(跳过这条)或 QUIT(整体退出)。"""
    print()
    print("─" * 72)
    print(f"[{item['id']}] {item.get('note', '')}")
    print()
    print(f"  念这句: {item['text']}")
    if item.get("terms"):
        print(f"  关键词: {', '.join(item['terms'])}  ← 这些必须被识别成英文")
    print()

    choice = input("回车开始录音(s 跳过 / q 退出): ").strip().lower()
    if choice == "q":
        return QUIT
    if choice == "s":
        return SKIP

    recorder.start()
    t0 = time.time()
    print("  ● 录音中…… 说完按回车停止")
    input()
    pcm = recorder.stop()
    elapsed = time.time() - t0

    if len(pcm) == 0:
        print("  ✗ 录音为空或过短,这条没存")
        return SKIP

    peak = float(np.abs(pcm).max())
    rms = float(np.sqrt(np.mean(pcm ** 2)))
    print(f"  ✓ {elapsed:.1f}s  峰值 {peak:.3f}  RMS {rms:.4f}")
    if peak > 0.99:
        print("    ⚠ 削顶了(峰值到顶),离麦远一点或把输入增益调小")
    elif peak < 0.05:
        print("    ⚠ 音量太小,离麦近一点或把输入增益调大 —— 这会直接拉低识别率")

    return pcm


def main() -> int:
    parser = argparse.ArgumentParser(description="录 vibevibe 的测试语料")
    parser.add_argument("--mic", default="", help="麦克风:设备名子串或索引,留空用系统默认")
    parser.add_argument("--label", default="default", help="这一轮的标签,作为子目录名")
    parser.add_argument("--only", default="", help="只录这些 id,逗号分隔")
    parser.add_argument("--list", action="store_true", help="列出输入设备后退出")
    parser.add_argument("--sample-rate", type=int, default=16000)
    args = parser.parse_args()

    if args.list:
        print("可用的音频输入设备:")
        for line in list_input_devices():
            print("  " + line)
        return 0

    import soundfile as sf

    corpus = load_corpus()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        corpus = [c for c in corpus if c["id"] in wanted]
        if not corpus:
            print(f"没有匹配 {args.only!r} 的条目", file=sys.stderr)
            return 1

    out_dir = DATA_DIR / args.label
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = AudioConfig(input_device=args.mic, sample_rate=args.sample_rate)
    recorder = Recorder(cfg)

    print(f"这一轮标签: {args.label}   麦克风: {args.mic or '(系统默认)'}")
    print(f"输出目录:   {out_dir}")
    print(f"共 {len(corpus)} 条")
    print()
    print("提示:按平时说话的语速和口气念,别刻意念稿——")
    print("     刻意念出来的识别率会虚高,天天用起来就会翻车。")

    manifest_path = out_dir / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for item in corpus:
        pcm = record_one(recorder, item)
        if pcm is QUIT:
            break
        if pcm is SKIP:
            continue

        wav_path = out_dir / f"{item['id']}.wav"
        sf.write(str(wav_path), pcm, args.sample_rate, subtype="PCM_16")
        manifest[item["id"]] = {
            "wav": wav_path.name,
            "text": item["text"],
            "terms": item.get("terms", []),
            "note": item.get("note", ""),
            "duration_sec": round(len(pcm) / args.sample_rate, 2),
            "mic": args.mic or "(default)",
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"完成,已录 {len(manifest)} 条 → {out_dir}")
    total = sum(v["duration_sec"] for v in manifest.values())
    print(f"总时长 {total:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
