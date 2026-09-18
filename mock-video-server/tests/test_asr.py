from __future__ import annotations

import os
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from services.asr.config import AsrSettings
from services.asr.main import _json_response, _public_intent
from services.asr.service import SpeechRecognizer


class MockSpeechRecognizerTest(IsolatedAsyncioTestCase):
    async def test_json_response_keeps_chinese_readable(self) -> None:
        response = _json_response({"text": "派机器狗巡逻园区内A区域。"})
        self.assertIn('"text": "派机器狗巡逻园区内A区域。"', response.text)
        self.assertNotIn("\\u", response.text)

    async def test_public_patrol_intent_uses_business_slots(self) -> None:
        self.assertEqual(
            {
                "executor": "robot dog",
                "intent": "security patrol",
                "area": "A",
                "matched": True,
                "backend": "rules",
            },
            _public_intent("patrol", "A区域", "rules"),
        )

    async def test_transcribe_returns_fixed_payload(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ASR_MODEL": "mock-fixed-transcription",
                "ASR_LANGUAGE": "zh",
                "MOCK_AUDIO_TRANSCRIPTION_TEXT": "向左转",
            },
            clear=True,
        ):
            settings = AsrSettings.from_env()
        recognizer = SpeechRecognizer(settings)
        with self.assertLogs("sandbox.asr", level="INFO") as captured:
            with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
                result = await recognizer.transcribe(
                    Path(audio.name),
                    language=None,
                    session_id="session-1",
                    task_id="task-1",
                    source="test",
                    stop_reason=None,
                    original_filename="speech.wav",
                )

        self.assertEqual("向左转", result["text"])
        self.assertEqual(1000, result["durationMs"])
        self.assertEqual("speech.wav", result["audioFilename"])
        self.assertEqual("zh", result["language"])
        self.assertEqual([{"startSec": 0.0, "endSec": 1.0, "text": "向左转"}], result["segments"])
        health = recognizer.health()
        self.assertTrue(health["ready"])
        self.assertEqual("ready", health["status"])
        self.assertEqual("mock-fixed-transcription", health["modelName"])
        self.assertEqual(result, health["latestTranscript"])
        self.assertIn("session_id=session-1", captured.output[0])
        self.assertIn('text="向左转"', captured.output[0])

    async def test_transcription_text_follows_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = AsrSettings.from_env()
        recognizer = SpeechRecognizer(settings)
        self.assertEqual("向左转", recognizer.transcription_text())
        with patch.dict(
            os.environ, {"MOCK_AUDIO_TRANSCRIPTION_TEXT": "向前"}, clear=False
        ):
            self.assertEqual("向前", recognizer.transcription_text())

    async def test_disabled_recognizer_reports_and_raises(self) -> None:
        with patch.dict(os.environ, {"ASR_ENABLED": "false"}, clear=True):
            settings = AsrSettings.from_env()
        recognizer = SpeechRecognizer(settings)

        with self.assertRaises(RuntimeError):
            await recognizer.transcribe(
                Path("unused.wav"),
                language=None,
                session_id="session-1",
                task_id="task-1",
                source="test",
                stop_reason=None,
                original_filename="speech.wav",
            )

        health = recognizer.health()
        self.assertFalse(health["ready"])
        self.assertEqual("disabled", health["status"])
