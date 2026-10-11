import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from backend.server_access import AccessMiddleware, COOKIE_NAME


TOKEN = "test-only-admin-token-12345678901234567890"


def client(mode="server", origin="https://video.example.com", token=TOKEN, remote="203.0.113.20"):
    app = FastAPI()
    app.add_middleware(AccessMiddleware, mode=mode, access_token=token, public_origin=origin)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/change")
    def change():
        return {"saved": True}

    @app.get("/mcp/sse")
    def sse():
        async def messages():
            yield b"event: ready\ndata: first\n\n"
            await asyncio.sleep(0)
            yield b"data: second\n\n"
        return StreamingResponse(messages(), media_type="text/event-stream")

    return TestClient(app, base_url=origin or "http://127.0.0.1:8000", client=(remote, 1234))


def test_server_rejects_missing_or_short_secret():
    for token in ("", "short"):
        with pytest.raises(ValueError, match="24"):
            AccessMiddleware(lambda: None, mode="server", access_token=token)


def test_desktop_rejects_remote_even_with_forged_forwarded_header():
    with client(mode="desktop", origin="", remote="203.0.113.20") as browser:
        response = browser.get("/api/health", headers={"X-Forwarded-For": "127.0.0.1"})
        assert response.status_code == 403
        assert "仅允许本机访问" in response.json()["detail"]


def test_server_protects_api_and_mcp_and_accepts_bearer_stream():
    with client() as browser:
        assert browser.get("/api/health").status_code == 401
        assert browser.get("/mcp/sse").status_code == 401
        assert browser.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
        response = browser.get("/mcp/sse", headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 200
        assert "first" in response.text and "second" in response.text
        assert response.headers["x-content-type-options"] == "nosniff"


def test_https_login_cookie_tamper_logout_and_csrf():
    with client() as browser:
        assert "管理员登录" in browser.get("/").text
        assert browser.post("/auth/login", data={"token": "bad"}).status_code == 401
        response = browser.post("/auth/login", data={"token": TOKEN}, follow_redirects=False)
        assert response.status_code == 303
        cookie_header = response.headers["set-cookie"]
        assert "HttpOnly" in cookie_header and "Secure" in cookie_header and "SameSite=Strict" in cookie_header
        assert TOKEN not in cookie_header
        assert browser.get("/api/health").status_code == 200
        assert browser.post("/api/change").status_code == 403
        assert browser.post("/api/change", headers={"Origin": "https://evil.example"}).status_code == 403
        assert browser.post("/api/change", headers={"Origin": "https://video.example.com"}).status_code == 200
        cookie = browser.cookies.get(COOKIE_NAME)
        browser.cookies.clear()
        changed_last = "A" if cookie[-1] != "A" else "B"
        assert browser.get("/api/health", headers={"Cookie": f"{COOKIE_NAME}={cookie[:-1]}{changed_last}"}).status_code == 401
        browser.cookies.set(COOKIE_NAME, cookie, domain="video.example.com", path="/")
        response = browser.post("/auth/logout", headers={"Origin": "https://video.example.com"}, follow_redirects=False)
        assert response.status_code == 303 and "Max-Age=0" in response.headers["set-cookie"]
        assert browser.get("/api/health").status_code == 401


def test_fixed_origin_rejects_host_and_cross_site_login():
    with client() as browser:
        assert browser.get("/api/health", headers={"Host": "evil.example", "Authorization": "Bearer " + TOKEN}).status_code == 400
        assert browser.post("/auth/login", data={"token": TOKEN}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert browser.post("/auth/login", data={"token": TOKEN}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_login_rate_limit_and_body_limit():
    with client() as browser:
        assert browser.post("/auth/login", content=b"x" * 4097).status_code == 413
        for _ in range(10):
            assert browser.post("/auth/login", data={"token": "bad"}).status_code == 401
        assert browser.post("/auth/login", data={"token": TOKEN}).status_code == 429


def test_signed_cookie_expires_and_server_without_origin_only_allows_loopback():
    middleware = AccessMiddleware(None, mode="server", access_token=TOKEN)
    session = middleware._make_session()
    assert middleware._valid_session(f"{COOKIE_NAME}={session}")
    expired = session.replace(session.split(".")[0], str(int(time.time()) - 1), 1)
    assert not middleware._valid_session(f"{COOKIE_NAME}={expired}")
    with client(origin="", remote="203.0.113.20") as browser:
        assert browser.get("/api/health", headers={"Authorization": "Bearer " + TOKEN}).status_code == 503
    with client(origin="", remote="127.0.0.1") as browser:
        assert browser.get("/api/health", headers={"Authorization": "Bearer " + TOKEN}).status_code == 200


def test_server_initializer_creates_secret_once_without_printing_it(tmp_path):
    script = tmp_path / "init-server.py"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "deploy" / "init-server.py", script)
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, str(script), "--domain", "video.example.com"], capture_output=True, text=True, encoding="utf-8", env=child_env)
    assert result.returncode == 0
    env_file = tmp_path / ".env"
    original = env_file.read_bytes()
    values = dict(line.split("=", 1) for line in original.decode().splitlines())
    assert len(values["VIDEOTONOTES_ACCESS_TOKEN"]) >= 24
    assert values["VIDEOTONOTES_ACCESS_TOKEN"] not in result.stdout
    assert values["VIDEOTONOTES_PUBLIC_ORIGIN"] == "https://video.example.com"
    result = subprocess.run([sys.executable, str(script), "--domain", "other.example.com"], capture_output=True, text=True, encoding="utf-8", env=child_env)
    assert result.returncode == 0
    assert env_file.read_bytes() == original
