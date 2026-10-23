---
name: vmware-vnc
description: Help AI assistants reliably observe and control VMware virtual machines through VNC. Use for screenshots, mouse and keyboard actions, text input, display recovery, visual condition waits and independent console sessions.
license: Apache-2.0
---

# VMware VNC

让 AI 通过 VNC 可靠地观察并操作虚拟机，再核对界面结果。当前主要面向 VMware；基础截图、键鼠和画面条件不要求特定来宾 OS、来宾账号、Tools 或外部 CLI，但需要可达的 VNC 端点、相应认证和有效画面。其他服务端的正式支持待定，范围见 [兼容性](docs/COMPATIBILITY.md)。

MCP 是 AI 连续交互的主要入口，可保留连接、会话和画面参考，不绑定特定 AI 宿主。随包的 `vmware-vnc` CLI 作为持续维护的辅助入口，提供单次操作、诊断和脚本集成；两者复用同一套核心能力。任务也可按需结合 `vmrun` 或其他工具，基础 VNC 接管独立可用。

本文件所在目录也可作为 `vmware-vnc` 技能分发；使用 MCP 或 CLI 本身不要求安装 Codex skill。

## 准备

- 首次安装、环境缺失或需要注册 MCP 时，读 [安装与接入](references/setup.md)。脚本从技能实际位置定位资源，依赖使用随包锁文件。
- 使用当前聊天已加载的 VNC 工具。修改磁盘代码或配置后，先确认 MCP 进程已重载；`vnc_diagnostics` 可核对运行版本。
- MCP 暂不可用时，可使用随包 CLI 执行独立的 VNC 操作。命令参数见 [操作参考](references/operations.md)。

## 调用顺序

1. 使用明确的 `host`、`port` 和 `session_id` 连接，或显式选择已配置的 `profile`，也可单独使用 `url` 选择原生／TLS／WebSocket 端点；保存返回的 `connection_id`。
2. 立即截图，核对实际画面、窗口和原生尺寸后再输入。默认截图为原生尺寸 RGB PNG；只有需要更小的有损文件时才选 JPEG。RGB8 描述 VNC 服务端帧缓冲，不表示显示器 HDR 或校准色彩。
3. 具名会话的截图、输入、等待和断开同时传入 `session_id`、本次 `connection_id`。不同桌面使用不同会话名。
4. 根据新画面执行动作，再观察结果。区域截图内的坐标须加上截图原点，鼠标接口始终接收桌面原生坐标。
5. 结束时断开本次连接。重连后换用新 ID，并重新检查画面。

会话名可由调用方选取，避免覆盖其他任务的会话。省略 `session_id` 会访问独立的 default 会话，不会自动选择最近连接的具名会话。会话默认空闲 300 秒回收；旧连接 ID 失效后须显式重连。具体参数、多桌面与 CLI 示例见 [操作参考](references/operations.md)。

## 文本与画面条件

- `vnc_text`、`vnc_type` 与 CLI `text` / `type` 的 `auto` 只选已协商的 UTF-8 剪贴板或明确绑定的来宾 profile。两者都没有时，任何非空文本都在点击焦点和发送输入前拒绝；需要键盘事件时，先从截图确认目标控件和输入法，再显式选 `method="keys"`。逐键结果带 `input_method_sensitive=true`，`application_verified` 仍为 `false`；输入可能被输入法转换，必须截图核对应用结果。`delay_ms` 仅与显式 `method="keys"` 同用。
- Linux 来宾 profile 可选择 X11 或 Wayland 剪贴板后端。Wayland 只在 socket 明确且归属当前账号时使用；GNOME 原生 Wayland 的焦点限制尚未实测解决，显式使用 Xwayland 与现有 X11 剪贴板工具是兼容路径。配置见 [文本输入](references/text-input.md)。
- 来宾辅助输入需要本次截图确认的 `focus_x/focus_y`。准备剪贴板后的点击会取消旧选区，替换内容前先明确清空。
- 等待变化或匹配前，用 `vnc_screenshot(remember=true)` 保存参考；stable 使用自己的起始画面。参考生命周期、区域和容差见 [画面条件](references/visual-waits.md)。
- 需要精确读像素、检查微小变化或放大局部时调用 `vnc_inspect`。它返回未放大的原色 PNG、精确 RGB 与可选细节图；放大差异图会改变颜色，不能当作原始桌面像素。
- `vnc_inspect(frame_id=...)` 可复用最近截图帧而不发网络请求。新捕获或重连会使旧 `frame_id` 失效；`remember=true` 的参考图仍按原有连接生命周期保留。
- `matched`、剪贴板校验和已发送粘贴分别描述工具完成的步骤。应用是否收到文本或完成任务，仍须检查实际结果。

## 异常与维护

失败、超时或取消的输入不自动重放；先检查当前画面与连接状态。若连接返回 `display_state="unavailable"`（例如 0×0 帧缓冲），显式调用 `vnc_wake(session_id, connection_id)` 或 CLI `wake`，再截图并检查新画面。wake 只发送两次指针移动，不点击、不按键、不解锁；`wake_sent` 只表示动作已发送，不能证明显示已恢复。0×0 resize 会使当前可用画面失效，截图不会返回旧图；普通非零 resize 的旧快照仍保留原尺寸，直到新画面完整。截图的 `auto` 模式会先在已有连接上尝试一次有界完整刷新，失败后用短暂共享连接取完整首帧；需要指定行为时仍可选 `refresh` 或 `fresh`。保持的按键、鼠标按钮及会话资源由断开清理。组合键必须完整释放，分辨率变化后重新截图并确认坐标。连接、刷新、输入和队列问题见 [故障排查](references/troubleshooting.md)。

修改后运行 `scripts/Test.ps1`；默认检查只连接本机人工 RFB 服务。兼容范围及实机验收方法见 [兼容性](docs/COMPATIBILITY.md)，维护要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。
