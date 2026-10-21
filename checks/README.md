# 检查工具分类

默认入口为仓库根目录的 `scripts/Test.ps1`。下列七个脚本会由它自动执行，使用本机人工 RFB 服务或 mock，不操作真实虚拟机。

| 默认回归 | 验证范围 |
| --- | --- |
| vnc_mcp_smoke.py | MCP 初始化、工具、模拟连接、截图及输入协议 |
| vnc_mcp_regression.py | refresh / fresh 生命周期、状态清理及 CLI 行为 |
| vnc_mcp_deep_checks.py | 队列、取消、预算、编码、启动竞态与历史对照 |
| vnc_mcp_multisession.py | 多会话隔离、ID 校验、容量与空闲回收 |
| vnc_mcp_diagnostics.py | 本机依赖诊断、vmrun 发现与无副作用边界 |
| vnc_mcp_extensions.py | UTF-8 codec 与 loopback 协商、惰性请求、sync-flush、profile、取消和关闭后的缓冲释放 |
| vnc_mcp_visual.py | 逐像素/区域条件、参考隔离、MCP 取消、轮询并发、单次最终编码、CLI 和参考文件边界 |

以下工具只在显式指定端点后手动执行，不属于默认回归。对真实桌面运行时，先满足 [SKILL.md](../SKILL.md) 中的 Computer Use 授权要求。

| 手动工具 | 用途与注意事项 |
| --- | --- |
| vnc_mcp_multisession_live.py | 至少两台目标的连接、截图、重连及隔离验收；不发送键鼠，优先作为具名会话实机验收入口 |
| vnc_mcp_soak.py | 显式 --allow-live 后持续只读截图与计划重连；--self-test 只访问人工服务 |
| vnc_mcp_probe.py | 单目标连接与截图；兼容 default 会话的历史探测工具 |
| vnc_mcp_optimized_probe.py | JPEG / PNG、区域及连续截图比较；使用兼容 default 会话 |
| vnc_mcp_refresh_diagnose.py | 直接检查帧缓冲更新与首帧完成情况；不发送键鼠 |
| vnc_mcp_actions.py | 历史手动动作驱动器，会按 JSON 列表发送输入。执行前审阅完整动作，首项必须截图核对；不作为技能常规交互入口，不自动重放失败或取消的列表 |

`fixtures/baselines/` 是深挖回归使用的固定历史样本，不是备用运行后端。`results/` 存放本机生成的 JSON、日志和截图，已排除在 Git 外。私有动作列表与连接资料放 `.local/`，不要加入技能包。

`fixtures/textbox_windows.ps1` 与 `fixtures/textbox_linux.py` 是手动 Unicode 验收的独立测试窗口，需在明确选择的来宾中运行。它们把测试文本写入指定结果文件，便于通过官方 CLI 独立读回；不会由默认回归或技能安装启动。用临时测试文字，测试后关闭窗口并删除自己的文件。流程见 [兼容性与验收](../docs/COMPATIBILITY.md)。

`fixtures/visual_windows.ps1` 是显式实机视觉验收窗口。PowerShell 5.1 / STA 下传入 ControlPath 和 ResultPath；控制 JSON 包含 generation、mode（static/pulse）、red/green/blue。左右两块区域分别变化和保持固定，结果 JSON 给出原生坐标及当前颜色。连接后先截图核对，再用官方 vmrun 更新控制文件、独立核对结果；测试后只关闭自己的窗口并清理自己的目录。
