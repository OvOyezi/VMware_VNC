# 来源与实现差异

基于 [thanhvn-shinkoxd/VNC-MCP](https://github.com/thanhvn-shinkoxd/VNC-MCP/tree/abe747b4dc42bc936870959861a436c849aef482) 的固定提交 `abe747b4dc42bc936870959861a436c849aef482` 继续开发，使用源码构建。原作者及许可保留在 [LICENSE](LICENSE)、[NOTICE](NOTICE) 和 `pyproject.toml`。

当前实现与上游的主要差异：

- 提供独立具名会话、连接代次校验、有界队列、协作取消及空闲回收；清理保持的输入和协议缓冲，失败输入不自动重放。
- 修复 vncdotool 同步代理调用后的协议对象丢失及首次并发启动 reactor 的竞态。
- 鼠标定位后再发送按钮事件，为点击、按键和滚轮保留短暂按下时间；等待核对实际时长，中断后释放已登记的输入并关闭连接。
- 提供 fresh / refresh 截图模式、原生坐标与区域原点；编码移出 reactor，文字逐字发送并保留至少 20 毫秒间隔。
- 拖动使用最多 30 个可取消的移动步骤，避免上游每步 200 毫秒的等待累积超过连接超时。
- 按公开 RFB 协议实现文本专用 Extended Clipboard，处理惰性请求、sync-flush、大小限制和异常断开。
- 为缺少 VNC UTF-8 剪贴板的 VMware 端点提供可选来宾剪贴板后端，限定端点、凭据、文本校验和临时文件清理。
- 通过 Pillow 逐像素比较画面，支持区域变化、参考匹配和采样稳定等待；参考图只驻留当前连接。
- 在 MCP 与本项目 CLI 暴露相同 VNC 能力，支持按实际安装位置生成配置；默认检查使用人工 RFB 服务。

运行依赖固定在 `requirements.lock.txt`。第三方协议和库的适配说明见 [文本输入](references/text-input.md)、[画面条件](references/visual-waits.md)及 [兼容性](docs/COMPATIBILITY.md)。
