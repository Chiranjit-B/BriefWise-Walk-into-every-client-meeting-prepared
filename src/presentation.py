"""Advisor-facing presentation models built from validated briefing evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from src.models import Briefing, StrictModel


class EvidenceReference(StrictModel):
    reference: str
    kind: Literal["transaction", "metric", "analysis"]
    timestamp: str = ""
    amount: float | None = None
    direction: str = ""
    transaction_type: str = ""
    recipient_id: str = ""
    description: str = ""


class AttentionItem(StrictModel):
    rule_id: str
    title: str
    what_happened: str
    why_highlighted: str
    crm_context: str
    uncertainty: str
    advisor_question: str
    evidence: list[EvidenceReference]


class FinancialObservation(StrictModel):
    rule_id: str
    title: str
    status: Literal["not_triggered", "insufficient_history", "not_evaluated"]
    explanation: str


class FinancialOverview(StrictModel):
    currency: str
    salary: float
    total_debits: float
    cash_flow_amount: float
    cash_flow_label: Literal["Recorded surplus", "Recorded shortfall", "Recorded balance"]
    preceding_three_month_average: float | None


class AdvisorReview(StrictModel):
    client_id: str
    analysis_month: str
    month_label: str
    review_status: str
    revision_id: str = ""
    source_availability: dict[str, str]
    executive_summary: str
    financial_overview: FinancialOverview
    attention_items: list[AttentionItem]
    financial_observations: list[FinancialObservation]
    meeting_questions: list[str] = Field(min_length=1)
    crm_context: dict[str, str]
    limitations: list[str]
    suspicious_transfer_review_recommended: bool


def _month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def _evidence_lookup(financial_data: dict[str, Any] | None) -> dict[str, dict]:
    if not financial_data:
        return {}
    return {
        item["transaction_id"]: item
        for item in financial_data.get("transactions", [])
        if isinstance(item, dict) and isinstance(item.get("transaction_id"), str)
    }


def _evidence_item(reference: str, lookup: dict[str, dict]) -> EvidenceReference:
    if reference.startswith("metrics."):
        return EvidenceReference(reference=reference, kind="metric")
    transaction = lookup.get(reference)
    if transaction is None:
        return EvidenceReference(reference=reference, kind="analysis")
    return EvidenceReference(
        reference=reference,
        kind="transaction",
        timestamp=str(transaction.get("timestamp") or ""),
        amount=float(transaction["amount"]) if transaction.get("amount") is not None else None,
        direction=str(transaction.get("direction") or ""),
        transaction_type=str(transaction.get("transaction_type") or ""),
        recipient_id=str(transaction.get("recipient_id") or ""),
        description=str(transaction.get("description") or ""),
    )


def _deduplicated_questions(briefing: Briefing) -> list[str]:
    questions = [item.advisor_follow_up_question for item in briefing.findings]
    questions.extend(briefing.advisor_follow_up_questions)
    seen: set[str] = set()
    result = []
    for question in questions:
        normalized = question.strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result or ["Have the client's circumstances, priorities, or financial goals changed?"]


def build_advisor_review(
    briefing: Briefing,
    *,
    financial_data: dict[str, Any] | None = None,
    review_status: str = "pending_review",
    revision_id: str = "",
) -> AdvisorReview:
    """Convert a validated briefing into a meeting-ready view without recalculation."""
    lookup = _evidence_lookup(financial_data)
    metrics = briefing.financial_metrics
    if metrics.income_minus_spending > 0:
        cash_flow_label = "Recorded surplus"
    elif metrics.income_minus_spending < 0:
        cash_flow_label = "Recorded shortfall"
    else:
        cash_flow_label = "Recorded balance"

    attention_items = [
        AttentionItem(
            rule_id=finding.rule_id,
            title=finding.rule_label,
            what_happened=finding.what_happened,
            why_highlighted=finding.why_flagged,
            crm_context=finding.crm_context,
            uncertainty=finding.uncertainty,
            advisor_question=finding.advisor_follow_up_question,
            evidence=[_evidence_item(reference, lookup) for reference in finding.evidence_references],
        )
        for finding in briefing.findings
    ]
    triggered = {item.rule_id for item in briefing.findings}
    observations = [
        FinancialObservation(
            rule_id=evaluation.rule_id,
            title=evaluation.rule_label,
            status=evaluation.status,
            explanation=evaluation.explanation,
        )
        for evaluation in briefing.rule_evaluations
        if evaluation.rule_id not in triggered
    ]
    crm = briefing.relevant_crm_context
    limitations = list(dict.fromkeys([
        crm.history_limitation,
        *briefing.limitations,
    ]))
    return AdvisorReview(
        client_id=briefing.client_id,
        analysis_month=briefing.analysis_month,
        month_label=_month_label(briefing.analysis_month),
        review_status=review_status,
        revision_id=revision_id,
        source_availability=briefing.source_availability.model_dump(),
        executive_summary=briefing.executive_summary,
        financial_overview=FinancialOverview(
            currency=metrics.currency,
            salary=metrics.selected_month_salary,
            total_debits=metrics.selected_month_spending,
            cash_flow_amount=abs(metrics.income_minus_spending),
            cash_flow_label=cash_flow_label,
            preceding_three_month_average=metrics.preceding_three_month_average,
        ),
        attention_items=attention_items,
        financial_observations=observations,
        meeting_questions=_deduplicated_questions(briefing),
        crm_context={
            "risk_tolerance": crm.risk_tolerance,
            "financial_goals": crm.financial_goals,
            "advisor_context": crm.advisor_context,
            "interpretation": crm.interpretation,
            "uncertainty": crm.uncertainty,
            "history_limitation": crm.history_limitation,
        },
        limitations=limitations,
        suspicious_transfer_review_recommended=briefing.human_review_recommended,
    )
