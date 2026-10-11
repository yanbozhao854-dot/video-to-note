# Linux 服务器版与手机访问

服务器版和 Windows 本机版使用同一套界面，默认同时处理 **5 个视频任务**。服务器版需要管理员口令登录；手机浏览器登录后可以查看和操作。管理员口令也用于本机自动同步及 MCP/API 鉴权。

Windows 本机安装和启动方法见 [Windows 本机版说明](windows-local.md)。

Docker 把运行环境、依赖和服务打包起来。你在 Linux 服务器启动它，再从电脑或手机浏览器打开服务器地址。运行费用来自你自己的服务器；这不是第三方云服务。

## 有域名：两步启动 HTTPS 服务

服务器需安装 Docker Engine 和 Compose 插件，并提供 Python 3。把域名指向服务器公网 IP，放行 80、443 端口。进入项目目录：

```sh
python3 deploy/init-server.py --domain video.example.com
docker compose -f deploy/compose.yaml -f deploy/compose.https.yaml up -d --build
```

把 `video.example.com` 换成你的域名。Caddy 会申请证书并提供 HTTPS。服务只把 8000 端口绑定到服务器回环地址，公网访问由 Caddy 处理。数据和模型保存在持久化 Docker 卷内；更新镜像不会删除记录。

从服务器本地打开 `deploy/.env`，复制 `VIDEOTONOTES_ACCESS_TOKEN` 后面的值作为**管理员口令**。浏览器访问 `https://video.example.com`，输入口令登录。口令由初始化脚本随机生成，不存在默认密码，重复运行脚本也不会覆盖旧口令。请保存该文件；它不会进入 GitHub 或 Docker 镜像。

## 已有反向代理，或者只先本机验证

已有 HTTPS 反向代理时，初始化地址使用它对外提供的完整网站地址：

```sh
python3 deploy/init-server.py --origin https://video.example.com
docker compose -f deploy/compose.yaml up -d --build
```

让反向代理转发到 `127.0.0.1:8000`，保留网站的 `Host` 请求头，并允许 SSE 长连接。服务器通过固定的 `VIDEOTONOTES_PUBLIC_ORIGIN` 校验网站地址，Cookie 是否设置 Secure 也由这个地址决定。应用不信任客户端传入的代理头。

没有域名时，可以不带参数运行初始化脚本，然后启动基础 Compose。服务地址为服务器本机 `http://127.0.0.1:8000`，可通过 SSH 隧道验证。手机正式使用时，请配置域名或其他 HTTPS 反向代理地址，更新 `.env` 的公开地址后重启服务。

## 本机与服务器自动同步

Windows 本机界面填写服务器完整 HTTPS 地址和管理员口令，开启“自动同步”并保存。服务运行期间，会自动同步完成的视频转录、笔记、截图和任务记录，也能点击“立即同步”。电脑离线时可以继续本地使用，下次连接再同步。

同步不会传输下载的原始视频、音频、模型、API Key 或平台 Cookie。各端的 AI 总结 API Key、Base URL 和 B 站等登录凭据需要分别配置。Windows 加密的凭据不能直接搬到 Linux。

优先读取视频现有字幕；没有字幕时，需要明确允许语音识别后再重试。CPU 服务器转录 5 个无字幕视频可能较慢，首次选择模型还需要下载模型。并发数通过 `MAX_CONCURRENT_TASKS` 调整。

Linux 无桌面服务器不提供打开本机浏览器扫码登录的功能。需要登录才能访问的平台，应在界面填入自己的 Cookie，或先在本机处理后同步。

## MCP 和 API

服务器 MCP SSE 地址为 `https://video.example.com/mcp/sse`，客户端需发送请求头：

```text
Authorization: Bearer <管理员口令>
```

桌面版 MCP 可继续使用本机地址。若通过项目的 stdio MCP 连接远端服务器，设置 `VIDEOTONOTES_BACKEND_URL` 为服务器地址、`VIDEOTONOTES_ACCESS_TOKEN` 为管理员口令。不要把真实口令写入公开配置或 GitHub。

## 更新、备份、停止

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.https.yaml up -d --build
docker compose -f deploy/compose.yaml -f deploy/compose.https.yaml logs --tail=100 video-to-note
docker compose -f deploy/compose.yaml -f deploy/compose.https.yaml down
```

`down` 保留数据卷。备份时保存 `deploy/.env` 和 `video_data` 卷；HTTPS 部署还应保存 Caddy 数据卷。不要使用 `down -v`，它会删除数据卷。

服务器进程以 UID 10001 的普通用户运行，移除容器权限，所有 API、静态文件和 MCP 入口都要求登录或 Bearer 鉴权。网页登录会设置 HttpOnly、SameSite=Strict Cookie，默认有效期 12 小时；错误登录达到 10 次后暂停 5 分钟。
