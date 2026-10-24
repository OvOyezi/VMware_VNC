# VNC 操作参考

按画面条件等待变化、匹配或稳定时，见 [visual-waits.md](visual-waits.md)。`vnc_screenshot(remember=true)` 返回连接内参考 ID，`vnc_wait_for` 返回最终图和条件结果；`vnc_inspect` 可精确读取像素和比较画面；普通定时 `vnc_wait` 仍可用。

以下均为本项目的 VNC 接口，调用省略宿主提供的工具前缀，以当前 `vmware_vnc` MCP 注册的实际名称为准。

## 连接与截图

```python
vnc_connect(host="127.0.0.1", port=5901, session_id="desktop-a")
# 保存返回的 connection_id，替换后面的示例占位值。
vnc_screenshot(session_id="desktop-a", connection_id="<本次返回的 ID>")
```

连接返回目标、原生尺寸和 connection_id。确认截图与指定虚拟机一致后再输入。具名会话名为 1–64 个 ASCII 字母、数字或 `._-`，首字符须为字母或数字。

MCP 当前注册 24 个工具。若 ServerInit 为 0×0，或认证连接在有界等待内仍没有完整的初始帧，连接可保持 `connected=true`，状态为 `display_state="unavailable"`。不完整画面不会作为截图暴露；这表示控制连接仍在，但没有可用帧缓冲。

需要 VNC 认证时，在 MCP 启动环境中提供 `VNC_PASSWORD_ENV` 指定的变量，默认变量名为 `VNC_PASSWORD`；单次 `vnc_connect` 也接受 `password` 参数。通过宿主的凭据机制传递真实值，不在示例、日志或报告中展开。

截图默认使用 PNG，保留原生尺寸和 RGB8 帧缓冲像素；JPEG 仅在显式指定 `image_format="jpeg"` 时使用，质量参数范围为 1–95。区域示例：`region={"x": 100, "y": 200, "width": 640, "height": 480}`。点击图内 `(20, 30)` 时，应使用原生坐标 `(120, 230)`；优先采用本次截图元数据中的实际原点。RGB8 是服务端提供的像素值，不保证显示器 HDR 或色彩配置文件。MCP 图片携带 `_meta.codex/imageDetail="original"` 客户端提示，不代表后续模型链路一定保留原始细节。

MCP 会话默认 `screenshot_mode="auto"`：先在当前连接上做一次有界完整刷新；如果服务端未按时返回完整帧，则通过共享的短暂连接获取初始完整帧。`refresh` 和 `fresh` 仍可显式选择。截图只返回完整捕获帧，永不返回部分画面或缓存旧图。捕获超时会清除最近 frame/reference，状态变为 `unavailable`，并报告无新帧、未返回缓存图像；只有后续完整截图成功才恢复为 `ready`。若连接仍在，可显式调用 `vnc_wake` 后重新截图。

## 工具参数

除连接、状态和清单外，下表每个具名会话调用还须同时携带 `session_id`、`connection_id`。

| 工具 | 主要参数 / 行为 |
| --- | --- |
| vnc_connect | host、port、session_id、可选 password／profile；或单独 url（vnc／vnc+tls／ws／wss）；重连同名会话会生成新 connection_id |
| vnc_profiles | 无参数；列出配置端点，不读取凭据 |
| vnc_diagnostics | 无参数；读取 VNC 运行依赖及可选来宾剪贴板后端的发现信息 |
| vnc_status | session_id；等待该会话此前操作后读取实时状态 |
| vnc_sessions | 无参数；非阻塞缓存清单，不证明端点此刻仍在线 |
| vnc_screenshot | image_format、quality、region、remember；记住参考时返回 reference_id |
| vnc_inspect | region、reference_id、points、detail_region、zoom、pixel_threshold、frame_id；返回原色 PNG、RGB 采样及可选细节/差异图 |
| vnc_wait_for | condition、reference_id、region、timeout、interval、stable_for、pixel_threshold、max_changed_ratio；见画面条件参考 |
| vnc_click | x、y、button（left/middle/right）、count（1–3） |
| vnc_move | x、y |
| vnc_drag | x1、y1、x2、y2、button |
| vnc_scroll | x、y、direction（up/down/left/right）、amount（1–20） |
| vnc_keypress | key，例如 enter、ctrl-c、alt-tab、f5 |
| vnc_key_down / vnc_key_up | key；保持 / 释放按键 |
| vnc_mouse_down / vnc_mouse_up | x、y、button；保持 / 释放鼠标按钮 |
| vnc_text | text、method、paste_key、focus_x/focus_y；auto 只使用已协商 UTF-8 剪贴板或绑定 profile，否则发送前拒绝 |
| vnc_type | 与 vnc_text 相同；`delay_ms` 仅可与显式 `method="keys"` 一起使用，逐键最小间隔为 20 毫秒 |
| vnc_clipboard_set | text；协商成功使用 UTF-8 扩展，否则仅 Latin-1；只准备剪贴板 |
| vnc_clipboard_get | 读取服务最近接收的远端剪贴板通知，可能是缓存 |
| vnc_wake | 无参数；只发送两次指针移动，不点击、不按键、不解锁；之后须重新截图 |
| vnc_wait | seconds，最多 60 秒；等待后须重新截图 |
| vnc_disconnect | 断开指定连接并清理保持的输入 |

