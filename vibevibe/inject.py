"""把识别出来的文字送进当前光标处。

两种办法:

  clipboard_paste(默认,推荐)
      把文字塞进剪贴板,再模拟一次粘贴。中英混排、表情、换行都不会出问题,
      速度也快(一次粘贴 vs 逐字敲几十下)。代价是会动到剪贴板——
      所以默认会先存下你原来的内容,粘完再还回去。

      **粘贴键不是固定的 Ctrl+V**:终端里 Ctrl+V 是 readline 的 quoted-insert,
      不是粘贴,发过去不但不出字还会吃掉你下一个按键。所以粘贴前先看一眼焦点
      窗口的 WM_CLASS,按 inject.paste_key_by_window_class 那张表挑键。

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

# 查焦点窗口类名的超时。这两次调用在「说完 → 出字」的关键路径上,
# 实测一次 1.8 ms;给到 1 秒纯粹是防止 X 卡住时把整条链路一起拖死。
WINDOW_QUERY_TIMEOUT_SEC = 1.0


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
        """检查依赖是否齐全,返回缺失的工具名列表(空列表 = 都在)。

        window_tool(xprop)**不算在内** —— 它缺了只是挑不了粘贴键,
        听写本身照常工作,不该报成"缺依赖"。doctor 会单独提醒。
        """
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

    # ── 焦点窗口 ────────────────────────────────────────────────────

    def _active_window_classes(self) -> list[str]:
        """当前焦点窗口的 WM_CLASS,拿不到就返回空列表。

        WM_CLASS 有两个值:instance 和 class,两个都还回去让调用方挨个匹 ——
        同一个终端两边写法可能完全不同(GNOME Terminal 是
        "gnome-terminal-server" 和 "Gnome-terminal"),只匹一个必漏。

        **这个方法绝不抛异常。** 查不出焦点窗口(xprop 没装、Wayland、
        没有窗口获得焦点)只意味着挑不了粘贴键,不该让整次听写作废 ——
        退回默认键至少在普通程序里还是对的。
        """
        try:
            xdotool = shutil.which(self.cfg.type_tool)
            xprop = shutil.which(self.cfg.window_tool)
            if not xdotool or not xprop:
                return []

            win = subprocess.run(
                [xdotool, "getactivewindow"],
                capture_output=True, text=True,
                timeout=WINDOW_QUERY_TIMEOUT_SEC,
            )
            window_id = win.stdout.strip()
            if win.returncode != 0 or not window_id:
                return []

            prop = subprocess.run(
                [xprop, "-id", window_id, "WM_CLASS"],
                capture_output=True, text=True,
                timeout=WINDOW_QUERY_TIMEOUT_SEC,
            )
            if prop.returncode != 0:
                return []
            # 形如: WM_CLASS(STRING) = "gnome-terminal-server", "Gnome-terminal"
            _, _, values = prop.stdout.partition("=")
            return [v.strip().strip('"') for v in values.split(",") if v.strip()]
        except Exception as exc:
            log.debug("查焦点窗口类名失败(退回默认粘贴键): %s", exc)
            return []

    def _paste_key_for_active_window(self) -> str:
        """这次该发哪个粘贴键。查不出窗口就用默认的。"""
        overrides = self.cfg.paste_key_by_window_class
        if not overrides:
            return self.cfg.paste_key

        # 大小写不敏感:同一个终端的 instance 和 class 常常只差大小写
        lowered = {name.lower(): key for name, key in overrides.items()}
        for name in self._active_window_classes():
            key = lowered.get(name.lower())
            if key:
                log.debug("焦点窗口 %s → 粘贴键 %s", name, key)
                return key
        return self.cfg.paste_key

    # ── 注入 ────────────────────────────────────────────────────────

    def _paste(self, text: str) -> None:
        xdotool = _require(self.cfg.type_tool)
        saved = self._clipboard_read() if self.cfg.restore_clipboard else None

        # 粘贴键要在写剪贴板**之前**定下来:焦点窗口就是文字要落进去的那个,
        # 而 xclip 起后台进程的一瞬间焦点偶尔会飘。
        paste_key = self._paste_key_for_active_window()

        self._clipboard_write(text.encode("utf-8"))
        time.sleep(CLIPBOARD_SETTLE_SEC)
        subprocess.run(
            [xdotool, "key", "--clearmodifiers", paste_key],
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
