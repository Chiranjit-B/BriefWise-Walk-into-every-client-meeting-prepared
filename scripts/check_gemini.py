"""Perform one small, bounded Gemini connectivity check."""

from src.config import PROJECT_ROOT, Settings
from src.gemini_client import GeminiClient, GeminiClientError
from src.run_logging import RunLogger


def main() -> int:
    settings = Settings.from_env()
    logger = RunLogger(PROJECT_ROOT / "logs" / "gemini_access_check.jsonl", "gemini_access_check")
    try:
        model = GeminiClient(settings, logger)
        response = model.generate(
            contents="Reply with exactly: OK",
            config=model.types.GenerateContentConfig(temperature=0, max_output_tokens=5),
            operation="connectivity_check",
        )
        if (response.text or "").strip().upper() != "OK":
            print("Gemini responded, but the connectivity response was unexpected.")
            return 1
        print(f"SUCCESS: Gemini model access verified for {settings.gemini_model}.")
        return 0
    except GeminiClientError as exc:
        print(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

