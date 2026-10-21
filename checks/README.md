# 检查工具

默认入口为仓库根目录的 `scripts/Test.ps1`，使用本机人工 RFB 服务或 mock，不操作真实虚拟机。

| 默认回归 | 验证范围 |
| --- | --- |
| vnc_mcp_smoke.py | MCP 初始化、模拟连接、截图像素与输入协议 |
| vnc_mcp_regression.py | refresh / fresh 生命周期、状态清理及本项目 CLI |
| vnc_mcp_deep_checks.py | 队列、取消、输入前预算核对、按键时间戳、拖动顺序与终点、编码及分段输入完整性 |
| vnc_mcp_multisession.py | 会话隔离、ID、容量、空闲回收与并发 reactor 启动 |
| vnc_mcp_diagnostics.py | 运行依赖诊断及可选剪贴板后端发现的只读边界 |
| vnc_mcp_extensions.py | UTF-8 协商、惰性请求、sync-flush、profile、来宾剪贴板与缓冲释放 |
| vnc_mcp_visual.py | 逐像素/区域条件、参考隔离、取消、并发、编码及参考文件限制 |

检查直接验证当前实现。输出 JSON、日志和截图保存在 `results/`，不加入 Git。

## 显式端点检查

以下工具只在明确指定真实端点后执行，不属于默认回归：

| 工具 | 用途 |
| --- | --- |
| vnc_mcp_multisession_live.py | 通过实际注册 MCP 检查至少两台桌面的连接、截图、重连和会话隔离；不发送键鼠 |
| vnc_mcp_soak.py | 持续只读截图与计划重连；`--self-test` 只访问人工服务，实机须传 `--allow-live` |
| vnc_mcp_refresh_diagnose.py | 跟踪 RFB 帧缓冲请求和完成情况；不发送键鼠 |
| vnc_mcp_input_live.py | 对四台明确配置的来宾执行输入、视觉、Unicode、会话隔离和应用结果读回验收；必须传 `--allow-live` |

多会话检查示例，先替换配置和端点：

```powershell
& "$skillRoot/.venv/Scripts/python.exe" "$skillRoot/checks/vnc_mcp_multisession_live.py" --config '<实际 MCP 配置文件>' --host '<实际地址>' --targets desktop-a=5901 desktop-b=5902 --output "$skillRoot/checks/results/live.json"
```

持续检查显式提供 `--targets NAME=HOST:PORT` 和 `--allow-live`；每 50 轮计划重连一个目标并拒绝旧 ID，出现异常即停止。它不发送键鼠或剪贴板，也不自动恢复失败操作。完整参数可查看脚本 `--help`。

实机输入验收须提供四个不同的 VNC profile / 端点 / VMX，以及匹配的目标清单和来宾结果文件路径。清单格式为 `{"targets":[{"name":"...","host":"...","port":5901,"profile":"...","result_path":"...","token":"..."}]}`；profile 凭据从其命名环境变量读取。脚本会启动本仓库的 stdio MCP、向四台来宾发送真实输入，并用官方 `vmrun` 轮询夹具 JSON；报告和 PNG 截图写到 `--output` 同目录。

```powershell
& "$skillRoot/.venv/Scripts/python.exe" "$skillRoot/checks/vnc_mcp_input_live.py" --manifest "$skillRoot/.local/input-live.targets.json" --profiles-file "$skillRoot/.local/guest-profiles.toml" --output "$skillRoot/checks/results/input-live.json" --allow-live
```

该命令会操作真实来宾桌面，不能作为默认回归运行。JSON 报告按目标列出 `passed`、`failed` 和 `not_executed` 步骤；非零退出码表示失败或未完成。

任务限定纯 VNC 时，不运行这个依赖 `vmrun` 读回的驱动。改用 VNC MCP 在真实系统应用和常用第三方应用中操作，以截图核对结果；逐键文字显式选择 `method="keys"`。本轮真实应用与重复操作范围见 [兼容性](../docs/COMPATIBILITY.md)。

## 测试窗口

- `fixtures/textbox_windows.ps1`、`textbox_linux.py` 提供独立文本窗口，把实际收到的内容写入指定 JSON，供应用级读回比较。只使用临时测试文字。
- `fixtures/visual_windows.ps1` 提供变化与固定区域。Windows PowerShell 5.1 / STA 下传入 ControlPath、ResultPath，控制文件可指定 generation、mode（static/pulse）和 red/green/blue。
- `fixtures/input_windows.ps1`、`input_linux.py` 提供文本、按钮、可拖动画布和滚轮区域，记录真实事件与控件坐标。Windows 参数为 `-ResultPath PATH -Token TOKEN`；Linux 参数为 `RESULT_PATH TOKEN`，需要 GTK 3 / PyGObject 或 tkinter。GTK 夹具的坐标验收使用 X11，Wayland 会话可通过 Xwayland 运行夹具。

这些窗口不会被安装或默认回归启动。实机先截图核对，具名调用同时携带两个 ID；结束后关闭自己的窗口并清理文件。验收步骤见 [兼容性](../docs/COMPATIBILITY.md)。
