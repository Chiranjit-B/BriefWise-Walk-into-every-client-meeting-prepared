"""Bounded, rate-limited access to the official Google Gen AI Python SDK."""

from __future__ import annotations

import random
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from src.config import Settings

T = TypeVar("T", bound=BaseModel)
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
MAX_RETRY_DELAY_SECONDS = 300.0


class GeminiClientError(RuntimeError):
    """Safe error suitable for logs and CLI output."""


class RequestRateLimiter:
    """Evenly space requests made by one process, including retry attempts."""

    def __init__(self, requests_per_minute: int, logger=None, *, clock=None, sleeper=None):
        if not isinstance(requests_per_minute, int) or requests_per_minute <= 0:
            raise ValueError("GEMINI_REQUESTS_PER_MINUTE must be a positive integer.")
        self.requests_per_minute = requests_per_minute
        self.interval_seconds = 60.0 / requests_per_minute
        self.logger = logger
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._next_request_at: float | None = None
        self._lock = threading.Lock()

    def wait(self, *, operation: str, attempt: int, logger=None) -> float:
        """Wait for the next slot; the first request starts immediately."""
        with self._lock:
            now = self._clock()
            wait_seconds = 0.0 if self._next_request_at is None else max(0.0, self._next_request_at - now)
            active_logger = logger or self.logger
            if active_logger is None:
                raise ValueError("A logger is required for request pacing.")
            active_logger.log(
                "request_pacing",
                operation=operation,
                attempt=attempt,
                requests_per_minute=self.requests_per_minute,
                interval_seconds=round(self.interval_seconds, 3),
                wait_seconds=round(wait_seconds, 3),
            )
            if wait_seconds > 0:
                self._sleep(wait_seconds)
            self._next_request_at = self._clock() + self.interval_seconds
            return wait_seconds


