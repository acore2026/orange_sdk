from __future__ import annotations

import asyncio
import sys
import unittest
from fractions import Fraction
from pathlib import Path

import numpy as np
from aiohttp import ClientSession, web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import MockVideoServer  # noqa: E402


class SyntheticVideoTrack(VideoStreamTrack):
    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()
        image = np.zeros((120, 160, 3), dtype=np.uint8)
        image[:, :] = (15, 30, 45)
        frame = VideoFrame.from_ndarray(image, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base or Fraction(1, 90000)
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

    async def test_end_to_end_source_and_consumer(self) -> None:
        response = await self.http.post(
            f"{self.base}/compute/v1/offloading-sessions",
            json={
                "request_id": "create-1",
                "agent_id": "agent-b",
                "group_id": "group-ab",
                "workload_type": "video",
            },
        )
        self.assertEqual(response.status, 201)
        allocated = await response.json()
        session_id = allocated["session_id"]
        token = allocated["producer"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        response = await self.http.post(
            f"{self.base}/video/v1/sessions/{session_id}/source",
            headers=headers,
            json={"action": "create_offer"},
        )
        offer = (await response.json())["sdp_offer"]

        source_pc = RTCPeerConnection()
        self.peer_connections.append(source_pc)
        source_pc.addTrack(SyntheticVideoTrack())
        await source_pc.setRemoteDescription(RTCSessionDescription(**offer))
        answer = await source_pc.createAnswer()
        await source_pc.setLocalDescription(answer)
        await wait_ice(source_pc)
        response = await self.http.post(
            f"{self.base}/video/v1/sessions/{session_id}/source",
            headers=headers,
            json={
                "sdp_answer": {
                    "type": source_pc.localDescription.type,
                    "sdp": source_pc.localDescription.sdp,
                }
            },
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["state"], "SOURCE_CONNECTED")

        response = await self.http.post(
            f"{self.base}/compute/v1/offloading-sessions/{session_id}/consumers",
            json={
                "agent_id": "agent-b",
                "group_id": "group-ab",
                "target_agent_ids": ["agent-a", "agent-c"],
            },
        )
        grants = (await response.json())["consumers"]
        self.assertNotEqual(grants["agent-a"]["access_ticket"], grants["agent-c"]["access_ticket"])

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
            grants["agent-a"]["offer_url"],
            headers={"Authorization": f"Bearer {grants['agent-a']['access_ticket']}"},
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
        self.assertGreaterEqual(debug["sessions"][0]["frames_seen"], 1)


if __name__ == "__main__":
    unittest.main()
