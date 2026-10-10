import io
import os
import unittest
from unittest.mock import patch

from src.config import Settings
from tests import test_gemini_client as gemini_tests
from tests import test_tools as tool_tests


class VerificationConfigurationTests(unittest.TestCase):
    def test_offline_environment_tests_preserve_configured_5000_analyzer_cap(self):
        selected_tests = unittest.TestSuite([
            gemini_tests.ConfigurationTests("test_new_defaults"),
            gemini_tests.ConfigurationTests("test_rate_must_be_positive_integer"),
            gemini_tests.ConfigurationTests("test_output_limits_must_be_positive_integers"),
            tool_tests.HubSpotToolTests("test_transient_failure_retries_then_returns_not_found"),
            tool_tests.HubSpotToolTests("test_api_failure_is_distinct_from_not_found"),
        ])
        with patch.dict(os.environ, {
            "GEMINI_FETCHER_MAX_OUTPUT_TOKENS": "512",
            "GEMINI_ANALYZER_MAX_OUTPUT_TOKENS": "5000",
        }):
            before = Settings.from_env()
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(selected_tests)
            after = Settings.from_env()
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(before.gemini_fetcher_max_output_tokens, 512)
        self.assertEqual(after.gemini_fetcher_max_output_tokens, 512)
        self.assertEqual(before.gemini_analyzer_max_output_tokens, 5000)
        self.assertEqual(after.gemini_analyzer_max_output_tokens, 5000)


if __name__ == "__main__":
    unittest.main()
