from __future__ import annotations

import asyncio
import os
import unittest
from fractions import Fraction
from unittest.mock import patch

import numpy as np
from aiohttp import ClientSession, web
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from av import VideoFrame

from services.sandbox.api import canonical_digest
from services.sandbox.main import VideoRuntime, create_management_app, create_user_app
from services.video.config import VideoSettings


class RawVideoTrack(MediaStreamTrack):
    """A terminal-like raw camera source for the standard Offer flow."""

    kind = "video"

    def __init__(self) -> None:
        super().__init__()
        self._frame_number = 0

    async def recv(self) -> VideoFrame:
        if self._frame_number:
            await asyncio.sleep(1 / 15)
        image = np.zeros((120, 160, 3), dtype=np.uint8)
        image[:, :] = (15, 30, 45)
        frame = VideoFrame.from_ndarray(image, format="bgr24")
        frame.pts = self._frame_number
        frame.time_base = Fraction(1, 15)
        self._frame_number += 1
        return frame


async def wait_ice(pc: RTCPeerConnection) -> None:
    if pc.iceGatheringState == "complete":
        return
    ready = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_state() -> None:
        if pc.iceGatheringState == "complete":
            ready.set()

    await asyncio.wait_for(ready.wait(), 8)


def _configuration(session_id: str) -> dict:
    return {
        "compute_service_session_id": session_id,
        "connection_parameters": {
            "transport": "WEBRTC",
            "media_connections_path": "/v1/media-connections",
            "recognition_target_path_template": "/v1/recognition-targets/{compute_service_session_id}",
        },
        "participants_network_facts": [
            {
                "role": "consumer",
                "agent_id": "agent-a",
                "up_session_id": "ups-agent-a",
                "pdu_session_id": 1,
                "ue_ipv4_address": "10.60.0.11",
                "dnn": "internet",
                "snssai": "1-010203",
                "generation": "1",
            },
            {
                "role": "producer",
                "agent_id": "agent-b",
                "up_session_id": "ups-agent-b",
                "pdu_session_id": 1,
                "ue_ipv4_address": "10.60.0.12",
                "dnn": "internet",
                "snssai": "1-010203",
                "generation": "1",
            },
        ],
    }


