# VMware VNC Skill

一个可独立安装的 `vmware-vnc` 技能，附带 VNC MCP 服务与 CLI。在 Windows 上通过截图与键鼠操作 VMware 或其他 VNC 桌面，支持多个虚拟机的独立会话、原生尺寸全图和区域截图。

| 项目 | 说明 |
| --- | --- |
| 技能名 | `vmware-vnc`，仓库根目录就是技能目录 |
| 分类 | 开发工具 / 虚拟机桌面自动化 |
| 接入方式 | Codex skill + 本地 stdio MCP，或独立 CLI |
| 环境 | Windows、PowerShell 7、Python 3.11+；本机验证环境为 Python 3.15.0 |
| 许可证 | Apache-2.0，保留上游署名与本地修改记录 |
| 发布状态 | 仅本地 Git；尚未配置远程或发布 GitHub 仓库 |

## 安装技能

### 从本地 Git 安装

本项目已经是本地 Git 仓库。以下命令把已提交的文件安装到一个技能目录，不会复制源码工作区中的 `.venv`、`.local`、截图或检查结果。替换 `$repository` 为本地仓库路径；目标目录须尚不存在。

```powershell
$repository = 'C:/path/to/VMware_VNC'
$skillRoot = "$env:USERPROFILE/.agents/skills/vmware-vnc"
git clone --local --no-hardlinks "$repository" "$skillRoot"
pwsh -NoProfile -File "$skillRoot/scripts/Setup.ps1"
pwsh -NoProfile -File "$skillRoot/scripts/Test.ps1"
```

`git clone` 获取当前分支已提交的版本；先提交要安装的更改。该副本用于安装，源项目继续作为唯一维护位置。

技能目录遵循 [OpenAI 官方技能文档](https://learn.chatgpt.com/docs/build-skills)。按实际宿主选择安装位置：当前文档使用 `~/.agents/skills`；本机捆绑的 skill-installer 默认使用 `$CODEX_HOME/skills`，未设置时为 `~/.codex/skills`。选择一种即可，避免重复安装同名技能。

### 将来从 GitHub 安装

上传自己的仓库后，可向 Codex 的 `$skill-installer` 提供真实 GitHub 地址，要求安装根目录 `.`，名称为 `vmware-vnc`。其脚本参数为：

```text
--repo OWNER/REPO --path . --name vmware-vnc
```

`OWNER/REPO` 是待替换值，目前没有可用的本项目在线安装地址。不要改用上游 VNC-MCP 的地址；上游仓库没有本技能的入口与本地修改。安装后仍需在技能目录运行 Setup。

## 配置 MCP

技能提供操作流程；附带的 MCP 服务提供实际工具。仅安装技能不会自动注册 MCP。

```powershell
pwsh -NoProfile -File "$skillRoot/scripts/Get-McpConfig.ps1"
```

该命令只输出按安装位置生成的 TOML 片段，不修改 Codex 全局配置，不设置默认虚拟机，也不保存密码。需要接入时，将片段合并到选定的 Codex 配置，保留其他条目，再核对 MCP 初始化与当前聊天的工具加载状态。详见 [安装与接入](references/setup.md)和 [OpenAI MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

如果只是维护当前仓库，可直接在仓库根目录执行：

```powershell
pwsh -NoProfile -File ./scripts/Setup.ps1
pwsh -NoProfile -File ./scripts/Test.ps1
pwsh -NoProfile -File ./scripts/Get-McpConfig.ps1
```

Python 不在 PATH 或需要选择版本时，给 Setup 传入 `-PythonExecutable`。环境在当前目录重建，不搬运其他安装的 `.venv`。依赖固定在 `requirements.lock.txt`。

## 使用

在 Codex 中调用 `$vmware-vnc`，说明目标地址、端口和任务。真实桌面 Computer Use 必须取得用户明确同意使用它的确切答复；同意安装、配置或笼统同意方案，不等于授权桌面操作。

MCP 示例（先确认端点，再替换返回的 ID）：

```python
vnc_connect(host="127.0.0.1", port=5901, session_id="WIN11")
vnc_screenshot(session_id="WIN11", connection_id="<本次连接返回的 ID>")
vnc_disconnect(session_id="WIN11", connection_id="<同一个 ID>")
```

连接后先截图核对目标。具名会话的截图、输入、等待和断开始终同时携带 session_id、connection_id。多虚拟机分别记录 ID；重连后旧 ID 失效，不自动重放失败或取消的输入。完整流程见 [SKILL.md](SKILL.md)，具体参数见 [操作参考](references/operations.md)。

VMware 的电源、快照和来宾命令使用官方 vmrun；需要桌面画面时使用 VNC。本技能安装和默认测试不启动、连接或修改真实虚拟机。

## 目录分类

```text
.
├── SKILL.md                  技能入口、触发条件、授权与操作流程
├── agents/openai.yaml        Codex 界面元数据
├── references/               安装接入、操作参数、故障排查
├── assets/                   MCP 配置模板
├── scripts/                  环境安装、配置生成、模拟回归入口
├── src/vnc_mcp/              唯一一份运行源码
├── main.py                   本地启动入口
├── pyproject.toml            Python 包与 CLI 入口
├── requirements.lock.txt     固定运行依赖
├── checks/                   模拟回归、显式实机验收及历史调查工具
├── docs/                     维护状态、迁移记录与原始报告
└── LICENSE / NOTICE          上游许可证和署名
```

`.venv/`、`.local/`、`checks/results/`、索引和构建产物均排除在 Git 外。源码、固定依赖、测试样本和许可证随技能一起安装；不依赖原作者绝对路径。原始调查报告保留当时的路径与结论，当前使用方式以 SKILL.md 和 references 为准。

检查脚本按模拟回归、显式端点验收和历史调查工具分类，详见 [checks/README.md](checks/README.md)。

## 验证与限制

`scripts/Test.ps1` 默认仅连接本机临时人工 RFB 服务，覆盖 MCP 协议、连接生命周期、取消、队列与多会话隔离。运行结果在 `checks/results/project-*.json`。历史性能与竞态对照样本固定在 `checks/fixtures/baselines/`，不依赖旧工作区。

真实端点测试需明确目标及授权，不能把模拟通过写成真实虚拟机兼容性通过；具体命令见 [操作参考](references/operations.md)。当前与历史验收分开记录在 [维护状态](docs/STATUS.md)。

中文直接输入尚未通过 VMware 验证，剪贴板仅支持 Latin-1。fresh 截图仍传输完整首帧；协作取消不能撤回已发送输入。长期运行、真实网络故障及 Codex 模型 WebSocket 问题不属于默认回归的验证范围。

## 来源

基于 [thanhvn-shinkoxd/VNC-MCP](https://github.com/thanhvn-shinkoxd/VNC-MCP/tree/abe747b4dc42bc936870959861a436c849aef482)，固定提交 `abe747b4dc42bc936870959861a436c849aef482`。保留原作者的 [LICENSE](LICENSE)、[NOTICE](NOTICE)；本地差异见 [LOCAL-CHANGES.md](LOCAL-CHANGES.md)。本项目与上游仓库分别维护。
