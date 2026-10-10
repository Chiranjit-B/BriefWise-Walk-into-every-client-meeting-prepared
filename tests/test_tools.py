import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import tools


class FinancialToolTests(unittest.TestCase):
    def test_client_catalog_contains_the_expected_25_clients(self):
        self.assertEqual(tools.get_financial_client_ids(), [f"C{number:03d}" for number in range(1, 26)])

    def test_duplicate_client_ids_return_error(self):
        record = {
            "client_id": "C001", "currency": "USD", "monthly_income": 1,
            "opening_balance": 1, "closing_balance": 1, "transactions": [],
        }
        dataset = {
            "schema_version": "2.0", "currency": "USD", "period_start": "2026-01-01",
            "period_end": "2026-06-30", "clients": [record, record],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text(json.dumps(dataset), encoding="utf-8")
            with patch.object(tools, "FINANCIAL_DATA_PATH", path):
                result = tools.get_financial_data("C001")
        self.assertEqual(result["status"], "error")
        self.assertIn("Duplicate", result["message"])

    def test_missing_top_level_field_returns_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text('{"clients": []}', encoding="utf-8")
            with patch.object(tools, "FINANCIAL_DATA_PATH", path):
                result = tools.get_financial_data("C001")
        self.assertEqual(result["status"], "error")


class HubSpotToolTests(unittest.TestCase):
    @patch.dict(os.environ, {"HUBSPOT_ACCESS_TOKEN": "test-token"})
    @patch("src.tools.time.sleep", return_value=None)
    @patch("src.tools.requests.post")
    def test_transient_failure_retries_then_returns_not_found(self, post, _sleep):
        transient = Mock(status_code=503, ok=False)
        missing = Mock(status_code=200, ok=True)
        missing.json.return_value = {"total": 0, "results": []}
        post.side_effect = [transient, missing]
        retries = []
        result = tools.get_crm_profile("C005", retry_callback=lambda attempt, reason: retries.append((attempt, reason)))
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(retries, [(1, 503)])

    @patch.dict(os.environ, {"HUBSPOT_ACCESS_TOKEN": "test-token"})
    @patch("src.tools.requests.post")
    def test_api_failure_is_distinct_from_not_found(self, post):
        post.return_value = Mock(status_code=401, ok=False)
        result = tools.get_crm_profile("C001", max_attempts=1)
        self.assertEqual(result["status"], "error")
        self.assertIn("authentication", result["message"].lower())


if __name__ == "__main__":
    unittest.main()

