from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
import sys
from typing import Any

from .errors import AgentSdkError, ErrorCode
from .models import ComputingSession


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
        pc = RTCPeerConnection()
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
            transceiver = pc.addTransceiver(track, direction="sendonly")
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
        pc = RTCPeerConnection()
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
