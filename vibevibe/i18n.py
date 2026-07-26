"""界面和日志的中英切换。

为什么不用 gettext:gettext 要维护 .po/.mo 还得编译,对一个自带两种语言的
小工具来说太重。这里就是一张表,查不到就回落到中文,改起来一目了然。

用法:

    from .i18n import t, set_language
    set_language("en")
    log.info(t("daemon.started"), pid)     # 日志保持惰性格式化,不预先拼串

**只翻译用户看得见的东西**(日志、菜单、设置界面、命令行输出)。
代码注释一律保持中文——那是写给维护者看的,不是给用户看的。
"""

from __future__ import annotations

LANGUAGES = {
    "zh": "中文",
    "en": "English",
}

DEFAULT_LANGUAGE = "zh"

_current = DEFAULT_LANGUAGE


def set_language(lang: str) -> str:
    """切换语言。不认识的值一律回落到默认,不报错——
    语言设错不该让整个程序起不来。"""
    global _current
    _current = lang if lang in LANGUAGES else DEFAULT_LANGUAGE
    return _current


def get_language() -> str:
    return _current


def t(key: str) -> str:
    """查一条文案。查不到就把 key 原样返回,方便一眼看出漏翻了哪条。"""
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    return entry.get(_current) or entry.get(DEFAULT_LANGUAGE) or key


def _m(zh: str, en: str) -> dict[str, str]:
    return {"zh": zh, "en": en}


# ── 文案表 ──────────────────────────────────────────────────────────────
#
# 带 % 占位符的条目是给日志用的,两种语言的占位符个数和顺序**必须一致**,
# 否则切了语言就会在格式化时炸掉。

