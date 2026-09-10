#!/usr/bin/env python3
"""Synthetic WebRTC end-to-end probe for a deployed Mock Video Server."""

from __future__ import annotations

import argparse
import asyncio
import fractions
import json

import numpy as np
from aiohttp import ClientSession
from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from av import CodecContext, VideoFrame

from server import H264_HIGH_PROFILE_LEVEL_IDS, register_h264_high_profiles


register_h264_high_profiles()


class HighProfilePacketTrack(MediaStreamTrack):
    """Generate real Annex-B H264 High Profile packets for the source smoke path."""

    kind = "video"

    def __init__(self) -> None:
        super().__init__()
        self._codec = CodecContext.create("libx264", "w")
        self._codec.width = 320
        self._codec.height = 180
        self._codec.bit_rate = 300_000
        self._codec.pix_fmt = "yuv420p"
        self._codec.framerate = fractions.Fraction(15, 1)
        self._codec.time_base = fractions.Fraction(1, 15)
        self._codec.options = {
            "level": "31",
            "profile": "high",
            "preset": "medium",
            "tune": "zerolatency",
        }
        self._frame_number = 0

    async def recv(self):
        if self._frame_number:
            await asyncio.sleep(1 / 15)
        while True:
            image = np.zeros((180, 320, 3), dtype=np.uint8)
            image[:, :] = (20, 40, 60)
            frame = VideoFrame.from_ndarray(image, format="bgr24")
            frame.pts = self._frame_number
            frame.time_base = fractions.Fraction(1, 15)
            self._frame_number += 1
            packets = self._codec.encode(frame)
            if packets:
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


def prefer_h264_baseline(transceiver) -> None:
    codecs = RTCRtpSender.getCapabilities("video").codecs
    h264 = [
        codec
        for codec in codecs
        if codec.mimeType.lower() == "video/h264"
        and str(codec.parameters.get("profile-level-id", "")).lower()
        not in H264_HIGH_PROFILE_LEVEL_IDS
    ]
    retransmission = [codec for codec in codecs if codec.mimeType.lower() == "video/rtx"]
    fallback = [
        codec
        for codec in codecs
        if codec.mimeType.lower() not in {"video/h264", "video/rtx"}
    ]
    if h264:
        transceiver.setCodecPreferences(h264 + retransmission + fallback)


async def run(base_url: str) -> dict[str, object]:
    base_url = base_url.rstrip("/")
    peers: list[RTCPeerConnection] = []
    async with ClientSession() as http:
        try:
            response = await http.post(
                f"{base_url}/compute/v1/offloading-sessions",
                json={
                    "request_id": "smoke-create",
                    "workload_type": "video_relay",
                    "sandbox_spec": {"vcpus": 2, "memory_mb": 4096},
                },
            )
            response.raise_for_status()
            session = await response.json()
            session_id = session["session_id"]
            producer = session["producer"]
            processed_stream = session["processed_stream"]
            consumer_pc = RTCPeerConnection()
            peers.append(consumer_pc)
            track_ready = asyncio.get_running_loop().create_future()

            @consumer_pc.on("track")
            def on_track(track) -> None:
                if not track_ready.done():
                    track_ready.set_result(track)

            consumer_transceiver = consumer_pc.addTransceiver("video", direction="recvonly")
            prefer_h264_baseline(consumer_transceiver)
            offer = await consumer_pc.createOffer()
            await consumer_pc.setLocalDescription(offer)
            await wait_ice(consumer_pc)
            response = await http.post(
                processed_stream["offer_url"],
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
            original_track_id = remote_track.id
            placeholder = await asyncio.wait_for(remote_track.recv(), 8)
            placeholder_image = placeholder.to_ndarray(format="bgr24")
            if int(placeholder_image[2, 2, 0]) >= 100:
                raise RuntimeError("consumer did not receive a placeholder before source startup")

            async def wait_for_processed_frame() -> tuple[VideoFrame, list[int]]:
                while True:
                    candidate = await remote_track.recv()
                    image = candidate.to_ndarray(format="bgr24")
                    marker = image[2, 2].tolist()
                    if marker[0] >= 150 and marker[2] >= 150:
                        return candidate, marker

            processed_frame_task = asyncio.create_task(wait_for_processed_frame())

            response = await http.post(
                producer["source_start_url"],
                json={"action": "create_offer"},
            )
            response.raise_for_status()
            source_offer = (await response.json())["sdp_offer"]
            if not all(
                f"profile-level-id={profile_level_id}" in source_offer["sdp"]
                for profile_level_id in H264_HIGH_PROFILE_LEVEL_IDS
            ):
                raise RuntimeError("source offer does not require H264 High Profile")
            source_pc = RTCPeerConnection()
            peers.append(source_pc)
            source_pc.addTrack(HighProfilePacketTrack())
            await source_pc.setRemoteDescription(RTCSessionDescription(**source_offer))
            answer = await source_pc.createAnswer()
            await source_pc.setLocalDescription(answer)
            await wait_ice(source_pc)
            response = await http.post(
                producer["source_start_url"],
                json={
                    "sdp_answer": {
                        "type": source_pc.localDescription.type,
                        "sdp": source_pc.localDescription.sdp,
                    }
                },
            )
            response.raise_for_status()
            source_state = await response.json()

            frame, marker = await asyncio.wait_for(processed_frame_task, 8)
            if remote_track.id != original_track_id:
                raise RuntimeError("processed source switch unexpectedly replaced the consumer track")
            if marker[0] < 150 or marker[2] < 150:
                raise RuntimeError(f"processed frame marker is missing: {marker}")
            return {
                "ok": True,
                "session_id": session_id,
                "source_state": source_state["state"],
                "placeholder_frame": f"{placeholder.width}x{placeholder.height}",
                "processed_frame": f"{frame.width}x{frame.height}",
                "processed_marker_bgr": marker,
                "consumer_track_reused": True,
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
