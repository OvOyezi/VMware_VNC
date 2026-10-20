# VMware VNC 深挖与优化记录

## 第三轮：多会话与资源管理（2026-10-20，已完成）

原来的单例连接已替换为显式 `session_id` 注册表：不同名称对应独立后端和调度器，同名重连只替换该槽位。`connection_id` 仍标识连接代次。具名会话除 connect/status/列表外的工具必须同时携带两种 ID，截图也会回传它们；省略会话名使用独立的兼容 default，会话选择不会随最近一次具名连接改变。

新增 `sessions.py` 负责路由和生命周期；`operations.py` 保持每会话有界调度，`client.py` 继续负责传输，`imaging.py` 负责编码。新增 `vnc_sessions`，工具总数 18；其状态明确标为缓存，并附查询年龄，忙碌会话不会阻塞列表。需要实时状态时调用 `vnc_status(session_id=...)`。

资源默认值为最多 4 个会话（包含使用中的 default）、每会话最多 16 个未完成调用（包含异步等待），空闲 300 秒后回收，扫描间隔不超过 30 秒。`VNC_MAX_SESSIONS=1..32`、`VNC_IDLE_TIMEOUT=0..86400` 可在下次启动前调整，0 禁用空闲回收；本轮未改 Codex 配置。活动调用、排队操作和等待持有生命周期引用，防止处理中回收。关闭期间保留槽位，清理失败时隔离容量，防止一边失败一边无限建立新连接；关闭时再重试清理。断开、失败认证及被取消而断开的会话会释放空槽位。空闲回收会释放保持的键鼠输入。

另一个已复现的根因是 vncdotool 1.4.2 的全局 reactor 启动缺少同步保护：两台首次并发连接，在网络线程启动被人为延迟时会启动两个 reactor 线程，并触发 `ReactorAlreadyRunning`。现在客户端创建路径串行经过首个 reactor 回调，之后的握手、I/O、截图编码和操作各自执行。独立子进程同样条件下由 2 个线程/1 个异常变为 1 个线程/0 异常；没有更改依赖或修补第三方安装目录。

验收结果：

- 原有 44 项重新执行通过，新增 21 项通过，合计 65 项模拟协议和后端检查。包括超时/取消/队列隔离、旧 ID 拒绝、默认会话独立、异步等待上限、空闲释放按键、回收竞态、失败清理隔离，以及 40 次连接/截图/断开后模拟 TCP 连接数回到 0。
- 在模拟桌面 A 截图故意不响应期间，B 的按键及截图完成约 4.3 ms；A 过载时，列表及 B 按键约 1.5 ms。这是本机模拟 RFB 数据，不代表 Windows 应用响应时间或长期负载保证。
- 按真实 Codex 注册配置启动一个 MCP 进程，同时连接 WIN11 5901 / WIN10 5902；各自初始图、三轮并行全图、区域图均成功。三次双桌面截图批次约 93/101/84 ms，随画面和宿主机负载变化，不作为保证或改前对照。
- 独立断开 WIN11 后，WIN10 保持原 `connection_id` 并继续截图；WIN11 重连获得新代次，旧 ID 请求被拒绝。两台截图已实际查看，本轮未向真实虚拟机发送键鼠输入。
- 实机单独 5 项断言通过，测试结束会话数为 0，Codex 配置哈希保持不变。没有修改 VMX、防火墙、密码、依赖或重启虚拟机。

证据：`checks/results/vnc-multi-smoke.json`、`vnc-multi-lifecycle.json`、`vnc-multi-deep-regression.json`、`vnc-multisession-checks.json`、`vmware-vnc-multisession-live.json`；汇总 `vnc-multisession-summary.json`。完整源码补丁 `vnc-mcp-multisession.patch`，相对第二轮的审阅补丁 `vnc-mcp-multisession-review.patch`，改动前备份 `checks/backups/vnc-multisession-20261020`。

