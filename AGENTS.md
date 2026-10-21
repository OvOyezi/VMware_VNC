# VMware VNC 维护约定

- 该目录是 VNC MCP 的唯一维护项目；本次迁移不保留旧副本或迁移备份。
- 默认使用 PowerShell 7。环境用 `scripts/Setup.ps1` 重建，不复制 `.venv`。
- 结构性代码查询使用已安装的 codebase-memory 技能；先核对索引根目录和覆盖情况。
- 核心运行代码位于 `src/vnc_mcp`，`main.py` 是本地入口。不要把业务逻辑放入启动脚本。
- 仓库根目录即 `vmware-vnc` 技能；`SKILL.md` 保留核心工作流，按需细节放 `references/`，界面元数据放 `agents/openai.yaml`。源码只维护一份，不另建重复技能副本。
- 安装和运行不得依赖本机绝对路径；MCP 片段由 `scripts/Get-McpConfig.ps1` 按安装位置生成。技能安装不自动修改用户的 Codex 配置。
- 修改后运行 `scripts/Test.ps1`。默认测试只接触本机模拟 RFB 服务，不操作真实虚拟机。
- 实机测试必须显式提供目标地址及端口，连接后先截图核对；具名操作同时携带 session_id、connection_id。不要自动重放失败或取消的输入。
- VMware 电源等操作默认使用官方 vmrun；截图和键鼠通过本项目的 VNC MCP 或 CLI，只操作明确选定的 VNC 端点。
- 依赖固定在 `requirements.lock.txt`；升级需单独说明并重新验证。上游来源和本地差异见 `LOCAL-CHANGES.md`。
- `.local/`、`checks/results/`、`.venv/` 为本机资料，不加入 Git；不要提交真实密码、全局 Codex 配置、截图或访问凭据。
- 历史报告保留原始路径和结论。新结论须有新证据，区分模拟协议、真实端点和当前聊天加载状态。
