# 中文与 Unicode 文本

`vnc_text`、`vnc_type` 和 CLI `text` / `type` 共用 `send_text`。连接后先检查完整截图和输入控件；具名调用始终携带 `session_id`、`connection_id`。若连接仍在但 `display_state="unavailable"`，不能据旧画面继续输入；可尝试显式 `vnc_wake`，再截图确认完整新帧。未完成的首帧不会作为可用截图返回。

## 自动通道与显式键盘

| method | 行为与条件 |
| --- | --- |
| auto | 只选择已协商的 VNC UTF-8 Extended Clipboard，或当前连接显式绑定的 guest profile。两者都没有时，任何非空文本（包括 ASCII）都在点击焦点或发送输入前拒绝；不会退回按键 |
| clipboard | 通过 VNC 剪贴板发送，再按 `paste_key` 粘贴；要求协商 UTF-8 扩展，缺少扩展时只接受 Latin-1 |
| guest | 使用绑定到当前 VNC 端点的 VMware profile 准备来宾剪贴板，校验并清理临时文件，再粘贴；必须提供截图中目标控件的 `focus_x/focus_y` |
| keys | 显式发送 VNC 键盘事件，不调用剪贴板。按键如何映射成文字取决于服务端、目标应用和来宾当前输入法 |

`auto` 没有可用剪贴板通道时不会点击焦点，也不会发送任何键盘事件。查看 `vnc_status` 的 capabilities 是否包含 `utf8_clipboard`、连接是否绑定 guest profile。需要纯键盘输入时，先截图确认目标控件和当前输入法，再显式调用 `method="keys"`；工具不会替你切换输入法。键盘结果带 `input_method_sensitive=true`，且 `application_verified` 为 `false`。按键事件可能被输入法转换，不能据此保证字面文本；必须再截图核对实际应用内容。

```python
# 若没有 UTF-8 协商或显式 guest profile，此调用会在输入前拒绝，不回退到按键。
vnc_text(text="中文与 emoji 😀", focus_x=300, focus_y=200,
         session_id="linux", connection_id="<本次 ID>")

# 仅在截图确认焦点和输入法后显式使用键盘事件。
vnc_type(text="已确认模式下的按键", method="keys", delay_ms=40,
         session_id="linux", connection_id="<本次 ID>")
```

键盘事件至少间隔 20 毫秒；`vnc_type` 或 CLI `type` 的 `delay_ms` 只能与显式 `method="keys"` 一起使用。`method="auto"` 加 `delay_ms` 会在输入前拒绝，即使剪贴板通道可用。超出动作预算的键盘文字也会在点击焦点或发送按键前拒绝；按应用状态分段提交并逐段检查。

按版本记录的历史应用输入见 [兼容性](../docs/COMPATIBILITY.md)。旧版 `auto` 曾可能回退按键，因此旧记录不能证明当前自动通道行为；当前键盘路径须显式指定 `method="keys"`。

## 标准 VNC 剪贴板

无需来宾账号的 VNC UTF-8 输入依赖服务端协商 Extended Clipboard。`vnc_status` 返回 `utf8_clipboard` capability；安装 VMware Tools 本身不会使该 VNC 扩展可用。

```python
vnc_text(text="中文与 emoji 😀\n第二行", focus_x=300, focus_y=200,
         session_id="linux", connection_id="<本次 ID>")
```

focus 坐标来自刚检查过的截图。普通文本控件默认 ctrl-v；终端常用 ctrl-shift-v 或 shift-insert，按实际应用设置 `paste_key`。`clipboard_offered` 表示已提交剪贴板所有权通知，`clipboard_data_sent` 表示文字数据已写入 VNC 连接；两者都不代表应用已读到内容。`vnc_clipboard_get` 返回最近接收的远端通知缓存，不是来宾剪贴板的实时查询接口。

## 可选来宾剪贴板后端

