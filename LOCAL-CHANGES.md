# 来源与实现差异

基于 [thanhvn-shinkoxd/VNC-MCP](https://github.com/thanhvn-shinkoxd/VNC-MCP/tree/abe747b4dc42bc936870959861a436c849aef482) 的固定提交 `abe747b4dc42bc936870959861a436c849aef482` 继续开发。原作者署名、许可与必要版权声明保留在 [LICENSE](LICENSE)、[NOTICE](NOTICE) 和 `pyproject.toml`。从 0.4.0 起，运行时 RFB 传输由本项目实现，不依赖 vncdotool 或 Twisted。

当前实现与上游的主要差异：

- 提供独立具名会话、连接代次校验、有界队列、协作取消及空闲回收；清理保持的输入和协议缓冲，失败输入不自动重放。
- 在 `src/vnc_mcp/rfb.py` 实现 asyncio RFB 握手、认证、连接状态、帧读取、输入与扩展消息；通过共享的 `vnc-rfb-io` 线程为同步会话边界提供显式 facade。
- 在 `src/vnc_mcp/rfb_codec.py` 实现有界 Raw、CopyRect、Hextile、ZRLE 和 Tight 无损矩形解码；拒绝 Tight JPEG。自动编码协商只声明客户端实现的无损编码，并保留 RAW 回退。
- 鼠标定位后再发送按钮事件，为点击、按键和滚轮保留短暂按下时间；等待核对实际时长，中断后释放已登记的输入并关闭连接。
- 提供 `auto` / `refresh` / `fresh` 截图策略、原生坐标与区域原点；默认先对当前连接执行一次有界完整刷新，失败时从短暂连接取得初始完整帧。图像编码不阻塞 RFB I/O，文字逐字发送并保留至少 20 毫秒间隔。
- 拖动使用最多 30 个可取消的移动步骤，避免上游每步 200 毫秒的等待累积超过连接超时。
- 按公开 RFB 协议实现文本专用 Extended Clipboard，处理惰性请求、sync-flush、大小限制和异常断开。
- 为缺少 VNC UTF-8 剪贴板的 VMware 端点提供可选来宾剪贴板后端，限定端点、凭据、文本校验和临时文件清理。
- 通过 Pillow 逐像素比较画面，支持区域变化、参考匹配和采样稳定等待；参考图只驻留当前连接。
- 默认返回原生尺寸 RGB8 PNG，并提供来源 RGB SHA-256、帧 ID、接收时间与更新区域等元数据；JPEG 是显式的有损选项。
- 新增 `vnc_inspect` 以检查原生坐标像素、参考差异和细节区域；放大差异图单独标明经过颜色增强。增加 CLI `inspect`，不引入 OCR 或模型依赖。
- 在 MCP 与本项目 CLI 暴露相同 VNC 能力，支持按实际安装位置生成配置；默认检查使用人工 RFB 服务。

运行依赖固定在 `requirements.lock.txt`，包括 MCP、Pillow 和 cryptography；诊断会报告 native asyncio RFB 引擎状态。PNG 的 RGB8 是 VNC 服务端帧缓冲数据，不承诺显示器 HDR 或 ICC 色彩配置。MCP 图片的 `_meta.codex/imageDetail="original"` 只是客户端提示，不保证端到端模型保真。接口说明见 [操作参考](references/operations.md)、[文本输入](references/text-input.md)、[画面条件](references/visual-waits.md)及 [兼容性](docs/COMPATIBILITY.md)。