同一会话按顺序执行，默认至多 16 个未完成请求，包含异步等待。不同会话可独立工作。并行操作只用于已确认身份的不同桌面，不向同一桌面并发发送依赖前一步结果的输入。

未连接的默认会话可通过 `vnc_status()` 查询，返回 `connected=false`，不占用连接槽位。未知具名会话仍需先显式连接。

点击、拖动、滚轮及显式鼠标按下/松开会先定位指针并等待 150 毫秒，避免来宾在旧位置处理按钮事件。点击、`vnc_keypress` 和滚轮脉冲保持按下 80 毫秒，连续点击或滚轮脉冲间隔 40 毫秒。等待计入动作预算；取消或超时会清理保持的输入并断开连接，不重放动作。

`vnc_wake(session_id, connection_id)` 只对仍连接的桌面发送两次 pointer motion；保持了按键或鼠标按钮时会拒绝。结果中的 `wake_sent=true` 只表示移动事件已发送。wake 不会点击、输入或解锁，随后须重新截图确认桌面；没有有效帧缓冲时不保证能唤醒。若强制关闭了来宾显示或开启不可由鼠标活动恢复的省电状态，VNC motion 可能无效，`display_state` 会继续为 `unavailable`。

收到 0×0 `DesktopSize` 后，当前画面状态失效，截图不会返回先前帧来冒充当前画面。非零分辨率变化期间，已发布的旧 snapshot 继续保留原尺寸，直到新尺寸的完整画面到达；请以新截图确认坐标。初始 ServerInit 尺寸非零也不代表已有可用帧，只有完整首帧发布后状态才为 `ready`。

拖动在按下后等待 80 毫秒，再按直线发送最多 30 个移动位置，每步间隔 20 毫秒，到达终点后等待 80 毫秒并松开。移动期间持续检查取消和超时。

`vnc_keypress` 和 `vnc_key_down` 按 keysym 拒绝与已保持按键重叠的组合键。Ctrl/Alt/Super 组合中的 ASCII 字母大小写只是快捷键记法，例如 `Ctrl+A` 等同 `ctrl+a`，不会隐含 Shift；需要 Shift 时显式写 `ctrl+shift+a`。独立 `A` 仍表示 Shift+A，标点的既有映射不变。`vnc_key_up` 必须释放本会话按下的完整组合键；别名与修饰键顺序会规范化，不允许用单独 Ctrl 提前释放已保持的 Ctrl+A。分辨率变化后须重新截图，再使用新画面坐标。

文字输入与来宾配置见 [text-input.md](text-input.md)。截图元数据包括 `capture_seconds`、`transport_bytes`、`wire_encoding`、`frame_id`、`frame_received_monotonic`、`updated_regions`、`capture_source`、`lossless` 和 `source_rgb_sha256`。`frame_received_monotonic` 来自单调时钟，只适合本进程计算帧龄。`source_rgb_sha256` 是将图像区域宽高按大端序打包后与 RGB 字节拼接计算的 SHA-256；它描述编码前的源像素，JPEG 输出仍是有损的。`capture_seconds` 是本次客户端捕获耗时，`transport_bytes` 是该阶段接收的 RFB 字节数；fresh 包含短截图连接握手，refresh 可能含同期通知。它们不代表应用响应时间或整个系统的带宽。

RFB 传输由本项目的 asyncio 实现管理，运行依赖诊断报告 `mcp`、Pillow、cryptography、websockets 及传输引擎状态（I/O 线程与连接 lease 数）。`VNC_ENCODING` / CLI `--encoding` 可选 `auto`、`raw`、`hextile`、`zrle`、`tight`。`auto` 声明支持无损 ZRLE、Tight、Hextile、CopyRect 和 RAW；显式编码偏好也保留 RAW 回退。Tight 只接受无损子集，遇到 Tight JPEG 矩形会拒绝该帧。

## 画面检查

