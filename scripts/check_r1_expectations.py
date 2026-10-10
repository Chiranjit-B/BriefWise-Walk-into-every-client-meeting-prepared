"""Independently verify revised-R1 fixtures directly from raw transaction JSON."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def derive_r1(dataset: dict) -> dict:
    """Derive R1 without importing or calling the production analyzer."""
    months = [f"2026-{number:02d}" for number in range(1, 7)]
    result = {}
    for month in months:
        triggered = {}
        not_evaluated = []
        for client in dataset["clients"]:
            selected = [item for item in client["transactions"] if item["timestamp"][:7] == month]
            salary = sum(
                (
                    Decimal(str(item["amount"])) for item in selected
                    if item["direction"] == "credit" and item["transaction_type"] == "salary"
                ),
                Decimal("0"),
            )
            if salary <= 0:
                not_evaluated.append(client["client_id"])
                continue
            threshold = salary * Decimal("0.5")
            hits = [
                {"transaction_id": item["transaction_id"], "amount": str(item["amount"])}
                for item in selected
                if item["direction"] == "debit" and Decimal(str(item["amount"])) > threshold
            ]
            if hits:
                triggered[client["client_id"]] = {
                    "salary_basis": str(salary),
                    "threshold_amount": str(threshold),
                    "transactions": hits,
                }
        result[month] = {"triggered": triggered, "not_evaluated": sorted(not_evaluated)}
    return result


def main() -> int:
    dataset = json.loads((ROOT / "data" / "financial_data.json").read_text(encoding="utf-8"))
    fixture = json.loads((ROOT / "data" / "fixtures" / "expected_findings.json").read_text(encoding="utf-8"))
    actual = derive_r1(dataset)
    expected = fixture["expected_monthly_r1"]
    if actual != expected:
        print("FAIL: independently derived R1 results differ from the fixture.")
        print(json.dumps({"expected": expected, "actual": actual}, indent=2, sort_keys=True))
        return 1
    print("PASS: independently derived revised-R1 expectations match all 25 clients for January-June.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
