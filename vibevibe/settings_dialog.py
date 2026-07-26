"""设置界面(GTK)。

托盘菜单里点「设置…」打开的就是它。

**为什么还留着配置文件**:界面上只放常用的十几项,而全部可调项有七十多个,
每一项都带着"这个值是怎么测出来的"的注释——那些注释比值本身还值钱,
不该塞进界面,也不该丢掉。所以界面是**配置文件的常用项前端**,
高级项仍然去文件里改,界面上给一个直达入口。

改动怎么落地:
    改控件 → **只暂存在内存里**,不碰配置文件
      → 点「保存」才写进 ~/.config/vibevibe/config.toml
         (逐行替换,保留全部注释)
      → 通知守护进程 reload
      → 能热改的立刻生效;要重启的会明确提示

**为什么要暂存而不是即改即存**:开关拨错了得能反悔。即改即存看着"省事",
实际是把撤销的责任丢给用户自己去回想改过什么。点「取消」就该干干净净
什么都没发生。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import ipc
from .config import USER_CONFIG_PATH, Config, load_config, patch_config_value
from .i18n import LANGUAGES, set_language, t

# 界面上放哪些项。刻意只放常用的 —— 全放会变成一堵墙,反而没人看。
BACKENDS = [
    ("qwen_onnx_1p7b", "Qwen3-ASR 1.7B  (更准)", "Qwen3-ASR 1.7B  (accurate)"),
    ("qwen_onnx", "Qwen3-ASR 0.6B  (更快)", "Qwen3-ASR 0.6B  (faster)"),
    ("whisper_ct2", "Whisper large-v3-turbo  (对照基线)",
     "Whisper large-v3-turbo  (baseline)"),
]


class SettingsDialog:
    def __init__(self, cfg: Config, gtk_mods) -> None:  # noqa: ANN001
        self.Gtk, self.GLib = gtk_mods
        self.cfg = cfg
        self.dirty_restart = False
        # 暂存的改动: (段, 键) → 值。点「保存」才落盘。
        self.pending: dict[tuple[str, str], object] = {}
        # 这些键改了要重启服务才生效
        self.pending_restart: set[tuple[str, str]] = set()
        self._building = True   # 构造期间别把初始值当成用户改动

        Gtk = self.Gtk
        self.win = Gtk.Window(title=t("settings.title"))
        self.win.set_default_size(560, 400)
        self.win.set_position(Gtk.WindowPosition.CENTER)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.win.add(outer)

        self.notebook = Gtk.Notebook()
        self.notebook.set_margin_top(8)
        self.notebook.set_margin_start(8)
        self.notebook.set_margin_end(8)
        outer.pack_start(self.notebook, True, True, 0)

        self._page_general()
        self._page_perf()
        self._page_feedback()
        self._page_advanced()

        # 底部状态条 + 关闭
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(8)
        bar.set_margin_bottom(10)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        self.status = Gtk.Label(label="")
        self.status.set_xalign(0.0)
        bar.pack_start(self.status, True, True, 0)

        self.btn_restart = Gtk.Button(label=t("settings.restart_now"))
        self.btn_restart.connect("clicked", self.on_restart)
        self.btn_restart.set_no_show_all(True)
        bar.pack_start(self.btn_restart, False, False, 0)

        btn_cancel = Gtk.Button(label=t("settings.cancel"))
        btn_cancel.connect("clicked", self.on_cancel)
        bar.pack_start(btn_cancel, False, False, 0)

        self.btn_save = Gtk.Button(label=t("settings.save"))
        self.btn_save.get_style_context().add_class("suggested-action")
        self.btn_save.connect("clicked", self.on_save)
        self.btn_save.set_sensitive(False)   # 没改动时按不动
        bar.pack_start(self.btn_save, False, False, 0)

        outer.pack_start(bar, False, False, 0)

        # 点窗口的 × 时,如果还有没保存的改动,要拦一下再问
        self.win.connect("delete-event", self.on_delete)

        # 明确停在第一页。不设的话某些 GTK 版本会停在最后添加的那页,
        # 打开设置第一眼看到「高级」很奇怪。
        self.notebook.set_current_page(0)

        self.win.show_all()
        self.btn_restart.hide()
        self._building = False
        self._update_status()

    # ── 布局小工具 ──────────────────────────────────────────────────

    def _page(self, title: str):  # noqa: ANN202
        Gtk = self.Gtk
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        self.notebook.append_page(box, Gtk.Label(label=title))
        return box

    def _row(self, box, label_text: str, widget, hint: str = ""):  # noqa: ANN001
        """一行设置:标题在上、控件靠右、说明在下(小字灰色)。

        说明文字很重要 —— 这个项目里几乎每个默认值背后都有一次实测,
        不写出来的话用户只会看到一堆不知道该不该动的开关。
        """
        Gtk = self.Gtk
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)

        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        lbl = Gtk.Label(label=label_text)
        lbl.set_xalign(0.0)
        line.pack_start(lbl, True, True, 0)
        line.pack_start(widget, False, False, 0)
        row.pack_start(line, False, False, 0)

        if hint:
            h = Gtk.Label()
            h.set_markup(f'<span size="small" alpha="60%">{_esc(hint)}</span>')
            h.set_xalign(0.0)
            h.set_line_wrap(True)
            h.set_max_width_chars(58)
            row.pack_start(h, False, False, 0)

        box.pack_start(row, False, False, 0)
        return row

    # ── 各页 ────────────────────────────────────────────────────────

    def _page_general(self) -> None:
        Gtk = self.Gtk
        box = self._page(t("settings.tab_general"))

        # 语言
        self.combo_lang = Gtk.ComboBoxText()
        for code, name in LANGUAGES.items():
            self.combo_lang.append(code, name)
        self.combo_lang.set_active_id(self.cfg.ui.language)
        self.combo_lang.connect("changed", self.on_language)
        self._row(box, t("settings.language"), self.combo_lang,
                  t("settings.language_hint"))

        # 模型
        self.combo_backend = Gtk.ComboBoxText()
        lang = self.cfg.ui.language
        for code, zh, en in BACKENDS:
            self.combo_backend.append(code, zh if lang == "zh" else en)
        self.combo_backend.set_active_id(self.cfg.asr.backend)
        self.combo_backend.connect(
            "changed", self.on_simple, "asr", "backend", True)
        self._row(box, t("settings.backend"), self.combo_backend,
                  t("settings.backend_hint"))

        # 麦克风
        self.combo_mic = Gtk.ComboBoxText()
        self.combo_mic.append("", t("settings.mic_default"))
        try:
            import sounddevice as sd

            for d in sd.query_devices():
                if d["max_input_channels"] > 0:
                    self.combo_mic.append(d["name"], d["name"])
        except Exception:
            pass
        self.combo_mic.set_active_id(self.cfg.audio.input_device or "")
        self.combo_mic.connect("changed", self.on_simple, "audio", "input_device", False)
        self._row(box, t("settings.mic"), self.combo_mic)

    def _page_perf(self) -> None:
        Gtk = self.Gtk
        box = self._page(t("settings.tab_perf"))

        self.sw_hot = Gtk.Switch()
        self.sw_hot.set_active(self.cfg.daemon.hot_reload)
        self.sw_hot.connect("notify::active", self.on_hot)
        self._row(box, t("settings.hot_reload"), _wrap(Gtk, self.sw_hot),
                  t("settings.hot_reload_hint"))

        self.spin_idle = Gtk.SpinButton.new_with_range(5, 3600, 5)
        self.spin_idle.set_value(self.cfg.daemon.idle_unload_sec)
        self.spin_idle.connect(
            "value-changed", self.on_spin, "daemon", "idle_unload_sec", False)
        self._row(box, t("settings.idle_unload"), self.spin_idle,
                  t("settings.idle_unload_hint"))

        self.spin_threads = Gtk.SpinButton.new_with_range(0, 64, 1)
        self.spin_threads.set_value(
            self.cfg.asr.qwen_onnx_1p7b.intra_op_num_threads)
        self.spin_threads.connect(
            "value-changed", self.on_spin,
            "asr.qwen_onnx_1p7b", "intra_op_num_threads", True)
        self._row(box, t("settings.threads"), self.spin_threads,
                  t("settings.threads_hint"))

    def _page_feedback(self) -> None:
        Gtk = self.Gtk
        box = self._page(t("settings.tab_feedback"))

        self.sw_sound = Gtk.Switch()
        self.sw_sound.set_active(self.cfg.sound.enabled)
        self.sw_sound.connect(
            "notify::active", self.on_switch, "sound", "enabled", False)
        self._row(box, t("settings.sound_enabled"), _wrap(Gtk, self.sw_sound),
                  t("settings.sound_hint"))

        vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.scale_vol = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.02)
        self.scale_vol.set_size_request(180, -1)
        self.scale_vol.set_value(self.cfg.sound.volume)
        self.scale_vol.set_draw_value(False)
        self.scale_vol.connect(
            "value-changed", self.on_scale, "sound", "volume", False)
        vol_box.pack_start(self.scale_vol, False, False, 0)
        btn_test = Gtk.Button(label=t("settings.test_sound"))
        btn_test.connect("clicked", self.on_test_sound)
        vol_box.pack_start(btn_test, False, False, 0)
        self._row(box, t("settings.volume"), vol_box)

        self.combo_inject = Gtk.ComboBoxText()
        self.combo_inject.append("clipboard_paste", t("settings.inject_clipboard"))
        self.combo_inject.append("type", t("settings.inject_type"))
        self.combo_inject.set_active_id(self.cfg.inject.method)
        self.combo_inject.connect("changed", self.on_simple, "inject", "method", False)
        self._row(box, t("settings.inject_method"), self.combo_inject)

    def _page_advanced(self) -> None:
        Gtk = self.Gtk
        box = self._page(t("settings.tab_advanced"))

        self.sw_preproc = Gtk.Switch()
        self.sw_preproc.set_active(self.cfg.preprocess_audio.enabled)
        self.sw_preproc.connect(
            "notify::active", self.on_switch, "preprocess_audio", "enabled", False)
        self._row(box, t("settings.preproc"), _wrap(Gtk, self.sw_preproc),
                  t("settings.preproc_hint"))

        btn = Gtk.Button(label=t("settings.open_config_file"))
        btn.connect("clicked", self.on_open_config)
        self._row(box, "", btn, t("settings.open_config_hint"))

    # ── 回调 ────────────────────────────────────────────────────────

    def _stage(self, section: str, key: str, value, needs_restart: bool) -> None:  # noqa: ANN001
        """把一处改动记下来,但**先不写盘**。点「保存」才真正落地。"""
        if self._building:
            return
        self.pending[(section, key)] = value
        if needs_restart:
            self.pending_restart.add((section, key))
        self._update_status()

    def _update_status(self) -> None:
        n = len(self.pending)
        self.btn_save.set_sensitive(n > 0)
        if n == 0:
            self.status.set_markup(
                f'<span size="small" alpha="55%">{_esc(t("settings.no_change"))}</span>')
        else:
            self.status.set_markup(
                f'<span size="small">● {_esc(t("settings.unsaved") % n)}</span>')

    def _notify_daemon(self) -> None:
        try:
            ipc.command(self.cfg.socket_path, ipc.CMD_RELOAD, timeout=8.0)
        except ipc.IpcError:
            pass  # 服务没开着也没关系,配置已经写进文件了

    def on_language(self, combo) -> None:  # noqa: ANN001
        code = combo.get_active_id()
        if not code or self._building:
            return
        # 语言也走暂存。之前是选了就立刻重建窗口,那样"取消"就没意义了。
        # 保存之后再重建,界面文字才跟着换。
        self._stage("ui", "language", code, needs_restart=False)

    def on_simple(self, combo, section, key, restart) -> None:  # noqa: ANN001
        value = combo.get_active_id()
        if value is None:
            return
        self._stage(section, key, value, restart)

    def on_switch(self, sw, _p, section, key, restart) -> None:  # noqa: ANN001
        self._stage(section, key, sw.get_active(), restart)

    def on_spin(self, spin, section, key, restart) -> None:  # noqa: ANN001
        value = spin.get_value()
        self._stage(section, key, int(value) if float(value).is_integer() else value,
                    restart)

    def on_scale(self, scale, section, key, restart) -> None:  # noqa: ANN001
        self._stage(section, key, round(scale.get_value(), 3), restart)

    def on_hot(self, sw, _p) -> None:  # noqa: ANN001
        # 热加载同样暂存。托盘菜单里那个开关是即时的(那儿就该即时),
        # 但设置窗口里所有东西都得能被「取消」撤销,不能有例外。
        self._stage("daemon", "hot_reload", sw.get_active(), needs_restart=False)

    def on_test_sound(self, _btn) -> None:  # noqa: ANN001
        from .sound import Player

        cfg = load_config().sound
        cfg.volume = self.scale_vol.get_value()
        cfg.enabled = True
        Player(cfg).play("start")

    def on_open_config(self, _btn) -> None:  # noqa: ANN001
        path = USER_CONFIG_PATH if USER_CONFIG_PATH.exists() else self.cfg.source_path
        if path:
            subprocess.Popen(["xdg-open", str(path)])

    def on_save(self, _btn=None) -> None:  # noqa: ANN001
        """把暂存的改动一次性写进配置文件,再通知守护进程。"""
        if not self.pending:
            return

        lang_changed = ("ui", "language") in self.pending
        hot = self.pending.get(("daemon", "hot_reload"))
        failed = []

        for (section, key), value in self.pending.items():
            if not patch_config_value(USER_CONFIG_PATH, section, key, value):
                failed.append(f"{section}.{key}")

        if failed:
            self.status.set_markup(
                f'<span size="small">✗ {_esc(t("settings.save_failed") % ", ".join(failed))}</span>')
            return

        # 热加载要走专用命令(它会顺手加载/卸载模型),不是光改配置就完事
        if hot is not None:
            try:
                ipc.command(self.cfg.socket_path, ipc.CMD_SET_HOT,
                            hot=bool(hot), timeout=15.0)
            except ipc.IpcError:
                pass
        self._notify_daemon()

        if ("ui", "language") in self.pending:
            set_language(str(self.pending[("ui", "language")]))
        needs_restart = bool(self.pending_restart)

        self.pending.clear()
        self.pending_restart.clear()
        self.dirty_restart = needs_restart

        if lang_changed:
            # 语言变了,整个窗口的文字都得换 —— 重建是最省事也最可靠的办法
            self.win.destroy()
            dlg = SettingsDialog(load_config(), (self.Gtk, self.GLib))
            dlg.dirty_restart = needs_restart
            if needs_restart:
                dlg.btn_restart.show()
                dlg.status.set_markup(
                    f'<span size="small">⚠ {_esc(t("settings.restart_needed"))}</span>')
            return

        self._update_status()
        if needs_restart:
            self.btn_restart.show()
            self.status.set_markup(
                f'<span size="small">⚠ {_esc(t("settings.restart_needed"))}</span>')
        else:
            self.status.set_markup(
                f'<span size="small" alpha="60%">{_esc(t("settings.saved"))}</span>')

    def on_cancel(self, _btn=None) -> None:  # noqa: ANN001
        """直接关掉。因为什么都没写盘,所以不需要"撤销"——本来就没发生过。"""
        self.win.destroy()

    def on_delete(self, _win, _event) -> bool:  # noqa: ANN001
        """点窗口的 × 。有未保存改动就先问一句,免得手滑丢掉。"""
        if not self.pending:
            return False    # 没改动,正常关

        Gtk = self.Gtk
        dlg = Gtk.MessageDialog(
            transient_for=self.win, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            text=t("settings.discard_title"),
            secondary_text=t("settings.discard_body") % len(self.pending),
        )
        dlg.add_button(t("settings.discard_back"), Gtk.ResponseType.CANCEL)
        dlg.add_button(t("settings.discard_yes"), Gtk.ResponseType.OK)
        dlg.add_button(t("settings.save"), Gtk.ResponseType.ACCEPT)
        resp = dlg.run()
        dlg.destroy()

        if resp == Gtk.ResponseType.ACCEPT:
            self.on_save()
            return False
        if resp == Gtk.ResponseType.OK:
            return False    # 放弃,照常关
        return True         # 拦住,回去继续改

    def on_restart(self, _btn) -> None:  # noqa: ANN001
        subprocess.run(["systemctl", "--user", "restart", "vibevibe.service"],
                       capture_output=True, timeout=30)
        self.dirty_restart = False
        self.btn_restart.hide()
        self.status.set_markup(
            f'<span size="small" alpha="60%">{_esc(t("settings.saved"))}</span>')


def _wrap(Gtk, widget):  # noqa: ANN001
    """Switch 默认会被拉伸得很难看,包一层让它保持原始大小。"""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.set_valign(Gtk.Align.CENTER)
    box.pack_start(widget, False, False, 0)
    return box


def _esc(s: str) -> str:
    from html import escape

    return escape(s)


def open_settings(gtk_mods, cfg: Config | None = None) -> None:  # noqa: ANN001
    SettingsDialog(cfg or load_config(), gtk_mods)


def main() -> int:
    """独立窗口方式打开(`vibevibe settings`),不经过托盘。"""
    from .tray import _import_gi

    try:
        Gtk, GLib, _ = _import_gi()
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    cfg = load_config()
    dlg = SettingsDialog(cfg, (Gtk, GLib))
    dlg.win.connect("destroy", Gtk.main_quit)
    Gtk.main()
    return 0
