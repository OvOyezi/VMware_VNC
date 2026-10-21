# 兼容性与验收

本项目从 Windows 宿主访问 VNC 桌面，脚本要求 PowerShell 7、Python 3.11+。来宾系统与宿主要求分开；实际功能还取决于 VNC 服务端、显示会话和目标应用。

## 兼容范围

| 能力 | 条件与限制 |
| --- | --- |
| 截图、鼠标、按键 | 依赖服务端提供有效帧和输入事件。不能保证受保护桌面、锁屏或特殊应用的可见性与可操作性 |
| fresh 截图 | 独立共享连接取首帧；可避开部分 VMware refresh 不更新的问题，服务端可能返回完整帧 |
| Hextile / RAW | auto 提供两种无损编码，由服务端选择。固定 vncdotool 1.4.2 的 ZRLE 有兼容问题，当前不协商 |
| 原生 UTF-8 剪贴板 | 服务端须协商 Extended Clipboard；没有扩展时不把 Unicode keysyms 当作通用中文输入方法 |
| 来宾剪贴板后端 | 可选 VMware 文本实现，需要显式 profile、Tools、凭据和相应来宾剪贴板工具，见 [文本输入](../references/text-input.md) |
| 画面条件 | 对采样图像比较；可能遗漏采样间的短暂变化，匹配或稳定不证明应用完成任务 |
| 区域采样 | 发送 RFB 矩形请求，但服务端可返回更大区域。裁剪和减少图像编码不等于节省网络流量 |

## 纯 VNC 实机覆盖

2026-10-21 至 22 日在四台 VMware 来宾中测试真实应用，全部来宾操作经 VNC 完成，没有执行来宾命令或调用来宾剪贴板后端。发现连续输入丢字和拖动超时后，用新启动的 0.3.3 MCP 进程复测默认输入和窗口拖动；测试进程未配置 profile。

| 环境 | 已验证 | 本轮范围 |
| --- | --- | --- |
| Windows 11 | Win+E、Win+I、Ctrl+Shift+Esc、Ctrl+Alt+Del；记事本输入、保存；桌面及文件右键菜单、左键选项、文件双击；设置页面操作；YY 临时设置输入；窗口拖动 | YY 未登录账号，输入后取消设置；安全界面仅检查画面并点击取消 |
| Windows 10 | Win+I、Ctrl+Shift+Esc、Ctrl+Alt+Del；记事本输入、保存；桌面及文件菜单、文件双击；设置页面操作；YY 临时设置输入；窗口拖动 | 同上 |
| Ubuntu 26.10 / GNOME | Super 搜索与桌面切换；文本编辑器输入和保存；桌面及文件菜单、文件双击；系统设置；Firefox 设置搜索、页面切换、滚轮和窗口拖动 | 使用现有桌面与应用，不代表所有 Linux 显示会话均兼容 |
| Debian 13.6 / tty | 字母、大小写、数字、符号、退格、Ctrl+U、Ctrl+Alt+F1/F2 | 按本轮要求只测控制台键盘；未测试鼠标、图形应用和中文 |

四台各通过 50 轮默认逐键输入，共 9,450 字符。每轮与已检查的正文区域逐像素比较；Windows 另核对光标行列，确认换行完成。三个图形桌面各通过 20 次窗口拖动，每次按位移后的文字区域核对实际终点，调用耗时为 1.25–1.29 秒。两台 Windows 各完成 50 轮桌面右键、左键刷新，逐轮核对菜单展开与关闭；Firefox 完成 50 组页面切换，共 100 次鼠标点击。

YY 在两台 Windows 中各通过 50 轮临时文本替换和鼠标焦点切换，共 900 字符；另核对无边框窗口往返拖动。测试后取消设置，重新打开确认仍为“不使用代理”、临时输入框已清空。

中文在 Windows 记事本、YY 临时代理设置，以及 Ubuntu 文本编辑器、Firefox 设置搜索中通过来宾拼音输入法选词验证。四个 VMware 内置端点均未协商 UTF-8 Extended Clipboard；本轮结果不代表直接发送 Unicode keysyms 或任意表情可用。

可选来宾剪贴板后端的 Windows / Linux 夹具读回验证来自 0.3.2，本轮纯 VNC 测试未复验该通道。原生 UTF-8 剪贴板和 Hextile / RAW 的 TigerVNC X11 覆盖来自 0.2.0。Ubuntu 的原生 Wayland 剪贴板准备曾超时，不能据此宣称该后端可用。

本机报告、调用记录及截图保存在 `checks/results/`，不随包发布。有限测试不证明多日稳定性、所有应用或受保护画面均可用，也不构成完整安全审计。

## 自动检查

`scripts/Test.ps1` 的 162 项检查覆盖人工 RFB 连接、像素、输入协议、按键节奏、拖动顺序与终点、预算、生命周期、多会话、队列、取消、诊断、Unicode 通道和画面条件。默认不启动或访问真实虚拟机。分类见 [检查工具](../checks/README.md)，自动运行状态见 [GitHub Actions](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml)。

## 实机验收

仅测 VNC 时，选择已有系统应用和常用第三方应用，在自建文件或可取消的临时输入框中测试。明确使用 `method="keys"`，先检查目标应用的输入法状态，再输入并等待画面更新。重复输入需每轮核对文字；菜单操作核对展开与关闭；窗口拖动核对实际终点。控制台来宾只计入实际完成的键盘范围。

需要独立应用读回时，可另外进行夹具验收；以下流程包含可选来宾辅助工具，不属于纯 VNC 测试：

1. 明确目标地址及端口，准备独立测试窗口。`checks/fixtures/input_windows.ps1`、`input_linux.py` 提供文本、按钮、拖动和滚轮区域，并记录真实事件；简单文本或画面检查也可使用 textbox / visual 夹具。
2. Windows 文字窗口在已登录桌面用系统 PowerShell 5.1 / STA 运行；Linux 窗口在目标显示会话用 Python 3 运行。来宾启动和文件传递使用该环境已有的工具，按夹具参数指定临时文件。
3. 显式连接 VNC，截图确认窗口与坐标后提交输入或等待条件。核对应用结果；文本读回仅作预期的 CRLF/LF 规范化，不能把剪贴板确认代替应用读回。
4. 四来宾完整输入验收使用 `checks/vnc_mcp_input_live.py --allow-live`，配合私有 manifest 和 profile；GTK 坐标验收使用 X11，Wayland 会话可运行 Xwayland 夹具。多桌面只读检查使用 `vnc_mcp_multisession_live.py`，持续截图检查使用 `vnc_mcp_soak.py --allow-live`。参数见 [检查工具](../checks/README.md)。
5. 失败或取消时停止，不自动重放输入。结束后断开测试连接，关闭自己的测试窗口并删除自己的临时文件。

## 协议与实现参考

- [RFB 与 Extended Clipboard](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst)
- [vncdotool](https://github.com/sibson/vncdotool)
- [Pillow ImageChops](https://pillow.readthedocs.io/en/stable/reference/ImageChops.html)
