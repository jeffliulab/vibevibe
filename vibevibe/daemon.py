"""守护进程。

它是整个系统的中心:模型常驻在它内存里,两条触发通道
(组合键 / F19 小键盘)都汇到它这里。

  组合键  → vibevibe toggle → Unix socket ─┐
                                            ├→ 状态机 → 录音 → 转写 → 注入
  F19 键 → evdev 监听线程 ─────────────────┘

状态机只有三个状态,任何时刻只能处于其一:
  idle          闲着
  recording     正在录
  transcribing  正在转写(此时再按键不会开始新的录音,会被拒绝)

转写放在独立线程里跑,这样即使一次转写要两秒,socket 也一直是活的,
`vibevibe status` 随时能查。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any

from . import ipc
from .asr import build_backend
from .audio_preproc import preprocess
from .config import Config
from .guard import GuardTripped, check_text
from .inject import Injector
from .recorder import Recorder
from .sound import Player
from .text import postprocess

from .i18n import t

log = logging.getLogger("vibevibe.daemon")


def _rss_mb() -> float:
    """当前进程的常驻内存(MB)。"""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def _release_heap() -> int:
    """把释放掉的堆内存真正还给操作系统。

    **这一步不能省。** 光在 Python 层把模型对象删掉是不够的:
    glibc 的 malloc 默认把 free 掉的内存留在自己的 arena 里等复用,
    操作系统那边看到的常驻内存纹丝不动。实测卸载 1.7B 之后:

        只 del + gc      3306 MB   ← 看着像没卸载
        再 malloc_trim    306 MB   ← 才是真还回去了

    malloc_trim 是 glibc 特有的,musl(Alpine)之类没有,所以要兜住异常。
    返回值:1 = 确实释放了内存,0 = 没什么可释放,-1 = 这个平台不支持。
    """
    import ctypes

    try:
        libc = ctypes.CDLL("libc.so.6")
        return int(libc.malloc_trim(0))
    except Exception:
        return -1


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.socket_path = cfg.socket_path
        self.recorder = Recorder(cfg.audio)
        self.injector = Injector(cfg.inject)
        self.sound = Player(cfg.sound)
        self.backend = build_backend(cfg)

        self._state = ipc.STATE_IDLE
        self._lock = threading.RLock()
        self._server: socket.socket | None = None
        self._stop_event = threading.Event()
        self._hotkey = None
        self._idle_thread: threading.Thread | None = None
        # 最后一次用完模型的时刻,空闲卸载靠它计时
        self._last_used = time.monotonic()
        self._model_loaded = False

        # 最近一次的结果,给 status 看
        self.last_text = ""
        self.last_error = ""
        self.last_rtf = 0.0
        self.last_duration = 0.0

    # ── 状态 ────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set_state(self, state: str) -> None:
        with self._lock:
            if self._state != state:
                log.debug(t("daemon.state_change"), self._state, state)
                self._state = state

    # ── 录音 / 转写 ─────────────────────────────────────────────────

    def start_recording(self) -> dict[str, Any]:
        with self._lock:
            if self._state == ipc.STATE_RECORDING:
                return {"ok": True, "state": self._state, "note": "已经在录了"}
            if self._state == ipc.STATE_TRANSCRIBING:
                return {"ok": False, "state": self._state,
                        "error": "上一段还在转写,等它完事"}
            try:
                self.recorder.start()
            except Exception as exc:
                log.error(t("daemon.rec_start_failed"), exc)
                self.last_error = str(exc)
                return {"ok": False, "state": self._state, "error": str(exc)}
            self._set_state(ipc.STATE_RECORDING)
        self.sound.play("start")
        log.info(t("daemon.rec_start"))
        return {"ok": True, "state": ipc.STATE_RECORDING}

    def stop_recording(self) -> dict[str, Any]:
        with self._lock:
            if self._state != ipc.STATE_RECORDING:
                return {"ok": False, "state": self._state, "error": "当前没有在录音"}
            try:
                pcm = self.recorder.stop()
            except Exception as exc:
                log.error(t("daemon.rec_stop_failed"), exc)
                self._set_state(ipc.STATE_IDLE)
                return {"ok": False, "state": self._state, "error": str(exc)}

            if len(pcm) == 0:
                self._set_state(ipc.STATE_IDLE)
                self.sound.play("error")
                log.info(t("daemon.rec_too_short"))
                return {"ok": True, "state": ipc.STATE_IDLE, "note": "录音过短,已忽略"}

            self._set_state(ipc.STATE_TRANSCRIBING)

        duration = len(pcm) / self.cfg.audio.sample_rate
        log.info(t("daemon.rec_done"), duration)
        threading.Thread(
            target=self._transcribe_and_inject,
            args=(pcm,),
            name="vibevibe-transcribe",
            daemon=True,
        ).start()
        return {"ok": True, "state": ipc.STATE_TRANSCRIBING,
                "duration_sec": round(duration, 2)}

    def cancel_recording(self) -> dict[str, Any]:
        with self._lock:
            if self._state != ipc.STATE_RECORDING:
                return {"ok": False, "state": self._state, "error": "当前没有在录音"}
            self.recorder.abort()
            self._set_state(ipc.STATE_IDLE)
        log.info(t("daemon.rec_cancelled"))
        return {"ok": True, "state": ipc.STATE_IDLE}

    def toggle(self) -> dict[str, Any]:
        if self.state == ipc.STATE_RECORDING:
            return self.stop_recording()
        return self.start_recording()

    def _transcribe_and_inject(self, pcm) -> None:  # noqa: ANN001
        try:
            # 录音预处理(削顶重建 + 归一化)。默认关,开关见配置
            # [preprocess_audio] enabled。这台机器的 C920 + 满增益会削顶,
            # 开了能减少谐波失真。
            if self.cfg.preprocess_audio.enabled:
                pcm, pp = preprocess(
                    pcm, self.cfg.audio.sample_rate, self.cfg.preprocess_audio)
                if pp["clip_ratio_before"] > 0:
                    log.info(
                        "预处理: 削顶率 %.2f%% → %.2f%%,修复 %d 段",
                        pp["clip_ratio_before"] * 100,
                        pp["clip_ratio_after"] * 100, pp["declipped_runs"])
            with self._lock:
                self._model_loaded = True
            result = self.backend.transcribe(pcm)
            self._last_used = time.monotonic()
            text = postprocess(result.text, self.cfg.postprocess)
            check_text(text, self.cfg.guard)

            self.last_text = text
            self.last_rtf = result.timing.rtf
            self.last_duration = result.timing.audio_duration_s
            self.last_error = ""

            if result.guard_note:
                log.warning(t("daemon.guard_note"), result.guard_note)

            log.info(
                "转写完成 %.2fs 音频 / %.2fs 耗时 (RTF %.2f) 语言=%s: %s",
                result.timing.audio_duration_s, result.timing.total_s,
                result.timing.rtf, result.language or "?", text,
            )
            self.injector.inject(text)
            self.sound.play("done")

        except GuardTripped as exc:
            # 闸门拦下的,一个字都不注入
            self.last_error = str(exc)
            log.warning(t("daemon.guard_blocked"), exc)
            self.sound.play("error")
        except Exception as exc:
            self.last_error = str(exc)
            log.exception(t("daemon.transcribe_failed"), exc)
            self.sound.play("error")
        finally:
            self._set_state(ipc.STATE_IDLE)

    # ── 空闲卸载 ────────────────────────────────────────────────────

    def _idle_watch(self) -> None:
        """空闲够久就把模型放掉,把那几个 G 还给系统。

        只在 idle 状态下卸载——正在录音或转写时绝不动它。
        """
        while not self._stop_event.wait(2.0):
            timeout = self.cfg.daemon.idle_unload_sec
            with self._lock:
                # 热加载开着就什么都不做
                if self.cfg.daemon.hot_reload or timeout <= 0:
                    continue
                if self._state != ipc.STATE_IDLE or not self._model_loaded:
                    continue
                idle_for = time.monotonic() - self._last_used
                if idle_for < timeout:
                    continue
                self._model_loaded = False

            try:
                import gc

                before = _rss_mb()
                self.backend.unload()
                gc.collect()
                _release_heap()
                after = _rss_mb()
                log.info(
                    "空闲 %.0f 秒,已卸载模型: 内存 %.0f MB → %.0f MB"
                    "(下次按键自动重新加载,多等一两秒)",
                    idle_for, before, after)
            except Exception as exc:
                log.warning(t("daemon.unload_failed"), exc)

    # ── 命令分发 ────────────────────────────────────────────────────

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        cmd = payload.get("cmd", "")
        if cmd == ipc.CMD_START:
            return self.start_recording()
        if cmd == ipc.CMD_STOP:
            return self.stop_recording()
        if cmd == ipc.CMD_TOGGLE:
            return self.toggle()
        if cmd == ipc.CMD_CANCEL:
            return self.cancel_recording()
        if cmd == ipc.CMD_PING:
            return {"ok": True, "state": self.state}
        if cmd == ipc.CMD_STATUS:
            return {
                "ok": True,
                "state": self.state,
                "backend": self.backend.name,
                "pid": os.getpid(),
                "config": str(self.cfg.source_path or "(全默认值)"),
                "hotkey": self._hotkey.describe() if self._hotkey else "未启用",
                "recording_sec": round(self.recorder.duration_sec, 2),
                "model_in_memory": self._model_loaded,
                "hot_reload": self.cfg.daemon.hot_reload,
                "memory_mb": round(_rss_mb()),
                "last_text": self.last_text,
                "last_rtf": round(self.last_rtf, 3) if self.last_rtf else None,
                "last_duration_sec": round(self.last_duration, 2) or None,
                "last_error": self.last_error,
            }
        if cmd == ipc.CMD_RELOAD:
            return self.reload_config()
        if cmd == ipc.CMD_SET_HOT:
            return self.set_hot(bool(payload.get("hot", True)))
        if cmd == ipc.CMD_SHUTDOWN:
            log.info(t("daemon.shutdown_cmd"))
            self._stop_event.set()
            return {"ok": True, "state": "shutting_down"}
        return {"ok": False, "error": f"不认识的命令 {cmd!r}"}

    # 改了要重启服务才生效的项。设置界面据此提示用户。
    RESTART_KEYS = ("asr.backend", "asr.qwen_onnx_1p7b.intra_op_num_threads",
                    "asr.qwen_onnx.intra_op_num_threads", "audio.sample_rate",
                    "hotkey.enabled", "hotkey.device", "daemon.socket_path")

    def reload_config(self) -> dict[str, Any]:
        """重新读配置文件,把**能热改的**项应用上。

        哪些能热改、哪些必须重启,取决于这个值在哪儿被用到:
          能热改 —— 语言、提示音、兜底闸阈值、文本后处理、热加载开关
                    (每次用的时候现读)
          要重启 —— 模型后端、推理线程数、socket 路径
                    (在启动时就固化进对象里了)

        返回里会告诉调用方"有没有需要重启才生效的改动",
        设置界面拿它来决定要不要弹提示。
        """
        from .config import load_config
        from .i18n import set_language

        old_backend = self.cfg.asr.backend
        old_threads = self.cfg.asr.qwen_onnx_1p7b.intra_op_num_threads
        try:
            new = load_config(self.cfg.source_path)
        except Exception as exc:
            log.error("重新读配置失败: %s", exc)
            return {"ok": False, "error": str(exc)}

        with self._lock:
            # 热改的部分整段换掉
            self.cfg.ui = new.ui
            self.cfg.sound = new.sound
            self.cfg.guard = new.guard
            self.cfg.postprocess = new.postprocess
            self.cfg.inject = new.inject
            self.cfg.preprocess_audio = new.preprocess_audio
            self.cfg.audio.input_device = new.audio.input_device
            self.cfg.daemon.hot_reload = new.daemon.hot_reload
            self.cfg.daemon.idle_unload_sec = new.daemon.idle_unload_sec

        set_language(self.cfg.ui.language)
        self.sound.cfg = self.cfg.sound
        self.injector.cfg = self.cfg.inject
        log.info(t("daemon.lang_changed"), self.cfg.ui.language)

        needs_restart = (
            new.asr.backend != old_backend
            or new.asr.qwen_onnx_1p7b.intra_op_num_threads != old_threads
        )
        # 热加载开关顺带同步一下(可能是在文件里手改的)
        self.set_hot(self.cfg.daemon.hot_reload)

        return {
            "ok": True,
            "state": self.state,
            "language": self.cfg.ui.language,
            "needs_restart": needs_restart,
        }

    def set_hot(self, hot: bool) -> dict[str, Any]:
        """运行时切换热加载。托盘那个开关调的就是这个。

        开 → 立刻把模型载进内存,之后不再卸载
        关 → 立刻卸载(如果当前空闲),之后用完 idle_unload_sec 秒再卸

        改动同时写回用户配置,重启后依然生效。
        """
        from .config import USER_CONFIG_PATH, patch_config_value

        with self._lock:
            self.cfg.daemon.hot_reload = hot
            busy = self._state != ipc.STATE_IDLE

        persisted = patch_config_value(
            USER_CONFIG_PATH, "daemon", "hot_reload", hot)

        if hot:
            # 后台加载,不阻塞 socket —— 加载要一两秒,托盘不能卡住
            def warm() -> None:
                try:
                    self.backend.load()
                    with self._lock:
                        self._model_loaded = True
                    self._last_used = time.monotonic()
                    log.info(t("daemon.hot_warmed"), _rss_mb())
                except Exception as exc:
                    log.error(t("daemon.warm_failed"), exc)

            threading.Thread(target=warm, name="vibevibe-warm", daemon=True).start()
            log.info(t("daemon.hot_on"))
        elif not busy:
            import gc

            with self._lock:
                self._model_loaded = False
            before = _rss_mb()
            try:
                self.backend.unload()
                gc.collect()
                _release_heap()
                log.info(t("daemon.hot_off_unloaded"), before, _rss_mb())
            except Exception as exc:
                log.warning(t("daemon.unload_failed"), exc)
        else:
            log.info(t("daemon.hot_off_busy"))

        return {
            "ok": True,
            "state": self.state,
            "hot_reload": hot,
            "persisted": persisted,
            "memory_mb": round(_rss_mb()),
        }

    # ── socket 服务 ─────────────────────────────────────────────────

    def _serve_client(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(ipc.DEFAULT_TIMEOUT_SEC)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(ipc.RECV_BUFSIZE)
                if not chunk:
                    return
                buf += chunk
            payload = json.loads(buf.split(b"\n", 1)[0].decode(ipc.ENCODING))
            response = self.handle(payload)
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            conn.sendall(
                (json.dumps(response, ensure_ascii=False) + "\n").encode(ipc.ENCODING))
        except OSError:
            pass
        finally:
            conn.close()

    def _bind(self) -> socket.socket:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            # 先探一下是不是还有活的守护进程占着
            try:
                ipc.command(self.socket_path, ipc.CMD_PING, timeout=1.0)
            except ipc.IpcError:
                log.info(t("daemon.stale_socket"))
                self.socket_path.unlink()
            else:
                raise RuntimeError(
                    f"已经有一个守护进程在跑了({self.socket_path})。"
                    "要重启的话先: vibevibe shutdown"
                )
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        # 只有当前用户能连
        os.chmod(self.socket_path, 0o600)
        server.listen(8)
        server.settimeout(0.5)  # 让主循环能定期检查停止标志
        return server

    # ── 生命周期 ────────────────────────────────────────────────────

    def run(self) -> int:
        from .i18n import set_language

        set_language(self.cfg.ui.language)
        log.info(t("daemon.started"), os.getpid())
        log.info(t("daemon.config"), self.cfg.source_path or "(defaults)")

        missing = self.injector.check()
        if missing:
            # 不直接退出——录音和转写还是能验证的,只是注入不了。
            # 但必须说清楚,不能等到用户按了键才发现没反应。
            log.warning(
                "缺少注入工具 %s,识别出来的文字将无法打进光标处。"
                "装一下: sudo apt install %s",
                ", ".join(missing), " ".join(missing),
            )

        if self.cfg.daemon.hot_reload:
            log.info(t("daemon.loading_model"), self.backend.name)
            self._set_state(ipc.STATE_LOADING)
            try:
                self.backend.load()
                self._model_loaded = True
                self._last_used = time.monotonic()
                log.info(t("daemon.model_ready"))
            except Exception as exc:
                log.error(t("daemon.model_load_failed"), exc)
                self.last_error = str(exc)
            finally:
                self._set_state(ipc.STATE_IDLE)

        self._server = self._bind()
        log.info(t("daemon.listening"), self.socket_path)

        # 空闲线程一直在,内部按 hot_reload 决定要不要动手 ——
        # 因为托盘可以随时切换这个开关,不能启动时决定了就不管了
        self._idle_thread = threading.Thread(
            target=self._idle_watch, name="vibevibe-idle", daemon=True)
        self._idle_thread.start()
        log.info("热加载: %s", "开(模型常驻)" if self.cfg.daemon.hot_reload
                 else f"关(空闲 {self.cfg.daemon.idle_unload_sec:.0f} 秒后卸载)")

        if self.cfg.hotkey.enabled:
            from .hotkey_evdev import HotkeyListener

            self._hotkey = HotkeyListener(self.cfg.hotkey, self)
            self._hotkey.start()

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self._stop_event.set())

        try:
            while not self._stop_event.is_set():
                try:
                    conn, _ = self._server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._serve_client, args=(conn,),
                    name="vibevibe-client", daemon=True,
                ).start()
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        log.info(t("daemon.shutting_down"))
        if self._hotkey:
            self._hotkey.stop()
        if self.state == ipc.STATE_RECORDING:
            self.recorder.abort()
        if self._server:
            self._server.close()
            self._server = None
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass


def setup_logging(cfg: Config, to_console: bool = True) -> None:
    log_path = cfg.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [logging.FileHandler(log_path, encoding="utf-8")]
    if to_console:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=getattr(logging, cfg.daemon.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
