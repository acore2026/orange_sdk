from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import shlex
import sys
from typing import Any

from .errors import AgentSdkError, ErrorCode
from .models import ComputingSession
from .video_diagnostics import observe_delivery, observe_source


# Leave room for RTP extensions, SRTP authentication and UDP/IP headers inside
# the 1280-byte CONNECT-IP TUN, matching the existing Sandbox packetization.
VIDEO_RTP_PAYLOAD_BYTES = 1150


def limit_video_frame_rate(source: Any, fps: int, *, max_frame_age: float = 0.3) -> Any:
    """Drain capture independently of the sender, retaining only the newest frame."""
    from aiortc import MediaStreamTrack
    from aiortc.mediastreams import MediaStreamError

    if fps <= 0 or max_frame_age <= 0:
        raise ValueError("fps and max_frame_age must be positive")
    observe_source(source)

    class LatestVideoTrack(MediaStreamTrack):
        kind = "video"

        def __init__(self) -> None:
            super().__init__()
            self._reader: asyncio.Task | None = None
            self._changed = asyncio.Event()
            self._latest = None
            self._error: Exception | None = None
            self._next_delivery = 0.0
            self._clock_origin: float | None = None
            self._previous_pts: float | None = None
            self._last_report = 0.0
            self.captured = self.dropped = self.delivered = 0
            # Drain during signaling too: opening the camera before ICE
            # connects must not leave old frames waiting in its FIFO.
            self._reader = asyncio.create_task(self._read(), name="video-latest-frame-reader")

        async def _read(self) -> None:
            try:
                while self.readyState == "live":
                    frame = await source.recv()
                    now = asyncio.get_running_loop().time()
                    pts = float(frame.time) if frame.time is not None else None
                    if pts is not None:
                        if self._clock_origin is None or (self._previous_pts is not None and pts < self._previous_pts):
                            self._clock_origin = now - pts
                        self._previous_pts = pts
                        captured_at = self._clock_origin + pts
                    else:
                        captured_at = now
                    self.captured += 1
                    if self._latest is not None:
                        self.dropped += 1
                    self._latest = (frame, captured_at)
                    self._changed.set()
                    # A buffered source can return immediately. Keep cancellation
                    # and the sender runnable while draining its accumulated frames.
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = exc
                logging.getLogger(__name__).warning("video_source_reader_failed source=%s", getattr(source, "id", id(source)), exc_info=True)
            finally:
                self._changed.set()

        async def recv(self):
            if self.readyState != "live":
                raise MediaStreamError
            if self._reader is None:
                self._reader = asyncio.create_task(self._read(), name="video-latest-frame-reader")
            loop = asyncio.get_running_loop()
            delay = self._next_delivery - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            while self.readyState == "live":
                if self._latest is not None:
                    frame, captured_at = self._latest
                    self._latest = None
                    age = max(0.0, loop.time() - captured_at)
                    if age <= max_frame_age:
                        self.delivered += 1
                        self._next_delivery = loop.time() + 1 / fps
                        if loop.time() - self._last_report >= 5:
                            self._last_report = loop.time()
                            logging.getLogger(__name__).info(
                                "video_source_freshness captured=%s dropped=%s delivered=%s capture_age_ms=%.1f",
                                self.captured, self.dropped, self.delivered, age * 1000,
                            )
                        observe_delivery(source, frame)
                        return frame
                    self.dropped += 1
                if self._error is not None:
                    raise self._error
                self._changed.clear()
                await self._changed.wait()
            raise MediaStreamError

        def stop(self) -> None:
            super().stop()
            if self._reader is not None:
                self._reader.cancel()
            self._latest = None
            self._changed.set()
            source.stop()

    return LatestVideoTrack()


def _media_error(message: str, cause: BaseException | None = None) -> AgentSdkError:
    error = AgentSdkError(ErrorCode.MEDIA_NEGOTIATION_FAILED, message)
    if cause is not None:
        error.__cause__ = cause
    return error


def require_aiortc() -> None:
    """Fail before network setup when the packaged WebRTC runtime is unavailable."""
    try:
        import aiortc  # noqa: F401
        import av  # noqa: F401
    except ImportError as exc:
        package = exc.name or "aiortc"
        command = (
            f"{shlex.quote(sys.executable)} -m pip install "
            "'aiortc>=1.14,<2'"
        )
        raise _media_error(
            f"Python WebRTC dependency is unavailable: {package}; "
            f"install it before starting the Agent: {command}",
            exc,
        ) from exc


async def _wait_for_connection(pc: Any, timeout_seconds: float) -> None:
    if pc.connectionState == "connected":
        return
    if pc.connectionState in {"failed", "closed"}:
        raise _media_error(f"WebRTC connection entered {pc.connectionState}")
    ready = asyncio.Event()

    @pc.on("connectionstatechange")
    async def connectionstatechange() -> None:
        if pc.connectionState in {"connected", "failed", "closed"}:
            ready.set()

    await asyncio.wait_for(ready.wait(), timeout_seconds)
    if pc.connectionState != "connected":
        raise _media_error(f"WebRTC connection entered {pc.connectionState}")


