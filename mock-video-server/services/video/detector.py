from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

from .config import VideoSettings


class YoloDetector:
    """固定结果检测器：保持 pruned_sandbox YoloDetector 接口，但不加载模型。

    每一帧都输出与正式 Mock 一致的左上角紫色块和移动绿条（用于证明视频经过
    服务端处理），并为当前激活的每个类别返回一个固定置信度的检测结果。
    """

    def __init__(self, settings: VideoSettings, model_factory: Any | None = None) -> None:
        self.settings = settings
        self._model_factory = model_factory
        self._inference_lock = threading.Lock()
        self._requested_classes = list(settings.yolo_classes)
        self._frame_number = 0
        self.loaded = True
        self.last_error: str | None = None
        self.last_inference_ms = 0
        self.last_detections: list[dict[str, Any]] = []

    def set_classes(self, classes: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in classes if item.strip()))
        self._requested_classes = normalized
        return list(self._requested_classes)

    def process(
        self,
        image: np.ndarray,
        classes: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[np.ndarray, list[dict[str, Any]]]:
        if not self.settings.yolo_enabled:
            return image, []
        started = time.perf_counter()
        with self._inference_lock:
            requested_classes = (
                list(classes) if classes is not None else list(self._requested_classes)
            )
            requested_classes = list(
                dict.fromkeys(str(item).strip() for item in requested_classes if str(item).strip())
            )
            annotated = self._annotate(image, self._frame_number)
            self._frame_number += 1
            detections = self._fixed_detections(image, requested_classes)
            self.last_inference_ms = int((time.perf_counter() - started) * 1000)
            self.last_detections = detections
            self.last_error = None
            return annotated, detections

    @staticmethod
    def _annotate(image: np.ndarray, frame_number: int) -> np.ndarray:
        """与既有 Mock 一致的处理标记：紫色块加移动绿条。"""
        annotated = image.copy()
        height, width = annotated.shape[:2]
        marker_height = min(28, height)
        marker_width = min(96, width)
        annotated[:marker_height, :marker_width] = (210, 30, 210)
        bar_width = min(marker_width, 4 + frame_number % max(marker_width, 1))
        annotated[max(marker_height - 5, 0):marker_height, :bar_width] = (30, 230, 30)
        return annotated

    @staticmethod
    def _fixed_detections(
        image: np.ndarray,
        requested_classes: list[str],
    ) -> list[dict[str, Any]]:
        height, width = image.shape[:2]
        box = [
            round(width * 0.25, 2),
            round(height * 0.25, 2),
            round(width * 0.6, 2),
            round(height * 0.6, 2),
        ]
        return [
            {
                "classId": index,
                "label": label,
                "confidence": 0.99,
                "box": list(box),
            }
            for index, label in enumerate(requested_classes)
        ]

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.yolo_enabled,
            "loaded": self.loaded,
            "model": "mock-fixed-detector",
            "device": "cpu",
            "classes": list(self._requested_classes),
            "lastInferenceMs": self.last_inference_ms,
            "lastDetectionCount": len(self.last_detections),
            "lastError": self.last_error,
        }
