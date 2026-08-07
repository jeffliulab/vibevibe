"""启动登记点 —— 别的系统替我们记着的那几条启动命令。

一共三处,都是**绝对路径**(必须如此:GNOME 快捷键、systemd、桌面自启,
三者的 PATH 都跟你的终端不一样,只写命令名多半找不到):

    桌面快捷键    GNOME dconf 里的 custom-keybinding,记着 `… vibevibe toggle`
    systemd 服务  ~/.config/systemd/user/vibevibe.service 的 ExecStart=
    托盘自启      ~/.config/autostart/vibevibe-tray.desktop 的 Exec=

绝对路径的代价是**项目目录一改名、venv 一重建,这三条就全变成指向空气的
死链接**,而登记本身还在原地。所以「登记过没有」和「登记的东西还活着没」
是两个问题,只问前一个会得到"一切正常"的假象 —— 2026-08-07 工作区改名之后
就是这样:快捷键、自启双双指着已删除的目录,doctor 却报一切正常。

这个模块只**读和判断**,不改任何东西。改由 `setup_wizard` 负责(它会问你),
查由 `vibevibe doctor` 负责。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .service import SERVICE

# GNOME 自定义快捷键存放的 dconf 路径
GNOME_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GNOME_CUSTOM_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/vibevibe/"
)
GNOME_CUSTOM_SCHEMA = f"{GNOME_KEYS_SCHEMA}.custom-keybinding:{GNOME_CUSTOM_PATH}"

# 读 gsettings / systemd 的超时。都是本地查询,正常在毫秒级。
QUERY_TIMEOUT_SEC = 10.0


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def unit_path() -> Path:
    return _config_home() / "systemd" / "user" / SERVICE


def autostart_path() -> Path:
    return _config_home() / "autostart" / "vibevibe-tray.desktop"


def vibevibe_bin() -> str:
    """当前这份代码对应的 vibevibe 命令,绝对路径。

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


# ── 三个读取器 ──────────────────────────────────────────────────────────


def _read_shortcut() -> str | None:
    if not shutil.which("gsettings"):
        return None
    try:
        listed = subprocess.run(
            ["gsettings", "get", GNOME_KEYS_SCHEMA, "custom-keybindings"],
            capture_output=True, text=True, timeout=QUERY_TIMEOUT_SEC)
        if listed.returncode != 0 or GNOME_CUSTOM_PATH not in listed.stdout:
            return None
        got = subprocess.run(
            ["gsettings", "get", GNOME_CUSTOM_SCHEMA, "command"],
            capture_output=True, text=True, timeout=QUERY_TIMEOUT_SEC)
        if got.returncode != 0:
            return None
        return got.stdout.strip().strip("'\"") or None
    except Exception:
        return None


def _read_key_from_file(path: Path, prefix: str) -> str | None:
    """从 ini 风格的文件里挑出 `prefix=` 那一行的值(unit 和 .desktop 都是这个格式)。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip() or None
    return None


# ── 登记点 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Launcher:
    key: str            # "shortcut" / "service" / "tray_autostart"
    label: str          # 人话名字,给 doctor 打印
    where: str          # 登记在哪
    command: str | None  # 登记的完整命令;None = 压根没登记过

    @property
    def binary(self) -> str | None:
        """命令里的可执行文件那一段。"""
        if not self.command:
            return None
        parts = self.command.split()
        return parts[0] if parts else None

    @property
    def alive(self) -> bool | None:
        """登记的那个可执行文件还在不在。

        None = 没登记过 —— 那不是"坏了",只是这条通道没装,别报成问题。
        """
        if self.command is None:
            return None
        binary = self.binary
        return bool(binary) and Path(binary).exists()


def scan() -> list[Launcher]:
    return [
        Launcher(
            key="shortcut",
            label="桌面快捷键",
            where=f"gsettings {GNOME_CUSTOM_PATH}",
            command=_read_shortcut(),
        ),
        Launcher(
            key="service",
            label="systemd 服务",
            where=str(unit_path()),
            command=_read_key_from_file(unit_path(), "ExecStart="),
        ),
        Launcher(
            key="tray_autostart",
            label="托盘开机自启",
            where=str(autostart_path()),
            command=_read_key_from_file(autostart_path(), "Exec="),
        ),
    ]


def get(key: str) -> Launcher:
    for launcher in scan():
        if launcher.key == key:
            return launcher
    raise KeyError(key)


def stale_reason(launcher: Launcher, want_command: str) -> str | None:
    """要不要重写这条登记?要就把那条**旧命令**还回来,不要就返回 None。

    判据故意宽松:命令跟我们现在会写的不一样**不一定**是坏的
    (用户可能自己加了参数、或者故意指着另一份安装),只有那个可执行文件
    压根不存在才是确定坏了。宁可漏报也别把用户手工调过的设置冲掉。
    """
    if launcher.command is None:
        return None
    if launcher.command == want_command:
        return None
    return None if launcher.alive else launcher.command
