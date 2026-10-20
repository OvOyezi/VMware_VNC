# VNC 操作参考

以下调用省略宿主提供的工具前缀，以当前聊天中 `vmware_vnc` MCP 注册的实际工具为准。真实桌面操作必须先满足 SKILL.md 的授权与目标要求。

## 连接与截图

```python
vnc_connect(host="127.0.0.1", port=5901, session_id="WIN11")
# 保存返回的 connection_id，替换后面的示例占位值。
vnc_screenshot(session_id="WIN11", connection_id="<本次返回的 ID>")
```

连接返回目标、原生尺寸和 connection_id。确认截图与指定虚拟机一致后再输入。具名会话名为 1–64 个 ASCII 字母、数字或 `._-`，首字符须为字母或数字。

截图默认 JPEG、质量 80、4:4:4、不缩放；精确像素用 `image_format="png"`。区域使用 `region={"x": 100, "y": 200, "width": 640, "height": 480}`。点击图内 `(20, 30)` 时，应使用原生坐标 `(120, 230)`；优先采用本次截图元数据中的实际原点。

## 工具参数

除连接、状态和清单外，下表每个具名会话调用还须同时携带 `session_id`、`connection_id`。

| 工具 | 主要参数 / 行为 |
| --- | --- |
| vnc_connect | host、port、session_id；重连同名会话会生成新 connection_id |
| vnc_status | session_id；等待该会话此前操作后读取实时状态 |
| vnc_sessions | 无参数；非阻塞缓存清单，不证明端点此刻仍在线 |
| vnc_screenshot | image_format、quality、region |
| vnc_click | x、y、button（left/middle/right）、count（1–3） |
| vnc_move | x、y |
| vnc_drag | x1、y1、x2、y2、button |
| vnc_scroll | x、y、direction（up/down/left/right）、amount（1–20） |
| vnc_keypress | key，例如 enter、ctrl-c、alt-tab、f5 |
| vnc_key_down / vnc_key_up | key；保持 / 释放按键 |
| vnc_mouse_down / vnc_mouse_up | x、y、button；保持 / 释放鼠标按钮 |
| vnc_type | text、delay_ms；输入到当前焦点控件 |
| vnc_clipboard_set | text；仅 Latin-1 |
| vnc_clipboard_get | 读取服务最近接收的远端剪贴板通知，可能是缓存 |
| vnc_wait | seconds，最多 60 秒；等待后须重新截图 |
| vnc_disconnect | 断开指定连接并清理保持的输入 |

同一会话按顺序执行，默认至多 16 个未完成请求，包含异步等待。不同会话可独立工作。并行操作只用于已确认身份的不同桌面，不向同一桌面并发发送依赖前一步结果的输入。

## 多桌面

```python
vnc_connect(host="127.0.0.1", port=5902, session_id="WIN10")
vnc_screenshot(session_id="WIN10", connection_id="<WIN10 本次 ID>")
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

## 验收

默认 `scripts/Test.ps1` 只操作人工 RFB 服务，不需要真实桌面授权。

如果用户已明确同意真实端点验收，先提供地址及端口，再运行连接与截图检查，例如：

```powershell
& "$skillRoot/.venv/Scripts/python.exe" "$skillRoot/checks/vnc_mcp_multisession_live.py" --config '<实际 MCP 配置文件>' --host 127.0.0.1 --targets WIN11=5901 WIN10=5902 --output "$skillRoot/checks/results/project-live.json"
```

该检查要求至少两台目标，配置条目名为 vmware_vnc；只连接、截图和断开，不发送键鼠，但仍属于真实桌面 Computer Use。不要用示例端口代替用户确认的目标。历史实机通过不能替代本次端点和当前聊天的验证。
