# 中文与 Unicode 文本

连接后先检查完整截图和输入控件。具名输入与画面检查都须携带 `session_id`、`connection_id`。

| 任务 | 接口 |
| --- | --- |
| 把命令、路径或一段文字原样交给应用 | `vnc_text` / CLI `text`。先看连接的 `capabilities.literal_text_transport` |
| 判断物理敲键会受到哪些输入状态影响 | `vnc_inspect(keyboard_fields=[...])` / CLI `inspect --keyboard-field ...`。按字段直读，需要时查看同一张图 |
| 模拟普通打字、拼音选词，或在已知控件中输入 Unicode 码位 | `vnc_type_keys` / CLI `type-keys`。确认焦点和输入模式后可一次发送整段 |

## 原样输入文字

命令、文件路径、代码和中英文混排都使用同一个文字接口。以 PowerShell 为例，美元符号、反引号、引号和反斜杠均作为数据传输。通过 CLI 提交包含宿主 shell 特殊字符的文字时，可用 `text --text-file <UTF-8 文件>` 保留原文。

| method | 行为与条件 |
| --- | --- |
| auto | 优先使用协商的 UTF-8 Extended Clipboard，其次使用当前连接明确绑定的来宾 profile |
| clipboard | 仅使用已协商的 UTF-8 Extended Clipboard |
| guest | 使用绑定到当前 VNC 端点的来宾 profile 准备剪贴板，校验内容并清理临时文件，随后点击截图中指定的 `focus_x/focus_y` 并粘贴 |

连接和 `vnc_status` 返回的 `capabilities.literal_text_transport` 为 `clipboard`、`guest` 或 `null`。值为 `null` 时，非空文字会在点击或按键前被拒绝。若目标支持本文的控件码位输入，可显式改用键盘。若已配置来宾通道，也可通过 `vnc_profiles()` 选择与端点匹配的 profile。部分 VMware 端点会忽略基础 VNC 的 Latin-1 ClientCutText。

```python
vnc_connect(profile="windows-guest", session_id="work")
vnc_screenshot(session_id="work", connection_id="<本次 ID>")
vnc_text(text=r"""$r = Join-Path $env:TEMP '中文 A+B'; $r | Write-Output""",
         focus_x=300, focus_y=200, session_id="work", connection_id="<本次 ID>")
```

## 模拟敲键

`vnc_type_keys` 和 CLI `type-keys` 只发送普通 RFB 键盘事件。焦点与输入条件确认后，整段可在一次调用中发送，结束后核对结果。焦点变化或出现候选框时再观察。

| `entry_mode` | 按键行为 | 适用条件 |
| --- | --- | --- |
| `direct`（默认） | 按提交字符发送键符。CRLF、CR、LF 各按一次回车 | 拼音选词、物理键盘测试或已知输入模式下的普通敲键 |
| `richedit-hex` | 每个字符输入 4 至 6 位十六进制，选中刚输入的数字，再按 Alt+X | 已聚焦、支持十六进制转换的 RichEdit 控件，如 Windows 10 写字板 |
| `gtk-hex` | 每个字符依次发送 Ctrl+Shift+U、十六进制、回车 | 已确认支持 GTK 码位输入的控件。其他控件可能把回车当作提交 |

码位输入要求 ASCII 数字和 A 至 F 能直接进入控件。大小写都能表示同一个十六进制数字。两种码位方式都拒绝 C0/C1 和 DEL 控制字符。模式由调用方选择，保持当前输入法。`delay_ms`（CLI `--delay-ms`）可增加逐键间隔，最小间隔为 20 毫秒。预估展开后的按键超出动作预算时，整批在第一个键前被拒绝。输入中断后先检查已有内容。

```python
vnc_type_keys(text="A4F中文+7，$", entry_mode="richedit-hex",
              session_id="work", connection_id="<本次 ID>")
```

