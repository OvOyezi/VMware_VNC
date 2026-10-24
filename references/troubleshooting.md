# 排查与已知限制

先记录实际 host、port、session_id、connection_id、截图模式和结构化错误。不得记录密码。遇到失败先确认服务与当前状态，不通过重复输入推断问题。

| 现象 | 核对与处理 |
| --- | --- |
| 当前聊天没有 VNC 工具 | 区分技能已安装、MCP 已登记、进程初始化成功、当前聊天工具已加载。按 setup.md 逐层验证 |
| 连接拒绝 / 超时 | 核对目标地址、端口和 VNC 服务监听。IP 可达不证明 VNC 可用；来宾剪贴板后端的认证另行检查 |
| 已连接但 `display_state="unavailable"` | 0×0 ServerInit、初始首帧缺失/不完整超时、0×0 resize 或后续捕获超时都可能使显示不可用；控制连接仍可保持 `connected=true`。不完整帧不会返回。释放保持的按键或按钮后，可显式调用 `vnc_wake`（CLI 使用 `wake`），再请求新截图并检查 |
| VMware refresh 截图超时 | 默认 `auto` 会在已有连接上做一次有界完整刷新；失败后用短暂共享连接取得完整首帧。也可显式选 `fresh`。它不是缓存截图，也不保证更少网络流量 |
| 截图超时或报告没有缓存图像 | 捕获超时会清除最近 frame/reference，保持连接的状态变为 `unavailable`；错误明确表示没有完整新帧且没有返回缓存图。释放保持输入，尝试 `vnc_wake` 后重新截图；只有完整新截图成功才恢复 `ready`。0×0 `DesktopSize` 也会使旧画面失效 |
| VMware / GNOME 空闲后报告 0×0 或无法恢复 | 先尝试显式 `vnc_wake` 并重新截图；若仍无可用帧，检查来宾显示节能状态。来宾显示服务强制关闭时，可能需要从来宾侧恢复显示。wake 不解锁 |
| connection_id 失效 | 重连、断线或空闲回收会改变连接。确认目标后显式重连，保存新 ID 并截图；不要自动重放旧输入 |
| 队列满 / 操作超时 / 取消 | 停止提交后续输入，检查状态与新截图。取消为协作式，不能撤回已发送输入，也不能强行抢占单步网络等待与编码 |
| 字母被输入法转换 | `method="keys"` 会经过当前来宾输入法，工具不切换输入法。先截图确认目标控件的焦点和输入模式，再重新观察；不要盲发整段文字 |
| 连续输入缺字 | 显式键盘路径至少间隔 20 毫秒。先检查已有文字，再决定下一步；必要时只对 `method="keys"` 增加 `delay_ms`、分段输入并等待应用处理 |
| 拖动超时 | 检查动作预算、网络响应与 MCP 服务进程版本。超时后先重连截图，确认实际窗口位置，不重放旧动作 |
| `method="auto"` 拒绝文本 | 自动路径只选已协商 UTF-8 剪贴板或绑定 profile；没有任一通道时 ASCII 和 Unicode 都会在点击焦点及输入前拒绝。查看 capabilities，或显式配置 guest profile；若目标是键盘事件，先目视确认输入法后指定 `method="keys"` |
| 键盘文字与预期不同 | `method="keys"` 结果带 `input_method_sensitive=true`，且 `application_verified=false`。检查该控件的输入模式并重新截图；工具不会自动切换 IME，也不保证字面文本 |
| `clipboard_verified=true` 但控件没有文字 | 校验只证明剪贴板已准备。检查 focus 坐标、当前窗口、粘贴快捷键与应用是否接受粘贴；不要自动再发同一段 |
| Linux 来宾剪贴板后端报告 Tools 不可用 | 检查发行版是否把 guest operations 拆成独立软件包；基础服务运行与来宾辅助通道可用须分别核对 |
| Linux 剪贴板准备超时 | 查看固定阶段、后端及原因标签。后端在写入前选定，失败不会切换后端或重新发送文字；本次创建的剪贴板子进程组会在失败时回收。GNOME 原生 Wayland 的焦点策略仍可能受 compositor 限制，且新 backend 尚未完成 VMware 实机验收。可明确指定 Xwayland `display`、`clipboard_backend="x11"` 并使用 xclip/xsel，之后在应用内重新验收 |
| Wayland socket 歧义或不可用 | 只允许使用当前账号拥有的 `XDG_RUNTIME_DIR` socket。多个候选时设置 `wayland_display` 明确选择；配置的 socket 必须是该目录内当前账号拥有的 socket。也可显式选用 Xwayland X11 后端；失败不会自动换后端 |
| RFB protocol rejected | 原生 asyncio RFB 校验发现协议或压缩内容不受支持，连接会停用；保留结构化原因，确认服务端与目标后再显式重连。Tight JPEG 矩形明确不支持；ZRLE、Tight 的无损内容由项目解码器处理 |
| `vnc_inspect` 报 frame_id unknown/expired | 最新捕获帧或连接已变更。重新截图并使用新元数据中的 `frame_id`；若要保留对比参考，单独用 `remember=true` 和 `reference_id` |
| 放大的差异图颜色与桌面不符 | `difference_amplified` 将每通道绝对差乘以 16 并截到 255，颜色被刻意改变。核对原色 current/reference 图或 points 返回的 RGB 与 delta |
| `source_rgb_sha256` 与 JPEG 文件不同 | 哈希针对编码前的 RGB8 源区域；JPEG 有损。要求精确像素时使用默认 PNG，并核对源像素元数据 |
| 点击位置偏移 | 核对本次截图原点、原生桌面尺寸与多屏位置；区域图内坐标要加原点，不用显示预览的缩放尺寸 |
| 移动技能后无法启动 | 在新目录重新运行 Setup，重新生成 MCP 配置，核对 command/args/cwd。虚拟环境不能直接搬运 |

密码可以由服务环境提供，错误报告中不回显。不要为排查方便关闭防火墙、启用空密码登录或改动未授权虚拟机。

默认回归测试：`pwsh -NoProfile -File <skill-root>/scripts/Test.ps1`。它验证模拟协议、无损编码、UTF-8 扩展、来宾调用边界、生命周期、队列和会话隔离。`vnc_diagnostics` 报告 `mcp`、Pillow、cryptography 和 native asyncio RFB 引擎状态；不会联系 VNC 端点。真实桌面文字与耐久测试单独验收，具体范围见 [兼容性记录](../docs/COMPATIBILITY.md)；不以有限时长测试承诺长期永不出错。

模块职责与状态管理见 [核心架构](../docs/DESIGN.md)。

TLS／WSS 证书失败时核对目标主机名、系统时间和 CA 文件；客户端不会跳过证书验证。WebSocket 代理需协商 binary 子协议并发送二进制消息，普通 KasmVNC 浏览器地址不等同于标准 RFB 代理。URL 错误只返回固定诊断，不回显其路径、查询或内嵌凭据。

首帧或 resize 未完整覆盖所需区域时，客户端会继续累积并请求更新，直到完整或超时；旧画面不会伪装成新尺寸截图。最后一个连接关闭后 RFB I/O 线程应退出，可通过诊断中的 `lease_count` 和 `io_threads` 核对。清理失败会报告错误，不把未释放的资源当成已关闭。
