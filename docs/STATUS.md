# 维护状态

更新日期：2026-10-20，Asia/Shanghai。

主维护根目录：`C:\Users\Administrator\Documents\VMware_VNC`。本地 Git 分支为 main，未配置远程仓库。旧 VMware_Personal 目录保留回退副本，并添加迁移提示；后续维护使用本项目。

## 本次迁移已验证

- main.py、7 个运行模块、包定义、2 个依赖文件、LICENSE、NOTICE 共 13 个文件与复制前 SHA-256 一致。VNC 行为没有在本次迁移中调整。
- Python 3.15.0 环境在本目录重建；39 个固定依赖版本逐一一致，pip check 通过。editable 包与 CLI 可用，从项目外部工作目录导入也指向新 src。
- 新 scripts/Test.ps1 全部通过：基础 10 项、两种模式生命周期 18 项、深挖回归 16 项、多会话 21 项，合计 65 项。两个历史对照样本已固定到项目内，测试不再依赖旧目录或临时源副本。
- Codex vmware_vnc 的 command、args、cwd 已指向本项目；其它配置字段经完整解析比较保持一致。修改前配置备份在 ~/.codex 下，具体文件名见本机迁移记录。
- 按真实注册配置启动新 MCP 进程，18 个工具可用。WIN11/WIN10 同时连接、三轮并行全图、区域截图、独立断开和旧 ID 拒绝共 5 项实机断言通过，图像已查看；实机未发送键鼠输入，结束会话数为 0。
- 日志、截图、历史资料和环境被 Git 排除。已运行的 MCP 进程没有被终止，后续重载使用新入口；整个 Codex 应用重启未在本次执行。

## 证据

- [基础检查](../checks/results/project-smoke.json)
- [生命周期](../checks/results/project-lifecycle.json)
- [深挖回归](../checks/results/project-deep.json)
- [多会话](../checks/results/project-multisession.json)
- [注册配置实机验收](../checks/results/project-live.json)
- [本机迁移记录](../.local/project-migration.json)
- [迁移前报告与备份](../.local/history/)

这些证据属于本机资料，未提交到 Git；新环境运行 scripts/Test.ps1 后生成自己的结果。

## 继续维护的边界

中文直接输入与 Latin-1 剪贴板限制未改变。fresh 仍采用独立共享连接取首帧，没有实现持久增量画面。多小时运行、真实网络故障和 Codex WebSocket 问题没有获得新增验证。
