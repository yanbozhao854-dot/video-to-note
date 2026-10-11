import asyncio
import io
import json
import stat
import uuid
import zipfile

import httpx
import pytest
from fastapi import FastAPI

from backend import sync_service as sync


def write_task(service, *, task_id=None, text="完整转录", notes="笔记", status="completed"):
    task_id = task_id or str(uuid.uuid4())
    directory = service.workspace / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "task.json").write_text(json.dumps({
        "task_id": task_id, "title": "示例视频", "output": "note",
        "origin": "web", "created_at": "2026-10-11T00:00:00Z",
        "runtime": {"status": status, "progress": 100},
    }), encoding="utf-8")
    (directory / "transcript.json").write_text(json.dumps({
        "segments": [{"start": 0, "end": 5.25, "text": text}],
        "language": "zh", "source": "platform",
    }), encoding="utf-8")
    (directory / "transcript.md").write_text(f"[00:00-00:05] {text}", encoding="utf-8")
    (directory / "notes.md").write_text(notes, encoding="utf-8")
    return task_id


def connect(local, remote_url="https://sync.test"):
    local.save_config(sync.SyncConfig(remote_url=remote_url, token="private-sync-token", enabled=True))


def app_for(service):
    app = FastAPI()
    app.include_router(service.router)
    return app


def archive_bytes(files):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return target.getvalue()


