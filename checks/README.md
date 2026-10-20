# 检查工具分类

默认入口为仓库根目录的 `scripts/Test.ps1`。只有下列四个脚本会由它自动执行，均使用本机人工 RFB 桌面，不操作真实虚拟机。

| 默认回归 | 验证范围 |
| --- | --- |
| vnc_mcp_smoke.py | MCP 初始化、工具、模拟连接、截图及输入协议 |
| vnc_mcp_regression.py | refresh / fresh 生命周期、状态清理及 CLI 行为 |
| vnc_mcp_deep_checks.py | 队列、取消、预算、编码、启动竞态与历史对照 |
| vnc_mcp_multisession.py | 多会话隔离、ID 校验、容量与空闲回收 |

以下工具只在显式指定端点后手动执行，不属于默认回归。对真实桌面运行时，先满足 [SKILL.md](../SKILL.md) 中的 Computer Use 授权要求。

| 手动工具 | 用途与注意事项 |
| --- | --- |
| vnc_mcp_multisession_live.py | 至少两台目标的连接、截图、重连及隔离验收；不发送键鼠，优先作为具名会话实机验收入口 |
| vnc_mcp_probe.py | 单目标连接与截图；兼容 default 会话的历史探测工具 |
| vnc_mcp_optimized_probe.py | JPEG / PNG、区域及连续截图比较；使用兼容 default 会话 |
| vnc_mcp_refresh_diagnose.py | 直接检查帧缓冲更新与首帧完成情况；不发送键鼠 |
| vnc_mcp_actions.py | 历史手动动作驱动器，会按 JSON 列表发送输入。执行前审阅完整动作，首项必须截图核对；不作为技能常规交互入口，不自动重放失败或取消的列表 |

`fixtures/baselines/` 是深挖回归使用的固定历史样本，不是备用运行后端。`results/` 存放本机生成的 JSON、日志和截图，已排除在 Git 外。私有动作列表与连接资料放 `.local/`，不要加入技能包。
