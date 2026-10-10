import copy
import json
import unittest
from pathlib import Path

from src.analysis import analyze_financial_data, detect_transfer_risks, validate_client_record
from src.tools import get_financial_data
from scripts.check_r1_expectations import derive_r1

ROOT = Path(__file__).resolve().parent.parent


def tx(identifier, timestamp, amount, *, direction="debit", kind="purchase", recipient="R1"):
    return {
        "transaction_id": identifier,
        "timestamp": timestamp,
        "amount": amount,
        "direction": direction,
        "transaction_type": kind,
        "category": "personal_transfer" if kind == "transfer" else ("salary" if kind == "salary" else "shopping"),
        "recipient_id": None if direction == "credit" else recipient,
        "description": "Test transaction",
    }


def client(transactions):
    return {
        "client_id": "C999",
        "currency": "USD",
        "monthly_income": 1000,
        "opening_balance": 10000,
        "closing_balance": 10000,
        "transactions": transactions,
    }


class FullDatasetTests(unittest.TestCase):
    def test_revised_r1_fixture_matches_independent_raw_derivation(self):
        dataset = json.loads((ROOT / "data/financial_data.json").read_text(encoding="utf-8"))
        expected = json.loads((ROOT / "data/fixtures/expected_findings.json").read_text(encoding="utf-8"))
        self.assertEqual(derive_r1(dataset), expected["expected_monthly_r1"])

    def test_all_clients_match_evaluation_fixture(self):
        expected = json.loads((ROOT / "data/fixtures/expected_findings.json").read_text(encoding="utf-8"))
        expected_map = {item["client_id"]: item["expected_triggered_rule_ids"] for item in expected["clients"]}
        actual = {}
        for client_id in sorted(expected_map):
            result = get_financial_data(client_id)
            self.assertEqual(result["status"], "ok")
            analysis = analyze_financial_data(result["data"], "2026-06")
            actual[client_id] = [finding["rule_id"] for finding in analysis["findings"]]
        self.assertEqual(actual, expected_map)
        self.assertEqual(
            {client_id for client_id, rules in actual.items() if rules},
            {"C001", "C004", "C010", "C022", "C025"},
        )

    def test_malformed_and_duplicate_transactions_rejected(self):
        record = client([tx("T1", "2026-06-01T00:00:00+00:00", 10)])
        broken = copy.deepcopy(record)
        del broken["transactions"][0]["amount"]
        with self.assertRaises(ValueError):
            validate_client_record(broken)
        duplicate = copy.deepcopy(record)
        duplicate["transactions"].append(copy.deepcopy(duplicate["transactions"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate transaction IDs"):
            validate_client_record(duplicate)


class StrictBoundaryTests(unittest.TestCase):
    def test_r1_r2_r3_strict_boundaries_do_not_trigger(self):
        transactions = [
            tx("M", "2026-03-01T00:00:00+00:00", 100),
            tx("A", "2026-04-01T00:00:00+00:00", 100),
            tx("Y", "2026-05-01T00:00:00+00:00", 100),
            tx("J1", "2026-06-01T00:00:00+00:00", 150),
            tx("S", "2026-06-01T01:00:00+00:00", 150, direction="credit", kind="salary", recipient=None),
            tx("J2", "2026-06-02T00:00:00+00:00", 5000),
            tx("S2", "2026-06-02T01:00:00+00:00", 9850, direction="credit", kind="salary", recipient=None),
        ]
        rules = {finding["rule_id"] for finding in analyze_financial_data(client(transactions), "2026-06")["findings"]}
        self.assertNotIn("R1", rules)
        self.assertNotIn("R3", rules)
        # R2 does trigger here because the separate $5,000 boundary transaction adds to June.
        r2_only = transactions[:3] + transactions[3:4] + transactions[4:5]
        rules = {finding["rule_id"] for finding in analyze_financial_data(client(r2_only), "2026-06")["findings"]}
        self.assertNotIn("R2", rules)

    def test_r4_includes_exactly_thirty_minutes(self):
        transfers = [
            tx(f"T{i}", timestamp, 100, kind="transfer", recipient="NEW")
            for i, timestamp in enumerate([
                "2026-06-01T10:00:00+00:00", "2026-06-01T10:05:00+00:00",
                "2026-06-01T10:10:00+00:00", "2026-06-01T10:20:00+00:00",
                "2026-06-01T10:30:00+00:00",
            ], 1)
        ]
        self.assertIn("R4", {item["rule_id"] for item in detect_transfer_risks(transfers, "2026-06")})
        transfers[-1]["timestamp"] = "2026-06-01T10:30:01+00:00"
        self.assertNotIn("R4", {item["rule_id"] for item in detect_transfer_risks(transfers, "2026-06")})

    def test_r4_does_not_trigger_for_previously_known_recipient(self):
        transfers = [tx("OLD", "2026-05-01T10:00:00+00:00", 100, kind="transfer", recipient="KNOWN")]
        transfers.extend(
            tx(f"T{i}", f"2026-06-01T10:{i * 5:02}:00+00:00", 100, kind="transfer", recipient="KNOWN")
            for i in range(5)
        )
        self.assertNotIn("R4", {item["rule_id"] for item in detect_transfer_risks(transfers, "2026-06")})

    def test_r5_strict_median_boundary_and_history_minimum(self):
        baseline = [tx(f"H{i}", f"2026-0{i}-01T00:00:00+00:00", 1200, kind="transfer", recipient=f"R{i}") for i in range(1, 6)]
        exact = baseline + [tx("NEW", "2026-06-01T00:00:00+00:00", 6000, kind="transfer", recipient="NEW")]
        self.assertNotIn("R5", {item["rule_id"] for item in detect_transfer_risks(exact, "2026-06")})
        insufficient = baseline[:4] + [tx("BIG", "2026-06-01T00:00:00+00:00", 10000, kind="transfer", recipient="NEW")]
        self.assertNotIn("R5", {item["rule_id"] for item in detect_transfer_risks(insufficient, "2026-06")})


class MonthlyAnalysisTests(unittest.TestCase):
    @staticmethod
    def evaluation(analysis, rule_id):
        return next(item for item in analysis["rule_evaluations"] if item["rule_id"] == rule_id)

    def test_selected_month_excludes_future_transactions(self):
        transactions = [
            tx("APR", "2026-04-10T00:00:00+00:00", 100),
            tx("APRSAL", "2026-04-01T00:00:00+00:00", 1000, direction="credit", kind="salary", recipient=None),
            tx("MAY", "2026-05-10T00:00:00+00:00", 9000),
            tx("MAYSAL", "2026-05-01T00:00:00+00:00", 10000, direction="credit", kind="salary", recipient=None),
        ]
        analysis = analyze_financial_data(client(transactions), "2026-04")
        self.assertNotIn("R1", {item["rule_id"] for item in analysis["findings"]})
        self.assertEqual(analysis["metrics"]["selected_month_spending"], 100.0)
        self.assertEqual(analysis["metrics"]["r1_debit_threshold"], 500.0)

    def test_r1_exactly_half_salary_does_not_trigger(self):
        transactions = [
            tx("SAL", "2026-04-01T00:00:00+00:00", 4800, direction="credit", kind="salary", recipient=None),
            tx("HALF", "2026-04-02T00:00:00+00:00", 2400, kind="purchase"),
        ]
        analysis = analyze_financial_data(client(transactions), "2026-04")
        self.assertEqual(self.evaluation(analysis, "R1")["status"], "not_triggered")
        self.assertEqual(analysis["metrics"]["r1_debit_threshold"], 2400.0)
        self.assertNotIn("R1", {item["rule_id"] for item in analysis["findings"]})

    def test_r1_above_half_salary_triggers_purchase_and_transfer(self):
        transactions = [
            tx("SAL", "2026-04-01T00:00:00+00:00", 4800, direction="credit", kind="salary", recipient=None),
            tx("PURCHASE", "2026-04-02T00:00:00+00:00", "2400.01", kind="purchase"),
            tx("TRANSFER", "2026-04-03T00:00:00+00:00", "2500.00", kind="transfer", recipient="NEW"),
        ]
        analysis = analyze_financial_data(client(transactions), "2026-04")
        r1 = next(item for item in analysis["findings"] if item["rule_id"] == "R1")
        self.assertEqual(r1["salary_basis"], 4800.0)
        self.assertEqual(r1["threshold_amount"], 2400.0)
        self.assertEqual(r1["transaction_ids"], ["PURCHASE", "TRANSFER"])
        self.assertEqual(r1["triggering_amounts"], [2400.01, 2500.0])

    def test_r1_sums_multiple_salary_credits(self):
        transactions = [
            tx("SAL1", "2026-04-01T00:00:00+00:00", 2000, direction="credit", kind="salary", recipient=None),
            tx("SAL2", "2026-04-15T00:00:00+00:00", 2800, direction="credit", kind="salary", recipient=None),
            tx("DEBIT", "2026-04-20T00:00:00+00:00", "2400.01"),
        ]
        analysis = analyze_financial_data(client(transactions), "2026-04")
        r1 = next(item for item in analysis["findings"] if item["rule_id"] == "R1")
        self.assertEqual(r1["salary_basis"], 4800.0)
        self.assertEqual(r1["salary_transaction_ids"], ["SAL1", "SAL2"])
        self.assertEqual(r1["threshold_amount"], 2400.0)

    def test_r1_without_salary_is_not_evaluated(self):
        analysis = analyze_financial_data(
            client([tx("DEBIT", "2026-04-20T00:00:00+00:00", 9000)]),
            "2026-04",
        )
        evaluation = self.evaluation(analysis, "R1")
        self.assertEqual(evaluation["status"], "not_evaluated")
        self.assertIn("salary credits were zero or unavailable", evaluation["reason"])
        self.assertIsNone(analysis["metrics"]["r1_debit_threshold"])
        self.assertNotIn("R1", {item["rule_id"] for item in analysis["findings"]})

    def test_rolling_three_month_baseline(self):
        transactions = [
            tx("JAN", "2026-01-10T00:00:00+00:00", 100),
            tx("FEB", "2026-02-10T00:00:00+00:00", 200),
            tx("MAR", "2026-03-10T00:00:00+00:00", 300),
            tx("APR", "2026-04-10T00:00:00+00:00", 301),
            tx("SAL", "2026-04-01T00:00:00+00:00", 1000, direction="credit", kind="salary", recipient=None),
        ]
        analysis = analyze_financial_data(client(transactions), "2026-04")
        evaluation = self.evaluation(analysis, "R2")
        self.assertEqual(evaluation["status"], "triggered")
        self.assertEqual(evaluation["baseline_period"], ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(analysis["metrics"]["preceding_three_month_average"], 200.0)

    def test_early_month_r2_is_insufficient_not_passed(self):
        financial = get_financial_data("C001")["data"]
        for month in ("2026-01", "2026-02", "2026-03"):
            evaluation = self.evaluation(analyze_financial_data(financial, month), "R2")
            self.assertEqual(evaluation["status"], "insufficient_history")
            self.assertIn("required", evaluation["reason"])

    def test_future_transfers_do_not_satisfy_r5_history(self):
        baseline = [
            tx(f"JAN{i}", f"2026-01-{i + 1:02}T00:00:00+00:00", 100, kind="transfer", recipient=f"OLD{i}")
            for i in range(4)
        ]
        selected = [tx("FEBBIG", "2026-02-10T00:00:00+00:00", 10000, kind="transfer", recipient="NEW")]
        future = [
            tx(f"MAR{i}", f"2026-03-{i + 1:02}T00:00:00+00:00", 100, kind="transfer", recipient=f"FUT{i}")
            for i in range(5)
        ]
        analysis = analyze_financial_data(client(baseline + selected + future), "2026-02")
        evaluation = self.evaluation(analysis, "R5")
        self.assertEqual(evaluation["status"], "insufficient_history")
        self.assertEqual(evaluation["baseline_count"], 4)
        self.assertNotIn("R5", {item["rule_id"] for item in analysis["findings"]})

    def test_r4_window_can_cross_month_boundary(self):
        timestamps = [
            "2026-04-30T23:50:00+00:00",
            "2026-04-30T23:55:00+00:00",
            "2026-05-01T00:00:00+00:00",
            "2026-05-01T00:10:00+00:00",
            "2026-05-01T00:20:00+00:00",
        ]
        transfers = [
            tx(f"X{i}", timestamp, 100, kind="transfer", recipient="NEW")
            for i, timestamp in enumerate(timestamps)
        ]
        findings = detect_transfer_risks(transfers, "2026-04")
        r4 = next(item for item in findings if item["rule_id"] == "R4")
        self.assertTrue(r4["crosses_month_boundary"])
        self.assertEqual(r4["transfer_count"], 5)

    def test_r4_qualifying_window_can_begin_after_first_month_transfer(self):
        timestamps = [
            "2026-04-01T09:00:00+00:00",
            "2026-04-01T10:00:00+00:00",
            "2026-04-01T10:05:00+00:00",
            "2026-04-01T10:10:00+00:00",
            "2026-04-01T10:20:00+00:00",
            "2026-04-01T10:30:00+00:00",
        ]
        transfers = [
            tx(f"W{i}", timestamp, 100, kind="transfer", recipient="NEW")
            for i, timestamp in enumerate(timestamps)
        ]
        findings = detect_transfer_risks(transfers, "2026-04")
        r4 = next(item for item in findings if item["rule_id"] == "R4")
        self.assertEqual(r4["window_start"], "2026-04-01T10:00:00+00:00")
        self.assertEqual(r4["transfer_count"], 5)

    def test_r5_new_recipient_uses_prior_month_history_not_first_month_transfer(self):
        baseline = [
            tx(f"H{i}", f"2026-03-{i + 1:02}T00:00:00+00:00", 100, kind="transfer", recipient=f"OLD{i}")
            for i in range(5)
        ]
        selected = [
            tx("SMALL", "2026-04-01T00:00:00+00:00", 10, kind="transfer", recipient="NEW"),
            tx("BIG", "2026-04-02T00:00:00+00:00", 6000, kind="transfer", recipient="NEW"),
        ]
        findings = detect_transfer_risks(baseline + selected, "2026-04")
        r5 = next(item for item in findings if item["rule_id"] == "R5")
        self.assertEqual(r5["transaction_ids"], ["BIG"])

    def test_prior_recipient_excludes_selected_month_r4(self):
        transfers = [tx("OLD", "2026-03-31T23:59:00+00:00", 100, kind="transfer", recipient="KNOWN")]
        transfers.extend(
            tx(f"APR{i}", f"2026-04-01T00:{i * 5:02}:00+00:00", 100, kind="transfer", recipient="KNOWN")
            for i in range(5)
        )
        findings = detect_transfer_risks(transfers, "2026-04")
        self.assertNotIn("R4", {item["rule_id"] for item in findings})


class ExplanationQualityTests(unittest.TestCase):
    @staticmethod
    def evaluation(client_id, rule_id, month="2026-06"):
        financial = get_financial_data(client_id)["data"]
        analysis = analyze_financial_data(financial, month)
        return analysis, next(item for item in analysis["rule_evaluations"] if item["rule_id"] == rule_id)

    def test_every_rule_has_label_explanation_and_rule_specific_references(self):
        analysis, _ = self.evaluation("C010", "R1")
        self.assertEqual(len(analysis["rule_evaluations"]), 5)
        for evaluation in analysis["rule_evaluations"]:
            self.assertTrue(evaluation["rule_label"])
            self.assertIn(f"({evaluation['rule_id']})", evaluation["explanation"])
            self.assertIn("2026", evaluation["explanation"])
            self.assertIsInstance(evaluation["evidence_references"], list)
        for finding in analysis["findings"]:
            evaluation = next(item for item in analysis["rule_evaluations"] if item["rule_id"] == finding["rule_id"])
            self.assertEqual(finding["evidence_references"], evaluation["evidence_references"])
            self.assertTrue(finding["what_happened"])
            self.assertTrue(finding["why_flagged"])

    def test_r1_equality_and_trigger_explanations_use_personalized_threshold(self):
        _, c022 = self.evaluation("C022", "R1")
        self.assertEqual(c022["status"], "not_triggered")
        for text in ("$12,000.00", "$6,000.00", "Furniture store purchase", "equaled the threshold", "greater than 50%"):
            self.assertIn(text, c022["explanation"])

        analysis, c010 = self.evaluation("C010", "R1")
        self.assertEqual(c010["status"], "triggered")
        for text in ("$14,000.00", "$7,000.00", "$8,000.00", "exceeded the threshold", "R1 triggered"):
            self.assertIn(text, c010["explanation"])
        r1 = next(item for item in analysis["findings"] if item["rule_id"] == "R1")
        self.assertIn(r1["transaction_ids"][0], r1["evidence_references"])

    def test_missing_salary_and_missing_three_month_history_are_explicit(self):
        missing_salary = analyze_financial_data(
            client([tx("DEBIT", "2026-04-20T00:00:00+00:00", 9000)]), "2026-04",
        )
        r1 = next(item for item in missing_salary["rule_evaluations"] if item["rule_id"] == "R1")
        self.assertEqual(r1["status"], "not_evaluated")
        self.assertIn("threshold could not be calculated", r1["explanation"])

        _, r2 = self.evaluation("C001", "R2", "2026-01")
        self.assertEqual(r2["status"], "insufficient_history")
        self.assertIn("October-December 2025", r2["explanation"])
        self.assertIn("coverage is missing", r2["explanation"])
        self.assertIn("not evaluated", r2["explanation"])
        self.assertIn("not treated as zero", r2["explanation"])

    def test_c001_r3_explains_recorded_shortfall_without_overclaiming(self):
        _, r3 = self.evaluation("C001", "R3")
        for text in ("$5,173.85", "$4,800.00", "$373.85", "triggering R3"):
            self.assertIn(text, r3["explanation"])
        self.assertIn("does not establish", r3["explanation"])

    def test_c025_r4_explains_inclusive_window_and_new_to_records_meaning(self):
        analysis, r4 = self.evaluation("C025", "R4")
        for text in (
            "C025-R08", "5 transfers", "$400.00", "$2,000.00", "14:00:00 UTC",
            "14:24:00 UTC", "24.0 minutes", "inclusive 30-minute", "does not necessarily mean unknown",
        ):
            self.assertIn(text, r4["explanation"])
        finding = next(item for item in analysis["findings"] if item["rule_id"] == "R4")
        self.assertEqual(finding["evidence_references"], finding["transaction_ids"])

    def test_c010_r5_names_both_distinct_strict_thresholds(self):
        analysis, r5 = self.evaluation("C010", "R5")
        for text in (
            "$8,000.00", "C010-R08", "$5,000", "$1,500.00", "$300.00",
            "8 outgoing transfers", "January-May 2026", "Both strict comparisons were satisfied",
        ):
            self.assertIn(text, r5["explanation"])
        finding = next(item for item in analysis["findings"] if item["rule_id"] == "R5")
        self.assertEqual(finding["evidence_references"], ["C010-T0105"])

    def test_r5_nontrigger_identifies_each_failed_condition(self):
        _, r5 = self.evaluation("C025", "R5")
        self.assertIn("strictly greater than $5,000", r5["explanation"])
        self.assertIn("strictly greater than $1,402.92", r5["explanation"])


if __name__ == "__main__":
    unittest.main()

