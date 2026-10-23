# 兼容性与验收

本页按版本与环境汇总已有验收结论、限制和复现方式。设计方向见 [设计说明](DESIGN.md)，检查工具与完整参数见 [checks/README.md](../checks/README.md)。

## 支持范围

| 层次 | 当前范围与验证边界 |
| --- | --- |
| 安装宿主 | 现有维护脚本面向 Windows，要求 PowerShell 7；Python 运行时要求 3.11+。GitHub Actions 配置了 Windows / Python 3.11、3.15 检查及 Ubuntu / Python 3.11 POSIX helper job；本机安装脚本仍只面向 Windows |
| 虚拟化环境 | 主要面向 VMware 内置 VNC 控制台；其他虚拟化平台或 VNC 服务端是否正式支持仍待定 |
| 来宾系统 | 基础画面与键鼠不要求特定 OS、系统账号、Tools 或额外来宾服务；实际可操作范围由端点画面、输入协议及目标应用决定 |
| 启动与安装阶段 | 原理上可操作端点提供的有效画面，但 BIOS、启动、安装和登录阶段尚无完整分阶段验收，不据此宣称全覆盖 |
| AI 宿主 | 接口采用 MCP stdio，不依赖 Codex。合成检查通过标准 MCP Python 客户端调用；各类 AI 编程助手的完整接入兼容性尚须分别验收 |
| CLI | 持续维护的辅助入口，与 MCP 共享核心；默认回归覆盖单命令行为。外部控制 CLI 不作为基础 VNC 操作的依赖 |

VNC 认证与来宾系统认证是不同条件。“不要求来宾账号”不表示免除 VNC 认证，也不表示没有有效帧缓冲时仍可截图。纯 VNC 实测未调用来宾辅助后端；这也不等于已完成卸载 Tools、移除全部外部 CLI 的环境对照验收。

## 能力与限制

| 能力 | 条件与限制 |
| --- | --- |
| RFB 传输 | 0.5.0 分离字节流、运行时、帧缓冲与解码，直接连接 TCP 或验证证书的 TLS，并可选 WS／WSS；标准 WebSocket 复用 websockets |
| 无损矩形编码 | auto 提供 RAW、CopyRect、Hextile、ZRLE 和 Tight 无损子集，服务端决定实际编码；显式偏好保留 RAW 回退。拒绝 Tight JPEG |
| 截图策略 | auto 先在已有连接上尝试一次有界完整刷新，失败后从共享短连接取完整首帧；可显式选择 refresh / fresh。只发布完整帧，首帧未完成或捕获超时不会返回部分图或缓存旧图 |
| PNG 与像素 | 默认 PNG 保留原生尺寸和服务端帧缓冲 RGB8；不承诺显示器 HDR、色彩配置或 AI 模型端到端逐像素感知。JPEG 是显式有损选项；Codex 图片元数据仅为可选宿主提示 |
| 像素检查 | vnc_inspect 提供原生坐标 RGB、参考差异和原色细节图；difference_amplified 经过颜色增强，不能代替原图 |
| 帧复用 | frame_id 只复用本连接最新捕获帧，不发网络刷新，并报告捕获时间与帧龄；新捕获或重连使旧 ID 失效。捕获超时清除 frame/reference 并使连接显示状态为 unavailable；完整截图成功后恢复 ready |
| Fence / ContinuousUpdates | 有协商与处理路径，合成检查覆盖；不自动开启 ContinuousUpdates，既有 VMware 验收端点未协商这些扩展 |
| 原生 UTF-8 剪贴板 | 须由服务端协商 Extended Clipboard；没有扩展时不能把 Unicode keysyms 当作通用中文输入方式 |
| 自动文本通道 | 只选择已协商 UTF-8 剪贴板或显式绑定的 profile；两者都没有时，ASCII 和 Unicode 都在焦点点击及输入前拒绝。纯 VNC 键盘须显式 `method="keys"` |
| 纯 VNC 键盘文字 | 经过来宾输入法，结果标记 `input_method_sensitive=true`，且 `application_verified=false`；可按已配置输入法逐键操作，但不保证字面文本 |
| 可选来宾剪贴板 | 显式配置的 VMware 文本增强，需要 vmrun、Tools、凭据及来宾剪贴板工具；Linux profile 可选择 X11 或 Wayland，自动选择固定在写入前，不会在失败后切换并重发。POSIX 清理与缺少工具时拒绝已分别验证；Wayland/X11 成功粘贴仍未实测，见 [文本输入](../references/text-input.md) |
| 显示状态与唤醒 | 0×0 ServerInit、未完整的初始帧、0×0 resize 或捕获超时可保留已认证连接并报告 `display_state="unavailable"`；不暴露部分帧或旧缓存。显式 `vnc_wake` 只发两次指针移动，须再完成截图才回到 ready；它不解锁，也不保证恢复强制关闭的显示 |
| 画面条件 | 采样比较可能遗漏采样间的短暂变化；匹配、稳定、输入已发送和剪贴板已校验均不证明应用任务完成 |
| 区域与带宽 | 服务端可返回大于请求范围的区域；裁剪、压缩或缓存检查不自动证明网络流量降低 |
| 连接安全 | 普通 TCP／WS 不加密；TLS／WSS 验证证书与主机名，可指定 CA。未实现 VeNCrypt 等新增 RFB 安全类型；锁屏、受保护桌面及特殊应用须分别验证 |

