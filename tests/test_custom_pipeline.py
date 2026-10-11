"""User-visible subtitle consent and five-task queue behavior."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend import main
from backend.config_store import ConfigStore
from backend.transcript import TranscriptSegment
from backend.video_processor import SubtitleResult, VideoProcessor


@pytest.fixture
def isolated_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    processor = VideoProcessor(tmp_path)
    monkeypatch.setattr(main, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(main, "config_store", ConfigStore(tmp_path))
    monkeypatch.setattr(main, "video_processor", processor)
    monkeypatch.setattr(main, "tasks", {})
    monkeypatch.setattr(main, "_task_notify_hook", None)
    processor.get_video_info = AsyncMock(return_value={
        "title": "字幕与语音选择", "source": "youtube", "duration": 30,
    })
    processor.fetch_subtitles = AsyncMock(return_value=None)
    processor.download_audio = AsyncMock(return_value=tmp_path / "audio.m4a")
    transcribe = AsyncMock(return_value={
        "segments": [TranscriptSegment(0, 30, "这是明确允许识别之后生成的完整测试转录。")],
        "language": "zh", "duration": 30, "device": "cpu",
        "model": "base", "requested_model": "base",
    })
    monkeypatch.setattr(main, "run_transcription", transcribe)
    task_id = "consent-pipeline"
    (tmp_path / task_id).mkdir()
    main.tasks[task_id] = main.new_task(task_id=task_id)
    return processor, transcribe, task_id


def transcript_request(**kwargs):
    return main.SummarizeRequest(
        video_url="https://www.youtube.com/watch?v=consent-test",
        output="transcript", llm_config=main.LLMConfig(), **kwargs,
    )


@pytest.mark.asyncio
async def test_missing_subtitles_wait_for_explicit_asr_consent(isolated_pipeline, tmp_path):
    processor, transcribe, task_id = isolated_pipeline
    request = transcript_request()
    assert request.allow_asr is False
    assert main.TranscribeRequest().allow_asr is False

    await main.process_video_task(task_id, request)

    processor.fetch_subtitles.assert_awaited_once()
    processor.download_audio.assert_not_awaited()
    transcribe.assert_not_awaited()
    task = main.tasks[task_id]
    assert task["status"] == "failed"
    assert "无字幕时允许语音识别" in task["error"]
    persisted = json.loads((tmp_path / task_id / "task.json").read_text(encoding="utf-8"))
    assert persisted["runtime"]["status"] == "failed"
    assert not (tmp_path / task_id / "transcript.json").exists()


@pytest.mark.asyncio
async def test_explicit_asr_consent_downloads_and_exports_timestamped_transcript(
    isolated_pipeline, tmp_path,
):
    processor, transcribe, task_id = isolated_pipeline
    await main.process_video_task(task_id, transcript_request(allow_asr=True))

    processor.download_audio.assert_awaited_once()
    transcribe.assert_awaited_once()
    assert main.tasks[task_id]["status"] == "completed"
    markdown = (tmp_path / task_id / "transcript.md").read_text(encoding="utf-8")
    assert "[00:00-00:30]" in markdown
    assert "完整测试转录" in markdown


@pytest.mark.asyncio
async def test_existing_subtitles_complete_without_downloading_audio(isolated_pipeline, tmp_path):
    processor, transcribe, task_id = isolated_pipeline
    processor.fetch_subtitles.return_value = SubtitleResult(
        [TranscriptSegment(0, 4, "平台原字幕，一字不改保留。")], "zh", "platform_subtitle",
    )
    await main.process_video_task(task_id, transcript_request())

    processor.download_audio.assert_not_awaited()
    transcribe.assert_not_awaited()
    assert main.tasks[task_id]["status"] == "completed"
    assert "[00:00-00:04] 平台原字幕，一字不改保留。" in (
        tmp_path / task_id / "transcript.md"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_six_jobs_execute_five_and_queue_the_sixth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(main, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(main, "tasks", {})
    assert main.MAX_CONCURRENT_TASKS == 5
    monkeypatch.setattr(main, "task_slots", asyncio.Semaphore(main.MAX_CONCURRENT_TASKS))
    release = asyncio.Event()
    five_started = asyncio.Event()
    started: list[str] = []
    active = 0
    peak = 0

    async def blocked_process(task_id, request):
        nonlocal active, peak
        main.tasks[task_id]["status"] = "processing"
        started.append(task_id)
        active += 1
        peak = max(peak, active)
        if len(started) == 5:
            five_started.set()
        try:
            await release.wait()
            main.tasks[task_id]["status"] = "completed"
        finally:
            active -= 1

    monkeypatch.setattr(main, "process_video_task", blocked_process)
    job_ids = [f"parallel-job-{i}" for i in range(6)]
    for task_id in job_ids:
        (tmp_path / task_id).mkdir()
        main.tasks[task_id] = main.new_task(task_id=task_id)
    jobs = [asyncio.create_task(main.run_queued_video_task(task_id, transcript_request()))
            for task_id in job_ids]
    try:
        await asyncio.wait_for(five_started.wait(), timeout=2)
        # Let the sixth coroutine reach its semaphore acquisition.
        await asyncio.sleep(0)
        assert len(started) == 5
        assert sum(t["status"] == "processing" for t in main.tasks.values()) == 5
        assert sum(t["status"] == "queued" for t in main.tasks.values()) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*jobs), timeout=2)
        assert len(started) == 6
        assert peak == 5
        assert all(t["status"] == "completed" for t in main.tasks.values())
    finally:
        release.set()
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
