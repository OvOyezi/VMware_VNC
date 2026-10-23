# 按画面条件等待

连接后先截图核对桌面，所有具名调用同时携带 session_id、connection_id。`vnc_wait_for` 只采样图像，不点击、不输入、不自动重连。

## 选择条件

| condition | 参考与结果 |
| --- | --- |
| changed | 使用动作前 `vnc_screenshot(remember=true)` 的 reference_id，超过容差时满足 |
| matches | 使用记住的 reference_id，差异在容差以内时满足，可等待回到之前的界面 |
| stable | 不传 reference_id；从自己的首帧开始，持续 stable_for 秒处于容差内时满足 |

先检查完整截图，再选择目标控件所在区域，避免时钟、光标或无关动画影响判断。示例中的区域须替换为实际截图确认的位置：

```python
vnc_screenshot(region={"x": 100, "y": 180, "width": 300, "height": 200},
               remember=True, session_id="work", connection_id="<当前连接 ID>")
# 保存返回元数据中的 reference_id，再执行已授权的具体动作。
vnc_wait_for(condition="changed", reference_id="<上一步的参考 ID>", timeout=10,
             session_id="work", connection_id="<同一个连接 ID>")
# 仅在上一步元数据 matched=true 时继续；超时先检查最后图，不重放动作。
vnc_wait_for(condition="stable", region={"x": 100, "y": 180, "width": 300, "height": 200},
             timeout=10, stable_for=0.5, session_id="work", connection_id="<同一个连接 ID>")
```

changed / matches 默认比较参考图区域；也可以指定其内部的子区域。每个连接只保留一张显式记住的图，至多 16,777,216 像素。普通截图不替换参考，新 remember、桌面尺寸变化、断开或重连会使旧参考失效。参考仅驻留内存，不自动保存到磁盘。

`frame_id` 与 `reference_id` 用途不同。截图元数据中的 `frame_id` 标识最近一张捕获帧；传给 `vnc_inspect(frame_id=...)` 会直接检查这帧，不发送网络刷新请求。每个连接只缓存最新帧，新截图或重连会使旧 ID 失效。`reference_id` 仍只由 `remember=true` 创建，并按上段规则保留。

需要精确像素或局部细节时，可用 `vnc_inspect` 对最近帧采样：

```python
vnc_inspect(frame_id="<最近截图的 frame_id>", reference_id="<remember 返回的 ID>",
            points=[{"x": 120, "y": 230}], zoom=4, pixel_threshold=0,
            session_id="work", connection_id="<同一个连接 ID>")
```

current 图保持原生 RGB 颜色和像素尺寸。比较时可返回 `detail_current`、`detail_reference` 与 `difference_amplified`；最后一张把每通道绝对差乘以 16 并截到 255，颜色经过修改，只用于显示微小差异。逐点结果给出原生坐标、当前 RGB、参考 RGB 与有符号通道差值。细节图元数据会说明原生区域、输出尺寸和缩放比例。显式区域每边不超过 512 像素，放大输出最多 4,194,304 像素；自动差异区域在变化边界外留 8 像素并将每边限制为 256 像素。检查能力不依赖 OCR 或模型。

截图还包含 `frame_received_monotonic`、`updated_regions`、`capture_source`、`lossless` 与 `source_rgb_sha256`。源像素哈希对区域宽高的大端序字节和 RGB 字节计算 SHA-256。PNG `lossless=true`；JPEG 仍是有损压缩。单调时间只用于本进程计算帧龄，不可跨进程比较。

## 容差和预算

- pixel_threshold 默认 8，任一 RGB 通道的差异严格大于它才计为变化像素，范围 0–255。
- max_changed_ratio 默认 0.001，即允许 0.1% 的像素变化。matches 在不超过该比例时满足，changed 在超过时满足；设为 0 可要求严格的变化像素计数。
- stable_for 默认 0.5 秒，范围 0.1–10 秒；使用同一个窗口起始参考，累积漂移超过容差时重新计时。至少比较两帧，不按相邻帧的小变化无限放宽标准。
- timeout 默认 10 秒，上限 60 秒。changed / matches 可用 0 做一次当前帧比较；stable_for 不得超过 timeout。
- interval 默认 0.25 秒，范围 0.1–5 秒；从一帧处理结束后等待该间隔，再取下一帧。

条件未满足时，正常返回 matched=false、timed_out=true 与最后有效截图；无有效首帧、断线、目标变化、参考失效或协议错误会报错。等待预算包含排队和采样，取消仍需等待已经开始的单步网络操作收尾，最终编码也有耗时，不能把预算当作严格的进程抢占时限。

结果包含 samples、comparisons、elapsed_seconds、observed_stable_seconds、difference，以及 sampled_transport_bytes / sampled_capture_seconds。difference 的 changed_bbox 使用原生桌面坐标，右、下边界不包含在矩形内。last_sample_age_seconds 表明最后一帧距返回时有多久，超时结果尤其应检查它。

stable 只证明采样到的图像在观察窗口内满足容差。两次采样之间的短暂变化可能被遗漏，应用是否完成仍须按任务检查；application_verified 固定为 false。等待之间允许同会话其他操作；若另一个操作切换或断开连接，等待会拒绝继续。同一会话同时只能有一个视觉等待。

## CLI

CLI 每次创建独立连接，使用相同的比较与等待逻辑：

```powershell
& "$skillRoot/.venv/Scripts/vnc-mcp.exe" wait-for --host 127.0.0.1 --port 5901 --screenshot-mode fresh --condition stable --region 100 180 300 200 --wait-timeout 10 --stable-for 0.5 --output 'C:/path/to/final.png'
& "$skillRoot/.venv/Scripts/vnc-mcp.exe" wait-for --host 127.0.0.1 --port 5901 --screenshot-mode fresh --condition matches --reference-file 'C:/path/to/reference.png' --wait-timeout 10 --output 'C:/path/to/final.png'
```

changed 未给 reference-file 时先记下当前图，再等待后续变化；要与之前保存的画面比较，应显式提供 reference-file。PNG 适合精确比较，JPEG 需要按压缩差异设置容差。参考文件仅接受 PNG / JPEG，至多 20 MiB、16,777,216 像素，尺寸须与选择的桌面区域一致。

--timeout 是单步 VNC 网络等待，--wait-timeout 是视觉条件预算。条件满足退出 0，未满足退出 3，操作失败退出 1；参数解析错误退出 2。满足和未满足都会保存最后有效图，输出目录须已经存在。连续操作优先 MCP，以保持连接和参考 ID。

## 性能边界

比较使用逐像素位置差异，中间帧不编码成 PNG/JPEG，只编码最终返回的图。区域截图发送对应的 RFB 矩形请求，实际服务端可能返回更大区域；已测 VMware 内置 VNC 的 fresh 连接仍返回完整帧，区域采样不能据此保证节省网络流量。

编码、服务端兼容范围和实机覆盖见 [兼容性](../docs/COMPATIBILITY.md)。