def test_exports_portable_documents_without_credentials_or_raw_media(tmp_path):
    service = sync.create_sync_service(tmp_path)
    task_id = write_task(service)
    directory = tmp_path / task_id
    manifest = json.loads((directory / "task.json").read_text())
    manifest.update(api_key="sensitive-key", cookie="sensitive-cookie", source_url="https://user:password@example.com/video?v=42&token=hidden")
    manifest["runtime"].update(logs=["D:/private/credential"], error="sensitive-key", result={"title": "视频", "output_directory": "D:/private", "api_key": "sensitive-key"})
    (directory / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    transcript = json.loads((directory / "transcript.json").read_text())
    transcript["api_key"] = "sensitive-key"
    transcript["segments"][0]["cookie"] = "sensitive-cookie"
    (directory / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
    for name in ("audio.mp3", "input.mp4", "cookies.txt", "llm_config.json"):
        (directory / name).write_text("private media or credentials")
    (directory / "frames").mkdir()
    (directory / "frames" / "001.jpg").write_bytes(b"image")
    exported = service.snapshot(task_id)
    assert set(exported) == {"task.json", "transcript.json", "transcript.md", "notes.md", "frames/001.jpg"}
    content = b"".join(exported.values())
    for private in (b"sensitive-key", b"sensitive-cookie", b"D:/private", b"password", b"token=hidden"):
        assert private not in content
    metadata = json.loads(exported["task.json"])
    assert metadata["source_url"] == "https://example.com/video?v=42"
    assert json.loads(exported["transcript.json"])["segments"] == [{"start": 0, "end": 5.25, "text": "完整转录"}]


def test_private_config_keeps_token_on_empty_update_and_rejects_unsafe_urls(tmp_path):
    service = sync.create_sync_service(tmp_path)
    connect(service)
    saved = service.config["token"]
    result = service.save_config(sync.SyncConfig(remote_url="https://sync.test", token="", enabled=True))
    assert result == {"remote_url": "https://sync.test", "enabled": True, "has_token": True}
    assert service.config["token"] == saved
    assert "private-sync-token" not in (tmp_path / ".sync" / "config.json").read_text()
    for url in ("http://remote.example", "https://user:pass@example.com", "https://example.com/#frag", "https://example.com?token=secret"):
        with pytest.raises(sync.ArtifactError):
            service.save_config(sync.SyncConfig(remote_url=url))
    assert sync._remote_url("http://127.0.0.1:18000/") == "http://127.0.0.1:18000"


@pytest.mark.parametrize("name", ["../escape.md", "/etc/passwd", "frames/../../escape.png", "frames\\escape.png", "credentials.json"])
def test_zip_rejects_disallowed_paths(name):
    with pytest.raises(sync.ArtifactError):
        sync._unpack(archive_bytes({name: b"escaped"}), str(uuid.uuid4()))


def test_zip_rejects_symlinks_and_size_limits(monkeypatch):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        item = zipfile.ZipInfo("notes.md")
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(item, "../../outside")
    with pytest.raises(sync.ArtifactError):
        sync._unpack(target.getvalue(), str(uuid.uuid4()))
    monkeypatch.setattr(sync, "MAX_TOTAL_BYTES", 4)
    with pytest.raises(sync.ArtifactError):
        sync._unpack(archive_bytes({"notes.md": b"too long"}), str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_bidirectional_sync_and_single_sided_edits_converge(tmp_path):
    imported = []
    local = sync.create_sync_service(tmp_path / "local", on_import=imported.append)
    remote = sync.create_sync_service(tmp_path / "server")
    local_id = write_task(local, notes="本机笔记")
    remote_id = write_task(remote, notes="服务器笔记")
    connect(local)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(remote))) as client:
        result = await local.run_once(client)
        assert result["last_error"] is None
        assert {item["task_id"] for item in remote.manifest()["tasks"]} == {local_id, remote_id}
        assert remote_id in imported
        (local.workspace / local_id / "notes.md").write_text("本机更新", encoding="utf-8")
        await local.run_once(client)
        assert (remote.workspace / local_id / "notes.md").read_text(encoding="utf-8") == "本机更新"
        (remote.workspace / remote_id / "notes.md").write_text("服务器更新", encoding="utf-8")
        await local.run_once(client)
        assert (local.workspace / remote_id / "notes.md").read_text(encoding="utf-8") == "服务器更新"
        await local.run_once(client)
        assert len(local.manifest()["tasks"]) == len(remote.manifest()["tasks"]) == 2
        assert local.manifest()["tasks"] == remote.manifest()["tasks"]


@pytest.mark.asyncio
async def test_concurrent_edits_preserve_one_conflict_copy_without_echo(tmp_path):
    local = sync.create_sync_service(tmp_path / "local")
    remote = sync.create_sync_service(tmp_path / "server")
    task_id = write_task(local)
    connect(local)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(remote))) as client:
        await local.run_once(client)
        (local.workspace / task_id / "notes.md").write_text("本机修改", encoding="utf-8")
        (remote.workspace / task_id / "notes.md").write_text("服务器修改", encoding="utf-8")
        await local.run_once(client)
        assert local.last_error is None
        assert len(local.manifest()["tasks"]) == 2
        assert local.manifest()["tasks"] == remote.manifest()["tasks"]
        assert {files["notes.md"].decode() for files in (local.snapshot(item["task_id"]) for item in local.manifest()["tasks"])} == {"本机修改", "服务器修改"}
        for _ in range(3):
            await local.run_once(client)
        assert len(local.manifest()["tasks"]) == 2
        assert local.manifest()["tasks"] == remote.manifest()["tasks"]
        # A restart retains the shared baseline and still creates no extra copies.
        restarted = sync.create_sync_service(local.workspace)
        await restarted.run_once(client)
        assert len(restarted.manifest()["tasks"]) == 2


@pytest.mark.asyncio
async def test_active_tasks_cannot_be_exported_or_overwritten(tmp_path):
    service = sync.create_sync_service(tmp_path)
    task_id = write_task(service, status="processing")
    assert service.manifest()["tasks"] == []
    metadata = {"task_id": task_id, "runtime": {"status": "completed"}}
    with pytest.raises(sync.ArtifactError):
        await service.import_files(task_id, {"task.json": json.dumps(metadata).encode(), "notes.md": b"new"}, expected_revision="")
    assert (tmp_path / task_id / "notes.md").read_text(encoding="utf-8") == "笔记"


