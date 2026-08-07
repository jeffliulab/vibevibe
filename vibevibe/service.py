"""跟 systemd 用户服务打交道,以及「把 vibevibe 整个停掉」这件事。

单独成一个模块,是因为有三个地方都要用它,而它们彼此不能互相 import:

    tray.py         托盘的服务开关、退出按钮 —— 会 import gi/GTK
    cli.py          `vibevibe quit` —— **绝不能**碰 GTK,命令行得能在没有
                    桌面的环境里跑
    setup_wizard.py 装 / 启用服务

这里只调 systemctl 和 Unix socket,不加载模型、不碰 GTK。
"""

from __future__ import annotations

import subprocess

from . import ipc
from .config import Config

SERVICE = "vibevibe.service"

# systemctl 调用的超时。这些都是本地 D-Bus 往返,正常在毫秒级;
# 给到 20 秒是留给 `stop` —— 守护进程收到 SIGTERM 后要收尾(停录音、
# 关 socket),systemd 那边默认还有一段 TimeoutStopSec。
SYSTEMCTL_TIMEOUT_SEC = 20.0

# 停守护进程时等它回话的时间。IPC 的默认是 10 秒,但退出这件事上
# 用户在等着,宁可早点放弃走下一步也不要卡住界面。
SHUTDOWN_IPC_TIMEOUT_SEC = 3.0


def systemctl(*args: str) -> tuple[bool, str]:
    """跑一条 `systemctl --user ...`,返回(成功与否, 合并后的输出)。

    任何异常都收敛成 (False, 说明) —— 调用方要的是「成没成」,
    不该为了 systemctl 没装而崩掉。
    """
    try:
        p = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_SEC)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def is_active() -> bool:
    ok, out = systemctl("is-active", SERVICE)
    return ok and out.strip() == "active"


def is_installed() -> bool:
    ok, _ = systemctl("cat", SERVICE)
    return ok


def stop_all(cfg: Config) -> tuple[bool, list[str]]:
    """把守护进程停干净。返回 (是否全部停下, 给人看的过程说明)。

    两条路都要走,因为守护进程有两种起法:

    1. systemd 管的 —— 必须 `systemctl --user stop`。直接发 IPC shutdown 没用:
       进程一退,unit 里 `Restart=on-failure` 会在 RestartSec 秒后把它拉回来
       (SIGTERM 自杀算不算 failure 取决于退出码,不能赌)。
    2. 手动起的 —— systemd 不认识它,只能发 IPC shutdown。

    所以:先 stop 服务(装了的话),再探一次 socket 兜底。socket 探不到
    **不算失败** —— 那正是我们想要的结果。
    """
    notes: list[str] = []
    ok = True

    if is_installed():
        stopped, out = systemctl("stop", SERVICE)
        if stopped:
            notes.append(f"已停止 {SERVICE}")
        else:
            ok = False
            notes.append(f"停止 {SERVICE} 失败: {out or '(没有输出)'}")
    else:
        notes.append(f"没装 {SERVICE},跳过 systemd")

    # 兜底:还连得上就说明另有一个守护进程活着(手动起的,或者 stop 没生效)
    try:
        ipc.command(cfg.socket_path, ipc.CMD_SHUTDOWN,
                    timeout=SHUTDOWN_IPC_TIMEOUT_SEC)
        notes.append("已通知守护进程退出(IPC shutdown)")
    except ipc.DaemonNotRunning:
        notes.append("守护进程已经不在了")
    except ipc.IpcError as exc:
        ok = False
        notes.append(f"IPC shutdown 失败: {exc}")

    return ok, notes
