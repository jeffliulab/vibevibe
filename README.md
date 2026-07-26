# vibevibe

**本地中英混说语音听写 —— 说一下,按一下。**

按一个键说话,松开,文字就出现在光标处。全程在本机跑,不联网,不上传音频。
名字来自两个动作:说一下(键 1),按一下回车(键 2)。

## 这东西为什么存在

用 Claude Code 和各种 AI agent 时打字太慢,想按键说话。但有一条硬约束:
**说话是中文英文夹着来的**——中文句子里嵌 commit、pipeline、CAN 总线这类词。
这一条把现有方案几乎全部淘汰了:

- **Claude Code 自带的 `/voice`**:音频要传到服务器,而且支持的听写语言里**没有中文**
- **Linux 上十几个开源听写工具**(Handy、hyprwhspr、VoxType、OpenWhispr、whisrs…):
  后端清一色是 whisper.cpp / Parakeet / Vosk,**没有一个支持 Qwen3-ASR**
- **商业产品**(Wispr Flow 等):几乎全是 Mac/Windows,Linux 是被放弃的市场

而 Qwen3-ASR(2026-01 开源,Apache 2.0)是目前唯一**专门为中英混说训练**的开源模型
——训练时往中文语料里注入了 4 万多个英文技术词。

所以「Linux + 本地 + 中英混说」这个交叉点是空的。vibevibe 就站在这儿。

## 设计上的几个取舍

**只用 CPU,不用 GPU。** 三个理由:

1. CTranslate2 在 Blackwell(sm_120)显卡上用 int8 会崩,PyTorch 稳定版
   对 sm_120 的支持至今没合入。走 ONNX Runtime CPU 直接绕开这摊浑水。
2. 不占显存 → 跑训练任务的时候听写照样能用。
3. 发给别人用时不要求对方有 N 卡。

**说完再转,不做边说边冒字。** Qwen3-ASR 天生是整段转的。流式方案会让字
跳来跳去、更吃 CPU,而且切碎了对中英混说更不利。不划算。

**注入走剪贴板 + 模拟粘贴,不逐字敲。** 中英混排逐字敲不可靠,粘贴一次到位。

## 两条触发通道

```
通道 A  组合键(如 Ctrl+Alt+Space)
        └→ GNOME 快捷键 → `vibevibe toggle` ─┐
                                              ├→ Unix socket → 守护进程
通道 B  F19 小键盘                            │      (模型常驻其中)
        └→ 直读输入设备(evdev)──────────────┘

键 2 = 回车:硬件直接发 Enter 键码,软件完全不参与
```

**为什么 F19 必须走 evdev 直读**:实测这台机器上 F19 在 X11 里
`keycode 197` 没有任何 keysym,GNOME 快捷键界面根本绑不了它。
反过来说,正因为它是空的,才不会跟任何东西冲突。

evdev 这条路还额外带来两个好处:能精确知道按下和松开(于是 hold
"按住说话"模式才成立),以及可以对**专用小键盘**整个独占,按键
一个字符都不会漏进当前窗口。

> 独占是**设备级**的,不能只独占某一个键。所以只能对专用小键盘开,
> 对主键盘开会吃掉你所有输入。代码里有硬闸:按键数超过阈值直接拒绝独占。

## 目录

```
vibevibe/          主程序
  config.py          配置(所有可调项集中于此,代码里不写死)
  asr/               三个可插拔的识别后端
    qwen_onnx.py       Qwen3-ASR ONNX —— 主力
    whisper_ct2.py     Whisper large-v3-turbo —— 对照基线
    qwen_hf.py         Qwen3-ASR PyTorch —— 需额外装 torch,默认不用
  recorder.py        录音
  inject.py          把文字送进光标处
  guard.py           兜底闸(挡 Qwen3-ASR 的无限重复 bug)
  daemon.py          守护进程 + 状态机
  hotkey_evdev.py    F19 通道
  sound.py           提示音(现算的正弦波,不依赖音频文件)
  audio_preproc.py   削顶重建(默认关,见下)
  setup_wizard.py    `vibevibe setup` 安装向导
  cli.py             命令行
  data/              打进 wheel 的模板:配置、udev 规则、systemd 单元
bench/             选型验证
  corpus.toml        要念的测试语料(10 条中英夹杂 + 2 条基线)
  record.py          录语料(命令行版)
  record_gui.py      录语料(图形界面,带实时波形)
  compare.py         对照跑分
  metrics.py         指标
models/            模型权重(不进 git)
```

## 安装

两条命令:

