# 核心架构

MCP 与辅助 CLI 共用 `VNCApplication`，由应用层管理配置、目标选择和会话生命周期。运行代码位于 `src/vmware_vnc/`。

```text
MCP / CLI
    ↓
VNCApplication
    ↓
会话与操作队列
    ↓
VNC 客户端：截图、输入、像素检查、画面条件
    ↓
RFB 协议与帧缓冲
    ↓
TCP / TLS / WebSocket / WSS
```

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `application.py` / `config.py` | 应用生命周期、目标解析与配置校验 |
| `server.py` / `cli.py` | MCP 工具与命令行适配 |
| `sessions.py` / `operations.py` | 会话注册、连接 ID、队列、取消和空闲回收 |
| `client.py` | 单桌面的观察与输入 |
| `runtime.py` / `transport.py` | asyncio 运行时及网络传输 |
| `rfb.py` / `rfb_codec.py` / `frames.py` | 握手、认证、矩形解码与完整帧发布 |
| `keys.py` | 按键、组合键与修饰键解析 |
| `imaging.py` / `inspection.py` / `visual.py` | 图像输出、像素检查、参考比较与画面等待 |
| `clipboard.py` / `guest.py` / `resources/` | VNC 剪贴板与可选来宾文本通道 |

## 状态与生命周期

每个会话保存独立连接和操作队列。重连生成新的 `connection_id`，旧 ID 的输入会被拒绝。断开、取消或超时会清理保持的输入；最后一条连接关闭后释放共享 RFB I/O 线程。

连接与画面状态分别报告。认证成功不表示已有完整帧；首帧或分辨率变化后的像素完整后才发布新画面。截图超时会使当前帧与参考失效，不返回旧图代替新截图。

默认截图先刷新当前连接；端点不响应时，使用相同目标与认证参数的共享短连接获取首帧。截图和指针坐标以最新完整观察为依据。

可选来宾 profile 只在显式使用时加载，并绑定 VNC 端点。它仅增强文字输入；基础截图、键鼠和画面条件独立运行。

接口和状态字段见 [操作参考](../references/operations.md)，贡献与验证入口见 [贡献指南](../CONTRIBUTING.md)。
