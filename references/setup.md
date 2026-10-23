# 安装与 MCP 接入

## 运行环境

- `scripts/` 中的安装、配置生成和检查脚本面向 Windows，要求 PowerShell 7（`pwsh`）。
- Python 3.11+，可以用 `-PythonExecutable` 选择解释器；实际环境以安装与检查结果为准。
- 可达的 VNC 端点及相应认证。基础画面与键鼠不要求特定来宾 OS、系统账号、Tools 或外部 CLI。可选 VMware 来宾剪贴板后端另需 `vmrun`、Tools 和来宾凭据，见 [文本输入](text-input.md)。

将仓库克隆到选定目录，或安装可选 skill 后，以实际项目根目录确定 `$projectRoot`。MCP 与 CLI 均可直接使用，不要求安装 Codex。

```powershell
$projectRoot = 'C:/path/to/vmware-vnc' # 替换为实际安装目录
pwsh -NoProfile -File "$projectRoot/scripts/Setup.ps1"
pwsh -NoProfile -File "$projectRoot/scripts/Test.ps1"
& "$projectRoot/.venv/Scripts/vmware-vnc.exe" --help
```

Setup 在该目录创建或复用 `.venv`，按固定依赖安装，再以 editable 方式安装包。它不修改 AI 宿主配置、不连接 VNC，也不复制其他目录的环境。目录移动后重新运行 Setup；不要搬运现有 `.venv`。

## 通用 MCP stdio 接入

在所用 AI 宿主中注册本地 stdio 服务，由宿主启动并保持进程：

| 进程字段 | 值 |
| --- | --- |
| command | 实际安装目录下的 `.venv/Scripts/python.exe` 绝对路径 |
| args | 实际安装目录下 `main.py` 的绝对路径，以及第二个参数 `mcp` |
| cwd | 实际安装目录；按宿主支持的方式设置 |
| env | 按需传入下表的环境变量；基础连接不需要 profile 或来宾凭据 |

对应的进程启动命令为：

```powershell
& "$projectRoot/.venv/Scripts/python.exe" "$projectRoot/main.py" mcp
```

也可运行 `pwsh -NoProfile -File "$projectRoot/scripts/Get-McpConfig.ps1" -Format StdioJson` 生成上述通用进程描述。生成结果不含凭据，也不会写入宿主配置。

这是 stdio 服务，不是 HTTP 地址。不同宿主的配置文件格式和重载方式各异，上表描述进程参数，不代表统一的配置文件结构。宿主需要支持 MCP stdio、工具调用和图片结果；逐一验证初始化、工具发现、图片接收和连续会话。当前合成检查使用标准 MCP Python 客户端，不代表已验收所有 AI 产品。

保存宿主配置后重新加载服务，核对当前聊天实际可用的工具；`vnc_diagnostics` 可检查运行版本。磁盘文件或静态配置已更新，不表示旧进程已重载。安装 skill 本身也不会自动注册 MCP。

## Codex 配置示例与可选 skill

`scripts/Get-McpConfig.ps1` 默认提供 Codex TOML 适配示例，也可显式指定 `-Format CodexToml`：

```powershell
pwsh -NoProfile -File "$projectRoot/scripts/Get-McpConfig.ps1"
```

默认模式只向标准输出生成 TOML；`-Format StdioJson` 输出通用进程 JSON。command、args、cwd 均根据实际安装位置生成，不含默认目标或密码。模板在 `assets/codex-mcp.toml.template`；其他 MCP 宿主使用上节的进程参数，无需使用该模板。

用户要求接入 Codex 时，将输出合并到其选定的配置文件，只更新 `[mcp_servers.vmware_vnc]` 及其 env 表，保留其他配置。已有同名条目时先核对来源。安装和生成片段本身不修改任何用户配置，也不表示服务已加载。

需要 Codex skill 时，可让 `$skill-installer` 安装本仓库根目录：

```text
--repo OvOyezi/VMware_VNC --path . --name vmware-vnc
```

也可克隆到宿主实际使用的技能目录并命名为 `vmware-vnc`，避免同名重复安装。`agents/openai.yaml` 是 Codex 界面元数据；不被其他宿主使用时，不影响 VNC 核心。安装后仍须在该副本运行 Setup，并按所用宿主接入 MCP。

## 配置选项

