"""配置读取。

设计原则:代码里不写死任何可调的值。所有阈值、路径、设备、线程数
都在这里给出默认值,并且可以被 ~/.config/vibevibe/config.toml 覆盖。

查找顺序:
  1. 环境变量 VIBEVIBE_CONFIG 指定的文件
  2. ~/.config/vibevibe/config.toml
  3. 项目目录下的 config/config.example.toml(仅当前两者都不存在)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

# ── 路径 ────────────────────────────────────────────────────────────────
#
# 这个包有两种活法,路径规则不一样,必须分清楚:
#
#   源码目录里跑(开发)   —— 相对路径基于项目根目录,模型放 <项目>/models/
#   pip 装完了跑(用户)   —— 项目根目录变成了 site-packages,里面当然没有
#                          models/,所以相对路径要基于 ~/.local/share/vibevibe/
#
# 判据就是项目根目录里有没有 pyproject.toml。这条规则不写清楚的话,
# 装到 site-packages 之后会去那儿找 4GB 权重,然后一脸茫然地报"找不到"。

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DATA_FILES_DIR = PACKAGE_DIR / "data"   # 打进 wheel 的模板文件


def _xdg(env_name: str, default: Path) -> Path:
    return Path(os.environ.get(env_name) or default) / "vibevibe"


CONFIG_HOME = _xdg("XDG_CONFIG_HOME", Path.home() / ".config")
DATA_HOME = _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share")
STATE_HOME = _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state")


def is_source_checkout() -> bool:
    """是不是在源码目录里跑(而不是 pip 装完的)。"""
    return (PROJECT_ROOT / "pyproject.toml").exists()


def data_root() -> Path:
    """相对路径(比如 models/xxx)的解析基准。"""
    return PROJECT_ROOT if is_source_checkout() else DATA_HOME


ENV_CONFIG_PATH = "VIBEVIBE_CONFIG"
USER_CONFIG_PATH = CONFIG_HOME / "config.toml"
EXAMPLE_CONFIG_PATH = DATA_FILES_DIR / "config.example.toml"
UDEV_RULE_TEMPLATE = DATA_FILES_DIR / "70-vibevibe.rules.example"
SERVICE_TEMPLATE = DATA_FILES_DIR / "vibevibe.service"
TRAY_DESKTOP_TEMPLATE = DATA_FILES_DIR / "vibevibe-tray.desktop"
ICONS_DIR = DATA_FILES_DIR / "icons"


def _expand(path_str: str) -> Path:
    """展开 ~ 和环境变量;相对路径按 data_root() 解析(见上面的说明)。"""
    p = Path(os.path.expandvars(os.path.expanduser(path_str)))
    return p if p.is_absolute() else (data_root() / p)


# ── 各段配置 ────────────────────────────────────────────────────────────


@dataclass
class QwenOnnxConfig:
    """Qwen3-ASR ONNX 后端(主选,纯 CPU)。"""

    # 权重目录,内含 onnx_models/ 与 tokenizer.json
    model_dir: str = "models/Qwen3-ASR-0.6B-ONNX-CPU"
    # `vibevibe setup` 从哪儿下权重。想换镜像就改这里。
    hf_repo: str = "Daumee/Qwen3-ASR-0.6B-ONNX-CPU"
    hf_files: list[str] = field(default_factory=list)  # 空 = 整个仓库
    # 解码器量化档:int8(默认,省内存快) 或 none(FP32,更准但更慢更占内存)
    quantize: str = "int8"
    # 单个算子内部的并行线程数。0 = 交给 onnxruntime 用满所有核。
    # 设成较小的值可以避免抢占整机 CPU,代价是单次转写变慢。
    intra_op_num_threads: int = 4
    # 算子之间的并行度。CPU 上这条流水线基本是串行的,给 1 即可。
    inter_op_num_threads: int = 1
    # 词嵌入矩阵(622MB)用 mmap 打开而不是整个读进内存。
    # 每次只按 token id 取几行,随机访问量极小,所以 mmap 几乎不影响速度,
    # 但能省下 622MB 常驻内存。内存富裕且想要极致速度时可以关掉。
    mmap_embeddings: bool = True


@dataclass
class WhisperCt2Config:
    """faster-whisper(CTranslate2)后端,作为对照基线。

    注:CTranslate2 在 Blackwell(sm_120)GPU 上用 int8 会崩,
    但这里走的是 CPU 路径,不受该问题影响。
    """

    model_dir: str = "models/faster-whisper-large-v3-turbo-int8-ct2"
    hf_repo: str = "Zoont/faster-whisper-large-v3-turbo-int8-ct2"
    hf_files: list[str] = field(default_factory=list)
    compute_type: str = "int8"
    cpu_threads: int = 4
    num_workers: int = 1
    beam_size: int = 1
    vad_filter: bool = True


@dataclass
class QwenHfConfig:
    """Qwen3-ASR transformers CPU 后端。

    仅用于 1.7B 的准确率对照(它需要 torch,属于可选依赖,
    默认不装;只有在 bench 里显式选用时才需要)。
    """

    model_id: str = "Qwen/Qwen3-ASR-1.7B"
    model_dir: str = ""  # 留空则用 model_id 从 HuggingFace 加载
    device: str = "cpu"
    dtype: str = "float32"


@dataclass
class AsrConfig:
    # 用哪个后端:qwen_onnx(0.6B) | qwen_onnx_1p7b | whisper_ct2 | qwen_hf
    #
    # 默认 1.7B —— 这是实测选出来的,不是默认最大就好:
    #   技术词命中 77% vs 0.6B 的 70%,CER 0.082 vs 0.132,英文 WER 0.213 vs 0.309
    #   代价只是 RTF 从 0.08 涨到 0.15(八秒的话 1.2 秒出字,依然远快于实时)
    # 想更快可以换回 "qwen_onnx",准确率会掉一档。
    backend: str = "qwen_onnx_1p7b"
    # 语言。**中英混说必须留空**,留空 = 自动检测。
    # 填了具体语言会强制模型只输出那一种语言,混说就废了。
    language: str = ""
    # 单段最多生成多少 token(也是无限重复 bug 的第一道闸)
    max_new_tokens: int = 512
    # 超过 45 秒的音频按静音点切分,这是每段的目标长度
    chunk_sec: int = 30

    qwen_onnx: QwenOnnxConfig = field(default_factory=QwenOnnxConfig)
    # 1.7B 走同一套代码,只是权重目录和量化档不同。
    # 它的 ONNX 导出结构跟 0.6B 不一样(单文件 encoder、预填充吃 input_ids、
    # 隐藏维 2048、词嵌入是 float16),这些都在加载时自省出来,不用改配置。
    qwen_onnx_1p7b: QwenOnnxConfig = field(default_factory=lambda: QwenOnnxConfig(
        model_dir="models/Qwen3-ASR-1.7B-ONNX",
        quantize="int4",   # 这个导出只提供 fp32 和 int4,没有 int8
        hf_repo="andrewleech/qwen3-asr-1.7b-onnx",
        # 只下 int4 那一套(约 4.1GB)。仓库里还有 fp32 版和打包的 tar.gz,
        # 全下要 21GB,没必要。
        hf_files=[
            "encoder.int4.onnx", "decoder_init.int4.onnx", "decoder_step.int4.onnx",
            "decoder_weights.int4.data", "embed_tokens.bin", "tokenizer.json",
            "config.json", "preprocessor_config.json",
        ],
    ))
    whisper_ct2: WhisperCt2Config = field(default_factory=WhisperCt2Config)
    qwen_hf: QwenHfConfig = field(default_factory=QwenHfConfig)


@dataclass
class AudioConfig:
    # 输入设备。留空 = 系统默认。可填 sounddevice 的设备名(子串匹配)或索引。
    # 这台机器上可选:"C920"(摄像头麦,远场) / "ALC1220"(主板模拟口)
    input_device: str = ""
    sample_rate: int = 16000  # Qwen3-ASR 和 Whisper 都要求 16kHz
    channels: int = 1
    # 录音硬上限,防止忘记停止把内存吃光
    max_record_sec: float = 120.0
    # 录音块大小(秒),影响停止响应速度
    block_sec: float = 0.05
    # 静音自动停止(默认关,hold 模式下用不着)
    silence_auto_stop: bool = False
    silence_threshold_db: float = -40.0
    silence_duration_sec: float = 2.0
    # 低于这个时长的录音直接丢弃,当作误触
    min_record_sec: float = 0.3


@dataclass
class PreprocessAudioConfig:
    """录音预处理。针对"输入增益过高导致削顶"这个真实工况。

    实测这台机器上 C920 + ALSA 增益拉满(+44dB)时,0.2%~2.2% 的样本
    被削平到 ±1.0。削顶的危害是**谐波失真**——波形顶被削平等于凭空
    多了一堆高频谐波,而 mel 频谱对高频敏感。

    ⚠️ 默认是**关**的,而且应该保持关着,直到 A/B 跑分证明它有用
    (bench/compare.py --preproc both)。加了预处理不等于就更好,
    没测过就打开属于想当然。
    """

    enabled: bool = False

    # 削顶重建:用两侧波形插值补回被削平的波峰
    declip: bool = True
    clip_threshold: float = 0.99
    # 下面这两个值是**实测扫出来的**,不是拍脑袋定的:
    # 用录音里未削顶的干净片段当基准真值,人为削顶再重建,扫参数取最优。
    # context=8 / overshoot=2.0 时平均改善 +2.77dB,最差的一条也有 +0.81dB。
    # 锚点取太多(context 20~32)反而变差——拟合窗口一宽就抓不住局部峰形。
    declip_context: int = 8
    declip_max_overshoot: float = 2.0  # 重建峰值最多是阈值的几倍
    declip_max_run: int = 64   # 超过这么长的削顶段不修(硬插值只会造假波形)

    # 归一化。削顶重建后峰值会超过 1.0,必须缩回来
    normalize: bool = True
    target_peak: float = 0.85
    target_rms: float = 0.0    # >0 则优先按 RMS 归一化

    # 高通滤波。实测这批录音 80Hz 以下只占 0.7%~1.8% 能量,
    # 所以默认 0(关闭)。换麦克风或换环境后可能用得上。
    highpass_hz: float = 0.0


@dataclass
class GuardConfig:
    """兜底。挡住 Qwen3-ASR 已知的无限重复 token bug(官方 issue #129 未修)。

    这几个值必须可调:不同模型、不同语速下合适的阈值不一样。
    """

    # 连续重复检测:最近 repeat_ngram_size 个 token 组成的片段
    # 如果连续出现 repeat_max_times 次,判定为陷入重复,立刻停止解码
    repeat_ngram_size: int = 8
    repeat_max_times: int = 3
    # 单个 token 连续重复多少次判定为卡死
    single_token_repeat_max: int = 24
    # 整段转写超时(秒),超了就放弃并报错,绝不把半成品粘出去
    transcribe_timeout_sec: float = 60.0
    # 转写结果短于这个长度就当作没识别到,不注入
    min_text_len: int = 1
    # 结果字符数上限。正常口述一句话不会超过这个数,超了说明跑飞了。
    max_text_len: int = 4000


@dataclass
class SoundConfig:
    """提示音。听写时眼睛盯着输入框,声音比桌面通知更合适。

    频率给多个 = 和弦,听起来比单音饱满、不刺耳。
    """

    enabled: bool = True
    volume: float = 0.18          # 0~1。默认小声,提示而不是打扰
    output_device: str = ""       # 留空 = 系统默认输出

    # 按下去了,正在录 —— 高一点,清脆
    start_freqs: list[float] = field(default_factory=lambda: [880.0, 1320.0])
    start_sec: float = 0.075

    # 出字了,已经粘好 —— 低一点,有收尾感
    done_freqs: list[float] = field(default_factory=lambda: [660.0, 880.0])
    done_sec: float = 0.075

    # 出问题了,一个字都没粘 —— 明显不同的低音,一听就知道不对劲
    error_freqs: list[float] = field(default_factory=lambda: [220.0, 233.0])
    error_sec: float = 0.28


@dataclass
class InjectConfig:
    # clipboard_paste:填剪贴板再模拟粘贴(推荐,中英混排最可靠)
    # type:用 xdotool 逐字敲(中文容易出问题,仅作备选)
    method: str = "clipboard_paste"
    # 绝大多数程序的粘贴键。终端不是,见下面那张覆盖表。
    paste_key: str = "ctrl+v"

    # 按焦点窗口的 WM_CLASS 覆盖粘贴键。
    #
    # 为什么需要:终端里 ctrl+v **不是粘贴** —— 它落到 shell 的 readline 手里
    # 是 quoted-insert(下一个字符按字面插入),不但粘不上,还会吃掉你接下来敲的
    # 第一个键。所以对着终端听写必须换成那个终端自己的粘贴键。
    #
    # 表里的值是各终端**自己文档写明的默认粘贴键**,不是我们逐个实测的
    # (本项目只在 GNOME Terminal 上实测过)。你的终端改过键位就自己改这里。
    # 查窗口类名: xprop WM_CLASS 然后点那个窗口。
    #
    # ⚠️ 在配置文件里写这张表 = **整张替换**下面这些默认值,不是往上追加。
    paste_key_by_window_class: dict[str, str] = field(default_factory=lambda: {
        "gnome-terminal-server": "ctrl+shift+v",
        "org.gnome.Ptyxis": "ctrl+shift+v",
        "konsole": "ctrl+shift+v",
        "xfce4-terminal": "ctrl+shift+v",
        "tilix": "ctrl+shift+v",
        "terminator": "ctrl+shift+v",
        "alacritty": "ctrl+shift+v",
        "kitty": "ctrl+shift+v",
        "org.wezfurlong.wezterm": "ctrl+shift+v",
        "foot": "ctrl+shift+v",
        # xterm 没有 ctrl+shift+v,它的粘贴是 shift+insert
        "xterm": "shift+insert",
    })
    # 查焦点窗口类名用的工具(x11-utils 里的 xprop)。
    # 没装 / 查不出来 → 安静退回上面那个 paste_key,听写照常工作。
    window_tool: str = "xprop"

    # 粘贴后是否恢复你原来的剪贴板内容
    restore_clipboard: bool = True
    restore_delay_sec: float = 0.5
    clipboard_tool: str = "xclip"
    type_tool: str = "xdotool"
    # 逐字敲的间隔(毫秒),仅 method = "type" 时有效
    type_delay_ms: int = 12


@dataclass
class HotkeyConfig:
    """evdev 直读通道(F19 小键盘)。

    注意:F19 在 X11 默认布局下没有 keysym(keycode 197 为空),
    GNOME 快捷键界面绑不了它,所以只能走这条直读设备的路。
    """

    enabled: bool = False
    # 必须用 /dev/input/by-id/ 下的稳定路径,不要写 /dev/input/eventN
    # (event 号在重新插拔后会变)
    device: str = ""
    # evdev 键名。KEY_F19 对应 evdev code 189。
    #
    # 默认固定 F19 是为了**对应两键小键盘的左键**:小键盘的固件里也烧成
    # F19,这样换任何一台电脑插上去都能直接用,不依赖电脑上的软件改键。
    #
    # 为什么是 F19 而不是 F13:实测这台机器的 X11 布局里,F13~F18 被映射成
    # XF86Tools / XF86Launch5~9,F20~F23 是音量和触摸板键 —— 十二个里只有
    # F19 和 F24 没有 keysym。没有 keysym 意味着漏给应用也不会有任何反应,
    # 这在「不能独占设备」的前提下是决定性的。
    key: str = "KEY_F19"
    # hold:按住录、松开转写(专用键推荐)
    # toggle:按一下开始、再按一下停
    mode: str = "hold"
    # 是否独占设备(EVIOCGRAB)。
    #
    # ⚠️ **独占是整设备级的,不能只独占某一个键。**
    # 所以「一个键触发听写 + 一个键透传回车」这种两键布局**必须关掉它**——
    # 独占了 event 节点,右键的 Enter 也一起被吞,永远送不到你正在打字的窗口。
    #
    # 实测下来这也不构成问题:F19 在 X11 默认布局里没有 keysym
    # (这台机器上 keycode 197 是空的),漏给应用不会有任何反应。
    # 当初为「不跟别的快捷键冲突」选 F19,顺带解决了「不能独占」。
    #
    # 什么情况下才该开:整个设备上的每一个键都由 vibevibe 消费,
    # 没有任何键需要透传给别的程序。
    grab: bool = False
    # 设备不存在或被拔掉后的重连间隔
    reconnect_interval_sec: float = 2.0
    # 安全闸:开启 grab 时,设备名必须包含这个字符串,否则拒绝独占。
    #
    # 为什么不用「按键数量」当判据(原来就是这么写的,实测证明是错的):
    # QMK 系固件不管物理上几个键,都会声明整个键位范围。实测这块
    # 两键小键盘声明了 279 个键,比主键盘(143)还多 —— 数量根本
    # 区分不出主键盘,那个闸给的是虚假的安全感。
    #
    # 设备名匹配挡的是真实风险:by-id 路径在换硬件后指向了别的设备。
    # 留空 = 不做名字校验(不推荐)。
    grab_expect_name: str = ""


@dataclass
class UiConfig:
    """界面和日志的语言。托盘设置里那个下拉框改的就是它。

    只影响**用户看得见的东西**(日志、菜单、设置界面、命令行输出);
    代码注释一律保持中文,那是给维护者看的。
    """

    language: str = "zh"   # zh | en


@dataclass
class ShortcutConfig:
    """桌面快捷键通道 —— 不需要专用小键盘也能用。

    跟 [hotkey] 那条 evdev 直读通道的分工:

        [hotkey]    固定 KEY_F19,对应两键小键盘的左键。
                    需要 udev 规则,但只针对那一个小键盘。
        [shortcut]  桌面快捷键,**零权限**,任何人 pip 装完就能用。
                    GNOME 会把它抢下来,所以不会漏给当前应用。

    两条通道同时生效,互不干扰 —— 有小键盘就用小键盘,没有就用快捷键。

    默认值 <Super><Shift>v 是实测选的,不是随手挑的:
      · 应用程序基本不碰 Super 键(它是桌面环境的保留键),
        所以 Super 系组合几乎不会跟应用撞车
      · Ctrl+Shift+V 看着顺手,但在浏览器/VSCode/终端里全都是
        「粘贴为纯文本」—— 这类冲突 gsettings 查不出来,用起来天天撞
      · Super+V 本身被 GNOME 占了(toggle-message-tray),所以要加 Shift
    """

    enabled: bool = True
    # GNOME 加速器写法。改这个要重新绑定(设置界面里会自动做)。
    accel: str = "<Super><Shift>v"


@dataclass
class DaemonConfig:
    socket_path: str = "~/.local/state/vibevibe/vibevibe.sock"
    log_path: str = "~/.local/state/vibevibe/vibevibe.log"
    log_level: str = "INFO"
    # 热加载:模型常驻内存。托盘图标里那个开关就是它。
    #
    #   开(默认) —— 启动就加载、永不卸载。按键立刻出字,代价是常驻约 3.8GB
    #   关       —— 用的时候才加载,用完 idle_unload_sec 秒后放掉。
    #                内存降到约 0.24GB,代价是每次冷启动多等 1.7~2.5 秒
    #
    # 为什么这 3.8GB 是真占着的:实测 ONNX Runtime 把权重**复制进匿名内存**
    # (不是 mmap),内核回收不了。而且光在 Python 层删掉模型对象也没用——
    # glibc 会把 free 掉的内存扣在自己的 arena 里,得额外调 malloc_trim(0)
    # 才真正还给系统(实测 3306 MB → 306 MB)。
    hot_reload: bool = True

    # 热加载关闭时,用完多久把模型放掉。太短会导致连续口述时反复加载,
    # 太长又失去省内存的意义。
    idle_unload_sec: float = 20.0


@dataclass
class PostprocessConfig:
    """转写后的文本修正。

    replacements 是技术词纠正表,形如:
        replacements = [{ from = "康米特", to = "commit" }]
    放配置文件里,不写死在代码中。
    """

    strip_surrounding_space: bool = True
    # 去掉中文句末的句号(口述时通常不想要)
    strip_trailing_period: bool = False
    replacements: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Config:
    ui: UiConfig = field(default_factory=UiConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    preprocess_audio: PreprocessAudioConfig = field(
        default_factory=PreprocessAudioConfig)
    guard: GuardConfig = field(default_factory=GuardConfig)
    sound: SoundConfig = field(default_factory=SoundConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    shortcut: ShortcutConfig = field(default_factory=ShortcutConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    postprocess: PostprocessConfig = field(default_factory=PostprocessConfig)

    # 实际加载的配置文件路径(没有就是 None,表示全用默认值)
    source_path: Path | None = None

    # ── 路径便捷访问 ────────────────────────────────────────────────
    @property
    def socket_path(self) -> Path:
        return _expand(self.daemon.socket_path)

    @property
    def log_path(self) -> Path:
        return _expand(self.daemon.log_path)

    def backend_model_dir(self) -> Path:
        """当前后端的权重目录。"""
        backend = self.asr.backend
        if backend == "qwen_onnx":
            return _expand(self.asr.qwen_onnx.model_dir)
        if backend == "qwen_onnx_1p7b":
            return _expand(self.asr.qwen_onnx_1p7b.model_dir)
        if backend == "whisper_ct2":
            return _expand(self.asr.whisper_ct2.model_dir)
        if backend == "qwen_hf":
            d = self.asr.qwen_hf.model_dir
            return _expand(d) if d else Path(self.asr.qwen_hf.model_id)
        raise ValueError(f"未知的 ASR 后端: {backend!r}")


# ── 加载 ────────────────────────────────────────────────────────────────


def _apply(obj: Any, data: dict[str, Any], path: str = "") -> None:
    """把 TOML 里的值递归写进 dataclass,遇到不认识的键直接报错。

    宁可启动失败也不要静默忽略拼错的配置项——静默忽略会让人
    以为改了配置生效了,实际没有,这种坑最难查。
    """
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        full_key = f"{path}{key}"
        if key not in known:
            raise ValueError(f"配置项不认识: [{full_key}]")
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value, path=f"{full_key}.")
        else:
            setattr(obj, key, value)


def find_config_path() -> Path | None:
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        p = Path(os.path.expanduser(env))
        if not p.exists():
            raise FileNotFoundError(f"{ENV_CONFIG_PATH} 指向的配置文件不存在: {p}")
        return p
    if USER_CONFIG_PATH.exists():
        return USER_CONFIG_PATH
    if EXAMPLE_CONFIG_PATH.exists():
        return EXAMPLE_CONFIG_PATH
    return None


def load_config(path: Path | None = None) -> Config:
    """读配置。找不到文件就全用默认值,不报错。"""
    cfg = Config()
    resolved = path or find_config_path()
    if resolved is None:
        return cfg
    with open(resolved, "rb") as f:
        data = tomllib.load(f)
    _apply(cfg, data)
    cfg.source_path = resolved

    # 配置一读进来就把语言定下来,后面所有 t() 才拿得到正确的文案
    from .i18n import set_language

    set_language(cfg.ui.language)
    return cfg


def patch_config_value(path: Path, section: str, key: str, value: Any) -> bool:
    """在 TOML 文件里就地改一个值,**保留所有注释和排版**。

    为什么不用 toml 库重写整个文件:那样会把注释全丢掉。这个配置文件里
    的注释解释了每个取舍是怎么测出来的,比值本身还值钱。

    只做最朴素的行匹配:找到 [section] 段落,在里面找 `key =` 开头的行,
    替换等号右边。找不到就返回 False,不硬塞。
    """
    if not path.exists():
        return False

    if isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, (int, float)):
        literal = str(value)
    else:
        literal = f'"{value}"'

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_section = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped.split("]")[0].lstrip("[").strip() == section
            continue
        if not in_section or stripped.startswith("#"):
            continue
        head = stripped.split("=", 1)[0].strip()
        if head == key:
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}{key} = {literal}\n"
            path.write_text("".join(lines), encoding="utf-8")
            return True
    return False
