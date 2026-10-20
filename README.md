# VMware VNC

用于长期维护的 VMware 通用 VNC MCP 项目。主目录：`C:\Users\Administrator\Documents\VMware_VNC`。基于固定上游源码和本地验证过的改动，使用独立 Python 环境，通过 stdio 提供 MCP 工具，也提供 CLI。

支持多台虚拟机的独立会话、原生尺寸全图/区域截图、键鼠和 Latin-1 剪贴板。不会接管宿主机的全局鼠标。

## 开发与验证

在本目录使用 PowerShell 7；维护检查要求 Python 3.11+，本机验收使用 Python 3.15.0。

```powershell
pwsh -NoProfile -File .\scripts\Setup.ps1
pwsh -NoProfile -File .\scripts\Test.ps1
.\.venv\Scripts\vnc-mcp.exe --help
```

如果 `python` 不是需要的解释器，可给 Setup 传 `-PythonExecutable` 指定完整路径。Setup 重建或复用本目录 `.venv`，按 `requirements.lock.txt` 安装固定运行依赖，再将当前源码以 editable 方式安装。源码变更不需要重新拷贝包；已运行的 MCP 进程仍需要重新加载。

Test 默认只连接临时的本机模拟 RFB 服务，不操作真实虚拟机。结果在 `checks/results/project-*.json`。性能比较所需的两个历史后端固定在 `checks/fixtures/baselines/`，测试不依赖旧工作区或临时下载目录。

## MCP 接入

`codex-mcp.toml` 提供本机配置示例，入口为 `vmware_vnc`。启动解释器、main.py 和 cwd 均指向当前项目。不指定默认目标；每次连接明确传地址和端口。配置字段按 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)核对。

示例 MCP 调用（替换各自连接返回的 ID）：

```python
vnc_connect(host="127.0.0.1", port=5901, session_id="WIN11")
vnc_screenshot(session_id="WIN11", connection_id="<WIN11 返回的 ID>")
vnc_connect(host="127.0.0.1", port=5902, session_id="WIN10")
vnc_screenshot(session_id="WIN10", connection_id="<WIN10 返回的 ID>")
vnc_sessions()
```

端口以对应虚拟机实际配置为准，同一宿主机不应重复。省略 session_id 使用独立的兼容 default 会话。具名会话除连接、状态和列表外，必须同时传 session_id 和 connection_id。重连生成新 ID，旧请求不得自动重放。

默认最多 4 个会话、每会话 16 个未完成调用（包含异步等待）。空闲 300 秒后回收，扫描间隔不超过 30 秒；活动调用和等待不会被误回收。回收会释放保持的键鼠输入。会话列表提供缓存状态，实时状态用 `vnc_status(session_id=...)`。

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| VNC_TIMEOUT | 15 秒；本机配置为 5 | 单步网络等待 |
| VNC_ACTION_TIMEOUT | 20 秒 | 协作式操作预算 |
| VNC_SCREENSHOT_MODE | refresh；本机配置为 fresh | fresh 以独立共享连接获取首帧 |
| VNC_MAX_SESSIONS | 4 | 会话数量上限，1–32 |
| VNC_IDLE_TIMEOUT | 300 秒 | 空闲回收，0 禁用 |
| VNC_HOST / VNC_PORT | 无 | 可选默认目标；当前配置不设置 |
| VNC_PASSWORD / VNC_PASSWORD_ENV | 无 / VNC_PASSWORD | 可选认证凭据，不写入版本库 |

## 只读实机验收

明确端点后运行下列命令，连接及截图测试不会发送键鼠输入：

```powershell
.\.venv\Scripts\python.exe .\checks\vnc_mcp_multisession_live.py --config "$env:USERPROFILE\.codex\config.toml" --host 127.0.0.1 --targets WIN11=5901 WIN10=5902 --output .\checks\results\project-live.json
```

CLI 单张截图示例：

```powershell
.\.venv\Scripts\vnc-mcp.exe screenshot --host 127.0.0.1 --port 5901 --screenshot-mode fresh --format jpeg --output .\checks\results\screen.jpg
```

## 项目布局

| 路径 | 用途 |
| --- | --- |
| src/vnc_mcp/ | 运行代码：MCP、会话、调度、VNC、图像与 CLI |
| main.py / pyproject.toml | 本地入口和包定义 |
| requirements.lock.txt | 固定的运行依赖 |
| scripts/ | 环境安装和回归入口 |
| checks/ | 协议、并发、生命周期及显式实机验证工具 |
| docs/ | 维护状态与迁移记录 |
| .local/history/ | 迁移前的原始报告、截图、补丁和备份，仅本机保存 |

`.venv/`、`.local/`、`checks/results/` 和构建产物均排除在 Git 外。原目录保留回退副本，后续代码维护在本项目进行。迁移结果见 [维护状态](docs/STATUS.md)，来源及本地差异见 [LOCAL-CHANGES.md](LOCAL-CHANGES.md)。

## 已知边界

VMware 端此前未接受直接发送的中文 Unicode 键盘字符，剪贴板仅支持 Latin-1。中文输入法可能转换字母输入，发送命令前应确认英文输入状态。JPEG 默认质量 80、4:4:4，不缩放；精确像素检查可用 PNG。区域截图返回原点，鼠标坐标始终是桌面原生坐标。

fresh 仍会传输完整首帧；没有用旧缓存冒充新截图。协作取消不能撤回已发送的输入，单步网络等待与编码也不能被强行抢占。多小时运行、真实网络故障和 Codex 模型 WebSocket 问题没有因本次迁移获得验证。

上游为 [thanhvn-shinkoxd/VNC-MCP](https://github.com/thanhvn-shinkoxd/VNC-MCP/tree/abe747b4dc42bc936870959861a436c849aef482)，固定提交 `abe747b4dc42bc936870959861a436c849aef482`。保留原作者署名、Apache-2.0 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
