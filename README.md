# VideoToNo · 本机与服务器版

把 YouTube、B 站等视频转成完整、带时间戳的 Markdown 转录，配置自己的 API Key、Base URL 和模型即可继续生成笔记。支持网页操作和 MCP Agent 接入。

本项目基于 [like-attract/video-to-note v1.4.5](https://github.com/like-attract/video-to-note/tree/v1.4.5) 定制，保留上游 MIT 许可证。[上游说明](docs/upstream-README.md)供参考；本定制版本以以下文档为准。

## 两个版本

| 版本 | 启动入口 | 访问方式 |
| --- | --- | --- |
| Windows 本机版 | start-windows.cmd | 自己电脑的浏览器，本机回环访问 |
| Linux 服务器版 | deploy/compose.yaml | HTTPS 网页，管理员口令登录，手机可用 |

两端共用功能，资料可自动同步。同步完整转录、笔记、截图和历史；原视频、音频、模型、API Key 和平台 Cookie 各自保存。

## Windows

首次从源码安装运行 setup-windows.ps1，需要 uv 或 Python 3.11；下载和处理音视频需要 FFmpeg 加入 PATH。环境就绪后双击 start-windows.cmd；停止服务双击 stop-windows.cmd。

[Windows 使用步骤](docs/windows-local.md)

## Linux 与手机

Docker 把应用、Python 和 FFmpeg 封装在一起，由服务器持续运行，手机只需打开网页。

[服务器部署与 HTTPS 配置](docs/server-deployment.md)

服务器会生成独立的管理员连接口令。网页登录、MCP 和同步均需要鉴权。

## 功能

- 默认完整转录和时间戳，优先已有字幕。
- 无字幕先提示，勾选“无字幕时允许语音识别”后重新提交才识别。
- 自定义 API Key、Base URL 和模型 ID，生成详细笔记或摘要。
- 最多同时处理 **5 个任务**，多余任务排队；点“新建任务”继续提交，旧任务后台运行。
- 从历史记录查看资料，导出 Markdown 或 TXT，手机可切换工作区和设置。

## 自动同步

本机展开“本机与服务器同步”，填写 HTTPS 服务器地址和管理员连接口令，保存并开启。每 30 秒双向检查，也可以立即同步；断线时本机仍可使用。

同时修改同一份资料会保留副本。删除仅影响当前设备，另一端保留的资料可能重新同步回来。两端的模型 API Key 和平台凭据分别配置。

## MCP

本机 SSE 地址：http://127.0.0.1:8000/mcp/sse

服务器使用自己的 HTTPS 地址加 /mcp/sse，并添加 Authorization: Bearer <管理员连接口令> 请求头。

transcribe_video → wait_for_task → get_transcript 获取带时间戳原文；summarize_video 使用本端模型配置生成笔记。无字幕识别选项 allow_asr 默认关闭，Agent 需征求用户选择再开启。

## 数据与验证

本机数据在 workspace/，日志在 .runtime/；服务端使用独立数据卷。资料和密钥不会提交到 GitHub 或打入 Docker 镜像。

安装 backend/requirements-dev.txt 后，执行 python -m pytest；前端语法检查：node --check frontend/script.js 和 node --check frontend/sync.js。本机回归测试和两个独立服务之间的同步、鉴权已通过；Docker 配置已准备，Linux 容器构建仍需在装有 Docker 的环境验证。
