"""V4L2 capture that never hands camera-backed frames to the event loop."""
from __future__ import annotations

import asyncio
import logging
import errno
import threading
import traceback
from typing import Any

import av
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError


_LOGGER = logging.getLogger(__name__)


def _detach_frame(frame: av.VideoFrame) -> av.VideoFrame:
    """Copy every plane while the V4L2 container still owns its buffers."""
    detached = av.VideoFrame(frame.width, frame.height, frame.format.name)
    for source, destination in zip(frame.planes, detached.planes):
        if source.buffer_size == destination.buffer_size and source.line_size == destination.line_size:
            destination.update(source)
        else:
            data = bytearray(destination.buffer_size)
            row_size = min(abs(source.line_size), abs(destination.line_size))
            source_data = memoryview(source)
            for row in range(min(source.height, destination.height)):
                start = row * destination.line_size
                offset = row * source.line_size
                data[start:start + row_size] = source_data[offset:offset + row_size]
            destination.update(data)
    detached.pts = frame.pts
    detached.time_base = frame.time_base
    return detached


class _DetachedCameraTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, container: Any) -> None:
        super().__init__()
        self._container = container
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._quit = threading.Event()
        self._queue: asyncio.Queue[av.VideoFrame | None] = asyncio.Queue(maxsize=1)
        self._closed = False
        self._lock = threading.Lock()
        self._pending = None
        self._notification_pending = False

    def _enqueue(self, frame: av.VideoFrame | None) -> None:
        if self.readyState != "live":
            return
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(frame)

    def _publish(self, frame: av.VideoFrame | None) -> None:
        # One mailbox and one callback, even if the event loop is stalled.
        with self._lock:
            self._pending = frame
            if self._notification_pending:
                return
            self._notification_pending = True
        self._loop.call_soon_threadsafe(self._flush)

    def _flush(self) -> None:
        with self._lock:
            frame, self._pending = self._pending, None
            self._notification_pending = False
        self._enqueue(frame)

    def _worker(self) -> None:
        first_pts: int | None = None
        frame = None
        try:
            while not self._quit.is_set():
                # Exhaust/close the decode iterator while the camera is live:
                # PyAV generators can retain the last packet or raw frame.
                frames = self._container.decode(video=0)
                try:
                    frame = next(frames)
                    if frame.pts is None:
                        continue
                    detached = _detach_frame(frame)
                except av.FFmpegError as exc:
                    if exc.errno == errno.EAGAIN:
                        self._quit.wait(0.01)
                        continue
                    raise
                finally:
                    frame = None
                    frames.close()
                if first_pts is None:
                    first_pts = detached.pts
                detached.pts -= first_pts
                if not self._quit.is_set():
                    self._publish(detached)
        except StopIteration:
            pass
        except Exception:
            if not self._quit.is_set():
                _LOGGER.error("camera_capture_failed %s", traceback.format_exc())
        finally:
            if not self._quit.is_set():
                try:
                    self._publish(None)
                except RuntimeError:  # event loop is already closed
                    pass

    async def recv(self) -> av.VideoFrame:
        if self.readyState != "live":
            raise MediaStreamError
        if self._thread is None:
            self._loop = asyncio.get_running_loop()
            self._thread = threading.Thread(
                name="detached-camera", target=self._worker, daemon=True
            )
            self._thread.start()
        frame = await self._queue.get()
        if frame is None:
            self.stop()
            raise MediaStreamError
        return frame

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        super().stop()
        self._quit.set()
        if self._thread is not None:
            self._thread.join()
        # The worker has released every raw frame before the V4L2 mapping is
        # closed. Pending callbacks and consumers hold detached frames only.
        self._container.close()
        with self._lock:
            self._pending = None
        _LOGGER.info("camera_closed_after_worker_exit")
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)


class DetachedCameraPlayer:
    """Minimal MediaPlayer-compatible holder for a single V4L2 video track."""

    def __init__(self, file: str, *, options: dict[str, str]) -> None:
        self.video = _DetachedCameraTrack(
            av.open(file=file, format="v4l2", mode="r", options=options)
        )
