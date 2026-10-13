import asyncio
import gc
import threading
import time
import weakref
from fractions import Fraction

import av
import pytest
from aiortc.codecs.vpx import Vp8Encoder
from aiortc.mediastreams import MediaStreamError
from agent_sdk.camera import _DetachedCameraTrack, _detach_frame
from agent_sdk.webrtc import limit_video_frame_rate


@pytest.mark.parametrize('pixel_format', ['yuyv422', 'yuv420p', 'nv12'])
def test_detached_planes_do_not_alias_capture_memory(pixel_format):
    raw = av.VideoFrame(34, 18, pixel_format)
    raw.pts, raw.time_base = 123, Fraction(1, 90000)
    for plane in raw.planes:
        plane.update(bytes([57]) * plane.buffer_size)
    detached = _detach_frame(raw)
    assert detached.pts == raw.pts and detached.time_base == raw.time_base
    for original, copy in zip(raw.planes, detached.planes):
        assert original.buffer_ptr != copy.buffer_ptr
        original.update(bytes(original.buffer_size))
        assert bytes(copy)[0] == 57


class BorrowedFrame:
    def __init__(self, frame):
        self.frame = frame

    def __getattr__(self, name):
        return getattr(self.frame, name)


class Camera:
    def __init__(self):
        self.closed = False
        self.raw_refs = []
        self.active_generators = 0
        self.frames = 0
        self.closes = 0

    def decode(self, **kwargs):
        assert not self.closed
        self.active_generators += 1
        raw = av.VideoFrame(32, 32, 'yuyv422')
        raw.pts, raw.time_base = self.frames * 3000, Fraction(1, 90000)
        self.frames += 1
        raw = BorrowedFrame(raw)
        self.raw_refs.append(weakref.ref(raw))
        try:
            time.sleep(0.001)
            yield raw
        finally:
            self.active_generators -= 1

    def close(self):
        assert not self.active_generators
        assert all(ref() is None for ref in self.raw_refs)
        self.closed = True
        self.closes += 1


async def test_stop_with_pending_callbacks_and_delayed_encoder():
    camera = Camera()
    source = _DetachedCameraTrack(camera)
    track = limit_video_frame_rate(source, 1000)
    delivered = await asyncio.wait_for(track.recv(), 2)
    allowed, started = threading.Event(), threading.Event()

    def delayed_encode():
        started.set()
        assert allowed.wait(3)
        assert camera.closed
        assert Vp8Encoder().encode(delivered, True)[0]

    job = asyncio.create_task(asyncio.to_thread(delayed_encode))
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        time.sleep(0.03)  # simulate blocked event loop with capture still active
        track.stop()  # also used by RTP sender failure
        track.stop()
        assert not source._thread.is_alive()
        assert camera.closes == 1
        allowed.set()
        await asyncio.wait_for(job, 3)
        await asyncio.sleep(0)
        gc.collect()
        assert source._queue.qsize() == 1 and source._pending is None
        with pytest.raises(MediaStreamError):
            await track.recv()
    finally:
        allowed.set()
        track.stop()
        await job


async def test_stop_before_start_and_with_pending_recv():
    camera = Camera()
    source = _DetachedCameraTrack(camera)
    source.stop()
    assert camera.closes == 1 and camera.frames == 0
    with pytest.raises(MediaStreamError):
        await source.recv()
    camera = Camera()
    source = _DetachedCameraTrack(camera)
    pending = asyncio.create_task(source.recv())
    await asyncio.sleep(0)
    source.stop()
    with pytest.raises(MediaStreamError):
        await asyncio.wait_for(pending, 1)
    assert camera.closed


async def test_capture_failure_closes_on_end_of_stream():
    class FailingCamera(Camera):
        def decode(self, **kwargs):
            raise RuntimeError('camera disconnected')
            yield
    camera = FailingCamera()
    source = _DetachedCameraTrack(camera)
    with pytest.raises(MediaStreamError):
        await asyncio.wait_for(source.recv(), 1)
    assert camera.closed and not source._thread.is_alive()


async def test_repeated_reconnect_and_delayed_gc():
    retained = []
    for _ in range(20):
        camera = Camera()
        source = _DetachedCameraTrack(camera)
        retained.append(await asyncio.wait_for(source.recv(), 1))
        source.stop()
        assert camera.closed
    for frame in retained:
        assert Vp8Encoder().encode(frame, True)[0]
    retained.clear()
    gc.collect()


async def test_copy_failure_does_not_retain_raw_frame_in_log_traceback(monkeypatch, caplog):
    def fail(raw):
        raise RuntimeError('copy failed with raw frame on stack')
    monkeypatch.setattr('agent_sdk.camera._detach_frame', fail)
    camera = Camera()
    source = _DetachedCameraTrack(camera)
    with pytest.raises(MediaStreamError):
        await asyncio.wait_for(source.recv(), 1)
    assert camera.closed
    assert 'copy failed' in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_real_peer_connection_close_releases_camera():
    from aiortc import RTCConfiguration, RTCPeerConnection
    sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    camera = Camera()
    source = _DetachedCameraTrack(camera)
    track = limit_video_frame_rate(source, 30)
    received = asyncio.Event()
    tasks = []

    @receiver.on('track')
    def on_track(remote):
        async def consume():
            await remote.recv()
            received.set()
        tasks.append(asyncio.create_task(consume()))

    sender.addTrack(track)
    try:
        await sender.setLocalDescription(await sender.createOffer())
        await receiver.setRemoteDescription(sender.localDescription)
        await receiver.setLocalDescription(await receiver.createAnswer())
        await sender.setRemoteDescription(receiver.localDescription)
        await asyncio.wait_for(received.wait(), 5)
        await sender.close()
        assert camera.closed and not source._thread.is_alive()
        await asyncio.sleep(0)
        assert track._reader.done()
    finally:
        await sender.close()
        await receiver.close()
        track.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
