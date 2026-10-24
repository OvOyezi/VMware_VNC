# VMware VNC

**让 AI 看见并操作 VMware 虚拟机。**

[![checks](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml/badge.svg)](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml)

通过 MCP，把虚拟机桌面交给 AI 编程助手：查看真实画面、操作应用、等待界面变化，再核对执行结果。适合需要图形界面的软件操作、界面检查和多虚拟机任务。

核心工作方式很直接：**观察画面 → 执行操作 → 等待变化 → 核对结果**。AI 根据每一步的新画面继续工作；VMware VNC 提供截图、输入、画面条件和会话管理工具。

[快速开始](#快速开始) · [使用示例](#开始使用) · [接口参考](references/operations.md) · [支持范围](docs/COMPATIBILITY.md)

## 能帮你做什么

- 操作只有图形界面的应用，打开菜单、调整设置、拖动窗口，并检查最终状态。
- 检查软件界面和操作结果，比较画面差异，定位像素或局部细节。
- 等待加载结束、区域发生变化或画面稳定，让 AI 根据观察结果继续操作。
- 同时处理多台虚拟机，为每个桌面保留独立连接、操作队列和画面参考。

## 为 AI 连续操作而设计

| 能力 | 使用价值 |
| --- | --- |
| 原生尺寸、无损 PNG | 保留桌面像素与坐标；支持区域截图、精确 RGB 和局部细节检查 |
| 键鼠与文本输入 | 支持点击、拖动、滚动、组合键，以及按通道条件选择的文字输入 |
| 画面条件等待 | 等待变化、参考图匹配或持续稳定，并返回最后有效截图 |
| 独立会话 | 用会话名与连接 ID 区分桌面和重连，拒绝过期连接上的操作 |
| 明确的失败状态 | 报告显示不可用、超时和取消；失败输入不自动重放 |
| 标准 MCP 接口 | 可接入支持 MCP stdio、工具调用和图片结果的 AI 助手 |

基础截图与键鼠直接连接 VMware 的 VNC 端点，不需要来宾系统账号、VMware Tools 或外部控制 CLI。另有持续维护的 `vmware-vnc` CLI，方便单次截图、诊断和脚本集成。

## 快速开始

### 1. 准备环境

安装脚本面向 **Windows**，需要 **Python 3.11+、PowerShell 7 和 Git**。准备一台已开启 VNC、可访问的 VMware 虚拟机，记下地址、端口和 VNC 密码。

连接地址是运行 VMware 的宿主机地址。本机运行时通常使用 `127.0.0.1`；同一宿主机上的多台虚拟机应使用不同 VNC 端口。

### 2. 安装

```powershell
git clone https://github.com/OvOyezi/VMware_VNC.git
cd VMware_VNC
pwsh -NoProfile -File ./scripts/Setup.ps1
```

安装脚本创建 `.venv` 并安装依赖。需要选择 Python 解释器时，可为 Setup 传入 `-PythonExecutable`。

### 3. 接入 AI 助手

生成当前安装位置对应的 MCP 进程参数：

```powershell
pwsh -NoProfile -File ./scripts/Get-McpConfig.ps1 -Format StdioJson
```

将输出的 `command`、`args`、`cwd` 填入 AI 助手的 MCP stdio 服务配置，服务名可设为 `vmware_vnc`。需要认证时，在该服务的环境变量中设置 `VNC_PASSWORD`。保存后重新加载 MCP 服务，即可发现 `vnc_*` 工具。

不同助手的配置格式各异，具体步骤及 Codex 示例见 [安装与 MCP 接入](references/setup.md)。Codex 是可选接入方式；MCP 与 CLI 均可独立使用，技能安装也是可选项。

## 开始使用

接入后，向 AI 提供明确的端点和任务。例如：

> 连接 Windows 虚拟机的 VNC：127.0.0.1:5901。先截图确认桌面，打开系统设置，查看当前显示分辨率，把结果和最终截图发给我，完成后断开连接。

AI 可以通过以下工具完成一个观察循环。这里是工具调用示意，地址与连接 ID 按实际返回值替换：

```python
vnc_connect(host="127.0.0.1", port=5901, session_id="work")
vnc_screenshot(session_id="work", connection_id="<连接返回的 ID>")
# 根据截图选择输入动作；等待画面稳定后检查结果。
vnc_wait_for(condition="stable", timeout=10, stable_for=0.5,
             session_id="work", connection_id="<同一个 ID>")
vnc_disconnect(session_id="work", connection_id="<同一个 ID>")
```

完整工具、坐标用法和多桌面示例见 [接口参考](references/operations.md)；AI 操作流程见 [SKILL.md](SKILL.md)。

### CLI 辅助入口

单独保存一张截图，或检查本机运行环境：

```powershell
./.venv/Scripts/vmware-vnc.exe screenshot --host 127.0.0.1 --port 5901 --output screenshot.png
./.venv/Scripts/vmware-vnc.exe doctor
```

CLI 每次命令独立连接并在结束后断开。连续多步任务使用 MCP，可以保留会话和画面参考；两种入口共享同一套核心能力。

## 使用前了解

- 当前主要面向 VMware 虚拟机，其他平台与 VNC 服务端的正式支持待定。基础操作要求 VNC 认证通过且有可用画面。
- 中文等文字输入取决于服务端剪贴板能力或可选来宾通道；逐键输入需要显式选择，并受来宾输入法影响。配置见 [文本输入](references/text-input.md)。
- 普通 TCP 和 `ws://` 不加密。跨网络使用时，可选择验证证书的 TLS／WSS 或已有加密通道。

更多环境要求见 [支持范围](docs/COMPATIBILITY.md)，连接和显示问题见 [故障排查](references/troubleshooting.md)。

## 文档与参与

[安装与接入](references/setup.md) · [接口参考](references/operations.md) · [画面条件](references/visual-waits.md) · [版本记录](CHANGELOG.md) · [贡献指南](CONTRIBUTING.md) · [反馈问题](https://github.com/OvOyezi/VMware_VNC/issues)

采用 [Apache-2.0](LICENSE) 许可证。
