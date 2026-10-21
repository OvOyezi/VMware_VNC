# 中文与 Unicode 文本

`vnc_text` 和默认的 `vnc_type` 共享同一套文字通道。连接并检查截图后再提交文本；每次具名调用同时携带 session_id、connection_id。

## 选择通道

| method | 行为与条件 |
| --- | --- |
| auto | 已协商 UTF-8 Extended Clipboard 时优先使用；否则使用当前连接显式绑定的 VMware profile；没有这两者时只自动发送 Latin-1 范围的 keysyms，Unicode 提前报错 |
| clipboard | 使用 VNC 剪贴板后发送粘贴快捷键；没有扩展时仅接受 Latin-1 |
| guest | 用官方 vmrun 把文字交给来宾剪贴板，校验 SHA-256、清理临时文件，再由 VNC 点击目标并粘贴 |
| keys | 显式发送键盘事件；是否支持 Unicode keysyms、当前输入法如何转换，取决于来宾与服务端 |

`vnc_status` 的 capabilities 给出当前连接是否支持 utf8_clipboard、是否绑定 guest_text，以及 preferred_encoding / wire_encoding。只安装 VMware Tools 不代表来宾认证或文字输入已可用。

## 纯 VNC 键盘输入

`method="keys"` 只发送 VNC 键盘事件，不调用来宾辅助后端。逐字发送至少间隔 20 毫秒；`delay_ms` 可增加间隔，结果中的 `effective_key_delay_ms` 返回实际采用的下限。文字过长、仅这些间隔就会耗尽动作预算时，工具在点击焦点或发送按键前拒绝请求；按应用状态分段输入。

键盘事件会经过来宾当前输入法。Windows 可为不同应用保留不同的中英文状态，桌面显示的状态不一定是切回应用后的状态。先检查目标输入框和输入法指示；英文模式下核对字母、数字及符号，中文模式下可输入拼音，再用空格或候选序号选词。这条路径无需来宾命令，但依赖已安装的输入法，不能把任意 Unicode 字符直接当作可用按键。

本轮 VMware 的原生键盘中文验证覆盖 Windows 记事本、YY 临时设置输入框，以及 Ubuntu GNOME 文本编辑器和 Firefox 设置搜索。输入已发送仍须等应用处理，再截图核对正文、候选词或光标位置。

## 标准 VNC 剪贴板

支持扩展的 VNC 服务端无需来宾账号即可提交 UTF-8 文本：

```python
vnc_text(text="中文与 emoji 😀\n第二行", focus_x=300, focus_y=200,
         session_id="linux", connection_id="<本次 ID>")
```

focus 坐标来自已检查的截图。普通文本控件默认 ctrl-v；终端常用 ctrl-shift-v 或 shift-insert，按实际应用选择 paste_key。工具还支持 meta-v。

部分 X11 服务先接收剪贴板所有权通知，等应用粘贴后才请求文字。`clipboard_offered` 表示已提交所有权通知，`clipboard_data_sent` 表示文字数据已写入 VNC 连接；工具不会等待应用确认。`vnc_clipboard_get` 返回最近接收的通知缓存，不能当作来宾的实时查询接口。

## 可选来宾剪贴板后端

此后端通过官方 vmrun 准备并校验目标来宾的剪贴板，随后由 VNC 点击并粘贴。它只服务文本输入。标准 UTF-8 剪贴板通道只依赖 VNC 服务的协商能力。

复制 [profiles.toml.example](../assets/profiles.toml.example) 到 `.local/profiles.toml`，按实际机器填写：

- host、port 是 VNC 端点，vmx 是该来宾的 VMX 路径；相对 VMX 路径按配置文件目录解析。选择 profile 后端点必须完全匹配，避免把文字准备到另一台来宾。
- Windows 的 guest_temp_dir 使用已登录用户可读写的临时目录。系统服务创建的 `C:/Windows/Temp` 文件可能无法被交互桌面读取。
- username_env、password_env、可选 vm_password_env 只填写环境变量名；真实值由启动 MCP/CLI 的本机环境提供。vmrun 可由 profile 的 vmrun 或 VNC_VMRUN_PATH 显式指定，否则查找官方安装位置。
- Linux 需要来宾 Python 3，并已有 xclip/xsel，或 wl-copy 与 wl-paste。X11 配置实际 display 和必要的 xauthority；Wayland 只使用当前账号拥有的运行时目录，发现多个 socket 时不猜测目标。临时目录应为该账号私有可写目录。

GNOME 的 Xwayland 认证文件可能随登录变化。显式指定 `display` 且没有 `xauthority` 时，辅助程序仅从当前账号拥有的 `/run/user/<uid>/` 中选择唯一、同属该账号的 `.mutter-Xwaylandauth.*` 普通文件；多个候选会报错。明确提供的 `xauthority` 优先。

Wayland 通道是否可用还取决于桌面会话。若 `wl-copy` / `wl-paste` 超时，先检查当前状态；可以明确配置 Xwayland 的 `display` 并安装 xclip/xsel，再单独验收应用粘贴。失败后不会自动换通道或重发文字。

服务启动时设置 VNC_PROFILES_FILE 指向本机配置。`vnc_profiles()` 只返回端点与通道信息。显式选择后连接：

```python
vnc_connect(profile="windows-guest", session_id="work")
vnc_screenshot(session_id="work", connection_id="<本次 ID>")
vnc_text(text="中文输入验证", focus_x=500, focus_y=300,
         session_id="work", connection_id="<本次 ID>")
```

辅助进程会覆盖来宾剪贴板。Windows 使用系统自带 PowerShell 5.1 的 STA 线程，ExecutionPolicy 只作用于本次进程；Linux 通过明确的显示会话访问剪贴板。文字以 UTF-8 JSON 文件传递，不拼进待执行的 shell 命令。vmrun 的认证参数在子进程命令行中短暂存在，受宿主本地进程权限保护。

必须提供 focus_x/focus_y：部分 Windows 辅助进程会影响焦点，准备完成后会点击该点，等待 150 毫秒让鼠标与键盘设备事件顺序稳定，再发送粘贴。点击会取消旧选区；要替换原内容，先明确清空，再提交。持有按键或鼠标按钮时禁止自动文本提交。

## 验收与失败

每次至多 100,000 字符，拒绝 NUL 和无效 Unicode surrogate；协议消息及解压内容各有 1 MiB 上限。Windows 辅助输入将换行规范为 CRLF；协议读回以 LF 表示。

结果中的 submitted_characters / 兼容字段 typed_characters 是提交数量。clipboard_verified 只证明来宾剪贴板校验成功，paste_sent 只证明已发粘贴事件；application_verified 固定为 false。用新截图或应用自己的读回结果确认实际文本。

Linux 成功结果可包含 `clipboard_backend`（wayland、xclip 或 xsel）。准备失败时只报告固定的阶段、后端和错误类型，不回显辅助命令、文字或凭据。

准备、认证、清理、超时或取消失败时，不自动重放输入。剪贴板可能已经变化，输入也可能已部分送达；先核对当前状态，再决定下一步。未配置可靠 Unicode 通道时，不尝试盲切中文输入法。

CLI 支持相同的 `text` / `type`、`--profile`、`--profiles-file`、`--focus X Y`、`--method`、`--paste-key`；`text --text-file file.txt` 从 UTF-8 文件读取。需要逐键延迟时使用 `type --delay-ms`，该方式属于键盘事件路径。
