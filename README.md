[![Language: English](https://img.shields.io/badge/Language-English-2f81f7?style=flat-square)](README.md) [![语言: 简体中文](https://img.shields.io/badge/语言-简体中文-e67e22?style=flat-square)](README_zh.md)

# vibevibe

[![PyPI](https://img.shields.io/pypi/v/vibevibe?style=flat-square&color=3775a9)](https://pypi.org/project/vibevibe/)
[![Python](https://img.shields.io/pypi/pyversions/vibevibe?style=flat-square)](https://pypi.org/project/vibevibe/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20%C2%B7%20X11-lightgrey?style=flat-square)](#requirements)
[![Status](https://img.shields.io/badge/status-alpha-orange?style=flat-square)](#status)

**Offline Chinese-English code-switching dictation for Linux. Press a key, speak, and the text lands at your cursor — nothing leaves your machine.**

[PyPI](https://pypi.org/project/vibevibe/) · [Releases](https://github.com/jeffliulab/vibevibe/releases) · [Issues](https://github.com/jeffliulab/vibevibe/issues)

---

## Highlights

- **Built for mixed Chinese-English speech.** Powered by [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR), the only open ASR family explicitly trained for code-switching — 40 000+ English keywords were injected into its Chinese training data.
- **CPU only.** No CUDA, no PyTorch, no VRAM. Runs on ONNX Runtime, so dictation keeps working while your GPU is busy training something else.
- **Fully offline.** No account, no API key, no network calls at runtime.
- **0.4–0.8 s from key-up to text**, measured on a Ryzen 7 9800X3D with 4 inference threads.
- **Memory you control.** A tray switch flips between "model resident" (~3.9 GB) and "load on demand" (~0.24 GB); turning the service off frees everything.
- **Bilingual UI.** Interface and logs in English or Simplified Chinese, switchable at runtime.

---

## Table of Contents

- [Why this exists](#why-this-exists)
- [Benchmarks](#benchmarks)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Usage](#usage)
- [Configuration](#configuration)
- [Project Structure](#project-structure)
- [Run the benchmark yourself](#run-the-benchmark-yourself)
- [Troubleshooting](#troubleshooting)
- [Status](#status)
- [Links](#links)
- [Acknowledgments](#acknowledgments)

---

## Why this exists

Typing is the bottleneck when working with coding agents. Dictation solves that — unless you speak the way many engineers do: **Chinese sentences with English technical terms embedded in them**. That single constraint rules out nearly everything:

| Option | Why it does not work |
| --- | --- |
| Claude Code's built-in `/voice` | Audio is uploaded for transcription, and Chinese is not among its supported dictation languages |
| Open-source Linux dictation tools (Handy, hyprwhspr, VoxType, OpenWhispr, whisrs …) | All of them wrap whisper.cpp / Parakeet / Vosk. None supports Qwen3-ASR |
| Commercial apps (Wispr Flow, superwhisper, VoiceInk …) | macOS / Windows only. Linux is an abandoned market |

`Linux + offline + code-switching` was an empty intersection. vibevibe fills it.

---

## Benchmarks

Measured on **12 recordings of the author's own speech** containing **30 technical terms**, captured with a webcam microphone at maximum gain — i.e. realistic conditions including clipping, not a clean lab recording.

| Backend | Technical-term hit rate | CER | English WER | Mean RTF |
| --- | --- | --- | --- | --- |
| **Qwen3-ASR 1.7B int4** (default) | **77 %** (23/30) | **0.082** | **0.213** | 0.15 |
| Qwen3-ASR 0.6B int8 | 70 % (21/30) | 0.132 | 0.309 | 0.08 |
| Whisper large-v3-turbo int8 | 60 % (18/30) | 0.143 | 0.503 | 0.50 |

**Technical-term hit rate is the metric that matters here.** The characteristic failure of code-switching ASR is transliterating an English word into Chinese characters: the sentence still reads fluently, so CER barely moves, but the output is unusable. In one measured sample `commit` / `push` / `remote` were *all* transliterated and CER was still only 0.47.

`RTF` = processing time ÷ audio duration. Lower is faster; 0.15 means an 8-second utterance is transcribed in about 1.2 s.

<details>
<summary>Terms that still fail on every model tested, Whisper included</summary>

`MuJoCo`, `CAN` (a homophone of a common Chinese word), `daemon`, `Claude Code`, `checkpoint`, `benchmark`.

These are proper nouns whose pronunciation is genuinely ambiguous inside a Chinese sentence. Qwen3-ASR supports context biasing — a hotword list placed in the system prompt — which should fix most of them. It is not wired up yet.

</details>

<details>
<summary>Why audio preprocessing is disabled by default</summary>

The repo contains a de-clipping stage that reconstructs clipped waveform peaks. A controlled experiment (clean speech segments, artificially clipped, then repaired) showed **+1.7 dB SNR at the clipping level of the real recordings, with zero samples made worse**.

It still ships **off**, because an A/B benchmark showed the gain is model-specific:

| Backend | Technical terms | Key metric |
| --- | --- | --- |
| 0.6B | 67 % → **70 %** | English WER 0.774 → **0.309** |
| 1.7B | 77 % → 77 % | CER 0.082 → 0.086 (slightly worse) |
| Whisper | 60 % → 60 % | WER 0.503 → 0.526 (slightly worse) |

A strong enough model absorbs the distortion by itself, and "repairing" it afterwards is counterproductive. Worth re-testing after changing microphones: `bench/compare.py --preproc both`.

</details>

---

## Requirements

| | |
| --- | --- |
| OS | Linux with **X11** (Wayland not supported yet) |
| Python | 3.11+ |
| Disk | ~4 GB for model weights |
| RAM | ~4 GB while the model is resident, ~0.25 GB otherwise |
| GPU | **Not required and not used** |

System packages, installed by your package manager rather than pip:

```bash
sudo apt install xdotool xclip                                  # text injection
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1     # tray icon (optional)
```

On GNOME 46+ the tray icon needs a shell extension. Ubuntu ships `ubuntu-appindicators` and enables it by default.

---

## Quick Start

```bash
pip install vibevibe
vibevibe setup
```

`setup` handles everything pip cannot. It asks before each step, it is idempotent, and **it never runs `sudo` for you** — if a system package is missing it prints the command for you to run.

```
[1/7] Python dependencies
[2/7] System tools
[3/7] Config file            ~/.config/vibevibe/config.toml
[4/7] Model weights          ~4 GB
[5/7] Desktop shortcut      default: Super+Shift+V
[6/7] Autostart service      systemd --user
[7/7] Tray icon
```

Then press **Super+Shift+V**, say something, press it again. The text appears at your cursor.

---

## Triggering

Two independent channels, both active at once:

| Channel | Key | Needs | For |
| --- | --- | --- | --- |
| **Desktop shortcut** | `Super+Shift+V` (configurable) | **nothing** — no hardware, no permissions | anyone, right after `pip install` |
| **Keypad** | `F19` (fixed) | a two-key macropad + one udev rule scoped to it | people with the dedicated hardware |

The keypad channel reads the input device directly, so it can use F13–F24 — keys that
desktop shortcuts cannot bind at all. The macropad's firmware is flashed with F19 too,
so it works on any machine without installing anything.

**Why `Super+Shift+V` and not `Ctrl+Shift+V`:** the latter is "paste as plain text" in
browsers, VS Code and terminals. Those are *in-application* bindings, invisible to any
system-level check. Applications rarely touch Super — it belongs to the desktop
environment — so Super combos are the safe choice.

Settings → **Keys** lets you rebind either channel by pressing the key you want
(combos included). Every candidate is checked for conflicts first:

- already bound by a system shortcut → **rejected**
- already bound by vibevibe's own shortcut → **rejected** (both channels would fire, and
  in toggle mode that cancels out — it looks like nothing happened)
- has an X11 keysym → **allowed with a warning**, because it will also reach the focused
  application

## Usage

### Dictating

| Action | Result |
| --- | --- |
| Press the trigger key | Recording starts — short high beep |
| Press it again | Text is transcribed and pasted at the cursor — lower beep |
| Something went wrong | A distinctly lower, longer beep, and **nothing is pasted** |

Feedback is audible rather than visual on purpose: while dictating you are looking at the text field, not at the top of the screen.

### Tray icon

A double-**V** icon appears in the top bar, acting as both control panel and status indicator:

| Icon | Meaning |
| --- | --- |
| Dimmed V | Service stopped |
| Bright V | Idle, ready |
| V + red dot | Recording |
| V + amber dot | Transcribing |
| V + hollow ring | Idle, model not loaded — next press takes ~2 s longer |

Two switches:

| Switch | Effect | Memory |
| --- | --- | --- |
| **Service** | Starts / stops the daemon entirely | 3.9 GB → **0** (only the 44 MB tray remains) |
| **Hot reload** | Whether the model stays in RAM | on 3.9 GB / off **0.24 GB** |

The menu also has **About**, which shows the version, the paths in use, and a button to open the log.

> The tray is a **separate process** from the daemon. It has to be — otherwise "stop the service" would kill the very thing you use to start it again.

### Settings

Tray → **Settings…**, or `vibevibe settings`.

| Tab | Contains |
| --- | --- |
| General | Interface & log language, recognition model, microphone |
| Keys | Trigger mode, desktop shortcut, keypad device and trigger key — with conflict checking |
| Performance | Hot reload, idle-unload timeout, inference threads |
| Feedback | Sound cues and volume (with a test button), text injection method |
| Advanced | Audio preprocessing, **Open full config file** |

Changes are **staged, not applied immediately**. Nothing touches the config file until you press **Save**; **Cancel** means nothing ever happened. The footer shows `● N unsaved change(s)`, and closing the window with pending changes asks first.

### Command line

```bash
vibevibe toggle      # start/stop recording — this is what the hotkey runs
vibevibe status      # daemon state, memory, last transcription
vibevibe doctor      # check dependencies, weights, config (loads no model)
vibevibe devices     # list microphones and keyboard devices
vibevibe settings    # open the settings window
vibevibe tray        # start the tray icon
vibevibe daemon      # run the daemon in the foreground, for debugging
```

The daemon normally runs under systemd:

```bash
systemctl --user status vibevibe
journalctl --user -u vibevibe -f
```

---

## Configuration

`~/.config/vibevibe/config.toml`. The settings window covers the common options; the file holds all 70-plus, each with a comment explaining how its default was measured.

**Unknown keys make the program fail loudly rather than being ignored** — a silently dropped typo would leave you believing a setting took effect when it did not.

The two knobs most worth knowing:

```toml
[daemon]
# Keep the model in RAM. Off = load on demand, ~0.24 GB, +1.7~2.5 s per use.
hot_reload = true

[asr.qwen_onnx_1p7b]
# 0 = use every core. This is the main CPU-usage dial.
intra_op_num_threads = 4
```

---

## Project Structure

```
vibevibe/
  config.py           every tunable lives here; nothing is hardcoded elsewhere
  asr/                pluggable recognition backends
    qwen_onnx.py        Qwen3-ASR via ONNX Runtime — the default
    whisper_ct2.py      Whisper large-v3-turbo — comparison baseline
    qwen_hf.py          Qwen3-ASR via PyTorch — optional, needs torch
  recorder.py         audio capture
  audio_preproc.py    de-clipping, off by default (see Benchmarks)
  guard.py            blocks the known Qwen3-ASR runaway-repetition bug
  daemon.py           state machine + Unix socket server
  hotkey_evdev.py     raw input-device channel, for a dedicated macropad
  inject.py           puts text at the cursor
  sound.py            audio cues, synthesized — no asset files
  tray.py             system tray icon
  settings_dialog.py  GTK settings window
  setup_wizard.py     `vibevibe setup`
  i18n.py             English / Chinese strings
  data/               config template, udev rule, systemd unit, icons
bench/                model comparison tooling
  corpus.toml           sentences to read aloud
  record_gui.py         recording UI with live waveform
  compare.py            benchmark runner
  metrics.py            CER / WER / technical-term hit rate
```

---

## Run the benchmark yourself

The published numbers came from one person's voice and speaking habits. Yours will differ, so the tooling is included — measure instead of trusting:

```bash
python bench/record_gui.py                         # record the corpus (GUI, live waveform)
python bench/compare.py --label mine \
    --backends qwen_onnx_1p7b,qwen_onnx \
    --preproc both --threads 4
```

A two-backend comparison over 12 clips takes about a minute.

---

## Troubleshooting

<details>
<summary>Nothing happens when I press the key</summary>

```bash
vibevibe status
systemctl --user status vibevibe
```

If the daemon is healthy, the hotkey binding probably is not. Check GNOME Settings → Keyboard → Custom Shortcuts, or re-run `vibevibe setup`.

</details>

<details>
<summary>Speech is recognized but no text appears</summary>

`xdotool` or `xclip` is missing. `vibevibe doctor` will say so, and the daemon logs a warning at startup.

</details>

<details>
<summary>Recognition quality is poor</summary>

Check the microphone first — it is usually the microphone. Record a clip with `bench/record_gui.py` and watch the live waveform: if it slams against the ceiling, lower the ALSA capture gain (`alsamixer`, or `amixer -c N sset Mic 48`). A cheap dedicated microphone beats any amount of model tuning.

</details>

<details>
<summary>The tray icon does not appear</summary>

```bash
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1
gnome-extensions list --enabled | grep appindicator
```

GNOME 46+ removed built-in tray support; an extension must provide it.

</details>

---

## Status

Alpha. Working and used daily by the author, but young.

- [x] CPU inference, model choice backed by measurements
- [x] Hotkey channel via GNOME shortcuts
- [x] Tray icon, settings window, bilingual UI
- [x] systemd user service, PyPI release
- [x] **Two-key macropad** — keys flashed over the VIA protocol (left → F19, right → Enter), verified on real hardware
- [ ] Wayland support
- [ ] Context biasing to fix the remaining proper nouns

---

## Links

- **PyPI** — https://pypi.org/project/vibevibe/
- **Source** — https://github.com/jeffliulab/vibevibe
- **Qwen3-ASR** — [paper](https://arxiv.org/abs/2601.21337) · [repo](https://github.com/QwenLM/Qwen3-ASR)

---

## Acknowledgments

The ONNX pipeline is adapted from the reference implementation shipped with
[Daumee/Qwen3-ASR-0.6B-ONNX-CPU](https://huggingface.co/Daumee/Qwen3-ASR-0.6B-ONNX-CPU) (Apache-2.0). It is not vendored here; fetch it if you want to compare:

```bash
curl -O https://huggingface.co/Daumee/Qwen3-ASR-0.6B-ONNX-CPU/raw/main/onnx_inference.py
```

Model weights: [andrewleech/qwen3-asr-1.7b-onnx](https://huggingface.co/andrewleech/qwen3-asr-1.7b-onnx) ·
[Daumee/Qwen3-ASR-0.6B-ONNX-CPU](https://huggingface.co/Daumee/Qwen3-ASR-0.6B-ONNX-CPU) ·
[Zoont/faster-whisper-large-v3-turbo-int8-ct2](https://huggingface.co/Zoont/faster-whisper-large-v3-turbo-int8-ct2)

Licensed under Apache-2.0.