`vnc_inspect` 接受可选 `region`、`reference_id`、`points=[{"x": 120, "y": 230}]`、`detail_region`、`zoom`、`pixel_threshold` 与 `frame_id`。坐标均为桌面原生坐标。它始终返回当前原色 PNG；指定参考图时还会返回参考比较、逐点 RGB 和 delta，以及可选的 `detail_current`、`detail_reference`、`difference_amplified`。差异图把每通道绝对差乘以 16、最高截到 255，并放大显示，因此颜色是人为增强的；查看原像素请使用 current PNG 或精确 RGB 数值。

`zoom` 默认为 4，范围 1–8；显式细节区域每边最多 512 像素，放大结果最多 4,194,304 像素。若参考比较发现变化且未指定细节区域，会围绕变化边界加 8 像素留白并将每边限制为 256 像素。`frame_id` 可复用最近捕获帧，不发新的 RFB 请求；仅保留每个连接的最新帧，新捕获或重连会使旧 ID 失效。`reference_id` 生命周期不变，仍由最近一次 `remember=true` 截图持有。

## 多桌面

```python
vnc_connect(host="127.0.0.1", port=5902, session_id="desktop-b")
vnc_screenshot(session_id="desktop-b", connection_id="<desktop-b 本次 ID>")
vnc_sessions()
```

分别记录每个 session_id 对应的 host、port、connection_id。断开或重连一台时，不更换另一台的 ID。默认最多 4 个会话；空闲默认 300 秒回收，活动调用和等待不会被误回收。会话丢失后显式重连并截图，不能复用之前的输入请求。

## CLI

CLI 是持续维护的辅助入口，与 MCP 复用同一套 VNC 核心。每次命令都会建立独立连接，结束时断开，不保留 MCP 会话；适用于单次操作、诊断和脚本集成，连续交互优先 MCP。基础 VNC 操作不依赖外部控制 CLI。

```powershell
$projectRoot = 'C:/path/to/vmware-vnc' # 实际安装目录
$vnc = "$projectRoot/.venv/Scripts/vmware-vnc.exe"
& $vnc screenshot --host 127.0.0.1 --port 5901 --screenshot-mode fresh --format jpeg --output "$projectRoot/checks/results/screen.jpg"
& $vnc inspect --host 127.0.0.1 --port 5901 --point 120 230 --detail-region 100 180 120 80 --zoom 4 --pixel-threshold 0 --output-dir "$projectRoot/checks/results/inspect"
& $vnc keypress --host 127.0.0.1 --port 5901 ctrl-c
& $vnc --help
```

输出目录须先创建。截图命令返回本地文件绝对路径及原生尺寸信息；读取图像核对后再继续。CLI 可显式提供 host 和 port、选择 profile、使用 --url，或使用已配置的默认端点；默认从 VNC_PASSWORD 读取密码，只有显式 `--ask-password` 才提示输入；非交互自动化不要使用密码提示。

点击的两个位置参数依次为桌面原生 `x y`，例如 `& $vnc click --host $vncHost --port $vncPort $x $y --button left --count 1`。端点和坐标变量须来自已确认的目标及刚检查过的截图；不要直接使用示例端点或猜测坐标。完整参数可查 `& $vnc click --help`。

`vmware-vnc doctor` 为只读本机诊断；`vmware-vnc profiles --profiles-file <本机配置>` 列出端点。文字命令支持 `text --text-file <UTF-8 文件> --profile <名称> --profiles-file <文件> --focus X Y`；未显式提供 host、port 时使用该 profile 的绑定端点。

`inspect` 支持重复的 `--point X Y`、`--detail-region X Y WIDTH HEIGHT`、`--zoom`、`--pixel-threshold`、`--reference-file` 和 `--output-dir`；单命令 CLI 连接不能复用 MCP 的 `frame_id`。该功能不依赖 OCR 或模型。

回归分类和显式实机验收入口见 [检查工具](../checks/README.md)。

CLI 提供 `wake` 子命令；例如 `& $vnc wake --host 127.0.0.1 --port 5901` 显式发送 wake 移动后，再运行 `screenshot`。它只报告动作是否已发送，不保证解锁或显示恢复。`type --delay-ms` 必须同时指定 `--method keys`；查看目标输入法后才使用键盘路径。

CLI 的 `--url` 与 `--host`／`--port`／`--profile` 互斥；原生 host、port 必须成对提供。连接选项 `--tls-ca-file`、`--timeout`、`--action-timeout` 分别控制 CA 信任、单步网络等待及操作预算。URL 路径和查询参数不写入公开状态，TLS 配置详见 [安装与接入](setup.md)。
