"""命令行入口。

装完 pip 之后先跑一次:
    vibevibe setup      装剩下的东西(权重、配置、快捷键、开机自启)

托盘图标(两个开关:服务开关、热加载开关):
    vibevibe tray

日常只会用到两条:
    vibevibe daemon     起守护进程(模型常驻在它内存里)
    vibevibe toggle     开始/停止录音 —— GNOME 快捷键绑的就是这条

不想让它跑了:
    vibevibe quit       停掉服务和守护进程,内存全部还给系统
                        (托盘图标是独立进程,用它菜单里的「退出 vibevibe」)

其余都是排查用的:
    vibevibe status     看守护进程在干嘛、上一句识别成了什么
    vibevibe devices    列出音频输入设备和输入(键盘)设备
    vibevibe doctor     体检:依赖齐不齐、权重在不在、配置有没有问题
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, ipc
from .config import Config, load_config


def _load(args: argparse.Namespace) -> Config:
    return load_config(Path(args.config) if args.config else None)


def _print_response(resp: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
    else:
        if resp.get("ok"):
            state = resp.get("state", "")
            note = resp.get("note", "")
            print(f"状态: {state}" + (f" ({note})" if note else ""))
        else:
            print(f"失败: {resp.get('error', '(没说原因)')}", file=sys.stderr)
    return 0 if resp.get("ok") else 1


# ── 子命令 ──────────────────────────────────────────────────────────────


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import Daemon, setup_logging

    cfg = _load(args)
    setup_logging(cfg, to_console=not args.quiet)
    return Daemon(cfg).run()


def cmd_simple(args: argparse.Namespace) -> int:
    cfg = _load(args)
    try:
        resp = ipc.command(cfg.socket_path, args.command)
    except ipc.IpcError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return _print_response(resp, args.json)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load(args)
    try:
        resp = ipc.command(cfg.socket_path, ipc.CMD_STATUS)
    except ipc.IpcError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return 0

    labels = [
        ("状态", "state"), ("后端", "backend"), ("进程号", "pid"),
        ("配置文件", "config"), ("热键通道", "hotkey"),
        ("模型在内存", "model_in_memory"), ("占用内存MB", "memory_mb"),
        ("当前已录", "recording_sec"), ("上次来源", "last_source"),
        ("上次时长", "last_duration_sec"),
        ("上次 RTF", "last_rtf"), ("上次结果", "last_text"),
        ("上次错误", "last_error"),
    ]
    for label, key in labels:
        value = resp.get(key)
        if value not in (None, "", 0):
            print(f"{label:<10} {value}")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    if args.input or not args.audio:
        print("=== 输入设备(键盘 / 小键盘) ===")
        print("配置 hotkey.device 时**必须**用下面的稳定路径,不要用 /dev/input/eventN\n")
        try:
            from .hotkey_evdev import list_devices

            for line in list_devices():
                print("  " + line)
        except Exception as exc:
            print(f"  列举失败: {exc}")
        print()

    if args.audio or not args.input:
        print("=== 音频输入设备 ===")
        try:
            from .recorder import list_input_devices

            for line in list_input_devices():
                print("  " + line)
        except Exception as exc:
            print(f"  列举失败: {exc}")
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    from .settings_dialog import main as settings_main

    return settings_main()


def cmd_quit(args: argparse.Namespace) -> int:
    """把 vibevibe 整个停掉:服务 + 守护进程。

    和 `vibevibe shutdown` 的区别:shutdown 只发一条 IPC 让守护进程自己退,
    而它一退,systemd 那边 `Restart=on-failure` 很可能几秒后又把它拉回来。
    quit 是先停 unit 再兜底发 IPC,两条路都堵上。
    """
    from . import service

    cfg = _load(args)
    ok, notes = service.stop_all(cfg)

    if args.json:
        print(json.dumps({"ok": ok, "notes": notes}, ensure_ascii=False, indent=2))
    else:
        for note in notes:
            print(f"  {note}")
        print("vibevibe 已停止" if ok else "没能完全停下,详见上面")
    return 0 if ok else 1


def cmd_tray(args: argparse.Namespace) -> int:
    from .tray import run_tray

    return run_tray(Path(args.config) if args.config else None)


def cmd_setup(args: argparse.Namespace) -> int:
    from .setup_wizard import run_setup

    return run_setup(
        auto_yes=args.yes,
        key=args.key,
        skip_weights=args.skip_weights,
        skip_hotkey=args.skip_hotkey,
        skip_service=args.skip_service,
        skip_tray=args.skip_tray,
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    """体检。只做检查,不加载模型、不跑推理——这条命令必须是零成本的。"""
    import shutil

    cfg = _load(args)
    problems = 0

    from .config import CONFIG_HOME, STATE_HOME, data_root, is_source_checkout

    print(f"vibevibe {__version__}")
    print(f"运行方式      {'源码目录' if is_source_checkout() else 'pip 已安装'}")
    print(f"配置文件      {cfg.source_path or '(全默认值)'}")
    print(f"数据目录      {data_root()}")
    print(f"状态目录      {STATE_HOME}")
    print()

    print("── 系统工具 ──")
    for tool, why in (
        (cfg.inject.type_tool, "把文字打进光标处"),
        (cfg.inject.clipboard_tool, "读写剪贴板"),
    ):
        path = shutil.which(tool)
        if path:
            print(f"  ✓ {tool:<10} {path}")
        else:
            problems += 1
            print(f"  ✗ {tool:<10} 没装 —— {why}。装: sudo apt install {tool}")
    print()

    print("── Python 依赖 ──")
    # required=False 的是可选依赖,缺了不算问题——
    # 把可选的算成"问题"会让每次体检都是红的,红久了就没人看了
    for mod, why, required in (
        ("numpy", "数组运算", True),
        ("onnxruntime", "Qwen3-ASR ONNX 推理", True),
        ("librosa", "mel 频谱", True),
        ("soundfile", "读写 wav", True),
        ("tokenizers", "分词", True),
        ("sounddevice", "录音和提示音", True),
        ("evdev", "F19 通道", True),
        ("huggingface_hub", "setup 下载权重", True),
        ("faster_whisper", "Whisper 对照基线(可选,跑分才用)", False),
        ("scipy", "音频预处理的滤波(可选)", False),
    ):
        try:
            __import__(mod)
            print(f"  ✓ {mod:<16} {why}")
        except ImportError:
            if required:
                problems += 1
                print(f"  ✗ {mod:<16} 没装 —— {why}")
            else:
                print(f"  · {mod:<16} 没装 —— {why}")
    print()

    print("── 模型权重 ──")
    try:
        model_dir = cfg.backend_model_dir()
        if model_dir.is_dir():
            size_gb = sum(
                f.stat().st_size for f in model_dir.rglob("*") if f.is_file()
            ) / 1e9
            print(f"  ✓ {cfg.asr.backend}: {model_dir} ({size_gb:.2f} GB)")
        else:
            problems += 1
            print(f"  ✗ {cfg.asr.backend}: 目录不存在 {model_dir}")
            print("    → 跑 `vibevibe setup` 自动下载")
    except Exception as exc:
        problems += 1
        print(f"  ✗ 检查失败: {exc}")
    print()

    print("── 配置检查 ──")
    if cfg.asr.language:
        print(f"  ! asr.language = {cfg.asr.language!r} —— "
              "指定了语言会把模型钉死在一种语言上,中英混说要留空走自动检测")
    else:
        print("  ✓ asr.language 留空(自动检测),中英混说需要这样")

    if cfg.hotkey.enabled:
        if not cfg.hotkey.device:
            problems += 1
            print("  ✗ hotkey.enabled = true 但 hotkey.device 没填")
        elif not Path(cfg.hotkey.device).exists():
            print(f"  ! hotkey.device 当前不存在: {cfg.hotkey.device}(设备没插上?)")
        else:
            print(f"  ✓ hotkey.device {cfg.hotkey.device}")
        try:
            from .hotkey_conflict import check as check_conflicts

            for c in check_conflicts(cfg.hotkey.key):
                problems += 1 if c.blocker else 0
                mark = "✗" if c.blocker else "!"
                print(f"  {mark} 触发键冲突: {c.title}")
                for line in c.detail.splitlines():
                    print(f"      {line}")
            else:
                if not check_conflicts(cfg.hotkey.key):
                    print(f"  ✓ 触发键 {cfg.hotkey.key} 无冲突")
        except Exception as exc:
            print(f"  · 冲突检查跳过: {exc}")

        if cfg.hotkey.grab:
            print(f"  ! hotkey.grab = true —— 只应对专用小键盘开启"
                  f"(安全闸: 按键数 > {cfg.hotkey.grab_max_keys} 会拒绝独占)")
    else:
        print("  · hotkey 未启用(只走组合键通道)")
    print()

    print(f"{'一切正常' if problems == 0 else f'发现 {problems} 个问题'}")
    return 0 if problems == 0 else 1


# ── 参数解析 ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibevibe",
        description="本地中英混说语音听写 —— 说一下,按一下。",
    )
    parser.add_argument("--version", action="version", version=f"vibevibe {__version__}")
    parser.add_argument("-c", "--config", help="指定配置文件路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")

    sub = parser.add_subparsers(dest="subcommand", required=True)

    p = sub.add_parser(
        "setup", help="安装向导:权重、配置、快捷键、开机自启(装完 pip 先跑这个)")
    p.add_argument("-y", "--yes", action="store_true", help="全部默认同意,不逐步询问")
    p.add_argument("--key", default="<Super><Shift>v",
                   help="桌面快捷键绑哪个组合(默认 <Super><Shift>v)")
    p.add_argument("--skip-weights", action="store_true", help="不下载模型权重")
    p.add_argument("--skip-hotkey", action="store_true", help="不绑快捷键")
    p.add_argument("--skip-service", action="store_true", help="不装 systemd 服务")
    p.add_argument("--skip-tray", action="store_true", help="不装托盘图标")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("settings", help="打开设置窗口(语言、模型、内存策略、提示音)")
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("tray", help="启动系统托盘图标(服务开关 / 热加载开关)")
    p.set_defaults(func=cmd_tray)

    p = sub.add_parser("daemon", help="启动守护进程(模型常驻其中)")
    p.add_argument("-q", "--quiet", action="store_true", help="不往终端打日志")
    p.set_defaults(func=cmd_daemon)

    for name, help_text in (
        ("toggle", "开始/停止录音(快捷键绑这条)"),
        ("start", "开始录音"),
        ("stop", "停止录音并转写"),
        ("cancel", "放弃当前录音,不转写"),
        ("ping", "看守护进程还活着没"),
        ("shutdown", "让守护进程自己退(systemd 可能几秒后拉回来,要彻底停用 quit)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=cmd_simple, command=name)

    p = sub.add_parser("quit", help="退出 vibevibe:停掉服务和守护进程(托盘另外关)")
    p.set_defaults(func=cmd_quit)

    p = sub.add_parser("status", help="查看守护进程状态和上次结果")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("devices", help="列出音频输入设备和键盘设备")
    p.add_argument("--audio", action="store_true", help="只列音频设备")
    p.add_argument("--input", action="store_true", help="只列输入(键盘)设备")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("doctor", help="体检:依赖、权重、配置(不加载模型)")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
