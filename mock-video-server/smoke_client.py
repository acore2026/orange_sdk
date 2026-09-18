#!/usr/bin/env python3
"""Synthetic CMF/N6 probe for a deployed Compute Sandbox Mock.

Mirrors the pruned_sandbox contract: M-BIND on the management plane (28501),
U-MEDIA/U-RECOGNITION/control/audio actions on the N6 user plane (28502), the
standalone ASR helper (9004), then media teardown and unbind.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import numpy as np
from aiohttp import ClientSession, FormData
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame

# Importing the server package registers the H.264 High profiles process-wide,
# exactly like a real terminal presenting High-profile video to the Sandbox.
from services.sandbox.api import canonical_digest
from services.video.rtc_server import register_h264_high_profiles  # noqa: F401


BINDING_REF = "binding-smoke-001"
SESSION_ID = "css-smoke-001"
INSTANCE_ID = "ci-smoke-001"
OWNER_REF = "ca/css-smoke-001"
ACTIVATION_KEY = "activate-smoke-001"
UNBIND_KEY = "unbind-smoke-001"

CONFIGURATION = {
    "compute_service_session_id": SESSION_ID,
    "connection_parameters": {
        "transport": "WEBRTC",
        "media_connections_path": "/v1/media-connections",
        "recognition_target_path_template": (
            "/v1/recognition-targets/{compute_service_session_id}"
        ),
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

CONSUMER_CONTEXT = {
    "compute_service_session_id": SESSION_ID,
    "compute_instance_id": INSTANCE_ID,
    "binding_ref": BINDING_REF,
    "role": "consumer",
    "agent_id": "agent-a",
}
PRODUCER_CONTEXT = {
    "compute_service_session_id": SESSION_ID,
    "compute_instance_id": INSTANCE_ID,
    "binding_ref": BINDING_REF,
    "role": "producer",
    "agent_id": "agent-b",
}


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


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


async def checked(response, *, expected: int | tuple[int, ...] = 200) -> dict | None:
    if isinstance(expected, int):
        expected = (expected,)
    body_text = await response.text()
    if response.status not in expected:
        raise RuntimeError(
            f"{response.request_info.method} {response.url.path} "
            f"returned HTTP {response.status}: {body_text}"
        )
    if not body_text:
        return None
    return json.loads(body_text)


async def post_media_connection(
    http: ClientSession,
    base_url: str,
    body: dict[str, object],
) -> dict[str, object]:
    response = await http.post(f"{base_url}/v1/media-connections", json=body)
    result = await checked(response, expected=201)
    if result.get("request_id") != body["request_id"]:
        raise RuntimeError("Sandbox did not echo request_id")
    if result.get("computing_context") != body["computing_context"]:
        raise RuntimeError("Sandbox did not echo computing_context")
    return result


async def create_media_connection(
    http: ClientSession,
    base_url: str,
    pc: RTCPeerConnection,
    *,
    request_id: str,
    context: dict[str, str],
    timeout_seconds: float,
) -> tuple[dict[str, object], dict[str, object]]:
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await wait_ice(pc, timeout_seconds)
    body = {
        "request_id": request_id,
        "computing_context": context,
        "offer": {
            "type": "offer",
            "sdp": pc.localDescription.sdp,
        },
    }
    result = await post_media_connection(http, base_url, body)
    answer = result.get("answer")
    if not isinstance(answer, dict) or answer.get("type") != "answer":
        raise RuntimeError("Sandbox response does not contain an SDP Answer")
    await pc.setRemoteDescription(RTCSessionDescription(**answer))
    return result, body


async def wait_for_action(
    http: ClientSession,
    base_url: str,
    action_id: str,
    timeout_seconds: float,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        response = await http.get(f"{base_url}/v1/control-actions/{action_id}")
        action = await checked(response)
        if action["status"] not in {"ACCEPTED", "RUNNING"}:
            return action
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"control action {action_id} did not finish: {action}")
        await asyncio.sleep(0.2)


async def run(
    management_url: str,
    base_url: str,
    asr_url: str,
    management_token: str,
    media_timeout: float = 30.0,
) -> dict[str, object]:
    management_url = management_url.rstrip("/")
    base_url = base_url.rstrip("/")
    asr_url = asr_url.rstrip("/")
    headers = auth_headers(management_token)
    peers: list[RTCPeerConnection] = []
    connection_ids: list[str] = []
    bound = False
    async with ClientSession(headers=headers) as http:
        try:
            management_health = await checked(
                await http.get(f"{management_url}/healthz")
            )
            if management_health.get("service") != "sandbox-management":
                raise RuntimeError(f"unexpected management health: {management_health}")
            user_health = await checked(await http.get(f"{base_url}/healthz"))
            if user_health.get("service") != "orange-yolo-video-server":
                raise RuntimeError(f"unexpected user-plane health: {user_health}")
            asr_health = await checked(await http.get(f"{asr_url}/health"))
            if asr_health.get("service") != "asr" or not asr_health.get("ready"):
                raise RuntimeError(f"ASR is not ready: {asr_health}")

            # M-BIND on the CMF management plane.
            bind_response = await http.post(
                f"{management_url}/management/v1/compute-session-bindings:bind",
                json={
                    "activation_idempotency_key": ACTIVATION_KEY,
                    "owner_ref": OWNER_REF,
                    "binding_ref": BINDING_REF,
                    "compute_service_session_id": SESSION_ID,
                    "compute_instance_id": INSTANCE_ID,
                    "configuration": CONFIGURATION,
                    "configuration_digest": canonical_digest(CONFIGURATION),
                    "expected_binding_absent": True,
                },
            )
            binding = await checked(bind_response)
            if binding["state"] != "BOUND":
                raise RuntimeError(f"binding was not accepted: {binding}")
            bound = True
            replay = await checked(
                await http.get(
                    f"{management_url}/management/v1/compute-session-bindings/{BINDING_REF}",
                    params={"activation_idempotency_key": ACTIVATION_KEY},
                )
            )
            if replay != binding:
                raise RuntimeError("binding GET did not return the accepted binding")

            # Standalone ASR helper: fixed transcription plus public intent.
            asr_form = FormData()
            asr_form.add_field("session_id", SESSION_ID)
            asr_form.add_field("task_id", "asr-smoke-001")
            asr_form.add_field("source", "glasses")
            asr_form.add_field("language", "zh")
            asr_form.add_field(
                "file",
                b"RIFFmock-wave-bytes",
                filename="speech.wav",
                content_type="audio/wav",
            )
            transcript = await checked(
                await http.post(f"{asr_url}/api/v1/transcribe", data=asr_form)
            )
            if not transcript.get("text"):
                raise RuntimeError("ASR returned no transcription text")

            # Consumer connects first so the placeholder phase is observable.
            consumer_pc = RTCPeerConnection()
            peers.append(consumer_pc)
            track_ready = asyncio.get_running_loop().create_future()

            @consumer_pc.on("track")
            def on_track(track) -> None:
                if not track_ready.done():
                    track_ready.set_result(track)

            consumer_pc.addTransceiver("video", direction="recvonly")
            consumer_result, consumer_body = await create_media_connection(
                http,
                base_url,
                consumer_pc,
                request_id="media-consumer-smoke",
                context=CONSUMER_CONTEXT,
                timeout_seconds=media_timeout,
            )
            connection_ids.append(str(consumer_result["media_connection_id"]))
            remote_track = await asyncio.wait_for(track_ready, media_timeout)
            original_track_id = remote_track.id

            def purple_pixels(image: np.ndarray) -> int:
                blue, green, red = image[:, :, 0], image[:, :, 1], image[:, :, 2]
                return int(
                    np.count_nonzero((blue >= 150) & (red >= 150) & (green <= 100))
                )

            placeholder = await asyncio.wait_for(remote_track.recv(), media_timeout)
            placeholder_image = placeholder.to_ndarray(format="bgr24")
            if purple_pixels(placeholder_image) or int(placeholder_image.mean()) >= 100:
                raise RuntimeError(
                    "consumer did not receive a placeholder before producer startup"
                )

            # Producer media connection brings the processed frames.
            producer_pc = RTCPeerConnection()
            peers.append(producer_pc)
            producer_pc.addTrack(SyntheticVideoTrack())
            producer_result, _ = await create_media_connection(
                http,
                base_url,
                producer_pc,
                request_id="media-producer-smoke",
                context=PRODUCER_CONTEXT,
                timeout_seconds=media_timeout,
            )
            connection_ids.append(str(producer_result["media_connection_id"]))

            async def wait_for_processed_frame() -> tuple[VideoFrame, int]:
                while True:
                    candidate = await remote_track.recv()
                    marker_pixels = purple_pixels(
                        candidate.to_ndarray(format="bgr24")
                    )
                    if marker_pixels >= 100:
                        return candidate, marker_pixels

            frame, marker_pixels = await asyncio.wait_for(
                wait_for_processed_frame(), media_timeout
            )
            if remote_track.id != original_track_id:
                raise RuntimeError("processed source switch replaced the consumer track")

            # The same request_id and payload must replay the stored answer.
            replayed = await post_media_connection(http, base_url, consumer_body)
            if replayed["media_connection_id"] != consumer_result["media_connection_id"]:
                raise RuntimeError("media connection replay was not idempotent")
            conflict_body = {
                **consumer_body,
                "offer": {
                    "type": "offer",
                    "sdp": consumer_body["offer"]["sdp"] + "\r\n",
                },
            }
            conflict = await http.post(
                f"{base_url}/v1/media-connections", json=conflict_body
            )
            if conflict.status != 409:
                raise RuntimeError(
                    "reused request_id with different content returned "
                    f"HTTP {conflict.status}; expected 409"
                )

            # Management media-state observes both roles.
            media_state = {}
            deadline = asyncio.get_running_loop().time() + media_timeout
            while True:
                media_state = await checked(
                    await http.get(
                        f"{management_url}/management/v1/compute-session-bindings/"
                        f"{BINDING_REF}/media-state"
                    )
                )
                if media_state.get("producer_connected") and media_state.get(
                    "consumer_connected"
                ):
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(f"media-state did not connect: {media_state}")
                await asyncio.sleep(0.2)

            # U-RECOGNITION persistent target.
            recognition = await checked(
                await http.put(
                    f"{base_url}/v1/recognition-targets/{SESSION_ID}",
                    json={
                        "request_id": "recognition-smoke",
                        "computing_context": CONSUMER_CONTEXT,
                        "input": {
                            "type": "TEXT",
                            "text": "寻找红色玩偶",
                            "language": "zh",
                        },
                    },
                )
            )
            if recognition["target"] != {"label": "红色玩偶", "prompt": "red doll"}:
                raise RuntimeError(f"unexpected recognition target: {recognition}")
            fetched = await checked(
                await http.get(f"{base_url}/v1/recognition-targets/{SESSION_ID}")
            )
            if fetched != recognition:
                raise RuntimeError("recognition target GET did not return the applied target")

            # Text control action: search_object completes with fixed matches.
            control = await checked(
                await http.post(
                    f"{base_url}/v1/control-actions",
                    json={
                        "request_id": "control-smoke",
                        "computing_context": CONSUMER_CONTEXT,
                        "input": {"type": "TEXT", "text": "寻找杯子", "language": "zh"},
                    },
                ),
                expected=202,
            )
            if control["status"] != "ACCEPTED" or not control["control_triggered"]:
                raise RuntimeError(f"unexpected accepted action: {control}")
            if control["normalized_action"] != "search_object":
                raise RuntimeError(f"unexpected normalized action: {control}")
            completed = await wait_for_action(
                http, base_url, control["action_id"], media_timeout
            )
            if completed["status"] != "COMPLETED":
                raise RuntimeError(f"search action did not complete: {completed}")
            matches = completed.get("result", {}).get("matches") or []
            if not any(item.get("label") == "cup" for item in matches):
                raise RuntimeError(f"search action returned no cup match: {completed}")

            # Runtime audio control action through the sandbox ASR chain.
            audio_form = FormData()
            audio_form.add_field("request_id", "audio-control-smoke")
            audio_form.add_field("computing_context", json.dumps(CONSUMER_CONTEXT))
            audio_form.add_field("language", "zh")
            audio_form.add_field(
                "file",
                b"RIFFmock-wave-bytes",
                filename="command.wav",
                content_type="audio/wav",
            )
            audio_control = await checked(
                await http.post(f"{base_url}/v1/audio-control-actions", data=audio_form),
                expected=202,
            )
            if not audio_control.get("transcription", {}).get("text"):
                raise RuntimeError("audio control response has no transcription")
            if not audio_control.get("control_triggered"):
                raise RuntimeError(
                    f"fixed transcription did not trigger control: {audio_control}"
                )
            audio_final = await wait_for_action(
                http, base_url, audio_control["action_id"], media_timeout
            )

            # Media teardown stays idempotent.
            for connection_id in reversed(connection_ids):
                response = await http.delete(
                    f"{base_url}/v1/media-connections/{connection_id}"
                )
                if response.status != 204:
                    raise RuntimeError(
                        f"failed to delete media connection {connection_id}: "
                        f"HTTP {response.status}"
                    )
                repeat = await http.delete(
                    f"{base_url}/v1/media-connections/{connection_id}"
                )
                if repeat.status != 204:
                    raise RuntimeError(
                        f"repeated delete of {connection_id} returned HTTP {repeat.status}"
                    )
            connection_ids.clear()

            return {
                "ok": True,
                "compute_service_session_id": SESSION_ID,
                "binding_ref": BINDING_REF,
                "producer_media_connection_id": producer_result["media_connection_id"],
                "consumer_media_connection_id": consumer_result["media_connection_id"],
                "placeholder_frame": f"{placeholder.width}x{placeholder.height}",
                "processed_frame": f"{frame.width}x{frame.height}",
                "processed_marker_pixels": marker_pixels,
                "consumer_track_reused": True,
                "management_media_state": {
                    "producer_connected": media_state["producer_connected"],
                    "consumer_connected": media_state["consumer_connected"],
                },
                "recognition_target_revision": recognition["target_revision"],
                "text_control_action": completed["status"],
                "text_control_matches": matches,
                "asr_transcription": transcript["text"],
                "asr_intent": transcript.get("intent"),
                "audio_transcription": audio_control["transcription"]["text"],
                "audio_intent": audio_control.get("intent"),
                "audio_final_status": audio_final["status"],
                "audio_final_cause": audio_final.get("cause", ""),
            }
        finally:
            try:
                for connection_id in reversed(connection_ids):
                    await http.delete(f"{base_url}/v1/media-connections/{connection_id}")
            finally:
                if bound:
                    unbind_response = await http.post(
                        f"{management_url}/management/v1/compute-session-bindings/"
                        f"{BINDING_REF}:unbind",
                        json={
                            "owner_ref": OWNER_REF,
                            "unbind_idempotency_key": UNBIND_KEY,
                            "cause": "smoke-complete",
                        },
                    )
                    unbound = await unbind_response.json(content_type=None)
                    if unbind_response.status != 200 or unbound.get("state") != "UNBOUND":
                        raise RuntimeError(f"unbind failed: {unbound}")
                    # The user plane must reject the released binding.
                    rejected = await http.post(
                        f"{base_url}/v1/media-connections",
                        json={
                            "request_id": "media-after-unbind",
                            "computing_context": CONSUMER_CONTEXT,
                            "offer": {"type": "offer", "sdp": "v=0"},
                        },
                    )
                    if rejected.status != 409:
                        raise RuntimeError(
                            "user plane accepted a released binding: "
                            f"HTTP {rejected.status}"
                        )
                await asyncio.gather(
                    *(pc.close() for pc in peers), return_exceptions=True
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--management-url", default="http://172.30.0.10:28501")
    parser.add_argument("--base-url", default="http://172.30.0.10:28502")
    parser.add_argument("--asr-url", default="http://172.30.0.10:9004")
    parser.add_argument(
        "--management-token",
        default=os.getenv("FREE6GC_COMPUTING_SANDBOX_MANAGEMENT_TOKEN", ""),
    )
    parser.add_argument("--media-timeout", type=float, default=30.0)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run(
                    args.management_url,
                    args.base_url,
                    args.asr_url,
                    args.management_token,
                    args.media_timeout,
                )
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