## 0.5.0 验收记录

以下结果按具体路径列出。人工 RFB wire、POSIX helper、独立安装和真实 VMware 桌面是不同验证层，不能互相替代；表格不把各自检查数相加成一个默认回归总数。

| 证据 | 环境 | 已验证范围与边界 |
| --- | --- | --- |
| 默认合成回归 | Windows 11 宿主 / Python 3.15，2026-10-24 | 276 项通过，包含 MCP、CLI、传输、会话隔离、文本与显示恢复。Windows 跳过的 2 项 POSIX 进程组检查在 Ubuntu 来宾中另行实测；默认回归不操作真实虚拟机 |
| RFB 恢复检查 | Windows 11 宿主 / Python 3.15 / 人工 RFB wire | 11 项通过：0×0 ServerInit、无首帧或部分首帧超时仍保持认证连接并报告 unavailable，不暴露不完整画面；捕获超时清除 frame/reference 并报告没有 fresh/cached 图像；wake 后仍 unavailable，完整新截图成功后才 ready。这是协议 wire 证据，不保证 VMware 显示一定能被唤醒 |
| 非 editable wheel 安装 | 独立环境，固定依赖与重建 wheel | 5 项通过：安装后的包资源、CLI、MCP、像素与输入路径；不从工作区源码导入 |
| websockify 互操作 | websockify 0.13.0 / 本机人工 RFB | 3 项通过，覆盖真实代理上的连接及 MCP/CLI 路径；不代表所有 WebSocket 代理均已兼容 |
| Linux POSIX 剪贴板 helper | Ubuntu / POSIX 子进程运行 | 2 项通过：子进程超时后回收本次进程组；剪贴板读回不匹配后回收仍在运行的写入 owner。它们验证失败清理，不证明成功粘贴 |
| Ubuntu 来宾文本拒绝路径 | VMware / Ubuntu GNOME，未安装 xclip 与 wl-clipboard | guest 文本调用明确返回 `clipboard_utility_unavailable`，未发送粘贴；该结果验证缺依赖时的拒绝，不构成 X11 或原生 Wayland 成功路径验收 |
| Ubuntu GNOME 强制省电 | VMware / Ubuntu GNOME，`PowerSaveMode=3` | VNC pointer-only wake 未能恢复显示；测试驱动从来宾侧恢复显示服务后才重新取得截图。VNC 不承诺恢复强制关闭的显示或解锁 |
| VMware 纯 VNC 操作 | Windows 10、Ubuntu GNOME、Debian tty，2026-10-23 | 多会话截图、逐键文字、组合键释放、窗口拖动和画面等待；Debian 未提交登录。各桌面实际覆盖以该表述为限 |
| Windows 11 输入法边界 | VMware / Windows 11 图形桌面，2026-10-24 | 英文输入模式下曾完成 15 字符输入；进入编辑控件后输入法也会切换为中文并改写显式 `method="keys"` 文本。键盘模式不锁定 IME，也不保证字面结果 |
| MCP 接口 | 0.5.0 | 当前注册 24 个工具；具体工具及状态语义见 [操作参考](../references/operations.md) |
| GitHub Actions 配置 | Windows / Python 3.11、3.15；Ubuntu / Python 3.11 POSIX helper | Ubuntu helper job 已配置用于运行 POSIX 检查；配置本身不等于远端 workflow 已验收 |

