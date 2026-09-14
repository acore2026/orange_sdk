from __future__ import annotations

import asyncio
import sys
import unittest
from fractions import Fraction
from pathlib import Path

import numpy as np
from aiohttp import ClientSession, web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import MediaStreamTrack
from av import CodecContext, VideoFrame

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import (  # noqa: E402
    ApiError,
    H264_RTP_PAYLOAD_BYTES,
    MockVideoServer,
    require_h264_high_answer,
)


class SyntheticVideoTrack(VideoStreamTrack):
    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()
        image = np.zeros((120, 160, 3), dtype=np.uint8)
        image[:, :] = (15, 30, 45)
        frame = VideoFrame.from_ndarray(image, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base or Fraction(1, 90000)
        return frame


class HighProfilePacketTrack(MediaStreamTrack):
    """Send Annex-B H264 High Profile packets without aiortc re-encoding them."""

    kind = "video"

    def __init__(self) -> None:
        super().__init__()
        self._codec = CodecContext.create("libx264", "w")
        self._codec.width = 160
        self._codec.height = 120
        self._codec.bit_rate = 300_000
        self._codec.pix_fmt = "yuv420p"
        self._codec.framerate = Fraction(15, 1)
        self._codec.time_base = Fraction(1, 15)
        self._codec.options = {
            "level": "31",
            "profile": "high",
            "preset": "medium",
            "tune": "zerolatency",
        }
        self._frame_number = 0
        self.first_access_unit = b""

    async def recv(self):
        if self._frame_number:
            await asyncio.sleep(1 / 15)
        while True:
            image = np.zeros((120, 160, 3), dtype=np.uint8)
            image[:, :] = (15, 30, 45)
            frame = VideoFrame.from_ndarray(image, format="bgr24")
            frame.pts = self._frame_number
            frame.time_base = Fraction(1, 15)
            self._frame_number += 1
            packets = self._codec.encode(frame)
            if packets:
                if not self.first_access_unit:
                    self.first_access_unit = bytes(packets[0])
                return packets[0]


async def wait_ice(pc: RTCPeerConnection) -> None:
    if pc.iceGatheringState == "complete":
        return
    ready = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_state() -> None:
        if pc.iceGatheringState == "complete":
            ready.set()

    await asyncio.wait_for(ready.wait(), 8)


class MockVideoServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = MockVideoServer("127.0.0.1", 0, source_wait_seconds=8)
        self.runner = web.AppRunner(self.server.create_app())
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        socket = self.site._server.sockets[0]
        self.port = socket.getsockname()[1]
        self.server.port = self.port
        self.base = f"http://127.0.0.1:{self.port}"
        self.http = ClientSession()
        self.peer_connections: list[RTCPeerConnection] = []

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*(pc.close() for pc in self.peer_connections), return_exceptions=True)
        await self.http.close()
        await self.runner.cleanup()

    async def test_health_reports_bounded_h264_rtp_payload(self) -> None:
        response = await self.http.get(f"{self.base}/healthz")
        self.assertEqual(response.status, 200)
        health = await response.json()
        self.assertEqual(health["output_fps"], 30.0)
        self.assertEqual(health["h264_rtp_payload_bytes"], H264_RTP_PAYLOAD_BYTES)
        self.assertGreaterEqual(H264_RTP_PAYLOAD_BYTES, 1100)
        self.assertLessEqual(H264_RTP_PAYLOAD_BYTES, 1150)

    def test_source_answer_rejects_h264_baseline_fallback(self) -> None:
        baseline_answer = "\r\n".join(
            [
                "v=0",
                "m=video 9 UDP/TLS/RTP/SAVPF 99 100",
                "a=rtpmap:99 H264/90000",
                "a=fmtp:99 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
                "a=rtpmap:100 rtx/90000",
                "a=fmtp:100 apt=99",
                "",
            ]
        )
        with self.assertRaises(ApiError) as raised:
            require_h264_high_answer(baseline_answer)
        self.assertEqual(raised.exception.code, "SOURCE_CODEC_MISMATCH")

    async def test_create_requires_positive_sandbox_spec(self) -> None:
        response = await self.http.post(
            f"{self.base}/compute/v1/offloading-sessions",
            json={
                "request_id": "invalid-spec",
                "workload_type": "video",
                "sandbox_spec": {"vcpus": 0, "memory_mb": 4096},
            },
        )
        self.assertEqual(response.status, 400)
        error = await response.json()
        self.assertEqual(error["error"], "INVALID_ARGUMENT")

    async def test_formal_media_connections_are_role_scoped_and_idempotent(self) -> None:
        consumer_pc = RTCPeerConnection()
        self.peer_connections.append(consumer_pc)
        received = asyncio.get_running_loop().create_future()

        @consumer_pc.on("track")
        def on_track(track) -> None:
            if not received.done():
                received.set_result(track)

        consumer_pc.addTransceiver("video", direction="recvonly")
        consumer_offer = await consumer_pc.createOffer()
        await consumer_pc.setLocalDescription(consumer_offer)
        await wait_ice(consumer_pc)
        base_context = {
            "compute_service_session_id": "css-formal-1",
            "compute_instance_id": "ci-formal-1",
            "binding_ref": "binding-formal-1",
        }
        consumer_body = {
            "request_id": "media-consumer-1",
            "computing_context": {
                **base_context,
                "role": "consumer",
                "agent_id": "agent-a",
            },
            "offer": {
                "type": "offer",
                "sdp": consumer_pc.localDescription.sdp,
            },
        }
        response = await self.http.post(
            f"{self.base}/v1/media-connections",
            json=consumer_body,
        )
        self.assertEqual(response.status, 201, await response.text())
        consumer_result = await response.json()
        self.assertEqual(consumer_result["request_id"], consumer_body["request_id"])
        self.assertEqual(
            consumer_result["computing_context"],
            consumer_body["computing_context"],
        )
        self.assertEqual(consumer_result["answer"]["type"], "answer")
        await consumer_pc.setRemoteDescription(
            RTCSessionDescription(**consumer_result["answer"])
        )
        track = await asyncio.wait_for(received, 8)
        placeholder = await asyncio.wait_for(track.recv(), 8)
        self.assertLess(int(placeholder.to_ndarray(format="bgr24")[2, 2, 0]), 100)

        repeated = await self.http.post(
            f"{self.base}/v1/media-connections",
            json=consumer_body,
        )
        self.assertEqual(repeated.status, 201)
        self.assertEqual(
            (await repeated.json())["media_connection_id"],
            consumer_result["media_connection_id"],
        )
        conflict_body = {
            **consumer_body,
            "offer": {"type": "offer", "sdp": consumer_body["offer"]["sdp"] + "\r\n"},
        }
        conflict = await self.http.post(
            f"{self.base}/v1/media-connections",
            json=conflict_body,
        )
        self.assertEqual(conflict.status, 409)

        producer_pc = RTCPeerConnection()
        self.peer_connections.append(producer_pc)
        producer_pc.addTrack(SyntheticVideoTrack())
        producer_offer = await producer_pc.createOffer()
        await producer_pc.setLocalDescription(producer_offer)
        await wait_ice(producer_pc)
        producer_body = {
            "request_id": "media-producer-1",
            "computing_context": {
                **base_context,
                "role": "producer",
                "agent_id": "agent-b",
            },
            "offer": {
                "type": "offer",
                "sdp": producer_pc.localDescription.sdp,
            },
        }
        response = await self.http.post(
            f"{self.base}/v1/media-connections",
            json=producer_body,
        )
        self.assertEqual(response.status, 201, await response.text())
        producer_result = await response.json()
        await producer_pc.setRemoteDescription(
            RTCSessionDescription(**producer_result["answer"])
        )

        async def wait_for_source_frame() -> VideoFrame:
            while True:
                candidate = await track.recv()
                image = candidate.to_ndarray(format="bgr24")
                if int(image[2, 2, 0]) > 100:
                    return candidate

        self.assertIsNotNone(await asyncio.wait_for(wait_for_source_frame(), 8))

        for connection_id in (
            producer_result["media_connection_id"],
            consumer_result["media_connection_id"],
        ):
            deleted = await self.http.delete(
                f"{self.base}/v1/media-connections/{connection_id}"
            )
            self.assertEqual(deleted.status, 204)
            repeated_delete = await self.http.delete(
                f"{self.base}/v1/media-connections/{connection_id}"
            )
            self.assertEqual(repeated_delete.status, 204)

    async def test_end_to_end_source_and_consumer(self) -> None:
        response = await self.http.post(
            f"{self.base}/compute/v1/offloading-sessions",
            json={
                "request_id": "create-1",
                "workload_type": "video",
                "sandbox_spec": {"vcpus": 2, "memory_mb": 4096},
            },
        )
        self.assertEqual(response.status, 201)
        allocated = await response.json()
        session_id = allocated["session_id"]
        self.assertEqual(
            set(allocated),
            {"session_id", "state", "expires_at", "video_server_ip"},
        )
        self.assertEqual(allocated["video_server_ip"], "127.0.0.1")
        self.assertNotIn("producer", allocated)
        self.assertNotIn("processed_stream", allocated)

        response = await self.http.post(
            f"{self.base}/video/v1/sessions/{session_id}/source",
            json={"action": "create_offer"},
        )
        offer = (await response.json())["sdp_offer"]
        self.assertIn("profile-level-id=64001f", offer["sdp"])
        self.assertIn("profile-level-id=640c1f", offer["sdp"])
        self.assertNotIn("profile-level-id=42e01f", offer["sdp"])

        source_pc = RTCPeerConnection()
        self.peer_connections.append(source_pc)
        high_profile_track = HighProfilePacketTrack()
        source_pc.addTrack(high_profile_track)
        await source_pc.setRemoteDescription(RTCSessionDescription(**offer))
        answer = await source_pc.createAnswer()
        await source_pc.setLocalDescription(answer)
        self.assertIn(
            "profile-level-id=64001f",
            require_h264_high_answer(source_pc.localDescription.sdp),
        )
        await wait_ice(source_pc)
        response = await self.http.post(
            f"{self.base}/video/v1/sessions/{session_id}/source",
            json={
                "sdp_answer": {
                    "type": source_pc.localDescription.type,
                    "sdp": source_pc.localDescription.sdp,
                }
            },
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["state"], "SOURCE_CONNECTED")
        self.assertIn(b"\x00\x00\x00\x01\x67\x64", high_profile_track.first_access_unit)

        consumer_pc = RTCPeerConnection()
        self.peer_connections.append(consumer_pc)
        received = asyncio.get_running_loop().create_future()

        @consumer_pc.on("track")
        def on_track(track) -> None:
            if not received.done():
                received.set_result(track)

        consumer_pc.addTransceiver("video", direction="recvonly")
        consumer_offer = await consumer_pc.createOffer()
        await consumer_pc.setLocalDescription(consumer_offer)
        await wait_ice(consumer_pc)
        response = await self.http.post(
            f"{self.base}/video/v1/sessions/{session_id}/processed",
            json={
                "sdp_offer": {
                    "type": consumer_pc.localDescription.type,
                    "sdp": consumer_pc.localDescription.sdp,
                }
            },
        )
        self.assertEqual(response.status, 200, await response.text())
        remote_answer = (await response.json())["sdp_answer"]
        await consumer_pc.setRemoteDescription(RTCSessionDescription(**remote_answer))
        track = await asyncio.wait_for(received, 8)
        frame = await asyncio.wait_for(track.recv(), 8)
        image = frame.to_ndarray(format="bgr24")
        self.assertGreater(int(image[2, 2, 0]), 150)

        debug = await (await self.http.get(f"{self.base}/debug/v1/sessions")).json()
        session_debug = debug["sessions"][0]
        self.assertGreaterEqual(session_debug["frames_seen"], 1)
        self.assertIn("profile-level-id=64001f", session_debug["source_codec"])
        consumer_debug = session_debug["consumers"]["consumer-1"]
        self.assertGreaterEqual(consumer_debug["frames_processed"], 1)
        self.assertGreater(consumer_debug["packets_sent"], 0)
        self.assertGreater(consumer_debug["bytes_sent"], 0)
        self.assertNotIn(consumer_debug["codec"], {"<pending>", "<unknown>"})
        self.assertTrue(consumer_debug["first_frame_sent"])
        self.assertEqual(consumer_debug["keyframes_requested"], 1)

    async def test_consumer_connects_while_source_pending_and_switches_without_renegotiation(self) -> None:
        response = await self.http.post(
            f"{self.base}/compute/v1/offloading-sessions",
            json={
                "request_id": "create-parallel",
                "workload_type": "video",
                "sandbox_spec": {"vcpus": 2, "memory_mb": 4096},
            },
        )
        allocated = await response.json()
        session_id = allocated["session_id"]

        consumer_pc = RTCPeerConnection()
        self.peer_connections.append(consumer_pc)
        received = asyncio.get_running_loop().create_future()

        @consumer_pc.on("track")
        def on_track(track) -> None:
            if not received.done():
                received.set_result(track)

        consumer_pc.addTransceiver("video", direction="recvonly")
        consumer_offer = await consumer_pc.createOffer()
        await consumer_pc.setLocalDescription(consumer_offer)
        await wait_ice(consumer_pc)
        response = await self.http.post(
            f"{self.base}/video/v1/sessions/{session_id}/processed",
            json={
                "sdp_offer": {
                    "type": consumer_pc.localDescription.type,
                    "sdp": consumer_pc.localDescription.sdp,
                }
            },
        )
        self.assertEqual(response.status, 200, await response.text())
        consumer_answer = await response.json()
        self.assertEqual(consumer_answer["state"], "SOURCE_PENDING")
        await consumer_pc.setRemoteDescription(
            RTCSessionDescription(**consumer_answer["sdp_answer"])
        )
        track = await asyncio.wait_for(received, 8)
        original_track_id = track.id
        placeholder = await asyncio.wait_for(track.recv(), 8)
        placeholder_image = placeholder.to_ndarray(format="bgr24")
        self.assertLess(int(placeholder_image[2, 2, 0]), 100)

        observed_frames: list[tuple[int, int, int]] = []

        async def wait_for_source_frame() -> VideoFrame:
            while True:
                candidate = await track.recv()
                image = candidate.to_ndarray(format="bgr24")
                observed_frames.append(
                    (candidate.width, candidate.height, int(image[2, 2, 0]))
                )
                if int(image[2, 2, 0]) > 100:
                    return candidate

        source_frame_task = asyncio.create_task(wait_for_source_frame())

        response = await self.http.post(
            f"{self.base}/video/v1/sessions/{session_id}/source",
            json={"action": "create_offer"},
        )
        source_offer = (await response.json())["sdp_offer"]
        source_pc = RTCPeerConnection()
        self.peer_connections.append(source_pc)
        source_pc.addTrack(SyntheticVideoTrack())
        await source_pc.setRemoteDescription(RTCSessionDescription(**source_offer))
        source_answer = await source_pc.createAnswer()
        await source_pc.setLocalDescription(source_answer)
        await wait_ice(source_pc)
        response = await self.http.post(
            f"{self.base}/video/v1/sessions/{session_id}/source",
            json={
                "sdp_answer": {
                    "type": source_pc.localDescription.type,
                    "sdp": source_pc.localDescription.sdp,
                }
            },
        )
        self.assertEqual(response.status, 200, await response.text())

        source_frame = await asyncio.wait_for(source_frame_task, 8)
        self.assertIsNotNone(source_frame, observed_frames)
        self.assertEqual(track.id, original_track_id)

        debug = await (await self.http.get(f"{self.base}/debug/v1/sessions")).json()
        consumer_debug = debug["sessions"][0]["consumers"]["consumer-1"]
        self.assertGreater(consumer_debug["placeholder_frames_sent"], 0)
        self.assertGreater(consumer_debug["frames_processed"], 0)
        self.assertTrue(consumer_debug["first_source_frame_sent"])


if __name__ == "__main__":
    unittest.main()
