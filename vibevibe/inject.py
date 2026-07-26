"""把识别出来的文字送进当前光标处。

两种办法:

  clipboard_paste(默认,推荐)
      把文字塞进剪贴板,再模拟一次 Ctrl+V。中英混排、表情、换行都不会出问题,
      速度也快(一次粘贴 vs 逐字敲几十下)。代价是会动到剪贴板——
      所以默认会先存下你原来的内容,粘完再还回去。

  type
      用 xdotool 逐字敲。对中文不可靠(依赖输入法状态、键盘布局),
      只在粘贴被目标程序屏蔽时才考虑。

当前环境是 X11 + GNOME,用 xclip + xdotool。Wayland 需要换成
wl-copy + wtype/ydotool,留到后面做。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time

from .config import InjectConfig
from .i18n import t

log = logging.getLogger(__name__)

# 等剪贴板内容真正被 X 服务端接管的时间。xclip 是靠一个后台进程持有
# selection 的,写完立刻粘贴偶尔会拿到旧内容。
CLIPBOARD_SETTLE_SEC = 0.05


class InjectError(Exception):
    pass


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise InjectError(
            f"缺少 {tool}。装一下: sudo apt install {tool}"
        )
    return path


class Injector:
    def __init__(self, cfg: InjectConfig) -> None:
        self.cfg = cfg

    def check(self) -> list[str]:
        """检查依赖是否齐全,返回缺失的工具名列表(空列表 = 都在)。"""
        needed = [self.cfg.type_tool]
        if self.cfg.method == "clipboard_paste":
            needed.append(self.cfg.clipboard_tool)
        # 循环变量别用 t —— 会遮住翻译函数 t(),读代码时容易看岔
        return [tool for tool in needed if not shutil.which(tool)]

    # ── 剪贴板 ──────────────────────────────────────────────────────

    def _clipboard_read(self) -> bytes | None:
        tool = _require(self.cfg.clipboard_tool)
        try:
            proc = subprocess.run(
                [tool, "-selection", "clipboard", "-o"],
                capture_output=True, timeout=2.0,
            )
            return proc.stdout if proc.returncode == 0 else None
        except subprocess.TimeoutExpired:
            # 剪贴板是空的时候 xclip 会挂住,这不是错误
            return None

    def _clipboard_write(self, data: bytes) -> None:
        tool = _require(self.cfg.clipboard_tool)
        proc = subprocess.Popen(
            [tool, "-selection", "clipboard"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.communicate(input=data, timeout=5.0)

    # ── 注入 ────────────────────────────────────────────────────────

    def _paste(self, text: str) -> None:
        xdotool = _require(self.cfg.type_tool)
        saved = self._clipboard_read() if self.cfg.restore_clipboard else None

        self._clipboard_write(text.encode("utf-8"))
        time.sleep(CLIPBOARD_SETTLE_SEC)
        subprocess.run(
            [xdotool, "key", "--clearmodifiers", self.cfg.paste_key],
            check=True, timeout=5.0,
        )

        if saved is not None:
            # 等目标程序真的读完剪贴板再还原,还太早会粘出旧内容
            time.sleep(self.cfg.restore_delay_sec)
            try:
                self._clipboard_write(saved)
            except Exception as exc:
                log.warning(t("inject.restore_failed"), exc)

    def _type(self, text: str) -> None:
        xdotool = _require(self.cfg.type_tool)
        subprocess.run(
            [xdotool, "type", "--clearmodifiers",
             "--delay", str(self.cfg.type_delay_ms), "--", text],
            check=True, timeout=30.0,
        )

    def inject(self, text: str) -> None:
        if not text:
            return
        if self.cfg.method == "clipboard_paste":
            self._paste(text)
        elif self.cfg.method == "type":
            self._type(text)
        else:
            raise InjectError(
                f"未知的注入方式 {self.cfg.method!r},只支持 clipboard_paste / type")