def _sanitize_host_candidates(sdp: str, ue_ipv4: str) -> str:
    lines: list[str] = []
    for line in sdp.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped.startswith("a=candidate:"):
            parts = stripped.split()
            if len(parts) >= 8 and parts[7].lower() == "host" and parts[4] != ue_ipv4:
                continue
        lines.append(line)
    return "".join(lines)


def _set_video_bandwidth(sdp: str, bitrate_kbps: int) -> str:
    lines = sdp.splitlines(keepends=True)
    in_video = False
    insert_at: int | None = None
    for index, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        if stripped.startswith("m="):
            if in_video:
                break
            in_video = stripped.startswith("m=video ")
            if in_video:
                insert_at = index + 1
            continue
        if in_video and stripped.startswith("c="):
            insert_at = index + 1
        if in_video and stripped.startswith("b="):
            lines[index] = f"b=AS:{bitrate_kbps}\r\n"
            return "".join(lines)
        if in_video and stripped.startswith("a="):
            break
    if insert_at is not None:
        lines.insert(insert_at, f"b=AS:{bitrate_kbps}\r\n")
    return "".join(lines)


def _select_video_codecs(transceiver: Any, codec: str | None) -> None:
    if codec is None:
        return
    from aiortc import RTCRtpSender

    wanted = codec.lower().removeprefix("video/")
    codecs = [
        item
        for item in RTCRtpSender.getCapabilities("video").codecs
        if item.mimeType.lower() == f"video/{wanted}"
    ]
    if not codecs:
        raise _media_error(f"required video codec is not supported: {codec}")
    transceiver.setCodecPreferences(codecs)


class _VideoUploadHandle:
    def __init__(self, pc: Any, player: Any, track: Any) -> None:
        self._pc = pc
        self._player = player
        self._track = track
        self.track_id = track.id
        self.state = "RUNNING"

    async def pause(self) -> None:
        if self.state != "STOPPED":
            self._track.enabled = False
            self.state = "PAUSED"

    async def resume(self) -> None:
        if self.state != "STOPPED":
            self._track.enabled = True
            self.state = "RUNNING"

    async def stop(self) -> None:
        if self.state == "STOPPED":
            return
        self.state = "STOPPED"
        self._track.stop()
        await self._pc.close()


class _RemoteVideoStream:
    def __init__(self, pc: Any, track: Any) -> None:
        self._pc = pc
        self._track = track
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except Exception as exc:
            if exc.__class__.__name__ == "MediaStreamError":
                raise StopAsyncIteration from exc
            raise

    async def recv(self):
        return await self._track.recv()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._track.stop()
        await self._pc.close()


class _PreparedUpload:
    def __init__(self, pc: Any, player: Any, track: Any, offer_sdp: str) -> None:
        self._pc = pc
        self._player = player
        self._track = track
        self.offer_sdp = offer_sdp
        self._applied = False
        self._aborted = False

    async def apply_answer(self, answer_sdp: str, timeout_seconds: float):
        from aiortc import RTCSessionDescription

        try:
            await self._pc.setRemoteDescription(
                RTCSessionDescription(sdp=answer_sdp, type="answer")
            )
            await _wait_for_connection(self._pc, timeout_seconds)
        except Exception as exc:
            raise _media_error("failed to apply the Sandbox WebRTC Answer", exc) from exc
        self._applied = True
        return _VideoUploadHandle(self._pc, self._player, self._track)

    async def abort(self) -> None:
        if not self._applied and not self._aborted:
            self._aborted = True
            self._track.stop()
            await self._pc.close()


class _PreparedConsumer:
    def __init__(self, pc: Any, track_future: asyncio.Future[Any], offer_sdp: str) -> None:
        self._pc = pc
        self._track_future = track_future
        self.offer_sdp = offer_sdp
        self._applied = False
        self._aborted = False

    async def apply_answer(self, answer_sdp: str, timeout_seconds: float):
        from aiortc import RTCSessionDescription

        try:
            await self._pc.setRemoteDescription(
                RTCSessionDescription(sdp=answer_sdp, type="answer")
            )
            track = await asyncio.wait_for(
                asyncio.shield(self._track_future), timeout_seconds
            )
            await _wait_for_connection(self._pc, timeout_seconds)
        except Exception as exc:
            raise _media_error("failed to apply the Sandbox WebRTC Answer", exc) from exc
        self._applied = True
        return _RemoteVideoStream(self._pc, track)

    async def abort(self) -> None:
        if not self._applied and not self._aborted:
            self._aborted = True
            await self._pc.close()


