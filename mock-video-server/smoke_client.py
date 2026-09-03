#!/usr/bin/env python3
"""Synthetic WebRTC end-to-end probe for a deployed Mock Video Server."""

from __future__ import annotations

import argparse
import asyncio
import json

import numpy as np
from aiohttp import ClientSession
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame


class SyntheticVideoTrack(VideoStreamTrack):
    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()
        image = np.zeros((180, 320, 3), dtype=np.uint8)
        image[:, :] = (20, 40, 60)
        frame = VideoFrame.from_ndarray(image, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base
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


async def run(base_url: str) -> dict[str, object]:
    base_url = base_url.rstrip("/")
    peers: list[RTCPeerConnection] = []
    async with ClientSession() as http:
        try:
            response = await http.post(
                f"{base_url}/compute/v1/offloading-sessions",
                json={
                    "request_id": "smoke-create",
                    "agent_id": "smoke-agent-b",
                    "group_id": "smoke-group",
                    "workload_type": "video_relay",
                },
            )
            response.raise_for_status()
            session = await response.json()
            session_id = session["session_id"]
            producer = session["producer"]
            producer_headers = {"Authorization": f"Bearer {producer['access_token']}"}

            response = await http.post(
                producer["source_start_url"],
                headers=producer_headers,
                json={"action": "create_offer"},
            )
            response.raise_for_status()
            source_offer = (await response.json())["sdp_offer"]
            source_pc = RTCPeerConnection()
            peers.append(source_pc)
            source_pc.addTrack(SyntheticVideoTrack())
            await source_pc.setRemoteDescription(RTCSessionDescription(**source_offer))
            answer = await source_pc.createAnswer()
            await source_pc.setLocalDescription(answer)
            await wait_ice(source_pc)
            response = await http.post(
                producer["source_start_url"],
                headers=producer_headers,
                json={
                    "sdp_answer": {
                        "type": source_pc.localDescription.type,
                        "sdp": source_pc.localDescription.sdp,
                    }
                },
            )
            response.raise_for_status()
            source_state = await response.json()

            response = await http.post(
                f"{base_url}/compute/v1/offloading-sessions/{session_id}/consumers",
                json={
                    "agent_id": "smoke-agent-b",
                    "group_id": "smoke-group",
                    "target_agent_ids": ["smoke-agent-a"],
                },
            )
            response.raise_for_status()
            consumer = (await response.json())["consumers"]["smoke-agent-a"]
            consumer_pc = RTCPeerConnection()
            peers.append(consumer_pc)
            track_ready = asyncio.get_running_loop().create_future()

            @consumer_pc.on("track")
            def on_track(track) -> None:
                if not track_ready.done():
                    track_ready.set_result(track)

            consumer_pc.addTransceiver("video", direction="recvonly")
            offer = await consumer_pc.createOffer()
            await consumer_pc.setLocalDescription(offer)
            await wait_ice(consumer_pc)
            response = await http.post(
                consumer["offer_url"],
                headers={"Authorization": f"Bearer {consumer['access_ticket']}"},
                json={
                    "sdp_offer": {
                        "type": consumer_pc.localDescription.type,
                        "sdp": consumer_pc.localDescription.sdp,
                    }
                },
            )
            response.raise_for_status()
            remote_answer = (await response.json())["sdp_answer"]
            await consumer_pc.setRemoteDescription(RTCSessionDescription(**remote_answer))
            remote_track = await asyncio.wait_for(track_ready, 8)
            frame = await asyncio.wait_for(remote_track.recv(), 8)
            image = frame.to_ndarray(format="bgr24")
            marker = image[2, 2].tolist()
            if marker[0] < 150 or marker[2] < 150:
                raise RuntimeError(f"processed frame marker is missing: {marker}")
            return {
                "ok": True,
                "session_id": session_id,
                "source_state": source_state["state"],
                "processed_frame": f"{frame.width}x{frame.height}",
                "processed_marker_bgr": marker,
            }
        finally:
            await asyncio.gather(*(pc.close() for pc in peers), return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://172.30.0.10:28500")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.base_url)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