```bash
pip install vibevibe      # 代码 + Python 依赖
vibevibe setup            # 剩下的:权重、配置、快捷键、开机自启
```

`setup` 干的是 pip 干不了的活儿,每一步都先问你:

| 步骤 | 做什么 |
|---|---|
| Python 依赖 | 确认没漏(pip 应该已经装好) |
| 系统工具 | 检查 `xdotool` / `xclip`,缺了**只打印 apt 命令,绝不替你 sudo** |
| 配置文件 | 写到 `~/.config/vibevibe/config.toml` |
| 模型权重 | 约 4GB,下到 `~/.local/share/vibevibe/models/` |
| 快捷键 | 绑 GNOME 自定义快捷键(默认 `Pause`) |
| 开机自启 | 装 systemd `--user` 服务 |

它是**幂等**的:已经做好的步骤会跳过,重复跑不会搞坏东西。
不想要某步就 `--skip-weights` / `--skip-hotkey` / `--skip-service`;
不想被问就 `-y`。

**路径规则**(这条不说清楚容易踩坑):相对路径的基准分两种情况——
在源码目录里跑就用项目根目录,pip 装完了跑就用 `~/.local/share/vibevibe/`。
判据是项目根目录里有没有 `pyproject.toml`。

## 日常用法

```bash
vibevibe doctor      # 体检:依赖、权重、配置(不加载模型,零成本)
vibevibe devices     # 看有哪些麦克风、哪些键盘设备
vibevibe status      # 看守护进程在干嘛、上一句识别成了什么
vibevibe toggle      # 开始/停止录音 —— 快捷键绑的就是这条
```

守护进程由 systemd 管着,不用手动起。要手动调试就 `vibevibe daemon`。

配置在 `~/.config/vibevibe/config.toml`。**写错的键名会让程序启动失败并报错**,
而不是被静默忽略——静默忽略会让人以为配置生效了,那种坑最难查。

## 托盘图标

顶栏会出现一个 V 图标(双 V,对应 "vibevibe" 两个动作),里面两个开关:

| 开关 | 作用 | 内存 |
|---|---|---|
| **服务** | 守护进程的生死。关掉 = 进程被 systemd 杀掉 | 3.8 GB → **0**(只剩托盘 44 MB) |
| **热加载** | 服务活着时,模型要不要一直待在内存里 | 开 3.8 GB / 关 **0.24 GB** |

热加载关掉后,每次说话前要多等 1.7~2.5 秒把模型载进来;开着则按键立刻出字。
这个开关可以随时切,不用重启服务,而且会写回配置文件。

图标本身也是状态指示:

| 图标 | 含义 |
|---|---|
| 暗淡的 V | 服务已关闭 |
| 明亮的 V | 待命 |
| V + 红点 | 正在录音 |
| V + 琥珀点 | 正在转写 |
| V + 空心圈 | 待命,但模型未载入(按下去要多等一下) |

**托盘是独立进程,不是守护进程的一部分**——必须如此,否则用托盘"关掉服务"
会把托盘自己也关掉,再也没法打开。它只通过 `systemctl --user` 和 Unix socket
跟外界打交道,自己不加载任何模型。

需要 `python3-gi` 和 AppIndicator 绑定:

```bash
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1
```

GNOME 46+ 砍掉了内置托盘,靠扩展提供(Ubuntu 自带 `ubuntu-appindicators`,默认已启用)。

## 设置

托盘 →「设置…」,四个标签页:

| 标签页 | 放了什么 |
|---|---|
| 常规 | **界面与日志语言**(中/英)、识别模型、麦克风 |
| 性能 | 热加载开关、空闲卸载秒数、推理线程数 |
| 反馈 | 提示音开关与音量(带试听)、文字注入方式 |
| 高级 | 录音预处理开关、**打开完整配置文件** |

**改动要点「保存」才生效。** 拨错了开关就点「取消」——因为暂存期间
**一个字节都不会写进配置文件**,取消等于什么都没发生过。
底部会实时显示「● N 项改动未保存」;直接点窗口的 × 会先问一句,
可以选择放弃、返回、或者直接保存。

即改即存看着"省事",实际是把撤销的责任丢给用户自己去回想改过什么。

**为什么还留着配置文件**:界面上只放常用的十几项,而全部可调项有七十多个,
每一项都带着"这个值是怎么测出来的"的注释——那些注释比值本身还值钱。
所以界面是**配置文件常用项的前端**,不是替代品;改动会**逐行写回文件并
保留全部注释**(实测 131 行注释一行不丢)。

