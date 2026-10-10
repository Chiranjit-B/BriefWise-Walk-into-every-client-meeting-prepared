import tempfile
import unittest
from pathlib import Path

from src.advisor_app import AdvisorAppError, load_draft_review
from src.analysis import analyze_financial_data
from src.models import Briefing
from src.presentation import build_advisor_review
from src.state import StateStore
from src.tools import get_financial_data


def evidence_briefing(client_id: str, month: str = "2026-06", *, crm_available: bool = True) -> Briefing:
    financial_data = get_financial_data(client_id)["data"]
    analysis = analyze_financial_data(financial_data, month)
    findings = []
    for finding in analysis["findings"]:
        findings.append({
            "rule_id": finding["rule_id"],
            "rule_label": finding["rule_label"],
            "summary": finding["advisor_explanation"],
            "what_happened": finding["what_happened"],
            "why_flagged": finding["why_flagged"],
            "evidence_references": finding["evidence_references"],
            "crm_context": "Possible planning context; not evidence that the transaction was authorized.",
            "uncertainty": "The available records do not establish the client's intent.",
            "advisor_follow_up_question": f"Can the client explain the activity associated with {finding['rule_id']}?",
        })
    source = "available" if crm_available else "missing"
    return Briefing.model_validate({
        "client_id": client_id,
        "analysis_month": month,
        "source_availability": {"financial": "available", "crm": source},
        "executive_summary": (
            f"{len(findings)} configured rule(s) triggered."
            if findings else "No configured rules triggered; this is not a complete risk assessment."
        ),
        "financial_metrics": {"currency": analysis["currency"], **analysis["metrics"]},
        "rule_evaluations": analysis["rule_evaluations"],
        "findings": findings,
        "relevant_crm_context": {
            "risk_tolerance": "Moderate" if crm_available else "Unavailable",
            "financial_goals": "Retirement planning" if crm_available else "Unavailable",
            "advisor_context": "Current profile context" if crm_available else "Unavailable",
            "interpretation": "Use current planning context cautiously." if crm_available else "CRM context was unavailable.",
            "uncertainty": "The CRM profile is current, not historical." if crm_available else "No CRM profile was available.",
            "history_limitation": "Current CRM profile is not a historical snapshot." if crm_available else "CRM source was unavailable.",
        },
        "advisor_follow_up_questions": ["Have the client's priorities changed?"],
        "limitations": ["Transaction descriptions are unverified."],
        "human_review_recommended": any(item["rule_id"] in {"R4", "R5"} for item in findings),
    })


class AdvisorPresentationTests(unittest.TestCase):
    def test_triggered_rules_become_attention_items_with_transaction_evidence(self):
        briefing = evidence_briefing("C010")
        original = briefing.model_dump(mode="json")
        review = build_advisor_review(briefing, financial_data=get_financial_data("C010")["data"])

        self.assertEqual([item.rule_id for item in review.attention_items], ["R1", "R2", "R5"])
        r1_evidence = review.attention_items[0].evidence
        transaction = next(item for item in r1_evidence if item.reference == "C010-T0105")
        self.assertEqual(transaction.kind, "transaction")
        self.assertEqual(transaction.amount, 8000.0)
        self.assertIn("Personal payment", transaction.description)
        self.assertEqual([item.rule_id for item in review.financial_observations], ["R3", "R4"])
        self.assertEqual(briefing.model_dump(mode="json"), original)

    def test_no_trigger_month_remains_explicit_and_has_all_observations(self):
        review = build_advisor_review(evidence_briefing("C002"))
        self.assertEqual(review.attention_items, [])
        self.assertEqual(len(review.financial_observations), 5)
        self.assertIn("complete risk assessment", review.executive_summary)

    def test_cash_flow_and_crm_limitations_are_advisor_readable(self):
        review = build_advisor_review(evidence_briefing("C001", crm_available=False))
        self.assertEqual(review.financial_overview.cash_flow_label, "Recorded shortfall")
        self.assertEqual(review.source_availability["crm"], "missing")
        self.assertIn("CRM source was unavailable.", review.limitations)

    def test_questions_are_deduplicated_without_losing_finding_question(self):
        briefing = evidence_briefing("C001")
        repeated = briefing.findings[0].advisor_follow_up_question
        amended = briefing.model_copy(update={"advisor_follow_up_questions": [repeated, repeated]})
        review = build_advisor_review(amended)
        self.assertEqual(review.meeting_questions, [repeated])

    def test_draft_loader_uses_persistent_state_without_external_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.sqlite3", root / "outputs")
            store.create_session("run-ui", "C010", ["2026-06"])
            store.update_sources("run-ui", "ok", "ok")
            draft = store.create_pending_draft("run-ui", evidence_briefing("C010"))

            loaded, review = load_draft_review(draft["revision_id"], store)
            self.assertEqual(loaded["status"], "pending_review")
            self.assertEqual(review.revision_id, draft["revision_id"])
            self.assertEqual(review.client_id, "C010")

    def test_draft_loader_reports_unknown_revision_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.sqlite3", root / "outputs")
            with self.assertRaisesRegex(AdvisorAppError, "Could not open that draft"):
                load_draft_review("draft_missing", store)


if __name__ == "__main__":
    unittest.main()
