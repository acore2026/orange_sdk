#!/usr/bin/env python3
"""Synthetic U-MEDIA WebRTC probe for a deployed Mock Video Sandbox."""

from __future__ import annotations

import argparse
import asyncio
import json

import numpy as np
from aiohttp import ClientSession, FormData
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


async def wait_ice(pc: RTCPeerConnection, timeout_seconds: float) -> None:
    if pc.iceGatheringState == "complete":
        return
    ready = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_state() -> None:
        if pc.iceGatheringState == "complete":
            ready.set()

    await asyncio.wait_for(ready.wait(), timeout_seconds)


async def create_media_connection(
    http: ClientSession,
    base_url: str,
    pc: RTCPeerConnection,
    *,
    request_id: str,
    context: dict[str, str],
    timeout_seconds: float,
) -> dict[str, object]:
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await wait_ice(pc, timeout_seconds)
    response = await http.post(
        f"{base_url}/v1/media-connections",
        json={
            "request_id": request_id,
            "computing_context": context,
            "offer": {
                "type": "offer",
                "sdp": pc.localDescription.sdp,
            },
        },
    )
    response.raise_for_status()
    result = await response.json()
    if result.get("request_id") != request_id:
        raise RuntimeError("Sandbox did not echo request_id")
    if result.get("computing_context") != context:
        raise RuntimeError("Sandbox did not echo computing_context")
    answer = result.get("answer")
    if not isinstance(answer, dict) or answer.get("type") != "answer":
        raise RuntimeError("Sandbox response does not contain an SDP Answer")
    await pc.setRemoteDescription(RTCSessionDescription(**answer))
    return result


async def run(base_url: str, media_timeout: float = 30.0) -> dict[str, object]:
    base_url = base_url.rstrip("/")
    peers: list[RTCPeerConnection] = []
    connection_ids: list[str] = []
    base_context = {
        "compute_service_session_id": "css-smoke-001",
        "compute_instance_id": "ci-smoke-001",
        "binding_ref": "binding-smoke-001",
    }
    async with ClientSession() as http:
        try:
            consumer_context = {
                **base_context,
                "role": "consumer",
                "agent_id": "agent-a",
            }
            recognition_response = await http.put(
                f"{base_url}/v1/recognition-targets/{base_context['compute_service_session_id']}",
                json={
                    "request_id": "recognition-smoke",
                    "computing_context": consumer_context,
                    "input": {"type": "TEXT", "text": "寻找红色玩偶", "language": "zh"},
                },
            )
            recognition_response.raise_for_status()
            recognition = await recognition_response.json()
            fetched_recognition_response = await http.get(
                f"{base_url}/v1/recognition-targets/{base_context['compute_service_session_id']}"
            )
            fetched_recognition_response.raise_for_status()
            if await fetched_recognition_response.json() != recognition:
                raise RuntimeError("recognition target GET did not return the applied target")

            control_response = await http.post(
                f"{base_url}/v1/control-actions",
                json={
                    "request_id": "control-smoke",
                    "computing_context": consumer_context,
                    "input": {"type": "TEXT", "text": "寻找杯子", "language": "zh"},
                },
            )
            control_response.raise_for_status()
            control = await control_response.json()
            action_response = await http.get(
                f"{base_url}/v1/control-actions/{control['action_id']}"
            )
            action_response.raise_for_status()
            completed_action = await action_response.json()
            if completed_action.get("status") != "COMPLETED":
                raise RuntimeError("control action did not complete")

            audio_form = FormData()
            audio_form.add_field("request_id", "audio-control-smoke")
            audio_form.add_field("computing_context", json.dumps(consumer_context))
            audio_form.add_field("language", "zh")
            audio_form.add_field(
                "file",
                b"RIFFmock-wave-bytes",
                filename="command.wav",
                content_type="audio/wav",
            )
            audio_response = await http.post(
                f"{base_url}/v1/audio-control-actions",
                data=audio_form,
            )
            audio_response.raise_for_status()
            audio_control = await audio_response.json()
            if not audio_control.get("transcription", {}).get("text"):
                raise RuntimeError("audio control response has no transcription")

            consumer_pc = RTCPeerConnection()
            peers.append(consumer_pc)
            track_ready = asyncio.get_running_loop().create_future()

            @consumer_pc.on("track")
            def on_track(track) -> None:
                if not track_ready.done():
                    track_ready.set_result(track)

            consumer_pc.addTransceiver("video", direction="recvonly")
            consumer_result = await create_media_connection(
                http,
                base_url,
                consumer_pc,
                request_id="media-consumer-smoke",
                context=consumer_context,
                timeout_seconds=media_timeout,
            )
            connection_ids.append(str(consumer_result["media_connection_id"]))
            remote_track = await asyncio.wait_for(track_ready, media_timeout)
            original_track_id = remote_track.id
            placeholder = await asyncio.wait_for(remote_track.recv(), media_timeout)
            placeholder_image = placeholder.to_ndarray(format="bgr24")
            if int(placeholder_image[2, 2, 0]) >= 100:
                raise RuntimeError("consumer did not receive a placeholder before producer startup")

            async def wait_for_processed_frame() -> tuple[VideoFrame, list[int]]:
                while True:
                    candidate = await remote_track.recv()
                    image = candidate.to_ndarray(format="bgr24")
                    marker = image[2, 2].tolist()
                    if marker[0] >= 150 and marker[2] >= 150:
                        return candidate, marker

            processed_frame_task = asyncio.create_task(wait_for_processed_frame())
            producer_pc = RTCPeerConnection()
            peers.append(producer_pc)
            producer_pc.addTrack(SyntheticVideoTrack())
            producer_result = await create_media_connection(
                http,
                base_url,
                producer_pc,
                request_id="media-producer-smoke",
                context={**base_context, "role": "producer", "agent_id": "agent-b"},
                timeout_seconds=media_timeout,
            )
            connection_ids.append(str(producer_result["media_connection_id"]))

            frame, marker = await asyncio.wait_for(processed_frame_task, media_timeout)
            if remote_track.id != original_track_id:
                raise RuntimeError("processed source switch replaced the consumer track")
            return {
                "ok": True,
                "compute_service_session_id": base_context["compute_service_session_id"],
                "consumer_media_connection_id": connection_ids[0],
                "producer_media_connection_id": connection_ids[1],
                "placeholder_frame": f"{placeholder.width}x{placeholder.height}",
                "processed_frame": f"{frame.width}x{frame.height}",
                "processed_marker_bgr": marker,
                "consumer_track_reused": True,
                "recognition_target_revision": recognition["target_revision"],
                "text_control_action": control["normalized_action"],
                "audio_transcription": audio_control["transcription"]["text"],
            }
        finally:
            try:
                for connection_id in reversed(connection_ids):
                    response = await http.delete(
                        f"{base_url}/v1/media-connections/{connection_id}"
                    )
                    if response.status != 204:
                        raise RuntimeError(
                            f"failed to delete media connection {connection_id}: HTTP {response.status}"
                        )
            finally:
                await asyncio.gather(*(pc.close() for pc in peers), return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://172.30.0.10:28500")
    parser.add_argument("--media-timeout", type=float, default=30.0)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(run(args.base_url, args.media_timeout)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