来宾剪贴板是文本增强，使用 VMware `vmrun`、来宾凭据及来宾内已有剪贴板工具。profile 必须绑定到完全匹配的 VNC host/port；它不能服务基础截图或键鼠。只有显式绑定 profile 后，`auto` 才会在未协商 VNC UTF-8 扩展时选择该后端。

复制 [profiles.toml.example](../assets/profiles.toml.example) 到本机配置，再按实际来宾设置 profile。Linux 可选字段如下：

```toml
clipboard_backend = "auto" # auto、x11 或 wayland
# display = ":0"            # X11 display，可与 auto 或显式 x11 配合
# xauthority = "/home/guest/.Xauthority"
# wayland_display = "wayland-0" # 当前账号运行时目录内的 socket 名
```

`auto` 在显式设置 `display` 时固定使用 X11，并要求该会话可用且安装 xclip/xsel；没有 profile display 时，若继承的 X11 display 和工具可用则优先 X11，否则才尝试当前账号拥有的唯一 Wayland socket。Wayland socket 可通过 `wayland_display` 明确指定，也可在只有一个候选时自动选择；多个候选时拒绝猜测。显式 `clipboard_backend="wayland"` 不能与 `display` 同用；`wayland_display` 不能与显式 `clipboard_backend="x11"` 同用。X11 目标或 Wayland socket 一旦选定，准备失败不会切换后端重试，也不会重新发送文字。

Wayland 调用 `wl-copy` 与 `wl-paste` 时显式使用相同的 `text/plain;charset=utf-8` MIME 类型。超时或失败会回收本次调用创建的子进程组，诊断仅返回固定原因标签。上游 [wl-clipboard BUGS](https://github.com/bugaevc/wl-clipboard/blob/master/data/wl-clipboard.1) 记录了部分 compositor 未将焦点交给临时 surface 时剪贴板访问可能挂起，也指出文本 MIME 类型推断可能失败；本项目用显式 MIME 和有界失败处理这些边界，不复制其 GPL 代码。

GNOME 原生 Wayland 焦点策略仍是外部限制，当前新后端尚未完成 VMware 实机验收，不能据此宣称所有 Wayland 桌面都可用。需要复用现有工具的兼容路径时，可明确设置 Xwayland `display`，选择 `clipboard_backend="x11"` 并安装 xclip/xsel；仍须在目标应用中核对粘贴效果。

Linux 来宾需要 Python 3，并安装所选 backend 对应的剪贴板工具。X11 还需正确的 display 和必要的 xauthority。Windows 来宾的 `guest_temp_dir` 必须是已登录用户可读写的目录；系统服务临时目录可能无法由交互桌面读取。`username_env`、`password_env` 和可选 `vm_password_env` 只填写环境变量名，真实凭据由启动 MCP/CLI 的本机环境提供。

服务启动时用 `VNC_PROFILES_FILE` 指向本机配置；`vnc_profiles()` 只列端点与通道信息。显式选择 profile 后，仍须重新截图确认目标，再提交文字。准备后点击会取消旧选区；替换内容前先明确清空。按键或鼠标按钮仍被按住时，文字提交会被拒绝。

每次至多 100,000 字符，拒绝 NUL 和无效 Unicode surrogate；协议消息及解压内容各有 1 MiB 上限。Windows 辅助输入将换行规范为 CRLF，协议读回以 LF 表示。`submitted_characters` / 兼容字段 `typed_characters` 是提交数量；`clipboard_verified` 只证明剪贴板校验成功，`paste_sent` 只证明粘贴事件已发出，`application_verified` 固定为 false。请用新截图或应用自己的读回结果确认文本。

CLI `text` 和 `type` 都支持 `--method`、`--paste-key` 与 `--focus X Y`；`type --delay-ms N` 只可与 `--method keys` 搭配。准备、认证、清理、超时或取消失败时不自动重放输入；剪贴板可能已变化，键盘输入也可能部分送达，先观察当前状态再决定下一步。
