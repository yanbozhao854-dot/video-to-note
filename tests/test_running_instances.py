"""Real HTTP integration across separate desktop and authenticated server processes."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "integration-only-admin-token-1234567890"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def document(workspace, title):
    task_id = str(uuid.uuid4())
    folder = workspace / task_id
    folder.mkdir(parents=True)
    (folder / "task.json").write_text(json.dumps({
        "task_id": task_id, "title": title, "output": "transcript", "origin": "agent",
        "created_at": "2026-10-11T00:00:00Z",
        "runtime": {"status": "completed", "progress": 100, "result": {"title": title}},
    }), encoding="utf-8")
    (folder / "transcript.json").write_text(json.dumps({
        "segments": [{"start": 2.5, "end": 7.25, "text": title}], "source": "platform_subtitle", "language": "zh",
    }), encoding="utf-8")
    (folder / "transcript.md").write_text(f"# {title}\n\n[00:02-00:07] {title}\n", encoding="utf-8")
    (folder / "audio.mp3").write_bytes(b"raw media must remain on its original device")
    return task_id


def launch(workspace, mode, port, log):
    address = f"http://127.0.0.1:{port}"
    environment = {**os.environ, "PYTHONUTF8": "1", "VIDEOTONOTES_WORKSPACE": str(workspace),
                   "WHISPER_CACHE_DIR": str(workspace / "_models"), "VIDEOTONOTES_MODE": mode,
                   "VIDEOTONOTES_PUBLIC_ORIGIN": address if mode == "server" else "",
                   "VIDEOTONOTES_ACCESS_TOKEN": TOKEN if mode == "server" else "",
                   "MAX_CONCURRENT_TASKS": "5"}
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
                                "--port", str(port), "--no-proxy-headers"], cwd=ROOT, env=environment,
                               stdout=log, stderr=log,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return process, address


def test_two_running_instances_sync_and_authenticate(tmp_path):
    local_workspace, remote_workspace = tmp_path / "local", tmp_path / "server"
    local_id = document(local_workspace, "本机完整转录")
    remote_id = document(remote_workspace, "服务器完整转录")
    processes = []
    headers = {"Authorization": "Bearer " + TOKEN}
    with (tmp_path / "instances.log").open("wb") as log, httpx.Client(timeout=20, trust_env=False) as client:
        try:
            local, local_url = launch(local_workspace, "desktop", free_port(), log)
            processes.append(local)
            remote, remote_url = launch(remote_workspace, "server", free_port(), log)
            processes.append(remote)
            deadline = time.monotonic() + 45
            for address, authorization in ((local_url, {}), (remote_url, headers)):
                while True:
                    try:
                        health = client.get(address + "/api/health", headers=authorization)
                        if health.status_code == 200:
                            assert health.json()["max_concurrent_tasks"] == 5
                            break
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline, "Backend failed to start; inspect instances.log"
                    time.sleep(0.1)
            assert client.get(remote_url + "/api/health").status_code == 401
            assert client.get(remote_url + "/script.js").status_code == 401
            assert "管理员登录" in client.get(remote_url + "/").text
            assert client.get(remote_url + "/mcp/sse").status_code == 401
            with client.stream("GET", remote_url + "/mcp/sse", headers=headers) as stream:
                assert stream.status_code == 200
                assert next(stream.iter_lines()) == "event: endpoint"
            config = client.post(local_url + "/api/sync/config", json={"remote_url": remote_url, "token": TOKEN, "enabled": False})
            assert config.status_code == 200
            assert config.json()["has_token"] and TOKEN not in config.text
            cycle = client.post(local_url + "/api/sync/run").json()
            assert cycle["last_success"] and not cycle["last_error"]
            for address, authorization in ((local_url, {}), (remote_url, headers)):
                task_ids = {item["task_id"] for item in client.get(address + "/api/tasks", headers=authorization).json()["tasks"]}
                assert {local_id, remote_id} <= task_ids
            pulled = client.get(local_url + f"/api/task/{remote_id}/transcript").json()["segments"]
            assert pulled == [{"start": 2.5, "end": 7.25, "text": "服务器完整转录"}]
            assert not (local_workspace / remote_id / "audio.mp3").exists()
            assert not (remote_workspace / local_id / "audio.mp3").exists()
            revision_before = client.get(remote_url + "/api/sync/manifest", headers=headers).json()
            client.post(local_url + "/api/sync/run").raise_for_status()
            assert client.get(remote_url + "/api/sync/manifest", headers=headers).json() == revision_before
            login = client.post(remote_url + "/auth/login", data={"token": TOKEN}, headers={"Origin": remote_url})
            assert login.status_code == 303
            assert client.get(remote_url + "/api/health").status_code == 200
            assert client.post(remote_url + "/api/sync/run").status_code == 403
            assert client.post(remote_url + "/auth/logout", headers={"Origin": remote_url}).status_code == 303
            assert client.get(remote_url + "/api/health").status_code == 401
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
