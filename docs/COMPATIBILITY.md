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

下列范围来自 0.2.0 / 0.3.0 的验收，用于界定已知覆盖，不代表每个后续版本都已重复实测：

| 环境 | 已验证 | 覆盖缺口 |
| --- | --- | --- |
| VMware / Windows 11 | 截图、多会话、来宾辅助 Unicode 文本精确读回、画面条件 | 不代表所有 Windows 应用都接受相同粘贴方式 |
| VMware / Windows 10 | 截图、多会话、画面条件 | 未作为 Unicode 文本输入验收目标 |
| Linux / TigerVNC Xvnc / X11 | 原生 UTF-8 与 VMware 辅助文本通道精确读回，Hextile / RAW 像素一致性 | 新增画面条件未在该环境实测 |
| Wayland、其他桌面与其他 VNC 服务 | 有相应通道实现或协议检查 | 尚无实机兼容性结论 |

有限时长测试不证明多日稳定性或所有网络故障下的行为。当前发布版本的自动检查结果见 [GitHub Actions](https://github.com/OvOyezi/VMware_VNC/actions/workflows/checks.yml)，本机检查结果写入 `checks/results/`，不随包发布。

## 自动检查

`scripts/Test.ps1` 覆盖人工 RFB 连接、像素、输入协议、生命周期、多会话、队列、取消、诊断、Unicode 通道和画面条件。默认不启动或访问真实虚拟机。分类见 [检查工具](../checks/README.md)。

## 实机验收

1. 明确目标地址及端口，准备独立测试窗口。`checks/fixtures/textbox_windows.ps1` 和 `textbox_linux.py` 可把实际收到的文本写入指定 JSON；`visual_windows.ps1` 提供可控变化区域。
2. Windows 文字窗口在已登录桌面用系统 PowerShell 5.1 / STA 运行；Linux 窗口在目标显示会话用 Python 3 运行。来宾启动和文件传递使用该环境已有的工具，按夹具参数指定临时文件。
3. 显式连接 VNC，截图确认窗口与坐标后提交输入或等待条件。核对应用结果；文本读回仅作预期的 CRLF/LF 规范化，不能把剪贴板确认代替应用读回。
4. 多桌面只读验收使用 `checks/vnc_mcp_multisession_live.py`；持续截图检查使用 `vnc_mcp_soak.py --allow-live`，均显式指定端点。参数见 [检查工具](../checks/README.md)。
5. 失败或取消时停止，不自动重放输入。结束后断开测试连接，关闭自己的测试窗口并删除自己的临时文件。

## 协议与实现参考

- [RFB 与 Extended Clipboard](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst)
- [vncdotool](https://github.com/sibson/vncdotool)
- [Pillow ImageChops](https://pillow.readthedocs.io/en/stable/reference/ImageChops.html)
