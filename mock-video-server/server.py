#!/usr/bin/env python3
"""N6/DN mock compute video server used by the Agent SDK integration app."""

from __future__ import annotations

import argparse
import asyncio
import fractions
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from aiohttp import web
from aiortc import RTCPeerConnection, RTCRtpReceiver, RTCRtpSender, RTCSessionDescription
from aiortc.codecs import CODECS
from aiortc.codecs import h264 as aiortc_h264
from aiortc.contrib.media import MediaRelay
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.rtcrtpparameters import RTCRtcpFeedback, RTCRtpCodecParameters
from av import VideoFrame


LOG = logging.getLogger("mock-video-server")

H264_HIGH_PROFILE_LEVEL_IDS = ("64001f", "640c1f")
H264_BASELINE_PROFILE_LEVEL_IDS = ("42e01f", "42001f")
H264_PACKETIZATION_MODE = "1"
H264_RTP_PAYLOAD_BYTES = int(os.getenv("MOCK_VIDEO_H264_RTP_PAYLOAD_BYTES", "1150"))

if not 1100 <= H264_RTP_PAYLOAD_BYTES <= 1150:
    raise RuntimeError("MOCK_VIDEO_H264_RTP_PAYLOAD_BYTES must be between 1100 and 1150")

# aiortc uses this module-level limit when fragmenting H.264 NAL units into FU-A RTP
# payloads. Keep the complete inner UDP/IP packet below the Android TUN MTU (1280),
# including RTP extensions, SRTP authentication data and UDP/IP headers.
aiortc_h264.PACKET_MAX = H264_RTP_PAYLOAD_BYTES


def register_h264_high_profiles() -> None:
    """Add High / Constrained High to aiortc's process-wide video capabilities."""
    existing = {
        str(codec.parameters.get("profile-level-id", "")).lower()
        for codec in CODECS["video"]
        if codec.mimeType.lower() == "video/h264"
        and str(codec.parameters.get("packetization-mode", "0"))
        == H264_PACKETIZATION_MODE
    }
    used_payload_types = {codec.payloadType for codec in CODECS["video"]}
    next_payload_type = next(
        payload_type
        for payload_type in range(96, 127, 2)
        if payload_type not in used_payload_types
        and payload_type + 1 not in used_payload_types
    )
    for profile_level_id in H264_HIGH_PROFILE_LEVEL_IDS:
        if profile_level_id in existing:
            continue
        feedback = [
            RTCRtcpFeedback(type="nack"),
            RTCRtcpFeedback(type="nack", parameter="pli"),
            RTCRtcpFeedback(type="goog-remb"),
        ]
        CODECS["video"].extend(
            [
                RTCRtpCodecParameters(
                    mimeType="video/H264",
                    clockRate=90000,
                    payloadType=next_payload_type,
                    rtcpFeedback=feedback,
                    parameters={
                        "level-asymmetry-allowed": "1",
                        "packetization-mode": H264_PACKETIZATION_MODE,
                        "profile-level-id": profile_level_id,
                    },
                ),
                RTCRtpCodecParameters(
                    mimeType="video/rtx",
                    clockRate=90000,
                    payloadType=next_payload_type + 1,
                    parameters={"apt": next_payload_type},
                ),
            ]
        )
        next_payload_type += 2


