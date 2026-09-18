from __future__ import annotations

from dataclasses import dataclass
import os


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class IntentSettings:
    host: str
    port: int
    cors_origin: str
    backend: str
    model: str
    device: str
    max_new_tokens: int
    candidates: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "IntentSettings":
        return cls(
            host=os.getenv("INTENT_HOST", os.getenv("SEMANTIC_ROUTE_HOST", "127.0.0.1")),
            port=_int("INTENT_PORT", _legacy_port()),
            cors_origin=os.getenv("CORS_ORIGIN", "*"),
            # Mock 不带 Qwen 权重；即使部署配置了 hybrid/qwen 也只执行规则分类。
            backend="rules",
            model="",
            device=os.getenv("INTENT_DEVICE", "cpu").strip(),
            max_new_tokens=_int("INTENT_MAX_NEW_TOKENS", 128),
            candidates=_candidates(),
        )


def _legacy_port() -> int:
    try:
        return int(os.getenv("SEMANTIC_ROUTE_PORT", "8011"))
    except ValueError:
        return 8011


def _candidates() -> tuple[str, ...]:
    raw = os.getenv(
        "INTENT_CANDIDATES",
        "patrol,movement,find_object,grab,other",
    )
    values = tuple(dict.fromkeys(item.strip().lower() for item in raw.split(",") if item.strip()))
    return values if "other" in values else (*values, "other")
