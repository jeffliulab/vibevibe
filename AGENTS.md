# Project Agent Entry — vibevibe

This repository inherits shared engineering rules from `agent-rules`.

## Version Pin

- `agent-rules` version: `v0.3.0`
- upstream machine entry: [agent-rules/AGENTS.md](https://github.com/jeffliulab/agent-rules/blob/v0.3.0/AGENTS.md)
- upstream retrieval index: [_skeleton.md](https://github.com/jeffliulab/agent-rules/blob/v0.3.0/_skeleton.md)

## Read Order

1. Read this file first for project-local overrides.
2. Then load the pinned upstream `_skeleton.md` and Read only the nodes that
   actually apply to the task at hand (target ≤ 5 file reads).
3. Apply local rules after the shared upstream rules.

## What This Project Is

Offline Chinese-English code-switching dictation for Linux. A Python package
published to PyPI, plus a systemd user service, a GTK tray icon and a settings
window. **Not** an ML training project — no training loop, no dataset, no
checkpoints. It only runs inference on pre-published third-party weights.

Consequently the ML-research and HuggingFace-release paths upstream do **not**
apply. The relevant upstream nodes are `global`, `principles.engineering`,
`workflows.git` and `workflows.github`.

## Local Overrides

- **Comments and docstrings are Simplified Chinese; all identifiers are English.**
  This follows upstream `GLOBAL.md` and is not negotiable per-file.
- **Nothing is hardcoded.** Every threshold, path, device name and tuning knob
  lives in `vibevibe/config.py` with a comment explaining how its default was
  measured. If a value cannot be derived, say so explicitly rather than
  burying a magic number.
- **Unknown config keys are a hard error**, never a silent fallback. A typo that
  is silently ignored leaves the user believing a setting took effect.
- **Measured, not assumed.** Any claim about accuracy, latency or memory in the
  README or in code comments must come from a reproducible measurement in
  `bench/`. Do not publish numbers taken from upstream papers as if they were
  measured here.
- **Never auto-`sudo`.** Steps requiring root print the command for the user to
  run. This applies to `vibevibe setup` and to any agent working in this repo.
- **Recordings under `bench/data/` are the maintainer's own voice** and must
  never be committed or published. They are gitignored; keep it that way.
- **Secrets stay out of the repo and out of chat.** PyPI tokens belong in
  `~/.pypirc` (mode 600) or a keyring, never in a file under version control.

## Platform Constraints

- Linux + X11 only. Wayland support is unimplemented, not merely untested —
  do not claim otherwise in docs.
- CPU inference only. Do not introduce a CUDA or PyTorch dependency into the
  runtime path; `qwen_hf` is an optional extra and must stay optional.

## Notes

- Do not point this repository at `agent-rules@main`; upgrade by bumping the
  pinned tag and re-validating.
- Local rules win on conflict with upstream.
