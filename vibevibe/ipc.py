"""守护进程和命令行客户端之间的通信。

Unix socket + 一行一条 JSON。够用、好调试(可以直接用 socat 手动发)、
不占端口、不走网络——这东西必须是纯本地的。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

ENCODING = "utf-8"
RECV_BUFSIZE = 65536
DEFAULT_TIMEOUT_SEC = 10.0

# 客户端能发的命令
CMD_START = "start"        # 开始录音
CMD_STOP = "stop"          # 停止录音并转写
CMD_TOGGLE = "toggle"      # 在录/不录之间切换
CMD_CANCEL = "cancel"      # 放弃当前录音,不转写
CMD_STATUS = "status"      # 查状态
CMD_PING = "ping"
CMD_SHUTDOWN = "shutdown"
CMD_SET_HOT = "set_hot"    # 运行时切换热加载(托盘那个开关)
CMD_RELOAD = "reload"      # 重新读配置文件,把能热改的项应用上

# 守护进程的状态
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"
STATE_LOADING = "loading"


class IpcError(Exception):
    pass


class DaemonNotRunning(IpcError):
    pass


def send(socket_path: Path, payload: dict[str, Any],
         timeout: float = DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
    """给守护进程发一条命令,等它回一条。"""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(socket_path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise DaemonNotRunning(
                f"连不上守护进程({socket_path})。先起一个: vibevibe daemon"
            ) from exc

        sock.sendall((json.dumps(payload) + "\n").encode(ENCODING))

        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(RECV_BUFSIZE)
            if not chunk:
                break
            buf += chunk
        if not buf.strip():
            raise IpcError("守护进程没有回应")
        return json.loads(buf.split(b"\n", 1)[0].decode(ENCODING))
    finally:
        sock.close()


def command(socket_path: Path, cmd: str, **kwargs: Any) -> dict[str, Any]:
    return send(socket_path, {"cmd": cmd, **kwargs})