class GeminiClient:
    _shared_limiter_lock = threading.Lock()
    _shared_rate_limiter: RequestRateLimiter | None = None
    _shared_rate: int | None = None

    def __init__(self, settings: Settings, logger):
        if not settings.gemini_api_key:
            raise GeminiClientError("Missing GEMINI_API_KEY in .env.")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise GeminiClientError("google-genai is not installed. Run pip install -r requirements.txt.") from exc
        self.types = types
        self.settings = settings
        self.logger = logger
        self.calls_used = 0
        self._calls_lock = threading.Lock()
        self._sleep = time.sleep
        self._random_uniform = random.uniform
        # All GeminiClient instances in this Python process share one pacing
        # timeline. A configured rate change intentionally starts a new timeline.
        with self._shared_limiter_lock:
            if self._shared_rate_limiter is None or self._shared_rate != settings.gemini_requests_per_minute:
                type(self)._shared_rate_limiter = RequestRateLimiter(settings.gemini_requests_per_minute)
                type(self)._shared_rate = settings.gemini_requests_per_minute
            self.rate_limiter = self._shared_rate_limiter
        try:
            self.client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=settings.gemini_timeout_seconds * 1000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        except Exception as exc:
            raise GeminiClientError("Could not initialize the Gemini client.") from exc

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        try:
            return int(status) if status is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _classification_text(exc: Exception) -> str:
        """Use provider text only for classification; never return or log it."""
        parts = [type(exc).__name__, str(exc)]
        details = getattr(exc, "details", None)
        if details is not None:
            parts.append(str(details))
        return " ".join(parts).lower()

    def _is_daily_quota_exhausted(self, exc: Exception) -> bool:
        text = self._classification_text(exc)
        if self._status_code(exc) != 429 and "resourceexhausted" not in re.sub(r"[^a-z]", "", text):
            return False
        compact = re.sub(r"[^a-z0-9]", "", text)
        return any(marker in text for marker in ("per day", "per_day", "daily quota")) or any(
            marker in compact for marker in ("requestsperday", "generatedrequestsperday", "perdayperproject")
        )

    def _safe_error(self, exc: Exception) -> str:
        status = self._status_code(exc)
        name = type(exc).__name__.lower()
        if self._is_daily_quota_exhausted(exc):
            return "Gemini daily quota is exhausted; retry after the provider's daily reset."
        if status in (401, 403) or "auth" in name:
            return "Gemini authentication or permission failed. Check GEMINI_API_KEY."
        if status == 429 or "quota" in name or "resourceexhausted" in name:
            return "Gemini rate limit or quota was exceeded after bounded retries."
        if status in (408, 500, 502, 503, 504) or "timeout" in name:
            return "Gemini had a transient timeout or service failure after bounded retries."
        return "Gemini request failed."

    def _is_retryable(self, exc: Exception) -> bool:
        if self._is_daily_quota_exhausted(exc):
            return False
        status = self._status_code(exc)
        name = type(exc).__name__.lower()
        return status in RETRYABLE_STATUS_CODES or "timeout" in name or "resourceexhausted" in name

    @staticmethod
    def _parse_delay(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return max(0.0, float(value))
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        try:
            return max(0.0, float(stripped))
        except ValueError:
            pass
        duration = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)s", stripped, re.IGNORECASE)
        if duration:
            return float(duration.group(1))
        try:
            when = parsedate_to_datetime(stripped)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def _find_structured_retry_delay(cls, value: Any) -> float | None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = re.sub(r"[^a-z]", "", str(key).lower())
                if normalized in {"retryafter", "retrydelay"}:
                    parsed = cls._parse_delay(item)
                    if parsed is not None:
                        return parsed
                nested = cls._find_structured_retry_delay(item)
                if nested is not None:
                    return nested
        elif isinstance(value, (list, tuple)):
            for item in value:
                nested = cls._find_structured_retry_delay(item)
                if nested is not None:
                    return nested
        return None

    def _server_retry_delay(self, exc: Exception) -> float | None:
        header_sources = [getattr(exc, "headers", None), getattr(getattr(exc, "response", None), "headers", None)]
        for headers in header_sources:
            if headers:
                for key, value in headers.items():
                    if str(key).lower() == "retry-after":
                        parsed = self._parse_delay(value)
                        if parsed is not None:
                            return min(parsed, MAX_RETRY_DELAY_SECONDS)
        for details in (getattr(exc, "details", None), getattr(exc, "args", ())):
            parsed = self._find_structured_retry_delay(details)
            if parsed is not None:
                return min(parsed, MAX_RETRY_DELAY_SECONDS)
        return None

    def _retry_delay(self, exc: Exception, attempt: int) -> tuple[float, str]:
        server_delay = self._server_retry_delay(exc)
        if server_delay is not None:
            return server_delay, "server_guidance"
        exponential = min(2.0 ** (attempt - 1), 30.0)
        return min(exponential + self._random_uniform(0.0, 0.5), MAX_RETRY_DELAY_SECONDS), "exponential_jitter"

    @staticmethod
    def _finish_reasons(response: Any) -> list[str]:
        reasons = []
        for candidate in getattr(response, "candidates", None) or []:
            reason = getattr(candidate, "finish_reason", None)
            value = getattr(reason, "value", reason)
            if value is not None:
                reasons.append(str(value).upper())
        response_status = getattr(response, "status", None)
        status_value = getattr(response_status, "value", response_status)
        if status_value is not None:
            reasons.append(str(status_value).upper())
        return reasons

    def _is_truncated(self, response: Any) -> bool:
        return any(reason in {"MAX_TOKENS", "INCOMPLETE"} for reason in self._finish_reasons(response))

    def _reserve_call(self) -> None:
        with self._calls_lock:
            if self.calls_used >= self.settings.gemini_max_calls:
                raise GeminiClientError("Gemini call limit reached for this run.")
            self.calls_used += 1

    def generate(self, *, contents: Any, config: Any, operation: str):
        last_error: Exception | None = None
        effective_output_cap = getattr(config, "max_output_tokens", None)
        for attempt in range(1, self.settings.gemini_max_attempts + 1):
            self._reserve_call()
            self.rate_limiter.wait(operation=operation, attempt=attempt, logger=self.logger)
            started = time.perf_counter()
            try:
                response = self.client.models.generate_content(
                    model=self.settings.gemini_model,
                    contents=contents,
                    config=config,
                )
                usage = getattr(response, "usage_metadata", None)
                finish_reasons = self._finish_reasons(response)
                event = {
                    "operation": operation,
                    "attempt": attempt,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "input_tokens": getattr(usage, "prompt_token_count", None),
                    "output_tokens": getattr(usage, "candidates_token_count", None),
                    "thought_tokens": getattr(usage, "thoughts_token_count", None),
                    "total_tokens": getattr(usage, "total_token_count", None),
                    "max_output_tokens": effective_output_cap,
                    "finish_reasons": finish_reasons,
                }
                if self._is_truncated(response):
                    self.logger.log("model_call", status="error", error="Gemini response was truncated by max_output_tokens.", **event)
                    self.logger.log("output_validation", operation=operation, status="error", reason="truncated")
                    raise GeminiClientError("Gemini response was truncated by max_output_tokens; no output was saved.")
                self.logger.log("model_call", status="ok", **event)
                return response
            except GeminiClientError:
                raise
            except Exception as exc:
                last_error = exc
                safe_message = self._safe_error(exc)
                retryable = self._is_retryable(exc)
                can_retry = retryable and attempt < self.settings.gemini_max_attempts
                self.logger.log(
                    "model_call",
                    operation=operation,
                    status="retry" if can_retry else "error",
                    attempt=attempt,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    error=safe_message,
                    http_status=self._status_code(exc),
                    max_output_tokens=effective_output_cap,
                )
                if can_retry:
                    delay, delay_source = self._retry_delay(exc, attempt)
                    self.logger.log(
                        "model_retry",
                        operation=operation,
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        reason="rate_limit" if self._status_code(exc) == 429 else "transient_failure",
                        delay_seconds=round(delay, 3),
                        delay_source=delay_source,
                    )
                    self._sleep(delay)
                    continue
                raise GeminiClientError(safe_message) from exc
        raise GeminiClientError(self._safe_error(last_error or RuntimeError()))

    def generate_structured(
        self,
        *,
        prompt: str,
        schema: type[T],
        system_instruction: str,
        operation: str,
        max_output_tokens: int,
    ) -> T:
        config = self.types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.1,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            # JSON Schema preserves nested Pydantic definitions more reliably than
            # the SDK's OpenAPI-schema converter; validation still happens below.
            response_json_schema=schema.model_json_schema(),
        )
        response = self.generate(contents=prompt, config=config, operation=operation)
        try:
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, schema):
                return parsed
            text = getattr(response, "text", None)
            if not text:
                raise ValueError("Structured response was empty.")
            return schema.model_validate_json(text)
        except (ValidationError, ValueError, TypeError, AttributeError) as exc:
            self.logger.log(
                "output_validation",
                operation=operation,
                status="error",
                reason="empty_or_invalid_structured_output",
                finish_reasons=self._finish_reasons(response),
            )
            raise GeminiClientError("Gemini returned empty or invalid structured output; no briefing was saved.") from exc
