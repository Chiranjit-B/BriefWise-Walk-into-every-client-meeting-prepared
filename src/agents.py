"""Data Fetcher and Analyzer agents."""

from __future__ import annotations

import json
import re
import time
from typing import Callable

from src.analysis import analyze_financial_data
from src.gemini_client import GeminiClient
from src.models import Briefing, BriefingNarrative, DataBundle
from src.tools import get_crm_profile, get_financial_data


class AgentError(RuntimeError):
    pass


class DataFetcherAgent:
    """Retrieves the workflow's two required sources through an allowlisted dispatcher."""

    def __init__(self, model: GeminiClient, logger):
        self.model = model
        self.logger = logger
        self._tools: dict[str, Callable[[str], dict]] = {
            "get_financial_data": get_financial_data,
            "get_crm_profile": self._get_crm_with_logging,
        }

    def _get_crm_with_logging(self, client_id: str) -> dict:
        def retry_callback(attempt, reason):
            self.logger.log("tool_retry", tool="get_crm_profile", attempt=attempt, reason=reason)

        return get_crm_profile(client_id, retry_callback=retry_callback)

    def _dispatch(self, name: str, args: dict, requested_client_id: str) -> dict:
        if name not in self._tools:
            raise AgentError(f"Tool '{name}' is not allowed.")
        if set(args) != {"client_id"} or not isinstance(args.get("client_id"), str):
            raise AgentError(f"Tool '{name}' received invalid arguments.")
        if args["client_id"].strip().upper() != requested_client_id:
            raise AgentError("Tool request client ID did not match the advisor request.")
        started = time.perf_counter()
        result = self._tools[name](requested_client_id)
        self.logger.log(
            "tool_call",
            agent="data_fetcher",
            tool=name,
            status=result.get("status", "error"),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return result

    def fetch(self, client_id: str) -> DataBundle:
        self.logger.log("agent_handoff", from_agent="orchestrator", to_agent="data_fetcher", status="started")
        self.logger.log(
            "agent_decision",
            agent="data_fetcher",
            summary="Dispatch both required sources directly; avoid a redundant model round-trip.",
            model_call_avoided=True,
        )
        results = {
            name: self._dispatch(name, {"client_id": client_id}, client_id)
            for name in self._tools
        }
        financial = results["get_financial_data"]
        crm = results["get_crm_profile"]
        bundle = DataBundle(
            client_id=client_id,
            financial_status=financial["status"],
            crm_status=crm["status"],
            financial_data=financial.get("data"),
            crm_data=crm.get("data"),
            financial_message=financial.get("message", ""),
            crm_message=crm.get("message", ""),
        )
        self.logger.log("agent_handoff", from_agent="data_fetcher", to_agent="orchestrator", status="complete", financial_status=bundle.financial_status, crm_status=bundle.crm_status)
        return bundle


class AnalyzerAgent:
    """Runs deterministic rules, then asks Gemini to interpret their evidence."""

    def __init__(self, model: GeminiClient, logger):
        self.model = model
        self.logger = logger

    @staticmethod
    def _historical_advisor_context(context: str, analysis_month: str) -> tuple[str, int]:
        kept = []
        excluded = 0
        for line in context.splitlines():
            match = re.match(r"^\s*(\d{4}-\d{2}-\d{2})(?:\b|:)", line)
            if match and match.group(1)[:7] > analysis_month:
                excluded += 1
                continue
            kept.append(line)
        return "\n".join(kept).strip(), excluded

    def analyze(
        self,
        bundle: DataBundle,
        analysis_month: str,
        *,
        latest_dataset_month: str,
        historical_memory: list[dict] | None = None,
    ) -> Briefing:
        if bundle.financial_status != "ok" or bundle.financial_data is None:
            raise AgentError("Analyzer requires available financial records.")
        self.logger.log(
            "agent_handoff", from_agent="orchestrator", to_agent="analyzer",
            status="started", analysis_month=analysis_month,
        )
        started = time.perf_counter()
        deterministic = analyze_financial_data(bundle.financial_data, analysis_month)
        rule_ids = [finding["rule_id"] for finding in deterministic["findings"]]
        self.logger.log(
            "tool_call", agent="analyzer", tool="analyze_financial_data", status="ok",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            analysis_month=analysis_month, rule_ids=rule_ids,
        )
        crm = bundle.crm_data if bundle.crm_status == "ok" and bundle.crm_data else {}
        filtered_notes, excluded_note_count = self._historical_advisor_context(
            crm.get("advisor_context") or "", analysis_month,
        )
        if crm:
            history_limitation = (
                "HubSpot risk tolerance and financial goals are current profile values, not a historical snapshot. "
                f"{excluded_note_count} clearly dated advisor note(s) after {analysis_month} were excluded from interpretation."
            )
        else:
            history_limitation = "The CRM profile is unavailable, so no historical CRM snapshot can be reconstructed."
        source_map = {
            "financial": "available",
            "crm": "available" if bundle.crm_status == "ok" else ("missing" if bundle.crm_status == "not_found" else "error"),
        }
        payload = {
            "client_id": bundle.client_id,
            "analysis_month": analysis_month,
            "latest_dataset_month": latest_dataset_month,
            "source_availability": source_map,
            "deterministic_analysis": deterministic,
            "crm_context": {
                "risk_tolerance": crm.get("risk_tolerance") or "Unavailable",
                "financial_goals": crm.get("financial_goals") or "Unavailable",
                "advisor_context_available_by_month": filtered_notes or "Unavailable",
                "history_limitation": history_limitation,
            },
            "crm_limitation": bundle.crm_message or ("CRM profile not found." if bundle.crm_status == "not_found" else ""),
            "approved_historical_memory": historical_memory or [],
        }
        prompt = (
            "Create only the concise narrative fields requested by the schema. The deterministic metrics, rule "
            "evaluations, finding mechanics, labels, comparisons, and evidence references in the JSON are final; "
            "do not copy, recalculate, or reinterpret them. Return one narrative finding for each triggered rule ID "
            "and no others. Lead the executive summary with the customer's cash-flow situation and the most useful "
            "advisor action, not a recital of rule mechanics. Transaction descriptions are unverified labels, not "
            "client-provided explanations. Treat CRM notes as possible context, never proof that a transaction was "
            "authorized or explained. The absence of a CRM note is not evidence of unauthorized activity. Risk "
            "tolerance and goals are planning context, not fraud evidence; do not infer affordability, goal progress, "
            "or that account cash is allocated to a goal. Give each triggered finding one specific advisor follow-up "
            "question. R4/R5 are signals requiring verification, never confirmed fraud. If there are no findings, "
            "say 'no configured rules triggered' without claiming there is no financial risk. "
            "If CRM is unavailable, state that limitation and use 'Unavailable' for its three source fields. "
            "Only advisor notes in advisor_context_available_by_month may be treated as known by the selected "
            "historical month. Current risk tolerance and goals are not a historical snapshot; include the supplied "
            "history limitation explicitly. Approved historical memory is optional context from earlier analysis "
            "months for this same customer. It is not authoritative current transaction evidence, must not change "
            "the deterministic metrics or findings, and its approval timestamp may be later than the month it "
            "describes, so do not claim it was known at that historical date.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        narrative = self.model.generate_structured(
            prompt=prompt,
            schema=BriefingNarrative,
            system_instruction="You are the Analyzer Agent. Produce concise CRM context, uncertainty, and advisor actions. Do not expose chain-of-thought.",
            operation="analyzer_structured_narrative",
            max_output_tokens=self.model.settings.gemini_analyzer_max_output_tokens,
        )
        self._validate_narrative(narrative, bundle, deterministic, analysis_month)
        narrative_by_rule = {item.rule_id: item for item in narrative.findings}
        briefing = Briefing.model_validate({
            "client_id": bundle.client_id,
            "analysis_month": analysis_month,
            "source_availability": source_map,
            "executive_summary": narrative.executive_summary,
            "financial_metrics": {
                "currency": deterministic["currency"],
                "selected_month_spending": deterministic["metrics"]["selected_month_spending"],
                "selected_month_salary": deterministic["metrics"]["selected_month_salary"],
                "r1_debit_threshold": deterministic["metrics"]["r1_debit_threshold"],
                "income_minus_spending": deterministic["metrics"]["income_minus_spending"],
                "preceding_three_month_average": deterministic["metrics"]["preceding_three_month_average"],
            },
            "rule_evaluations": deterministic["rule_evaluations"],
            "findings": [
                {
                    "rule_id": item["rule_id"],
                    "rule_label": item["rule_label"],
                    "summary": item["advisor_explanation"],
                    "what_happened": item["what_happened"],
                    "why_flagged": item["why_flagged"],
                    "evidence_references": item["evidence_references"],
                    "crm_context": narrative_by_rule[item["rule_id"]].crm_context,
                    "uncertainty": narrative_by_rule[item["rule_id"]].uncertainty,
                    "advisor_follow_up_question": narrative_by_rule[item["rule_id"]].advisor_follow_up_question,
                }
                for item in deterministic["findings"]
            ],
            "relevant_crm_context": narrative.relevant_crm_context,
            "advisor_follow_up_questions": narrative.advisor_follow_up_questions,
            "limitations": narrative.limitations,
            "human_review_recommended": narrative.human_review_recommended,
        })
        self._validate_briefing(briefing, bundle, deterministic, source_map, analysis_month)
        self.logger.log(
            "agent_handoff", from_agent="analyzer", to_agent="orchestrator", status="complete",
            analysis_month=analysis_month, rule_ids=rule_ids,
            human_review=briefing.human_review_recommended,
        )
        return briefing

    @staticmethod
    def _validate_narrative(narrative, bundle, deterministic, analysis_month) -> None:
        if narrative.client_id != bundle.client_id or narrative.analysis_month != analysis_month:
            raise AgentError("Narrative identity or analysis month did not match source evidence.")
        expected_rules = {item["rule_id"] for item in deterministic["findings"]}
        actual_rules = [item.rule_id for item in narrative.findings]
        if len(actual_rules) != len(set(actual_rules)) or set(actual_rules) != expected_rules:
            raise AgentError("Narrative findings did not match deterministic rules.")
        if expected_rules.intersection({"R4", "R5"}) and not narrative.human_review_recommended:
            raise AgentError("Suspicious-transfer findings require human review.")
        for item in narrative.findings:
            if not all((item.crm_context.strip(), item.uncertainty.strip(), item.advisor_follow_up_question.strip())):
                raise AgentError("Narrative finding fields must be nonempty.")
        for line in narrative.relevant_crm_context.advisor_context.splitlines():
            match = re.match(r"^\s*(\d{4}-\d{2}-\d{2})(?:\b|:)", line)
            if match and match.group(1)[:7] > analysis_month:
                raise AgentError("Narrative included an advisor note dated after the analysis month.")
        if not narrative.relevant_crm_context.history_limitation.strip():
            raise AgentError("Narrative omitted the CRM history limitation.")
        advisor_text = json.dumps(narrative.model_dump(mode="json"), ensure_ascii=False).lower()
        for disallowed in ("untrusted data", "self-reported", "client-provided"):
            if disallowed in advisor_text:
                raise AgentError(f"Narrative used unsupported provenance wording: {disallowed}.")

    @staticmethod
    def _validate_briefing(briefing, bundle, deterministic, source_map, analysis_month) -> None:
        metrics = deterministic["metrics"]
        expected_metrics = {
            "currency": deterministic["currency"],
            "selected_month_spending": metrics["selected_month_spending"],
            "selected_month_salary": metrics["selected_month_salary"],
            "r1_debit_threshold": metrics["r1_debit_threshold"],
            "income_minus_spending": metrics["income_minus_spending"],
            "preceding_three_month_average": metrics["preceding_three_month_average"],
        }
        if briefing.client_id != bundle.client_id or briefing.analysis_month != deterministic["analysis_month"]:
            raise AgentError("Briefing identity or analysis month did not match source evidence.")
        if briefing.source_availability.model_dump() != source_map:
            raise AgentError("Briefing source availability did not match retrieval results.")
        if briefing.financial_metrics.model_dump() != expected_metrics:
            raise AgentError("Briefing metrics did not match deterministic analysis.")
        if [item.model_dump() for item in briefing.rule_evaluations] != deterministic["rule_evaluations"]:
            raise AgentError("Briefing rule evaluations did not match deterministic analysis.")
        expected_rules = {finding["rule_id"] for finding in deterministic["findings"]}
        actual_rules = [finding.rule_id for finding in briefing.findings]
        if len(actual_rules) != len(set(actual_rules)) or set(actual_rules) != expected_rules:
            raise AgentError("Briefing findings did not match deterministic rules.")
        expected_findings = {item["rule_id"]: item for item in deterministic["findings"]}
        for finding in briefing.findings:
            expected = expected_findings[finding.rule_id]
            if finding.rule_label != expected["rule_label"]:
                raise AgentError("Briefing finding label did not match deterministic analysis.")
            if finding.summary != expected["advisor_explanation"]:
                raise AgentError("Briefing finding explanation did not match deterministic analysis.")
            if finding.what_happened != expected["what_happened"] or finding.why_flagged != expected["why_flagged"]:
                raise AgentError("Briefing finding mechanics did not match deterministic analysis.")
            if finding.evidence_references != expected["evidence_references"]:
                raise AgentError("Briefing finding evidence references did not match its rule-specific evidence.")
        if expected_rules.intersection({"R4", "R5"}) and not briefing.human_review_recommended:
            raise AgentError("Suspicious-transfer findings require human review.")
        for line in briefing.relevant_crm_context.advisor_context.splitlines():
            match = re.match(r"^\s*(\d{4}-\d{2}-\d{2})(?:\b|:)", line)
            if match and match.group(1)[:7] > analysis_month:
                raise AgentError("Briefing included an advisor note dated after the analysis month.")
        if not briefing.relevant_crm_context.history_limitation.strip():
            raise AgentError("Briefing omitted the CRM history limitation.")