改完能不能立刻生效,取决于这个值在哪儿被用到:

- **立刻生效**:语言、提示音、兜底闸阈值、文本后处理、热加载开关
- **要重启服务**:模型后端、推理线程数 —— 界面会明确提示,并给一个「立即重启服务」按钮

语言只影响**用户看得见的东西**(日志、托盘菜单、设置界面、命令行输出)。
代码注释一律保持中文,那是给维护者看的。

## 反馈方式

按下去和出字时各响一声提示音(出错另有一个明显不同的低音)。
为什么用声音不用桌面通知:**听写时眼睛盯着输入框,不会去看屏幕顶部**——
声音不需要你移开视线,也不遮挡任何东西。音量、频率、时长都可配。

## 控制 CPU 占用

主旋钮是配置里的 `asr.qwen_onnx.intra_op_num_threads`。
这台机器 16 线程,给 4 就只占约 1/4,代价是单次转写慢一些。

守护进程**空闲时 CPU 接近 0**,只在你说完话的那一两秒突发占用。

## 选型是怎么定下来的

用**本人的声音、本人的说话习惯**实测,不靠推测。12 条录音、30 个技术词,
其中 10 条是中英夹杂的真实工作用语。

```bash
python bench/record_gui.py                              # 录语料(图形界面)
python bench/compare.py --label c920 --backends qwen_onnx_1p7b --preproc both
```

实测结果:

| 后端 | 技术词命中 | CER | 英文 WER | 平均 RTF |
|---|---|---|---|---|
| Whisper-large-v3-turbo | 60% (18/30) | 0.143 | 0.503 | 0.50 |
| Qwen3-ASR-0.6B | 67% (20/30) | 0.137 | 0.774 | 0.09 |
| Qwen3-ASR-0.6B + 预处理 | 70% (21/30) | 0.132 | 0.309 | 0.08 |
| **Qwen3-ASR-1.7B int4** | **77% (23/30)** | **0.082** | **0.213** | **0.15** |
| Qwen3-ASR-1.7B + 预处理 | 77% (23/30) | 0.086 | 0.243 | 0.15 |

**最该看的指标是技术词命中率**:嵌在中文里的英文词有没有被识别成英文,
而不是音译成中文。这种错在 CER 上看着不严重(实测把 commit / push / remote
三个词全音译掉,CER 也才 0.47),但会让听写完全没法用。

**关于那个削顶重建预处理:它对 0.6B 有明显帮助(英文 WER 0.774→0.309),
但对最终选定的 1.7B 反而略有害,所以默认关闭。** 模型够强的时候,
削顶带来的失真它自己就扛得住,再去"修"是画蛇添足。
换麦克风或换模型后值得重新 A/B 一次(`--preproc both`)。

真实使用的延迟(说完到出字):**0.4 ~ 0.8 秒**,4 个推理线程。

## 状态

- [x] 全部代码、打包、安装向导
- [x] 选型定案:Qwen3-ASR-1.7B int4 + ONNX Runtime + 纯 CPU
- [x] 通道 A:GNOME 快捷键(`Pause`)+ 提示音,日常可用
- [x] systemd `--user` 服务,开机自启
- [x] 托盘图标:服务开关 + 热加载开关
- [ ] 通道 B:F19 + hold 模式 + udev 规则 + 设备独占(等小键盘到货)
- [ ] Wayland 支持(现在只做了 X11)
- [ ] 发布(**未上传 PyPI**)

## 硬件

两键小键盘,键 1 = F19,键 2 = Enter。
**买的时候认准可自定义键码 / 支持 VIA 或 QMK**,别买只能录宏发文本的廉价款
——那种设不了 F19。

## 致谢

Qwen3-ASR ONNX 流水线改造自权重仓库自带的参考实现
[Daumee/Qwen3-ASR-0.6B-ONNX-CPU](https://huggingface.co/Daumee/Qwen3-ASR-0.6B-ONNX-CPU)(Apache 2.0)。
原文没有随本仓库分发,需要对照的话:

```bash
curl -O https://huggingface.co/Daumee/Qwen3-ASR-0.6B-ONNX-CPU/raw/main/onnx_inference.py
```

1.7B 的 ONNX 导出来自 [andrewleech/qwen3-asr-1.7b-onnx](https://huggingface.co/andrewleech/qwen3-asr-1.7b-onnx),
Whisper 对照基线来自 [Zoont/faster-whisper-large-v3-turbo-int8-ct2](https://huggingface.co/Zoont/faster-whisper-large-v3-turbo-int8-ct2)。
