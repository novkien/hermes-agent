"""Hardening regressions for Telegram faststart delivery copies."""

import asyncio
import os
import shutil
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

import plugins.platforms.telegram.adapter as telegram_adapter


FFMPEG = shutil.which("ffmpeg") or ""
FFPROBE = shutil.which("ffprobe") or ""
requires_ffmpeg = pytest.mark.skipif(
    not FFMPEG or not FFPROBE,
    reason="ffmpeg/ffprobe not available in this env",
)


def _box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def _synthetic_mp4(path, *, faststart: bool) -> None:
    ftyp = _box(b"ftyp", b"isom")
    mdat = _box(b"mdat", b"media")
    moov = _box(b"moov", b"meta")
    path.write_bytes(ftyp + (moov + mdat if faststart else mdat + moov))


def _make_mp4(path, *, faststart: bool = False, two_audio_tracks: bool = False) -> None:
    command = [
        FFMPEG,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x208:rate=10:duration=1",
    ]
    if two_audio_tracks:
        command.extend([
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=1",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
        ])
    command.extend(["-c:v", "mpeg4", "-q:v", "5"])
    if two_audio_tracks:
        command.extend(["-c:a", "aac"])
    else:
        command.append("-an")
    if faststart:
        command.extend(["-movflags", "+faststart"])
    command.append(str(path))
    proc = subprocess.run(command, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def _probe_stream_count(path: str, selector: str) -> int:
    proc = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            selector,
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return len([line for line in proc.stdout.splitlines() if line.strip()])


def _install_telegram_mock(monkeypatch, bot) -> None:
    telegram_module = ModuleType("telegram")
    constants_module = ModuleType("telegram.constants")
    telegram_module.Bot = lambda **kwargs: bot
    constants_module.ParseMode = SimpleNamespace(
        HTML="HTML",
        MARKDOWN_V2="MarkdownV2",
    )
    monkeypatch.setitem(sys.modules, "telegram", telegram_module)
    monkeypatch.setitem(sys.modules, "telegram.constants", constants_module)


def _delivery_files(root) -> list:
    return sorted(root.glob("telegram_video_*.mp4"))


def test_layout_probe_only_selects_mp4_with_trailing_moov(tmp_path):
    trailing = tmp_path / "trailing.mp4"
    faststart = tmp_path / "faststart.mp4"
    webm = tmp_path / "clip.webm"
    _synthetic_mp4(trailing, faststart=False)
    _synthetic_mp4(faststart, faststart=True)
    webm.write_bytes(b"not-an-mp4")

    assert telegram_adapter._mp4_has_trailing_moov(str(trailing)) is True
    assert telegram_adapter._mp4_has_trailing_moov(str(faststart)) is False
    assert telegram_adapter._mp4_has_trailing_moov(str(webm)) is False


@pytest.mark.asyncio
async def test_unaffected_videos_stay_on_zero_copy_path(tmp_path, monkeypatch):
    faststart = tmp_path / "faststart.mp4"
    webm = tmp_path / "clip.webm"
    _synthetic_mp4(faststart, faststart=True)
    webm.write_bytes(b"not-an-mp4")

    def unexpected_build(*args, **kwargs):
        raise AssertionError("unaffected video must not be remuxed")

    monkeypatch.setattr(
        telegram_adapter,
        "_build_delivery_video_path",
        unexpected_build,
    )
    assert await telegram_adapter._prepare_telegram_delivery_video_path(
        str(faststart)
    ) == (str(faststart), None)
    assert await telegram_adapter._prepare_telegram_delivery_video_path(str(webm)) == (
        str(webm),
        None,
    )


@pytest.mark.asyncio
async def test_missing_ffmpeg_keeps_original_native_path(tmp_path, monkeypatch):
    source = tmp_path / "trailing.mp4"
    _synthetic_mp4(source, faststart=False)
    monkeypatch.setattr(
        telegram_adapter,
        "_resolve_telegram_ffmpeg_binary",
        lambda: None,
    )

    assert await telegram_adapter._prepare_telegram_delivery_video_path(
        str(source)
    ) == (str(source), None)


def test_temp_allocation_failure_returns_fallback(tmp_path, monkeypatch):
    source = tmp_path / "trailing.mp4"
    _synthetic_mp4(source, faststart=False)
    monkeypatch.setattr(
        telegram_adapter,
        "_resolve_telegram_ffmpeg_binary",
        lambda: FFMPEG or "/fake/ffmpeg",
    )

    def fail_mkstemp(**kwargs):
        raise OSError("temp unavailable")

    monkeypatch.setattr(telegram_adapter.tempfile, "mkstemp", fail_mkstemp)
    derived, error = telegram_adapter._build_delivery_video_path(str(source))

    assert derived is None
    assert error == "ffmpeg remux error"


@pytest.mark.asyncio
async def test_cancelled_remux_cleans_late_worker_output(tmp_path, monkeypatch):
    source = tmp_path / "trailing.mp4"
    _synthetic_mp4(source, faststart=False)
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    late_output = tmp_path / "telegram_video_late.mp4"

    def delayed_build(video_path):
        started.set()
        assert release.wait(5)
        late_output.write_bytes(b"derived")
        completed.set()
        return str(late_output), None

    monkeypatch.setattr(
        telegram_adapter,
        "_build_delivery_video_path",
        delayed_build,
    )
    prepare_task = asyncio.create_task(
        telegram_adapter._prepare_telegram_delivery_video_path(str(source))
    )
    assert await asyncio.to_thread(started.wait, 2)
    prepare_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await prepare_task
    assert await asyncio.to_thread(completed.wait, 2)
    for _ in range(50):
        if not late_output.exists():
            break
        await asyncio.sleep(0.01)
    assert not late_output.exists()


def test_remux_command_maps_streams_and_uses_process_guards(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(
        telegram_adapter,
        "_resolve_telegram_ffmpeg_binary",
        lambda: "/fake/ffmpeg",
    )
    monkeypatch.setattr(telegram_adapter.subprocess, "run", fake_run)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    derived, error = telegram_adapter._build_delivery_video_path(str(source))
    try:
        assert error is None
        command = observed["command"]
        assert command[command.index("-map") + 1] == "0"
        assert observed["kwargs"]["stdin"] is subprocess.DEVNULL
        assert "creationflags" in observed["kwargs"]
    finally:
        telegram_adapter._remove_delivery_video_path(derived)


@requires_ffmpeg
def test_remux_preserves_all_input_streams(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    source = tmp_path / "multistream.mp4"
    _make_mp4(source, two_audio_tracks=True)
    assert _probe_stream_count(str(source), "a") == 2

    derived, error = telegram_adapter._build_delivery_video_path(str(source))
    try:
        assert error is None
        assert derived is not None
        assert _probe_stream_count(derived, "a") == 2
    finally:
        telegram_adapter._remove_delivery_video_path(derived)


def test_ffmpeg_resolver_uses_repository_discovery(monkeypatch):
    from plugins.platforms.discord import ffmpeg_utils

    monkeypatch.setattr(
        ffmpeg_utils,
        "resolve_ffmpeg_executable",
        lambda: "/opt/homebrew/bin/ffmpeg",
    )
    assert (
        telegram_adapter._resolve_telegram_ffmpeg_binary() == "/opt/homebrew/bin/ffmpeg"
    )


@requires_ffmpeg
@pytest.mark.asyncio
async def test_standalone_telegram_send_uses_and_cleans_faststart_copy(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    source = tmp_path / "portrait.mp4"
    _make_mp4(source)
    observed = {}

    async def fake_send_video(**kwargs):
        observed["path"] = kwargs["video"].name
        observed["filename"] = kwargs["filename"]
        observed["faststart"] = not telegram_adapter._mp4_has_trailing_moov(
            kwargs["video"].name
        )
        return SimpleNamespace(message_id=23)

    _install_telegram_mock(
        monkeypatch,
        SimpleNamespace(send_video=fake_send_video),
    )
    monkeypatch.setattr(
        "gateway.platforms.base.resolve_proxy_url",
        lambda *args, **kwargs: None,
    )
    from tools.send_message_tool import _send_telegram

    result = await _send_telegram(
        "token", "123", "", media_files=[(str(source), False)]
    )

    assert result["success"] is True
    assert observed["path"] != str(source)
    assert observed["filename"] == source.name
    assert observed["faststart"] is True
    assert _delivery_files(tmp_path) == []
