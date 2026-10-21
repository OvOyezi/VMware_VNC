# 跨系统兼容与验证

记录日期：2026-10-21。当前版本：0.3.0。宿主为 Windows / PowerShell 7 / Python 3.15.0，39 个固定运行依赖未升级。来宾系统与宿主要求分开；本项目的维护方向是 VNC 桌面交互与官方 CLI 配合。

## 0.3.0 画面条件验收

新增 vnc_wait_for 和 CLI wait-for，支持 changed / matches / stable、区域、容差与有限预算，返回最后有效图及明确的条件状态。每连接至多一张显式参考，轮询间允许其他会话操作；重连、尺寸变化和引用替换均须重新取得适用的参考。操作说明与复用来源见 [画面条件](../references/visual-waits.md)。

当前默认回归合计 136 项，包含原有 104 项和新增 32 项视觉检查。新增检查覆盖位置敏感比较、累积漂移、参考文件限制、连接隔离、MCP 协议取消、条件未满足返回最后图、CLI 退出码、多次采样仅编码一次，以及 refresh / fresh 的非零原点局部首帧与后续全图像素。

本次 WIN11 专用窗口与 WIN10 只读端点完成 15 项实机检查：检测应用内实际颜色变化、恢复后匹配、稳定窗口、持续动画超时、区域外动画隔离、等待中同会话状态查询及官方 vmrun 更新、CLI 与连接清理。没有发送 VNC 键鼠输入。原始报告为本机 `checks/results/visual-live.json`；新版本的视觉条件本次未在 Linux / Wayland 实机重复验收，不能沿用以下 0.2.0 的文字输入证据代替。

对同一张实际 1680×1053 截图做离线图像处理，10 次 PNG 编码中位数为 75.1 ms，JPEG 为 12.1 ms；9 次相同图像比较的中位数为 1.0 ms。中间采样不编码，只返回最终一张图。该测试不包含 RFB、排队、网络或应用耗时，结果随画面变化；原始数据在 `visual-performance.json`。

区域请求已用遵守请求矩形的人工 RFB 服务逐像素核对，包括局部首帧后再取完整画面。本次 VMware 内置 VNC 的 fresh 连接仍返回约 7.08 MB 的完整帧，即使请求区域只有 280×200；因此没有声称该端点减少了 RFB 流量。当前改进减少重复编码及调用方收到的中间图片。

双机持续条件测试完成 300.7 秒、241 轮：484 次截图、1,444 次条件调用（含拒绝被替换的参考），没有发送键鼠或剪贴板，结束后会话数为 0。29 个进程资源采样的私有提交量为 87.0–95.3 MB，末次 93.8 MB；原始报告为本机 `visual-live-soak.json`。这次专门覆盖参考替换、匹配与稳定等待；有限窗口不能证明多日运行或所有网络故障下的稳定性。

## 0.3.0 独立安装验收

从 Git 索引导出到包含中文、空格和单引号的新目录，重新运行 Setup 及全部 136 项默认检查，fresh / refresh 耐久工具自检各 7 轮通过。39 个依赖逐一符合锁文件，pip check、外部工作目录导入、CLI 和 doctor 通过；从外部目录生成的 TOML 仅定位该安装，不隐式配置端点或密码。

另建环境安装 wheel，14 个源码和辅助资源文件与维护目录逐字节一致；外部目录导入来自该环境的 site-packages。22 工具 MCP 初始化、缺少目标时拒绝连接、基础 10 项、文字扩展 33 项及视觉 32 项检查通过；视觉检查使用同一检查集并显式绑定已安装模块。技能安装与 wheel 均通过 pip check，全局 Codex 配置哈希保持一致。摘要为本机 `checks/results/visual-install-validation.json`，隔离安装副本验收后清除。

## 0.2.0 跨系统文字验证

| 来宾与 VNC 服务 | 文字通道 | 实机结果 |
| --- | --- | --- |
| Windows 11 / VMware 内置 VNC | 官方 vmrun 准备并校验 Unicode 剪贴板，VNC 点击与粘贴 | 30 轮中、日、希腊文、emoji、Tab、换行逐轮精确读回；另验证字面引号/PowerShell 符号、64,000 字符及 CRLF 规范化 |
| Alpine Linux 3.24.2 / TigerVNC Xvnc 1.16.2 / X11 | 原生 UTF-8 Extended Clipboard | 6 轮多语言精确读回；另以静态多语言画面做编码对照 |
| 同一 Linux 来宾 / X11 | 官方 vmrun + Python 3 + xclip | 6 轮多语言精确读回，临时文件清理完成 |
| Windows 10 / VMware 内置 VNC | 本次只读 | 连接、截图与多会话耐久测试；本次未把它作为文字输入兼容性证明 |

读回来自独立测试窗口的结果文件，通过官方 vmrun 取回后比较原文；不会以工具返回成功替代应用验收。Windows 使用系统自带 PowerShell 5.1，宿主维护仍使用 PowerShell 7。

Wayland 的 wl-copy/wl-paste、xsel、其他 Linux 桌面和非 VMware VNC 端点有实现支持，但尚无本次实机验收。VMware 内置 VNC 没有协商 UTF-8 扩展，直接 Unicode keysyms 仍不是通用中文输入办法。受保护桌面、锁屏、特殊应用与不同粘贴快捷键需要单独确认。

## 性能与资源

1280×800 的 Linux 静态多语言文字窗口，10 组 fresh PNG 对照，RGB 像素全部一致：

| 实际传输 | 本次接收字节数中位数 | 捕获耗时中位数 |
| --- | ---: | ---: |
| RAW | 4,096,772 B | 55.8 ms |
| Hextile | 311,900 B | 219.5 ms |

