# 贡献指南

欢迎通过 [Issues](https://github.com/OvOyezi/VMware_VNC/issues) 报告问题、提出需求，或提交 Pull Request。请说明具体任务、当前行为与预期结果。

## 开发环境

使用 Windows、PowerShell 7 和 Python 3.11+：

```powershell
pwsh -NoProfile -File ./scripts/Setup.ps1
pwsh -NoProfile -File ./scripts/Test.ps1
```

运行源码位于 `src/vmware_vnc/`，固定依赖见 `requirements.lock.txt`。模块分工见 [核心架构](docs/DESIGN.md)，产品边界与实现约束见 [AGENTS.md](AGENTS.md)。

## 验证修改

默认检查连接临时人工 RFB 服务，覆盖 MCP、CLI、协议和会话行为，不操作真实虚拟机。检查工具及实机入口见 [checks/README.md](checks/README.md)。

- 修改公共能力时，同时验证 MCP 与 CLI 的对应行为。
- 修改安装、资源或打包时，在独立安装目录检查导入、MCP 初始化和相关操作。
- 修改接口或配置时，同步更新使用说明和示例，检查文档链接。
- 实机验证明确指定端点，分别核对画面、输入与应用结果，结束后断开连接并清理测试内容。

## 提交与反馈

提交说明写清问题、行为变化和完成的验证。报告兼容性时，提供项目版本、VNC 服务端、来宾显示环境和 AI 宿主；区分模拟服务与真实端点结果。不要提交密码、私有 profile 或含敏感信息的截图和日志。

版本变化记录在 [CHANGELOG.md](CHANGELOG.md)。发布前核对版本、安装说明、接口、支持范围与固定依赖是否一致。
