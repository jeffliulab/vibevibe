#!/usr/bin/env python3
"""三模型对照跑分 —— 阶段 0 的地基。

⚠️ **这是整个项目里最吃 CPU 的一步。** 它会把每个模型对每条录音
都跑一遍推理。默认会先问一句再开跑,别手滑。

用法:
    # 先只跑主选的 0.6B,看够不够用(最省时间的路子)
    ./.venv/bin/python bench/compare.py --label c920 --backends qwen_onnx

    # 全套对照
    ./.venv/bin/python bench/compare.py --label c920 --backends qwen_onnx,whisper_ct2

    # 双麦对照:同一个模型跑两批录音,量化麦克风的影响
    ./.venv/bin/python bench/compare.py --label onboard --backends qwen_onnx

    # 让路给别的任务:少给几个线程 + 降优先级
    nice -n 19 ./.venv/bin/python bench/compare.py --label c920 --threads 2

输出:
    bench/results/<label>_<时间戳>.json   原始数据
    终端上一张对照表

指标里最该看的是**技术词命中率**——中英混说的典型失败是
把英文词音译成中文,那种错在 CER 上看着不严重,实际完全没法用。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from bench import metrics  # noqa: E402
from vibevibe.config import load_config  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent
DATA_DIR = BENCH_DIR / "data"
RESULTS_DIR = BENCH_DIR / "results"


def load_manifest(label: str) -> dict:
    path = DATA_DIR / label / "manifest.json"
    if not path.exists():
        raise SystemExit(
            f"找不到录音清单 {path}\n"
            f"先录语料: ./.venv/bin/python bench/record.py --label {label}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def load_wav(path: Path, sample_rate: int) -> np.ndarray:
    import soundfile as sf

    pcm, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if pcm.ndim > 1:
        pcm = pcm.mean(axis=1)
    if sr != sample_rate:
        import librosa

        pcm = librosa.resample(pcm, orig_sr=sr, target_sr=sample_rate)
    return pcm.astype(np.float32)


def run_backend(backend_name: str, cfg, manifest: dict, label: str,
                preproc: bool = False) -> dict:
    from vibevibe.asr import build_backend
    from vibevibe.audio_preproc import preprocess

    backend = build_backend(cfg, backend_name)
    tag = "预处理开" if preproc else "预处理关"
    print(f"\n{'=' * 72}")
    print(f"后端: {backend.name}   [{tag}]")
    print("=" * 72)

    print("加载模型……", end="", flush=True)
    t0 = time.time()
    try:
        backend.load()
    except Exception as exc:
        print(f" 失败\n  ✗ {exc}")
        return {"backend": backend_name, "error": str(exc), "items": []}
    load_s = time.time() - t0
    print(f" 用时 {load_s:.1f}s")

    items = []
    for item_id, meta in manifest.items():
        wav_path = DATA_DIR / label / meta["wav"]
        if not wav_path.exists():
            print(f"  ! {item_id}: 录音文件不见了 {wav_path}")
            continue

        pcm = load_wav(wav_path, backend.sample_rate)
        pp_info = {}
        if preproc:
            pcm, pp_info = preprocess(pcm, backend.sample_rate, cfg.preprocess_audio)
        try:
            result = backend.transcribe(pcm)
        except Exception as exc:
            print(f"  ✗ {item_id}: {type(exc).__name__}: {exc}")
            items.append({"id": item_id, "error": f"{type(exc).__name__}: {exc}"})
            continue

        ref, hyp = meta["text"], result.text
        terms = meta.get("terms", [])
        hits, missed = metrics.term_hits(terms, hyp)

        row = {
            "id": item_id,
            "reference": ref,
            "hypothesis": hyp,
            "language": result.language,
            "duration_sec": result.timing.audio_duration_s,
            "total_sec": result.timing.total_s,
            "rtf": result.timing.rtf,
            "tokens": result.timing.tokens_generated,
            "cer": metrics.cer(ref, hyp),
            "wer": metrics.wer(ref, hyp),
            "terms_total": len(terms),
            "terms_hit": hits,
            "terms_missed": missed,
            "repetition": metrics.has_repetition(hyp),
            "guard_note": result.guard_note,
            "preproc": pp_info,
        }
        items.append(row)

        flag = ""
        if missed:
            flag += f"  ✗漏词: {', '.join(missed)}"
        if row["repetition"]:
            flag += "  ⚠检测到重复"
        if result.guard_note:
            flag += f"  ⚠闸门: {result.guard_note}"
        print(f"  {item_id:<14} CER {row['cer']:.3f}  RTF {row['rtf']:.2f}  "
              f"{row['duration_sec']:.1f}s{flag}")
        print(f"    → {hyp}")

    backend.unload()
    return {
        "backend": backend_name,
        "backend_id": backend.name,
        "preproc": preproc,
        "label": f"{backend_name}{'+预处理' if preproc else ''}",
        "load_sec": load_s,
        "items": items,
    }


def summarize(run: dict) -> dict:
    rows = [r for r in run["items"] if "error" not in r]
    if not rows:
        return {}

    cers = [r["cer"] for r in rows]
    wers = [r["wer"] for r in rows if not np.isnan(r["wer"])]
    rtfs = [r["rtf"] for r in rows]
    terms_total = sum(r["terms_total"] for r in rows)
    terms_hit = sum(r["terms_hit"] for r in rows)

    return {
        "n": len(rows),
        "cer_mean": float(np.mean(cers)),
        "wer_mean": float(np.mean(wers)) if wers else float("nan"),
        "term_rate": terms_hit / terms_total if terms_total else float("nan"),
        "terms_hit": terms_hit,
        "terms_total": terms_total,
        "rtf_mean": float(np.mean(rtfs)),
        "rtf_max": float(np.max(rtfs)),
        "total_sec": float(np.sum([r["total_sec"] for r in rows])),
        "audio_sec": float(np.sum([r["duration_sec"] for r in rows])),
        "load_sec": run.get("load_sec", 0.0),
        "repetitions": sum(1 for r in rows if r["repetition"]),
        "errors": len(run["items"]) - len(rows),
    }


def print_table(runs: list[dict]) -> None:
    print(f"\n{'=' * 96}")
    print("对照表")
    print("=" * 96)
    header = (f"{'后端':<22} {'条数':>4} {'技术词命中':>10} {'CER':>7} "
              f"{'英文WER':>8} {'平均RTF':>8} {'最差RTF':>8} {'加载s':>7} {'跑飞':>5}")
    print(header)
    print("-" * 96)
    for run in runs:
        s = summarize(run)
        if not s:
            print(f"{run.get('label', run['backend']):<22} (没有可用结果: {run.get('error', '全部失败')})")
            continue
        term_str = (f"{s['term_rate'] * 100:.0f}% ({s['terms_hit']}/{s['terms_total']})"
                    if s["terms_total"] else "—")
        wer_str = f"{s['wer_mean']:.3f}" if not np.isnan(s["wer_mean"]) else "—"
        print(f"{run.get('label', run['backend']):<22} {s['n']:>4} {term_str:>10} {s['cer_mean']:>7.3f} "
              f"{wer_str:>8} {s['rtf_mean']:>8.2f} {s['rtf_max']:>8.2f} "
              f"{s['load_sec']:>7.1f} {s['repetitions']:>5}")
    print("-" * 96)
    print("技术词命中 = 嵌在中文里的英文词有没有被识别成英文(而不是音译成中文)。")
    print("            这是中英混说场景下最该看的指标。")
    print("RTF        = 处理耗时 / 音频时长。小于 1 表示比实时快。")


def main() -> int:
    parser = argparse.ArgumentParser(description="vibevibe 三模型对照跑分")
    parser.add_argument("--label", required=True, help="用哪一批录音(bench/data/<label>)")
    parser.add_argument("--backends", default="qwen_onnx",
                        help="逗号分隔: qwen_onnx,whisper_ct2,qwen_hf")
    parser.add_argument("--threads", type=int, default=None,
                        help="覆盖推理线程数。给小值可以少占 CPU")
    parser.add_argument("--config", default=None)
    parser.add_argument("--preproc", default="off", choices=["off", "on", "both"],
                        help="录音预处理(削顶重建+归一化)。both = 开关各跑一遍做 A/B,"
                             "用数据证明预处理到底有没有用")
    parser.add_argument("--yes", action="store_true", help="跳过开跑前的确认")
    args = parser.parse_args()

    cfg = load_config(Path(args.config) if args.config else None)
    if args.threads is not None:
        cfg.asr.qwen_onnx.intra_op_num_threads = args.threads
        cfg.asr.whisper_ct2.cpu_threads = args.threads

    manifest = load_manifest(args.label)
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    audio_sec = sum(v.get("duration_sec", 0) for v in manifest.values())

    print(f"录音批次   {args.label}({len(manifest)} 条,共 {audio_sec:.0f}s 音频)")
    print(f"参评后端   {', '.join(backends)}")
    print(f"预处理     {args.preproc}"
          + ("(开关各跑一遍做 A/B,耗时翻倍)" if args.preproc == "both" else ""))
    print(f"推理线程   {cfg.asr.qwen_onnx.intra_op_num_threads}"
          f"(0 表示用满所有核)")
    print()
    print("⚠ 这一步会真正跑推理,是整个项目最吃 CPU 的部分。")

    if not args.yes:
        if input("确认开始?(y/N) ").strip().lower() not in ("y", "yes"):
            print("已取消,什么都没跑。")
            return 0

    if args.preproc == "off":
        modes = [False]
    elif args.preproc == "on":
        modes = [True]
    else:
        modes = [False, True]
    if args.preproc != "off":
        cfg.preprocess_audio.enabled = True

    runs = []
    t_start = time.time()
    for name in backends:
        for pp in modes:
            runs.append(run_backend(name, cfg, manifest, args.label, preproc=pp))

    print_table(runs)
    print(f"\n总耗时 {time.time() - t_start:.1f}s")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = RESULTS_DIR / f"{args.label}_{stamp}.json"
    out_path.write_text(json.dumps({
        "label": args.label,
        "timestamp": stamp,
        "threads": cfg.asr.qwen_onnx.intra_op_num_threads,
        "preproc_mode": args.preproc,
        "runs": runs,
        "summary": {r.get("label", r["backend"]): summarize(r) for r in runs},
    }, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"原始结果 → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
