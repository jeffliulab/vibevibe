"""F19 通道 —— 直接读输入设备。

**为什么必须这么做**:F19 在 X11 默认布局下没有 keysym
(实测这台机器 keycode 197 是空的),GNOME 的快捷键设置界面
根本绑不了它。所以只能绕过 X,直接从内核的 evdev 层读原始按键事件。

这条路反过来还带来两个好处:
  1. 能精确知道"按下"和"松开",于是 hold(按住说话)模式才成立
     ——GNOME 快捷键只能感知按下
  2. 对**专用小键盘**可以整个独占(EVIOCGRAB),按键一个字符都
     不会漏进当前窗口

独占是设备级的,不能只独占某一个键。所以:
  - 专用小键盘(就一两个键)→ 可以独占,干净
  - 主键盘 → **绝不能独占**,否则你所有的输入都会被吞掉

代码里有一道硬闸:开启 grab 时如果设备的按键数超过阈值,直接拒绝,
免得配置写错把整台机器的键盘吞了。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .config import HotkeyConfig

log = logging.getLogger("vibevibe.hotkey")

# evdev 的按键事件值
KEY_UP = 0
KEY_DOWN = 1
KEY_REPEAT = 2


class HotkeyError(Exception):
    pass


def resolve_keycode(key_name: str) -> int:
    """把 "KEY_F19" 这样的名字翻成 evdev 的数字码。"""
    from evdev import ecodes

    code = ecodes.ecodes.get(key_name.upper())
    if code is None:
        raise HotkeyError(
            f"不认识的按键名 {key_name!r}。要用 evdev 的写法,比如 KEY_F19、KEY_F20。"
        )
    return code


def list_devices() -> list[str]:
    """列出所有输入设备,并标出 by-id 的稳定路径。

    配置里**必须**填 by-id 路径:/dev/input/eventN 的编号会在
    重新插拔后变化,写死它迟早会指错设备。

    注意:这里是自己遍历 /dev/input/,而不是用 evdev.list_devices()
    ——后者会把没权限打开的设备直接过滤掉,于是在还没配 udev 规则时
    会列出一片空白,让人以为"没有设备",实际是"没有权限"。
    这两种情况必须区分清楚。
    """
    from evdev import InputDevice, ecodes

    by_id_dir = Path("/dev/input/by-id")
    # 真实路径 → by-id 稳定路径
    by_id: dict[str, str] = {}
    if by_id_dir.is_dir():
        for link in sorted(by_id_dir.iterdir()):
            try:
                by_id[str(link.resolve())] = str(link)
            except OSError:
                continue

    event_nodes = sorted(
        Path("/dev/input").glob("event*"),
        key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0,
    )
    if not event_nodes:
        return ["(/dev/input 下一个 event 节点都没有?)"]

    out: list[str] = []
    denied = 0
    for path in event_nodes:
        stable = by_id.get(str(path.resolve()), "")
        try:
            dev = InputDevice(str(path))
        except PermissionError:
            denied += 1
            out.append(
                f"{path}  ⚠ 没有读取权限\n"
                f"    稳定路径: {stable or '(没有 by-id 链接)'}"
            )
            continue
        except OSError as exc:
            out.append(f"{path}  (打不开: {exc})")
            continue

        n_keys = len(dev.capabilities().get(ecodes.EV_KEY, []))
        hint = "  ← 按键很少,像是专用小键盘" if 0 < n_keys <= 8 else ""
        out.append(
            f"{dev.name!r}  按键数={n_keys}{hint}\n"
            f"    临时路径: {path}(会变,别写进配置)\n"
            f"    稳定路径: {stable or '(这个设备没有 by-id 链接)'}"
        )
        dev.close()

    if denied:
        out.append(
            f"\n  ⚠ 有 {denied} 个设备因为权限读不到。这是正常的——默认只有 root 和\n"
            "    input 组能读输入设备。等小键盘到货后,按 config/70-vibevibe.rules.example\n"
            "    装一条只针对它的 udev 规则即可(不需要把你加进 input 组,\n"
            "    那等于给了读取所有键盘的权限)。"
        )
    return out


class HotkeyListener:
    """在后台线程里监听指定设备上的指定按键。

    daemon 参数是 Daemon 实例,这里只用它的 start/stop/toggle。
    """

    def __init__(self, cfg: HotkeyConfig, daemon) -> None:  # noqa: ANN001
        self.cfg = cfg
        self.daemon = daemon
        self.keycode = resolve_keycode(cfg.key)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._device = None
        self._connected = False

    def describe(self) -> str:
        state = "已连接" if self._connected else "等待设备"
        grab = ",独占" if self.cfg.grab else ""
        return f"{self.cfg.key} @ {self.cfg.device} [{self.cfg.mode}{grab}] {state}"

    # ── 设备 ────────────────────────────────────────────────────────

    def _open(self):  # noqa: ANN202
        from evdev import InputDevice, ecodes

        if not self.cfg.device:
            raise HotkeyError(
                "hotkey.device 没填。先跑 `vibevibe devices --input` 看看有哪些设备,"
                "然后把 /dev/input/by-id/ 下的稳定路径填进配置。"
            )
        path = Path(self.cfg.device)
        if not path.exists():
            return None  # 设备还没插上,交给重连循环

        dev = InputDevice(str(path))

        keys = dev.capabilities().get(ecodes.EV_KEY, [])
        if self.keycode not in keys:
            def key_name(code: int) -> str:
                # ecodes.KEY[code] 可能是字符串,也可能是同码多名的列表
                name = ecodes.KEY.get(code, code)
                return name if isinstance(name, str) else str(name)

            names = ", ".join(key_name(k) for k in keys[:20])
            dev.close()
            raise HotkeyError(
                f"设备 {dev.name!r} 上没有 {self.cfg.key} 这个键。"
                f"它有的键(前 20 个): {names}"
            )

        if self.cfg.grab:
            # 安全闸:独占是设备级的。按键太多说明这多半是主键盘,
            # 独占它会把你所有的输入都吞掉。宁可不独占也不能吞。
            if len(keys) > self.cfg.grab_max_keys:
                dev.close()
                raise HotkeyError(
                    f"拒绝独占 {dev.name!r}:它有 {len(keys)} 个按键,"
                    f"超过了 grab_max_keys={self.cfg.grab_max_keys}。"
                    "独占是整个设备的,对主键盘独占会吃掉你所有输入。"
                    "只应该对专用小键盘开 grab。"
                )
            dev.grab()
            log.info("已独占设备 %r(按键不会漏进当前窗口)", dev.name)

        log.info("监听 %r 上的 %s [%s 模式]", dev.name, self.cfg.key, self.cfg.mode)
        return dev

    def _close(self) -> None:
        if self._device is None:
            return
        try:
            if self.cfg.grab:
                self._device.ungrab()
        except OSError:
            pass
        try:
            self._device.close()
        except OSError:
            pass
        self._device = None
        self._connected = False

    # ── 事件循环 ────────────────────────────────────────────────────

    def _handle_key(self, value: int) -> None:
        if self.cfg.mode == "hold":
            if value == KEY_DOWN:
                self.daemon.start_recording()
            elif value == KEY_UP:
                self.daemon.stop_recording()
            # KEY_REPEAT(按住不放时内核发的重复事件)直接忽略
        elif self.cfg.mode == "toggle":
            if value == KEY_DOWN:
                self.daemon.toggle()
        else:
            log.error("不认识的 hotkey.mode %r,只支持 hold / toggle", self.cfg.mode)

    def _loop(self) -> None:
        from evdev import ecodes

        while not self._stop.is_set():
            try:
                if self._device is None:
                    self._device = self._open()
                    if self._device is None:
                        # 设备没插上,等一会儿再看
                        self._stop.wait(self.cfg.reconnect_interval_sec)
                        continue
                    self._connected = True

                for event in self._device.read_loop():
                    if self._stop.is_set():
                        break
                    if event.type != ecodes.EV_KEY or event.code != self.keycode:
                        continue
                    self._handle_key(event.value)

            except HotkeyError as exc:
                # 配置层面的错误,重试也没用,说清楚然后停
                log.error("热键通道停止: %s", exc)
                return
            except PermissionError:
                log.error(
                    "没有权限读 %s。需要装一条 udev 规则给当前用户放行 —— "
                    "见 config/70-vibevibe.rules.example",
                    self.cfg.device,
                )
                self._close()
                self._stop.wait(self.cfg.reconnect_interval_sec * 5)
            except OSError as exc:
                # 设备被拔了之类,进重连循环
                log.warning("输入设备断开(%s),%.1fs 后重试",
                            exc, self.cfg.reconnect_interval_sec)
                self._close()
                self._stop.wait(self.cfg.reconnect_interval_sec)
            except Exception:
                log.exception("热键线程出错,%.1fs 后重试", self.cfg.reconnect_interval_sec)
                self._close()
                self._stop.wait(self.cfg.reconnect_interval_sec)

    # ── 生命周期 ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="vibevibe-hotkey", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
