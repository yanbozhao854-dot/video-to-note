"""Desktop loopback access and authenticated, stream-safe server access."""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import secrets
import time
from collections import OrderedDict
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit


COOKIE_NAME = "videotonotes_admin"
MAX_LOGIN_BYTES = 4096
MAX_CREDENTIAL_LENGTH = 1024
DEFAULT_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def is_loopback(host: str | None) -> bool:
    if not host or host in {"localhost", "testclient"}:
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or bool(
            address.version == 6 and address.ipv4_mapped and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        return False


def normalize_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("VIDEOTONOTES_PUBLIC_ORIGIN 必须是完整网站地址，例如 https://video.example.com")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("网站地址端口无效") from exc
    hostname = parsed.hostname.lower().encode("idna").decode("ascii")
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if parsed.scheme == "https" else 80
    authority = hostname + (f":{port}" if port and port != default_port else "")
    return f"{parsed.scheme}://{authority}"


class AccessMiddleware:
    """Wrap ASGI directly so authenticated MCP SSE remains a streaming response.

    Forwarded headers are never used for client identity or cookie security. The
    configured public origin supplies both the allowed Host and HTTPS decision.
    """

    def __init__(
        self,
        app: Any,
        mode: str = "desktop",
        access_token: str = "",
        security_headers: dict[str, str] | None = None,
        public_origin: str = "",
        session_ttl: int = 43200,
    ) -> None:
        if mode not in {"desktop", "server"}:
            raise ValueError("VIDEOTONOTES_MODE 必须为 desktop 或 server")
        if mode == "server" and not (24 <= len(access_token) <= MAX_CREDENTIAL_LENGTH):
            raise ValueError("服务器启动需要至少 24 字符的 VIDEOTONOTES_ACCESS_TOKEN，请运行 deploy/init-server.py")
        self.app = app
        self.mode = mode
        self.access_token = access_token
        self.public_origin = normalize_origin(public_origin) if public_origin else ""
        self.session_ttl = max(60, min(int(session_ttl), 86400 * 7))
        self.headers = {**DEFAULT_HEADERS, **(security_headers or {})}
        self.login_attempts: OrderedDict[str, list[float]] = OrderedDict()

    def _credential_matches(self, value: str) -> bool:
        if not value or len(value) > MAX_CREDENTIAL_LENGTH:
            return False
        return hmac.compare_digest(value.encode("utf-8"), self.access_token.encode("utf-8"))

    def _make_session(self) -> str:
        payload = f"{int(time.time()) + self.session_ttl}.{secrets.token_urlsafe(18)}"
        signature = hmac.new(self.access_token.encode(), payload.encode(), hashlib.sha256).digest()
        return payload + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

    def _valid_session(self, cookie_header: str) -> bool:
        if len(cookie_header) > 8192:
            return False
        try:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
            value = cookies[COOKIE_NAME].value
            expiry, nonce, signature = value.split(".")
            expires_at = int(expiry)
            if not (time.time() < expires_at <= time.time() + self.session_ttl + 5):
                return False
            if len(nonce) > 64 or len(signature) != 43:
                return False
            payload = expiry + "." + nonce
            expected = base64.urlsafe_b64encode(
                hmac.new(self.access_token.encode(), payload.encode(), hashlib.sha256).digest()
            ).decode().rstrip("=")
            return hmac.compare_digest(signature, expected)
        except (ValueError, KeyError, CookieError, UnicodeError):
            return False

    def _origin(self, headers: dict[str, str], scope: dict[str, Any]) -> str:
        if self.public_origin:
            return self.public_origin
        return normalize_origin(f"{scope.get('scheme', 'http')}://{headers.get('host', '')}")

    def _same_origin(self, headers: dict[str, str], expected: str) -> bool:
        origin = headers.get("origin")
        if origin:
            try:
                if normalize_origin(origin) != expected:
                    return False
            except ValueError:
                return False
        return headers.get("sec-fetch-site", "") not in {"cross-site", "same-site"}

    def _limited(self, client_host: str) -> bool:
        now = time.monotonic()
        previous = self.login_attempts.pop(client_host, [])
        current = [item for item in previous if now - item < 300]
        self.login_attempts[client_host] = current
        while len(self.login_attempts) > 1024:
            self.login_attempts.popitem(last=False)
        return len(current) >= 10

    async def _respond(
        self, send: Any, status: int, body: bytes = b"", content_type: str = "application/json; charset=utf-8",
        extra: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        headers = [(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode())]
        headers.extend((name.lower().encode(), value.encode()) for name, value in self.headers.items())
        headers.extend(extra or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _error(self, send: Any, status: int, detail: str) -> None:
        await self._respond(send, status, json.dumps({"detail": detail}, ensure_ascii=False).encode())

    def _login_page(self, error: str = "") -> bytes:
        return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>登录 VideoToNo</title><style>body{font-family:system-ui,sans-serif;background:#f3f5f8;"
                "color:#1e293b;margin:0;display:grid;place-items:center;min-height:100vh}main{background:white;"
                "border:1px solid #dbe2ea;border-radius:18px;padding:32px;width:min(340px,calc(100vw - 80px))}"
                "h1{font-size:24px}p{line-height:1.6;color:#475569}label{display:block;margin-bottom:8px}"
                "input,button{box-sizing:border-box;width:100%;padding:13px;border-radius:9px;font:inherit}"
                "input{border:1px solid #a8b5c7}button{margin-top:16px;border:0;background:#2563eb;color:white}"
                ".error{color:#b91c1c}</style><main><h1>VideoToNo 管理员登录</h1>"
                "<p>登录后查看视频转录、笔记和同步记录。</p>"
                f"<p class='error'>{html.escape(error)}</p>"
                "<form method='post' action='/auth/login'><label for='token'>管理员口令</label>"
                "<input id='token' name='token' type='password' autocomplete='current-password' required maxlength='1024'>"
                "<button type='submit'>登录</button></form></main></html>").encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        client_host = client[0] if isinstance(client, (tuple, list)) and client else None
        if self.mode == "desktop":
            if not is_loopback(client_host):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await self._error(send, 403, "VideoToNo 仅允许本机访问")
                return
        else:
            headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
            if not self.public_origin and not is_loopback(client_host):
                await self._deny(scope, send, 503, "请设置服务器公开地址 VIDEOTONOTES_PUBLIC_ORIGIN")
                return
            try:
                origin = self._origin(headers, scope)
                # Host is compared using the fixed public scheme, never forwarded headers.
                received_origin = normalize_origin(f"{urlsplit(origin).scheme}://{headers.get('host', '')}")
                if received_origin != origin:
                    raise ValueError()
                if not self.public_origin and not is_loopback(urlsplit(origin).hostname):
                    raise ValueError()
            except ValueError:
                await self._deny(scope, send, 400, "网站地址与服务器配置不一致")
                return
            authorization = headers.get("authorization", "")
            bearer = authorization[7:] if authorization.startswith("Bearer ") and len(authorization) <= 1031 else ""
            has_bearer = self._credential_matches(bearer)
            has_cookie = self._valid_session(headers.get("cookie", ""))
            method = scope.get("method", "GET")
            path = scope.get("path", "/")
            if scope["type"] == "http" and path == "/auth/login":
                if method == "GET":
                    await self._respond(send, 200, self._login_page(), "text/html; charset=utf-8")
                    return
                if method != "POST":
                    await self._error(send, 405, "不支持的登录请求")
                    return
                if not self._same_origin(headers, origin):
                    await self._error(send, 403, "请从本站登录页面提交")
                    return
                if self._limited(client_host or "unknown"):
                    await self._error(send, 429, "登录尝试过多，请 5 分钟后重试")
                    return
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > MAX_LOGIN_BYTES:
                        await self._error(send, 413, "登录请求过大")
                        return
                    if not message.get("more_body", False):
                        break
                try:
                    fields = parse_qs(body.decode("utf-8"), max_num_fields=8)
                    token = fields.get("token", [""])[0]
                except (UnicodeError, ValueError):
                    token = ""
                if not self._credential_matches(token):
                    self.login_attempts[client_host or "unknown"].append(time.monotonic())
                    await self._respond(send, 401, self._login_page("管理员口令不正确"), "text/html; charset=utf-8")
                    return
                self.login_attempts.pop(client_host or "unknown", None)
                cookie = f"{COOKIE_NAME}={self._make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={self.session_ttl}"
                if origin.startswith("https://"):
                    cookie += "; Secure"
                await self._respond(send, 303, extra=[(b"location", b"/"), (b"set-cookie", cookie.encode())])
                return
            if not (has_bearer or has_cookie):
                if scope["type"] == "http" and path == "/" and method == "GET":
                    await self._respond(send, 200, self._login_page(), "text/html; charset=utf-8")
                else:
                    await self._deny(scope, send, 401, "请先登录管理员账户或提供 Bearer 口令")
                return
            if not has_bearer and (scope["type"] == "websocket" or method not in {"GET", "HEAD", "OPTIONS"}):
                has_origin_evidence = bool(headers.get("origin")) or headers.get("sec-fetch-site") == "same-origin"
                if not has_origin_evidence or not self._same_origin(headers, origin):
                    await self._deny(scope, send, 403, "请求来源不匹配，请从本站操作")
                    return
            if scope["type"] == "http" and path == "/auth/logout":
                if method != "POST":
                    await self._error(send, 405, "退出登录需要 POST 请求")
                    return
                cookie = f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                if origin.startswith("https://"):
                    cookie += "; Secure"
                await self._respond(send, 303, extra=[(b"location", b"/auth/login"), (b"set-cookie", cookie.encode())])
                return

        async def wrapped_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                existing = {key.lower() for key, _value in response_headers}
                for key, value in self.headers.items():
                    if key.lower().encode() not in existing:
                        response_headers.append((key.lower().encode(), value.encode()))
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, receive, wrapped_send)

    async def _deny(self, scope: Any, send: Any, status: int, detail: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await self._error(send, status, detail)
