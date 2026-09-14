from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import av

from agent_sdk.webrtc import AiortcMediaOffloadAdapter


VIDEO_PATH = (
    Path(__file__).parents[1]
    / "examples"
    / "assets"
    / "video-offload-test.mp4"
)


def test_bundled_video_is_a_decodable_720p_h264_stream() -> None:
    assert VIDEO_PATH.is_file()
    with av.open(str(VIDEO_PATH)) as container:
        video = container.streams.video[0]
        frame = next(container.decode(video=0))

    assert video.codec_context.name == "h264"
    assert video.width == 1280
    assert video.height == 720
    assert frame.width == 1280
    assert frame.height == 720


async def test_bundled_video_produces_an_aiortc_media_frame() -> None:
    from aiortc.contrib.media import MediaPlayer

    player = MediaPlayer(str(VIDEO_PATH), loop=True)
    assert player.video is not None
    try:
        frame = await asyncio.wait_for(player.video.recv(), timeout=3.0)
        assert (frame.width, frame.height) == (1280, 720)
    finally:
        player.video.stop()


def test_aiortc_adapter_selects_and_loops_the_configured_file() -> None:
    player = object()
    player_factory = Mock(return_value=player)
    adapter = AiortcMediaOffloadAdapter(
        video_file_path=VIDEO_PATH,
        loop_video_file=True,
    )

    opened = adapter._open_video_player(
        player_factory,
        camera_id=7,
        width=640,
        height=480,
        fps=15,
    )

    assert opened is player
    player_factory.assert_called_once_with(str(VIDEO_PATH.resolve()), loop=True)


def test_aiortc_adapter_keeps_v4l2_camera_mode() -> None:
    player_factory = Mock(return_value=object())
    adapter = AiortcMediaOffloadAdapter()

    adapter._open_video_player(
        player_factory,
        camera_id=2,
        width=640,
        height=480,
        fps=15,
    )

    player_factory.assert_called_once_with(
        "/dev/video2",
        format="v4l2",
        options={"video_size": "640x480", "framerate": "15"},
    )
