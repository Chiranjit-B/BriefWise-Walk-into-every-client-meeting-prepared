"""Compare deterministic analysis against evaluation-only expected findings."""

import json
from pathlib import Path

from src.analysis import analyze_financial_data
from src.tools import get_financial_data

ROOT = Path(__file__).resolve().parent.parent


def evaluate_fixture() -> dict:
    """Return the June fixture comparison without printing or exiting."""
    fixture = json.loads((ROOT / "data/fixtures/expected_findings.json").read_text(encoding="utf-8"))
    evaluation_month = fixture["evaluation_month"]
    failures = []
    triggered = []
    for expected in fixture["clients"]:
        client_id = expected["client_id"]
        result = get_financial_data(client_id)
        if result["status"] != "ok":
            failures.append(f"{client_id}: financial status {result['status']}")
            continue
        actual = [item["rule_id"] for item in analyze_financial_data(result["data"], evaluation_month)["findings"]]
        wanted = expected["expected_triggered_rule_ids"]
        if actual != wanted:
            failures.append(f"{client_id}: expected {wanted}, got {actual}")
        if actual:
            triggered.append((client_id, actual))
    return {
        "evaluation_month": evaluation_month,
        "client_count": len(fixture["clients"]),
        "triggered": triggered,
        "failures": failures,
    }


def main() -> int:
    result = evaluate_fixture()
    print(f"Evaluated {result['client_count']} clients for {result['evaluation_month']}; triggered: {result['triggered']}")
    if result["failures"]:
        print("FAIL:")
        for failure in result["failures"]:
            print(f"- {failure}")
        return 1
    print("PASS: all deterministic findings match the evaluation fixture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
