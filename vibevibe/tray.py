"""系统托盘图标。

GNOME 46 砍掉了内置托盘,靠 `ubuntu-appindicators` 扩展(Ubuntu 自带并默认
启用)提供宿主端,我们这边用 AyatanaAppIndicator3 挂上去。

两个开关,对应两件截然不同的事:

    服务开关   —— 整个守护进程的生死。关掉 = 进程被 systemd 杀掉,
                  **所有内存全部还给系统**(包括那 200 多 MB 的运行时开销),
                  只剩托盘自己
    热加载开关 —— 服务活着的前提下,模型要不要一直待在内存里。
                  开 ≈ 3.8GB 常驻、按键立刻出字;
                  关 ≈ 0.24GB、每次冷启动多等 1.7~2.5 秒

**托盘是独立进程,不是守护进程的一部分**——必须如此,否则"关掉服务"就把
托盘自己也关掉了,再也没法打开。

它跟外界只有两条通道:
    systemctl --user  控制服务生死
    Unix socket       查状态、切热加载
两条都很轻,托盘自己不加载任何模型。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import ipc
from .config import USER_CONFIG_PATH, Config, load_config
from .i18n import set_language, t
from .service import SERVICE, is_active, is_installed, stop_all, systemctl

# 状态 → 图标名。图标本身在 data/icons/ 里。
ICON_OFF = "vibevibe-off"
ICON_IDLE = "vibevibe-idle"
ICON_RECORDING = "vibevibe-recording"
ICON_BUSY = "vibevibe-busy"
ICON_COLD = "vibevibe-cold"

# 轮询间隔。托盘要跟得上"开始录音→出字"这种一两秒的变化,
# 但也不能太密——它是个常驻进程,别自己变成负担。
POLL_MS = 1200

GI_SYSTEM_PATH = "/usr/lib/python3/dist-packages"

MISSING_GI_HINT = """\
托盘需要 PyGObject 和 AppIndicator 绑定,当前环境里没有。

Debian / Ubuntu:
    sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1

如果你用的是虚拟环境,注意 venv 默认看不到系统的 python3-gi。
两个办法:
    · 建 venv 时加 --system-site-packages
    · 或者让 vibevibe 自己去找(它会自动尝试 /usr/lib/python3/dist-packages)