| 环境变量 | 默认 | 用途 |
| --- | --- | --- |
| VNC_TIMEOUT | 服务 15 秒；生成片段 5 秒 | 单步网络等待 |
| VNC_ACTION_TIMEOUT | 20 秒 | 协作式操作预算 |
| VNC_SCREENSHOT_MODE | auto | 先尝试当前连接刷新，超时后使用共享短连接取首帧；也可指定 refresh / fresh |
| VNC_ENCODING | auto | 提供无损 ZRLE、Tight、Hextile、CopyRect 和 RAW；也可指定 zrle / tight / hextile / raw，保留 RAW 回退 |
| VNC_MAX_SESSIONS | 4 | 上限 1–32 |
| VNC_MAX_PENDING | 16 | 每会话未完成请求上限 1–256 |
| VNC_IDLE_TIMEOUT | 300 秒 | 0 禁用空闲回收 |
| VNC_HOST / VNC_PORT | 无 | 可选原生默认目标；生成片段不设置 |
| VNC_URL | 无 | 可选 vnc／vnc+tls／ws／wss 默认目标，与 VNC_HOST／VNC_PORT 互斥 |
| VNC_TLS_CA_FILE | 系统信任库 | TLS／WSS 的可选 CA 文件；验证证书与主机名，不跳过验证 |
| VNC_PASSWORD_ENV | VNC_PASSWORD | 服务进程读取的密码环境变量名 |
| VNC_PROFILES_FILE | 无 | 显式来宾 profile TOML 路径；配置方法见 text-input.md |
| VNC_VMRUN_PATH | 自动发现 | 可选的官方 vmrun 可执行文件路径 |

认证需要时，在启动 MCP 的本机环境中提供对应变量，或使用宿主支持的环境变量转发。变量值不能提交 Git。密码变化后须确认服务进程实际获得新环境；不要在聊天中回显凭据。

客户端连接 `127.0.0.1` 不会限制 VNC 服务端的监听范围。使用前核对服务端认证和网络访问规则；普通 TCP 和 ws 不加密；远程可使用验证证书的 vnc+tls／wss 或环境已有的加密通道。

## CLI 辅助入口

Setup 同时安装 `vmware-vnc.exe`。CLI 持续维护单次操作、诊断和脚本集成；每次命令连接后自行断开，不保留 MCP 会话或参考帧。完整操作见 [接口参考](operations.md)。它与 MCP 共享核心实现，基础操作不调用外部 `vmrun`；只有显式选择的来宾文本增强依赖该工具。

## 可选传输

```python
vnc_connect(url="vnc://127.0.0.1:5901", session_id="native")
vnc_connect(url="wss://vnc.example.test:6080/websockify", session_id="proxy")
```

URL 与 host／port／profile 互斥，禁止内嵌账号密码及 fragment。VNC URL 可省略端口（默认 5900），WS／WSS 默认 80／443；原生 host／port 形式仍要求明确端口。公开状态只返回传输类型、主机和端口，不包含代理路径或查询参数。`vnc+tls` 是 TLS 包裹的 RFB 字节流，不表示支持 VeNCrypt 或其他新增 RFB 安全类型。

WebSocket 使用二进制子协议，可连接 websockify 等标准代理。项目不自带代理进程，也不将代理作为直接 VMware VNC 的前提。TLS／WSS 默认验证证书，可通过 `VNC_TLS_CA_FILE` 或 CLI `--tls-ca-file` 指定 CA；不存在关闭验证的配置。可选来宾 profile 只绑定原生 host／port，不能通过代理 URL 推断来宾身份。

## 从旧包迁移

0.5.0 的分发名称与命令行为 `vmware-vnc`，Python 导入包为 `vmware_vnc`。旧标识仅用于一次性迁移，不提供重复源码或旧包运行适配层。在确认旧 editable 包属于当前安装目录后，使用该目录的 `.venv` 卸载旧分发，并清除该项目原有的生成元数据，再运行 Setup：

```powershell
& "$projectRoot/.venv/Scripts/python.exe" -m pip show vnc-mcp
# 确认 Editable project location 就是 $projectRoot 后继续。
& "$projectRoot/.venv/Scripts/python.exe" -m pip uninstall vnc-mcp
$legacyMetadata = Join-Path $projectRoot 'src/vnc_mcp.egg-info'
if (Test-Path -LiteralPath $legacyMetadata) {
    Remove-Item -LiteralPath $legacyMetadata -Recurse
}
pwsh -NoProfile -File "$projectRoot/scripts/Setup.ps1"
```

直接导入旧包的脚本和旧可执行文件路径需要更新。`main.py mcp` 和 `vnc_*` 工具名不变。安装本项目不会替换其他目录的环境、注册服务或全局配置。
