"""Deterministic, month-relative financial analysis rules."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Literal

ZERO = Decimal("0")
ANALYSIS_RULESET_VERSION = "2026-10-r1-income-relative-v2"
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CLIENT_FIELDS = {"client_id", "currency", "monthly_income", "opening_balance", "closing_balance", "transactions"}
TRANSACTION_FIELDS = {
    "transaction_id", "timestamp", "amount", "direction", "transaction_type",
    "category", "recipient_id", "description",
}
RuleStatus = Literal["triggered", "not_triggered", "insufficient_history", "not_evaluated"]
RULE_LABELS = {
    "R1": "Large debit relative to salary",
    "R2": "Spending increase",
    "R3": "Spending exceeds salary",
    "R4": "Rapid transfers to a new recipient",
    "R5": "Large transfer to a new recipient",
}


def validate_month(month: str) -> str:
    if not isinstance(month, str) or not MONTH_PATTERN.fullmatch(month):
        raise ValueError("Analysis month must use YYYY-MM format.")
    return month


def shift_month(month: str, offset: int) -> str:
    validate_month(month)
    year, number = (int(part) for part in month.split("-"))
    absolute = year * 12 + number - 1 + offset
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def month_range(first_month: str, last_month: str) -> list[str]:
    validate_month(first_month)
    validate_month(last_month)
    if first_month > last_month:
        raise ValueError("Dataset period start must not be after period end.")
    months = []
    current = first_month
    while current <= last_month:
        months.append(current)
        current = shift_month(current, 1)
    return months


def amount(transaction: dict) -> Decimal:
    try:
        value = Decimal(str(transaction["amount"]))
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError("Transaction amount must be numeric.") from exc
    if not value.is_finite() or value <= ZERO:
        raise ValueError("Transaction amount must be finite and positive.")
    return value


def _timestamp(transaction: dict) -> datetime:
    try:
        parsed = datetime.fromisoformat(transaction["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Transaction timestamp must be valid ISO 8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Transaction timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _transaction_month(transaction: dict) -> str:
    return _timestamp(transaction).strftime("%Y-%m")


def validate_client_record(client: Any, *, period_start: str | None = None, period_end: str | None = None) -> None:
    """Raise ValueError when a financial client record violates the input contract."""
    if not isinstance(client, dict) or not CLIENT_FIELDS.issubset(client):
        raise ValueError("Client record is missing required fields.")
    if not isinstance(client["client_id"], str) or not client["client_id"]:
        raise ValueError("Client record has an invalid client_id.")
    if client["currency"] != "USD":
        raise ValueError("Client currency must be USD.")
    for field in ("monthly_income", "opening_balance", "closing_balance"):
        try:
            value = Decimal(str(client[field]))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError(f"Client {field} must be numeric.") from exc
        if not value.is_finite() or value < ZERO:
            raise ValueError(f"Client {field} must be finite and nonnegative.")
    transactions = client["transactions"]
    if not isinstance(transactions, list):
        raise ValueError("Client transactions must be a list.")
    seen_ids: set[str] = set()
    for transaction in transactions:
        if not isinstance(transaction, dict) or not TRANSACTION_FIELDS.issubset(transaction):
            raise ValueError("Transaction is missing required fields.")
        transaction_id = transaction["transaction_id"]
        if not isinstance(transaction_id, str) or not transaction_id:
            raise ValueError("Transaction ID must be a nonempty string.")
        if transaction_id in seen_ids:
            raise ValueError("Duplicate transaction IDs within client record.")
        seen_ids.add(transaction_id)
        amount(transaction)
        when = _timestamp(transaction).date().isoformat()
        if period_start is not None and when < period_start:
            raise ValueError("Transaction is before the dataset period.")
        if period_end is not None and when > period_end:
            raise ValueError("Transaction is after the dataset period.")
        if transaction["direction"] not in {"credit", "debit"}:
            raise ValueError("Transaction direction must be credit or debit.")
        if transaction["transaction_type"] not in {"salary", "purchase", "transfer"}:
            raise ValueError("Transaction type is invalid.")
        if not isinstance(transaction["description"], str):
            raise ValueError("Transaction description must be a string.")
        if transaction["direction"] == "debit" and not isinstance(transaction["recipient_id"], str):
            raise ValueError("Debit transactions require a recipient_id.")


def available_months_for_client(client: dict) -> list[str]:
    validate_client_record(client)
    return sorted({_transaction_month(transaction) for transaction in client["transactions"]})


def _evidence(transaction: dict) -> dict:
    return {
        "transaction_id": transaction["transaction_id"],
        "timestamp": transaction["timestamp"],
        "amount": float(amount(transaction)),
        "description": transaction["description"],
        "recipient_id": transaction.get("recipient_id"),
    }


def _rule_evaluation(
    rule_id: str,
    status: RuleStatus,
    reason: str,
    *,
    baseline_period: list[str] | None = None,
    baseline_count: int = 0,
) -> dict:
    return {
        "rule_id": rule_id,
        "rule_label": RULE_LABELS[rule_id],
        "status": status,
        "reason": reason,
        "explanation": reason,
        "evidence_references": [],
        "baseline_period": baseline_period or [],
        "baseline_count": baseline_count,
    }


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _month_display(month: str) -> str:
    parsed = datetime.strptime(month, "%Y-%m")
    return parsed.strftime("%B %Y")


def _period_display(months: list[str]) -> str:
    if not months:
        return "no earlier recorded months"
    if len(months) == 1:
        return _month_display(months[0])
    first = datetime.strptime(months[0], "%Y-%m")
    last = datetime.strptime(months[-1], "%Y-%m")
    if first.year == last.year:
        return f"{first.strftime('%B')}-{last.strftime('%B %Y')}"
    return f"{first.strftime('%B %Y')}-{last.strftime('%B %Y')}"


def _time_display(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp)
    zone = parsed.tzname() or parsed.strftime("%z") or "local time"
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year} at {parsed.strftime('%H:%M:%S')} {zone}"


def _comparison_text(value: Decimal, threshold: Decimal) -> str:
    if value > threshold:
        return f"exceeded the threshold by {_money(value - threshold)}"
    if value == threshold:
        return "equaled the threshold"
    return f"was {_money(threshold - value)} below the threshold"


def _enrich_explanations(
    *,
    transactions: list[dict],
    selected_month: str,
    selected_debits: list[dict],
    selected_salary: Decimal,
    salary_transactions: list[dict],
    selected_spending: Decimal,
    preceding_months: list[str],
    baseline_average: Decimal | None,
    evaluations: list[dict],
    findings: list[dict],
) -> None:
    """Attach deterministic, advisor-readable mechanics without changing rule decisions."""
    month_name = _month_display(selected_month)
    by_rule = {item["rule_id"]: item for item in evaluations}
    findings_by_rule: dict[str, list[dict]] = {
        rule_id: [item for item in findings if item["rule_id"] == rule_id]
        for rule_id in RULE_LABELS
    }

    # R1
    r1 = by_rule["R1"]
    largest_debit = max(selected_debits, key=amount) if selected_debits else None
    r1_refs = ["metrics.selected_month_salary", "metrics.r1_debit_threshold"]
    if r1["status"] == "triggered":
        threshold = selected_salary * Decimal("0.5")
        r1_refs.extend(item["transaction_id"] for item in selected_debits if amount(item) > threshold)
    elif largest_debit:
        r1_refs.append(largest_debit["transaction_id"])
    if r1["status"] == "not_evaluated":
        r1_text = (
            f"{RULE_LABELS['R1']} (R1) checks whether one debit is greater than 50% of salary credits in {month_name}. "
            "No salary credits were recorded for the month, so a salary-relative threshold could not be calculated "
            "and R1 was not evaluated."
        )
    else:
        threshold = selected_salary * Decimal("0.5")
        if largest_debit:
            largest_amount = amount(largest_debit)
            description = largest_debit["description"] or "No description"
            observed = (
                f"The largest debit was {_money(largest_amount)} "
                f"(unverified transaction description: \"{description}\") and {_comparison_text(largest_amount, threshold)}."
            )
        else:
            observed = "No debits were recorded in the selected month."
        if r1["status"] == "triggered":
            hit_text = ", ".join(
                f"{item['transaction_id']} for {_money(amount(item))}"
                for item in selected_debits if amount(item) > threshold
            )
            outcome = f"R1 triggered because these debit(s) were strictly greater than the threshold: {hit_text}."
        else:
            outcome = "R1 did not trigger because no debit was greater than 50% of salary credits."
        r1_text = (
            f"{RULE_LABELS['R1']} (R1) checks whether one debit is greater than 50% of salary credits in {month_name}. "
            f"Salary credits were {_money(selected_salary)}, so the threshold was {_money(threshold)}. {observed} {outcome}"
        )
    r1.update(explanation=r1_text, evidence_references=r1_refs)

    # R2
    r2 = by_rule["R2"]
    r2_refs = ["metrics.selected_month_spending", "metrics.preceding_three_month_average"]
    if r2["status"] == "insufficient_history":
        covered = {_transaction_month(item) for item in transactions}
        missing = [month for month in preceding_months if month not in covered]
        r2_text = (
            f"{RULE_LABELS['R2']} (R2) checks whether {month_name} total debits are more than 1.5 times the "
            "average monthly debits of the three preceding calendar months (more than 50% above that average). "
            f"The required comparison period is {_period_display(preceding_months)}, but coverage is missing for "
            f"{', '.join(_month_display(month) for month in missing)}. R2 was not evaluated; missing months were not treated as zero spending."
        )
    else:
        threshold = baseline_average * Decimal("1.5")
        comparison = _comparison_text(selected_spending, threshold)
        outcome = (
            "R2 triggered because total debits were strictly greater than 1.5 times the three-month average."
            if r2["status"] == "triggered" else
            "R2 did not trigger because total debits were not greater than 1.5 times the three-month average."
        )
        r2_text = (
            f"{RULE_LABELS['R2']} (R2) compares {month_name} total debits with {_period_display(preceding_months)}. "
            f"Total debits were {_money(selected_spending)}. Average monthly debits during those three months were "
            f"{_money(baseline_average)}, making the 1.5-times threshold {_money(threshold)} (50% above the historical average). "
            f"The selected-month total {comparison}. {outcome} This comparison alone does not establish unaffordability or fraud."
        )
    r2.update(explanation=r2_text, evidence_references=r2_refs)

    # R3
    r3 = by_rule["R3"]
    difference = selected_spending - selected_salary
    if difference > ZERO:
        cash_position = f"Debits exceeded recorded salary credits by {_money(difference)}, triggering R3."
    elif difference == ZERO:
        cash_position = "Debits equaled recorded salary credits, so R3 did not trigger."
    else:
        cash_position = f"Recorded salary credits exceeded debits by {_money(-difference)}, so R3 did not trigger."
    r3_text = (
        f"{RULE_LABELS['R3']} (R3) compares recorded debits with recorded salary credits for {month_name}. "
        f"Debits were {_money(selected_spending)} and salary credits were {_money(selected_salary)}. {cash_position} "
        "This comparison does not establish the customer's savings balance, solvency, or other income."
    )
    r3.update(
        explanation=r3_text,
        evidence_references=["metrics.selected_month_spending", "metrics.selected_month_salary", "metrics.income_minus_spending"],
    )

    transfers = sorted(
        [item for item in transactions if item["direction"] == "debit" and item["transaction_type"] == "transfer"],
        key=_timestamp,
    )
    selected_transfers = [item for item in transfers if _transaction_month(item) == selected_month]
    earlier_transfers = [item for item in transfers if _transaction_month(item) < selected_month]
    seen = {item.get("recipient_id") for item in earlier_transfers if item.get("recipient_id")}
    eligible = [item for item in selected_transfers if item.get("recipient_id") and item.get("recipient_id") not in seen]

    # R4
    r4 = by_rule["R4"]
    r4_refs: list[str] = []
    if r4["status"] == "triggered":
        finding = findings_by_rule["R4"][0]
        amounts = [Decimal(str(item["amount"])) for item in finding["evidence"]]
        amount_phrase = ", ".join(_money(value) for value in amounts)
        elapsed = Decimal(str(finding["elapsed_minutes"]))
        r4_text = (
            f"{RULE_LABELS['R4']} (R4) checks for at least five outgoing transfers to the same recipient, absent from "
            f"earlier available outgoing-transfer records, within an inclusive 30-minute period beginning in {month_name}. "
            f"Recipient {finding['recipient_id']} received {finding['transfer_count']} transfers ({amount_phrase}), totaling "
            f"{_money(Decimal(str(finding['total_amount'])))}. They ran from {_time_display(finding['window_start'])} to "
            f"{_time_display(finding['window_end'])}, an elapsed {elapsed:g} minutes. This meets the count and time requirements, "
            "so R4 triggered. New to the available records does not necessarily mean unknown to the customer."
        )
        r4_refs = finding["transaction_ids"]
    elif not eligible:
        r4_text = (
            f"{RULE_LABELS['R4']} (R4) checks for at least five transfers to the same recipient that was absent from "
            f"earlier available outgoing-transfer records, within 30 minutes. No eligible new-recipient transfer began in "
            f"{month_name}, so R4 did not trigger. Absence from the records is not proof about whether the customer knows a recipient."
        )
    else:
        windows = []
        for first in eligible:
            end = _timestamp(first) + timedelta(minutes=30)
            window = [item for item in transfers if item.get("recipient_id") == first.get("recipient_id") and _timestamp(first) <= _timestamp(item) <= end]
            windows.append((len(window), _timestamp(first), first["recipient_id"], window))
        count, _, recipient, closest = max(windows, key=lambda item: (item[0], -item[1].timestamp()))
        r4_refs = [item["transaction_id"] for item in closest]
        r4_text = (
            f"{RULE_LABELS['R4']} (R4) requires at least five transfers to the same new-to-records recipient within an "
            f"inclusive 30-minute period. The closest observed eligible pattern in {month_name} was {count} transfer(s) to "
            f"{recipient}, from {_time_display(closest[0]['timestamp'])} to {_time_display(closest[-1]['timestamp'])}. "
            f"R4 did not trigger because {count} is fewer than five. New to the available records does not necessarily mean unknown to the customer."
        )
    r4.update(explanation=r4_text, evidence_references=r4_refs)

    # R5
    r5 = by_rule["R5"]
    earlier_months = sorted({_transaction_month(item) for item in earlier_transfers})
    r5_refs: list[str] = []
    if r5["status"] == "insufficient_history":
        r5_text = (
            f"{RULE_LABELS['R5']} (R5) checks whether a {month_name} transfer to a recipient absent from earlier records is strictly "
            "greater than both $5,000 and five times the median earlier outgoing transfer. "
            f"Only {len(earlier_transfers)} earlier outgoing transfer(s) were available during {_period_display(earlier_months)}; "
            "at least five are required to calculate the historical comparison. R5 was not evaluated."
        )
    else:
        transfer_median = median([amount(item) for item in earlier_transfers])
        median_threshold = Decimal("5") * transfer_median
        if r5["status"] == "triggered":
            finding = findings_by_rule["R5"][0]
            candidate = next(item for item in eligible if item["transaction_id"] == finding["transaction_ids"][0])
            candidate_amount = amount(candidate)
            r5_refs = [candidate["transaction_id"]]
            r5_text = (
                f"{RULE_LABELS['R5']} (R5) checks whether a {month_name} transfer to a recipient absent from earlier available records "
                "is strictly greater than both $5,000 and five times the historical median. "
                f"The {_money(candidate_amount)} transfer to {candidate['recipient_id']} exceeded $5,000 and {_money(median_threshold)}. "
                f"The second amount is five times the {_money(transfer_median)} median of {len(earlier_transfers)} outgoing "
                f"transfers recorded during {_period_display(earlier_months)}. Both strict comparisons were satisfied, so R5 triggered. "
                "New to the available records does not necessarily mean unknown to the customer."
            )
        elif not eligible:
            r5_text = (
                f"{RULE_LABELS['R5']} (R5) requires a selected-month transfer to a recipient absent from earlier records, "
                f"strictly above both $5,000 and {_money(median_threshold)}. The latter is five times the {_money(transfer_median)} "
                f"median of {len(earlier_transfers)} transfers during {_period_display(earlier_months)}. No eligible new-recipient "
                f"transfer occurred in {month_name}, so R5 did not trigger."
            )
        else:
            candidate = max(eligible, key=amount)
            candidate_amount = amount(candidate)
            r5_refs = [candidate["transaction_id"]]
            fixed_result = "greater than" if candidate_amount > Decimal("5000") else ("equal to" if candidate_amount == Decimal("5000") else "below")
            median_result = "greater than" if candidate_amount > median_threshold else ("equal to" if candidate_amount == median_threshold else "below")
            failed = []
            if candidate_amount <= Decimal("5000"):
                failed.append("the requirement to be strictly greater than $5,000")
            if candidate_amount <= median_threshold:
                failed.append(f"the requirement to be strictly greater than {_money(median_threshold)}")
            failed_text = " and ".join(failed)
            r5_text = (
                f"{RULE_LABELS['R5']} (R5) requires a {month_name} new-to-records transfer to be strictly greater than both $5,000 and "
                f"{_money(median_threshold)}, which is five times the {_money(transfer_median)} median of {len(earlier_transfers)} "
                f"outgoing transfers during {_period_display(earlier_months)}. The largest eligible transfer was "
                f"{_money(candidate_amount)} to {candidate['recipient_id']}; it was {fixed_result} $5,000 and {median_result} "
                f"the five-times-median amount. R5 did not trigger because it failed {failed_text}."
            )
    r5.update(explanation=r5_text, evidence_references=r5_refs)

    for finding in findings:
        evaluation = by_rule[finding["rule_id"]]
        finding["rule_label"] = evaluation["rule_label"]
        finding["advisor_explanation"] = evaluation["explanation"]
        finding["evidence_references"] = evaluation["evidence_references"]
        if finding["rule_id"] == "R1":
            debits = ", ".join(
                f"{transaction_id} ({_money(Decimal(str(value)))})"
                for transaction_id, value in zip(finding["transaction_ids"], finding["triggering_amounts"])
            )
            finding["what_happened"] = (
                f"In {month_name}, salary credits were {_money(Decimal(str(finding['salary_basis'])))} and the 50% "
                f"threshold was {_money(Decimal(str(finding['threshold_amount'])))}. Triggering debit(s): {debits}."
            )
            finding["why_flagged"] = "Each listed debit was strictly greater than 50% of selected-month salary credits."
        elif finding["rule_id"] == "R2":
            finding["what_happened"] = (
                f"{month_name} debits were {_money(Decimal(str(finding['selected_month_spending'])))} versus average "
                f"monthly debits of {_money(Decimal(str(finding['baseline_average'])))} during {_period_display(preceding_months)}."
            )
            finding["why_flagged"] = (
                f"Total debits exceeded the 1.5-times threshold of "
                f"{_money(Decimal(str(finding['threshold_amount'])))} (more than 50% above the three-month average)."
            )
        elif finding["rule_id"] == "R3":
            finding["what_happened"] = (
                f"{month_name} debits were {_money(Decimal(str(finding['selected_month_spending'])))} against "
                f"salary credits of {_money(Decimal(str(finding['selected_month_salary'])))}."
            )
            finding["why_flagged"] = (
                f"Recorded debits exceeded recorded salary credits by {_money(Decimal(str(finding['shortfall'])))}."
            )
        elif finding["rule_id"] == "R4":
            finding["what_happened"] = (
                f"Recipient {finding['recipient_id']} received {finding['transfer_count']} outgoing transfers totaling "
                f"{_money(Decimal(str(finding['total_amount'])))} from {_time_display(finding['window_start'])} to "
                f"{_time_display(finding['window_end'])}."
            )
            finding["why_flagged"] = (
                f"The recipient was absent from earlier available outgoing-transfer records, and at least five transfers "
                f"occurred within {Decimal(str(finding['elapsed_minutes'])):g} minutes, inside the inclusive 30-minute limit."
            )
        elif finding["rule_id"] == "R5":
            five_times = Decimal(str(finding["baseline_transfer_median"])) * Decimal("5")
            finding["what_happened"] = (
                f"A {_money(Decimal(str(finding['amount'])))} transfer was sent to recipient {finding['recipient_id']}, "
                f"which was absent from earlier available outgoing-transfer records."
            )
            finding["why_flagged"] = (
                f"The transfer was strictly greater than both $5,000.00 and {_money(five_times)}, which is five times "
                f"the {_money(Decimal(str(finding['baseline_transfer_median'])))} median of "
                f"{finding['historical_transfer_count']} earlier outgoing transfers."
            )


def _evaluate_transfer_risks(transactions: list[dict], analysis_month: str) -> tuple[list[dict], dict, dict]:
    """Evaluate R4/R5 without allowing future data into history."""
    validate_month(analysis_month)
    transfers = sorted(
        [t for t in transactions if t["direction"] == "debit" and t["transaction_type"] == "transfer"],
        key=_timestamp,
    )
    selected_transfers = [t for t in transfers if _transaction_month(t) == analysis_month]
    earlier_transfers = [t for t in transfers if _transaction_month(t) < analysis_month]
    earlier_months = sorted({_transaction_month(t) for t in earlier_transfers})
    seen_recipients = {t.get("recipient_id") for t in earlier_transfers if t.get("recipient_id")}
    # "New" is evaluated only against history before the selected month. Every
    # selected-month transfer to such a recipient can therefore begin an R4
    # window or satisfy R5; earlier selected-month transfers do not turn the
    # recipient into historical data.
    new_recipient_transfers = [
        transaction for transaction in selected_transfers
        if transaction.get("recipient_id") and transaction.get("recipient_id") not in seen_recipients
    ]

    r4_findings: list[dict] = []
    qualifying_recipients: set[str] = set()
    for first_transaction in new_recipient_transfers:
        recipient = first_transaction["recipient_id"]
        if recipient in qualifying_recipients:
            continue
        start = _timestamp(first_transaction)
        end = start + timedelta(minutes=30)
        # The first transfer determines selected-month attribution. Later transfers
        # in the inclusive window may fall in the next calendar month.
        window = [
            transaction for transaction in transfers
            if transaction.get("recipient_id") == recipient
            and start <= _timestamp(transaction) <= end
        ]
        if len(window) >= 5:
            qualifying_recipients.add(recipient)
            r4_findings.append({
                "rule_id": "R4",
                "description": (
                    "At least five transfers to a previously unseen recipient occurred within an inclusive "
                    "30-minute window. The finding is attributed to the month of the first transfer; the window "
                    "may cross a month boundary. Verify with the client."
                ),
                "recipient_id": recipient,
                "transfer_count": len(window),
                "window_start": window[0]["timestamp"],
                "window_end": window[-1]["timestamp"],
                "elapsed_minutes": float((_timestamp(window[-1]) - _timestamp(window[0])).total_seconds() / 60),
                "crosses_month_boundary": _transaction_month(window[-1]) != analysis_month,
                "total_amount": float(sum((amount(t) for t in window), ZERO)),
                "transaction_ids": [t["transaction_id"] for t in window],
                "evidence": [_evidence(t) for t in window],
            })
    if r4_findings:
        r4_evaluation = _rule_evaluation(
            "R4", "triggered",
            f"{len(r4_findings)} new-recipient transfer pattern(s) met the requirement of at least five transfers within an inclusive 30-minute period beginning in {analysis_month}.",
            baseline_period=earlier_months,
            baseline_count=len(earlier_transfers),
        )
    else:
        r4_evaluation = _rule_evaluation(
            "R4", "not_triggered",
            "No new recipient had at least five outgoing transfers in an inclusive 30-minute window beginning in the selected month.",
            baseline_period=earlier_months,
            baseline_count=len(earlier_transfers),
        )

    r5_findings: list[dict] = []
    if len(earlier_transfers) < 5:
        r5_evaluation = _rule_evaluation(
            "R5", "insufficient_history",
            f"Only {len(earlier_transfers)} earlier outgoing transfer(s) were available; at least five are required.",
            baseline_period=earlier_months,
            baseline_count=len(earlier_transfers),
        )
    else:
        historical_amounts = [amount(t) for t in earlier_transfers]
        usual_amount = median(historical_amounts)
        for transaction in new_recipient_transfers:
            transfer_amount = amount(transaction)
            if transfer_amount > Decimal("5000") and transfer_amount > Decimal("5") * usual_amount:
                r5_findings.append({
                    "rule_id": "R5",
                    "description": (
                        "A selected-month transfer to a previously unseen recipient exceeds both strict "
                        "thresholds; verify purpose and authorization."
                    ),
                    "recipient_id": transaction["recipient_id"],
                    "amount": float(transfer_amount),
                    "historical_transfer_count": len(historical_amounts),
                    "baseline_transfer_median": float(usual_amount),
                    "amount_to_median_ratio": float(transfer_amount / usual_amount),
                    "transaction_ids": [transaction["transaction_id"]],
                    "evidence": [_evidence(transaction)],
                })
        if r5_findings:
            r5_evaluation = _rule_evaluation(
                "R5", "triggered",
                f"{len(r5_findings)} new-recipient transfer(s) exceeded both $5,000 and five times the earlier-transfer median (${usual_amount * Decimal('5')}).",
                baseline_period=earlier_months,
                baseline_count=len(earlier_transfers),
            )
        else:
            r5_evaluation = _rule_evaluation(
                "R5", "not_triggered",
                f"No selected-month new-recipient transfer was strictly greater than both $5,000 and five times the earlier-transfer median (${usual_amount * Decimal('5')}).",
                baseline_period=earlier_months,
                baseline_count=len(earlier_transfers),
            )
    return r4_findings + r5_findings, r4_evaluation, r5_evaluation


def detect_transfer_risks(transactions: list[dict], analysis_month: str) -> list[dict]:
    """Compatibility helper returning only triggered R4/R5 findings."""
    return _evaluate_transfer_risks(transactions, analysis_month)[0]


def analyze_financial_data(client: dict, analysis_month: str) -> dict:
    """Calculate selected-month metrics and evaluate rules R1 through R5."""
    validate_client_record(client)
    selected_month = validate_month(analysis_month)
    transactions = client["transactions"]
    covered_months = sorted({_transaction_month(t) for t in transactions})
    if selected_month not in covered_months:
        raise ValueError(f"Analysis month {selected_month} is outside this financial record's coverage.")

    selected = [t for t in transactions if _transaction_month(t) == selected_month]
    selected_debits = [t for t in selected if t["direction"] == "debit"]
    selected_spending = sum((amount(t) for t in selected_debits), ZERO)
    salary_transactions = [t for t in selected if t["direction"] == "credit" and t["transaction_type"] == "salary"]
    selected_salary = sum((amount(t) for t in salary_transactions), ZERO)
    findings: list[dict] = []
    evaluations: list[dict] = []

    r1_threshold: Decimal | None = None
    if selected_salary <= ZERO:
        evaluations.append(_rule_evaluation(
            "R1", "not_evaluated",
            "Selected-month salary credits were zero or unavailable, so the 50%-of-salary debit threshold was not calculated.",
            baseline_period=[selected_month],
            baseline_count=len(salary_transactions),
        ))
    else:
        r1_threshold = selected_salary * Decimal("0.5")
        large_debits = [t for t in selected_debits if amount(t) > r1_threshold]
        if large_debits:
            findings.append({
                "rule_id": "R1",
                "description": (
                    f"Individual debit in {selected_month} strictly greater than 50% of that month's salary credits."
                ),
                "salary_basis": float(selected_salary),
                "salary_transaction_ids": [t["transaction_id"] for t in salary_transactions],
                "threshold_percentage": 50.0,
                "threshold_amount": float(r1_threshold),
                "transaction_ids": [t["transaction_id"] for t in large_debits],
                "triggering_amounts": [float(amount(t)) for t in large_debits],
                "evidence": {
                    "salary_credits": [_evidence(t) for t in salary_transactions],
                    "triggering_debits": [_evidence(t) for t in large_debits],
                },
            })
            evaluations.append(_rule_evaluation(
                "R1", "triggered",
                f"{len(large_debits)} debit(s) strictly exceeded 50% of selected-month salary credits "
                f"(salary basis ${selected_salary}, threshold ${r1_threshold}).",
                baseline_period=[selected_month],
                baseline_count=len(salary_transactions),
            ))
        else:
            evaluations.append(_rule_evaluation(
                "R1", "not_triggered",
                f"No selected-month debit strictly exceeded 50% of salary credits "
                f"(salary basis ${selected_salary}, threshold ${r1_threshold}).",
                baseline_period=[selected_month],
                baseline_count=len(salary_transactions),
            ))

    preceding_months = [shift_month(selected_month, offset) for offset in (-3, -2, -1)]
    missing_baseline_months = [month for month in preceding_months if month not in covered_months]
    baseline_average: Decimal | None = None
    if missing_baseline_months:
        evaluations.append(_rule_evaluation(
            "R2", "insufficient_history",
            "All three immediately preceding calendar months are required; missing coverage for " + ", ".join(missing_baseline_months) + ".",
            baseline_period=preceding_months,
            baseline_count=3 - len(missing_baseline_months),
        ))
    else:
        baseline_totals = [
            sum((amount(t) for t in transactions if _transaction_month(t) == month and t["direction"] == "debit"), ZERO)
            for month in preceding_months
        ]
        baseline_average = sum(baseline_totals, ZERO) / Decimal("3")
        if selected_spending > Decimal("1.5") * baseline_average:
            findings.append({
                "rule_id": "R2",
                "description": (
                    f"{selected_month} spending strictly exceeds 1.5 times the average for "
                    f"{preceding_months[0]} through {preceding_months[-1]}."
                ),
                "selected_month_spending": float(selected_spending),
                "baseline_average": float(baseline_average),
                "threshold_amount": float(Decimal("1.5") * baseline_average),
                "transaction_ids": [t["transaction_id"] for t in selected_debits],
                "evidence": {
                    "baseline_month_totals": [
                        {"month": month, "debit_total": float(total)}
                        for month, total in zip(preceding_months, baseline_totals)
                    ],
                    "selected_month_debits": [_evidence(t) for t in selected_debits],
                },
            })
            evaluations.append(_rule_evaluation(
                "R2", "triggered",
                "Selected-month spending strictly exceeded 1.5 times the rolling three-month average.",
                baseline_period=preceding_months,
                baseline_count=3,
            ))
        else:
            evaluations.append(_rule_evaluation(
                "R2", "not_triggered",
                "Selected-month spending did not strictly exceed 1.5 times the rolling three-month average.",
                baseline_period=preceding_months,
                baseline_count=3,
            ))

    if selected_spending > selected_salary:
        findings.append({
            "rule_id": "R3",
            "description": f"{selected_month} spending strictly exceeds salary credits in that month.",
            "selected_month_spending": float(selected_spending),
            "selected_month_salary": float(selected_salary),
            "shortfall": float(selected_spending - selected_salary),
            "transaction_ids": [t["transaction_id"] for t in selected_debits + salary_transactions],
            "evidence": {
                "salary_credits": [_evidence(t) for t in salary_transactions],
                "selected_month_debits": [_evidence(t) for t in selected_debits],
            },
        })
        evaluations.append(_rule_evaluation("R3", "triggered", "Selected-month spending strictly exceeded selected-month salary credits."))
    else:
        evaluations.append(_rule_evaluation("R3", "not_triggered", "Selected-month spending did not strictly exceed selected-month salary credits."))

    transfer_findings, r4_evaluation, r5_evaluation = _evaluate_transfer_risks(transactions, selected_month)
    findings.extend(transfer_findings)
    evaluations.extend([r4_evaluation, r5_evaluation])
    _enrich_explanations(
        transactions=transactions,
        selected_month=selected_month,
        selected_debits=selected_debits,
        selected_salary=selected_salary,
        salary_transactions=salary_transactions,
        selected_spending=selected_spending,
        preceding_months=preceding_months,
        baseline_average=baseline_average,
        evaluations=evaluations,
        findings=findings,
    )
    return {
        "client_id": client["client_id"],
        "currency": client["currency"],
        "analysis_month": selected_month,
        "metrics": {
            "selected_month_spending": float(selected_spending),
            "selected_month_salary": float(selected_salary),
            "r1_debit_threshold": float(r1_threshold) if r1_threshold is not None else None,
            "income_minus_spending": float(selected_salary - selected_spending),
            "preceding_three_month_average": round(float(baseline_average), 2) if baseline_average is not None else None,
        },
        "rule_evaluations": evaluations,
        "findings": findings,
    }
