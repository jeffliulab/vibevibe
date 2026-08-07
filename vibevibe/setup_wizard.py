"""`vibevibe setup` —— 装完 pip 之后,把 pip 干不了的那些活儿一次做完。

pip 只能装 Python 代码和 Python 依赖。剩下这些它管不了:

    系统工具   xdotool / xclip —— 得用 apt,而且要 sudo
    模型权重   约 4GB —— 不该塞进 pip 包
    配置文件   要写到 ~/.config/vibevibe/
    快捷键     GNOME 的 gsettings
    开机自启   systemd --user 服务

所以就有了这条命令。原则:

  · **每一步都先问你**,`--yes` 才跳过确认
  · **绝不自己 sudo**。需要 root 的事情(就 apt 那一件)只把命令打出来,你自己跑
  · **幂等**。已经装好的步骤会跳过,重复跑不会搞坏东西
  · 任何一步都能单独跳过(`--skip-weights` 之类)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import (
    CONFIG_HOME,
    EXAMPLE_CONFIG_PATH,
    ICONS_DIR,
    SERVICE_TEMPLATE,
    STATE_HOME,
    TRAY_DESKTOP_TEMPLATE,
    Config,
    data_root,
    is_source_checkout,
    load_config,
)
from .service import SERVICE as SERVICE_NAME

# GNOME 自定义快捷键存放的 dconf 路径
GNOME_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GNOME_CUSTOM_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/vibevibe/"
)
GNOME_CUSTOM_SCHEMA = f"{GNOME_KEYS_SCHEMA}.custom-keybinding:{GNOME_CUSTOM_PATH}"


# ── 小工具 ──────────────────────────────────────────────────────────────


def _say(msg: str = "") -> None:
    print(msg)


def _step(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}")
    print("─" * 66)


def _ask(question: str, auto_yes: bool, default: bool = True) -> bool:
    if auto_yes:
        return True
    hint = "Y/n" if default else "y/N"
    ans = input(f"  {question} ({hint}) ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _vibevibe_bin() -> str:
    """当前这个 vibevibe 命令的绝对路径,给快捷键和 systemd 用。

    不能只写 "vibevibe" —— GNOME 快捷键和 systemd 的 PATH 跟你的终端不一样,
    只写命令名很可能找不到。
    """
    exe = shutil.which("vibevibe")
    if exe:
        return exe
    # venv 里 pip install -e . 装的,通常跟 python 在同一个 bin 下
    candidate = Path(sys.executable).parent / "vibevibe"
    if candidate.exists():
        return str(candidate)
    return f"{sys.executable} -m vibevibe.cli"


# ── 各步骤 ──────────────────────────────────────────────────────────────


def step_python_deps() -> bool:
    """检查 Python 依赖。pip 应该已经装好了,这里只是确认没漏。"""
    required = [
        ("numpy", "数组运算"),
        ("onnxruntime", "ONNX 推理"),
        ("librosa", "mel 频谱"),
        ("soundfile", "读写 wav"),
        ("tokenizers", "分词"),
        ("sounddevice", "录音和提示音"),
        ("huggingface_hub", "下载模型权重"),
    ]
    missing = []
    for mod, why in required:
        try:
            __import__(mod)
            _say(f"  ✓ {mod:<16} {why}")
        except ImportError:
            missing.append(mod)
            _say(f"  ✗ {mod:<16} {why} —— 没装")
    if missing:
        _say()
        _say(f"  依赖不全。补一下: pip install {' '.join(missing)}")
        return False
    return True


def step_system_tools(cfg: Config, auto_yes: bool) -> bool:
    """检查系统工具。缺了只打印命令,**绝不自己 sudo**。"""
    needed = {
        cfg.inject.type_tool: "把识别出的文字打进光标处",
        cfg.inject.clipboard_tool: "读写剪贴板",
    }
    missing = []
    for tool, why in needed.items():
        path = shutil.which(tool)
        if path:
            _say(f"  ✓ {tool:<10} {path}")
        else:
            missing.append(tool)
            _say(f"  ✗ {tool:<10} 没装 —— {why}")

    if not missing:
        return True

    _say()
    _say("  这两个得用系统包管理器装,需要 root。我不会替你 sudo,")
    _say("  请自己跑这条:")
    _say()
    _say(f"      sudo apt install {' '.join(missing)}")
    _say()
    _say("  (Fedora 用 dnf,Arch 用 pacman,包名一样)")
    _say("  装完再跑一次 `vibevibe setup` 就行,前面做过的步骤会自动跳过。")
    return False


def step_config(auto_yes: bool) -> bool:
    """把配置模板写到 ~/.config/vibevibe/config.toml。"""
    target = CONFIG_HOME / "config.toml"
    if target.exists():
        _say(f"  · 配置已存在,保持不动: {target}")
        _say("    (想重置就先删掉它,再跑一次 setup)")
        return True

    if not EXAMPLE_CONFIG_PATH.exists():
        _say(f"  ✗ 找不到配置模板: {EXAMPLE_CONFIG_PATH}")
        return False

    _say(f"  要写入: {target}")
    if not _ask("生成配置文件?", auto_yes):
        _say("  · 跳过。不写也能跑,那样全用内置默认值。")
        return True

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(EXAMPLE_CONFIG_PATH, target)
    _say(f"  ✓ 已写入 {target}")
    return True


def step_weights(cfg: Config, auto_yes: bool) -> bool:
    """下载当前后端的模型权重。"""
    backend = cfg.asr.backend
    model_dir = cfg.backend_model_dir()

    if model_dir.is_dir() and any(model_dir.iterdir()):
        size = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file()) / 1e9
        _say(f"  · 权重已就位: {model_dir} ({size:.2f} GB)")
        return True

    spec = {
        "qwen_onnx": cfg.asr.qwen_onnx,
        "qwen_onnx_1p7b": cfg.asr.qwen_onnx_1p7b,
        "whisper_ct2": cfg.asr.whisper_ct2,
    }.get(backend)
    if spec is None or not getattr(spec, "hf_repo", ""):
        _say(f"  ✗ 后端 {backend} 没有配下载地址(hf_repo),没法自动下")
        return False

    _say(f"  后端     {backend}")
    _say(f"  来源     {spec.hf_repo}")
    _say(f"  下载到   {model_dir}")
    _say(f"  大小     约 4GB(1.7B int4);下载期间只占网络和磁盘,不占 CPU")
    if not _ask("现在下载?", auto_yes):
        _say("  · 跳过。没有权重的话听写起不来,记得后面补上。")
        return False

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        _say("  ✗ 缺 huggingface_hub,没法下载")
        return False

    model_dir.parent.mkdir(parents=True, exist_ok=True)
    _say("  下载中……(4GB,视网速可能要几分钟)")
    try:
        snapshot_download(
            repo_id=spec.hf_repo,
            local_dir=str(model_dir),
            allow_patterns=list(spec.hf_files) or None,
        )
    except Exception as exc:
        _say(f"  ✗ 下载失败: {type(exc).__name__}: {exc}")
        return False

    size = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file()) / 1e9
    _say(f"  ✓ 完成,{size:.2f} GB")
    return True


def step_hotkey(key: str, auto_yes: bool) -> bool:
    """绑桌面快捷键。

    这条通道**不需要任何权限**,也不需要专用硬件 —— 是 pip 装完之后
    立刻能用的那条路。小键盘那条(evdev 直读 KEY_F19)需要 udev 规则,
    是可选的加强项。
    """
    if not shutil.which("gsettings"):
        _say("  · 没有 gsettings(不是 GNOME?),跳过。")
        _say("    自己在桌面环境里绑一个快捷键,执行:")
        _say(f"        {_vibevibe_bin()} toggle")
        return True

    ok, current = _run(["gsettings", "get", GNOME_KEYS_SCHEMA, "custom-keybindings"])
    if ok and GNOME_CUSTOM_PATH in current:
        ok2, bound = _run(["gsettings", "get", GNOME_CUSTOM_SCHEMA, "binding"])
        _say(f"  · 快捷键已绑过: {bound.strip() if ok2 else '?'}")
        _say("    想换键就先删掉再跑,或者直接在 GNOME 设置里改")
        return True

    command = f"{_vibevibe_bin()} toggle"
    _say(f"  按键     {key}")
    _say("  说明     应用程序基本不碰 Super 键(它是桌面环境的保留键),")
    _say("           所以 Super 系组合最不容易撞车。Ctrl+Shift+V 看着顺手,")
    _say("           但它在浏览器/VS Code/终端里都是「粘贴为纯文本」。")
    _say(f"  执行     {command}")
    _say("  手感     按一下开始录,再按一下停止并出字(GNOME 感知不到松手,")
    _say("           所以是 toggle 而不是按住说话)")
    if not _ask("绑定这个快捷键?", auto_yes):
        _say("  · 跳过。可以自己在 GNOME 设置 → 键盘 → 自定义快捷键里加。")
        return True

    # 保留用户已有的其它自定义快捷键,只把自己这条追加进去
    existing = []
    if ok and current.strip() not in ("@as []", "[]"):
        existing = [
            s.strip().strip("'\"")
            for s in current.strip().lstrip("@as ").strip("[]").split(",")
            if s.strip()
        ]
    if GNOME_CUSTOM_PATH not in existing:
        existing.append(GNOME_CUSTOM_PATH)
    value = "[" + ", ".join(f"'{p}'" for p in existing) + "]"

    for args in (
        ["gsettings", "set", GNOME_KEYS_SCHEMA, "custom-keybindings", value],
        ["gsettings", "set", GNOME_CUSTOM_SCHEMA, "name", "vibevibe 语音听写"],
        ["gsettings", "set", GNOME_CUSTOM_SCHEMA, "command", command],
        ["gsettings", "set", GNOME_CUSTOM_SCHEMA, "binding", key],
    ):
        ok, out = _run(args)
        if not ok:
            _say(f"  ✗ 设置失败: {' '.join(args[:4])}\n    {out}")
            return False

    _say(f"  ✓ 已绑定 {key}")
    return True


def step_service(auto_yes: bool) -> bool:
    """装 systemd --user 服务,让守护进程开机自启、崩了自动重启。"""
    if not shutil.which("systemctl"):
        _say("  · 没有 systemctl,跳过。自己想办法让下面这条常驻:")
        _say(f"        {_vibevibe_bin()} daemon")
        return True

    unit_dir = Path(
        os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    ) / "systemd" / "user"
    unit_path = unit_dir / SERVICE_NAME

    if unit_path.exists():
        ok, out = _run(["systemctl", "--user", "is-enabled", SERVICE_NAME])
        _say(f"  · 服务已安装: {unit_path}({out.strip() or '状态未知'})")
        return True

    exec_start = f"{_vibevibe_bin()} daemon --quiet"
    _say(f"  单元文件 {unit_path}")
    _say(f"  启动命令 {exec_start}")
    _say("  作用     开机自启、崩溃自动重启、跟任何终端会话解耦")
    _say("  级别     --user,不需要 root,不动系统任何配置")
    if not _ask("安装并启用?", auto_yes):
        _say("  · 跳过。那守护进程得自己手动起: vibevibe daemon")
        return True

    if not SERVICE_TEMPLATE.exists():
        _say(f"  ✗ 找不到服务模板: {SERVICE_TEMPLATE}")
        return False

    text = SERVICE_TEMPLATE.read_text(encoding="utf-8")
    # 模板里的占位符换成这台机器上的真实路径
    text = text.replace("@EXEC_START@", exec_start)
    text = text.replace("@WORKING_DIR@", str(data_root()))

    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(text, encoding="utf-8")
    _say(f"  ✓ 已写入 {unit_path}")

    for args, desc in (
        (["systemctl", "--user", "daemon-reload"], "重载 systemd"),
        (["systemctl", "--user", "enable", "--now", SERVICE_NAME], "启用并启动"),
    ):
        ok, out = _run(args)
        if not ok:
            _say(f"  ✗ {desc}失败: {out}")
            return False
        _say(f"  ✓ {desc}")

    _say()
    _say("  查看状态: systemctl --user status vibevibe")
    _say("  看日志:   journalctl --user -u vibevibe -f")
    return True


def _stale_autostart_exec(desktop: Path, want_exec: str) -> str | None:
    """自启文件里的 Exec= 还指得到东西吗?指不到就把旧的那行还回来。

    返回 None = 没问题(可以照旧跳过);返回字符串 = 那条已经失效的旧命令。

    为什么需要这个:`.desktop` 里的 Exec= 是**绝对路径**(必须如此,自启时
    的 PATH 跟终端不一样)。项目目录一改名、venv 一重建,这个文件就变成了
    指向空气的死链接,而它还在那儿——只判断"文件存不存在"会一直报"已装",
    托盘却再也起不来。这坑本机踩过一次:工作区从 8888-夏日大作战 改名成
    8888-Projects 之后,自启文件在原地放了一整天没人发现。
    """
    try:
        text = desktop.read_text(encoding="utf-8")
    except OSError:
        return "(读不出来)"

    for line in text.splitlines():
        if not line.startswith("Exec="):
            continue
        old = line[len("Exec="):].strip()
        if old == want_exec:
            return None
        # 命令行不同不一定是坏的(用户可能自己加了参数),
        # 但可执行文件本身不存在就一定是坏的
        binary = old.split()[0] if old.split() else ""
        return None if binary and Path(binary).exists() else (old or "(空)")

    return "(没有 Exec= 这一行)"


def step_tray(auto_yes: bool) -> bool:
    """装托盘图标的开机自启。

    托盘是**独立进程**,不是守护进程的一部分——必须如此,否则用托盘
    "关掉服务"会把托盘自己也关掉,再也没法打开。
    """
    # 先看看这台机器能不能显示托盘
    try:
        import gi  # noqa: F401
    except ImportError:
        sys.path.append("/usr/lib/python3/dist-packages")
    ok_gi = True
    try:
        import gi

        gi.require_version("AyatanaAppIndicator3", "0.1")
    except Exception:
        ok_gi = False

    if not ok_gi:
        _say("  ✗ 缺 PyGObject / AppIndicator 绑定,托盘用不了")
        _say()
        _say("      sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1")
        _say()
        _say("  (GNOME 46+ 还需要托盘扩展才能显示图标;Ubuntu 自带")
        _say("   ubuntu-appindicators,默认已启用)")
        return False

    autostart = Path(
        os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    ) / "autostart"
    target = autostart / "vibevibe-tray.desktop"

    exec_cmd = f"{_vibevibe_bin()} tray"
    icon = str(ICONS_DIR / "vibevibe-idle.svg")

    if target.exists():
        stale = _stale_autostart_exec(target, exec_cmd)
        if stale is None:
            _say(f"  · 托盘自启已装: {target}")
            return True
        # 装过,但里面那条命令已经指向不存在的地方了 —— 项目目录改名 / 换了
        # venv 都会这样。这时候光"已装"是骗人的:下次登录托盘根本起不来。
        _say(f"  ! 托盘自启里的路径已失效: {target}")
        _say(f"    旧: {stale}")
        _say(f"    新: {exec_cmd}")
        if not _ask("重写成新路径?", auto_yes):
            _say("  · 跳过。托盘下次登录仍然起不来")
            return True
    else:
        _say(f"  自启文件 {target}")
        _say(f"  启动命令 {exec_cmd}")
        _say("  作用     顶栏出现一个 V 图标,里面两个开关:")
        _say("           · 服务    —— 关掉 = 守护进程退出,内存全部还给系统")
        _say("           · 热加载  —— 模型是否常驻内存(约 3.8GB vs 0.24GB)")
        if not _ask("装上托盘自启?", auto_yes):
            _say("  · 跳过。想手动开就跑: vibevibe tray")
            return True

    if not TRAY_DESKTOP_TEMPLATE.exists():
        _say(f"  ✗ 找不到模板: {TRAY_DESKTOP_TEMPLATE}")
        return False

    text = TRAY_DESKTOP_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("@EXEC@", exec_cmd).replace("@ICON@", icon)
    autostart.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    target.chmod(0o755)
    _say(f"  ✓ 已写入 {target}(下次登录自动出现)")

    # 现在就起一个,不用等重新登录
    if _ask("现在就启动托盘?", auto_yes):
        try:
            subprocess.Popen(
                exec_cmd.split(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            _say("  ✓ 已启动,看一眼屏幕右上角")
        except Exception as exc:
            _say(f"  ✗ 启动失败: {exc}")
    return True


# ── 主流程 ──────────────────────────────────────────────────────────────


def run_setup(
    auto_yes: bool = False,
    key: str = "<Super><Shift>v",
    skip_weights: bool = False,
    skip_hotkey: bool = False,
    skip_service: bool = False,
    skip_tray: bool = False,
) -> int:
    from . import __version__

    _say(f"vibevibe {__version__} · 安装向导")
    _say()
    _say(f"  运行方式   {'源码目录' if is_source_checkout() else 'pip 已安装'}")
    _say(f"  配置目录   {CONFIG_HOME}")
    _say(f"  数据目录   {data_root()}")
    _say(f"  状态目录   {STATE_HOME}")
    if not auto_yes:
        _say()
        _say("  每一步都会先问你。需要 root 的事情我只打印命令,不会替你 sudo。")

    total = 7
    failures = []

    _step(1, total, "Python 依赖")
    if not step_python_deps():
        _say("\n依赖不全,先补齐再继续。")
        return 1

    _step(2, total, "系统工具")
    if not step_system_tools(load_config(), auto_yes):
        failures.append("系统工具(xdotool / xclip)")

    _step(3, total, "配置文件")
    if not step_config(auto_yes):
        failures.append("配置文件")

    # 配置可能刚写好,重新读一遍再往下走
    cfg = load_config()

    _step(4, total, "模型权重")
    if skip_weights:
        _say("  · 按参数要求跳过")
    elif not step_weights(cfg, auto_yes):
        failures.append("模型权重")

    _step(5, total, "快捷键")
    if skip_hotkey:
        _say("  · 按参数要求跳过")
    elif not step_hotkey(key, auto_yes):
        failures.append("快捷键")

    _step(6, total, "开机自启服务")
    if skip_service:
        _say("  · 按参数要求跳过")
    elif not step_service(auto_yes):
        failures.append("systemd 服务")

    _step(7, total, "托盘图标")
    if skip_tray:
        _say("  · 按参数要求跳过")
    elif not step_tray(auto_yes):
        failures.append("托盘图标")

    _say()
    _say("=" * 66)
    if failures:
        _say("没全部完成,还差:")
        for f in failures:
            _say(f"  · {f}")
        _say()
        _say("处理完再跑一次 `vibevibe setup`,做过的步骤会自动跳过。")
    else:
        _say("装好了。")
        _say()
        _say(f"  按 {key} 开始说话,再按一下出字。")
        _say("  体检: vibevibe doctor")
        _say("  状态: vibevibe status")
    _say("=" * 66)
    return 1 if failures else 0
