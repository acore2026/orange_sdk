from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any
import os
import uuid

from .config import AsrSettings


LOGGER = logging.getLogger("sandbox.asr")

DEFAULT_MOCK_TRANSCRIPTION_TEXT = "向左转"


class SpeechRecognizer:
    """固定结果转写：保持 pruned_sandbox SpeechRecognizer 接口，但不加载模型。

    每次转写都返回 `MOCK_AUDIO_TRANSCRIPTION_TEXT` 配置的固定文本（默认
    “向左转”），响应字段与 faster-whisper 版本完全一致。
    """

    def __init__(
        self,
        settings: AsrSettings,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self._model_factory = model_factory
        self._load_lock = threading.Lock()
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        self.loaded = True
        self.last_error: str | None = None
        self.latest_transcript: dict[str, Any] | None = None

    def transcription_text(self) -> str:
        raw = os.getenv("MOCK_AUDIO_TRANSCRIPTION_TEXT", DEFAULT_MOCK_TRANSCRIPTION_TEXT)
        normalized = str(raw or "").strip()
        return normalized or DEFAULT_MOCK_TRANSCRIPTION_TEXT

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        session_id: str,
        task_id: str,
        source: str,
        stop_reason: str | None,
        original_filename: str,
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            raise RuntimeError("ASR service is disabled")
        async with self._semaphore:
            return await asyncio.to_thread(
                self._transcribe_sync,
                audio_path,
                language=language,
                session_id=session_id,
                task_id=task_id,
                source=source,
                stop_reason=stop_reason,
                original_filename=original_filename,
            )

    def _transcribe_sync(
        self,
        audio_path: Path,
        *,
        language: str | None,
        session_id: str,
        task_id: str,
        source: str,
        stop_reason: str | None,
        original_filename: str,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        created_at_ms = int(time.time() * 1000)
        text = self.transcription_text()
        segments = [{"startSec": 0.0, "endSec": 1.0, "text": text}]
        self.last_error = None
        result = {
            "transcriptId": uuid.uuid4().hex,
            "sessionId": session_id,
            "taskId": task_id,
            "source": source,
            "text": text,
            "language": language or self.settings.language,
            "languageProbability": 1.0,
            "durationMs": 1000,
            "processingMs": int((time.perf_counter() - started) * 1000),
            "createdAtMs": created_at_ms,
            "stopReason": stop_reason,
            "segments": segments,
            "audioFilename": original_filename,
        }
        self.latest_transcript = result
        LOGGER.info(
            "transcription completed session_id=%s task_id=%s source=%s "
            "language=%s duration_ms=%s processing_ms=%s text=%s",
            session_id,
            task_id,
            source,
            result["language"],
            result["durationMs"],
            result["processingMs"],
            json.dumps(text, ensure_ascii=False),
        )
        return result

    def health(self) -> dict[str, Any]:
        ready = self.settings.enabled and self.last_error is None
        if not self.settings.enabled:
            status = "disabled"
        elif self.last_error is not None:
            status = "error"
        else:
            status = "ready"
        return {
            "ok": ready,
            "ready": ready,
            "status": status,
            "service": "asr",
            "enabled": self.settings.enabled,
            "loaded": self.loaded,
            "model": self.settings.model,
            "modelName": self.settings.model,
            "device": self.settings.device,
            "computeType": self.settings.compute_type,
            "lastError": self.last_error,
            "latestTranscript": self.latest_transcript,
        }
