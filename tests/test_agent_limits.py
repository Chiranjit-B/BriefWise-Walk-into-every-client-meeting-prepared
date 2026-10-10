import unittest

from src.agents import AgentError, AnalyzerAgent, DataFetcherAgent
from src.analysis import analyze_financial_data
from src.models import BriefingNarrative, DataBundle
from src.tools import get_financial_data

from tests.test_gemini_client import RecordingLogger, settings


class FetcherModel:
    def __init__(self):
        self.settings = settings()
        self.calls_used = 0

    def generate(self, **_kwargs):
        raise AssertionError("Data retrieval must not use a model call.")


class AnalyzerModel:
    def __init__(self, briefing, configured=None):
        self.settings = configured or settings()
        self.briefing = briefing
        self.arguments = None

    def generate_structured(self, **kwargs):
        self.arguments = kwargs
        return self.briefing


class AgentOutputLimitTests(unittest.TestCase):
    @staticmethod
    def narrative(client_id, rule_ids, *, human_review=False):
        return BriefingNarrative.model_validate({
            "client_id": client_id,
            "analysis_month": "2026-06",
            "executive_summary": "Discuss the recorded cash-flow change and verify the highlighted activity.",
            "findings": [
                {
                    "rule_id": rule_id,
                    "crm_context": "Current CRM details may provide planning context but do not prove authorization.",
                    "uncertainty": "The transaction purpose and authorization have not been independently verified.",
                    "advisor_follow_up_question": f"What was the purpose of the activity highlighted by {rule_id}?",
                }
                for rule_id in rule_ids
            ],
            "relevant_crm_context": {
                "risk_tolerance": "Unavailable",
                "financial_goals": "Unavailable",
                "advisor_context": "Unavailable",
                "interpretation": "CRM context unavailable.",
                "uncertainty": "Goals and notes could not be considered.",
                "history_limitation": "The CRM profile is unavailable, so no historical snapshot can be reconstructed.",
            },
            "advisor_follow_up_questions": ["Have circumstances changed?"],
            "limitations": ["CRM profile not found."],
            "human_review_recommended": human_review,
        })

    def test_future_dated_crm_notes_are_filtered_for_historical_month(self):
        context = (
            "2026-02-10: Earlier note.\n"
            "Undated current observation.\n"
            "2026-05-15: Future note."
        )
        filtered, excluded = AnalyzerAgent._historical_advisor_context(context, "2026-04")
        self.assertIn("2026-02-10", filtered)
        self.assertIn("Undated current observation", filtered)
        self.assertNotIn("2026-05-15", filtered)
        self.assertEqual(excluded, 1)

    def test_fetcher_dispatches_required_tools_without_a_model_call(self):
        model = FetcherModel()
        agent = DataFetcherAgent(model, RecordingLogger())
        agent._tools = {
            "get_financial_data": lambda _client_id: {"status": "ok", "data": {}},
            "get_crm_profile": lambda _client_id: {"status": "not_found", "client_id": "C001"},
        }
        bundle = agent.fetch("C001")
        self.assertEqual(bundle.financial_status, "ok")
        self.assertEqual(bundle.crm_status, "not_found")
        self.assertEqual(model.calls_used, 0)

    def test_analyzer_passes_its_output_limit(self):
        financial = get_financial_data("C002")["data"]
        deterministic = analyze_financial_data(financial, "2026-06")
        narrative = self.narrative("C002", [])
        model = AnalyzerModel(narrative, settings(gemini_analyzer_max_output_tokens=5000))
        agent = AnalyzerAgent(model, RecordingLogger())
        briefing = agent.analyze(DataBundle(
            client_id="C002",
            financial_status="ok",
            crm_status="not_found",
            financial_data=financial,
            crm_message="CRM profile not found.",
        ), "2026-06", latest_dataset_month="2026-06")
        self.assertEqual(model.arguments["max_output_tokens"], 5000)
        self.assertIs(model.arguments["schema"], BriefingNarrative)
        self.assertEqual(
            [item.model_dump() for item in briefing.rule_evaluations],
            deterministic["rule_evaluations"],
        )

    def test_analyzer_assembles_rule_mechanics_and_references_deterministically(self):
        financial = get_financial_data("C010")["data"]
        deterministic = analyze_financial_data(financial, "2026-06")
        model = AnalyzerModel(self.narrative("C010", ["R1", "R2", "R5"], human_review=True))
        agent = AnalyzerAgent(model, RecordingLogger())
        briefing = agent.analyze(DataBundle(
            client_id="C010", financial_status="ok", crm_status="not_found",
            financial_data=financial, crm_message="CRM profile not found.",
        ), "2026-06", latest_dataset_month="2026-06")
        expected = {item["rule_id"]: item for item in deterministic["findings"]}
        for finding in briefing.findings:
            source = expected[finding.rule_id]
            self.assertEqual(finding.summary, source["advisor_explanation"])
            self.assertEqual(finding.what_happened, source["what_happened"])
            self.assertEqual(finding.why_flagged, source["why_flagged"])
            self.assertEqual(finding.evidence_references, source["evidence_references"])
        briefing.findings[0].evidence_references = ["C025-T0103"]
        with self.assertRaisesRegex(AgentError, "rule-specific evidence"):
            agent._validate_briefing(
                briefing,
                DataBundle(
                    client_id="C010", financial_status="ok", crm_status="not_found",
                    financial_data=financial, crm_message="CRM profile not found.",
                ),
                deterministic,
                {"financial": "available", "crm": "missing"},
                "2026-06",
            )


if __name__ == "__main__":
    unittest.main()
