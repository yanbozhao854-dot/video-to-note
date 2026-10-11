"""Private, two-way synchronization of finished video documents.

Only portable task artifacts are transferred. Credentials, media, model caches and
runtime paths remain on the originating computer. Deletions are intentionally not
propagated; concurrent edits preserve a deterministic conflict copy.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import io
import ipaddress
import json
import os
import re
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .secret_box import protect, unprotect

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_FILES = 512
INTERVAL_SECONDS = 30
TASK_FILES = {"task.json", "transcript.json", "transcript.md", "notes.md"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
TERMINAL = {"completed", "failed", "cancelled"}
MANIFEST_FIELDS = {
    "task_id", "created_at", "source_url", "normalized_source_url",
    "uploaded_filename", "processing_mode", "output", "origin", "summary_style",
    "reasoning_effort", "reused_task_id", "bilibili_pages", "bili_total_pages",
    "title", "owner", "duration", "source", "upload_date", "timestamp",
    "view_count", "like_count", "sync_conflict_of",
}
RUNTIME_FIELDS = {
    "status", "step", "step_name", "progress", "created_at", "finished_at",
    "elapsed_seconds",
}
RESULT_FIELDS = {
    "title", "output", "duration", "language", "transcript_source", "quality",
    "processing_seconds", "segment_count",
}
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ArtifactError(ValueError):
    pass


class RevisionConflict(ArtifactError):
    pass


class SyncConfig(BaseModel):
    remote_url: str = Field(default="", max_length=2048)
    token: str = Field(default="", max_length=4096)
    enabled: bool = False
    clear_token: bool = False


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_id(value: str) -> str:
    if not UUID_PATTERN.fullmatch(value) or str(uuid.UUID(value)) != value:
        raise ArtifactError("任务编号无效")
    return value


def _allowed_name(name: str) -> bool:
    if any(character in name for character in '\\:\x00<>"|?*'):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        return False
    if name in TASK_FILES:
        return True
    if len(path.name) > 255 or path.name.rstrip(" .") != path.name or any(ord(character) < 32 for character in path.name):
        return False
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", path.name.split(".")[0], re.IGNORECASE):
        return False
    return len(path.parts) == 2 and path.parts[0] == "frames" and path.suffix.lower() in IMAGE_SUFFIXES


def _portable_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        # Signed download URLs and credentials are unnecessary for document history.
        host = parts.hostname
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host += f":{parts.port}"
        query = urlencode([(key, val) for key, val in parse_qsl(parts.query) if key in {"v", "p", "list", "index", "t", "start"}])
        return urlunsplit((parts.scheme, host, parts.path, query, ""))
    except ValueError:
        return None


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _manifest(data: bytes, task_id: str) -> dict[str, Any]:
    try:
        original = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ArtifactError("任务记录格式无效") from exc
    if not isinstance(original, dict):
        raise ArtifactError("任务记录格式无效")
    result = {key: value for key, value in original.items() if key in MANIFEST_FIELDS and (value is None or isinstance(value, (str, int, float, bool)))}
    result["task_id"] = task_id
    for key in ("source_url", "normalized_source_url"):
        if key in result:
            result[key] = _portable_url(result[key])
    if isinstance(result.get("uploaded_filename"), str):
        result["uploaded_filename"] = result["uploaded_filename"].replace("\\", "/").split("/")[-1]
    pages = original.get("bilibili_pages")
    if isinstance(pages, list):
        result["bilibili_pages"] = [p for p in pages[:10000] if isinstance(p, int) and not isinstance(p, bool) and p > 0]
    runtime = original.get("runtime")
    if isinstance(runtime, dict):
        clean = {key: value for key, value in runtime.items() if key in RUNTIME_FIELDS and (value is None or isinstance(value, (str, int, float, bool)))}
        runtime_result = runtime.get("result")
        if isinstance(runtime_result, dict):
            clean["result"] = {key: value for key, value in runtime_result.items() if key in RESULT_FIELDS and (value is None or isinstance(value, (str, int, float, bool)))}
        result["runtime"] = clean
    return result


def _revision(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        name_bytes = name.encode("utf-8")
        data = files[name]
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _validate_files(files: dict[str, bytes], task_id: str) -> dict[str, bytes]:
    if "task.json" not in files or not any(name in files for name in ("notes.md", "transcript.json", "transcript.md")):
        raise ArtifactError("同步包缺少任务记录或文档")
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        raise ArtifactError("同步包超出大小限制")
    if any(not _allowed_name(name) or len(data) > MAX_FILE_BYTES for name, data in files.items()):
        raise ArtifactError("同步包包含不允许的文件")
    metadata = _manifest(files["task.json"], task_id)
    status = metadata.get("runtime", {}).get("status")
    if status and status not in TERMINAL:
        raise ArtifactError("只同步已结束的任务")
    if not status:
        metadata["runtime"] = {"status": "completed"}
    if "transcript.json" in files:
        try:
            transcript = json.loads(files["transcript.json"])
        except (ValueError, UnicodeDecodeError) as exc:
            raise ArtifactError("转录文件格式无效") from exc
        if not isinstance(transcript, dict) or not isinstance(transcript.get("segments"), list):
            raise ArtifactError("转录文件格式无效")
        clean_segments = []
        for segment in transcript["segments"]:
            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                raise ArtifactError("转录段落格式无效")
            if any(not isinstance(segment.get(key), (int, float)) for key in ("start", "end")):
                raise ArtifactError("转录时间戳格式无效")
            clean_segments.append({key: segment[key] for key in ("start", "end", "text")})
        cleaned = {"segments": clean_segments}
        for key in ("language", "source"):
            if isinstance(transcript.get(key), str):
                cleaned[key] = transcript[key]
        quality = transcript.get("quality")
        if isinstance(quality, dict):
            cleaned["quality"] = {key: value for key, value in quality.items() if key in {"characters", "speech_seconds", "speech_coverage", "characters_per_second", "insufficient"} and isinstance(value, (int, float, bool))}
        files = dict(files)
        files["transcript.json"] = _json_bytes(cleaned)
    files = dict(files)
    files["task.json"] = _json_bytes(metadata)
    return files


def _pack(files: dict[str, bytes]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    payload = target.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ArtifactError("同步包超出大小限制")
    return payload


def _unpack(payload: bytes, task_id: str) -> dict[str, bytes]:
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ArtifactError("同步包超出大小限制")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES or sum(item.file_size for item in members) > MAX_TOTAL_BYTES:
                raise ArtifactError("同步包超出大小限制")
            files: dict[str, bytes] = {}
            for item in members:
                if (not _allowed_name(item.filename) or item.filename in files
                    or item.file_size > MAX_FILE_BYTES or item.flag_bits & 1
                    or stat.S_ISLNK(item.external_attr >> 16)):
                    raise ArtifactError("同步包包含不允许的文件")
                files[item.filename] = archive.read(item)
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError) as exc:
        raise ArtifactError("同步包格式无效") from exc
    return _validate_files(files, task_id)


def _remote_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError
        # Validate the port without invoking DNS or contacting the destination.
        _ = parts.port
        loopback = parts.hostname.lower() == "localhost"
        with contextlib.suppress(ValueError):
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
            raise ValueError
    except ValueError as exc:
        raise ArtifactError("服务器地址须使用 HTTPS；本机测试可使用 HTTP") from exc
    return value


class SyncService:
    def __init__(self, workspace: Path, on_import: Callable[[str], Any] | None = None):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.private = self.workspace / ".sync"
        self.private.mkdir(exist_ok=True)
        if self.private.is_symlink():
            raise ArtifactError("同步配置目录不能是符号链接")
        with contextlib.suppress(OSError):
            self.private.chmod(0o700)
        self.on_import = on_import
        self.router = APIRouter(prefix="/api/sync", tags=["sync"])
        self.lock = asyncio.Lock()
        self.worker: asyncio.Task | None = None
        self.config = self._read_json(self.private / "config.json", {})
        self.baselines = self._read_json(self.private / "index.json", {})
        identity = self._read_json(self.private / "instance.json", {})
        self.instance_id = identity.get("instance_id")
        if not isinstance(self.instance_id, str) or not UUID_PATTERN.fullmatch(self.instance_id):
            self.instance_id = str(uuid.uuid4())
            self._write_json(self.private / "instance.json", {"instance_id": self.instance_id})
        self.last_success: str | None = None
        self.last_error: str | None = None
        self.message = "尚未连接服务器"
        self.running = False
        self._register_routes()

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            if path.is_symlink():
                return default
            result = json.loads(path.read_text(encoding="utf-8"))
            return result if isinstance(result, type(default)) else default
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        if path.is_symlink():
            raise ArtifactError("配置文件不能是符号链接")
        fd, name = tempfile.mkstemp(prefix=".sync-write-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(_json_bytes(value))
            with contextlib.suppress(OSError):
                os.chmod(name, 0o600)
            os.replace(name, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)

    def public_config(self) -> dict[str, Any]:
        return {"remote_url": self.config.get("remote_url", ""), "enabled": bool(self.config.get("enabled")), "has_token": bool(self.config.get("token"))}

    def status(self) -> dict[str, Any]:
        return {"enabled": bool(self.config.get("enabled")), "running": self.running, "last_success": self.last_success, "last_error": self.last_error, "message": self.message}

    def save_config(self, request: SyncConfig) -> dict[str, Any]:
        address = _remote_url(request.remote_url)
        token = self.config.get("token")
        if request.clear_token:
            token = None
        if request.token.strip():
            if (self.private / "entropy.bin").is_symlink():
                raise ArtifactError("密钥配置文件不能是符号链接")
            token = protect(request.token.strip(), entropy_file=self.private / "entropy.bin")
        if request.enabled and (not address or not token):
            raise ArtifactError("开启自动同步前，请填写服务器地址和连接口令")
        changed_server = address != self.config.get("remote_url", "")
        updated = {"remote_url": address, "enabled": request.enabled, "token": token}
        self._write_json(self.private / "config.json", updated)
        self.config = updated
        if changed_server:
            self.baselines = {}
            self._write_json(self.private / "index.json", {})
        self.last_error = None
        self.message = "自动同步已开启" if request.enabled else "自动同步已关闭"
        return self.public_config()

    def snapshot(self, task_id: str) -> dict[str, bytes] | None:
        _task_id(task_id)
        directory = self.workspace / task_id
        if not directory.exists():
            return None
        if directory.is_symlink() or not directory.is_dir():
            raise ArtifactError("任务目录无效")
        files: dict[str, bytes] = {}

        def read_file(name: str, path: Path) -> None:
            if len(files) >= MAX_FILES or path.stat().st_size > MAX_FILE_BYTES:
                raise ArtifactError("同步文件超出大小限制")
            if sum(map(len, files.values())) + path.stat().st_size > MAX_TOTAL_BYTES:
                raise ArtifactError("同步包超出大小限制")
            with path.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES or sum(map(len, files.values())) + len(data) > MAX_TOTAL_BYTES:
                raise ArtifactError("同步文件超出大小限制")
            files[name] = data

        for name in TASK_FILES:
            path = directory / name
            if path.is_symlink():
                raise ArtifactError("同步文件不能是符号链接")
            if path.is_file():
                read_file(name, path)
        frames = directory / "frames"
        if frames.is_symlink():
            raise ArtifactError("截图目录不能是符号链接")
        if frames.is_dir():
            for path in frames.iterdir():
                if path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                if path.is_symlink():
                    raise ArtifactError("同步文件不能是符号链接")
                if path.is_file():
                    read_file(f"frames/{path.name}", path)
        return _validate_files(files, task_id)

    def manifest(self) -> dict[str, Any]:
        records = []
        for directory in self.workspace.iterdir():
            if not UUID_PATTERN.fullmatch(directory.name):
                continue
            try:
                files = self.snapshot(directory.name)
                if files:
                    records.append({"task_id": directory.name, "revision": _revision(files)})
            except (ArtifactError, OSError):
                continue
        return {"tasks": sorted(records, key=lambda item: item["task_id"]), "instance_id": self.instance_id}

    async def import_files(self, task_id: str, files: dict[str, bytes], expected_revision: str | None = None) -> str:
        _task_id(task_id)
        files = _validate_files(files, task_id)
        directory = self.workspace / task_id
        old = None
        if directory.exists():
            old = self.snapshot(task_id)  # Refuses active tasks and unsafe existing paths.
        old_revision = _revision(old) if old else ""
        incoming_revision = _revision(files)
        if expected_revision is not None and old_revision != expected_revision:
            raise RevisionConflict("任务已被另一端更新，请重新同步")
        if old_revision == incoming_revision:
            return task_id
        if old and expected_revision is None:
            task_id, files = self.conflict_copy(task_id, files)
            return await self.import_files(task_id, files, expected_revision=None)
        directory.mkdir(exist_ok=True)
        if directory.is_symlink():
            raise ArtifactError("任务目录无效")
        # Validate every target before writing any file, including existing frames.
        for name in files:
            target = directory / name
            if target.is_symlink() or target.parent.is_symlink():
                raise ArtifactError("同步文件不能是符号链接")
        for name, data in files.items():
            target = directory / name
            target.parent.mkdir(exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".sync-artifact-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.replace(temporary, target)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
        # Remove obsolete synchronized artifacts only after the replacement validates.
        for name in (old or {}):
            if name not in files:
                (directory / name).unlink(missing_ok=True)
        if self.on_import:
            outcome = self.on_import(task_id)
            if inspect.isawaitable(outcome):
                await outcome
        return task_id

    @staticmethod
    def conflict_copy(task_id: str, files: dict[str, bytes]) -> tuple[str, dict[str, bytes]]:
        copied_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"videotonotes:{task_id}:{_revision(files)}"))
        copied = dict(files)
        metadata = _manifest(copied["task.json"], copied_id)
        metadata["sync_conflict_of"] = task_id
        title = str(metadata.get("title") or "视频文档")
        metadata["title"] = title + "（同步冲突副本）"
        if isinstance(metadata.get("runtime", {}).get("result"), dict):
            metadata["runtime"]["result"]["title"] = metadata["title"]
        copied["task.json"] = _json_bytes(metadata)
        for name in ("notes.md", "transcript.md"):
            if name in copied:
                copied[name] = copied[name].replace(task_id.encode(), copied_id.encode())
        return copied_id, copied

    def _register_routes(self) -> None:
        @self.router.get("/config")
        async def get_config():
            return self.public_config()

        @self.router.post("/config")
        async def set_config(config: SyncConfig, request: Request):
            async with self.lock:
                try:
                    if config.remote_url:
                        destination = urlsplit(_remote_url(config.remote_url))
                        current = request.url
                        def local_host(host: str | None) -> bool:
                            if host == "localhost":
                                return True
                            try:
                                return ipaddress.ip_address(host or "").is_loopback
                            except ValueError:
                                return False
                        same_host = destination.hostname == current.hostname or (local_host(destination.hostname) and local_host(current.hostname))
                        same_port = (destination.port or (443 if destination.scheme == "https" else 80)) == (current.port or (443 if current.scheme == "https" else 80))
                        if same_host and same_port:
                            raise ArtifactError("服务器地址指向当前应用，请填写另一端服务器地址")
                    return self.save_config(config)
                except ArtifactError as exc:
                    raise HTTPException(422, str(exc)) from exc

        @self.router.get("/status")
        async def get_status():
            return self.status()

        @self.router.post("/run")
        async def run_sync():
            await self.run_once()
            return self.status()

        @self.router.get("/manifest")
        async def get_manifest():
            # Read-only discovery stays available while a cycle is running. In
            # particular, a mistaken connection to this same instance must not
            # wait on its own worker lock.
            return await asyncio.to_thread(self.manifest)

        @self.router.get("/task/{task_id}")
        async def get_artifact(task_id: str):
            async with self.lock:
                try:
                    files = await asyncio.to_thread(self.snapshot, task_id)
                    if files is None:
                        raise HTTPException(404, "任务不存在")
                    payload = await asyncio.to_thread(_pack, files)
                    return Response(payload, media_type="application/zip", headers={"ETag": _revision(files), "Cache-Control": "no-store"})
                except (ArtifactError, OSError) as exc:
                    raise HTTPException(422, "任务不能同步或文件不安全") from exc

        @self.router.put("/task/{task_id}")
        async def put_artifact(task_id: str, request: Request, expected_revision: str | None = None):
            async with self.lock:
                try:
                    _task_id(task_id)
                    chunks = bytearray()
                    async for chunk in request.stream():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_ARCHIVE_BYTES:
                            raise HTTPException(413, "同步包超出大小限制")
                    files = await asyncio.to_thread(_unpack, bytes(chunks), task_id)
                    imported_id = await self.import_files(task_id, files, expected_revision)
                    return {"task_id": imported_id, "revision": _revision(self.snapshot(imported_id))}
                except RevisionConflict as exc:
                    raise HTTPException(409, str(exc)) from exc
                except (ArtifactError, OSError) as exc:
                    raise HTTPException(422, "同步包无效或任务不能覆盖") from exc

    async def run_once(self, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
        if self.lock.locked():
            return self.status()
        async with self.lock:
            address = self.config.get("remote_url", "")
            if not address or not self.config.get("token"):
                self.message = "请先保存服务器地址和连接口令"
                return self.status()
            self.running = True
            try:
                address = _remote_url(address)
                token = unprotect(self.config["token"], entropy_file=self.private / "entropy.bin")
                headers = {"Authorization": f"Bearer {token}"}
                async with contextlib.AsyncExitStack() as stack:
                    if client is None:
                        client = await stack.enter_async_context(httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False))
                    await self._exchange(client, address, headers)
                self.last_success = _utcnow()
                self.last_error = None
                self.message = "同步完成，转录和笔记已更新"
            except asyncio.CancelledError:
                raise
            except ArtifactError as exc:
                self.last_error = str(exc)
                self.message = str(exc)
            except Exception:
                # Never include exception URLs or headers: they can contain credentials.
                self.last_error = "无法完成同步，请检查服务器地址、连接口令和网络"
                self.message = "连接暂时不可用，本机仍可使用；稍后自动重试"
            finally:
                self.running = False
            return self.status()

    async def _exchange(self, client: httpx.AsyncClient, address: str, headers: dict[str, str]) -> None:
        async def request(method: str, path: str, **kwargs):
            response = await client.request(method, address + "/api/sync" + path, headers=headers, follow_redirects=False, **kwargs)
            if 300 <= response.status_code < 400:
                raise ArtifactError("服务器不应重定向同步请求")
            response.raise_for_status()
            return response

        remote_response = await request("GET", "/manifest")
        remote_payload = remote_response.json()
        if isinstance(remote_payload, dict) and remote_payload.get("instance_id") == self.instance_id:
            raise ArtifactError("服务器地址指向当前应用，请填写另一端服务器地址")
        records = remote_payload.get("tasks") if isinstance(remote_payload, dict) else None
        if not isinstance(records, list) or len(records) > 10000:
            raise ArtifactError("服务器同步记录无效")
        remote: dict[str, str] = {}
        for item in records:
            task_id = _task_id(item["task_id"])
            revision = item.get("revision", "")
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
                raise ArtifactError("服务器同步记录无效")
            remote[task_id] = revision
        local = {item["task_id"]: item["revision"] for item in (await asyncio.to_thread(self.manifest))["tasks"]}

        async def push(task_id: str, files: dict[str, bytes], expected: str):
            response = await request("PUT", f"/task/{task_id}", params={"expected_revision": expected}, content=await asyncio.to_thread(_pack, files))
            result = response.json()
            if result.get("task_id") != task_id or result.get("revision") != _revision(files):
                raise ArtifactError("服务器未确认同步结果")

        async def pull(task_id: str, expected: str, revision: str):
            # Bound streamed responses instead of trusting a remote Content-Length.
            async with client.stream("GET", address + f"/api/sync/task/{task_id}", headers=headers, follow_redirects=False) as response:
                if 300 <= response.status_code < 400:
                    raise ArtifactError("服务器不应重定向同步请求")
                response.raise_for_status()
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_ARCHIVE_BYTES:
                        raise ArtifactError("同步包超出大小限制")
            files = await asyncio.to_thread(_unpack, bytes(payload), task_id)
            if _revision(files) != revision:
                raise RevisionConflict("服务器文件已变化，请重新同步")
            await self.import_files(task_id, files, expected)

        for task_id in sorted(local.keys() | remote.keys()):
            local_revision, remote_revision = local.get(task_id), remote.get(task_id)
            if local_revision == remote_revision:
                self.baselines[task_id] = local_revision
            elif local_revision is None:
                await pull(task_id, "", remote_revision)
                self.baselines[task_id] = remote_revision
            elif remote_revision is None:
                files = await asyncio.to_thread(self.snapshot, task_id)
                if _revision(files) != local_revision:
                    raise RevisionConflict("本机文件已变化，请重新同步")
                await push(task_id, files, "")
                self.baselines[task_id] = local_revision
            elif self.baselines.get(task_id) == local_revision:
                await pull(task_id, local_revision, remote_revision)
                self.baselines[task_id] = remote_revision
            else:
                files = await asyncio.to_thread(self.snapshot, task_id)
                if _revision(files) != local_revision:
                    raise RevisionConflict("本机文件已变化，请重新同步")
                if self.baselines.get(task_id) == remote_revision:
                    await push(task_id, files, remote_revision)
                    self.baselines[task_id] = local_revision
                else:
                    copied_id, copied_files = self.conflict_copy(task_id, files)
                    await self.import_files(copied_id, copied_files)
                    copied_revision = _revision(copied_files)
                    if remote.get(copied_id) != copied_revision:
                        await push(copied_id, copied_files, remote.get(copied_id, ""))
                    self.baselines[copied_id] = copied_revision
                    await pull(task_id, local_revision, remote_revision)
                    self.baselines[task_id] = remote_revision
            # Persist each completed transfer so an interrupted cycle can resume safely.
            self._write_json(self.private / "index.json", self.baselines)

    async def start(self) -> None:
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._loop(), name="video-document-sync")

    async def stop(self) -> None:
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
            self.worker = None

    async def _loop(self) -> None:
        while True:
            if self.config.get("enabled"):
                await self.run_once()
            await asyncio.sleep(INTERVAL_SECONDS)


def create_sync_service(workspace: Path, on_import: Callable[[str], Any] | None = None) -> SyncService:
    return SyncService(workspace, on_import=on_import)
