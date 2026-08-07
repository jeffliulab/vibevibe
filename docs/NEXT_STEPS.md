# 当前任务清单

## v0.3.0 任务清单

### 进行中

- [ ] 上下文偏置(术语表进 system prompt),治 MuJoCo / daemon / CAN 这类专有名词
- [ ] Wayland 支持(注入换 wl-copy + wtype/ydotool)

### 待办

- [ ] 换近场麦克风后重跑 `bench/compare.py --preproc both`
- [ ] 静音自动停止(配置项已留,逻辑未接)
- [ ] 长音频 VAD 切分接 silero-vad
- [ ] 转写期间光标移动导致文字落到新位置 —— 已知行为,待定要不要修

### 已完成(v0.2.1)

- [x] 托盘「退出 vibevibe(停止服务)」+ 命令行 `vibevibe quit` —— 之前根本没有整个退出的入口
- [x] 自启 `.desktop` 里的 `Exec=` 路径失效时,`vibevibe setup` 自动重写

### 已完成(v0.2.0)

- [x] 拔插复验键位持久化通过 —— 读回 KC_F19 / KC_ENTER 不变,证明是板载 EEPROM

- [x] 两键小键盘接入:VIA 协议改键(左 F19 / 右 Enter)+ udev 规则
- [x] 双触发通道:evdev 小键盘 + 桌面快捷键,同时生效
- [x] 热键冲突检查(系统快捷键 / 自身快捷键 / keysym 泄漏)
- [x] 设置界面「按键」页 + 按下即捕获(含组合键)
- [x] 触发来源日志、托盘异常可见化