当前聊天仍暴露第二轮的工具定义，尚未热重载。新启动的注册进程已验收；重新加载 MCP 或重启 Codex 后才能在对话中直接调用新参数和 `vnc_sessions`。没有把此项验收宣称为整个 Codex 应用重启或模型 WebSocket 问题已修复。

剩余空间：`fresh` 仍每次建立短截图连接，区域截图不会减少前端 RAW 帧传输。持久增量与压缩编码需先验证 VMware 的实际协议响应，不能用旧缓存冒充新画面。中文输入限制、真实网络故障和多小时稳定运行尚未完成新的验证。

## 第二轮：以下保留原始验收记录

日期：2026-10-20，Asia/Shanghai。范围：固定部署源码、MCP 调度、VNC 连接生命周期、截图与输入、CLI 入口及验证工具。本轮没有更改 Codex 全局配置、依赖版本、VMX 或防火墙，没有向真实虚拟机发送键鼠输入。

本轮 codebase-memory 的项目与覆盖查询均返回 `Transport closed`，因此使用当前源码逐项核对，并以真实 MCP stdio、回环 RFB 服务和实机只读测试补证；没有把旧图索引当作当前完整审计依据。

## 已复现并修复的问题

| 问题 | 修复前证据 | 当前实现与验证 |
| --- | --- | --- |
| MCP 取消后后台仍输入 | 模拟桌面在取消前收到 3 个字符，取消后继续收到完整 20 个 | 取消信号传入实际工作线程，线程收尾后才释放串行调度；最新测试取消前 2 个、最终 3 个，没有继续发完整段 |
| 等待持有连接锁 | 1 秒等待期间，状态查询耗时 0.9403 秒 | MCP 等待使用异步睡眠，状态查询约 0.0016 秒；断开约 0.0016 秒，并能识别等待期间连接变化 |
| 全局网络线程执行图像编码 | 记录显示 Pillow 在 `Twisted Reactor` 线程执行保存 | reactor 只复制完成的帧，图像处理独立；人为阻塞编码时另一连接的按键调用仍成功 |
| 排队请求可能落入切换后的目标 | 原实现只在原生线程中竞争同一个锁，没有请求级目标约束 | 请求提交时绑定 `connection_id`；执行前变化则拒绝。调用者也可显式携带 ID；模拟切换验证旧目标按键未到达新桌面 |
| 原生线程池可能被锁等待者占满 | 每个 MCP 工具都直接 `to_thread`，排队发生在同步锁内 | 异步侧最多接受 16 个工作操作，实际启动一个工作线程；过载拒绝后能恢复，取消的排队输入不会事后执行 |
| CLI 截图无输出地挂起 | 有超时堆栈明确停在 `getpass.win_getpass`，尚未发起 VNC 连接 | 取消自动猜测密码提示，只有 `--ask-password` 才询问。相同实机 CLI 截图现已成功；CLI 也不再提前加载 MCP 服务器 |

取消是协作式的，不能撤回已发送的字符。控制信号在批次内、延迟等待中和操作边界检查；原生网络单步仍受 `VNC_TIMEOUT` 限制，图像编码也不能被 Python 强行抢占。默认 20 秒操作预算不等于所有底层调用都能在精确的第 20 秒停止。取消或超时后的部分输入不会自动重放。