## 历史兼容性证据

这些记录说明对应版本和端点上的具体覆盖，不扩大当前支持承诺。

| 版本与证据 | 环境 | 已验证范围与边界 |
| --- | --- | --- |
| 0.4.0 合成检查 | 本机人工 RFB 服务、mock 与真实 MCP stdio 通道 | 已有记录通过 216 项默认检查，涵盖协议、会话、CLI、取消、文本、像素和画面条件；不接触真实虚拟机 |
| 0.4.0 纯 VNC，2026-10-23 | VMware / Windows 11、Windows 10、Ubuntu 图形桌面 | PNG 原色与来源像素哈希、单像素单通道差 1 的检测、逐键文字、窗口拖动和拼音选词；未使用来宾命令或剪贴板辅助后端 |
| 0.4.0 纯 VNC，2026-10-23 | VMware / Debian 13.6 tty | 大小写、数字、符号和编辑键；不提交登录，不计入鼠标、图形应用或中文覆盖 |
| 0.4.0 短时只读检查 | 上述四个 VMware 端点 | 多会话截图、计划重连、旧连接 ID 拒绝和清理；未完成多日运行验证 |
| 0.3.3 历史覆盖，2026-10-21 至 22 | VMware / Windows 11、Windows 10、Ubuntu 26.10 GNOME；Debian 13.6 tty | 系统和第三方应用中的菜单、文本、窗口及重复操作；Debian 仅控制台键盘。不可替代 0.4.0 的全部应用回归 |
| 0.3.2 历史辅助通道 | VMware / Windows、Linux 夹具 | 来宾剪贴板文本及应用读回；0.4.0 纯 VNC 实测未复验该通道 |
| 0.2.0 历史互操作 | TigerVNC X11 | 原生 UTF-8 剪贴板与 Hextile / RAW；仅说明该次互操作范围，不构成其他服务端的正式支持承诺 |

已记录的 VMware 端点实际选择 RAW，未协商 Fence、ContinuousUpdates 或 UTF-8 Extended Clipboard。合成编码与扩展检查不证明 VMware 支持这些能力，也不能据此宣称带宽收益。

Ubuntu 原生 Wayland 与 Xwayland 剪贴板成功粘贴尚未实测。当前 VMware Ubuntu 环境缺少 xclip 和 wl-clipboard，因此 guest 文本被明确拒绝且未发送粘贴；POSIX helper 的超时和读回不匹配清理已通过上述独立检查。上游 [wl-clipboard BUGS](https://github.com/bugaevc/wl-clipboard/blob/master/data/wl-clipboard.1) 记录的 compositor 焦点和 MIME 推断限制仍适用。故障处理见 [排查说明](../references/troubleshooting.md)。

## 复现与新增验收

默认执行：

```powershell
pwsh -NoProfile -File ./scripts/Setup.ps1
pwsh -NoProfile -File ./scripts/Test.ps1
```

这仅验证合成协议与本地接口。真实 VMware 验收另行明确端点，基础路径不绑定来宾 profile、不通过外部 CLI 执行来宾操作：

1. 连接后立即截图，确认目标、原生尺寸、画面与输入位置；具名调用始终同时携带 session_id、connection_id。
2. 使用现有应用的可取消输入框或自建测试内容，分别核对文本、菜单、拖动和实际应用结果。通用自动文本只使用可用剪贴板通道；逐键文字显式选择 method="keys"，先检查输入法并复核应用内容；控制台只计入实际完成的键盘范围。
3. 分别记录版本、端点类型、来宾显示环境、AI 宿主、纯 VNC 或辅助通道及未执行项。失败或取消时停止，不重放；结束后断开连接并清理自己的测试内容。

需要独立应用读回时，可选择 [检查工具](../checks/README.md) 中的夹具验收；其中 input_live 驱动依赖 vmrun 和来宾结果文件，不属于纯 VNC 基础验收。新增的恢复与来宾剪贴板合成检查也不能替代 VMware 实机验收。只读多会话及持续截图工具也在该页说明。

新增 AI 宿主须实际完成初始化、工具发现、图片结果、连续会话与清理，不能仅凭“支持 MCP”标为已验证。新增服务端或启动阶段同样单列环境与限制；有限测试不构成所有应用、全部画面或长期稳定性的保证。
