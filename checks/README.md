# 检查工具

默认入口为仓库根目录的 `scripts/Test.ps1`，使用本机人工 RFB 服务或 mock，不操作真实虚拟机。

默认回归同时维护 MCP 主入口与 CLI 辅助入口，不要求外部 `vmrun`、Tools 或来宾账号。下列真实端点工具各有自己的依赖，不能把某个验收驱动的前提当作基础 VNC 接管的前提。

| 默认回归 | 验证范围 |
| --- | --- |
| vnc_smoke.py | MCP 初始化、模拟连接、截图像素与输入协议 |
| vnc_regression.py | refresh / fresh 生命周期、状态清理及本项目 CLI |
| vnc_deep_checks.py | 队列、取消、输入前预算核对、按键时间戳、拖动顺序与终点、编码及分段输入完整性 |
| vnc_multisession.py | 会话隔离、ID、容量、空闲回收与同时首次连接；检查所有连接可用、共用一个 native I/O 线程且能正常清理 |
| vnc_diagnostics.py | MCP、Pillow、cryptography、websockets 与 native RFB 引擎诊断及可选剪贴板后端发现的只读边界 |
| vnc_extensions.py | 混合 Unicode 精确协议读回、UTF-8 协商、惰性请求、sync-flush、profile、来宾剪贴板与断连后的状态清理 |
| vnc_visual.py | 逐像素/区域条件、参考隔离、取消、并发、编码及参考文件限制 |
| vnc_codec.py | 有界无损 RFB 矩形解码与 Tight JPEG 拒绝检查 |
| vnc_inspection.py | 像素采样、参考差异、细节图与坐标映射。通过本机人工 RFB 服务核对 MCP／CLI 键盘字段和未确定字段的单张原始 PNG 证据 |
| vnc_native_rfb.py | 原生 RFB 握手、认证、像素格式、完整帧、resize 与连接生命周期。覆盖 VMware/QEMU 键盘灯载荷、纯元数据更新与断连清理 |
| vnc_fidelity.py | 经真实 MCP stdio 通道检查合成桌面画面采集与图像保真 |
| vnc_frames.py | 分段首帧／resize、ROI、CopyRect 像素来源、快照与有界区域元数据 |
| vnc_keys.py | 组合键规范化、别名、隐式修饰键和无效输入预检 |
| vnc_keyboard.py | MCP／CLI 文字、直接敲键与控件码位输入的精确线序，锁定键条件、展开预算、取消及会话隔离 |
| vnc_transport.py | TCP／TLS／WS／WSS、本机证书验证、二进制流、取消排空与共享运行时清理 |
| vnc_application.py | 应用／MCP 工厂隔离、配置边界、Windows 默认 profile 发现与惰性加载、关闭状态 |
| vnc_state.py | 真实 RFB 消息的分段帧、resize、CopyRect、组合键冲突、无效输入后恢复与资源释放 |
| vnc_recovery.py | 0×0 ServerInit 与 DesktopSize、不可用画面拒绝、motion-only wake、保持输入保护及重新截图边界 |
| vnc_guest_clipboard.py | Linux X11／Wayland 后端固定选择、socket 归属与歧义拒绝、同 MIME 请求及失败清理边界 |

检查直接验证当前实现。输出 JSON、日志和截图保存在 `results/`，不加入 Git。报告的 `checks` 记录各项检查是否通过，平台限制导致的跳过记录在 `skipped` 中。

## 显式端点检查

以下工具只在明确指定真实端点后执行，不属于默认回归：

| 工具 | 用途 |
| --- | --- |
| vnc_multisession_live.py | 通过实际注册 MCP 检查至少两台桌面的连接、截图、重连和会话隔离；不发送键鼠 |
| vnc_soak.py | 持续只读截图与计划重连；`--self-test` 只访问人工服务，实机须传 `--allow-live` |
| vnc_refresh_diagnose.py | 只读记录会话与捕获元数据中的刷新耗时和来源；不追踪私有协议缓冲，也不发送键鼠 |
| vnc_input_live.py | 对四台明确配置的来宾执行输入、视觉、Unicode、会话隔离和应用结果读回验收；必须传 `--allow-live` |