另外 GNOME 46+ 需要托盘扩展才能显示图标(Ubuntu 自带 ubuntu-appindicators,
默认已启用)。
"""


def _import_gi():
    """把 gi 引进来。venv 里看不到系统的 python3-gi 时,自动去找一次。

    这不是什么优雅的做法,但比让用户重建 venv 强 —— PyGObject 从 pip 装
    需要一堆编译依赖,而系统上通常本来就有一份现成的。
    """
    try:
        import gi  # noqa: F401
    except ImportError:
        if GI_SYSTEM_PATH not in sys.path and Path(GI_SYSTEM_PATH).is_dir():
            sys.path.append(GI_SYSTEM_PATH)
        try:
            import gi  # noqa: F401
        except ImportError as exc:
            raise ImportError(MISSING_GI_HINT) from exc

    import gi

    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as appindicator
    except (ValueError, ImportError):
        try:
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3 as appindicator
        except (ValueError, ImportError) as exc:
            raise ImportError(MISSING_GI_HINT) from exc

    from gi.repository import GLib, Gtk

    return Gtk, GLib, appindicator


class Tray:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        set_language(cfg.ui.language)
        self.Gtk, self.GLib, self.appindicator = _import_gi()

        # 守护进程的最新状态。拿不到就是 None(服务没跑)
        self.status: dict | None = None
        self._suppress = False  # 程序化改开关时,别触发回调

        icon_dir = Path(__file__).resolve().parent / "data" / "icons"
        self.indicator = self.appindicator.Indicator.new_with_path(
            "vibevibe",
            ICON_OFF,
            self.appindicator.IndicatorCategory.APPLICATION_STATUS,
            str(icon_dir),
        )
        self.indicator.set_status(self.appindicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("vibevibe")

        self._build_menu()
        self._refresh()
        self.GLib.timeout_add(POLL_MS, self._tick)

    # ── 菜单 ────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        Gtk = self.Gtk
        menu = Gtk.Menu()

        self.item_state = Gtk.MenuItem(label="…")
        self.item_state.set_sensitive(False)
        menu.append(self.item_state)

        self.item_mem = Gtk.MenuItem(label="")
        self.item_mem.set_sensitive(False)
        menu.append(self.item_mem)

        menu.append(Gtk.SeparatorMenuItem())

        self.item_service = Gtk.CheckMenuItem(label=t("tray.service"))
        self.item_service.connect("toggled", self.on_service_toggled)
        menu.append(self.item_service)

        self.item_hot = Gtk.CheckMenuItem(label=t("tray.hot_reload"))
        self.item_hot.connect("toggled", self.on_hot_toggled)
        menu.append(self.item_hot)

        menu.append(Gtk.SeparatorMenuItem())

        self.item_settings = Gtk.MenuItem(label=t("tray.settings"))
        self.item_settings.connect("activate", self.on_settings)
        menu.append(self.item_settings)

        item_about = Gtk.MenuItem(label=t("tray.about"))
        item_about.connect("activate", self.on_about)
        menu.append(item_about)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label=t("tray.quit"))
        item_quit.connect("activate", self.on_quit)
        menu.append(item_quit)

        item_quit_all = Gtk.MenuItem(label=t("tray.quit_all"))
        item_quit_all.connect("activate", self.on_quit_all)
        menu.append(item_quit_all)

        menu.show_all()
        self.indicator.set_menu(menu)

    # ── 状态刷新 ────────────────────────────────────────────────────

    def _poll_daemon(self) -> dict | None:
        try:
            return ipc.command(self.cfg.socket_path, ipc.CMD_STATUS, timeout=2.0)
        except ipc.IpcError:
            return None

    def _refresh(self) -> None:
        active = is_active()
        self.status = self._poll_daemon() if active else None
        st = self.status or {}

        # ── 图标 ──
        if not active or self.status is None:
            icon, label = ICON_OFF, t("tray.title_off")
        else:
            state = st.get("state", "")
            if state == ipc.STATE_RECORDING:
                icon, label = ICON_RECORDING, t("tray.title_recording")
            elif state in (ipc.STATE_TRANSCRIBING, ipc.STATE_LOADING):
                icon, label = ICON_BUSY, t("tray.title_busy")
            elif not st.get("model_in_memory", True):
                icon, label = ICON_COLD, t("tray.title_cold")
            else:
                icon, label = ICON_IDLE, t("tray.title_idle")
        self.indicator.set_icon_full(icon, label)

        # ── 菜单文字 ──
        self.item_state.set_label(f"vibevibe · {label}")
        if self.status:
            mem = st.get("memory_mb")
            last = (st.get("last_text") or "").strip()
            self.item_mem.set_label(
                (t("tray.memory") % mem) + ("   " + t("tray.last") % last[:24] if last else ""))
            self.item_mem.show()
        else:
            self.item_mem.set_label(t("tray.memory_off"))

        # ── 两个开关 ──
        self._suppress = True
        self.item_service.set_active(active)
        hot = bool(st.get("hot_reload", self.cfg.daemon.hot_reload))
        self.item_hot.set_active(hot if active else False)
        # 热加载只有在服务开着时才有意义
        self.item_hot.set_sensitive(active and self.status is not None)
        self._suppress = False

    def _tick(self) -> bool:
        try:
            self._refresh()
        except Exception as exc:  # 托盘绝不能因为一次刷新失败就死掉
            print(t("tray.refresh_error") % exc, file=sys.stderr)
        return True  # 继续下一轮

    # ── 回调 ────────────────────────────────────────────────────────

    def on_service_toggled(self, item) -> None:  # noqa: ANN001
        if self._suppress:
            return
        want = item.get_active()
        if want:
            ok, out = systemctl("start", SERVICE)
        else:
            ok, out = systemctl("stop", SERVICE)
        if not ok:
            print(t("tray.service_failed") % out, file=sys.stderr)
        # 服务起停要点时间,稍后再刷新一次
        self.GLib.timeout_add(700, self._refresh_once)

    def on_hot_toggled(self, item) -> None:  # noqa: ANN001
        if self._suppress:
            return
        want = item.get_active()
        try:
            ipc.command(self.cfg.socket_path, ipc.CMD_SET_HOT, hot=want, timeout=5.0)
        except ipc.IpcError as exc:
            print(t("tray.hot_failed") % exc, file=sys.stderr)
        self.GLib.timeout_add(400, self._refresh_once)

    def _refresh_once(self) -> bool:
        self._refresh()
        return False  # 只跑一次

    def _error_dialog(self, title: str, detail: str) -> None:
        """把异常摆到用户面前。

        托盘通常是 nohup 起的,stderr 进了黑洞 —— 菜单点了没反应而没有
        任何提示,是最难查的一类故障(实际发生过:托盘跑着旧代码,
        新配置项它不认识,load_config 抛异常,点「设置」就是没反应)。
        """
        Gtk = self.Gtk
        dlg = Gtk.MessageDialog(
            modal=True, message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE, text=title, secondary_text=detail)
        dlg.run()
        dlg.destroy()

    def on_settings(self, _item) -> None:  # noqa: ANN001
        try:
            from .settings_dialog import SettingsDialog

            # 设置窗口关掉之后语言可能变了,菜单文案得跟着重建
            dlg = SettingsDialog(load_config(), (self.Gtk, self.GLib))
            dlg.win.connect("destroy", lambda _w: self._reload_language())
        except Exception as exc:
            self._error_dialog(
                t("tray.settings_failed"),
                f"{type(exc).__name__}: {exc}\n\n"
                + t("tray.stale_hint"))

    def _reload_language(self) -> None:
        cfg = load_config()
        if cfg.ui.language != self.cfg.ui.language:
            self.cfg = cfg
            set_language(cfg.ui.language)
            self._build_menu()
            self._refresh()

    def on_open_log(self, _item) -> None:  # noqa: ANN001
        # 优先看 systemd 的日志(服务是它管的);它不在就退回文件
        if is_installed():
            subprocess.Popen([
                "x-terminal-emulator", "-e",
                "journalctl --user -u vibevibe -f",
            ]) if _which("x-terminal-emulator") else subprocess.Popen(
                ["xdg-open", str(self.cfg.log_path)])
        else:
            subprocess.Popen(["xdg-open", str(self.cfg.log_path)])

    def on_about(self, _item) -> None:  # noqa: ANN001
        """关于窗口:版本 + 关键路径 + 日志入口。

        日志入口放这儿而不是菜单里 —— 菜单该只放**常用**的东西
        (两个开关 + 设置),看日志是排查时才做的事,归到关于里更合适。
        """
        try:
            self._about_dialog()
        except Exception as exc:
            self._error_dialog(t("about.title"), f"{type(exc).__name__}: {exc}")

    def _about_dialog(self) -> None:
        from . import __version__
        from .config import USER_CONFIG_PATH, data_root, is_source_checkout

        Gtk = self.Gtk
        cfg = load_config()

        win = Gtk.Window(title=t("about.title"))
        win.set_default_size(480, 300)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_modal(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(18)
        outer.set_margin_bottom(14)
        outer.set_margin_start(20)
        outer.set_margin_end(20)
        win.add(outer)

        # 图标 + 标题
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        icon_path = Path(__file__).resolve().parent / "data" / "icons" / "vibevibe-idle.svg"
        if icon_path.exists():
            try:
                from gi.repository import GdkPixbuf

                pix = GdkPixbuf.Pixbuf.new_from_file_at_size(str(icon_path), 56, 56)
                head.pack_start(Gtk.Image.new_from_pixbuf(pix), False, False, 0)
            except Exception:
                pass
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = Gtk.Label()
        name.set_markup(f'<span size="x-large" weight="bold">vibevibe {__version__}</span>')
        name.set_xalign(0.0)
        title_box.pack_start(name, False, False, 0)
        desc = Gtk.Label()
        desc.set_markup(f'<span alpha="70%">{_esc(t("about.desc"))}</span>')
        desc.set_xalign(0.0)
        desc.set_line_wrap(True)
        title_box.pack_start(desc, False, False, 0)
        head.pack_start(title_box, True, True, 0)
        outer.pack_start(head, False, False, 0)

        outer.pack_start(Gtk.Separator(), False, False, 14)

        # 关键信息表
        grid = Gtk.Grid()
        grid.set_column_spacing(14)
        grid.set_row_spacing(6)
        rows = [
            (t("about.runmode"),
             t("about.mode_dev") if is_source_checkout() else t("about.mode_pip")),
            (t("about.backend"), cfg.asr.backend),
            (t("about.config"), str(cfg.source_path or USER_CONFIG_PATH)),
            (t("about.data"), str(data_root())),
            (t("about.log"), str(cfg.log_path)),
        ]
        for i, (k, v) in enumerate(rows):
            kl = Gtk.Label()
            kl.set_markup(f'<span alpha="60%">{_esc(k)}</span>')
            kl.set_xalign(0.0)
            kl.set_valign(Gtk.Align.START)
            grid.attach(kl, 0, i, 1, 1)
            vl = Gtk.Label(label=v)
            vl.set_xalign(0.0)
            vl.set_selectable(True)      # 路径要能选中复制
            vl.set_line_wrap(True)
            vl.set_max_width_chars(46)
            grid.attach(vl, 1, i, 1, 1)
        outer.pack_start(grid, True, True, 0)

        # 底部按钮
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(16)

        btn_log = Gtk.Button(label=t("about.view_log"))
        btn_log.connect("clicked", lambda _b: self.on_open_log(None))
        bar.pack_start(btn_log, False, False, 0)

        btn_cfg = Gtk.Button(label=t("about.open_config"))
        btn_cfg.connect("clicked", lambda _b: self._open_path(
            USER_CONFIG_PATH if USER_CONFIG_PATH.exists() else cfg.source_path))
        bar.pack_start(btn_cfg, False, False, 0)

        btn_web = Gtk.Button(label=t("about.project"))
        btn_web.connect("clicked", lambda _b: subprocess.Popen(
            ["xdg-open", "https://github.com/jeffliulab/vibevibe"]))
        bar.pack_start(btn_web, False, False, 0)

        btn_close = Gtk.Button(label=t("about.close"))
        btn_close.connect("clicked", lambda _b: win.destroy())
        bar.pack_end(btn_close, False, False, 0)

        outer.pack_start(bar, False, False, 0)
        win.show_all()

    def _open_path(self, path) -> None:  # noqa: ANN001
        if path:
            subprocess.Popen(["xdg-open", str(path)])

    def on_quit(self, _item) -> None:  # noqa: ANN001
        """只关托盘,守护进程照旧跑着。

        留着它是有用的:托盘跑着旧代码时,「退出托盘 + 重新 vibevibe tray」
        是最快的自救(见 tray.stale_hint)。但它不该是**唯一**的退出——
        真正的「退出程序」是下面那个 on_quit_all。
        """
        self.Gtk.main_quit()

    def on_quit_all(self, _item) -> None:  # noqa: ANN001
        """退出整个 vibevibe:停掉守护进程,再关掉托盘自己。

        托盘是独立进程,所以「退出程序」必须显式做两件事——
        只关托盘会留下一个还在占内存的守护进程,只停服务会留下一个
        指着空气的托盘图标。
        """
        if not self._confirm(t("tray.quit_confirm_title"), t("tray.quit_confirm_body")):
            return

        ok, notes = stop_all(self.cfg)
        if not ok:
            self._error_dialog(t("tray.quit_failed"), "\n".join(notes))

        # 无论守护进程停没停干净,托盘都得退——「退出」按下去必须真的退出,
        # 否则就又回到了用户一开始遇到的那个问题。
        self.Gtk.main_quit()

    def _confirm(self, title: str, detail: str) -> bool:
        Gtk = self.Gtk
        dlg = Gtk.MessageDialog(
            modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL, text=title, secondary_text=detail)
        answer = dlg.run()
        dlg.destroy()
        return answer == Gtk.ResponseType.OK

    def run(self) -> int:
        self.Gtk.main()
        return 0


def _esc(text: str) -> str:
    from html import escape

    return escape(text)


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def run_tray(config_path: Path | None = None) -> int:
    cfg = load_config(config_path)
    try:
        tray = Tray(cfg)
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return tray.run()
