import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import BaseModel

from src.config import Settings
from src.gemini_client import GeminiClient, GeminiClientError, RequestRateLimiter


class RecordingLogger:
    def __init__(self):
        self.events = []

    def log(self, event, **fields):
        self.events.append({"event": event, **fields})


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.sleeps.append(seconds)
            self.now += seconds


class ProviderError(Exception):
    def __init__(self, status_code, message="provider failure", *, headers=None, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})
        self.details = details


def settings(**overrides):
    values = {
        "gemini_api_key": "test-key",
        "gemini_model": "test-model",
        "gemini_timeout_seconds": 30,
        "gemini_max_attempts": 2,
        "gemini_max_calls": 8,
        "gemini_requests_per_minute": 4,
        "gemini_fetcher_max_output_tokens": 512,
        "gemini_analyzer_max_output_tokens": 1024,
    }
    values.update(overrides)
    return Settings(**values)


def success_response(*, finish_reason="STOP", text="OK"):
    usage = SimpleNamespace(
        prompt_token_count=2,
        candidates_token_count=1,
        thoughts_token_count=3,
        total_token_count=6,
    )
    return SimpleNamespace(
        text=text,
        parsed=None,
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
        usage_metadata=usage,
    )


def bare_client(side_effect, *, client_settings=None):
    client = GeminiClient.__new__(GeminiClient)
    client.settings = client_settings or settings()
    client.logger = RecordingLogger()
    client.calls_used = 0
    client._calls_lock = threading.Lock()
    client._sleep = Mock()
    client._random_uniform = Mock(return_value=0.25)
    client.rate_limiter = Mock()
    client.client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=side_effect)))
    return client


class ConfigurationTests(unittest.TestCase):
    def test_new_defaults(self):
        with patch.dict(os.environ, {
            "GEMINI_REQUESTS_PER_MINUTE": "4",
            "GEMINI_FETCHER_MAX_OUTPUT_TOKENS": "512",
            "GEMINI_ANALYZER_MAX_OUTPUT_TOKENS": "1024",
        }):
            configured = Settings.from_env()
        self.assertEqual(configured.gemini_requests_per_minute, 4)
        self.assertEqual(configured.gemini_fetcher_max_output_tokens, 512)
        self.assertEqual(configured.gemini_analyzer_max_output_tokens, 1024)

    def test_rate_must_be_positive_integer(self):
        for value in ("0", "-1", "4.5", "invalid"):
            with self.subTest(value=value), patch.dict(os.environ, {"GEMINI_REQUESTS_PER_MINUTE": value}):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    Settings.from_env()

    def test_output_limits_must_be_positive_integers(self):
        for name in ("GEMINI_FETCHER_MAX_OUTPUT_TOKENS", "GEMINI_ANALYZER_MAX_OUTPUT_TOKENS"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "0"}):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    Settings.from_env()


class RateLimiterTests(unittest.TestCase):
    def test_four_per_minute_spaces_requests_by_fifteen_seconds(self):
        clock = FakeClock()
        limiter = RequestRateLimiter(4, RecordingLogger(), clock=clock.monotonic, sleeper=clock.sleep)
        waits = [limiter.wait(operation="test", attempt=i) for i in range(1, 4)]
        self.assertEqual(waits, [0.0, 15.0, 15.0])
        self.assertEqual(clock.sleeps, [15.0, 15.0])

    def test_lock_serializes_concurrent_callers(self):
        clock = FakeClock()
        limiter = RequestRateLimiter(4, RecordingLogger(), clock=clock.monotonic, sleeper=clock.sleep)
        barrier = threading.Barrier(3)
        waits = []

        def worker():
            barrier.wait()
            waits.append(limiter.wait(operation="concurrent", attempt=1))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(waits), [0.0, 15.0, 15.0])
        self.assertEqual(sum(clock.sleeps), 30.0)

    @patch("google.genai.Client")
    def test_client_instances_share_one_process_limiter(self, sdk_client):
        GeminiClient._shared_rate_limiter = None
        GeminiClient._shared_rate = None
        first = GeminiClient(settings(), RecordingLogger())
        second = GeminiClient(settings(), RecordingLogger())
        self.assertIs(first.rate_limiter, second.rate_limiter)
        self.assertEqual(sdk_client.call_count, 2)
        GeminiClient._shared_rate_limiter = None
        GeminiClient._shared_rate = None


class RetryTests(unittest.TestCase):
    def test_429_honors_retry_after_and_paces_retry(self):
        failure = ProviderError(429, headers={"Retry-After": "7"})
        client = bare_client([failure, success_response()])
        response = client.generate(contents="x", config=object(), operation="test")
        self.assertEqual(response.text, "OK")
        self.assertEqual(client.calls_used, 2)
        self.assertEqual(client.rate_limiter.wait.call_count, 2)
        client._sleep.assert_called_once_with(7.0)
        retry = next(event for event in client.logger.events if event["event"] == "model_retry")
        self.assertEqual(retry["delay_source"], "server_guidance")

    def test_429_without_guidance_uses_exponential_jitter(self):
        client = bare_client([ProviderError(429), success_response()])
        client.generate(contents="x", config=object(), operation="test")
        client._sleep.assert_called_once_with(1.25)
        retry = next(event for event in client.logger.events if event["event"] == "model_retry")
        self.assertEqual(retry["delay_source"], "exponential_jitter")

    def test_daily_quota_exhaustion_is_not_retried(self):
        failure = ProviderError(429, "GenerateRequestsPerDayPerProjectPerModel quota exhausted")
        client = bare_client([failure])
        with self.assertRaisesRegex(GeminiClientError, "daily quota is exhausted"):
            client.generate(contents="x", config=object(), operation="test")
        self.assertEqual(client.calls_used, 1)
        self.assertEqual(client.rate_limiter.wait.call_count, 1)
        client._sleep.assert_not_called()

    def test_max_call_limit_still_applies_to_retries(self):
        client = bare_client([ProviderError(429)], client_settings=settings(gemini_max_calls=1))
        with self.assertRaisesRegex(GeminiClientError, "call limit"):
            client.generate(contents="x", config=object(), operation="test")
        self.assertEqual(client.calls_used, 1)


class OutputValidationTests(unittest.TestCase):
    def test_model_call_logs_effective_output_cap(self):
        client = bare_client([success_response()])
        client.generate(
            contents="x",
            config=SimpleNamespace(max_output_tokens=5000),
            operation="analyzer_structured_narrative",
        )
        event = next(item for item in client.logger.events if item["event"] == "model_call")
        self.assertEqual(event["max_output_tokens"], 5000)

    def test_truncated_response_is_rejected(self):
        client = bare_client([success_response(finish_reason="MAX_TOKENS", text="{")])
        with self.assertRaisesRegex(GeminiClientError, "truncated"):
            client.generate(contents="x", config=object(), operation="test")

    def test_invalid_structured_response_is_rejected(self):
        from google.genai import types

        class Result(BaseModel):
            value: str

        client = bare_client([])
        client.types = types
        client.generate = Mock(return_value=success_response(text="{not-json"))
        with self.assertRaisesRegex(GeminiClientError, "invalid structured output"):
            client.generate_structured(
                prompt="x",
                schema=Result,
                system_instruction="x",
                operation="structured",
                max_output_tokens=1024,
            )
        config = client.generate.call_args.kwargs["config"]
        self.assertEqual(config.max_output_tokens, 1024)


if __name__ == "__main__":
    unittest.main()

