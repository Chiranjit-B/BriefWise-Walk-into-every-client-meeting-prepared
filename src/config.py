"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _configuration_values() -> dict[str, str]:
    """Read the project .env afresh, with explicit process variables taking precedence."""
    file_values = {
        key: str(value)
        for key, value in dotenv_values(PROJECT_ROOT / ".env").items()
        if value is not None
    }
    return {**file_values, **os.environ}


def _positive_int(name: str, default: int, values: dict[str, str]) -> int:
    raw = values.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    gemini_timeout_seconds: int
    gemini_max_attempts: int
    gemini_max_calls: int
    gemini_requests_per_minute: int
    gemini_fetcher_max_output_tokens: int
    gemini_analyzer_max_output_tokens: int

    @classmethod
    def from_env(cls) -> "Settings":
        values = _configuration_values()
        return cls(
            gemini_api_key=values.get("GEMINI_API_KEY", "").strip(),
            gemini_model=values.get("GEMINI_MODEL", "gemini-3.8-flash").strip(),
            gemini_timeout_seconds=max(5, min(int(values.get("GEMINI_TIMEOUT_SECONDS", "30")), 120)),
            gemini_max_attempts=max(1, min(int(values.get("GEMINI_MAX_ATTEMPTS", "2")), 3)),
            gemini_max_calls=max(2, min(int(values.get("GEMINI_MAX_CALLS", "8")), 12)),
            gemini_requests_per_minute=_positive_int("GEMINI_REQUESTS_PER_MINUTE", 4, values),
            gemini_fetcher_max_output_tokens=_positive_int("GEMINI_FETCHER_MAX_OUTPUT_TOKENS", 512, values),
            gemini_analyzer_max_output_tokens=_positive_int("GEMINI_ANALYZER_MAX_OUTPUT_TOKENS", 5000, values),
        )

