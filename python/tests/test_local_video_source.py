from __future__ import annotations

import asyncio
import builtins
import os
import sys
from fractions import Fraction
from pathlib import Path
from unittest.mock import Mock

import av
import pytest

from agent_sdk import AgentSdkError, ErrorCode
from agent_sdk.webrtc import AiortcMediaOffloadAdapter, VIDEO_RTP_PAYLOAD_BYTES, limit_video_frame_rate


VIDEO_PATH = (
    Path(__file__).parents[1]
    / "examples"
    / "assets"
    / "video-offload-test.mp4"
)


def test_aiortc_adapter_fails_early_with_current_interpreter_install_command(
    monkeypatch,
) -> None:
    real_import = builtins.__import__

    def import_without_aiortc(name, *args, **kwargs):
        if name == "aiortc":
            raise ModuleNotFoundError("No module named 'aiortc'", name="aiortc")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_aiortc)

    with pytest.raises(AgentSdkError) as caught:
        AiortcMediaOffloadAdapter(video_file_path=VIDEO_PATH)

    assert caught.value.code is ErrorCode.MEDIA_NEGOTIATION_FAILED
    assert "dependency is unavailable: aiortc" in str(caught.value)
    assert f"{sys.executable} -m pip install" in str(caught.value)


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


@pytest.mark.parametrize("codec", ["h264", "vpx"])
def test_real_encoder_packetization_fits_the_connect_ip_tun(codec) -> None:
    from aiortc.codecs import h264, vpx

    AiortcMediaOffloadAdapter()
    encoder = h264.H264Encoder() if codec == "h264" else vpx.Vp8Encoder()
    frame = av.VideoFrame(640, 480, "yuv420p")
    for plane in frame.planes:
        plane.update(os.urandom(plane.buffer_size))
    frame.pts = 0
    frame.time_base = Fraction(1, 90000)
    packets, timestamp = encoder.encode(frame, force_keyframe=True)
    assert len(packets) > 1
    assert timestamp == 0
    assert max(map(len, packets)) <= VIDEO_RTP_PAYLOAD_BYTES


class QueueCamera:
    def __init__(self):
        import asyncio
        self.frames = asyncio.Queue()
        self.readyState = "live"

    async def recv(self):
        return await self.frames.get()

    def stop(self):
        self.readyState = "ended"

    def put(self, pts):
        frame = av.VideoFrame(16, 16, "yuv420p")
        frame.pts = pts
        frame.time_base = Fraction(1, 1000000)
        self.frames.put_nowait(frame)


async def test_slow_sender_receives_latest_capture_instead_of_fifo_backlog():
    import asyncio
    camera = QueueCamera()
    track = limit_video_frame_rate(camera, 1000)
    camera.put(0)
    try:
        assert (await track.recv()).pts == 0
        # Capture continues even though the encoder/sender is not calling recv.
        for pts in range(1000, 21000, 1000):
            camera.put(pts)
        async with asyncio.timeout(1):
            while track.captured < 21:
                await asyncio.sleep(0)
        assert camera.frames.empty()
        assert (await track.recv()).pts == 20000
        assert track.dropped == 19
    finally:
        track.stop()
        await asyncio.sleep(0)
    assert track._reader.done()
    assert camera.readyState == "ended"


async def test_capture_drains_before_first_sender_request_during_signaling():
    camera = QueueCamera()
    track = limit_video_frame_rate(camera, 10)
    try:
        for pts in range(0, 21000, 1000):
            camera.put(pts)
        async with asyncio.timeout(1):
            while track.captured < 21:
                await asyncio.sleep(0)
        assert camera.frames.empty()
        assert (await track.recv()).pts == 20000
        assert track.dropped == 20
    finally:
        track.stop()
        await asyncio.sleep(0)


async def test_expired_capture_is_discarded_until_fresh_frame_arrives():
    import asyncio
    camera = QueueCamera()
    track = limit_video_frame_rate(camera, 1000, max_frame_age=0.01)
    camera.put(0)
    try:
        await track.recv()
        await asyncio.sleep(0.04)
        camera.put(1000)
        pending = asyncio.create_task(track.recv())
        await asyncio.sleep(0.01)
        assert not pending.done()
        camera.put(60000)
        assert (await asyncio.wait_for(pending, 1)).pts == 60000
        assert track.dropped >= 1
    finally:
        track.stop()
        await asyncio.sleep(0)


async def test_stopping_latest_track_unblocks_pending_receiver():
    import asyncio
    from aiortc.mediastreams import MediaStreamError
    camera = QueueCamera()
    track = limit_video_frame_rate(camera, 10)
    pending = asyncio.create_task(track.recv())
    await asyncio.sleep(0)
    track.stop()
    with pytest.raises(MediaStreamError):
        await asyncio.wait_for(pending, 1)
    await asyncio.sleep(0)
    assert track._reader.done()
