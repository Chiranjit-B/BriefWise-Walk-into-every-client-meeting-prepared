"""Report actual deterministic results for every client and dataset month."""

import json
from pathlib import Path

from src.analysis import analyze_financial_data
from src.tools import get_financial_data, get_financial_dataset_months


def evaluate_all_months() -> dict:
    """Return deterministic results for every client/month without printing or exiting."""
    root = Path(__file__).resolve().parent.parent
    fixture = json.loads((root / "data" / "fixtures" / "expected_findings.json").read_text(encoding="utf-8"))
    summary = {}
    for month in get_financial_dataset_months():
        triggered = {}
        status_counts = {
            rule_id: {"triggered": 0, "not_triggered": 0, "insufficient_history": 0, "not_evaluated": 0}
            for rule_id in ("R1", "R2", "R3", "R4", "R5")
        }
        for number in range(1, 26):
            client_id = f"C{number:03d}"
            result = get_financial_data(client_id)
            if result["status"] != "ok":
                raise ValueError(f"{client_id}: financial status {result['status']}")
            analysis = analyze_financial_data(result["data"], month)
            rules = [finding["rule_id"] for finding in analysis["findings"]]
            if rules:
                triggered[client_id] = rules
            for evaluation in analysis["rule_evaluations"]:
                status_counts[evaluation["rule_id"]][evaluation["status"]] += 1
        summary[month] = {"triggered_clients": triggered, "rule_status_counts": status_counts}
        expected_r1 = fixture["expected_monthly_r1"][month]
        actual_r1_clients = {
            client_id for client_id, rules in triggered.items() if "R1" in rules
        }
        if actual_r1_clients != set(expected_r1["triggered"]):
            raise ValueError(
                f"{month}: revised R1 clients differ from independently derived fixture; "
                f"expected {sorted(expected_r1['triggered'])}, got {sorted(actual_r1_clients)}"
            )
        if status_counts["R1"]["not_evaluated"] != len(expected_r1["not_evaluated"]):
            raise ValueError(f"{month}: revised R1 not_evaluated count differs from fixture.")
    return summary


def main() -> int:
    summary = evaluate_all_months()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
