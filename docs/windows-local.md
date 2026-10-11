# Windows 本机版

本版本在自己的电脑处理视频，并在浏览器里显示界面。已配置好运行环境的电脑，双击根目录 `start-windows.cmd` 即可；关闭服务用 `stop-windows.cmd`。关闭浏览器不会取消任务。

首次从 GitHub 下载源码时，需要 Python 3.11 或 uv。安装了 uv 后，在项目目录运行 `powershell -ExecutionPolicy Bypass -File setup-windows.ps1`；已有 Python 3.11 则加上 `-Python "C:\你的Python路径\python.exe"`。下载音频和截图还需要安装 FFmpeg 并加入 PATH。当前电脑的环境已准备好。

1. 粘贴 YouTube、B 站等平台视频链接，默认输出完整转录和时间戳。
2. 优先读取平台已有字幕。无法取得字幕时会提示原因；需要语音识别再勾选“无字幕时允许语音识别”，重新提交。首次识别会下载所选模型。
3. 生成总结时选择总结模型，可填写自己的 API Key、Base URL 和模型 ID。
4. 点“新建任务”即可继续提交，前一个任务在后台运行。最多同时处理 5 个任务，多出的会排队。
5. 服务端部署完成后，展开“本机与服务器同步”，填写 HTTPS 服务器地址与管理员连接口令并开启同步。每 30 秒同步一次，离线时本机仍然可用。

同步完整转录、笔记、截图和任务记录；原视频、音频、模型、API Key、平台 Cookie 不同步。两个设备发生资料冲突时保留副本；删除仅影响当前设备，另一端保留的资料可能再次同步回来。

让 AI Agent 操作：MCP SSE 地址是 `http://127.0.0.1:8000/mcp/sse`。服务器地址与鉴权方式见 [服务器部署说明](server-deployment.md)。

数据在 `workspace/`，运行日志在 `.runtime/`；这些文件不会提交到 GitHub。服务器 API Key 在服务器单独填写。
