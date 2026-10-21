# 版本记录

## 0.3.0 · 2026-10-21

- 新增 `vnc_wait_for` 和 CLI `wait-for`，可等待区域变化、匹配参考图或持续稳定。条件未满足时返回最后一张有效截图，操作错误单独报错。
- 截图可显式记住连接内参考。普通截图不替换参考，重连、尺寸变化和新参考会使旧 ID 失效。
- 用 Pillow 逐像素比较位置，避免相同颜色直方图掩盖画面变化。中间采样不编码，只编码最终返回的图。
- 区域截图发送 RFB 矩形请求，并处理非零原点的局部首帧。VMware 内置 VNC 的 fresh 端点在实测中仍返回完整帧。
- 默认检查增至 136 项。视觉条件通过双 Windows 来宾实机验收；Linux/Wayland 的新增视觉条件尚未实机验收。

## 0.2.0 · 2026-10-21

- 新增 `vnc_text`、`vnc_profiles`、`vnc_diagnostics`。文字优先使用 UTF-8 Extended Clipboard，VMware 内置 VNC 可显式绑定官方 `vmrun` 辅助通道。
- Windows 来宾辅助程序使用 PowerShell 5.1，Linux 使用 Python 3 与已有剪贴板工具。文字通过 JSON 传递，凭据通过进程环境提供。
- 支持无损 Hextile / RAW，截图附带捕获耗时、接收字节数和实际编码。固定依赖的 ZRLE 存在兼容问题，当前不协商该编码。
- 连接关闭时释放帧缓冲和协议状态，补充 Unicode、多会话、取消及耐久检查。
- 宿主最低 Python 版本统一为 3.11。

## 技能整理 · 2026-10-21

仓库根目录整理为 `vmware-vnc` skill，包含按需参考资料、Codex 元数据和按安装位置生成的 MCP 片段。技能安装、MCP 注册和本机来宾配置分别完成；安装过程不自动修改 Codex 全局配置。

上游来源和完整差异见 [LOCAL-CHANGES.md](LOCAL-CHANGES.md)，测试范围见 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)。
