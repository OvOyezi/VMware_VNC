# 产品方向与设计

本项目让 AI 从真实画面确认目标、提交输入，再根据新画面决定下一步。稳定约束见 [AGENTS.md](../AGENTS.md)，接口见 [操作参考](../references/operations.md)，实际验证范围见 [兼容性](COMPATIBILITY.md)。项目独立开发、版本管理和维护，不依赖其他 MCP 项目的安装或发布。

## 核心框架

MCP 与 CLI 调用同一个 `VNCApplication`，由它拥有配置、会话注册表和可选 profile。创建应用或 MCP 服务不会打开连接、启动 RFB 线程或读取来宾配置；profile 仅在显式使用时加载。每个应用实例独立，关闭时回收会话，清理失败会报告错误并允许重试。

| 模块 | 职责与边界 |
| --- | --- |
| `config.py` / `application.py` | 校验环境配置、解析明确目标、惰性选择可选来宾 profile；统一 MCP 与 CLI 的路由和生命周期 |
| `server.py` / `cli.py` | MCP 工厂与辅助命令行适配，不分别实现 VNC 业务；宿主无需安装 Codex skill |
| `sessions.py` / `operations.py` | 具名会话、连接代次、有界队列、取消、空闲回收和输入清理；失败输入不重放 |
| `client.py` | 单桌面的截图、键鼠、文本及像素观察；连接范围内维护已按住输入和参考帧 |
| `runtime.py` | 同步会话到 asyncio 的明确边界；共享 I/O 线程按连接引用计数，最后关闭时结束，超时后取消并等待任务退出 |
| `transport.py` | TCP、TLS、WebSocket、WSS 字节流；WebSocket 复用固定版本的 `websockets`，TLS 使用 Python `ssl` 并验证证书 |
| `rfb.py` / `rfb_codec.py` | RFB 握手、认证、扩展及有界无损矩形解码；只声明实现过的能力，保留 RAW 回退 |
| `frames.py` | 跨消息累积首帧与 resize 像素，只发布所需区域已初始化的快照；CopyRect 必须来源于已知像素 |
| `keys.py` | 组合键、别名、修饰键与 Unicode keysym 的统一解析；按物理 keysym 拦截冲突，完整组合键按反序释放 |
| `imaging.py` / `visual.py` / `inspection.py` | 原生像素、坐标、帧标识、参考比较、采样等待和明确标注的细节图 |
| `guest.py` / `clipboard.py` | VNC 剪贴板及显式选择的来宾文本增强；Linux 在写入前固定选择 X11 或 Wayland，歧义目标拒绝猜测，失败不切换后端重试 |

控制连接状态与有效画面分开管理。ServerInit 为 0×0，或认证后在有界等待内仍未收到完整首帧时，连接可保持打开并报告 `connected=true`、`display_state="unavailable"`；非零 ServerInit 也不能代替完整首帧。解码器只发布已初始化的完整像素，不把部分首帧或 resize 画面暴露为截图。捕获超时会清除该连接缓存的 frame/reference，返回明确的 fresh/no cached image 错误并使状态保持 `unavailable`；只有之后成功取得完整画面才变为 `ready`。

收到分辨率变化通知后，在重新获取画面前拒绝使用旧尺寸发送指针输入。非零 resize 期间，已发布的旧 snapshot 继续保留原尺寸，直到新尺寸的完整画面发布；之后的新截图不会把旧图冒充当前帧。0×0 `DesktopSize` 和捕获超时会使可用画面及其检查引用失效。PNG 保留解码后的 RGB8 像素，放大差异图单独标明颜色增强。

`auto` 截图先尝试当前连接刷新；针对不响应后续请求的端点，再使用共享短连接取得完整首帧。短连接复用同一个端点和传输配置。短连接也必须重新通过认证和帧完整性检查，不能把没有有效帧缓冲的情况掩盖成成功截图。

文本的 `auto` 路径只选择已协商的 UTF-8 VNC 剪贴板或明确绑定的来宾 profile。没有可用剪贴板时在任何焦点点击和输入事件前拒绝，避免把 ASCII 误当作不受输入法影响的文字；纯键盘事件仍可由调用方在目视确认输入模式后显式请求 `method="keys"`。这种结果标记为输入法敏感，应用结果仍须观察确认。

`vnc_wake` / CLI `wake` 是明确的两次 pointer motion 尝试，禁止点击、键入或解锁；`wake_sent` 只表示输入已发出，之后须重新截图。它可用于尝试恢复待机桌面，但不保证强制关闭的显示服务响应；Ubuntu GNOME 在 `PowerSaveMode=3` 时只发 VNC pointer motion 未能恢复画面，须由来宾侧恢复显示服务。锁屏或受保护桌面也不由 wake 解锁。

## 成熟项目对照与复用

以下依据官方资料与源码，核对日期为 2026-10-23。功能对照不等于完成各服务端的兼容验收。

