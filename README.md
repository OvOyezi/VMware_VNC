# VMware_VNC

[![checks](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml/badge.svg)](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml)

为虚拟机提供 VNC 画面观察与桌面交互的 Codex skill，附带 MCP 服务和 `vnc-mcp` CLI。来宾可以是 Windows、Linux 或其他提供 VNC 桌面的系统。

建议将 VMware_VNC 与官方 `vmrun`，或其他可操作虚拟机的 CLI 工具搭配使用：命令行处理适合程序化的步骤，VMware_VNC 补充画面观察、点击、按键和界面结果核对。大部分日常桌面操作也可以仅靠 VMware_VNC 完成。

## 能力

- 截取完整桌面或指定区域，保留原生尺寸与坐标，支持 PNG、JPEG。
- 点击、拖动、滚动、组合键和文本输入；多个桌面分别维护连接与操作队列。
- 输入中文、表情及多行文字。使用 VNC UTF-8 Extended Clipboard，或显式配置的来宾剪贴板辅助通道。
- 等待区域变化、匹配参考图或持续稳定，返回条件结果及最后有效截图。
- 提供连接诊断、超时、取消与空闲回收；失败或取消的输入不自动重放。

当前版本为 `0.3.1`，采用 Apache-2.0 许可证。MCP 和本项目 CLI 暴露同一套 VNC 能力；操作方法见 [SKILL.md](SKILL.md)及 [接口参考](references/operations.md)。

## 安装

宿主需要 Windows、PowerShell 7 和 Python 3.11+；目标需要可达的 VNC 服务。仅使用可选的 VMware 来宾剪贴板通道时，额外需要 `vmrun`、VMware Tools 和对应来宾凭据，见 [文本输入](references/text-input.md)。

向 Codex 提出以下请求：

```text
使用 $skill-installer 安装 https://github.com/OvOyezi/VMware_VNC
选择仓库根目录 .，技能名称为 vmware-vnc。
```

对应参数为 `--repo OvOyezi/VMware_VNC --path . --name vmware-vnc`。安装后在实际技能目录运行：

```powershell
$skillRoot = "$env:USERPROFILE/.codex/skills/vmware-vnc" # 按实际安装位置调整
pwsh -NoProfile -File "$skillRoot/scripts/Setup.ps1"
pwsh -NoProfile -File "$skillRoot/scripts/Test.ps1"
pwsh -NoProfile -File "$skillRoot/scripts/Get-McpConfig.ps1"
```

Setup 创建 `.venv` 并安装固定依赖；Test 只连接本机临时人工 RFB 服务。Get-McpConfig 生成实际安装位置对应的 TOML，将其合并到所用 Codex 配置后重新加载 MCP。安装与配置生成不会自动修改全局配置，也不会连接虚拟机。

技能发现目录、手动克隆及 MCP 核验步骤见 [安装与接入](references/setup.md)。

## 使用

在 Codex 中调用 `$vmware-vnc`，说明目标端点和需要完成的界面操作。以下地址和端口仅为示例，使用时替换为实际目标：

```python
vnc_connect(host="127.0.0.1", port=5901, session_id="work")
vnc_screenshot(session_id="work", connection_id="<连接返回的 ID>")
# 检查截图后调用需要的输入或画面等待工具。
vnc_disconnect(session_id="work", connection_id="<同一个 ID>")
```

连接后先检查画面再输入。具名会话的截图、输入、等待和断开同时传入两个 ID；重连后使用新连接 ID。区域图内坐标要加截图原点。

中文通道取决于服务端能力；安装技能不会自动创建本机 profile 或凭据。画面条件满足、剪贴板校验成功及已发送输入，都须结合应用结果核对。详见 [文本输入](references/text-input.md)、[画面条件](references/visual-waits.md)及 [故障排查](references/troubleshooting.md)。

## 验证与限制

默认回归覆盖人工 RFB 协议、会话隔离、取消、文本和画面条件；GitHub Actions 在 Windows / Python 3.11、3.15 矩阵执行。测试分类与实机入口见 [checks/README.md](checks/README.md)。

已有实机覆盖包括 VMware Windows 桌面和 Linux / TigerVNC X11 的部分文字通道。各项覆盖不能互相替代，也不能保证受保护画面可见。服务端可能返回比请求区域更大的画面，图像稳定也不等于应用任务完成。具体范围见 [兼容性](docs/COMPATIBILITY.md)。

## 维护

运行源码只有 `src/vnc_mcp/` 一份，仓库根目录同时是 skill 安装入口。维护步骤见 [CONTRIBUTING.md](CONTRIBUTING.md)，上游来源见 [LOCAL-CHANGES.md](LOCAL-CHANGES.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

`.venv/`、`.local/`、`checks/results/`、索引和构建产物不加入 Git。维护仓库、GitHub 与安装副本同步发布文件，本机 profile、凭据和环境由各自机器管理。