class DualPlaneServerTest(unittest.IsolatedAsyncioTestCase):
    """Standard-interface behaviour against the real mock media engine."""

    async def asyncSetUp(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VIDEO_PUBLIC_IP": "127.0.0.1",
                "VIDEO_SOURCE_WAIT_SECONDS": "8",
                "VIDEO_H264_RTP_PAYLOAD_BYTES": "1150",
                "WEBRTC_ICE_SERVERS": "",
                "SANDBOX_INTENT_URL": "",
                "VIDEO_WIDTH": "160",
                "VIDEO_HEIGHT": "120",
                "VIDEO_FPS": "10",
            },
            clear=True,
        ):
            self.settings = VideoSettings.from_env()
        self.runtime = VideoRuntime.build(self.settings)
        self.management_runner = web.AppRunner(create_management_app(self.runtime))
        self.user_runner = web.AppRunner(create_user_app(self.runtime))
        await self.management_runner.setup()
        await self.user_runner.setup()
        self.management_site = web.TCPSite(self.management_runner, "127.0.0.1", 0)
        self.user_site = web.TCPSite(self.user_runner, "127.0.0.1", 0)
        await self.management_site.start()
        await self.user_site.start()
        management_port = self.management_site._server.sockets[0].getsockname()[1]
        user_port = self.user_site._server.sockets[0].getsockname()[1]
        self.management_base = f"http://127.0.0.1:{management_port}"
        self.base = f"http://127.0.0.1:{user_port}"
        self.http = ClientSession()
        self.peer_connections: list[RTCPeerConnection] = []

    async def asyncTearDown(self) -> None:
        await asyncio.gather(
            *(pc.close() for pc in self.peer_connections),
            return_exceptions=True,
        )
        await self.http.close()
        await self.management_runner.cleanup()
        await self.user_runner.cleanup()

    async def bind(self, session_id: str = "css-1") -> dict:
        configuration = _configuration(session_id)
        response = await self.http.post(
            f"{self.management_base}/management/v1/compute-session-bindings:bind",
            json={
                "activation_idempotency_key": f"activate-{session_id}",
                "owner_ref": f"ca/{session_id}",
                "binding_ref": f"binding-{session_id}",
                "compute_service_session_id": session_id,
                "compute_instance_id": f"ci-{session_id}",
                "configuration": configuration,
                "configuration_digest": canonical_digest(configuration),
                "expected_binding_absent": True,
            },
        )
        self.assertEqual(200, response.status, await response.text())
        return await response.json()

    def context(self, session_id: str, role: str) -> dict:
        return {
            "compute_service_session_id": session_id,
            "compute_instance_id": f"ci-{session_id}",
            "binding_ref": f"binding-{session_id}",
            "role": role,
            "agent_id": "agent-a" if role == "consumer" else "agent-b",
        }

    async def wait_for_action(self, action_id: str) -> dict:
        for _ in range(200):
            response = await self.http.get(f"{self.base}/v1/control-actions/{action_id}")
            payload = await response.json()
            if payload["status"] not in {"ACCEPTED", "RUNNING"}:
                return payload
            await asyncio.sleep(0.05)
        self.fail(f"action {action_id} did not finish")

    async def test_user_plane_rejects_unbound_context(self) -> None:
        response = await self.http.post(
            f"{self.base}/v1/media-connections",
            json={
                "request_id": "media-unbound",
                "computing_context": self.context("css-missing", "consumer"),
                "offer": {"type": "offer", "sdp": "v=0"},
            },
        )
        self.assertEqual(409, response.status)
        body = await response.json()
        self.assertEqual("binding-mismatch", body["error"]["code"])

    async def test_management_routes_are_not_served_on_user_port(self) -> None:
        response = await self.http.post(
            f"{self.base}/management/v1/compute-session-bindings:bind",
            json={},
        )
        self.assertEqual(404, response.status)
        response = await self.http.get(f"{self.management_base}/v1/control-actions/x")
        self.assertEqual(404, response.status)

    async def test_media_connections_are_role_scoped_and_idempotent(self) -> None:
        await self.bind("css-media-1")
        consumer_pc = RTCPeerConnection()
        self.peer_connections.append(consumer_pc)
        received = asyncio.get_running_loop().create_future()

        @consumer_pc.on("track")
        def on_track(track) -> None:
            if not received.done():
                received.set_result(track)

        consumer_pc.addTransceiver("video", direction="recvonly")
        offer = await consumer_pc.createOffer()
        await consumer_pc.setLocalDescription(offer)
        await wait_ice(consumer_pc)
        body = {
            "request_id": "media-consumer-1",
            "computing_context": self.context("css-media-1", "consumer"),
            "offer": {"type": "offer", "sdp": consumer_pc.localDescription.sdp},
        }
        response = await self.http.post(f"{self.base}/v1/media-connections", json=body)
        self.assertEqual(201, response.status, await response.text())
        created = await response.json()
        self.assertEqual(body["request_id"], created["request_id"])
        self.assertEqual(body["computing_context"], created["computing_context"])
        await consumer_pc.setRemoteDescription(RTCSessionDescription(**created["answer"]))
        track = await asyncio.wait_for(received, 8)
        placeholder = await asyncio.wait_for(track.recv(), 8)
        self.assertLess(int(placeholder.to_ndarray(format="bgr24")[2, 2, 0]), 100)

        replay = await self.http.post(f"{self.base}/v1/media-connections", json=body)
        self.assertEqual(201, replay.status)
        self.assertEqual(
            created["media_connection_id"],
            (await replay.json())["media_connection_id"],
        )

        conflict = await self.http.post(
            f"{self.base}/v1/media-connections",
            json={
                **body,
                "offer": {"type": "offer", "sdp": body["offer"]["sdp"] + "\r\n"},
            },
        )
        self.assertEqual(409, conflict.status)
        self.assertEqual(
            "idempotency-conflict",
            (await conflict.json())["error"]["code"],
        )

        second_consumer_pc = RTCPeerConnection()
        self.peer_connections.append(second_consumer_pc)
        second_consumer_pc.addTransceiver("video", direction="recvonly")
        second_offer = await second_consumer_pc.createOffer()
        await second_consumer_pc.setLocalDescription(second_offer)
        await wait_ice(second_consumer_pc)
        duplicate_role = await self.http.post(
            f"{self.base}/v1/media-connections",
            json={
                "request_id": "media-consumer-2",
                "computing_context": self.context("css-media-1", "consumer"),
                "offer": {
                    "type": "offer",
                    "sdp": second_consumer_pc.localDescription.sdp,
                },
            },
        )
        self.assertEqual(409, duplicate_role.status)
        self.assertEqual(
            "media-connection-exists",
            (await duplicate_role.json())["error"]["code"],
        )

        delete_url = f"{self.base}/v1/media-connections/{created['media_connection_id']}"
        deleted = await self.http.delete(delete_url)
        self.assertEqual(204, deleted.status)
        repeated_delete = await self.http.delete(delete_url)
        self.assertEqual(204, repeated_delete.status)
        missing = await self.http.delete(f"{self.base}/v1/media-connections/media-connection-nope")
        self.assertEqual(404, missing.status)
        self.assertEqual(
            "media-connection-not-found",
            (await missing.json())["error"]["code"],
        )

    async def test_recognition_and_search_return_fixed_results(self) -> None:
        await self.bind("css-recognition-1")
        context = self.context("css-recognition-1", "consumer")

        recognition = await self.http.put(
            f"{self.base}/v1/recognition-targets/css-recognition-1",
            json={
                "request_id": "recognition-1",
                "computing_context": context,
                "input": {"type": "TEXT", "text": "寻找红色玩偶", "language": "zh"},
            },
        )
        self.assertEqual(200, recognition.status, await recognition.text())
        applied = await recognition.json()
        self.assertEqual("1", applied["target_revision"])
        self.assertEqual("APPLIED", applied["status"])
        self.assertEqual({"label": "红色玩偶", "prompt": "red doll"}, applied["target"])
        fetched = await self.http.get(
            f"{self.base}/v1/recognition-targets/css-recognition-1"
        )
        self.assertEqual(applied, await fetched.json())

        producer_pc = RTCPeerConnection()
        self.peer_connections.append(producer_pc)
        producer_pc.addTrack(RawVideoTrack())
        offer = await producer_pc.createOffer()
        await producer_pc.setLocalDescription(offer)
        await wait_ice(producer_pc)
        response = await self.http.post(
            f"{self.base}/v1/media-connections",
            json={
                "request_id": "media-producer-1",
                "computing_context": self.context("css-recognition-1", "producer"),
                "offer": {"type": "offer", "sdp": producer_pc.localDescription.sdp},
            },
        )
        self.assertEqual(201, response.status, await response.text())
        await producer_pc.setRemoteDescription(
            RTCSessionDescription(**(await response.json())["answer"])
        )

        text_action = await self.http.post(
            f"{self.base}/v1/control-actions",
            json={
                "request_id": "control-text-1",
                "computing_context": context,
                "input": {"type": "TEXT", "text": "寻找杯子", "language": "zh"},
            },
        )
        self.assertEqual(202, text_action.status, await text_action.text())
        accepted = await text_action.json()
        self.assertEqual("search_object", accepted["normalized_action"])
        self.assertEqual({"query": "cup"}, accepted["normalized_parameters"])
        self.assertEqual("ACCEPTED", accepted["status"])
        self.assertTrue(accepted["control_triggered"])
        completed = await self.wait_for_action(accepted["action_id"])
        self.assertEqual("COMPLETED", completed["status"])
        self.assertEqual("cup", completed["result"]["query"])
        self.assertEqual("cup", completed["result"]["matches"][0]["label"])
        self.assertEqual(0.99, completed["result"]["matches"][0]["confidence"])

        structured = await self.http.post(
            f"{self.base}/v1/control-actions",
            json={
                "request_id": "control-structured-1",
                "computing_context": context,
                "input": {"type": "STRUCTURED"},
                "action": "movement",
                "parameters": {"direction": "right"},
            },
        )
        self.assertEqual(202, structured.status, await structured.text())
        structured_body = await structured.json()
        self.assertEqual("movement", structured_body["normalized_action"])
        self.assertEqual({"direction": "right"}, structured_body["normalized_parameters"])
        # 未配置机器狗业务端点：与 pruned_sandbox 一致进入 FAILED。
        finished = await self.wait_for_action(structured_body["action_id"])
        self.assertEqual("FAILED", finished["status"])
        self.assertEqual("producer-control-endpoint-unconfigured", finished["cause"])

    async def test_unbind_releases_media_and_rejects_reuse(self) -> None:
        await self.bind("css-unbind-1")
        consumer_pc = RTCPeerConnection()
        self.peer_connections.append(consumer_pc)
        consumer_pc.addTransceiver("video", direction="recvonly")
        offer = await consumer_pc.createOffer()
        await consumer_pc.setLocalDescription(offer)
        await wait_ice(consumer_pc)
        response = await self.http.post(
            f"{self.base}/v1/media-connections",
            json={
                "request_id": "media-unbind-consumer",
                "computing_context": self.context("css-unbind-1", "consumer"),
                "offer": {"type": "offer", "sdp": consumer_pc.localDescription.sdp},
            },
        )
        self.assertEqual(201, response.status, await response.text())
        created = await response.json()

        response = await self.http.post(
            f"{self.management_base}/management/v1/compute-session-bindings/"
            "binding-css-unbind-1:unbind",
            json={
                "owner_ref": "ca/css-unbind-1",
                "unbind_idempotency_key": "unbind-css-unbind-1",
                "cause": "released",
            },
        )
        self.assertEqual(200, response.status, await response.text())
        unbound = await response.json()
        self.assertEqual("UNBOUND", unbound["state"])
        self.assertFalse(unbound["initialized"])

        state = await self.http.get(
            f"{self.management_base}/management/v1/compute-session-bindings/"
            "binding-css-unbind-1/media-state"
        )
        state_body = await state.json()
        self.assertFalse(state_body["consumer_connected"])
        self.assertFalse(state_body["producer_connected"])

        reuse = await self.http.post(
            f"{self.base}/v1/media-connections",
            json={
                "request_id": "media-after-unbind",
                "computing_context": self.context("css-unbind-1", "consumer"),
                "offer": {"type": "offer", "sdp": "v=0"},
            },
        )
        self.assertEqual(409, reuse.status)
        self.assertEqual("binding-mismatch", (await reuse.json())["error"]["code"])

        deleted = await self.http.delete(
            f"{self.base}/v1/media-connections/{created['media_connection_id']}"
        )
        self.assertEqual(204, deleted.status)

    async def test_detection_classes_extension_reports_fixed_detector(self) -> None:
        response = await self.http.post(
            f"{self.base}/api/v1/detection/classes",
            json={"classes": ["person", "dog"]},
        )
        self.assertEqual(200, response.status, await response.text())
        self.assertEqual({"classes": ["person", "dog"]}, await response.json())

        response = await self.http.get(f"{self.base}/api/v1/detection/classes")
        self.assertEqual({"classes": ["person", "dog"]}, await response.json())

        health = await (await self.http.get(f"{self.base}/health")).json()
        self.assertEqual("video", health["service"])
        self.assertEqual("mock-fixed-detector", health["yolo"]["model"])
        self.assertTrue(health["yolo"]["enabled"])

        invalid = await self.http.post(
            f"{self.base}/api/v1/detection/classes",
            json={"classes": "person"},
        )
        self.assertEqual(400, invalid.status)


if __name__ == "__main__":
    unittest.main()
