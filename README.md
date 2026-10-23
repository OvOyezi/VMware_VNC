# VMware_VNC

[![checks](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml/badge.svg)](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml)

让 AI 更可靠地可视化接管虚拟机。通过 VNC 获取画面、执行键鼠输入并核对界面结果，以 MCP 提供连续交互能力，同时维护 `vmware-vnc` CLI 作为单次操作、诊断和脚本集成的辅助入口。

当前主要面向 VMware 虚拟机。基础能力直接连接 VNC 端点，不要求特定来宾 OS、来宾账号、VMware Tools 或外部控制 CLI；需要满足 VNC 自身认证和有效画面条件。可按任务需要搭配官方 `vmrun` 或其他工具。其他虚拟化平台与 VNC 服务端的正式支持范围待定。

本项目使用 Codex 辅助开发。MCP 接口独立于 Codex，其他支持 MCP stdio、工具调用和图片结果的 AI 编程助手也可接入；具体宿主兼容性须分别验证。仓库根目录同时提供可选的 `vmware-vnc` skill。

## 能力

- 默认以 PNG 截取完整桌面或指定区域，保留原生尺寸与坐标；需要时可显式选择 JPEG。
- 用 `vnc_inspect` 读取指定像素 RGB、比较参考画面，并生成可映射回原生坐标的细节图。
- 点击、拖动、滚动、组合键和文本输入；多个桌面分别维护连接与操作队列。
- 自动文本只使用已协商的 VNC UTF-8 Extended Clipboard 或显式绑定的来宾 profile；两者都不可用时，即使 ASCII 也会在点击焦点或发送输入前拒绝。检查输入法后可显式选择逐键 `method="keys"`，但结果受输入法影响且不会验证应用文本。
- 连接保持但没有完整帧缓冲时，`connected` 仍可为 true、`display_state` 为 `unavailable`；未完成的初始画面不会返回。可显式调用 `vnc_wake` 或 CLI `wake` 发送两次指针移动，再请求新截图；这不会点击、按键或解锁，也不能保证恢复被强制关闭的来宾显示。
- 等待区域变化、匹配参考图或持续稳定，返回条件结果及最后有效截图。
- 提供连接诊断、超时、取消与空闲回收；最后一条连接关闭后释放 RFB I/O 线程，失败或取消的输入不自动重放。
- 直接 TCP 与可选 TLS／WebSocket／WSS 传输；分段首帧和分辨率变化只在像素完整后发布，组合键按物理按键检查冲突。

项目独立开发和维护。当前开发版本为 `0.5.0`，采用 Apache-2.0 许可证，使用项目自有的 asyncio RFB 传输与解码实现。支持无损 RFB Raw、CopyRect、Hextile、ZRLE 和 Tight 子集；不接受 Tight JPEG 矩形。PNG 的 RGB8 表示解码后的服务端帧缓冲像素，不承诺显示器 HDR 或色彩配置文件。操作流程见 [SKILL.md](SKILL.md)，参数见 [接口参考](references/operations.md)。

## 安装

现有 `scripts/` 维护脚本面向 Windows，要求 PowerShell 7；Python 运行时要求 3.11+。在选定目录克隆仓库并准备环境：

```powershell
git clone https://github.com/OvOyezi/VMware_VNC.git vmware-vnc
cd vmware-vnc
pwsh -NoProfile -File ./scripts/Setup.ps1
pwsh -NoProfile -File ./scripts/Test.ps1
```

Setup 创建 `.venv` 并安装固定依赖；Test 只连接本机临时人工 RFB 服务。MCP 宿主用该环境的 Python 启动 `main.py mcp`，保持 stdio 进程以保留连接和会话；CLI 安装在 `.venv/Scripts/vmware-vnc.exe`。

通用 MCP 进程参数、Codex 配置示例及可选 skill 安装见 [安装与接入](references/setup.md)。安装和配置生成不会自动修改宿主配置或连接虚拟机。只有可选的 VMware 来宾剪贴板通道额外需要 `vmrun`、Tools 和来宾凭据，见 [文本输入](references/text-input.md)。

## 使用

向已接入 MCP 的 AI 助手提供明确的 VNC 端点和任务。连接、观察、操作、核对结果，再断开；以下为工具调用顺序示意，端点须替换为实际目标：

```python
vnc_connect(host="127.0.0.1", port=5901, session_id="work")
vnc_screenshot(session_id="work", connection_id="<连接返回的 ID>")
# 检查截图后调用需要的输入或画面等待工具。
vnc_disconnect(session_id="work", connection_id="<同一个 ID>")
```

通过代理连接时，也可使用 `vnc_connect(url="ws://127.0.0.1:6080/websockify", session_id="work")`；URL 与 host／port／profile 互斥。TLS 和配置细节见 [安装与接入](references/setup.md)。

连接后先检查画面再输入。具名会话的截图、输入、等待和断开同时传入两个 ID；重连后使用新连接 ID。区域图内坐标要加截图原点。

CLI 每次建立独立连接，命令结束即断开；连续多步任务优先使用 MCP。两种入口复用同一套 VNC 核心，CLI 的参数和使用方法见 [接口参考](references/operations.md)。

中文通道取决于服务端能力；安装不会自动创建 profile 或凭据。画面条件满足、剪贴板校验成功及已发送输入，都须结合应用结果核对。详见 [文本输入](references/text-input.md)、[画面条件](references/visual-waits.md)及 [故障排查](references/troubleshooting.md)。

## 验证与限制

`scripts/Test.ps1` 默认运行本机人工 RFB 服务或 mock 上的合成检查，覆盖 MCP stdio、CLI、传输、会话隔离、取消、文本和画面条件，不连接真实虚拟机；GitHub Actions 配置了 Windows / Python 3.11、3.15 检查矩阵。检查分类与显式实机入口见 [checks/README.md](checks/README.md)。

兼容性页记录了 0.5.0 的 wire、独立 wheel、websockify、Windows 与 Ubuntu 实测范围，以及尚未验证的 Wayland 剪贴板成功路径。GitHub Actions 配置了 Windows / Python 3.11、3.15 检查和 Ubuntu / Python 3.11 helper job；Ubuntu job 尚无远端运行验收记录。各类 AI 宿主、其他服务端或长期运行的完整兼容验收仍需分别完成。启动与安装画面也须按具体端点单独验证。

普通 TCP 和 `ws://` 不加密；需要加密时可选择验证证书的 `vnc+tls://` 或 `wss://`，也可使用环境已有的加密通道。画面可见性、输入法、剪贴板及应用响应仍由目标环境决定。

## 维护

项目规范见 [AGENTS.md](AGENTS.md)，设计取舍、noVNC / TigerVNC / KasmVNC / websockify 对照与后续规划见 [设计说明](docs/DESIGN.md)。贡献步骤见 [CONTRIBUTING.md](CONTRIBUTING.md)，许可及必要署名见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

运行源码集中在 `src/vmware_vnc/`。`.venv/`、`.local/`、`checks/results/`、索引和构建产物不加入 Git；真实凭据和机器配置由各自环境管理。