多会话检查显式请求 JPEG，证据图写到报告同目录。运行前请替换示例中的配置、端点和端口。

```powershell
& "$projectRoot/.venv/Scripts/python.exe" "$projectRoot/checks/vnc_multisession_live.py" --config '<实际 MCP 配置文件>' --host '<实际地址>' --targets desktop-a=5901 desktop-b=5902 --output "$projectRoot/checks/results/live.json"
```

持续检查显式提供 `--targets NAME=HOST:PORT` 和 `--allow-live`；每 50 轮计划重连一个目标并拒绝旧 ID，出现异常即停止。它不发送键鼠或剪贴板，也不自动恢复失败操作。完整参数可查看脚本 `--help`。

实机输入验收须提供四个不同的 VNC profile / 端点 / VMX，以及匹配的目标清单和来宾结果文件路径。清单格式为 `{"targets":[{"name":"...","host":"...","port":5901,"profile":"...","result_path":"...","token":"..."}]}`。脚本按 profile 从指定的环境变量或 Windows 凭据管理器读取来宾凭据，启动本仓库的 stdio MCP，向四台来宾发送真实输入，并用官方 `vmrun` 读取夹具 JSON。报告和 PNG 截图写到 `--output` 同目录。

```powershell
& "$projectRoot/.venv/Scripts/python.exe" "$projectRoot/checks/vnc_input_live.py" --manifest "$projectRoot/.local/input-live.targets.json" --profiles-file "$projectRoot/.local/guest-profiles.toml" --output "$projectRoot/checks/results/input-live.json" --allow-live
```

该命令会操作真实来宾桌面，不能作为默认回归运行。JSON 报告按目标列出 `passed`、`failed` 和 `not_executed` 步骤；非零退出码表示失败或未完成。

验收基础 VNC 路径时，使用本项目 MCP 连接未绑定 profile 的 VNC 端点，在真实系统应用或可取消的输入框中操作，以截图核对结果；逐键文字先单独聚焦，再使用 `vnc_type_keys`。`vnc_input_live.py` 的来宾读回是独立辅助验证。环境条件见 [支持范围](../docs/COMPATIBILITY.md)。

## 测试窗口

- `fixtures/textbox_windows.ps1`、`textbox_linux.py` 提供独立文本窗口，把实际收到的内容写入指定 JSON，供应用级读回比较。只使用临时测试文字。
- `fixtures/visual_windows.ps1` 提供变化与固定区域。Windows PowerShell 5.1 / STA 下传入 ControlPath、ResultPath，控制文件可指定 generation、mode（static/pulse）和 red/green/blue。
- `fixtures/input_windows.ps1`、`input_linux.py` 提供文本、按钮、可拖动画布和滚轮区域，记录真实事件与控件坐标。Windows 参数为 `-ResultPath PATH -Token TOKEN`；Linux 参数为 `RESULT_PATH TOKEN`，需要 GTK 3 / PyGObject 或 tkinter。GTK 夹具的坐标验收使用 X11，Wayland 会话可通过 Xwayland 运行夹具。

这些窗口不会被安装或默认回归启动。实机先截图核对，具名调用同时携带两个 ID；结束后关闭测试窗口并清理文件。验证要求见 [贡献指南](../CONTRIBUTING.md)。

## 可选代理互操作

`vnc_websockify.py` 使用真实 websockify 进程和本机人工 RFB 服务验证核心、MCP 与 CLI，不访问真实虚拟机。把 websockify 安装在独立测试环境；它不是项目运行依赖，也不由默认检查自动安装。

```powershell
& "$projectRoot/.venv/Scripts/python.exe" "$projectRoot/checks/vnc_websockify.py" --source $projectRoot --websockify-python '<已安装 websockify 的 Python>' --output "$projectRoot/checks/results/websockify.json"
```

打包验收另用 `python -m pip wheel --no-deps .` 构建 wheel，在独立环境安装固定依赖和 wheel，核对 `vmware_vnc` 导入、CLI、MCP 初始化及随包资源；不能只用当前 editable 环境证明分发可用。