@pytest.mark.asyncio
async def test_offline_and_redirect_leave_local_documents_usable(tmp_path):
    local = sync.create_sync_service(tmp_path)
    task_id = write_task(local)
    connect(local)
    original = local.snapshot(task_id)

    async def disconnected(request):
        raise httpx.ConnectError("private-sync-token https://sensitive.example", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as client:
        result = await local.run_once(client)
    assert result["last_error"]
    assert "private-sync-token" not in json.dumps(result)
    assert local.snapshot(task_id) == original
    seen = []

    async def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://untrusted.example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as client:
        await local.run_once(client)
    assert seen == ["https://sync.test/api/sync/manifest"]
    assert local.snapshot(task_id) == original


@pytest.mark.asyncio
async def test_revision_guard_and_idempotent_api_import(tmp_path):
    source = sync.create_sync_service(tmp_path / "source")
    remote = sync.create_sync_service(tmp_path / "remote")
    task_id = write_task(source)
    files = source.snapshot(task_id)
    payload = sync._pack(files)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(remote)), base_url="https://sync.test") as client:
        first = await client.put(f"/api/sync/task/{task_id}", params={"expected_revision": ""}, content=payload)
        assert first.status_code == 200
        assert first.json()["revision"] == sync._revision(files)
        stale = await client.put(f"/api/sync/task/{task_id}", params={"expected_revision": ""}, content=payload)
        assert stale.status_code == 409
        repeated = await client.put(f"/api/sync/task/{task_id}", content=payload)
        assert repeated.status_code == 200
        assert len(remote.manifest()["tasks"]) == 1
        bad = await client.put("/api/sync/task/invalid", content=payload)
        assert bad.status_code == 422


@pytest.mark.asyncio
async def test_self_connection_fails_promptly_without_deadlock(tmp_path):
    service = sync.create_sync_service(tmp_path)
    connect(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(service)), base_url="https://sync.test") as client:
        result = await service.run_once(client)
        assert "当前应用" in result["last_error"]
        response = await client.post("/api/sync/config", json={"remote_url": "https://sync.test", "enabled": True})
        assert response.status_code == 422
        assert "当前应用" in response.json()["detail"]


@pytest.mark.asyncio
async def test_startup_worker_retries_offline_and_stops_cleanly(tmp_path, monkeypatch):
    local = sync.create_sync_service(tmp_path / "local")
    remote = sync.create_sync_service(tmp_path / "server")
    task_id = write_task(local, notes="自动同步笔记")
    connect(local)
    monkeypatch.setattr(sync, "INTERVAL_SECONDS", 0.01)
    real_client = httpx.AsyncClient
    requests = []
    disconnected = asyncio.Event()
    async with real_client(transport=httpx.ASGITransport(app=app_for(remote))) as remote_client:
        async def transport(request):
            requests.append((request.method, request.url.path))
            if len(requests) == 1:
                disconnected.set()
                raise httpx.ConnectError("临时离线", request=request)
            return await remote_client.request(
                request.method, str(request.url), headers=request.headers,
                content=await request.aread(), follow_redirects=False,
            )

        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(transport), **kwargs)

        monkeypatch.setattr(sync.httpx, "AsyncClient", client_factory)
        app = app_for(local)
        app.router.add_event_handler("startup", local.start)
        app.router.add_event_handler("shutdown", local.stop)
        # Use the same FastAPI lifecycle as the real app, without a manual /run.
        async with app.router.lifespan_context(app):
            worker = local.worker
            assert worker is not None
            await local.start()
            assert local.worker is worker
            await asyncio.wait_for(disconnected.wait(), timeout=2)
            async with asyncio.timeout(3):
                while local.last_success is None:
                    await asyncio.sleep(0.01)
            assert local.last_error is None
            assert remote.snapshot(task_id)["notes.md"].decode() == "自动同步笔记"
            assert requests.count(("GET", "/api/sync/manifest")) >= 2
        assert local.worker is None
        assert worker.done()
        stopped_requests = len(requests)
        await asyncio.sleep(0.03)
        assert len(requests) == stopped_requests
