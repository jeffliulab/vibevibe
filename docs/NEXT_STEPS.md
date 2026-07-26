# 当前任务清单

## v0.2.0 任务清单

### 进行中

- [ ] F19 + hold「按住说话」实机验证(等两键小键盘到货)
- [ ] udev 规则实测:`TAG+="uaccess"` 能否让当前用户读到设备
- [ ] EVIOCGRAB 独占实测:按键不会漏进当前窗口

### 待办

- [ ] 上下文偏置(术语表进 system prompt),治 MuJoCo / daemon / CAN 这类专有名词
- [ ] 换近场麦克风后重跑 `bench/compare.py --preproc both`,重新判断预处理该不该开
- [ ] 静音自动停止(配置项已留,逻辑未接)
- [ ] 长音频 VAD 切分接 silero-vad

### 阻塞

- [ ] 两键小键盘尚未到货 —— hold 模式的三项实机验证全部卡在这

### 已完成(v0.1.0)

- [x] 选型:三模型对照跑分,用数据选 Qwen3-ASR-1.7B int4
- [x] 纯 CPU 推理管线(ONNX Runtime,不碰 CUDA)
- [x] 兜底闸:挡 Qwen3-ASR 无限重复 bug
- [x] GNOME 快捷键通道 + 三种提示音
- [x] 托盘图标:服务开关 + 热加载开关
- [x] 设置界面:中英双语 + 保存/取消暂存语义
- [x] systemd 用户服务、安装向导、PyPI 发布
