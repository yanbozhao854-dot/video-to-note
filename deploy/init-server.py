"""Generate private server credentials once; never replace an existing file."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 VideoToNo 服务器管理员口令")
    parser.add_argument("--domain", help="已指向服务器的域名，例如 video.example.com")
    parser.add_argument("--origin", default="", help="已有反向代理的网站地址，例如 https://video.example.com")
    args = parser.parse_args()
    domain = (args.domain or "").strip().lower()
    origin = args.origin.strip().rstrip("/") or (f"https://{domain}" if domain else "http://127.0.0.1:8000")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        parser.error("网站地址无效")
    if any(char in origin for char in "\r\n #{}$") or any(char in domain for char in "\r\n /:#{}$"):
        parser.error("域名或网站地址包含无效字符")
    if domain and parsed.hostname != domain:
        parser.error("域名与网站地址不匹配")
    env_path = Path(__file__).resolve().parent / ".env"
    token = secrets.token_urlsafe(36)
    values = (
        "VIDEOTONOTES_MODE=server\n"
        f"VIDEOTONOTES_ACCESS_TOKEN={token}\n"
        f"VIDEOTONOTES_PUBLIC_ORIGIN={origin}\n"
        f"VIDEOTONOTES_DOMAIN={domain}\n"
        "MAX_CONCURRENT_TASKS=5\n"
    )
    try:
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"保留已有配置：{env_path}；管理员口令未更改。")
        return
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
        output.write(values)
    if os.name != "nt":
        env_path.chmod(0o600)
    print(f"已创建 {env_path}")
    print("管理员口令保存在文件的 VIDEOTONOTES_ACCESS_TOKEN 项。请在服务器本地查看，不要提交到 GitHub。")
    print(f"网站地址：{origin}")


if __name__ == "__main__":
    main()
