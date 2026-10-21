# VNC 操作参考

按画面条件等待变化、匹配或稳定时，见 [visual-waits.md](visual-waits.md)。`vnc_screenshot(remember=true)` 返回连接内参考 ID，`vnc_wait_for` 返回最终图和条件结果；普通定时 `vnc_wait` 仍可用。

以下均为本项目的 VNC 接口，调用省略宿主提供的工具前缀，以当前 `vmware_vnc` MCP 注册的实际名称为准。

## 连接与截图

```python
vnc_connect(host="127.0.0.1", port=5901, session_id="desktop-a")
# 保存返回的 connection_id，替换后面的示例占位值。
vnc_screenshot(session_id="desktop-a", connection_id="<本次返回的 ID>")
```

连接返回目标、原生尺寸和 connection_id。确认截图与指定虚拟机一致后再输入。具名会话名为 1–64 个 ASCII 字母、数字或 `._-`，首字符须为字母或数字。

需要 VNC 认证时，在 MCP 启动环境中提供 `VNC_PASSWORD_ENV` 指定的变量，默认变量名为 `VNC_PASSWORD`；单次 `vnc_connect` 也接受 `password` 参数。通过宿主的凭据机制传递真实值，不在示例、日志或报告中展开。

截图默认 JPEG、质量 80、4:4:4、不缩放；精确像素用 `image_format="png"`。区域使用 `region={"x": 100, "y": 200, "width": 640, "height": 480}`。点击图内 `(20, 30)` 时，应使用原生坐标 `(120, 230)`；优先采用本次截图元数据中的实际原点。

## 工具参数

除连接、状态和清单外，下表每个具名会话调用还须同时携带 `session_id`、`connection_id`。

| 工具 | 主要参数 / 行为 |
| --- | --- |
| vnc_connect | host、port、session_id、可选 password；或显式 profile 读取已配置端点；重连同名会话会生成新 connection_id |
| vnc_profiles | 无参数；列出配置端点，不读取凭据 |
| vnc_diagnostics | 无参数；读取 VNC 运行依赖及可选来宾剪贴板后端的发现信息 |
| vnc_status | session_id；等待该会话此前操作后读取实时状态 |
| vnc_sessions | 无参数；非阻塞缓存清单，不证明端点此刻仍在线 |
| vnc_screenshot | image_format、quality、region、remember；记住参考时返回 reference_id |
| vnc_wait_for | condition、reference_id、region、timeout、interval、stable_for、pixel_threshold、max_changed_ratio；见画面条件参考 |
| vnc_click | x、y、button（left/middle/right）、count（1–3） |
| vnc_move | x、y |
| vnc_drag | x1、y1、x2、y2、button |
| vnc_scroll | x、y、direction（up/down/left/right）、amount（1–20） |
| vnc_keypress | key，例如 enter、ctrl-c、alt-tab、f5 |
| vnc_key_down / vnc_key_up | key；保持 / 释放按键 |
| vnc_mouse_down / vnc_mouse_up | x、y、button；保持 / 释放鼠标按钮 |
| vnc_text | text、method、paste_key、focus_x/focus_y；按协商能力或绑定 profile 提交 Unicode 文本 |
| vnc_type | 与 vnc_text 相同的默认通道；delay_ms 设置逐键间隔，键盘路径至少 20 毫秒 |
| vnc_clipboard_set | text；协商成功使用 UTF-8 扩展，否则仅 Latin-1；只准备剪贴板 |
| vnc_clipboard_get | 读取服务最近接收的远端剪贴板通知，可能是缓存 |
| vnc_wait | seconds，最多 60 秒；等待后须重新截图 |
| vnc_disconnect | 断开指定连接并清理保持的输入 |

同一会话按顺序执行，默认至多 16 个未完成请求，包含异步等待。不同会话可独立工作。并行操作只用于已确认身份的不同桌面，不向同一桌面并发发送依赖前一步结果的输入。

未连接的默认会话可通过 `vnc_status()` 查询，返回 `connected=false`，不占用连接槽位。未知具名会话仍需先显式连接。

点击、拖动、滚轮及显式鼠标按下/松开会先定位指针并等待 150 毫秒，避免来宾在旧位置处理按钮事件。点击、`vnc_keypress` 和滚轮脉冲保持按下 80 毫秒，连续点击或滚轮脉冲间隔 40 毫秒。等待计入动作预算；取消或超时会清理保持的输入并断开连接，不重放动作。

拖动在按下后等待 80 毫秒，再按直线发送最多 30 个移动位置，每步间隔 20 毫秒，到达终点后等待 80 毫秒并松开。移动期间持续检查取消和超时。

`vnc_keypress` 会拒绝与当前保持按键重叠的组合键，先用 `vnc_key_up` 释放后再操作。

文字输入与来宾配置见 [text-input.md](text-input.md)。截图元数据另含 capture_seconds、transport_bytes、wire_encoding；前两者是本次客户端捕获耗时与该阶段接收的 RFB 字节数，fresh 包含短截图连接的握手，refresh 可能含同期通知。它们不代表应用响应时间或整个系统的带宽。

VNC_ENCODING / CLI `--encoding` 可选 auto、hextile、raw。auto 优先无损 Hextile，并提供 RAW。服务端决定实际编码；VMware 内置 VNC 实机仍选 RAW。当前固定依赖的 ZRLE 解码有兼容性问题，未向服务端声明支持。

## 多桌面

```python
vnc_connect(host="127.0.0.1", port=5902, session_id="desktop-b")
vnc_screenshot(session_id="desktop-b", connection_id="<desktop-b 本次 ID>")
vnc_sessions()
```

分别记录每个 session_id 对应的 host、port、connection_id。断开或重连一台时，不更换另一台的 ID。默认最多 4 个会话；空闲默认 300 秒回收，活动调用和等待不会被误回收。会话丢失后显式重连并截图，不能复用之前的输入请求。

## CLI

CLI 每次命令都会建立独立连接，结束时断开；不保留 MCP 会话。适用于单次诊断、截图或简单操作；连续交互优先 MCP。

```powershell
$skillRoot = 'C:/path/to/vmware-vnc' # 实际安装目录
$vnc = "$skillRoot/.venv/Scripts/vnc-mcp.exe"
& $vnc screenshot --host 127.0.0.1 --port 5901 --screenshot-mode fresh --format jpeg --output "$skillRoot/checks/results/screen.jpg"
& $vnc keypress --host 127.0.0.1 --port 5901 ctrl-c
& $vnc --help
```

输出目录须先创建。截图命令返回本地文件绝对路径及原生尺寸信息；读取图像核对后再继续。CLI 强制提供 host 和 port，默认从 VNC_PASSWORD 读取密码，只有显式 `--ask-password` 才提示输入；非交互自动化不要使用密码提示。

点击的两个位置参数依次为桌面原生 `x y`，例如 `& $vnc click --host $vncHost --port $vncPort $x $y --button left --count 1`。端点和坐标变量须来自已确认的目标及刚检查过的截图；不要直接使用示例端点或猜测坐标。完整参数可查 `& $vnc click --help`。

`vnc-mcp doctor` 为只读本机诊断；`vnc-mcp profiles --profiles-file <本机配置>` 列出端点。文字命令支持 `text --text-file <UTF-8 文件> --profile <名称> --profiles-file <文件> --focus X Y`，仍须显式提供 host、port。

回归分类和显式实机验收入口见 [检查工具](../checks/README.md)。
