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

## 已有实机覆盖

0.3.2 在四台前台运行的 VMware 来宾中各通过 23 项检查，另有 6 项跨会话检查，共 98 项，零失败、零跳过。每台均核对完整/区域截图、移动、单击/双击、拖动、滚轮、键盘事件、ASCII、中文/表情/多行文字、画面条件及持键时拒绝文本；文字以应用 JSON 精确读回验证。

| 环境 | 已验证 | 覆盖缺口 |
| --- | --- | --- |
| VMware / Windows 11 | 全套 23 项，系统 PowerShell / STA 准备剪贴板，WinForms 应用读回 | 不代表所有 Windows 应用都接受相同粘贴方式 |
| VMware / Windows 10 | 全套 23 项，系统 PowerShell / STA 准备剪贴板，WinForms 应用读回 | 同上；未覆盖所有受保护桌面 |
| VMware / Ubuntu 26.10 / GNOME Wayland | 全套 23 项使用 Xwayland GTK 3 夹具；另在原生 Wayland GTK 应用中精确读回 Unicode 文本 | 剪贴板明确使用 X11 / xclip 桥接；本环境 wl-copy / wl-paste 准备超时，不能据此宣称原生 Wayland 后端可用 |
| VMware / Debian 13.6 / Openbox X11 | 全套 23 项，xclip 准备剪贴板，GTK 3 应用读回 | 未覆盖其他 Linux 桌面组合 |

本轮 VMware 内置服务未协商 UTF-8 Extended Clipboard，Unicode 使用显式绑定的来宾剪贴板后端。原生 UTF-8 剪贴板与 Hextile / RAW 像素一致性的 TigerVNC X11 实机覆盖来自 0.2.0，未在本版重复实测。其他服务端仍需单独验收。

四台并行的三分钟只读检查完成 731 次截图，含计划重连与旧 ID 拒绝，未出现错误。默认人工服务回归共 159 项通过。有限时长测试不证明多日稳定性或所有网络故障下的行为。当前自动检查见 [GitHub Actions](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml)，本机报告与截图在 `checks/results/`，不随包发布。

## 自动检查

`scripts/Test.ps1` 覆盖人工 RFB 连接、像素、输入协议、生命周期、多会话、队列、取消、诊断、Unicode 通道和画面条件。默认不启动或访问真实虚拟机。分类见 [检查工具](../checks/README.md)。

## 实机验收

1. 明确目标地址及端口，准备独立测试窗口。`checks/fixtures/input_windows.ps1`、`input_linux.py` 提供文本、按钮、拖动和滚轮区域，并记录真实事件；简单文本或画面检查也可使用 textbox / visual 夹具。
2. Windows 文字窗口在已登录桌面用系统 PowerShell 5.1 / STA 运行；Linux 窗口在目标显示会话用 Python 3 运行。来宾启动和文件传递使用该环境已有的工具，按夹具参数指定临时文件。
3. 显式连接 VNC，截图确认窗口与坐标后提交输入或等待条件。核对应用结果；文本读回仅作预期的 CRLF/LF 规范化，不能把剪贴板确认代替应用读回。
4. 四来宾完整输入验收使用 `checks/vnc_mcp_input_live.py --allow-live`，配合私有 manifest 和 profile；GTK 坐标验收使用 X11，Wayland 会话可运行 Xwayland 夹具。多桌面只读检查使用 `vnc_mcp_multisession_live.py`，持续截图检查使用 `vnc_mcp_soak.py --allow-live`。参数见 [检查工具](../checks/README.md)。
5. 失败或取消时停止，不自动重放输入。结束后断开测试连接，关闭自己的测试窗口并删除自己的临时文件。

## 协议与实现参考

- [RFB 与 Extended Clipboard](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst)
- [vncdotool](https://github.com/sibson/vncdotool)
- [Pillow ImageChops](https://pillow.readthedocs.io/en/stable/reference/ImageChops.html)
