[![Language: English](https://img.shields.io/badge/Language-English-2f81f7?style=flat-square)](README.md) [![语言: 简体中文](https://img.shields.io/badge/语言-简体中文-e67e22?style=flat-square)](README_zh.md)

# vibevibe

[![PyPI](https://img.shields.io/pypi/v/vibevibe?style=flat-square&color=3775a9)](https://pypi.org/project/vibevibe/)
[![Python](https://img.shields.io/pypi/pyversions/vibevibe?style=flat-square)](https://pypi.org/project/vibevibe/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20%C2%B7%20X11-lightgrey?style=flat-square)](#运行环境)
[![Status](https://img.shields.io/badge/status-alpha-orange?style=flat-square)](#项目状态)

**Linux 上的离线中英混说语音听写。按一个键说话，文字直接落到光标处，全程不出本机。**

[PyPI](https://pypi.org/project/vibevibe/) · [版本发布](https://github.com/jeffliulab/vibevibe/releases) · [问题反馈](https://github.com/jeffliulab/vibevibe/issues)

---

## 亮点

- **为中英混说而生。** 基于 [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)——目前唯一明确针对 code-switching 训练的开源 ASR，训练时往中文语料里注入了 4 万多个英文技术词。
- **纯 CPU。** 不装 CUDA、不装 PyTorch、不占显存。跑在 ONNX Runtime 上，所以显卡忙着训练的时候，听写照样能用。
- **完全离线。** 不要账号、不要 API key，运行时一个网络请求都没有。
- **松手到出字 0.4–0.8 秒**，在 Ryzen 7 9800X3D 上用 4 个推理线程实测。
- **内存自己说了算。** 托盘开关在「模型常驻」（约 3.9 GB）和「用时才加载」（约 0.24 GB）之间切；关掉服务则全部归还。
- **中英双语界面。** 界面和日志可在运行时切换。

---

## 目录

- [这东西为什么存在](#这东西为什么存在)
- [实测数据](#实测数据)
- [运行环境](#运行环境)
- [快速开始](#快速开始)
- [用法](#用法)
- [配置](#配置)
- [项目结构](#项目结构)
- [自己跑一遍跑分](#自己跑一遍跑分)
- [排查](#排查)
- [项目状态](#项目状态)
- [链接](#链接)
- [致谢](#致谢)

---

## 这东西为什么存在

用 coding agent 时打字是瓶颈，听写本来能解决——除非你说话是很多工程师的那种方式：**中文句子里嵌英文技术词**。就这一条，把现有方案几乎全部淘汰了：

| 方案 | 为什么不行 |
| --- | --- |
| Claude Code 自带的 `/voice` | 音频要上传转写，而且支持的听写语言里没有中文 |
| Linux 上的开源听写工具（Handy、hyprwhspr、VoxType、OpenWhispr、whisrs…） | 后端清一色 whisper.cpp / Parakeet / Vosk，**没有一个支持 Qwen3-ASR** |
| 商业产品（Wispr Flow、superwhisper、VoiceInk…） | 只有 macOS / Windows，Linux 是被放弃的市场 |

`Linux + 离线 + 中英混说`这个交叉点是空的。vibevibe 站在这儿。

---

## 实测数据

用**本人的 12 条录音**、含 **30 个技术词**测的，录音设备是摄像头麦克风且增益拉满——也就是**带削顶的真实工况**，不是干净的实验室录音。

| 后端 | 技术词命中率 | CER | 英文 WER | 平均 RTF |
| --- | --- | --- | --- | --- |
| **Qwen3-ASR 1.7B int4**（默认） | **77 %**（23/30） | **0.082** | **0.213** | 0.15 |
| Qwen3-ASR 0.6B int8 | 70 %（21/30） | 0.132 | 0.309 | 0.08 |
| Whisper large-v3-turbo int8 | 60 %（18/30） | 0.143 | 0.503 | 0.50 |

**技术词命中率才是这里该看的指标。** 中英混说识别的典型失败是把英文词音译成中文字：句子读起来依然通顺，所以 CER 几乎不动，但结果根本没法用。实测有一条把 `commit` / `push` / `remote` 全音译掉了，CER 也才 0.47。

`RTF` = 处理耗时 ÷ 音频时长，越小越快；0.15 意味着 8 秒的话约 1.2 秒转写完。

<details>
<summary>所有模型（含 Whisper）都栽的词</summary>

`MuJoCo`、`CAN`（和常用中文词同音）、`daemon`、`Claude Code`、`checkpoint`、`benchmark`。

这些专有名词在中文句子里发音本身就有歧义。Qwen3-ASR 支持上下文偏置（把术语表放进 system prompt），应该能治好大半，目前还没接上。

</details>

<details>
<summary>为什么音频预处理默认关闭</summary>

仓库里有一段削顶重建，用两侧波形插值补回被削平的波峰。受控实验（拿未削顶的干净语音人为削顶再重建）显示：**在真实录音的削顶程度上信噪比改善 +1.7 dB，且没有一条被弄差**。

但它默认**关着**，因为 A/B 跑分表明这个收益是模型特异的：

| 后端 | 技术词命中 | 关键指标变化 |
| --- | --- | --- |
| 0.6B | 67 % → **70 %** | 英文 WER 0.774 → **0.309** |
| 1.7B | 77 % → 77 % | CER 0.082 → 0.086（略差） |
| Whisper | 60 % → 60 % | WER 0.503 → 0.526（略差） |

模型够强的时候，削顶带来的失真它自己就扛得住，再去「修」反而是画蛇添足。换麦克风后值得重测：`bench/compare.py --preproc both`。

</details>

---

## 运行环境

| | |
| --- | --- |
| 操作系统 | Linux + **X11**（Wayland 尚未支持） |
| Python | 3.11+ |
| 磁盘 | 模型权重约 4 GB |
| 内存 | 模型常驻时约 4 GB，否则约 0.25 GB |
| 显卡 | **不需要，也不会用** |

系统包（用包管理器装，不是 pip）：

```bash
sudo apt install xdotool xclip                                  # 文字注入
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1     # 托盘图标（可选）
```

GNOME 46+ 的托盘需要扩展提供，Ubuntu 自带 `ubuntu-appindicators` 且默认启用。

---

## 快速开始

```bash
pip install vibevibe
vibevibe setup
```

`setup` 负责 pip 干不了的一切。每一步都先问你、可以重复跑不会搞坏东西，而且**绝不替你 `sudo`**——缺系统包时只把命令打出来让你自己跑。

```
[1/7] Python 依赖
[2/7] 系统工具
[3/7] 配置文件            ~/.config/vibevibe/config.toml
[4/7] 模型权重            约 4 GB
[5/7] 桌面快捷键          默认 Super+Shift+V
[6/7] 开机自启服务        systemd --user
[7/7] 托盘图标
```

然后按 **Super+Shift+V**，说话，再按一下，文字就出现在光标处。

---

## 触发方式

两条独立通道，同时生效：

| 通道 | 按键 | 需要什么 | 给谁用 |
| --- | --- | --- | --- |
| **桌面快捷键** | `Super+Shift+V`（可改） | **什么都不需要**——零硬件、零权限 | 任何人，`pip install` 完就能用 |
| **小键盘** | `F19`（固定） | 两键小键盘 + 一条只针对它的 udev 规则 | 有专用硬件的人 |

小键盘通道直接读输入设备，所以能用 F13~F24——这些键桌面快捷键**根本绑不了**。
小键盘固件里也烧成 F19，换任何一台电脑插上都能用，不用装任何东西。

**为什么默认 `Super+Shift+V` 而不是 `Ctrl+Shift+V`：** 后者在浏览器、VS Code、
终端里都是「粘贴为纯文本」。那是**应用内部**的绑定，任何系统层的检查都看不到。
而应用基本不碰 Super 键——它属于桌面环境——所以 Super 系组合最安全。

设置 →「按键」里可以重绑任一通道，**按下你想用的键即可捕获**（支持组合键）。
每个候选都会先查冲突：

- 已被系统快捷键占用 → **拒绝**
- 已是 vibevibe 自己的快捷键 → **拒绝**（两条通道会同时触发，toggle 模式下等于
  连切两次，表现为「按了没反应」）
- 在 X11 里有 keysym → **允许但警告**，因为它同时会送到当前应用

## 用法

### 听写

| 动作 | 结果 |
| --- | --- |
| 按快捷键 | 开始录音——一声短促的高音 |
| 再按一次 | 转写并粘贴到光标处——一声较低的音 |
| 出问题了 | 明显更低更长的一声，而且**一个字都不会粘出去** |

用声音而不是弹窗是有意的：听写的时候你眼睛盯着输入框，不会去看屏幕顶部。

### 托盘图标

顶栏会出现一个双 **V** 图标，既是控制面板也是状态指示：

| 图标 | 含义 |
| --- | --- |
| 暗淡的 V | 服务已关闭 |
| 明亮的 V | 待命 |
| V + 红点 | 正在录音 |
| V + 琥珀点 | 正在转写 |
| V + 空心圈 | 待命，但模型未载入——下次按键要多等约 2 秒 |

两个开关：

| 开关 | 作用 | 内存 |
| --- | --- | --- |
| **服务** | 守护进程的生死 | 3.9 GB → **0**（只剩 44 MB 的托盘） |
| **热加载** | 模型是否常驻内存 | 开 3.9 GB / 关 **0.24 GB** |

菜单里还有**「关于」**，显示版本号、当前使用的各个路径，以及打开日志的按钮。

两个退出项，别点错：

| 菜单项 | 做什么 |
| --- | --- |
| **退出托盘（服务继续运行）** | 只关托盘图标，守护进程照旧待命，听写快捷键仍然好用 |
| **退出 vibevibe（停止服务）** | 守护进程和托盘一起退，内存全部还给系统；下次登录自动回来，想现在开回来就跑 `vibevibe tray` |

> 托盘是**独立进程**，不是守护进程的一部分。必须如此——否则用托盘「关掉服务」会把托盘自己也关掉，再也没法打开。正因为是两个进程，「退出整个程序」才必须显式地把两边都停掉。

### 设置

托盘 →「设置…」，或 `vibevibe settings`。

| 标签页 | 内容 |
| --- | --- |
| 常规 | 界面与日志语言、识别模型、麦克风 |
| 按键 | 触发方式、桌面快捷键、小键盘设备与触发键——带冲突检查 |
| 性能 | 热加载、空闲卸载秒数、推理线程数 |
| 反馈 | 提示音开关与音量（带试听）、文字注入方式 |
| 高级 | 音频预处理、**打开完整配置文件** |

改动**只暂存，不立即生效**。不点「保存」就一个字节都不会写进配置文件；点「取消」等于什么都没发生过。底部实时显示 `● N 项改动未保存`，带着未保存改动关窗口会先问一句。

### 命令行

```bash
vibevibe toggle      # 开始/停止录音 —— 快捷键绑的就是这条
vibevibe status      # 守护进程状态、内存、上次识别结果
vibevibe doctor      # 体检依赖、权重、配置（不加载模型）
vibevibe devices     # 列出麦克风和键盘设备
vibevibe settings    # 打开设置窗口
vibevibe tray        # 启动托盘图标
vibevibe daemon      # 前台运行守护进程，调试用
vibevibe quit        # 退出：停掉服务和守护进程（托盘另外关）
```

守护进程平时由 systemd 管着：

```bash
systemctl --user status vibevibe
journalctl --user -u vibevibe -f
systemctl --user disable vibevibe   # 不想让它开机自启了
```

---

## 配置

`~/.config/vibevibe/config.toml`。设置窗口覆盖常用项；文件里是全部七十多项，每一项都带注释说明这个默认值是怎么测出来的。

**写错的键名会让程序启动失败并报错，而不是被静默忽略**——静默忽略会让你以为改的配置生效了，实际没有。

最值得知道的两个旋钮：

```toml
[daemon]
# 模型常驻内存。关掉 = 用时才加载，约 0.24 GB，每次多等 1.7~2.5 秒。
hot_reload = true

[asr.qwen_onnx_1p7b]
# 0 = 用满所有核。这是控制 CPU 占用的主旋钮。
intra_op_num_threads = 4
```

---

## 项目结构

```
vibevibe/
  config.py           所有可调项集中于此，别处不写死任何值
  asr/                可插拔的识别后端
    qwen_onnx.py        Qwen3-ASR（ONNX Runtime）—— 默认
    whisper_ct2.py      Whisper large-v3-turbo —— 对照基线
    qwen_hf.py          Qwen3-ASR（PyTorch）—— 可选，需另装 torch
  recorder.py         录音
  audio_preproc.py    削顶重建，默认关闭（见实测数据）
  guard.py            兜底闸，挡 Qwen3-ASR 已知的无限重复 bug
  daemon.py           状态机 + Unix socket 服务端
  hotkey_evdev.py     直读输入设备的通道，给专用小键盘用
  inject.py           把文字送进光标处
  sound.py            提示音，现算的正弦波，不依赖音频文件
  tray.py             系统托盘图标
  service.py          systemd 用户服务的操作，以及「把 vibevibe 整个停掉」
  settings_dialog.py  GTK 设置窗口
  setup_wizard.py     `vibevibe setup`
  i18n.py             中英文案表
  data/               配置模板、udev 规则、systemd 单元、图标
bench/                选型验证工具
  corpus.toml           要念的测试语料
  record_gui.py         录音界面，带实时波形
  compare.py            对照跑分
  metrics.py            CER / WER / 技术词命中率
```

---

## 自己跑一遍跑分

上面那些数字来自一个人的声音和说话习惯，你的一定不一样。工具都包含在内——**自己测，别信**：

```bash
python bench/record_gui.py                         # 录语料（图形界面，实时波形）
python bench/compare.py --label mine \
    --backends qwen_onnx_1p7b,qwen_onnx \
    --preproc both --threads 4
```

两个后端跑 12 条录音大约一分钟。

---

## 排查

<details>
<summary>按键没反应</summary>

```bash
vibevibe status
systemctl --user status vibevibe
```

守护进程正常的话，多半是快捷键没绑上。去 GNOME 设置 → 键盘 → 自定义快捷键看看，或者重跑一次 `vibevibe setup`。

</details>

<details>
<summary>识别出来了但文字没出现</summary>

缺 `xdotool` 或 `xclip`。`vibevibe doctor` 会告诉你，守护进程启动时也会打警告。

</details>

<details>
<summary>识别不准</summary>

**先查麦克风，通常就是麦克风的问题。** 用 `bench/record_gui.py` 录一条，盯着实时波形：如果顶到天花板就是削顶了，去调低 ALSA 采集增益（`alsamixer`，或 `amixer -c N sset Mic 48`）。一个几十块的近场麦，胜过任何模型调优。

</details>

<details>
<summary>托盘图标没出现</summary>

```bash
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1
gnome-extensions list --enabled | grep appindicator
```

GNOME 46+ 砍掉了内置托盘，必须靠扩展提供。

</details>

---

## 项目状态

Alpha。作者天天在用，但还年轻。

- [x] 纯 CPU 推理，选型有实测支撑
- [x] GNOME 快捷键通道
- [x] 托盘图标、设置窗口、中英双语
- [x] systemd 用户服务、PyPI 发布
- [x] **两键小键盘** —— 用 VIA 协议烧键位（左 → F19，右 → Enter），已实机验证
- [ ] Wayland 支持
- [ ] 上下文偏置，治剩下那几个专有名词

---

## 链接

- **PyPI** —— https://pypi.org/project/vibevibe/
- **源码** —— https://github.com/jeffliulab/vibevibe
- **Qwen3-ASR** —— [技术报告](https://arxiv.org/abs/2601.21337) · [仓库](https://github.com/QwenLM/Qwen3-ASR)

---

## 致谢

ONNX 流水线改造自权重仓库自带的参考实现
[Daumee/Qwen3-ASR-0.6B-ONNX-CPU](https://huggingface.co/Daumee/Qwen3-ASR-0.6B-ONNX-CPU)（Apache-2.0）。原文没有随本仓库分发，需要对照的话：

```bash
curl -O https://huggingface.co/Daumee/Qwen3-ASR-0.6B-ONNX-CPU/raw/main/onnx_inference.py
```

模型权重：[andrewleech/qwen3-asr-1.7b-onnx](https://huggingface.co/andrewleech/qwen3-asr-1.7b-onnx) ·
[Daumee/Qwen3-ASR-0.6B-ONNX-CPU](https://huggingface.co/Daumee/Qwen3-ASR-0.6B-ONNX-CPU) ·
[Zoont/faster-whisper-large-v3-turbo-int8-ct2](https://huggingface.co/Zoont/faster-whisper-large-v3-turbo-int8-ct2)

本项目以 Apache-2.0 许可证发布。
