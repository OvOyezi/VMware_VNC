# 排查与已知限制

先记录实际 host、port、session_id、connection_id、截图模式和结构化错误。不得记录密码。遇到失败先确认服务与当前状态，不通过重复输入推断问题。

| 现象 | 核对与处理 |
| --- | --- |
| 当前聊天没有 VNC 工具 | 区分技能已安装、MCP 已登记、进程初始化成功、当前聊天工具已加载。按 setup.md 逐层验证 |
| 连接拒绝 / 超时 | 核对目标地址、端口和 VNC 服务监听。IP 可达不证明 VNC 可用；来宾剪贴板后端的认证另行检查 |
| VMware refresh 截图超时 | 默认 `auto` 会在已有连接上做一次有界完整刷新；失败后用短暂共享连接取得完整首帧。也可显式选 `fresh`。它不是缓存截图，也不保证更少网络流量 |
| VMware / GNOME 空闲后连接超时或报告 0×0 | 先恢复来宾显示，再显式重连并截图。本轮 Ubuntu 实测中，显示节能会让 VMware 移除帧缓冲；fresh 也无法读取不存在的画面 |
| connection_id 失效 | 重连、断线或空闲回收会改变连接。确认目标后显式重连，保存新 ID 并截图；不要自动重放旧输入 |
| 队列满 / 操作超时 / 取消 | 停止提交后续输入，检查状态与新截图。取消为协作式，不能撤回已发送输入，也不能强行抢占单步网络等待与编码 |
| 字母被输入法转换 | 先截图确认目标应用中的输入法和焦点；Windows 可按应用保留不同状态。切换后重新观察，不盲发整段文字 |
| 连续输入缺字 | 核对运行版本；0.3.3 起逐键路径至少间隔 20 毫秒。先检查已有文字，再决定下一步；必要时增加 delay_ms、分段输入并等待应用处理 |
| 拖动约 5 秒后超时 | 0.3.3 已替换上游每步 200 毫秒的拖动实现。核对 MCP 已加载新版本，超时后先重连截图确认实际窗口位置，不重放旧动作 |
| 中文无法输入或粘贴 | 查看 capabilities。已有来宾输入法可通过纯 VNC 拼音选词；通用 Unicode 文本优先使用 UTF-8 扩展，或显式配置可选来宾 profile。按 text-input.md 检查通道、显示会话与焦点，不把 raw Unicode keysyms 当成通用解法 |
| clipboard_verified=true 但控件没有文字 | 校验只证明剪贴板已准备。检查 focus 坐标、当前窗口、粘贴快捷键与应用是否接受粘贴；不要自动再发同一段 |
| Linux 来宾剪贴板后端报告 Tools 不可用 | 检查发行版是否把 guest operations 拆成独立软件包；基础服务运行与来宾辅助通道可用须分别核对 |
| Linux 剪贴板准备超时 | 查看报告的阶段和后端，检查登录会话及对应剪贴板工具。GNOME 可显式选择 Xwayland 的 display 与 xclip/xsel；变更后重新验收，不自动重发失败文字 |
| RFB protocol rejected | 原生 asyncio RFB 校验发现协议或压缩内容不受支持，连接会停用；保留结构化原因，确认服务端与目标后再显式重连。Tight JPEG 矩形明确不支持；ZRLE、Tight 的无损内容由项目解码器处理 |
| `vnc_inspect` 报 frame_id unknown/expired | 最新捕获帧或连接已变更。重新截图并使用新元数据中的 `frame_id`；若要保留对比参考，单独用 `remember=true` 和 `reference_id` |
| 放大的差异图颜色与桌面不符 | `difference_amplified` 将每通道绝对差乘以 16 并截到 255，颜色被刻意改变。核对原色 current/reference 图或 points 返回的 RGB 与 delta |
| `source_rgb_sha256` 与 JPEG 文件不同 | 哈希针对编码前的 RGB8 源区域；JPEG 有损。要求精确像素时使用默认 PNG，并核对源像素元数据 |
| 点击位置偏移 | 核对本次截图原点、原生桌面尺寸与多屏位置；区域图内坐标要加原点，不用显示预览的缩放尺寸 |
| 移动技能后无法启动 | 在新目录重新运行 Setup，重新生成 MCP 配置，核对 command/args/cwd。虚拟环境不能直接搬运 |

密码可以由服务环境提供，错误报告中不回显。不要为排查方便关闭防火墙、启用空密码登录或改动未授权虚拟机。

默认回归测试：`pwsh -NoProfile -File <skill-root>/scripts/Test.ps1`。它验证模拟协议、无损编码、UTF-8 扩展、来宾调用边界、生命周期、队列和会话隔离。`vnc_diagnostics` 报告 `mcp`、Pillow、cryptography 和 native asyncio RFB 引擎状态；不会联系 VNC 端点。真实桌面文字与耐久测试单独验收，具体范围见 [兼容性记录](../docs/COMPATIBILITY.md)；不以有限时长测试承诺长期永不出错。

实现来源与适配差异见 [LOCAL-CHANGES.md](../LOCAL-CHANGES.md)。
