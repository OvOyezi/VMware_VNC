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

## 复用与性能边界

复用固定 vncdotool 的连接、RFB 矩形请求和原生画布处理，逐像素差异使用已安装 Pillow 的 ImageChops、L 通道阈值和直方图计数；未引入其他视觉库或升级运行依赖。原依赖 expectScreen 比较颜色直方图，像素位置交换时仍可能匹配；本机用两个互换位置的红/蓝像素复现了该差异。

中间帧不编码成 PNG/JPEG，也不逐帧返回给调用方，只编码最终图。区域截图会请求对应的 RFB 矩形，实际服务端可能发送更大的区域；本次 VMware 内置 VNC 的 fresh 连接仍返回完整帧，因此区域比较不能据此宣称节省了它的 RFB 流量。

上游核查：vncdotool 已发布版本为 [v1.4.2](https://github.com/sibson/vncdotool/releases/tag/v1.4.2)，同时阅读了 [main 固定提交 5a98fb8](https://github.com/sibson/vncdotool/tree/5a98fb8fbe2ce7148f310f89c905ddc527e5801c) 的匹配和稳定等待。当前适配保留本项目的 fresh 模式、连接 ID、队列、取消及资源边界。矩形请求依据 [RFB 协议](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst)，区域传输思路也核对了 [noVNC #1990](https://github.com/novnc/noVNC/pull/1990)；未合入该未发布补丁或复制其实现。图像运算见 [Pillow ImageChops](https://pillow.readthedocs.io/en/stable/reference/ImageChops.html)。
