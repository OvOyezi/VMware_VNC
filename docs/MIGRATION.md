# 完整迁移记录

2026-10-20，VNC MCP 从 VMware_Personal 完整移植到本项目 VMware_VNC。运行模块、main.py、包定义、固定依赖和许可证保持一致；独立环境已在目标目录重建。

源码与维护脚本、测试对照数据、调查报告、测试结果及手动操作场景都已归入本项目。测试对照数据固定在 checks/fixtures/baselines；报告在 docs/reports，输出与截图在 checks/results，手动场景在 .local/scenarios。

按用户要求，已删除旧 VNC 源码和环境、旧检查目录、重复历史归档、迁移配置备份、临时上游源码与补丁回放目录。旧目录不再留有 VNC 副本或迁移提示文件。WIN10 的 vmrun 与地区设置记录属于独立工作，仍在原 VMware_Personal 目录。

Codex vmware_vnc 的 command、args、cwd 均指向本项目。索引项目为 vmware-vnc，旧部署和临时源索引已移除；结构查询使用新根目录。没有配置远程 Git 仓库。

验证状态见 STATUS.md，本机连接资料及迁移记录在 .local。它们不包含可回退的源码或环境备份。
