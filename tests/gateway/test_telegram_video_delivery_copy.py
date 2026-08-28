"""Regression tests for Telegram portrait-MP4 delivery copy (faststart remux).

Covers the delivery lifecycle added to ``TelegramAdapter.send_video``:

- a task-scoped derived MP4 (``ffmpeg -c copy -movflags +faststart``) is
  produced at send time and used for the Telegram upload;
- the remuxed output preserves the portrait resolution/aspect and the
  full duration of the source (probed directly from the actual path
  handed to the Telegram upload);
- the canonical producer file is never modified;
- derived temp files are cleaned up on success and on failure;
- ffmpeg absence degrades to the original send path (no crash, original
  file still used);
- the image/document send paths are untouched by the video-only delivery
  copy (no extra temp files created for documents/photos).
"""

import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from plugins.platforms.telegram.adapter import TelegramAdapter


FFMPEG = shutil.which("ffmpeg") or ""
FFPROBE = shutil.which("ffprobe") or ""

requires_ffmpeg = pytest.mark.skipif(
    not FFMPEG or not FFPROBE,
    reason="ffmpeg/ffprobe not available in this env",
)


def _make_adapter() -> TelegramAdapter:
    from gateway.config import PlatformConfig

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = None
    return adapter


def _make_portrait_mp4(path: str) -> None:
    """Generate a HEVC portrait MP4 (640x832, 1s, moov at end) via ffmpeg."""
    proc = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=640x832:rate=30:duration=1",
         "-c:v", "libx265", "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
         "-an", path],
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def _moov_offset(path: str) -> int:
    """Byte offset of the first moov box; -1 when absent."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 8:
        return -1
    pos = 0
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        if size < 8 or pos + size > len(data):
            return -1
        if data[pos + 4:pos + 8] == b"moov":
            return pos
        pos += size
    return -1


def _probe_width_height(path: str) -> tuple:
    """Return (width, height) via ffprobe, or (-1, -1) on failure."""
    proc = subprocess.run(
        [FFPROBE, "-v", "error",
         "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", path],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return (-1, -1)
    parts = proc.stdout.strip().split("x")
    if len(parts) != 2:
        return (-1, -1)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (-1, -1)


def _probe_duration_seconds(path: str) -> float:
    """Read the container duration (seconds) from the given MP4 via ffprobe.

    Returns -1.0 when the file cannot be probed (assertion will surface it).
    """
    proc = subprocess.run(
        [FFPROBE, "-v", "error",
         "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return -1.0
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return -1.0


def _glob_telegram_tmp_files(root: str, prefix: str = "") -> list:
    """Files under root whose name looks like a telegram video delivery copy."""
    out = []
    for name in sorted(os.listdir(root)):
        if name.startswith(prefix) and name.endswith(".mp4"):
            out.append(os.path.join(root, name))
    return out


# ---------------------------------------------------------------------------
# Delivery-copy lifecycle
# ---------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.asyncio
async def test_portrait_video_is_remuxed_with_faststart_before_send(tmp_path, monkeypatch):
    """send_video derives a faststart copy and upload path is to that copy."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    src = tmp_path / "portrait.mp4"
    _make_portrait_mp4(str(src))
    original_bytes = src.read_bytes()
    assert _moov_offset(str(src)) > 0, "fixture must have moov at end"

    observations = {}

    async def fake_send_video(**kwargs):
        upload = kwargs["video"]
        observations["upload_path"] = upload.name
        # Probe the ACTUAL derived copy handed to Telegram (not the fixture).
        observations["moov_offset"] = _moov_offset(upload.name)
        observations["width_height"] = _probe_width_height(upload.name)
        observations["duration"] = _probe_duration_seconds(upload.name)
        return SimpleNamespace(message_id=99)

    adapter = _make_adapter()
    adapter._bot = SimpleNamespace(send_video=fake_send_video)

    result = await adapter.send_video(chat_id="123", video_path=str(src),
                                      caption="hi")

    assert result.success is True
    assert result.message_id == "99"
    upload_path = observations["upload_path"]
    assert upload_path != str(src), "upload must use a derived copy, not the original"
    assert os.path.dirname(upload_path) == str(tmp_path), (
        "derived copy must live in the task-scoped temp dir"
    )
    # moov moved to front (faststart) => Telegram probes real dims/duration.
    assert observations["moov_offset"] <= 64, "moov must be near the front"
    assert observations["width_height"] == (640, 832), (
        f"expected 640x832, got {observations['width_height']}"
    )
    # Portrait aspect preserved and the remuxed output keeps the full
    # duration of the source (regression for Telegram's duration=0 probe).
    assert observations["duration"] >= 0.99, (
        f"expected remuxed duration >= 0.99s, got {observations['duration']}"
    )
    src_duration = _probe_duration_seconds(str(src))
    assert abs(observations["duration"] - src_duration) <= 0.05, (
        f"remuxed duration {observations['duration']} drifts from source "
        f"{src_duration}"
    )
    # Canonical producer artifact untouched.
    assert src.read_bytes() == original_bytes


