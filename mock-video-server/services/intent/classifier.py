from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading
from typing import Any

from .config import IntentSettings


DEFAULT_INTENTS = {"patrol", "find_object", "movement", "grab", "other"}
MOVEMENT_COMMANDS = {
    "向前": "forward",
    "向前走": "forward",
    "请向前走": "forward",
    "往前走": "forward",
    "前进": "forward",
    "move forward": "forward",
    "forward": "forward",
    "退后": "backward",
    "向后": "backward",
    "向后走": "backward",
    "请向后走": "backward",
    "往后退": "backward",
    "后退": "backward",
    "move back": "backward",
    "back": "backward",
    "backward": "backward",
    "向左": "left",
    "左转": "left",
    "向左转": "left",
    "turn left": "left",
    "left": "left",
    "向右": "right",
    "右转": "right",
    "向右转": "right",
    "turn right": "right",
    "right": "right",
    "挥手": "wave",
    "打招呼": "wave",
    "你好": "wave",
    "hello": "wave",
    "wave": "wave",
}
FIND_PREFIXES = (
    "请帮我寻找",
    "请帮我找",
    "帮我寻找",
    "帮我找",
    "帮我识别",
    "请识别",
    "寻找",
    "识别",
    "找",
    "look for",
    "identify",
    "locate",
    "find the",
    "find",
)
GRAB_PREFIXES = (
    "请帮我抓取",
    "帮我抓取",
    "请抓取",
    "抓取",
    "请帮我夹取",
    "帮我夹取",
    "夹取",
    "请帮我拿起",
    "帮我拿起",
    "请帮我拿",
    "帮我拿",
    "拿起",
    "请抓",
    "帮我抓",
    "抓",
    "please pick up",
    "please grasp",
    "please grab",
    "pick up",
    "grasp",
    "grab",
)
ZH_TO_EN = {
    "黄色": "yellow",
    "黄": "yellow",
    "红色": "red",
    "红": "red",
    "蓝色": "blue",
    "蓝": "blue",
    "绿色": "green",
    "绿": "green",
    "黑色": "black",
    "黑": "black",
    "白色": "white",
    "白": "white",
    "玩具熊": "toy bear",
    "小熊": "bear",
    "熊": "bear",
    "狗": "dog",
    "猫": "cat",
    "杯子": "cup",
    "瓶子": "bottle",
    "可乐": "cola",
    "娃娃": "doll",
    "玩偶": "doll",
    "盲盒": "mystery box",
    "拉布布": "labubu",
}


@dataclass(frozen=True, slots=True)
class IntentResult:
    intent: str
    argument: str
    confidence: float
    backend: str
    argument_i18n: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "success",
            "executor": "robot dog" if self.intent != "other" else None,
            "intent": self.intent,
            "scene": self.intent,
            "argument": self.argument,
            "normalized_argument": self.argument,
            "confidence": round(self.confidence, 4),
            "backend": self.backend,
        }
        if self.argument_i18n is not None:
            payload["normalized_argument_i18n"] = self.argument_i18n
        return payload


class RuleIntentClassifier:
    name = "rules"

    def classify(self, text: str) -> IntentResult:
        normalized = " ".join(str(text or "").strip().split())
        if not normalized:
            return IntentResult("other", "", 1.0, self.name)
        movement = self._movement(normalized)
        if movement:
            return IntentResult("movement", movement, 1.0, self.name)
        patrol = self._patrol(normalized)
        if patrol is not None:
            return IntentResult("patrol", patrol, 1.0, self.name)
        grab = self._argument_after_prefix(normalized, GRAB_PREFIXES)
        if grab is not None:
            argument, i18n = self._normalize_object(grab)
            return IntentResult("grab", argument, 0.98, self.name, i18n)
        found = self._argument_after_prefix(normalized, FIND_PREFIXES)
        if found is not None and found:
            argument, i18n = self._normalize_object(found)
            return IntentResult("find_object", argument, 0.98, self.name, i18n)
        return IntentResult("other", "", 0.8, self.name)

    @staticmethod
    def _movement(text: str) -> str:
        cleaned = re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", " ", text.lower())
        cleaned = " ".join(cleaned.split())
        if cleaned.startswith("please "):
            cleaned = cleaned[7:]
        return MOVEMENT_COMMANDS.get(cleaned, "")

    @staticmethod
    def _patrol(text: str) -> str | None:
        if "巡逻" not in text:
            return None
        match = re.search(r"([A-Za-z0-9一二三四五六七八九十]+区域)", text)
        return match.group(1) if match else ""

    @staticmethod
    def _argument_after_prefix(text: str, prefixes: tuple[str, ...]) -> str | None:
        lowered = text.lower()
        for prefix in prefixes:
            lowered_prefix = prefix.lower()
            if lowered == lowered_prefix:
                return ""
            if not lowered.startswith(lowered_prefix):
                continue
            remainder = text[len(prefix) :]
            if any("\u4e00" <= char <= "\u9fff" for char in prefix) or remainder.startswith(" "):
                return remainder.strip(" ，。！？,.!?")
        return None

    @staticmethod
    def _normalize_object(text: str) -> tuple[str, dict[str, str] | None]:
        original = text.strip(" ，。！？,.!?")
        if not original:
            return "", {"zh": "", "en": ""}
        if not any("\u4e00" <= char <= "\u9fff" for char in original):
            english = re.sub(r"^(the|a|an)\s+", "", " ".join(original.lower().split()))
            return english, {"zh": "", "en": english}
        compact = original.replace("一个", "").replace("一只", "").replace("的", "").replace(" ", "")
        translated = compact
        for source, target in sorted(ZH_TO_EN.items(), key=lambda item: len(item[0]), reverse=True):
            translated = translated.replace(source, f" {target} ")
        english = " ".join(translated.split()).strip().lower()
        if any("\u4e00" <= char <= "\u9fff" for char in english):
            english = original
        return english, {"zh": original, "en": english}


class IntentService:
    """规则意图分类；Mock 镜像不包含 Qwen 模型，行为等同 pruned_sandbox 的规则回退。"""

    def __init__(self, settings: IntentSettings) -> None:
        self.settings = settings
        self.rules = RuleIntentClassifier()
        self.last_backend = "rules"
        self.last_error: str | None = None

    async def classify(self, text: str) -> dict[str, Any]:
        normalized = " ".join(str(text or "").strip().split())
        rule_result = self.rules.classify(normalized)
        if rule_result.intent not in self.settings.candidates:
            rule_result = IntentResult("other", "", 1.0, self.rules.name)
        self.last_backend = rule_result.backend
        self.last_error = None
        return rule_result.to_dict()

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": "intent",
            "backend": self.settings.backend,
            "lastBackend": self.last_backend,
            "model": self.settings.model if self.settings.backend != "rules" else None,
            "lastError": self.last_error,
            "intents": list(self.settings.candidates),
        }
