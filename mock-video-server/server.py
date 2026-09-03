#!/usr/bin/env python3
"""N6/DN mock compute video server used by the Agent SDK integration app."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from av import VideoFrame


LOG = logging.getLogger("mock-video-server")


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


class ProcessedVideoTrack(MediaStreamTrack):
    """Relay a source track and add a tiny deterministic marker as mock processing."""

    kind = "video"

    def __init__(self, source: MediaStreamTrack) -> None:
        super().__init__()
        self._source = source
        self._frame_number = 0

    async def recv(self) -> VideoFrame:
        frame = await self._source.recv()
        image = frame.to_ndarray(format="bgr24")
        height, width = image.shape[:2]
        marker_height = min(28, height)
        marker_width = min(96, width)
        image[:marker_height, :marker_width] = (210, 30, 210)
        bar_width = min(marker_width, 4 + self._frame_number % max(marker_width, 1))
        image[max(marker_height - 5, 0):marker_height, :bar_width] = (30, 230, 30)
        processed = VideoFrame.from_ndarray(image, format="bgr24")
        processed.pts = frame.pts
        processed.time_base = frame.time_base
        self._frame_number += 1
        return processed


@dataclass
class ConsumerGrant:
    agent_id: str
    ticket: str
    peer_connections: set[RTCPeerConnection] = field(default_factory=set)


@dataclass
class VideoSession:
    session_id: str
    sandbox_id: str
    group_id: str
    source_agent_id: str
    workload_type: str
    producer_token: str
    expires_at: datetime
    state: str = "ALLOCATED"
    producer_pc: RTCPeerConnection | None = None
    source_track: MediaStreamTrack | None = None
    relay: MediaRelay = field(default_factory=MediaRelay)
    source_ready: asyncio.Event = field(default_factory=asyncio.Event)
    source_probe_task: asyncio.Task[None] | None = None
    consumers: dict[str, ConsumerGrant] = field(default_factory=dict)
    frames_seen: int = 0

    async def close(self) -> None:
        self.state = "STOPPED"
        if self.source_probe_task is not None:
            self.source_probe_task.cancel()
            await asyncio.gather(self.source_probe_task, return_exceptions=True)
        peers = [grant.peer_connections for grant in self.consumers.values()]
        peer_connections = set().union(*peers) if peers else set()
        if self.producer_pc is not None:
            peer_connections.add(self.producer_pc)
        await asyncio.gather(*(pc.close() for pc in peer_connections), return_exceptions=True)


class MockVideoServer:
    def __init__(self, public_ip: str, port: int, source_wait_seconds: float = 12.0) -> None:
        self.public_ip = public_ip
        self.port = port
        self.source_wait_seconds = source_wait_seconds
        self.sessions: dict[str, VideoSession] = {}

    @property
    def base_url(self) -> str:
        return f"http://{self.public_ip}:{self.port}"

    def create_app(self) -> web.Application:
        app = web.Application(
            middlewares=[api_error_middleware],
            client_max_size=2 * 1024 * 1024,
        )
        app.router.add_get("/healthz", self.health)
        app.router.add_get("/debug/v1/sessions", self.list_sessions)
        app.router.add_post("/compute/v1/offloading-sessions", self.create_session)
        app.router.add_post(
            "/compute/v1/offloading-sessions/{session_id}/consumers",
            self.create_consumers,
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
                "sessions": len(self.sessions),
            }
        )

    async def list_sessions(self, _: web.Request) -> web.Response:
        return web.json_response(
            {
                "sessions": [
                    {
                        "session_id": session.session_id,
                        "group_id": session.group_id,
                        "source_agent_id": session.source_agent_id,
                        "state": session.state,
                        "frames_seen": session.frames_seen,
                        "consumer_agents": sorted(session.consumers),
                        "consumer_connections": sum(
                            len(grant.peer_connections) for grant in session.consumers.values()
                        ),
                    }
                    for session in self.sessions.values()
                ]
            }
        )

    async def create_session(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        agent_id = self._required_string(body, "agent_id")
        group_id = self._required_string(body, "group_id")
        workload_type = self._required_string(body, "workload_type")
        session_id = f"mock-{uuid.uuid4()}"
        sandbox_id = str(body.get("preferred_sandbox_id") or "mock-video-sandbox")
        session = VideoSession(
            session_id=session_id,
            sandbox_id=sandbox_id,
            group_id=group_id,
            source_agent_id=agent_id,
            workload_type=workload_type,
            producer_token=secrets.token_urlsafe(32),
            expires_at=utc_now() + timedelta(hours=2),
        )
        self.sessions[session_id] = session
        LOG.info("session allocated id=%s source=%s group=%s", session_id, agent_id, group_id)
        return web.json_response(
            {
                "session_id": session_id,
                "sandbox_id": sandbox_id,
                "group_id": group_id,
                "source_agent_id": agent_id,
                "workload_type": workload_type,
                "state": session.state,
                "expires_at": rfc3339(session.expires_at),
                "producer": {
                    "video_server_ip": self.public_ip,
                    "source_start_url": f"{self.base_url}/video/v1/sessions/{session_id}/source",
                    "source_stop_url": f"{self.base_url}/video/v1/sessions/{session_id}/source/stop",
                    "access_token": session.producer_token,
                },
            },
            status=201,
        )

    async def create_consumers(self, request: web.Request) -> web.Response:
        session = self._session(request)
        body = await self._json(request)
        if body.get("agent_id") != session.source_agent_id:
            raise ApiError(403, "SOURCE_AGENT_MISMATCH", "agent_id is not the session source")
        if body.get("group_id") != session.group_id:
            raise ApiError(409, "GROUP_MISMATCH", "group_id does not match the session")
        targets = body.get("target_agent_ids")
        if not isinstance(targets, list) or not targets or any(
            not isinstance(value, str) or not value.strip() for value in targets
        ):
            raise ApiError(400, "INVALID_TARGETS", "target_agent_ids must be a non-empty string list")
        if len(set(targets)) != len(targets):
            raise ApiError(400, "INVALID_TARGETS", "target_agent_ids contains duplicates")
        if session.state != "SOURCE_CONNECTED":
            raise ApiError(409, "SOURCE_NOT_CONNECTED", "Video Server has not received a source frame")
        response: dict[str, Any] = {}
        for target in targets:
            grant = ConsumerGrant(agent_id=target, ticket=secrets.token_urlsafe(32))
            session.consumers[target] = grant
            response[target] = {
                "video_server_ip": self.public_ip,
                "offer_url": f"{self.base_url}/video/v1/sessions/{session.session_id}/processed",
                "access_ticket": grant.ticket,
                "protocol": "webrtc",
                "signaling": "non-trickle",
            }
        LOG.info("consumer grants id=%s targets=%s", session.session_id, targets)
        return web.json_response({"session_id": session.session_id, "consumers": response})

    async def source(self, request: web.Request) -> web.Response:
        session = self._session(request)
        self._require_bearer(request, session.producer_token)
        body = await self._json(request)
        sdp_type, sdp = self._sdp(body, "sdp_answer")
        if not sdp:
            if session.producer_pc is not None:
                await session.producer_pc.close()
            session.source_ready.clear()
            session.source_track = None
            session.state = "WAITING_FOR_SOURCE"
            pc = RTCPeerConnection()
            session.producer_pc = pc
            pc.addTransceiver("video", direction="recvonly")

            @pc.on("track")
            def on_track(track: MediaStreamTrack) -> None:
                if track.kind != "video":
                    return
                session.source_track = track
                session.source_probe_task = asyncio.create_task(self._probe_source(session, track))
                LOG.info("source track negotiated id=%s track=%s", session.session_id, track.id)

            @pc.on("connectionstatechange")
            async def on_state_change() -> None:
                LOG.info("source pc id=%s state=%s", session.session_id, pc.connectionState)
                if pc.connectionState in {"failed", "closed"} and session.state != "STOPPED":
                    session.state = "FAILED"

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
        self._require_bearer(request, session.producer_token)
        await session.close()
        return web.json_response({"session_id": session.session_id, "state": session.state})

    async def processed(self, request: web.Request) -> web.Response:
        session = self._session(request)
        if session.state != "SOURCE_CONNECTED" or session.source_track is None:
            raise ApiError(409, "SOURCE_NOT_CONNECTED", "processed stream is not ready")
        ticket = self._bearer(request)
        grant = next((value for value in session.consumers.values() if value.ticket == ticket), None)
        if grant is None:
            raise ApiError(403, "INVALID_CONSUMER_TICKET", "consumer ticket is invalid")
        body = await self._json(request)
        sdp_type, sdp = self._sdp(body, "sdp_offer")
        if sdp_type != "offer" or not sdp:
            raise ApiError(400, "INVALID_SDP", "consumer request must contain an SDP offer")
        pc = RTCPeerConnection()
        grant.peer_connections.add(pc)

        @pc.on("connectionstatechange")
        async def on_state_change() -> None:
            LOG.info(
                "consumer pc id=%s agent=%s state=%s",
                session.session_id,
                grant.agent_id,
                pc.connectionState,
            )
            if pc.connectionState in {"failed", "closed"}:
                grant.peer_connections.discard(pc)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        relayed = session.relay.subscribe(session.source_track)
        pc.addTrack(ProcessedVideoTrack(relayed))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await self._wait_ice_gathering(pc)
        local = pc.localDescription
        LOG.info("consumer connected id=%s agent=%s", session.session_id, grant.agent_id)
        return web.json_response(
            {
                "session_id": session.session_id,
                "consumer_agent_id": grant.agent_id,
                "state": "STREAMING",
                "sdp_answer": {"type": local.type, "sdp": local.sdp},
            }
        )

    async def _probe_source(self, session: VideoSession, track: MediaStreamTrack) -> None:
        probe = session.relay.subscribe(track)
        try:
            while True:
                await probe.recv()
                session.frames_seen += 1
                if session.frames_seen == 1:
                    session.state = "SOURCE_CONNECTED"
                    session.source_ready.set()
                    LOG.info("first source frame id=%s", session.session_id)
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
        await asyncio.gather(*(session.close() for session in self.sessions.values()), return_exceptions=True)

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

    def _required_string(self, body: dict[str, Any], field_name: str) -> str:
        value = body.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ApiError(400, "INVALID_ARGUMENT", f"{field_name} must be a non-empty string")
        return value

    def _bearer(self, request: web.Request) -> str:
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise ApiError(401, "MISSING_BEARER", "Authorization Bearer credential is required")
        return authorization[7:].strip()

    def _require_bearer(self, request: web.Request, expected: str) -> None:
        if not secrets.compare_digest(self._bearer(request), expected):
            raise ApiError(403, "INVALID_PRODUCER_TOKEN", "producer token is invalid")

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
