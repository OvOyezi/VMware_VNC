# VMware VNC 维护约定

- 该目录是 VNC MCP 的主维护项目；旧 VMware_Personal 副本保留作迁移回退。
- 默认使用 PowerShell 7。环境用 `scripts/Setup.ps1` 重建，不复制 `.venv`。
- 结构性代码查询使用已安装的 codebase-memory 技能；先核对索引根目录和覆盖情况。
- 核心运行代码位于 `src/vnc_mcp`，`main.py` 是本地入口。不要把业务逻辑放入启动脚本。
- 修改后运行 `scripts/Test.ps1`。默认测试只接触本机模拟 RFB 服务，不操作真实虚拟机。
- 实机测试必须显式提供目标地址及端口，连接后先截图核对；具名操作同时携带 session_id、connection_id。不要自动重放失败或取消的输入。
- VMware 电源等操作默认使用官方 vmrun。需要键鼠和截图时可用 VNC；全局 Computer Use 须遵守用户要求，取得确切同意后才能使用。
- 依赖固定在 `requirements.lock.txt`；升级需单独说明并重新验证。上游来源和本地差异见 `LOCAL-CHANGES.md`。
- `.local/`、`checks/results/`、`.venv/` 为本机资料，不加入 Git；不要提交真实密码、全局 Codex 配置、截图或访问凭据。
- 历史报告保留原始路径和结论。新结论须有新证据，区分模拟协议、真实端点和当前聊天加载状态。