该画面流量减少约 92.4%，代价是本机解码更慢。auto 优先压缩，局域网优先响应速度时可显式选择 raw。数值包含 fresh 握手，随画面和网络变化，不代表所有桌面都会获得相同比例。VMware 内置 VNC 在本次测试中仍选择 RAW。

关闭短截图连接后，立即释放帧缓冲、接收包和剪贴板状态，避免等待循环 GC。为隔离这个因素，对照进程禁用循环 GC、连续取 30 帧：旧关闭行为的私有提交量从 48.5 MB 升至 262.7 MB；修复后从 47.6 MB 至 56.9 MB。该受控测试不是常规运行配置，实际耐久测试使用正常 GC。

当前固定 vncdotool 1.4.2 的 ZRLE packed palette 读取没有按行处理填充位；TigerVNC 真实窄矩形触发解码错误，因此当前版本不协商 ZRLE。没有升级依赖或复制第三方完整解码器来掩盖兼容性范围。

## 自动回归与耐久

0.2.0 的 `scripts/Test.ps1` 有 104 项检查：基础 10、生命周期 18、深挖 16、多会话 21、诊断 6、文字扩展 33。包括真实 loopback UTF-8 协商、粘贴后才 Request、Z_SYNC_FLUSH、压缩上限、连接隔离、错误凭据边界、取消与关闭后的缓冲释放；不操作真实虚拟机。0.3.0 保留并重新运行这些检查。

WIN11 / WIN10 / Linux 的 30 分钟并行 fresh 截图测试通过：901 轮、每台 908 次截图，共 2,724 次；18 次计划重连及旧 ID 拒绝全部通过，结束后会话数为 0。每 50 轮计划重连一台，出现异常即停止，不通过自动恢复掩盖失败。报告为本机 `checks/results/soak-final-memory.json`。

同一 MCP 进程的 60 个资源采样中，私有提交量范围为 89.1–141.4 MB，末次为 102.0 MB；句柄数为 219–222。原始采样在 `soak-resources-final.json`。有限时长验收不能证明多日运行、故障网络或所有应用行为。

GitHub Actions 工作流已准备 Windows / Python 3.11、3.15 矩阵，运行固定依赖与人工 RFB 测试。当前仓库仅本地 Git，尚未在 GitHub 执行该工作流；本机实际验证版本为 Python 3.15.0。

## 0.2.0 独立安装记录

从 Git 索引导出技能到包含中文、空格和单引号的新目录，重新执行 Setup，39 个依赖版本全部匹配锁文件；完整 104 项默认回归通过，fresh / refresh 耐久工具自检均通过。由外部工作目录生成的 MCP 配置可解析，command、args、cwd 均指向安装位置；新进程列出 21 个工具，无默认目标，缺少端点的连接请求被拒绝。

wheel 另装到全新虚拟环境，13 个源码及辅助资源文件与维护目录逐字节一致；模块入口、CLI、doctor、21 工具初始化及 43 项基础/文字扩展检查通过。两种安装均通过 pip check，全局 Codex 配置的 SHA-256 保持一致。摘要为本机 `checks/results/install-final-validation.json`；临时安装副本验收后清除。

## 重现验收

使用已获授权的独立测试来宾，按 [文本输入参考](../references/text-input.md) 填写本机 profile 和凭据环境变量。默认测试与安装不会创建、启动或改动真实虚拟机。

1. 用官方 vmrun 把 `checks/fixtures/textbox_windows.ps1` 或 `textbox_linux.py` 复制到来宾私有临时目录。Windows 在已登录用户桌面用 PowerShell 5.1、STA、交互模式启动；Linux 在目标显示会话用 Python 3 启动。参数是该来宾结果文件路径。
2. 显式连接 VNC，截图确认测试窗口可见且已就绪；记录原生坐标。需要清空时先明确清空，再用 vnc_text 提交，避免准备后的焦点点击清除选区。
3. 用官方 vmrun 取回测试程序的 JSON，对照原文；只把 CRLF/LF 作预期规范化。应用不接受粘贴、结果不符或请求取消时停止，不自动重放。
4. 对计划持续截图的端点显式运行 `checks/vnc_mcp_soak.py --allow-live --source . --targets NAME=HOST:PORT --duration 1800 --interval 2 --screenshot-mode fresh --output checks/results/soak.json`。多个目标用空格分隔；不发送键鼠或剪贴板。`--self-test` 为人工服务自检。
5. 关闭自己创建的测试程序，删除自己的来宾临时文件。新建的测试 VM、临时网络访问规则和后台服务按本次实际清单清理；不要影响既有虚拟机和用户应用。

本次原始文字、截图、资源数据与实机报告均属于本机测试资料，位于被 Git 排除的 `checks/results/` 与 `.local/`。新安装应重新生成自己的证据。

## 参考来源

- [RFB 协议与 Extended Clipboard](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst)：能力协商、UTF-8、消息大小、压缩与编码格式。
- [TigerVNC](https://github.com/TigerVNC/tigervnc)：用真实服务验证惰性剪贴板请求与 sync-flush 兼容性；阅读互操作行为，没有复制 GPL 实现。
- [noVNC](https://github.com/novnc/noVNC)：参考扩展剪贴板交互与能力检测方式，未引入浏览器栈或复制其代码。
- [vncdotool](https://github.com/sibson/vncdotool)：继续使用已固定的 1.4.2；本地按其实际能力选择 Hextile / RAW。
- [VMware open-vm-tools](https://github.com/vmware/open-vm-tools)：来宾命令与文件通道。Alpine 将 VIX 功能分包，基础 Tools 服务运行不等于 guest operations 已可用。