register_h264_high_profiles()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def rfc3339(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@web.middleware
async def api_error_middleware(
    request: web.Request,
    handler,
) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as error:
        return web.json_response(
            {"error": error.code, "message": error.message},
            status=error.status,
        )


@dataclass
class ConsumerConnection:
    pc: RTCPeerConnection
    state: str = "new"
    codec: str = "<pending>"
    frames_processed: int = 0
    packets_sent: int = 0
    bytes_sent: int = 0
    placeholder_frames_sent: int = 0
    first_frame_logged: bool = False
    first_source_frame_logged: bool = False
    sender: RTCRtpSender | None = None
    keyframes_requested: int = 0
    stats_task: asyncio.Task[None] | None = None


@dataclass
class MediaConnectionRecord:
    media_connection_id: str
    request_id: str
    computing_context: dict[str, str]
    offer_sdp: str
    answer_sdp: str
    role: str
    session: "VideoSession"
    pc: RTCPeerConnection
    consumer: ConsumerConnection | None = None
    deleted: bool = False


@dataclass
class RecognitionTargetRecord:
    request_id: str
    computing_context: dict[str, str]
    target_revision: str
    label: str
    prompt: str

    def response(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "computing_context": self.computing_context,
            "status": "APPLIED",
            "target_revision": self.target_revision,
            "target": {"label": self.label, "prompt": self.prompt},
        }


@dataclass
class ControlActionRecord:
    request_id: str
    action_id: str
    computing_context: dict[str, str]
    normalized_action: str
    normalized_parameters: dict[str, Any]
    transcription: dict[str, Any] | None = None

    def response(self, *, completed: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "request_id": self.request_id,
            "action_id": self.action_id,
            "computing_context": self.computing_context,
            "normalized_action": self.normalized_action,
            "normalized_parameters": self.normalized_parameters,
            "status": "COMPLETED" if completed else "RUNNING",
            "result": {
                "phase": "completed" if completed else "started",
                "mock": True,
            },
            "cause": "",
        }
        if self.transcription is not None:
            result["transcription"] = self.transcription
        return result


async def refresh_consumer_stats(connection: ConsumerConnection) -> None:
    """Capture monotonic outbound RTP counters before a connection disappears."""
    try:
        report = await connection.pc.getStats()
    except Exception as error:
        LOG.debug("consumer RTP stats unavailable: %s", error)
        return
    outbound = [
        stat
        for stat in report.values()
        if getattr(stat, "type", "") == "outbound-rtp"
        and getattr(stat, "kind", "") == "video"
    ]
    connection.packets_sent = sum(int(getattr(stat, "packetsSent", 0)) for stat in outbound)
    connection.bytes_sent = sum(int(getattr(stat, "bytesSent", 0)) for stat in outbound)


def selected_video_format(sdp: str) -> tuple[str, dict[str, str]]:
    """Return the first negotiated base codec and FMTP parameters."""
    video_payloads: list[str] = []
    codec_by_payload: dict[str, str] = {}
    fmtp_by_payload: dict[str, dict[str, str]] = {}
    in_video_section = False
    for raw_line in sdp.splitlines():
        line = raw_line.strip()
        if line.startswith("m="):
            parts = line.split()
            in_video_section = line.startswith("m=video ")
            if in_video_section and len(parts) > 3:
                video_payloads = parts[3:]
            continue
        if not in_video_section or not line.startswith("a=rtpmap:"):
            if in_video_section and line.startswith("a=fmtp:"):
                payload_and_fmtp = line[len("a=fmtp:"):].split(None, 1)
                if len(payload_and_fmtp) == 2:
                    fmtp_by_payload[payload_and_fmtp[0]] = {
                        key.strip().lower(): value.strip().lower()
                        for item in payload_and_fmtp[1].split(";")
                        if "=" in item
                        for key, value in [item.split("=", 1)]
                    }
            continue
        payload_and_codec = line[len("a=rtpmap:"):].split(None, 1)
        if len(payload_and_codec) == 2:
            codec_by_payload[payload_and_codec[0]] = payload_and_codec[1]
    for payload in video_payloads:
        codec = codec_by_payload.get(payload)
        if codec and not codec.lower().startswith("rtx/"):
            return codec, fmtp_by_payload.get(payload, {})
    return "<unknown>", {}


def selected_video_codec(sdp: str) -> str:
    return selected_video_format(sdp)[0]


def require_h264_high_answer(sdp: str) -> str:
    """Reject an answer which silently falls back from the source High profile."""
    codec, parameters = selected_video_format(sdp)
    profile_level_id = parameters.get("profile-level-id", "<missing>")
    packetization_mode = parameters.get("packetization-mode", "0")
    if (
        codec.lower() != "h264/90000"
        or profile_level_id not in H264_HIGH_PROFILE_LEVEL_IDS
        or packetization_mode != H264_PACKETIZATION_MODE
    ):
        raise ApiError(
            409,
            "SOURCE_CODEC_MISMATCH",
            "source must negotiate H264 High Profile with packetization-mode=1; "
            f"got codec={codec} profile-level-id={profile_level_id} "
            f"packetization-mode={packetization_mode}",
        )
    return f"{codec};profile-level-id={profile_level_id};packetization-mode={packetization_mode}"


def prefer_h264_high(transceiver: Any) -> None:
    """Require a packetization-mode=1 H264 High profile on the source leg."""
    codecs = RTCRtpSender.getCapabilities("video").codecs
    h264 = [
        codec
        for codec in codecs
        if codec.mimeType.lower() == "video/h264"
        and str(codec.parameters.get("profile-level-id", "")).lower()
        in H264_HIGH_PROFILE_LEVEL_IDS
        and str(codec.parameters.get("packetization-mode", "0"))
        == H264_PACKETIZATION_MODE
    ]
    if len(h264) != len(H264_HIGH_PROFILE_LEVEL_IDS):
        raise RuntimeError("H264 High Profile capabilities were not registered")
    retransmission = [codec for codec in codecs if codec.mimeType.lower() == "video/rtx"]
    transceiver.setCodecPreferences(h264 + retransmission)


def prefer_h264_baseline(transceiver: Any) -> None:
    """Keep the server-encoded consumer leg on aiortc's Baseline encoder."""
    codecs = RTCRtpSender.getCapabilities("video").codecs
    h264 = [
        codec
        for codec in codecs
        if codec.mimeType.lower() == "video/h264"
        and str(codec.parameters.get("profile-level-id", "")).lower()
        in H264_BASELINE_PROFILE_LEVEL_IDS
    ]
    if not h264:
        return
    retransmission = [codec for codec in codecs if codec.mimeType.lower() == "video/rtx"]
    fallback = [
        codec
        for codec in codecs
        if codec.mimeType.lower() not in {"video/h264", "video/rtx"}
    ]
    transceiver.setCodecPreferences(h264 + retransmission + fallback)


class SessionProcessedVideoTrack(MediaStreamTrack):
    """A session-lifetime output track which switches from placeholder to latest source frame."""

    kind = "video"

    def __init__(self, session_id: str, output_fps: float) -> None:
        super().__init__()
        self._session_id = session_id
        self._output_fps = max(float(output_fps), 1.0)
        self._output_interval = 1.0 / self._output_fps
        self._timestamp_step = max(1, round(90000 / self._output_fps))
        self._timestamp = 0
        self._next_output_at = 0.0
        self._source_generation = 0
        self._source_reader_task: asyncio.Task[None] | None = None
        self._latest_source_image: np.ndarray[Any, Any] | None = None
        self._source_frame_ready = asyncio.Event()
        self._first_source_output_logged = False
        self._frame_number = 0
        self.frames_processed = 0
        self._placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
        self._placeholder[:, :] = (12, 12, 12)
        self._placeholder[212:268, 72:568] = (30, 30, 30)
        self._placeholder[232:248, 112:528] = (80, 80, 80)

    @property
    def source_frame_available(self) -> bool:
        return self._latest_source_image is not None

    def set_source(self, source: MediaStreamTrack) -> None:
        self._source_generation += 1
        generation = self._source_generation
        if self._source_reader_task is not None:
            self._source_reader_task.cancel()
        self._latest_source_image = None
        self._source_frame_ready.clear()
        self._first_source_output_logged = False
        self._source_reader_task = asyncio.create_task(self._read_source(source, generation))

    def clear_source(self) -> None:
        self._source_generation += 1
        if self._source_reader_task is not None:
            self._source_reader_task.cancel()
            self._source_reader_task = None
        self._latest_source_image = None
        self._source_frame_ready.clear()

    async def wait_for_source_frame(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._source_frame_ready.wait(), timeout=timeout)

    async def _read_source(self, source: MediaStreamTrack, generation: int) -> None:
        try:
            while generation == self._source_generation:
                frame = await source.recv()
                frame_number = self._frame_number
                image = await asyncio.to_thread(self._process_frame, frame, frame_number)
                if generation != self._source_generation:
                    return
                self._frame_number += 1
                self.frames_processed += 1
                self._latest_source_image = image
                if not self._source_frame_ready.is_set():
                    self._source_frame_ready.set()
                    LOG.info(
                        "first processed source frame id=%s size=%sx%s",
                        self._session_id,
                        image.shape[1],
                        image.shape[0],
                    )
        except asyncio.CancelledError:
            raise
        except MediaStreamError:
            LOG.info("processed source ended id=%s", self._session_id)
        except Exception:
            LOG.exception("processed source reader failed id=%s", self._session_id)

    @staticmethod
    def _process_frame(frame: VideoFrame, frame_number: int) -> np.ndarray[Any, Any]:
        target_width = 640
        target_height = 480
        scale = min(target_width / frame.width, target_height / frame.height)
        resized_width = max(2, int(frame.width * scale) // 2 * 2)
        resized_height = max(2, int(frame.height * scale) // 2 * 2)
        resized = frame.reformat(
            width=resized_width,
            height=resized_height,
            format="bgr24",
        ).to_ndarray()
        image = np.zeros((target_height, target_width, 3), dtype=np.uint8)
        left = (target_width - resized_width) // 2
        top = (target_height - resized_height) // 2
        image[top:top + resized_height, left:left + resized_width] = resized
        height, width = image.shape[:2]
        marker_height = min(28, height)
        marker_width = min(96, width)
        image[:marker_height, :marker_width] = (210, 30, 210)
        bar_width = min(marker_width, 4 + frame_number % max(marker_width, 1))
        image[max(marker_height - 5, 0):marker_height, :bar_width] = (30, 230, 30)
        return image

    async def _next_timestamp(self) -> tuple[int, fractions.Fraction]:
        now = time.perf_counter()
        if self._next_output_at <= 0:
            self._next_output_at = now
        else:
            self._next_output_at += self._output_interval
            delay = self._next_output_at - now
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -(self._output_interval * 2):
                self._next_output_at = time.perf_counter()
        self._timestamp += self._timestamp_step
        return self._timestamp, fractions.Fraction(1, 90000)

    async def recv(self) -> VideoFrame:
        pts, time_base = await self._next_timestamp()
        image = self._latest_source_image
        if image is not None and not self._first_source_output_logged:
            self._first_source_output_logged = True
            LOG.info("session output switched to source id=%s", self._session_id)
        output = VideoFrame.from_ndarray(
            image if image is not None else self._placeholder,
            format="bgr24",
        )
        output.pts = pts
        output.time_base = time_base
        return output

    async def close(self) -> None:
        task = self._source_reader_task
        self.clear_source()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self.stop()


class ConsumerVideoTrack(MediaStreamTrack):
    """Count actual source frames while relaying the session-lifetime output track."""

    kind = "video"

    def __init__(
        self,
        source: MediaStreamTrack,
        output: SessionProcessedVideoTrack,
        session_id: str,
        consumer_id: str,
        connection: ConsumerConnection,
    ) -> None:
        super().__init__()
        self._source = source
        self._output = output
        self._session_id = session_id
        self._consumer_id = consumer_id
        self._connection = connection

    async def recv(self) -> VideoFrame:
        frame = await self._source.recv()
        is_source = self._output.source_frame_available
        if is_source:
            self._connection.frames_processed += 1
        else:
            self._connection.placeholder_frames_sent += 1
        if not self._connection.first_frame_logged:
            self._connection.first_frame_logged = True
            LOG.info(
                "first consumer frame id=%s connection=%s kind=%s codec=%s",
                self._session_id,
                self._consumer_id,
                "source" if is_source else "placeholder",
                self._connection.codec,
            )
        if is_source and not self._connection.first_source_frame_logged:
            self._connection.first_source_frame_logged = True
            LOG.info(
                "first source frame sent id=%s agent=%s codec=%s frames_processed=%s",
                self._session_id,
                self._consumer_id,
                self._connection.codec,
                self._connection.frames_processed,
            )
        return frame


@dataclass
class VideoSession:
    session_id: str
    workload_type: str
    sandbox_vcpus: int
    sandbox_memory_mb: int
    expires_at: datetime
    output_fps: float = 30.0
    state: str = "ALLOCATED"
    producer_pc: RTCPeerConnection | None = None
    source_track: MediaStreamTrack | None = None
    relay: MediaRelay = field(default_factory=MediaRelay)
    output_relay: MediaRelay = field(default_factory=MediaRelay)
    output_track: SessionProcessedVideoTrack = field(init=False)
    source_ready: asyncio.Event = field(default_factory=asyncio.Event)
    source_probe_task: asyncio.Task[None] | None = None
    source_keyframe_task: asyncio.Task[None] | None = None
    source_codec: str = "<pending>"
    source_keyframes_requested: int = 0
    consumer_connections: list[ConsumerConnection] = field(default_factory=list)
    frames_seen: int = 0

    def __post_init__(self) -> None:
        self.output_track = SessionProcessedVideoTrack(self.session_id, self.output_fps)

    async def close(self) -> None:
        self.state = "STOPPED"
        if self.source_probe_task is not None:
            self.source_probe_task.cancel()
            await asyncio.gather(self.source_probe_task, return_exceptions=True)
        if self.source_keyframe_task is not None:
            self.source_keyframe_task.cancel()
            await asyncio.gather(self.source_keyframe_task, return_exceptions=True)
        connections = list(self.consumer_connections)
        await asyncio.gather(
            *(refresh_consumer_stats(connection) for connection in connections),
            return_exceptions=True,
        )
        for connection in connections:
            if connection.stats_task is not None:
                connection.stats_task.cancel()
        await asyncio.gather(
            *(connection.stats_task for connection in connections if connection.stats_task is not None),
            return_exceptions=True,
        )
        peer_connections = {connection.pc for connection in connections}
        if self.producer_pc is not None:
            peer_connections.add(self.producer_pc)
        await asyncio.gather(*(pc.close() for pc in peer_connections), return_exceptions=True)
        await self.output_track.close()


class MockVideoServer:
    def __init__(
        self,
        public_ip: str,
        port: int,
        source_wait_seconds: float = 12.0,
        output_fps: float | None = None,
    ) -> None:
        self.public_ip = public_ip
        self.port = port
        self.source_wait_seconds = source_wait_seconds
        self.output_fps = output_fps or float(os.getenv("MOCK_VIDEO_OUTPUT_FPS", "30"))
        self.sessions: dict[str, VideoSession] = {}
        self.binding_sessions: dict[str, VideoSession] = {}
        self.binding_facts: dict[str, tuple[str, str]] = {}
        self.binding_agents: dict[tuple[str, str], str] = {}
        self.media_connections: dict[str, MediaConnectionRecord] = {}
        self.media_requests: dict[tuple[str, str, str], MediaConnectionRecord] = {}
        self.active_media: dict[tuple[str, str], str] = {}
        self.recognition_targets: dict[str, RecognitionTargetRecord] = {}
        self.control_actions: dict[str, ControlActionRecord] = {}
        self.control_requests: dict[tuple[str, str], ControlActionRecord] = {}

    def create_app(self) -> web.Application:
        app = web.Application(
            middlewares=[api_error_middleware],
            client_max_size=52 * 1024 * 1024,
        )
        app.router.add_get("/healthz", self.health)
        app.router.add_get("/debug/v1/sessions", self.list_sessions)
        app.router.add_post("/compute/v1/offloading-sessions", self.create_session)
        app.router.add_post("/v1/media-connections", self.create_media_connection)
        app.router.add_delete(
            "/v1/media-connections/{media_connection_id}",
            self.delete_media_connection,
        )
        app.router.add_put(
            "/v1/recognition-targets/{compute_service_session_id}",
            self.update_recognition_target,
        )
        app.router.add_get(
            "/v1/recognition-targets/{compute_service_session_id}",
            self.get_recognition_target,
        )
        app.router.add_post("/v1/control-actions", self.create_control_action)
        app.router.add_get(
            "/v1/control-actions/{action_id}",
            self.get_control_action,
        )
        app.router.add_post(
            "/v1/audio-control-actions",
            self.create_audio_control_action,
        )
        app.router.add_post("/video/v1/sessions/{session_id}/source", self.source)
        app.router.add_post("/video/v1/sessions/{session_id}/source/stop", self.stop_source)
        app.router.add_post("/video/v1/sessions/{session_id}/processed", self.processed)
        app.on_shutdown.append(self.shutdown)
        return app

    async def health(self, _: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "service": "agent-sdk-mock-video-server",
                "video_server_ip": self.public_ip,
                "output_fps": self.output_fps,
                "h264_rtp_payload_bytes": H264_RTP_PAYLOAD_BYTES,
                "sessions": len(self.sessions),
                "recognition_targets": len(self.recognition_targets),
                "control_actions": len(self.control_actions),
            }
        )

    async def list_sessions(self, _: web.Request) -> web.Response:
        connections = [
            connection
            for session in self.sessions.values()
            for connection in session.consumer_connections
        ]
        await asyncio.gather(
            *(refresh_consumer_stats(connection) for connection in connections),
            return_exceptions=True,
        )
        return web.json_response(
            {
                "sessions": [
                    {
                        "session_id": session.session_id,
                        "workload_type": session.workload_type,
                        "sandbox_spec": {
                            "vcpus": session.sandbox_vcpus,
                            "memory_mb": session.sandbox_memory_mb,
                        },
                        "state": session.state,
                        "frames_seen": session.frames_seen,
                        "source_codec": session.source_codec,
                        "source_keyframes_requested": session.source_keyframes_requested,
                        "frames_processed": session.output_track.frames_processed,
                        "output_fps": session.output_fps,
                        "source_frame_available": session.output_track.source_frame_available,
                        "consumer_connections": sum(
                            connection.state not in {"failed", "closed"}
                            for connection in session.consumer_connections
                        ),
                        "consumers": {
                            f"consumer-{index}": {
                                "frames_processed": connection.frames_processed,
                                "placeholder_frames_sent": connection.placeholder_frames_sent,
                                "packets_sent": connection.packets_sent,
                                "bytes_sent": connection.bytes_sent,
                                "codec": connection.codec,
                                "first_frame_sent": connection.first_frame_logged,
                                "first_source_frame_sent": connection.first_source_frame_logged,
                                "keyframes_requested": connection.keyframes_requested,
                            }
                            for index, connection in enumerate(
                                session.consumer_connections,
                                start=1,
                            )
                        },
                    }
                    for session in self.sessions.values()
                ]
            }
        )

    async def create_session(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        workload_type = self._required_string(body, "workload_type")
        sandbox_spec = body.get("sandbox_spec")
        if not isinstance(sandbox_spec, dict):
            raise ApiError(400, "INVALID_ARGUMENT", "sandbox_spec must be an object")
        vcpus = self._required_positive_int(sandbox_spec, "vcpus", "sandbox_spec")
        memory_mb = self._required_positive_int(
            sandbox_spec, "memory_mb", "sandbox_spec"
        )
        session_id = f"mock-{uuid.uuid4()}"
        session = VideoSession(
            session_id=session_id,
            workload_type=workload_type,
            sandbox_vcpus=vcpus,
            sandbox_memory_mb=memory_mb,
            expires_at=utc_now() + timedelta(hours=2),
            output_fps=self.output_fps,
        )
        self.sessions[session_id] = session
        LOG.info(
            "session allocated id=%s workload=%s sandbox=%s-vCPU/%s-MiB",
            session_id,
            workload_type,
            vcpus,
            memory_mb,
        )
        return web.json_response(
            {
                "session_id": session_id,
                "state": session.state,
                "expires_at": rfc3339(session.expires_at),
                "video_server_ip": self.public_ip,
            },
            status=201,
        )

    async def create_media_connection(self, request: web.Request) -> web.Response:
        """Answer the terminal-created Offer defined by U-MEDIA."""
        body = await self._json(request)
        request_id = self._required_string(body, "request_id")
        offer = body.get("offer")
        if not isinstance(offer, dict):
            raise ApiError(400, "INVALID_ARGUMENT", "offer must be an object")
        if offer.get("type") != "offer":
            raise ApiError(400, "INVALID_ARGUMENT", "offer.type must be offer")
        offer_sdp = self._required_string(offer, "sdp", "offer")
        context = self._computing_context(body)
        role = context["role"]
        binding_ref = context["binding_ref"]
        request_key = (binding_ref, role, request_id)
        previous = self.media_requests.get(request_key)

        if previous is not None:
            if previous.computing_context != context or previous.offer_sdp != offer_sdp:
                raise ApiError(
                    409,
                    "idempotency-conflict",
                    "the request_id was already used with different content",
                )
            return web.json_response(self._media_response(previous), status=201)

        active_key = (binding_ref, role)
        if active_key in self.active_media:
            raise ApiError(
                409,
                "MEDIA_CONNECTION_EXISTS",
                f"an active {role} media connection already exists for this binding",
            )

        session = self._binding_session(context)
        connection_id = f"media-{uuid.uuid4()}"
        pc = RTCPeerConnection()
        self.active_media[active_key] = connection_id
        consumer: ConsumerConnection | None = None
        try:
            if role == "producer":
                answer_sdp = await self._answer_producer_offer(session, pc, offer_sdp)
            else:
                consumer, answer_sdp = await self._answer_consumer_offer(
                    session,
                    pc,
                    connection_id,
                    offer_sdp,
                )
        except Exception:
            self.active_media.pop(active_key, None)
            await pc.close()
            raise

        record = MediaConnectionRecord(
            media_connection_id=connection_id,
            request_id=request_id,
            computing_context=context,
            offer_sdp=offer_sdp,
            answer_sdp=answer_sdp,
            role=role,
            session=session,
            pc=pc,
            consumer=consumer,
        )
        self.media_connections[connection_id] = record
        self.media_requests[request_key] = record
        LOG.info(
            "U-MEDIA connection created id=%s session=%s binding=%s role=%s",
            connection_id,
            session.session_id,
            binding_ref,
            role,
        )
        return web.json_response(self._media_response(record), status=201)

    async def delete_media_connection(self, request: web.Request) -> web.Response:
        connection_id = request.match_info["media_connection_id"]
        record = self.media_connections.get(connection_id)
        if record is None:
            raise ApiError(404, "MEDIA_CONNECTION_NOT_FOUND", "media connection was not found")
        if record.deleted:
            return web.Response(status=204)
        record.deleted = True
        binding_ref = record.computing_context["binding_ref"]
        self.active_media.pop((binding_ref, record.role), None)
        if record.consumer is not None:
            await refresh_consumer_stats(record.consumer)
            if record.consumer.stats_task is not None:
                record.consumer.stats_task.cancel()
                await asyncio.gather(record.consumer.stats_task, return_exceptions=True)
            record.consumer.state = "closed"
        if record.role == "producer" and record.session.producer_pc is record.pc:
            if record.session.source_probe_task is not None:
                record.session.source_probe_task.cancel()
                await asyncio.gather(record.session.source_probe_task, return_exceptions=True)
                record.session.source_probe_task = None
            if record.session.source_keyframe_task is not None:
                record.session.source_keyframe_task.cancel()
                await asyncio.gather(record.session.source_keyframe_task, return_exceptions=True)
                record.session.source_keyframe_task = None
            record.session.producer_pc = None
            record.session.source_track = None
            record.session.source_ready.clear()
            record.session.output_track.clear_source()
            record.session.state = "WAITING_FOR_SOURCE"
        await record.pc.close()
        LOG.info("U-MEDIA connection deleted id=%s role=%s", connection_id, record.role)
        return web.Response(status=204)

    async def update_recognition_target(self, request: web.Request) -> web.Response:
        session_id = request.match_info["compute_service_session_id"]
        body = await self._json(request)
        request_id = self._required_string(body, "request_id")
        context = self._computing_context(body)
        self._require_consumer_session_path(context, session_id)
        raw_input = body.get("input")
        if not isinstance(raw_input, dict) or raw_input.get("type") != "TEXT":
            raise ApiError(400, "INVALID_ARGUMENT", "input.type must be TEXT")
        text = self._required_string(raw_input, "text", "input")
        revision = str(
            int(self.recognition_targets.get(session_id).target_revision) + 1
            if session_id in self.recognition_targets
            else 1
        )
        label = self._recognition_label(text)
        record = RecognitionTargetRecord(
            request_id=request_id,
            computing_context=context,
            target_revision=revision,
            label=label,
            prompt=text,
        )
        self.recognition_targets[session_id] = record
        LOG.info(
            "recognition target applied session=%s revision=%s label=%s",
            session_id,
            revision,
            label,
        )
        return web.json_response(record.response())

    async def get_recognition_target(self, request: web.Request) -> web.Response:
        session_id = request.match_info["compute_service_session_id"]
        record = self.recognition_targets.get(session_id)
        if record is None:
            raise ApiError(404, "recognition-target-not-set", "recognition target is not set")
        return web.json_response(record.response())

    async def create_control_action(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        request_id = self._required_string(body, "request_id")
        context = self._computing_context(body)
        self._require_consumer_context(context)
        request_key = (context["binding_ref"], request_id)
        previous = self.control_requests.get(request_key)
        if previous is not None:
            return web.json_response(previous.response(completed=False), status=202)
        action, parameters = self._normalize_control_action(body)
        record = ControlActionRecord(
            request_id=request_id,
            action_id=f"action-{uuid.uuid4()}",
            computing_context=context,
            normalized_action=action,
            normalized_parameters=parameters,
        )
        self.control_actions[record.action_id] = record
        self.control_requests[request_key] = record
        LOG.info(
            "control action accepted id=%s session=%s action=%s",
            record.action_id,
            context["compute_service_session_id"],
            action,
        )
        return web.json_response(record.response(completed=False), status=202)

    async def get_control_action(self, request: web.Request) -> web.Response:
        action_id = request.match_info["action_id"]
        record = self.control_actions.get(action_id)
        if record is None:
            raise ApiError(404, "action-not-found", "control action was not found")
        return web.json_response(record.response(completed=True))

    async def create_audio_control_action(self, request: web.Request) -> web.Response:
        if not request.content_type.startswith("multipart/"):
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "multipart/form-data is required")
        reader = await request.multipart()
        fields: dict[str, str] = {}
        audio = b""
        audio_filename = ""
        async for field in reader:
            if field.name == "file" and field.filename:
                audio_filename = field.filename
                audio = await field.read(decode=False)
            else:
                fields[str(field.name)] = await field.text()
        if not audio or not audio_filename:
            raise ApiError(400, "INVALID_ARGUMENT", "file must contain audio bytes")
        request_id = fields.get("request_id", "").strip()
        if not request_id:
            raise ApiError(400, "INVALID_ARGUMENT", "request_id is required")
        try:
            raw_context = json.loads(fields.get("computing_context", ""))
        except (TypeError, ValueError) as error:
            raise ApiError(
                400,
                "INVALID_ARGUMENT",
                "computing_context must be a JSON object",
            ) from error
        context = self._computing_context({"computing_context": raw_context})
        self._require_consumer_context(context)
        request_key = (context["binding_ref"], request_id)
        previous = self.control_requests.get(request_key)
        if previous is not None:
            return web.json_response(previous.response(completed=False), status=202)
        transcript = os.getenv("MOCK_AUDIO_TRANSCRIPTION_TEXT", "向左移动")
        record = ControlActionRecord(
            request_id=request_id,
            action_id=f"audio-action-{uuid.uuid4()}",
            computing_context=context,
            normalized_action="movement",
            normalized_parameters={"direction": "left"},
            transcription={
                "text": transcript,
                "language": fields.get("language", "zh") or "zh",
                "transcript_id": f"transcript-{uuid.uuid4()}",
            },
        )
        self.control_actions[record.action_id] = record
        self.control_requests[request_key] = record
        LOG.info(
            "audio control action accepted id=%s session=%s file=%s bytes=%s",
            record.action_id,
            context["compute_service_session_id"],
            audio_filename,
            len(audio),
        )
        return web.json_response(record.response(completed=False), status=202)

    async def _answer_producer_offer(
        self,
        session: VideoSession,
        pc: RTCPeerConnection,
        offer_sdp: str,
    ) -> str:
        if session.producer_pc is not None and session.producer_pc is not pc:
            await session.producer_pc.close()
        session.source_ready.clear()
        session.source_track = None
        session.source_codec = "<pending>"
        session.output_track.clear_source()
        session.state = "WAITING_FOR_SOURCE"
        session.producer_pc = pc

        @pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind != "video":
                return
            session.source_track = track
            session.output_track.set_source(session.relay.subscribe(track, buffered=False))
            session.source_probe_task = asyncio.create_task(
                self._probe_source(session, session.relay.subscribe(track, buffered=False))
            )
            LOG.info("U-MEDIA producer track negotiated id=%s track=%s", session.session_id, track.id)

        @pc.on("connectionstatechange")
        async def on_state_change() -> None:
            LOG.info("U-MEDIA producer pc id=%s state=%s", session.session_id, pc.connectionState)
            if pc.connectionState in {"failed", "closed"}:
                if session.producer_pc is pc and session.state != "STOPPED":
                    session.state = "SOURCE_ENDED"
            elif pc.connectionState == "connected" and session.source_keyframe_task is None:
                receiver = next(
                    (
                        transceiver.receiver
                        for transceiver in pc.getTransceivers()
                        if transceiver.kind == "video"
                    ),
                    None,
                )
                if receiver is not None:
                    session.source_keyframe_task = asyncio.create_task(
                        self._request_source_keyframes(session, receiver)
                    )

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await self._wait_ice_gathering(pc)
        local = pc.localDescription
        session.source_codec = selected_video_codec(local.sdp)
        return local.sdp

    async def _answer_consumer_offer(
        self,
        session: VideoSession,
        pc: RTCPeerConnection,
        connection_id: str,
        offer_sdp: str,
    ) -> tuple[ConsumerConnection, str]:
        connection = ConsumerConnection(pc=pc)
        session.consumer_connections.append(connection)

        @pc.on("connectionstatechange")
        async def on_state_change() -> None:
            connection.state = pc.connectionState
            LOG.info(
                "U-MEDIA consumer pc id=%s connection=%s state=%s",
                session.session_id,
                connection_id,
                pc.connectionState,
            )
            if pc.connectionState in {"failed", "closed"}:
                await refresh_consumer_stats(connection)
            elif pc.connectionState == "connected":
                self._request_consumer_keyframe_if_ready(
                    session,
                    connection,
                    connection_id,
                    reason="connected-and-source-ready",
                )

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        relayed = session.output_relay.subscribe(session.output_track, buffered=False)
        connection.sender = pc.addTrack(
            ConsumerVideoTrack(
                relayed,
                session.output_track,
                session.session_id,
                connection_id,
                connection,
            )
        )
        transceiver = next(
            item for item in pc.getTransceivers() if item.sender is connection.sender
        )
        prefer_h264_baseline(transceiver)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await self._wait_ice_gathering(pc)
        local = pc.localDescription
        connection.codec = selected_video_codec(local.sdp)
        connection.stats_task = asyncio.create_task(self._poll_consumer_stats(connection))
        return connection, local.sdp

    def _computing_context(self, body: dict[str, Any]) -> dict[str, str]:
        raw = body.get("computing_context")
        if not isinstance(raw, dict):
            raise ApiError(400, "INVALID_ARGUMENT", "computing_context must be an object")
        context = {
            field_name: self._required_string(raw, field_name, "computing_context")
            for field_name in (
                "compute_service_session_id",
                "compute_instance_id",
                "binding_ref",
                "role",
                "agent_id",
            )
        }
        if context["role"] not in {"producer", "consumer"}:
            raise ApiError(400, "INVALID_ARGUMENT", "computing_context.role is invalid")
        binding_ref = context["binding_ref"]
        facts = (
            context["compute_service_session_id"],
            context["compute_instance_id"],
        )
        known_facts = self.binding_facts.setdefault(binding_ref, facts)
        if known_facts != facts:
            raise ApiError(409, "BINDING_CONTEXT_MISMATCH", "binding compute identifiers changed")
        agent_key = (binding_ref, context["role"])
        known_agent = self.binding_agents.setdefault(agent_key, context["agent_id"])
        if known_agent != context["agent_id"]:
            raise ApiError(409, "BINDING_CONTEXT_MISMATCH", "binding role Agent changed")
        return context

    @staticmethod
    def _require_consumer_context(context: dict[str, str]) -> None:
        if context["role"] != "consumer":
            raise ApiError(
                409,
                "CONSUMER_BINDING_REQUIRED",
                "this resource requires a consumer computing context",
            )

    def _require_consumer_session_path(
        self,
        context: dict[str, str],
        session_id: str,
    ) -> None:
        self._require_consumer_context(context)
        if context["compute_service_session_id"] != session_id:
            raise ApiError(
                409,
                "BINDING_CONTEXT_MISMATCH",
                "path session does not match computing_context",
            )

    @staticmethod
    def _recognition_label(text: str) -> str:
        for prefix in ("寻找", "查找", "识别", "检测", "find", "detect"):
            if text.lower().startswith(prefix.lower()):
                label = text[len(prefix):].strip(" ：:")
                if label:
                    return label
        return text

    def _normalize_control_action(
        self,
        body: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        raw_input = body.get("input")
        if not isinstance(raw_input, dict):
            raise ApiError(400, "INVALID_ARGUMENT", "input must be an object")
        input_type = raw_input.get("type")
        if input_type == "STRUCTURED":
            action = self._required_string(body, "action")
            if action not in {"movement", "grab", "search_object"}:
                raise ApiError(400, "INVALID_ARGUMENT", "action is not supported")
            parameters = body.get("parameters")
            if not isinstance(parameters, dict):
                raise ApiError(400, "INVALID_ARGUMENT", "parameters must be an object")
            return action, dict(parameters)
        if input_type != "TEXT":
            raise ApiError(400, "INVALID_ARGUMENT", "input.type must be TEXT or STRUCTURED")
        text = self._required_string(raw_input, "text", "input")
        lowered = text.lower()
        if any(token in lowered for token in ("寻找", "查找", "搜索", "find", "search")):
            query = text
            for token in ("寻找", "查找", "搜索", "find", "search"):
                query = query.replace(token, "")
            return "search_object", {"query": query.strip(" ：:") or "object"}
        if any(token in lowered for token in ("抓", "拿", "grab", "pick")):
            return "grab", {"object": text}
        direction = "left" if "左" in text or "left" in lowered else (
            "right" if "右" in text or "right" in lowered else (
                "backward" if "后" in text or "back" in lowered else "forward"
            )
        )
        return "movement", {"direction": direction}

    def _binding_session(self, context: dict[str, str]) -> VideoSession:
        binding_ref = context["binding_ref"]
        session = self.binding_sessions.get(binding_ref)
        if session is None:
            session_id = context["compute_service_session_id"]
            session = VideoSession(
                session_id=session_id,
                workload_type="formal-computing-session",
                sandbox_vcpus=0,
                sandbox_memory_mb=0,
                expires_at=utc_now() + timedelta(hours=2),
                output_fps=self.output_fps,
            )
            self.binding_sessions[binding_ref] = session
            self.sessions.setdefault(session_id, session)
            LOG.info("U-MEDIA binding initialized session=%s binding=%s", session_id, binding_ref)
        return session

    @staticmethod
    def _media_response(record: MediaConnectionRecord) -> dict[str, Any]:
        return {
            "request_id": record.request_id,
            "computing_context": record.computing_context,
            "media_connection_id": record.media_connection_id,
            "answer": {"type": "answer", "sdp": record.answer_sdp},
        }

    async def source(self, request: web.Request) -> web.Response:
        session = self._session(request)
        body = await self._json(request)
        sdp_type, sdp = self._sdp(body, "sdp_answer")
        if not sdp:
            if session.source_keyframe_task is not None:
                session.source_keyframe_task.cancel()
                await asyncio.gather(session.source_keyframe_task, return_exceptions=True)
                session.source_keyframe_task = None
            if session.producer_pc is not None:
                await session.producer_pc.close()
            session.source_ready.clear()
            session.source_track = None
            session.source_codec = "<pending>"
            session.source_keyframes_requested = 0
            session.output_track.clear_source()
            session.state = "WAITING_FOR_SOURCE"
            pc = RTCPeerConnection()
            session.producer_pc = pc
            source_transceiver = pc.addTransceiver("video", direction="recvonly")
            prefer_h264_high(source_transceiver)

            @pc.on("track")
            def on_track(track: MediaStreamTrack) -> None:
                if track.kind != "video":
                    return
                session.source_track = track
                session.output_track.set_source(
                    session.relay.subscribe(track, buffered=False)
                )
                session.source_probe_task = asyncio.create_task(
                    self._probe_source(
                        session,
                        session.relay.subscribe(track, buffered=False),
                    )
                )
                LOG.info("source track negotiated id=%s track=%s", session.session_id, track.id)

            @pc.on("connectionstatechange")
            async def on_state_change() -> None:
                LOG.info("source pc id=%s state=%s", session.session_id, pc.connectionState)
                if pc.connectionState in {"failed", "closed"} and session.state != "STOPPED":
                    session.state = "FAILED"
                    if session.source_keyframe_task is not None:
                        session.source_keyframe_task.cancel()
                elif pc.connectionState == "connected" and session.source_keyframe_task is None:
                    session.source_keyframe_task = asyncio.create_task(
                        self._request_source_keyframes(session, source_transceiver.receiver)
                    )

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await self._wait_ice_gathering(pc)
            local = pc.localDescription
            return web.json_response(
                {
                    "session_id": session.session_id,
                    "state": session.state,
                    "sdp_offer": {"type": local.type, "sdp": local.sdp},
                }
            )

        if sdp_type != "answer" or session.producer_pc is None:
            raise ApiError(409, "SOURCE_SIGNALING_ORDER", "request a server offer before sending an answer")
        session.source_codec = require_h264_high_answer(sdp)
        LOG.info(
            "source answer selected id=%s codec=%s",
            session.session_id,
            session.source_codec,
        )
        await session.producer_pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        try:
            await asyncio.wait_for(session.source_ready.wait(), timeout=self.source_wait_seconds)
        except TimeoutError as error:
            session.state = "FAILED"
            raise ApiError(504, "SOURCE_FRAME_TIMEOUT", "no video frame arrived from the source") from error
        return web.json_response(
            {
                "session_id": session.session_id,
                "state": session.state,
                "track_id": session.source_track.id if session.source_track else "",
                "frames_seen": session.frames_seen,
            }
        )

    async def stop_source(self, request: web.Request) -> web.Response:
        session = self._session(request)
        await session.close()
        return web.json_response({"session_id": session.session_id, "state": session.state})

    async def processed(self, request: web.Request) -> web.Response:
        session = self._session(request)
        if session.state in {"STOPPED", "FAILED"}:
            raise ApiError(409, "SESSION_NOT_STREAMABLE", "processed stream is not available")
        body = await self._json(request)
        sdp_type, sdp = self._sdp(body, "sdp_offer")
        if sdp_type != "offer" or not sdp:
            raise ApiError(400, "INVALID_SDP", "consumer request must contain an SDP offer")
        pc = RTCPeerConnection()
        connection = ConsumerConnection(pc=pc)
        session.consumer_connections.append(connection)
        consumer_id = f"consumer-{len(session.consumer_connections)}"

        @pc.on("connectionstatechange")
        async def on_state_change() -> None:
            connection.state = pc.connectionState
            LOG.info(
                "consumer pc id=%s connection=%s state=%s",
                session.session_id,
                consumer_id,
                pc.connectionState,
            )
            if pc.connectionState in {"failed", "closed"}:
                await refresh_consumer_stats(connection)
            elif pc.connectionState == "connected":
                self._request_consumer_keyframe_if_ready(
                    session,
                    connection,
                    consumer_id,
                    reason="connected-and-source-ready",
                )

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        relayed = session.output_relay.subscribe(session.output_track, buffered=False)
        connection.sender = pc.addTrack(
            ConsumerVideoTrack(
                relayed,
                session.output_track,
                session.session_id,
                consumer_id,
                connection,
            )
        )
        consumer_transceiver = next(
            transceiver
            for transceiver in pc.getTransceivers()
            if transceiver.sender is connection.sender
        )
        prefer_h264_baseline(consumer_transceiver)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await self._wait_ice_gathering(pc)
        local = pc.localDescription
        connection.codec = selected_video_codec(local.sdp)
        connection.stats_task = asyncio.create_task(self._poll_consumer_stats(connection))
        LOG.info(
            "consumer answer ready id=%s connection=%s codec=%s",
            session.session_id,
            consumer_id,
            connection.codec,
        )
        return web.json_response(
            {
                "session_id": session.session_id,
                "consumer_id": consumer_id,
                "state": "SOURCE_CONNECTED" if session.source_ready.is_set() else "SOURCE_PENDING",
                "sdp_answer": {"type": local.type, "sdp": local.sdp},
            }
        )

    async def _request_source_keyframes(
        self,
        session: VideoSession,
        receiver: RTCRtpReceiver,
    ) -> None:
        """Request clean IDRs after DTLS so a lost startup keyframe is recoverable."""
        send_pli = getattr(receiver, "_send_rtcp_pli", None)
        if not callable(send_pli):
            LOG.warning("source keyframe request unsupported id=%s", session.session_id)
            return
        try:
            for index, delay_seconds in enumerate((0.0, 0.2, 0.5, 1.0, 2.0, 3.0), start=1):
                if delay_seconds:
                    await asyncio.sleep(delay_seconds)
                pc = session.producer_pc
                if pc is None or pc.connectionState != "connected" or session.source_ready.is_set():
                    return
                sources = receiver.getSynchronizationSources()
                if not sources:
                    continue
                for source in sources:
                    await send_pli(source.source)
                    session.source_keyframes_requested += 1
                    LOG.info(
                        "source keyframe requested id=%s ssrc=%s reason=decode-startup-%s count=%s",
                        session.session_id,
                        source.source,
                        index,
                        session.source_keyframes_requested,
                    )
        except asyncio.CancelledError:
            raise

    def _request_consumer_keyframe(
        self,
        connection: ConsumerConnection,
        session_id: str,
        agent_id: str,
        reason: str,
    ) -> None:
        sender = connection.sender
        if sender is None:
            return
        # aiortc currently exposes keyframe requests as an internal sender operation. Keeping
        # this compatibility check local makes a future public API migration straightforward.
        request_keyframe = getattr(sender, "_send_keyframe", None)
        if not callable(request_keyframe):
            LOG.warning(
                "consumer keyframe request unsupported id=%s connection=%s reason=%s",
                session_id,
                agent_id,
                reason,
            )
            return
        request_keyframe()
        connection.keyframes_requested += 1
        LOG.info(
            "consumer keyframe requested id=%s connection=%s reason=%s count=%s",
            session_id,
            agent_id,
            reason,
            connection.keyframes_requested,
        )

    def _request_consumer_keyframe_if_ready(
        self,
        session: VideoSession,
        connection: ConsumerConnection,
        agent_id: str,
        reason: str,
    ) -> None:
        # Exactly one manually requested startup keyframe is sufficient once both DTLS and the
        # real source are ready. Requesting at answer-ready, connected and source-ready used to
        # produce a nine-IDR burst, increasing queueing and fragmentation at the TUN boundary.
        if (
            connection.state != "connected"
            or not session.source_ready.is_set()
            or connection.keyframes_requested != 0
        ):
            return
        self._request_consumer_keyframe(
            connection,
            session.session_id,
            agent_id,
            reason=reason,
        )

    async def _poll_consumer_stats(self, connection: ConsumerConnection) -> None:
        try:
            while connection.state not in {"failed", "closed"}:
                await asyncio.sleep(1.0)
                await refresh_consumer_stats(connection)
        except asyncio.CancelledError:
            await refresh_consumer_stats(connection)
            raise

    async def _probe_source(self, session: VideoSession, track: MediaStreamTrack) -> None:
        try:
            while True:
                await track.recv()
                session.frames_seen += 1
                if session.frames_seen == 1:
                    session.state = "SOURCE_CONNECTED"
                    session.source_ready.set()
                    LOG.info("first source frame id=%s", session.session_id)
                    try:
                        await session.output_track.wait_for_source_frame()
                    except TimeoutError:
                        LOG.warning(
                            "processed source frame was not ready for keyframe id=%s",
                            session.session_id,
                        )
                    for index, connection in enumerate(
                        session.consumer_connections,
                        start=1,
                    ):
                        self._request_consumer_keyframe_if_ready(
                            session,
                            connection,
                            f"consumer-{index}",
                            reason="source-and-connection-ready",
                        )
        except (MediaStreamError, asyncio.CancelledError):
            if session.state not in {"STOPPED", "FAILED"}:
                session.state = "SOURCE_ENDED"

    async def _wait_ice_gathering(self, pc: RTCPeerConnection) -> None:
        if pc.iceGatheringState == "complete":
            return
        ready = asyncio.Event()

        @pc.on("icegatheringstatechange")
        def on_ice_state() -> None:
            if pc.iceGatheringState == "complete":
                ready.set()

        await asyncio.wait_for(ready.wait(), timeout=8.0)

    async def shutdown(self, _: web.Application) -> None:
        sessions = {id(session): session for session in self.sessions.values()}
        sessions.update({id(session): session for session in self.binding_sessions.values()})
        await asyncio.gather(*(session.close() for session in sessions.values()), return_exceptions=True)

    def _session(self, request: web.Request) -> VideoSession:
        session = self.sessions.get(request.match_info["session_id"])
        if session is None:
            raise ApiError(404, "SESSION_NOT_FOUND", "offloading session was not found")
        if session.expires_at <= utc_now():
            raise ApiError(410, "SESSION_EXPIRED", "offloading session has expired")
        return session

    async def _json(self, request: web.Request) -> dict[str, Any]:
        try:
            value = await request.json()
        except Exception as error:
            raise ApiError(400, "INVALID_JSON", "request body must be a JSON object") from error
        if not isinstance(value, dict):
            raise ApiError(400, "INVALID_JSON", "request body must be a JSON object")
        return value

    def _required_string(
        self,
        body: dict[str, Any],
        field_name: str,
        prefix: str = "",
    ) -> str:
        value = body.get(field_name)
        qualified = f"{prefix}.{field_name}" if prefix else field_name
        if not isinstance(value, str) or not value.strip():
            raise ApiError(400, "INVALID_ARGUMENT", f"{qualified} must be a non-empty string")
        return value

    def _required_positive_int(
        self,
        body: dict[str, Any],
        field_name: str,
        prefix: str = "",
    ) -> int:
        value = body.get(field_name)
        qualified = f"{prefix}.{field_name}" if prefix else field_name
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ApiError(400, "INVALID_ARGUMENT", f"{qualified} must be a positive integer")
        return value

    def _sdp(self, body: dict[str, Any], nested_field: str) -> tuple[str, str]:
        nested = body.get(nested_field)
        candidate = nested if isinstance(nested, dict) else body
        sdp_type = candidate.get("type")
        sdp = candidate.get("sdp")
        return (
            sdp_type if isinstance(sdp_type, str) else "",
            sdp if isinstance(sdp, str) else "",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("MOCK_LISTEN_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MOCK_VIDEO_PORT", "28500")))
    parser.add_argument("--public-ip", default=os.getenv("MOCK_VIDEO_SERVER_IP", "172.30.0.10"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server = MockVideoServer(public_ip=args.public_ip, port=args.port)
    web.run_app(server.create_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
