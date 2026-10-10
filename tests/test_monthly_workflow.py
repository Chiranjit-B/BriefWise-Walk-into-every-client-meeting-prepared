import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.main import build_parser
from src.models import Briefing, DataBundle
from src.orchestrator import run_review
from src.state import StateStore

from tests.test_gemini_client import settings


class FakeGeminiClient:
    def __init__(self, configured, logger):
        self.settings = configured
        self.logger = logger
        self.calls_used = 0


class FakeFetcher:
    calls = 0

    def __init__(self, model, logger):
        self.model = model

    def fetch(self, client_id):
        type(self).calls += 1
        return DataBundle(
            client_id=client_id,
            financial_status="ok",
            crm_status="ok",
            financial_data={"client_id": client_id},
            crm_data={"client_id": client_id},
        )


class FakeBriefing:
    def __init__(self, client_id, month):
        self.value = Briefing.model_validate({
            "client_id": client_id,
            "analysis_month": month,
            "source_availability": {"financial": "available", "crm": "available"},
            "executive_summary": "Mocked offline briefing.",
            "financial_metrics": {
                "currency": "USD", "selected_month_spending": 0, "selected_month_salary": 0,
                "r1_debit_threshold": None, "income_minus_spending": 0, "preceding_three_month_average": None,
            },
            "rule_evaluations": [
                {
                    "rule_id": rule, "rule_label": f"Mock {rule}", "status": "not_triggered",
                    "reason": "Mocked.", "explanation": "Mocked explanation.",
                    "evidence_references": [], "baseline_period": [], "baseline_count": 0,
                }
                for rule in ("R1", "R2", "R3", "R4", "R5")
            ],
            "findings": [],
            "relevant_crm_context": {
                "risk_tolerance": "Moderate", "financial_goals": "Mock goal",
                "advisor_context": "Mock context", "interpretation": "Mock interpretation",
                "uncertainty": "Mocked sources.", "history_limitation": "Not a historical snapshot.",
            },
            "advisor_follow_up_questions": ["Any changes?"],
            "limitations": ["Offline mock."],
            "human_review_recommended": False,
        })

    def __getattr__(self, name):
        return getattr(self.value, name)

    def model_dump(self, mode="json"):
        return self.value.model_dump(mode=mode)


class FakeAnalyzer:
    months = []
    memories = []

    def __init__(self, model, logger):
        self.model = model

    def analyze(self, bundle, analysis_month, *, latest_dataset_month, historical_memory=None):
        type(self).months.append(analysis_month)
        type(self).memories.append(historical_memory or [])
        self.model.calls_used += 1
        return FakeBriefing(bundle.client_id, analysis_month)


