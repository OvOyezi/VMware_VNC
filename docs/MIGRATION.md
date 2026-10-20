# 项目化迁移记录

2026-10-20，将 `C:\Users\Administrator\Documents\VMware_Personal\tools\vnc-mcp` 复制到 `C:\Users\Administrator\Documents\VMware_VNC`，提升为项目根目录。保留 src、main.py、包定义、许可、依赖和现有 VNC 功能。

维护工具移入本项目 checks，测试对照固定到 checks/fixtures/baselines；原始截图、报告、补丁和源码备份原样归档到 .local/history。原记录中的旧路径属于历史证据，未批量重写。docs/README-before-project-migration.md 同样是历史说明。

新增 PowerShell 7 环境和回归入口、项目 README、维护约定和 Git 忽略规则。重建 .venv 并 editable 安装，避免复制带旧路径的环境。项目化调整没有修改运行模块或运行依赖版本。

Codex 全局配置只更新 vmware_vnc 的 command、args、cwd；修改前完整备份留在 ~/.codex，不纳入项目版本库。旧目录及其环境保留；需要回退时可依据 .local/project-migration.json 中的原路径和备份恢复该服务器条目，恢复前先核对当前配置，避免覆盖之后的其它设置。

新索引项目为 vmware-vnc，根为本项目。旧目录的图索引作为保留副本仍可存在，结构查询应以新根为准。

验收与限制见 STATUS.md。本机迁移的机器路径、配置备份名、哈希清单及证据路径保存在 .local，不放入 Git。
