# VMware_VNC

[![checks](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml/badge.svg)](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml)

从 Windows 宿主机操作虚拟机桌面的 Codex skill，附带 VNC MCP 服务和 CLI。来宾可以是 Windows、Linux 或其他提供 VNC 的系统。VNC 负责截图和键鼠交互，VMware 官方 `vmrun` 负责电源、文件和来宾命令。

仓库根目录就是 `vmware-vnc` 技能，运行源码只有 `src/vnc_mcp/` 一份。当前版本为 `0.3.0`，采用 Apache-2.0 许可证。

## 功能

- 截取完整桌面或指定区域，保留原生尺寸和坐标；支持 PNG、JPEG。
- 点击、拖动、滚动、组合键和文本输入；多个虚拟机会话各自维护连接与队列。
- 按区域等待画面变化、匹配参考图或持续稳定，返回条件结果及最后一张有效截图。
- 通过标准 UTF-8 Extended Clipboard 输入中文、表情和多行文本；VMware 内置 VNC 可使用显式配置的 `vmrun` 来宾辅助通道。
- 提供连接诊断、操作预算、取消和空闲回收；失败或取消的输入不会自动重放。

宿主需要 Windows、PowerShell 7 和 Python 3.11+。管理 VMware 虚拟机时还需要官方 `vmrun`。来宾辅助通道的配置和兼容范围见 [中文输入](references/text-input.md)与 [验收记录](docs/COMPATIBILITY.md)。

## 安装

### 通过 Codex 安装 skill

向 Codex 提出以下请求：

```text
使用 $skill-installer 安装 https://github.com/OvOyezi/VMware_VNC
选择仓库根目录 .，技能名称为 vmware-vnc。
```

对应 installer 参数为 `--repo OvOyezi/VMware_VNC --path . --name vmware-vnc`。安装后，在实际技能目录运行环境准备和测试：

```powershell
$skillRoot = "$env:USERPROFILE/.codex/skills/vmware-vnc" # 按实际安装位置调整
pwsh -NoProfile -File "$skillRoot/scripts/Setup.ps1"
pwsh -NoProfile -File "$skillRoot/scripts/Test.ps1"
```

当前官方发现位置包含 `~/.agents/skills`；本机捆绑的 skill-installer 默认使用 `$CODEX_HOME/skills`，未设置时为 `~/.codex/skills`。按宿主实际位置选择一种，避免重复安装同名技能。依据见 [OpenAI 技能文档](https://learn.chatgpt.com/docs/build-skills)。

### 手动克隆

目标目录须尚不存在：

```powershell
$skillRoot = "$env:USERPROFILE/.agents/skills/vmware-vnc"
git clone https://github.com/OvOyezi/VMware_VNC.git "$skillRoot"
pwsh -NoProfile -File "$skillRoot/scripts/Setup.ps1"
pwsh -NoProfile -File "$skillRoot/scripts/Test.ps1"
```

Setup 在安装目录创建 `.venv` 并安装固定依赖。需要指定 Python 时，传入 `-PythonExecutable`。目录移动后重新运行 Setup；不要复制其他目录的虚拟环境。

## 接入 MCP

安装 skill 后，生成该安装位置对应的配置片段：

```powershell
pwsh -NoProfile -File "$skillRoot/scripts/Get-McpConfig.ps1"
```

将输出合并到选定的 Codex 配置文件，并保留其他条目。该脚本只输出 TOML，不自动注册 MCP、不设置默认虚拟机，也不保存密码。配置字段和重载方法见 [安装与接入](references/setup.md)及 [OpenAI MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

VMware 中文辅助通道还需要本机 profile 和凭据环境变量，安装技能不会替你创建这些资料。按 [文本输入参考](references/text-input.md)配置，再核对当前聊天的 `vnc_profiles` 和诊断版本。

## 使用

在 Codex 中调用 `$vmware-vnc`，说明目标地址、端口和任务。对真实桌面使用 Computer Use 前，需要用户明确同意该操作；仅同意安装或配置不构成桌面操作授权。已有授权在其目标和范围内持续有效。

```python
vnc_connect(host="127.0.0.1", port=5901, session_id="work")
vnc_screenshot(session_id="work", connection_id="<本次连接返回的 ID>")
vnc_disconnect(session_id="work", connection_id="<同一个 ID>")
```

端点须替换为已经确认的目标。连接后先检查截图，再发送输入。具名会话的截图、输入、等待和断开都同时传入 `session_id`、`connection_id`；重连后换用新 ID。

操作流程见 [SKILL.md](SKILL.md)，参数和 CLI 命令见 [操作参考](references/operations.md)，画面条件见 [等待参考](references/visual-waits.md)，连接与输入问题见 [故障排查](references/troubleshooting.md)。

## 测试与兼容范围

`scripts/Test.ps1` 包含 136 项默认检查，只连接本机临时人工 RFB 服务，不操作真实虚拟机。GitHub Actions 在 Windows / Python 3.11、3.15 矩阵运行这些检查，结果见本页 checks 链接。

Windows 和 Linux 的 Unicode 通道完成过应用级读回验证。0.3.0 的视觉条件在 WIN11、WIN10 完成过 15 项实机验收和 241 轮持续测试，尚未在 Linux/Wayland 实机验收。测试前提、数据与时间范围见 [兼容性记录](docs/COMPATIBILITY.md)。

VMware 内置 VNC 的 fresh 截图在实测中仍返回完整帧，即使请求区域较小。区域采样减少中间帧编码和返回图片，不能据此保证该端点节省 RFB 流量。画面稳定或匹配也不等于应用任务完成，仍需检查应用结果。

真实端点验收须明确目标和授权。完整检查分类见 [checks/README.md](checks/README.md)。

## 维护与目录

```text
SKILL.md                   技能入口和操作流程
agents/openai.yaml         Codex 界面元数据
references/                安装、操作、文字输入和排查说明
assets/                    MCP 与 profile 配置示例
scripts/                   环境准备、配置生成和回归入口
src/vnc_mcp/               运行源码及来宾辅助资源
checks/                    人工 RFB 检查和显式实机验收工具
docs/                      兼容记录、维护历史和原始报告
```

维护步骤见 [CONTRIBUTING.md](CONTRIBUTING.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。本地和 GitHub 使用同一份源码与提交；维护改动先在本地完成，再推送 `main`。

`.venv/`、`.local/`、`checks/results/`、索引和构建产物由 Git 排除。历史报告保留原始路径和结论，安装及当前操作以 SKILL.md 和 references 为准。

## 来源与许可

基于 [thanhvn-shinkoxd/VNC-MCP](https://github.com/thanhvn-shinkoxd/VNC-MCP/tree/abe747b4dc42bc936870959861a436c849aef482)，固定提交为 `abe747b4dc42bc936870959861a436c849aef482`。上游署名保留在 [LICENSE](LICENSE)、[NOTICE](NOTICE) 和包元数据中，具体修改见 [LOCAL-CHANGES.md](LOCAL-CHANGES.md)。