class AiortcMediaOffloadAdapter:
    """Linux WebRTC implementation used automatically by :class:`AgentSdk`."""

    def __init__(
        self,
        *,
        video_file_path: str | Path | None = None,
        loop_video_file: bool = True,
    ) -> None:
        require_aiortc()
        from aiortc.codecs import h264, vpx

        h264.PACKET_MAX = VIDEO_RTP_PAYLOAD_BYTES
        vpx.PACKET_MAX = VIDEO_RTP_PAYLOAD_BYTES
        self._peers: set[Any] = set()
        self._video_file_path = (
            Path(video_file_path).expanduser().resolve()
            if video_file_path is not None
            else None
        )
        self._loop_video_file = loop_video_file

    def _open_video_player(
        self,
        media_player_type: Any,
        *,
        camera_id: int,
        width: int,
        height: int,
        fps: int,
    ) -> Any:
        if self._video_file_path is not None:
            if not self._video_file_path.is_file():
                raise _media_error(
                    f"video file does not exist: {self._video_file_path}"
                )
            return media_player_type(
                str(self._video_file_path),
                loop=self._loop_video_file,
            )
        return media_player_type(
            f"/dev/video{camera_id}",
            format="v4l2",
            options={"video_size": f"{width}x{height}", "framerate": str(fps)},
        )

    @staticmethod
    def supports_video_codec(codec: str) -> bool:
        try:
            from aiortc import RTCRtpSender

            wanted = codec.lower().removeprefix("video/")
            return any(
                item.mimeType.lower() == f"video/{wanted}"
                for item in RTCRtpSender.getCapabilities("video").codecs
            )
        except ImportError:
            return False

    async def prepare_video_upload(
        self,
        session: ComputingSession,
        *,
        camera_id: int,
        width: int,
        height: int,
        fps: int,
        bitrate_kbps: int,
    ) -> _PreparedUpload:
        try:
            from aiortc import RTCPeerConnection
            from aiortc.contrib.media import MediaPlayer
        except ImportError as exc:
            raise _media_error(
                "Python WebRTC support requires the aiortc package", exc
            ) from exc
        from .rtc_policy import HighLatencyPeerConnection, limit_sender_bitrate

        pc = HighLatencyPeerConnection()
        self._peers.add(pc)
        track = None
        try:
            player = self._open_video_player(
                MediaPlayer,
                camera_id=camera_id,
                width=width,
                height=height,
                fps=fps,
            )
            track = player.video
            if track is None:
                raise _media_error("video source did not provide a video track")
            track = limit_video_frame_rate(track, fps)
            transceiver = pc.addTransceiver(track, direction="sendonly")
            limit_sender_bitrate(transceiver.sender, bitrate_kbps * 1000)
            _select_video_codecs(transceiver, session.connection_parameters.video_codec)
            offer = await pc.createOffer()
            from aiortc import RTCSessionDescription

            offer = RTCSessionDescription(
                sdp=_set_video_bandwidth(offer.sdp, bitrate_kbps), type="offer"
            )
            await pc.setLocalDescription(offer)
            local = pc.localDescription
            if local is None or pc.iceGatheringState != "complete":
                raise _media_error("ICE gathering did not produce a complete local Offer")
            return _PreparedUpload(
                pc,
                player,
                track,
                _sanitize_host_candidates(local.sdp, session.network_binding.ue_ipv4),
            )
        except BaseException as exc:
            if track is not None:
                track.stop()
            await pc.close()
            self._peers.discard(pc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, AgentSdkError):
                raise
            raise _media_error("failed to prepare the video WebRTC Offer", exc) from exc

    async def prepare_processed_video(
        self, session: ComputingSession
    ) -> _PreparedConsumer:
        try:
            from aiortc import RTCPeerConnection
        except ImportError as exc:
            raise _media_error(
                "Python WebRTC support requires the aiortc package", exc
            ) from exc
        from .rtc_policy import HighLatencyPeerConnection

        pc = HighLatencyPeerConnection()
        self._peers.add(pc)
        try:
            transceiver = pc.addTransceiver("video", direction="recvonly")
            _select_video_codecs(transceiver, session.connection_parameters.video_codec)
            track_future = asyncio.get_running_loop().create_future()

            @pc.on("track")
            def track_received(track: Any) -> None:
                if track.kind == "video" and not track_future.done():
                    track_future.set_result(track)

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            local = pc.localDescription
            if local is None or pc.iceGatheringState != "complete":
                raise _media_error("ICE gathering did not produce a complete local Offer")
            return _PreparedConsumer(
                pc,
                track_future,
                _sanitize_host_candidates(local.sdp, session.network_binding.ue_ipv4),
            )
        except BaseException as exc:
            await pc.close()
            self._peers.discard(pc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, AgentSdkError):
                raise
            raise _media_error("failed to prepare the processed-video WebRTC Offer", exc) from exc

    async def close(self) -> None:
        peers, self._peers = self._peers, set()
        await asyncio.gather(*(peer.close() for peer in peers), return_exceptions=True)
