# 参与维护

项目的维护方向是跨来宾系统的可视化操作。画面与键鼠交给 VNC，VMware 电源、文件和来宾命令优先使用官方 `vmrun`。运行源码集中在 `src/vnc_mcp/`；仓库根目录同时是可安装的 skill，不另建一份技能源码。

## 准备与验证

在 Windows、PowerShell 7 和 Python 3.11+ 环境执行：

```powershell
pwsh -NoProfile -File ./scripts/Setup.ps1
pwsh -NoProfile -File ./scripts/Test.ps1
```

默认检查只连接本机人工 RFB 服务。固定依赖在 `requirements.lock.txt`；升级依赖时说明原因，并重新验证安装和协议行为。

修改 skill 或安装说明后，检查文件链接、命令路径和元数据。涉及安装路径的改动，需要在包含空格或中文的新目录中验证。安装及测试不得依赖维护者机器的绝对路径。

## 提交改动

从 `main` 创建工作分支，在本地修改、验证并提交，再推送或发起 pull request。说明触发问题的条件、修改后的行为和已执行的检查。不要把人工服务通过写成真实虚拟机兼容性通过，也不要把工具接受请求写成应用已完成任务。

真实虚拟机测试需要明确目标和授权。用官方 `vmrun` 准备独立测试窗口，VNC 连接后先检查截图；具名调用同时携带 `session_id` 和 `connection_id`。失败或取消的输入不自动重放，测试结束后清理自己创建的窗口和文件。

凭据、profile、截图、日志和测试结果放在 `.local/` 或 `checks/results/`，不提交 Git。历史调查报告保留当时的路径和结论，新结果另记。

## 同步与分发

公开仓库为 [OvOyezi/VMware_VNC](https://github.com/OvOyezi/VMware_VNC)，安装入口是 `main` 分支根目录。维护时将 GitHub 上的变更同步回本地，再继续修改；发布后核对本地 `HEAD` 与 `origin/main`，保持跟踪文件内容一致。

用户安装后的 profile、凭据和虚拟环境由各自机器管理。它们不属于仓库源码，也不随提交上传。
