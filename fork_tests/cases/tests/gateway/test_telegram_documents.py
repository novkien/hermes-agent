"""Fork regression for Telegram video dimensions passed to sendVideo."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as telegram_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_send_video_forwards_source_metadata(tmp_path, monkeypatch):
    """Explicit dimensions prevent Telegram's bad square server probe."""
    test_file = tmp_path / "clip.mp4"
    test_file.write_bytes(b"not-a-real-mp4")

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    mock_msg = MagicMock()
    mock_msg.message_id = 202
    adapter._bot.send_video = AsyncMock(return_value=mock_msg)
    monkeypatch.setattr(
        telegram_adapter,
        "_probe_video_metadata",
        lambda _path: {"width": 736, "height": 416, "duration": 20},
    )

    result = await adapter.send_video(
        chat_id="12345",
        video_path=str(test_file),
    )

    assert result.success is True
    call_kwargs = adapter._bot.send_video.call_args.kwargs
    assert call_kwargs["width"] == 736
    assert call_kwargs["height"] == 416
    assert call_kwargs["duration"] == 20
