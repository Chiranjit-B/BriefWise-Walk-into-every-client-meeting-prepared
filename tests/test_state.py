import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.models import Briefing
from src.state import StateError, StateStore


def briefing(client_id="C001", month="2026-01", interpretation="Initial interpretation"):
    return Briefing.model_validate({
        "client_id": client_id,
        "analysis_month": month,
        "source_availability": {"financial": "available", "crm": "available"},
        "executive_summary": "Validated draft summary.",
        "financial_metrics": {
            "currency": "USD", "selected_month_spending": 100.0, "selected_month_salary": 1000.0,
            "r1_debit_threshold": 500.0, "income_minus_spending": 900.0,
            "preceding_three_month_average": None,
        },
        "rule_evaluations": [
            {
                "rule_id": rule, "rule_label": f"Mock {rule}", "status": "not_triggered", "reason": "No trigger.",
                "explanation": "No trigger in synthetic data.", "evidence_references": [],
                "baseline_period": [], "baseline_count": 0,
            }
            for rule in ("R1", "R2", "R3", "R4", "R5")
        ],
        "findings": [],
        "relevant_crm_context": {
            "risk_tolerance": "Moderate", "financial_goals": "Retirement",
            "advisor_context": "Earlier note", "interpretation": interpretation,
            "uncertainty": "Current CRM profile.", "history_limitation": "Not a historical snapshot.",
        },
        "advisor_follow_up_questions": ["Has anything changed?"],
        "limitations": ["Synthetic data."],
        "human_review_recommended": False,
    })


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.db_path = root / "state.sqlite3"
        self.store = StateStore(self.db_path, root / "outputs")

    def tearDown(self):
        self.temporary.cleanup()

    def pending(self, run_id, client_id="C001", month="2026-01"):
        self.store.create_session(run_id, client_id, [month])
        self.store.update_sources(run_id, "ok", "ok")
        return self.store.create_pending_draft(run_id, briefing(client_id, month))

    def test_session_and_customer_isolation(self):
        self.store.create_session("run-a", "C001", ["2026-01"])
        self.store.create_session("run-b", "C002", ["2026-02"])
        self.store.record_historical_memory("run-a", "2026-01", [{"client_id": "C001"}])
        first = self.store.get_session("run-a")
        second = self.store.get_session("run-b")
        self.assertEqual(first["client_id"], "C001")
        self.assertEqual(second["client_id"], "C002")
        self.assertEqual(second["historical_memory"], {})

    def test_persistent_retrieval_across_store_instances(self):
        draft = self.pending("run-persist")
        self.store.approve(draft["revision_id"], "Advisor One", "Reviewed")
        reopened = StateStore(self.db_path, Path(self.temporary.name) / "outputs")
        memory = reopened.retrieve_approved_memory("C001", "2026-02")
        self.assertEqual(len(memory), 1)
        self.assertEqual(memory[0]["originating_revision_id"], draft["revision_id"])

    def test_only_approved_same_customer_earlier_month_is_retrieved(self):
        january = self.pending("run-jan", "C001", "2026-01")
        self.store.approve(january["revision_id"], "Advisor One")
        future = self.pending("run-jun", "C001", "2026-06")
        self.store.approve(future["revision_id"], "Advisor One")
        other = self.pending("run-other", "C002", "2026-02")
        self.store.approve(other["revision_id"], "Advisor Two")
        pending = self.pending("run-pending", "C001", "2026-03")
        memory = self.store.retrieve_approved_memory("C001", "2026-05")
        self.assertEqual([item["analysis_month"] for item in memory], ["2026-01"])
        self.assertNotEqual(pending["status"], "approved")

    def test_rejection_does_not_create_memory(self):
        draft = self.pending("run-reject")
        rejected = self.store.reject(draft["revision_id"], "Advisor One", "Needs correction")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.store.retrieve_approved_memory("C001", "2026-02"), [])

    def test_amendment_preserves_immutable_evidence_and_original(self):
        draft = self.pending("run-amend")
        amended = self.store.amend_and_approve(
            draft["revision_id"], "Advisor One",
            interpretation="Advisor-amended interpretation.",
            follow_up_questions=["Please confirm the purpose."],
            advisor_disposition="Explained by client; retain the original signal for audit.",
            reason="Reviewed supporting information.",
        )
        original = self.store.get_draft(draft["revision_id"])
        self.assertEqual(original["status"], "superseded")
        self.assertEqual(amended["status"], "approved")
        for field in ("client_id", "analysis_month", "source_availability", "financial_metrics", "rule_evaluations", "findings"):
            self.assertEqual(original["briefing"][field], amended["briefing"][field])
        self.assertEqual(amended["advisor_disposition"], "Explained by client; retain the original signal for audit.")
        self.assertEqual(len(self.store.list_audit(draft["briefing_id"])), 1)

    def test_duplicate_approval_is_idempotent_and_superseded_memory_is_inactive(self):
        draft = self.pending("run-duplicate")
        self.store.approve(draft["revision_id"], "Advisor One")
        self.store.approve(draft["revision_id"], "Advisor One")
        self.assertEqual(len(self.store.retrieve_approved_memory("C001", "2026-02")), 1)
        self.assertEqual(len(self.store.list_audit(draft["briefing_id"])), 1)
        amended = self.store.amend_and_approve(
            draft["revision_id"], "Advisor One", interpretation="Superseding interpretation."
        )
        memory = self.store.retrieve_approved_memory("C001", "2026-02")
        self.assertEqual(len(memory), 1)
        self.assertEqual(memory[0]["originating_revision_id"], amended["revision_id"])

    def test_immutable_evidence_guard_and_ai_label(self):
        original = briefing().model_dump(mode="json")
        changed = copy.deepcopy(original)
        changed["financial_metrics"]["selected_month_spending"] = 999.0
        with self.assertRaisesRegex(StateError, "immutable"):
            self.store._assert_evidence_immutable(original, changed)
        draft = self.pending("run-ai")
        with self.assertRaisesRegex(StateError, "AI or model"):
            self.store.approve(draft["revision_id"], "AI Reviewer")

    def test_pending_draft_is_not_approved_memory(self):
        self.pending("run-pending-only")
        self.assertEqual(self.store.retrieve_approved_memory("C001", "2026-02"), [])
        connection = sqlite3.connect(self.db_path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM approved_memories").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)

    def test_obsolete_ruleset_drafts_and_memory_are_not_reused(self):
        draft = self.pending("run-obsolete")
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("UPDATE drafts SET ruleset_version='' WHERE revision_id=?", (draft["revision_id"],))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(StateError, "obsolete analysis ruleset"):
            self.store.approve(draft["revision_id"], "Advisor One")

        current = self.pending("run-current", "C001", "2026-01")
        self.store.approve(current["revision_id"], "Advisor One")
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT memory_id, summary_json FROM approved_memories WHERE originating_revision_id=?",
                (current["revision_id"],),
            ).fetchone()
            summary = json.loads(row[1])
            summary["ruleset_version"] = "obsolete"
            connection.execute(
                "UPDATE approved_memories SET summary_json=? WHERE memory_id=?",
                (json.dumps(summary), row[0]),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.store.retrieve_approved_memory("C001", "2026-02"), [])


if __name__ == "__main__":
    unittest.main()
