"""Validated application data models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataBundle(StrictModel):
    client_id: str
    financial_status: Literal["ok", "not_found", "error"]
    crm_status: Literal["ok", "not_found", "error"]
    financial_data: dict[str, Any] | None = None
    crm_data: dict[str, Any] | None = None
    financial_message: str = ""
    crm_message: str = ""


class SourceAvailability(StrictModel):
    financial: Literal["available", "missing", "error"]
    crm: Literal["available", "missing", "error"]


class FinancialMetrics(StrictModel):
    currency: str
    selected_month_spending: float
    selected_month_salary: float
    r1_debit_threshold: float | None = None
    income_minus_spending: float
    preceding_three_month_average: float | None


class RuleEvaluation(StrictModel):
    rule_id: Literal["R1", "R2", "R3", "R4", "R5"]
    rule_label: str
    status: Literal["triggered", "not_triggered", "insufficient_history", "not_evaluated"]
    reason: str
    explanation: str
    evidence_references: list[str]
    baseline_period: list[str]
    baseline_count: int


class BriefingFinding(StrictModel):
    rule_id: Literal["R1", "R2", "R3", "R4", "R5"]
    rule_label: str
    summary: str
    what_happened: str
    why_flagged: str
    evidence_references: list[str] = Field(min_length=1)
    crm_context: str
    uncertainty: str
    advisor_follow_up_question: str


class RelevantCRMContext(StrictModel):
    risk_tolerance: str
    financial_goals: str
    advisor_context: str
    interpretation: str
    uncertainty: str
    history_limitation: str


class NarrativeFinding(StrictModel):
    rule_id: Literal["R1", "R2", "R3", "R4", "R5"]
    crm_context: str
    uncertainty: str
    advisor_follow_up_question: str


class BriefingNarrative(StrictModel):
    client_id: str
    analysis_month: str
    executive_summary: str
    findings: list[NarrativeFinding]
    relevant_crm_context: RelevantCRMContext
    advisor_follow_up_questions: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    human_review_recommended: bool


class Briefing(StrictModel):
    client_id: str
    analysis_month: str
    source_availability: SourceAvailability
    executive_summary: str
    financial_metrics: FinancialMetrics
    rule_evaluations: list[RuleEvaluation] = Field(min_length=5, max_length=5)
    findings: list[BriefingFinding]
    relevant_crm_context: RelevantCRMContext
    advisor_follow_up_questions: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    human_review_recommended: bool


class MonthlyIndexEntry(StrictModel):
    month: str
    status: Literal["pending_review", "approved", "rejected", "superseded", "not_completed"]
    briefing_id: str = ""
    revision_id: str = ""
    output_path: str = ""
    triggered_rule_ids: list[str] = Field(default_factory=list)
    message: str = ""


class MultiMonthIndex(StrictModel):
    client_id: str
    run_id: str
    requested_months: list[str]
    completed_months: list[str]
    remaining_months: list[str]
    model_call_limit: int
    model_calls_used: int
    retrieval_count: int
    source_availability: SourceAvailability
    results: list[MonthlyIndexEntry]