| 项目 | 适用能力 | 本项目的选择与理由 |
| --- | --- | --- |
| [noVNC](https://github.com/novnc/noVNC) / [RFB API](https://github.com/novnc/noVNC/blob/master/docs/API.md) | 浏览器 JavaScript RFB 库，成熟的连接状态、编码、输入及扩展处理；通过 WebSocket 连接 | 参考消息顺序、帧更新与扩展协商。直接嵌入需要 JavaScript／浏览器桥接，再把像素和取消状态传回 Python，会扩大 AI 接管的运行边界；当前不引入人工浏览器客户端。新增标准 WebSocket 接口可直接连接合适的代理 |
| [TigerVNC](https://github.com/TigerVNC/tigervnc/blob/master/README.rst) | 原生客户端／服务端和成熟 RFB 解码、认证及扩展；主体采用 GPL | 用于协议和互操作行为对照。整合原生库还需 Windows 构建、绑定、取消及授权评估；本次没有复制其实现代码。TCP 消息和编码仍按公开协议验证 |
| [KasmVNC](https://github.com/kasmtech/KasmVNC/tree/v1.5.0) | 面向浏览器远程桌面与服务端的完整系统；官方明确其协议已偏离标准 RFB，传统 VNC 客户端不能直接连接 | 不作为 VMware VNC 的直接替换后端。借鉴能力发现和运行状态诊断；不为其服务端和人工界面建立未验收的兼容层 |
| [websockify](https://github.com/novnc/websockify) | WebSocket 与 TCP 之间的成熟二进制代理，支持可选 TLS、认证与目标路由 | 保留为可选代理并使用真实进程做互操作检查；不复制代理实现，也不强制启动代理。本项目直接使用 [websockets asyncio 客户端](https://websockets.readthedocs.io/en/17.3/reference/asyncio/client.html)，避免自行实现 WebSocket 握手、帧解析和关闭协议 |
| [LibVNCServer / LibVNCClient](https://github.com/LibVNC/libvncserver/blob/master/README.md) | 原生 C 客户端／服务端库及丰富编码 | 是协议核心复用候选，替换前须验证许可、Windows 二进制分发、绑定和取消边界；当前不额外引入这套原生依赖 |
| [公开 RFB 协议](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst) | 消息、编码及扩展格式 | 作为线协议依据；不把某个服务端的扩展当成 VMware 的必备能力 |

当前 Python RFB 核心保留的理由是能够直接控制完整像素发布、连接代次、取消后的排空和输入清理，且无需浏览器或额外原生构建链。这个选择仍有协议维护成本，因此解码与传输分层，并以独立人工服务验证消息和像素；需要新增认证、编码或扩展时继续比较成熟实现，不能因为已有代码就默认继续自研。TCP／TLS 使用标准库，图像与压缩复用 Pillow／zlib，认证复用 cryptography，WebSocket 复用 websockets，MCP 复用官方 SDK。

## 端点与依赖

基础接管只要求可达且受支持的 VNC 端点、相应认证和有效画面，不要求指定来宾 OS、系统账号或来宾辅助服务。VNC 密码与来宾密码是不同条件。BIOS、启动、安装及登录画面的可用性按实际端点验收。

原生连接可以提供 host／port 或 `vnc://` URL；TLS 使用 `vnc+tls://`，代理使用 `ws://` 或 `wss://`。TLS 默认验证证书，可显式指定 CA 文件，不提供跳过验证的开关。URL 不允许内嵌账号密码；公开状态只含传输类型、主机和端口，路径及查询参数不写入状态结果。TLS 不等同于 RFB 的 VeNCrypt／X509 安全类型，本项目未因此增加这些认证协议。

可选 VMware profile 绑定原生 host／port，不能用代理 URL 推定其来宾身份。只有显式选择该 profile 时才允许来宾文本增强。外部 CLI 可组合使用；它们缺失不能阻断直接 VNC 截图和输入。

Linux 剪贴板 profile 的 `clipboard_backend` 可选 `auto`、`x11` 或 `wayland`，`wayland_display` 可明确指定当前账号运行时目录内的 socket。自动模式有显式 X11 display 时固定选择 X11；否则在继承的 X11 display 与工具可用时优先 X11，Wayland 只接受归属当前账号的明确 socket 或唯一候选。`wl-copy` 和 `wl-paste` 使用同一个显式文本 MIME；上游 [wl-clipboard BUGS](https://github.com/bugaevc/wl-clipboard/blob/master/data/wl-clipboard.1) 说明临时 surface 焦点和 MIME 推断都可能造成限制，因此实现采用有界失败、清理本次子进程组和固定原因标签，不在失败后改走另一后端。新 backend 仍须在实际 VMware Wayland 桌面验收，不能据此宣称 GNOME 原生 Wayland 的焦点限制已解决；明确使用 Xwayland 与 xclip/xsel 是复用现有工具的兼容选项。

## 后续规划与验收

| 工作方向 | 验收条件 |
| --- | --- |
| VMware 基础能力与资源稳定性 | 保留合成协议与取消回归；实机分别核对画面、坐标、输入结果和清理；启动阶段、长时间运行分别记录 |
| 更多 AI 宿主 | 实际完成 stdio 初始化、工具发现、图片接收、连续会话及清理；SDK 测试不能代替具体 AI 产品验收 |
| 新认证、编码及服务端 | 先确认 AI 任务收益，再比较成熟库、维护成本与可复现测试；其他服务端正式支持仍待定 |
| 必要的视觉设计 | 服务原生坐标、原色、差异辨认与结果核对，不预设建设通用人工远控客户端 |