@requires_ffmpeg
@pytest.mark.asyncio
async def test_success_cleans_up_derived_copy(tmp_path, monkeypatch):
    """Derived temp file is removed after a successful send."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    src = tmp_path / "portrait.mp4"
    _make_portrait_mp4(str(src))

    async def fake_send_video(**kwargs):
        return SimpleNamespace(message_id=7)

    adapter = _make_adapter()
    adapter._bot = SimpleNamespace(send_video=fake_send_video)
    result = await adapter.send_video(chat_id="123", video_path=str(src))
    assert result.success is True
    assert _glob_telegram_tmp_files(str(tmp_path), prefix="telegram_video_") == []


@requires_ffmpeg
@pytest.mark.asyncio
async def test_send_failure_cleans_up_derived_copy(tmp_path, monkeypatch):
    """Derived temp file is removed when the Telegram upload fails."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    src = tmp_path / "portrait.mp4"
    _make_portrait_mp4(str(src))

    class Boom(RuntimeError):
        pass

    async def fake_send_video(**kwargs):
        raise Boom("upload blew up")

    adapter = _make_adapter()
    adapter._bot = SimpleNamespace(send_video=fake_send_video)
    # The adapter falls back to the base implementation, which warns and
    # returns a friendly failure; either way the derived file must be gone.
    await adapter.send_video(chat_id="123", video_path=str(src))
    assert _glob_telegram_tmp_files(str(tmp_path), prefix="telegram_video_") == []
    assert src.exists(), "original input must be preserved"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_remux_failure_falls_back_to_original(tmp_path, monkeypatch):
    """When ffmpeg remux fails, send the original path (prior behavior)."""
    src = tmp_path / "portrait.mp4"
    _make_portrait_mp4(str(src))

    sent = {}

    async def fake_send_video(**kwargs):
        sent["video"] = kwargs["video"]
        # Probe the ACTUAL path handed to Telegram: original must keep its
        # portrait dimensions and full duration (no drift, no truncation).
        sent["width_height"] = _probe_width_height(kwargs["video"].name)
        sent["duration"] = _probe_duration_seconds(kwargs["video"].name)
        return SimpleNamespace(message_id=12)

    adapter = _make_adapter()
    adapter._bot = SimpleNamespace(send_video=fake_send_video)

    # Force the delivery-copy builder to fail (e.g. ffmpeg missing).
    import plugins.platforms.telegram.adapter as tga_mod
    monkeypatch.setattr(tga_mod, "_build_delivery_video_path",
                        lambda *a, **kw: (None, "no ffmpeg"))

    result = await adapter.send_video(chat_id="123", video_path=str(src))
    assert result.success is True
    assert sent["video"].name == str(src), "original file must be sent when remux unavailable"
    assert sent["width_height"] == (640, 832), (
        f"expected 640x832, got {sent['width_height']}"
    )
    assert sent["duration"] >= 0.99, (
        f"expected fallback duration >= 0.99s, got {sent['duration']}"
    )


@pytest.mark.asyncio
async def test_no_delivery_copy_when_ffmpeg_missing(tmp_path, monkeypatch):
    """Without ffmpeg on PATH, send_video uses the original path directly."""
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + b"\x00" * 100)

    sent = {}

    async def fake_send_video(**kwargs):
        sent["video"] = kwargs["video"]
        return SimpleNamespace(message_id=3)

    import plugins.platforms.telegram.adapter as tga_mod
    monkeypatch.setattr(tga_mod, "_build_delivery_video_path",
                        lambda *a, **kw: (None, "ffmpeg not found"))

    adapter = _make_adapter()
    adapter._bot = SimpleNamespace(send_video=fake_send_video)
    result = await adapter.send_video(chat_id="123", video_path=str(src))
    assert result.success is True
    assert sent["video"].name == str(src)


# ---------------------------------------------------------------------------
# Image/document paths stay untouched by the video delivery copy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_send_does_not_create_delivery_copy(tmp_path):
    """send_document keeps its existing behavior (no video remux)."""
    src = tmp_path / "report.txt"
    src.write_text("hello", encoding="utf-8")

    sent = {}

    async def fake_send_document(**kwargs):
        sent["document"] = kwargs["document"]
        return SimpleNamespace(message_id=4)

    adapter = _make_adapter()
    adapter._bot = SimpleNamespace(send_document=fake_send_document)
    result = await adapter.send_document(chat_id="123", file_path=str(src))
    assert result.success is True
    assert sent["document"].name == str(src)


@requires_ffmpeg
@pytest.mark.asyncio
async def test_voice_send_does_not_create_video_delivery_copy(tmp_path):
    """send_voice keeps its existing behavior (no video remux)."""
    src = tmp_path / "note.ogg"
    src.write_bytes(b"OggS" + b"\x00" * 64)

    sent = {}

    async def fake_send_voice(**kwargs):
        sent["voice"] = kwargs["voice"]
        return SimpleNamespace(message_id=5)

    adapter = _make_adapter()
    adapter._bot = SimpleNamespace(send_voice=fake_send_voice)
    result = await adapter.send_voice(chat_id="123", audio_path=str(src))
    assert result.success is True
    assert sent["voice"].name == str(src)
    assert _glob_telegram_tmp_files(str(tmp_path), prefix="telegram_video_") == []