这符合 Python 对[运行中线程任务取消](https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Future.cancel)的限制。网络操作仍按 [Twisted 的线程约定](https://docs.twisted.org/en/stable/core/howto/threading.html)调回 reactor；没有把 transport 写入移到任意工作线程。

## 性能结果与实际范围

| 场景 | 之前 | 之后 | 范围 |
| --- | --- | --- | --- |
| 等待期间查询状态 | 940.3 ms | 约 1.6 ms | 模拟 MCP/RFB；不是 Windows 应用响应速度 |
| 2000 字符从提交到模拟服务端收到，中位数 5 次 | 81 ms | 25 ms | 同机、相同模拟 RFB 接收端，约 3.24 倍；不证明 Windows 应用能以此速度正确接收 |
| WIN11 默认全屏 JPEG 与 740×420 区域 | 802,496 字节 | 85,049 字节 | 两次实机采样，区域原点为 (470,300)，桌面/广告可能变化 |
| WIN10 默认全屏 JPEG 与相同区域 | 163,836 字节 | 54,893 字节 | 两次实机采样，不是相同区域的无损等价性比较 |

新增区域截图只减少返回图像、模型上下文和部分编码成本。`fresh` 仍获取 VMware 的首帧，不承诺减少握手或原始 RFB 网络流量。JPEG 保持质量 80、4:4:4；PNG 仍可显式选择。已实际查看 WIN11 区域图，文字和按钮可辨认。需要精确颜色或像素时使用 PNG。

## 可扩展性改动

- `server.py`：MCP 参数校验与返回格式。
- `operations.py`：有界调度、协作取消、连接代次校验；通过 `SessionBackend` 协议依赖后端。
- `client.py`：VNC 连接与输入，完成帧的线程安全复制；延迟输入和 64 字符小批次输入。
- `imaging.py`：独立图像编码、区域边界校验、原生尺寸及原点元数据。
- `cli.py`：独立入口，按需载入 MCP；显式目标、密码提示和截图模式。

示例：先 `vnc_connect(host="127.0.0.1", port=5901)`，保留返回的 `connection_id`。随后可调用 `vnc_screenshot(region={"x":470,"y":300,"width":740,"height":420}, connection_id="返回的ID")`。图中局部点 (20,30) 对应桌面点 (490,330)；鼠标工具始终接受桌面原生坐标，不自动按上一张图片改变坐标含义。

当前仍一次连接一台虚拟机；`connection_id` 是连接代次令牌，不是多会话路由。单例数量没有被悄悄扩展。

## 进一步空间与取舍

1. **多会话并行**：可以在现有后端与调度器边界上增加显式 `session_id` 注册表、各自队列和闲置回收。需要增加跨会话隔离、生命周期和资源上限验收；本轮未实施。不能用“自动切当前目标”模拟并行。
2. **持久增量画面与网络编码**：当前 VMware `fresh` 兼容路径每次重连，且源库默认 RAW。高分辨率或远程网络会有更明显成本。下一步应先测服务端的增量响应与支持的编码，再决定协商压缩或替换传输后端；目前没有凭假设启用新编码。
3. **更长时间的稳定性**：已覆盖模拟丢线、超时、认证拒绝、取消、队列过载与正常退出。仍需长期运行与真实网络中断测试。经典 VNC 密码认证仅在模拟服务验证，未修改实机认证设置；模拟实现依据 [RFC 6143](https://www.rfc-editor.org/rfc/rfc6143#section-7.2.2)。
4. **中文输入**：VMware 未接受直接 Unicode 键盘字符、剪贴板限 Latin-1 的现有实机限制没有改变。事件到达模拟服务端不等于 Windows 应用正确显示中文。

## 证据与应用状态

- 基础检查：`checks/results/vnc-deep-smoke.json`，10 项。
- 两种模式的生命周期检查：`checks/results/vnc-deep-lifecycle.json`，18 项。
- 本轮取消、排队、编码、认证、CLI 和性能检查：`checks/results/vnc-deep-checks.json`，16 项。
- 修复前复现：`checks/results/vnc-deep-baseline.json`；CLI 挂起堆栈：`checks/results/vnc-deep-cli-hang.json`。
- 实机全图与切换：`checks/results/vmware-vnc-deep-live.json`；区域与切换：`checks/results/vmware-vnc-deep-region-live.json`。
- CLI 实机区域图：`checks/results/vmware-vnc-deep-cli-region.jpg`。
- 完整源码补丁：`checks/results/vnc-mcp-deep-optimized.patch`；之前源码备份：`checks/backups/vnc-deep-audit-20261020`。

按当前 Codex 注册配置独立启动的新 MCP 进程已验证。当前聊天如果仍持有旧进程，需要重新加载 MCP 或重启 Codex 后才会使用新版；本轮没有重启 Codex，也没有将此前模型 WebSocket 的首次重连问题宣称为已修复。
