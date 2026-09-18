from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from services.asr.config import AsrSettings
from services.asr.main import create_app as create_asr_app
from services.sandbox.api import canonical_digest
from services.sandbox.main import create_app as create_sandbox_app
from services.video.config import VideoSettings


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
                "agent_id": "glasses",
                "up_session_id": "ups-glasses",
                "pdu_session_id": 1,
                "ue_ipv4_address": "10.60.0.11",
                "dnn": "internet",
                "snssai": "1-010203",
                "generation": "1",
            },
            {
                "role": "producer",
                "agent_id": "dog",
                "up_session_id": "ups-dog",
                "pdu_session_id": 1,
                "ue_ipv4_address": "10.60.0.12",
                "dnn": "internet",
                "snssai": "1-010203",
                "generation": "1",
            },
        ],
    }


class AsrToSandboxE2ETest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ASR_ENABLED": "true",
                "MOCK_AUDIO_TRANSCRIPTION_TEXT": "向左",
                "ASR_INTENT_URL": "",
                "SANDBOX_INTENT_URL": "",
                "WEBRTC_ICE_SERVERS": "",
            },
            clear=True,
        ):
            asr_settings = AsrSettings.from_env()
            video_settings = VideoSettings.from_env()
        self.asr = TestClient(TestServer(create_asr_app(asr_settings)))
        await self.asr.start_server()
        video_settings = replace(
            video_settings,
            asr_url=str(self.asr.make_url("/api/v1/transcribe")),
        )
        self.sandbox = TestClient(TestServer(create_sandbox_app(video_settings)))
        await self.sandbox.start_server()

    async def asyncTearDown(self) -> None:
        await self.asr.close()
        await self.sandbox.close()

    async def _bind(self, session_id: str) -> None:
        binding_ref = f"binding-{session_id}"
        configuration = _configuration(session_id)
        response = await self.sandbox.post(
            "/management/v1/compute-session-bindings:bind",
            json={
                "activation_idempotency_key": f"activate-{session_id}",
                "owner_ref": f"ca/{session_id}",
                "binding_ref": binding_ref,
                "compute_service_session_id": session_id,
                "compute_instance_id": f"ci-{session_id}",
                "configuration": configuration,
                "configuration_digest": canonical_digest(configuration),
                "expected_binding_absent": True,
            },
        )
        self.assertEqual(200, response.status, await response.text())

    async def test_sandbox_accepts_audio_and_creates_text_action(self) -> None:
        await self._bind("css-voice-1")
        context = {
            "compute_service_session_id": "css-voice-1",
            "compute_instance_id": "ci-css-voice-1",
            "binding_ref": "binding-css-voice-1",
            "role": "consumer",
            "agent_id": "glasses",
        }
        upload = FormData()
        upload.add_field("request_id", "audio-action-1")
        upload.add_field("computing_context", json.dumps(context))
        upload.add_field("language", "zh")
        upload.add_field(
            "file",
            b"mock-wave-bytes",
            filename="voice.wav",
            content_type="audio/wav",
        )
        with patch.dict(
            os.environ,
            {"MOCK_AUDIO_TRANSCRIPTION_TEXT": "向左"},
            clear=False,
        ):
            response = await self.sandbox.post("/v1/audio-control-actions", data=upload)
        action = await response.json()
        self.assertEqual(202, response.status, action)
        self.assertEqual("向左", action["transcription"]["text"])
        self.assertEqual("ACCEPTED", action["status"])
        self.assertTrue(action["control_triggered"])
        self.assertEqual(
            {
                "executor": "robot dog",
                "intent": "movement",
                "direction": "left",
                "matched": True,
                "backend": "rules",
            },
            action["intent"],
        )
        self.assertEqual("movement", action["normalized_action"])
        self.assertEqual({"direction": "left"}, action["normalized_parameters"])

        queried = await self._wait_for_completion(action["action_id"])
        self.assertEqual("向左", queried["transcription"]["text"])
        self.assertTrue(queried["control_triggered"])
        self.assertEqual("movement", queried["normalized_action"])
        self.assertEqual({"direction": "left"}, queried["normalized_parameters"])
        # 未配置机器狗业务端点时与 pruned_sandbox 一致：FAILED 且不伪装已执行。
        self.assertEqual("FAILED", queried["status"])
        self.assertEqual("producer-control-endpoint-unconfigured", queried["cause"])

        health = await (await self.asr.get("/health")).json()
        self.assertTrue(health["ready"])
        self.assertEqual("向左", health["latestTranscript"]["text"])

    async def test_non_action_audio_still_returns_transcription(self) -> None:
        with patch.dict(
            os.environ,
            {"MOCK_AUDIO_TRANSCRIPTION_TEXT": "今天天气不错"},
            clear=False,
        ):
            await self._bind("css-voice-2")
            context = {
                "compute_service_session_id": "css-voice-2",
                "compute_instance_id": "ci-css-voice-2",
                "binding_ref": "binding-css-voice-2",
                "role": "consumer",
                "agent_id": "glasses",
            }
            upload = FormData()
            upload.add_field("request_id", "audio-non-action-1")
            upload.add_field("computing_context", json.dumps(context))
            upload.add_field("language", "zh")
            upload.add_field(
                "file",
                b"mock-wave-bytes",
                filename="voice.wav",
                content_type="audio/wav",
            )
            response = await self.sandbox.post("/v1/audio-control-actions", data=upload)
        body = await response.json()
        self.assertEqual(202, response.status, body)
        self.assertEqual("今天天气不错", body["transcription"]["text"])
        self.assertFalse(body["control_triggered"])
        self.assertEqual("COMPLETED", body["status"])
        self.assertEqual("no-control-action", body["cause"])
        self.assertEqual(
            {
                "executor": None,
                "intent": "other",
                "matched": False,
                "backend": "rules",
            },
            body["intent"],
        )

    async def _wait_for_completion(self, action_id: str) -> dict:
        for _ in range(100):
            response = await self.sandbox.get(f"/v1/control-actions/{action_id}")
            body = await response.json()
            if body["status"] not in {"ACCEPTED", "RUNNING"}:
                return body
            await asyncio.sleep(0.01)
        self.fail("audio action did not complete")
