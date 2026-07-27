"""F19 通道 —— 直接读输入设备。

**为什么必须这么做**:F19 在 X11 默认布局下没有 keysym
(实测这台机器 keycode 197 是空的),GNOME 的快捷键设置界面
根本绑不了它。所以只能绕过 X,直接从内核的 evdev 层读原始按键事件。

这条路带来的真正好处是:**能精确知道"按下"和"松开"**,于是 hold
(按住说话)模式才成立 —— GNOME 快捷键只能感知按下。

## 关于独占(EVIOCGRAB),两条实测得出的结论

**一、两键布局下必须关掉独占。** 独占是**整设备级**的,不能只独占某一个键。
「左键触发听写 + 右键透传回车」这种布局,一旦独占,右键的 Enter 也会被吞掉,
永远送不到你正在打字的窗口。

不独占也没关系:F19 在 X11 默认布局里没有 keysym(实测这台机器
keycode 197 是空的),漏给应用不会有任何反应。当初为「不冲突」选 F19,
顺带解决了「不能独占」。

**二、用按键数量当安全闸的判据是错的。** 原来的代码是「按键数超过 8 就
拒绝独占」,想借此挡住主键盘。实测:

    T TYPEKEY Z2(两键小键盘)   声明 279 个键
    USB Keyboard(主键盘)       声明 143 个键

QMK 系固件不管物理上几个键,都会声明整个键位范围 —— 数量根本区分不出
主键盘。现在换成设备名匹配(grab_expect_name),挡的是真实风险:
by-id 路径在换硬件后指向了别的设备。
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


# 修饰键。组合键里除了最后一个,其余必须都是这里面的。
MODIFIER_NAMES = (
    "KEY_LEFTCTRL", "KEY_RIGHTCTRL",
    "KEY_LEFTSHIFT", "KEY_RIGHTSHIFT",
    "KEY_LEFTALT", "KEY_RIGHTALT",
    "KEY_LEFTMETA", "KEY_RIGHTMETA",
)


def modifier_codes() -> set[int]:
    from evdev import ecodes

    return {ecodes.ecodes[n] for n in MODIFIER_NAMES if n in ecodes.ecodes}


def resolve_keycode(key_name: str) -> int:
    """把 "KEY_F19" 这样的名字翻成 evdev 的数字码。"""
    from evdev import ecodes

    code = ecodes.ecodes.get(key_name.upper())
    if code is None:
        raise HotkeyError(
            f"不认识的按键名 {key_name!r}。要用 evdev 的写法,比如 KEY_F19、KEY_F20。"
        )
    return code


def parse_combo(spec: str) -> tuple[int, frozenset[int]]:
    """把 "KEY_LEFTCTRL+KEY_V" 解析成 (触发键码, 需要按住的修饰键码集合)。

    单个键就是 ("KEY_F19", 空集合)。约定:**最后一个是触发键**,
    前面的必须全是修饰键 —— 组合键只能是「按住若干修饰键 + 敲一个普通键」,
    不支持两个普通键同时按(那既不常见,也没法可靠地判断顺序)。
    """
    parts = [p.strip().upper() for p in spec.split("+") if p.strip()]
    if not parts:
        raise HotkeyError("触发键没填")

    trigger = resolve_keycode(parts[-1])
    mods = set()
    valid_mods = modifier_codes()
    for name in parts[:-1]:
        code = resolve_keycode(name)
        if code not in valid_mods:
            raise HotkeyError(
                f"{name} 不是修饰键。组合键的写法是「若干修饰键 + 一个普通键」,"
                f"比如 KEY_LEFTCTRL+KEY_V。修饰键只能是: "
                + ", ".join(MODIFIER_NAMES)
            )
        mods.add(code)
    return trigger, frozenset(mods)


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
        # 支持组合键:"KEY_F19" 或 "KEY_LEFTCTRL+KEY_V"
        self.keycode, self.required_mods = parse_combo(cfg.key)
        # 当前按住的修饰键。组合键要靠它判断"够不够条件"。
        self._held_mods: set[int] = set()
        self._mod_codes = modifier_codes()
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

        def key_name(code: int) -> str:
            # ecodes.KEY[code] 可能是字符串,也可能是同码多名的列表
            name = ecodes.KEY.get(code, code)
            return name if isinstance(name, str) else str(name)

        # 组合键要求的每一个键(触发键 + 各修饰键)设备上都得有
        keys = dev.capabilities().get(ecodes.EV_KEY, [])
        missing = [c for c in ({self.keycode} | self.required_mods) if c not in keys]
        if missing:
            names = ", ".join(key_name(k) for k in keys[:20])
            lacking = ", ".join(key_name(c) for c in missing)
            dev.close()
            raise HotkeyError(
                f"设备 {dev.name!r} 上没有 {lacking}"
                f"({self.cfg.key} 需要它)。它有的键(前 20 个): {names}"
            )

        if self.cfg.grab:
            # 安全闸:设备名必须对得上。
            #
            # 原来这里用「按键数量超过阈值就拒绝」,实测证明那个判据是错的:
            # QMK 系固件不管物理几个键都声明整个键位范围,两键小键盘声明了
            # 279 个键、比主键盘还多。数量区分不出主键盘。
            #
            # 名字匹配挡的是真实风险:by-id 路径在换硬件后指向了别的设备。
            expect = (self.cfg.grab_expect_name or "").strip()
            if expect and expect.lower() not in dev.name.lower():
                actual = dev.name
                dev.close()
                raise HotkeyError(
                    f"拒绝独占:配置里 grab_expect_name={expect!r},"
                    f"但这个设备叫 {actual!r}。\n"
                    "独占是整设备级的,认错设备会把那个设备的所有输入都吞掉。"
                    "确认无误后再改配置。"
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
        # 设备断开时清掉,否则重连后会残留"某个修饰键还按着"的假状态
        self._held_mods.clear()

    # ── 事件循环 ────────────────────────────────────────────────────

    def _handle_key(self, value: int) -> None:
        from .i18n import t

        source = t("daemon.src_hotkey") % self.cfg.key
        if self.cfg.mode == "hold":
            if value == KEY_DOWN:
                self.daemon.start_recording(source)
            elif value == KEY_UP:
                self.daemon.stop_recording()
            # KEY_REPEAT(按住不放时内核发的重复事件)直接忽略
        elif self.cfg.mode == "toggle":
            if value == KEY_DOWN:
                self.daemon.toggle(source)
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
                    if event.type != ecodes.EV_KEY:
                        continue

                    # 先更新修饰键按住状态(组合键靠它判断条件够不够)
                    if event.code in self._mod_codes:
                        if event.value == KEY_DOWN:
                            self._held_mods.add(event.code)
                        elif event.value == KEY_UP:
                            self._held_mods.discard(event.code)
                        continue

                    if event.code != self.keycode:
                        continue
                    # 组合键:按下时必须所有要求的修饰键都按住了。
                    # 松开时不查 —— 松手顺序不该影响 hold 模式的结束。
                    if (event.value == KEY_DOWN
                            and not self.required_mods <= self._held_mods):
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