class CLIValidationTests(unittest.TestCase):
    def test_month_and_all_months_are_mutually_exclusive(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--client-id", "C001", "--month", "2026-04", "--all-months"])

    def test_month_format_is_validated(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--client-id", "C001", "--month", "2026-13"])


class OrchestratorMonthTests(unittest.TestCase):
    def setUp(self):
        FakeFetcher.calls = 0
        FakeAnalyzer.months = []
        FakeAnalyzer.memories = []

    def patches(self, root):
        return (
            patch("src.orchestrator.PROJECT_ROOT", root),
            patch("src.orchestrator.get_financial_dataset_months", return_value=[
                "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
            ]),
            patch("src.orchestrator.GeminiClient", FakeGeminiClient),
            patch("src.orchestrator.DataFetcherAgent", FakeFetcher),
            patch("src.orchestrator.AnalyzerAgent", FakeAnalyzer),
        )

    def test_default_uses_latest_dataset_month(self):
        with tempfile.TemporaryDirectory() as directory:
            patchers = self.patches(Path(directory))
            with patchers[0], patchers[1], patchers[2], patchers[3], patchers[4]:
                result = run_review("C001", settings=settings())
        self.assertEqual(result.status, "ok")
        self.assertEqual(FakeAnalyzer.months, ["2026-06"])
        self.assertIn("2026-06", result.output_path.name)

    def test_runtime_root_isolates_logs_state_and_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patchers = self.patches(root)
            with patchers[1], patchers[2], patchers[3], patchers[4]:
                result = run_review("C001", month="2026-06", settings=settings(), runtime_root=root)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.log_path.is_relative_to(root))
        self.assertTrue(result.output_path.is_relative_to(root))

    def test_out_of_range_month_stops_before_model_or_retrieval(self):
        with tempfile.TemporaryDirectory() as directory:
            patchers = self.patches(Path(directory))
            model = Mock(side_effect=AssertionError("model should not be created"))
            with patchers[0], patchers[1], patch("src.orchestrator.GeminiClient", model), patchers[3], patchers[4]:
                result = run_review("C001", month="2025-12", settings=settings())
        self.assertEqual(result.status, "error")
        self.assertIn("outside dataset coverage", result.message)
        model.assert_not_called()
        self.assertEqual(FakeFetcher.calls, 0)

    def test_all_months_reuses_one_retrieval_and_writes_index(self):
        with tempfile.TemporaryDirectory() as directory:
            patchers = self.patches(Path(directory))
            with patchers[0], patchers[1], patchers[2], patchers[3], patchers[4]:
                result = run_review("C001", all_months=True, settings=settings())
                index = json.loads(result.index_path.read_text(encoding="utf-8"))
                store = StateStore(Path(directory) / "state" / "wealth_advisor.sqlite3", Path(directory) / "outputs")
                store.approve(result.draft_ids[0], "Offline Test Advisor")
                refreshed = json.loads(result.index_path.read_text(encoding="utf-8"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(FakeFetcher.calls, 1)
        self.assertEqual(FakeAnalyzer.months, [
            "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
        ])
        self.assertEqual(len(result.output_paths), 6)
        self.assertEqual(index["retrieval_count"], 1)
        self.assertEqual(index["model_calls_used"], 6)
        self.assertEqual(index["results"][0]["status"], "pending_review")
        self.assertEqual(refreshed["results"][0]["status"], "approved")
        self.assertTrue(all(item["status"] == "pending_review" for item in refreshed["results"][1:]))

    def test_all_months_reports_remaining_when_shared_budget_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            patchers = self.patches(Path(directory))
            limited = settings(gemini_max_calls=3)
            with patchers[0], patchers[1], patchers[2], patchers[3], patchers[4]:
                result = run_review("C001", all_months=True, settings=limited)
                index = json.loads(result.index_path.read_text(encoding="utf-8"))
        self.assertEqual(result.status, "partial")
        self.assertEqual(FakeFetcher.calls, 1)
        self.assertEqual(result.completed_months, ("2026-01", "2026-02", "2026-03"))
        self.assertEqual(result.remaining_months, ("2026-04", "2026-05", "2026-06"))
        self.assertEqual(index["model_call_limit"], 3)
        self.assertEqual(index["model_calls_used"], 3)

    def test_offline_mocked_draft_approval_becomes_later_month_memory(self):
        """Offline demonstration: mocked agents, explicit approval, then persisted retrieval."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "state.sqlite3", root / "outputs")
            patchers = self.patches(root)
            with patchers[0], patchers[1], patchers[2], patchers[3], patchers[4]:
                january = run_review("C001", month="2026-01", settings=settings(), state_store=store)
                self.assertEqual(store.get_draft(january.draft_ids[0])["status"], "pending_review")
                store.approve(january.draft_ids[0], "Offline Test Advisor", "Mocked approval")
                april = run_review("C001", month="2026-04", settings=settings(), state_store=store)
        self.assertEqual(april.status, "ok")
        self.assertEqual(len(FakeAnalyzer.memories[-1]), 1)
        self.assertEqual(FakeAnalyzer.memories[-1][0]["analysis_month"], "2026-01")


if __name__ == "__main__":
    unittest.main()