MESSAGES: dict[str, dict[str, str]] = {
    # ── 守护进程 ──
    "daemon.started": _m("vibevibe 守护进程启动 (pid %d)", "vibevibe daemon started (pid %d)"),
    "daemon.config": _m("配置文件: %s", "Config file: %s"),
    "daemon.listening": _m("监听 %s", "Listening on %s"),
    "daemon.shutting_down": _m("正在关闭", "Shutting down"),
    "daemon.shutdown_cmd": _m("收到关闭命令", "Received shutdown command"),
    "daemon.loading_model": _m("正在加载模型 %s ...", "Loading model %s ..."),
    "daemon.model_ready": _m("模型就绪", "Model ready"),
    "daemon.model_load_failed": _m("模型加载失败: %s", "Failed to load model: %s"),
    "daemon.missing_inject_tools": _m(
        "缺少注入工具 %s,识别出来的文字将无法打进光标处。装一下: sudo apt install %s",
        "Missing injection tools %s; transcribed text cannot be typed at the cursor. "
        "Install with: sudo apt install %s"),
    "daemon.rec_start": _m("开始录音", "Recording started"),
    "daemon.rec_start_failed": _m("开始录音失败: %s", "Failed to start recording: %s"),
    "daemon.rec_stop_failed": _m("停止录音失败: %s", "Failed to stop recording: %s"),
    "daemon.rec_too_short": _m("录音为空或过短,忽略", "Recording empty or too short, ignored"),
    "daemon.rec_done": _m("录音结束 %.2fs,开始转写",
                          "Recording finished (%.2fs), transcribing"),
    "daemon.rec_cancelled": _m("录音已取消", "Recording cancelled"),
    "daemon.busy_transcribing": _m("上一段还在转写,等它完事",
                                   "Still transcribing the previous clip"),
    "daemon.already_recording": _m("已经在录了", "Already recording"),
    "daemon.not_recording": _m("当前没有在录音", "Not currently recording"),
    "daemon.transcribed": _m(
        "转写完成 %.2fs 音频 / %.2fs 耗时 (RTF %.2f) 语言=%s: %s",
        "Transcribed %.2fs audio in %.2fs (RTF %.2f) language=%s: %s"),
    "daemon.guard_note": _m("兜底闸触发: %s", "Guard tripped: %s"),
    "daemon.guard_blocked": _m("结果被拦下,不注入: %s",
                               "Result blocked, nothing injected: %s"),
    "daemon.transcribe_failed": _m("转写或注入失败: %s",
                                   "Transcription or injection failed: %s"),
    "daemon.preproc": _m("预处理: 削顶率 %.2f%% → %.2f%%,修复 %d 段",
                         "Preprocess: clipping %.2f%% → %.2f%%, repaired %d runs"),
    "daemon.state_change": _m("状态 %s → %s", "State %s → %s"),
    "daemon.stale_socket": _m("清理上次残留的 socket 文件",
                              "Cleaning up stale socket file"),
    "daemon.already_running": _m(
        "已经有一个守护进程在跑了(%s)。要重启的话先: vibevibe shutdown",
        "A daemon is already running (%s). Stop it first: vibevibe shutdown"),
    "daemon.unknown_cmd": _m("不认识的命令 %r", "Unknown command %r"),
    "daemon.hot_on": _m("热加载 → 开(模型常驻)", "Hot reload → on (model stays resident)"),
    "daemon.hot_off_unloaded": _m("热加载 → 关,已卸载模型: %.0f MB → %.0f MB",
                                  "Hot reload → off, model unloaded: %.0f MB → %.0f MB"),
    "daemon.hot_off_busy": _m("热加载 → 关(当前忙,用完后会自动卸载)",
                              "Hot reload → off (busy now, will unload when done)"),
    "daemon.hot_warmed": _m("热加载已开启,模型已载入 (%.0f MB)",
                            "Hot reload enabled, model loaded (%.0f MB)"),
    "daemon.warm_failed": _m("预加载失败: %s", "Preload failed: %s"),
    "daemon.unload_failed": _m("卸载模型失败: %s", "Failed to unload model: %s"),
    "daemon.idle_unloaded": _m(
        "空闲 %.0f 秒,已卸载模型: 内存 %.0f MB → %.0f MB(下次按键自动重新加载,多等一两秒)",
        "Idle for %.0fs, model unloaded: %.0f MB → %.0f MB "
        "(reloads automatically on next use, ~2s extra)"),
    "daemon.hot_status": _m("热加载: %s", "Hot reload: %s"),
    "daemon.hot_on_short": _m("开(模型常驻)", "on (resident)"),
    "daemon.hot_off_short": _m("关(空闲 %.0f 秒后卸载)", "off (unload after %.0fs idle)"),
    "daemon.lang_changed": _m("界面语言 → %s", "Interface language → %s"),

    # ── 录音 ──
    "recorder.stream_status": _m("录音流状态: %s", "Audio stream status: %s"),
    "recorder.overflow": _m("录音达到上限 %.0f 秒,后面的部分被丢弃了",
                            "Recording hit the %.0fs limit; the rest was dropped"),
    "recorder.too_short": _m("录音太短(%.2fs),当作误触丢弃",
                             "Recording too short (%.2fs), treated as a mis-press"),
    "recorder.already": _m("已经在录音了", "Already recording"),
    "recorder.not_recording": _m("没有在录音", "Not recording"),
    "recorder.open_failed": _m("打不开录音设备: %s", "Cannot open audio input: %s"),

    # ── 注入 / 提示音 ──
    "inject.restore_failed": _m("恢复剪贴板失败(不影响已粘贴的文字): %s",
                                "Failed to restore clipboard (pasted text unaffected): %s"),
    "sound.play_failed": _m("提示音播放失败(%s): %s", "Failed to play cue (%s): %s"),

    # ── 热键通道 ──
    "hotkey.grabbed": _m("已独占设备 %r(按键不会漏进当前窗口)",
                         "Grabbed device %r (key presses won't leak to the focused window)"),
    "hotkey.listening": _m("监听 %r 上的 %s [%s 模式]",
                           "Listening for %s on %r [%s mode]"),
    "hotkey.stopped": _m("热键通道停止: %s", "Hotkey channel stopped: %s"),
    "hotkey.no_permission": _m(
        "没有权限读 %s。需要装一条 udev 规则给当前用户放行 —— 见 70-vibevibe.rules.example",
        "No permission to read %s. Install a udev rule for your user — "
        "see 70-vibevibe.rules.example"),
    "hotkey.disconnected": _m("输入设备断开(%s),%.1fs 后重试",
                              "Input device disconnected (%s), retrying in %.1fs"),
    "hotkey.thread_error": _m("热键线程出错,%.1fs 后重试",
                              "Hotkey thread error, retrying in %.1fs"),
    "hotkey.bad_mode": _m("不认识的 hotkey.mode %r,只支持 hold / toggle",
                          "Unknown hotkey.mode %r; only hold / toggle are supported"),

    # ── 托盘 ──
    "tray.title_idle": _m("待命", "Idle"),
    "tray.title_off": _m("服务已关闭", "Service stopped"),
    "tray.title_recording": _m("正在录音…", "Recording…"),
    "tray.title_busy": _m("正在转写…", "Transcribing…"),
    "tray.title_cold": _m("待命(模型未载入)", "Idle (model not loaded)"),
    "tray.memory": _m("内存 %s MB", "Memory %s MB"),
    "tray.memory_off": _m("内存 0 MB(未运行)", "Memory 0 MB (not running)"),
    "tray.last": _m("上次:%s", "Last: %s"),
    "tray.service": _m("服务", "Service"),
    "tray.hot_reload": _m("热加载(模型常驻内存)", "Hot reload (keep model in memory)"),
    "tray.settings": _m("设置…", "Settings…"),
    "tray.open_log": _m("查看日志", "View log"),
    "tray.quit": _m("退出托盘(不影响服务)", "Quit tray (service keeps running)"),
    "tray.refresh_error": _m("[tray] 刷新出错: %s", "[tray] Refresh error: %s"),
    "tray.service_failed": _m("[tray] 服务操作失败: %s", "[tray] Service operation failed: %s"),
    "tray.hot_failed": _m("[tray] 切换热加载失败: %s", "[tray] Failed to toggle hot reload: %s"),

    # ── 设置界面 ──
    "settings.title": _m("vibevibe 设置", "vibevibe Settings"),
    "settings.tab_general": _m("常规", "General"),
    "settings.tab_perf": _m("性能", "Performance"),
    "settings.tab_feedback": _m("反馈", "Feedback"),
    "settings.tab_advanced": _m("高级", "Advanced"),

    "settings.language": _m("界面与日志语言", "Interface & log language"),
    "settings.language_hint": _m(
        "改完立即生效。代码注释始终是中文,那是给维护者看的。",
        "Takes effect immediately. Code comments stay Chinese — they are for maintainers."),
    "settings.backend": _m("识别模型", "Recognition model"),
    "settings.backend_hint": _m(
        "1.7B 更准(技术词命中 77% vs 70%),0.6B 快约两倍。换完要重启服务。",
        "1.7B is more accurate (77% vs 70% on technical terms); 0.6B is ~2x faster. "
        "Restart the service after changing."),
    "settings.mic": _m("麦克风", "Microphone"),
    "settings.mic_default": _m("(系统默认)", "(system default)"),

    "settings.hot_reload": _m("热加载:模型常驻内存", "Hot reload: keep model in memory"),
    "settings.hot_reload_hint": _m(
        "开:按键立刻出字,常驻约 3.8GB。关:用时才加载,约 0.24GB,每次多等 1.7~2.5 秒。",
        "On: instant response, ~3.8GB resident. Off: loads on demand, ~0.24GB, "
        "adds 1.7–2.5s each time."),
    "settings.idle_unload": _m("空闲多久卸载模型(秒)", "Unload model after idle (seconds)"),
    "settings.idle_unload_hint": _m(
        "仅在热加载关闭时生效。太短会导致连续口述时反复加载。",
        "Only applies when hot reload is off. Too short causes repeated reloads "
        "during continuous dictation."),
    "settings.threads": _m("推理线程数", "Inference threads"),
    "settings.threads_hint": _m(
        "0 = 用满所有核。这台机器 16 线程,给 4 就只占约 1/4。改完要重启服务。",
        "0 = use all cores. This machine has 16 threads; 4 uses about a quarter. "
        "Restart the service after changing."),

    "settings.sound_enabled": _m("提示音", "Sound cues"),
    "settings.sound_hint": _m(
        "开始录音、出字、出错各响一声。听写时眼睛盯着输入框,声音比弹窗更合适。",
        "One cue each for start, done, and error. Your eyes stay on the text field — "
        "sound beats a popup."),
    "settings.volume": _m("音量", "Volume"),
    "settings.test_sound": _m("试听", "Test"),
    "settings.inject_method": _m("文字注入方式", "Text injection method"),
    "settings.inject_clipboard": _m("剪贴板 + 模拟粘贴(推荐)", "Clipboard + paste (recommended)"),
    "settings.inject_type": _m("逐字敲(中文不可靠)", "Type character by character (unreliable for Chinese)"),

    "settings.preproc": _m("录音预处理(削顶重建)", "Audio preprocessing (de-clipping)"),
    "settings.preproc_hint": _m(
        "实测:对 0.6B 有帮助,对 1.7B 反而略有害,所以默认关闭。换麦克风后值得重测。",
        "Measured: helps 0.6B, slightly hurts 1.7B — off by default. "
        "Worth re-testing after changing microphones."),
    "settings.open_config_file": _m("打开完整配置文件…", "Open full config file…"),
    "settings.open_config_hint": _m(
        "这里只放常用项。全部 70 多个可调项都在配置文件里,每一项都有注释说明。",
        "Only common options are shown here. All 70+ settings live in the config "
        "file, each with an explanatory comment."),

    "settings.save": _m("保存", "Save"),
    "settings.cancel": _m("取消", "Cancel"),
    "settings.close": _m("关闭", "Close"),
    "settings.unsaved": _m("%d 项改动未保存", "%d unsaved change(s)"),
    "settings.no_change": _m("没有改动", "No changes"),
    "settings.discard_title": _m("放弃改动?", "Discard changes?"),
    "settings.discard_body": _m(
        "有 %d 项改动还没保存。关掉窗口就会丢掉它们。",
        "%d change(s) have not been saved. Closing will discard them."),
    "settings.discard_yes": _m("放弃并关闭", "Discard and close"),
    "settings.discard_back": _m("回去继续改", "Go back"),
    "settings.save_failed": _m("保存失败:%s", "Save failed: %s"),
    "settings.restart_needed": _m("有改动需要重启服务才生效",
                                  "Some changes need a service restart"),
    "settings.restart_now": _m("立即重启服务", "Restart service now"),
    "settings.saved": _m("已保存", "Saved"),
}
