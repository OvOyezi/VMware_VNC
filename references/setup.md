# 安装与 MCP 接入

## 运行环境

- Windows；维护脚本使用 PowerShell 7（`pwsh`）。
- Python 3.11+，可以用 `-PythonExecutable` 选择解释器。固定依赖在 Windows / Python 3.15.0 完成过验证，其他 Python 版本须以安装与测试结果为准。
- 可达的 VNC 服务。可选的 VMware 来宾剪贴板后端额外需要 vmrun、Tools 和来宾凭据，配置见 [文本输入](text-input.md)。

用本 `SKILL.md` 的实际位置确定 `$skillRoot`，脚本和运行环境都从该位置定位。

```powershell
$skillRoot = 'C:/path/to/vmware-vnc' # 替换为实际技能目录
pwsh -NoProfile -File "$skillRoot/scripts/Setup.ps1"
pwsh -NoProfile -File "$skillRoot/scripts/Test.ps1"
& "$skillRoot/.venv/Scripts/vnc-mcp.exe" --help
```

Setup 在技能目录创建或复用 `.venv`，按固定依赖安装，再以 editable 方式安装包。它不修改 Codex 配置，不连接 VNC，不复制其他目录的环境。目录移动后重新运行 Setup；不要搬运现有 `.venv`。

## 生成配置片段

```powershell
pwsh -NoProfile -File "$skillRoot/scripts/Get-McpConfig.ps1"
```

该命令只向标准输出生成 TOML。command、args、cwd 均根据技能实际位置生成，不含默认目标或密码。需要保存时，由调用方把输出写入 `.local/` 等本机目录。模板在 `assets/codex-mcp.toml.template`。

用户要求接入 MCP 时，将输出合并到其选定的 Codex 配置文件中，只更新 `[mcp_servers.vmware_vnc]` 及其 env 表，保留其他配置。不要把整个片段覆盖到全局配置文件，也不要仅因安装技能就改动配置。已有同名条目时先核对其实际来源。

片段字段使用 command、args、cwd、env，见 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。生成片段不表示已经注册、加载或连接成功。

改动配置后，用 `codex mcp list --json` 核对静态配置；通过 MCP 初始化与工具清单验证新进程。当前聊天没有新工具时，按当前宿主的重载方式重新加载，仍未出现则重启 Codex。工具可用性须在当前聊天再次确认。

## 配置选项

| 环境变量 | 默认 | 用途 |
| --- | --- | --- |
| VNC_TIMEOUT | 服务 15 秒；生成片段 5 秒 | 单步网络等待 |
| VNC_ACTION_TIMEOUT | 20 秒 | 协作式操作预算 |
| VNC_SCREENSHOT_MODE | auto | 先尝试当前连接刷新，超时后使用共享短连接取首帧；也可指定 refresh / fresh |
| VNC_ENCODING | auto | 提供无损 ZRLE、Tight、Hextile、CopyRect 和 RAW；也可指定 zrle / tight / hextile / raw，保留 RAW 回退 |
| VNC_MAX_SESSIONS | 4 | 上限 1–32 |
| VNC_IDLE_TIMEOUT | 300 秒 | 0 禁用空闲回收 |
| VNC_HOST / VNC_PORT | 无 | 可选默认目标；生成片段不设置 |
| VNC_PASSWORD_ENV | VNC_PASSWORD | 服务进程读取的密码环境变量名 |
| VNC_PROFILES_FILE | 无 | 显式来宾 profile TOML 路径；配置方法见 text-input.md |
| VNC_VMRUN_PATH | 自动发现 | 可选的官方 vmrun 可执行文件路径 |

认证需要时，在启动 MCP 的本机环境中提供对应变量，或使用宿主支持的环境变量转发。变量值不能提交 Git。密码变化后须确认服务进程实际获得新环境；不要在聊天中回显凭据。

客户端连接 `127.0.0.1` 不会限制 VNC 服务端的监听范围。使用前核对服务端认证和网络访问规则；本项目的 RFB TCP 连接不提供 TLS 加密，远程连接需要环境已有的加密通道。

## 安装分发

本仓库根目录就是单个 skill。向 `$skill-installer` 提供 [OvOyezi/VMware_VNC](https://github.com/OvOyezi/VMware_VNC)，选择 `--repo OvOyezi/VMware_VNC --path . --name vmware-vnc`，再在实际安装目录运行 Setup。

本地安装可将已提交的 Git 仓库克隆到宿主的技能目录，并将目标文件夹命名为 `vmware-vnc`。当前官方本地发现位置包含 `$HOME/.agents/skills`；本机捆绑的 skill-installer 则默认使用 `$CODEX_HOME/skills`（未设置时为 `$HOME/.codex/skills`）。按实际宿主选择一种位置，避免同名重复安装。依据见 [技能官方文档](https://learn.chatgpt.com/docs/build-skills)及本机 installer 实现。

安装后运行该副本内的 Setup；Git 不含 `.venv`、截图或本机连接资料。安装技能不会自动使 MCP 工具出现在当前聊天中。