协议依据与控件行为见 [Windows RichEdit 说明](https://learn.microsoft.com/en-us/windows/win32/intl/hextounicode-ime) 和 [GTK 数字输入说明](https://docs.gtk.org/gtk4/class.IMContextSimple.html)。迁移已有脚本时，将模拟敲键用的 `method="keys"` 改为 `vnc_type_keys`。

### 检查键盘和输入法状态

`vnc_status` 可在已有连接上读取 `keyboard_state.leds`。端点尚未报告时为 `null`。收到报告后，`source` 给出 `vmware` 或 `qemu`，三个锁定键各有一个布尔值。本连接发送锁定键后会作废旧报告，直到端点再次上报。服务端的报告可能略晚于实际按键。[VMware LED State](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst#vmware-led-state-pseudo-encoding) · [QEMU LED State](https://www.qemu.org/docs/master/interop/vnc-ledstate-pseudo-encoding.html)

需要决定下一步如何敲键时，可在一次 `vnc_inspect` 中列出关心的字段。锁定键可选 `caps_lock`、`num_lock`、`scroll_lock`。输入法名称、输入模式、全半角、正在组合或选词分别使用 `ime_name`、`ime_mode`、`character_width`、`composition`。

```python
vnc_inspect(keyboard_fields=["caps_lock", "ime_mode"],
            region=indicator_region, session_id="work", connection_id="<本次 ID>")
```

`indicator_region` 是从该桌面确认的输入法指示区域，包含 `x`、`y`、`width`、`height`。也可省略区域以查看全桌面。全部字段可直读时，结果仅包含数据。有待确认字段时，调用最多返回一张原色 PNG。`keyboard.fields` 记录端点报告的值和来源，`keyboard.unresolved_fields` 列出仍需判断的字段，`keyboard.visual_evidence` 标明是否有可查看的图。明确提供 `frame_id` 时，图片表示那一帧的状态。

查看当前输入法指示、全半角标识或候选窗口，只判断图中清晰显示的项目。布局缩写或没有看到候选框都不足以确定中英文模式。在 Windows 10 写字板里切换搜狗的“中”和“英”时，实测三盏灯均保持关闭。如果只有 Caps Lock 是前置条件，可以直接在 `vnc_type_keys` 指定 `expect_caps_lock=False`（CLI `--expect-caps-lock off`）。报告缺失或不符时会停止发送。

### 在已有的 PowerShell 窗口传递数据

已确认 PowerShell 提示符和英文直输时，可以在宿主把原文转为 UTF-8 Base64，再把下面生成的 ASCII 表达式作为一段按键发往窗口。表达式只校验并显示数据，适用于要把文字交给目标 shell 的任务。

```python
import base64
import hashlib

data = "中文 A+B，$".encode("utf-8")
payload = base64.b64encode(data).decode("ascii")
digest = hashlib.sha256(data).hexdigest()
line = (
    f"$b=[Convert]::FromBase64String('{payload}');"
    "$h=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($b)).Replace('-','');"
    f"if($h -eq '{digest}'){{[Text.Encoding]::UTF8.GetString($b)}}else{{throw 'SHA-256 mismatch'}}"
)
vnc_type_keys(text=line, session_id="work", connection_id="<本次 ID>")
```

任务需要运行该表达式时，再发送回车。读取本地变量、显示输出和按下回车都发生在当前 PowerShell 窗口里。

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

Wayland 调用 `wl-copy` 与 `wl-paste` 时显式使用相同的 `text/plain;charset=utf-8` MIME 类型。超时或失败会回收本次调用创建的子进程组，诊断返回固定原因标签。桌面合成器的焦点策略可能使剪贴板访问失败，失败后不会切换后端重发。

GNOME 原生 Wayland 焦点策略仍是外部限制，当前新后端尚未完成 VMware 实机验收，不能据此宣称所有 Wayland 桌面都可用。需要复用现有工具的兼容路径时，可明确设置 Xwayland `display`，选择 `clipboard_backend="x11"` 并安装 xclip/xsel；仍须在目标应用中核对粘贴效果。

Linux 来宾需要 Python 3，并安装所选 backend 对应的剪贴板工具。X11 还需正确的 display 和必要的 xauthority。Windows 来宾的 `guest_temp_dir` 应指向已登录用户可读写的临时目录。

凭据可由 `username_env` 和 `password_env` 指定的环境变量提供。在 Windows 宿主上，也可以打开“凭据管理器 → Windows 凭据 → 添加普通凭据”，填写目标名（例如 `vmware-vnc/windows-guest`）、来宾账号与密码。在对应 profile 中填写 `windows_credential_target = "vmware-vnc/windows-guest"` 并移除两个环境变量字段。只有实际准备来宾剪贴板时才读取该条凭据。加密虚拟机可另用 `vm_password_env` 指定虚拟机密码的环境变量名。

将配置放在 Windows 宿主的 `%LOCALAPPDATA%/vmware-vnc/profiles.toml`，或用 `VNC_PROFILES_FILE` 显式指定文件。重新启动 MCP 后，`vnc_profiles()` 可列出目标。显式选择 profile 后，仍须重新截图确认目标，再提交文字。准备后点击会取消旧选区；替换内容前先明确清空。按键或鼠标按钮仍被按住时，文字提交会被拒绝。

每次至多 100,000 字符，拒绝 NUL 和无效 Unicode surrogate；协议消息及解压内容各有 1 MiB 上限。`submitted_characters` / 兼容字段 `typed_characters` 保持原提交文本的长度，只记录提交长度，不证明应用已收到文本。Windows 辅助输入仍将换行规范为 CRLF，协议读回以 LF 表示。`clipboard_verified` 只证明剪贴板校验成功，`paste_sent` 只证明粘贴事件已发出，`application_verified` 固定为 false。请用新截图或应用自己的读回结果确认文本。

CLI `text` 和 `type` 都支持 `--text-file`、`--method`、`--paste-key` 与 `--focus X Y`。准备、认证、清理、超时或取消失败时不自动重放输入；剪贴板可能已变化，键盘输入也可能部分送达，先观察当前状态再决定下一步。
