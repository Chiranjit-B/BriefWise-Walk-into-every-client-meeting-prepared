"""Single offline-first verification entry point for the Wealth Advisor Assistant."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from scripts.check_r1_expectations import derive_r1
from scripts.evaluate_all_months import evaluate_all_months
from scripts.evaluate_offline import evaluate_fixture
from src.analysis import ANALYSIS_RULESET_VERSION, analyze_financial_data
from src.config import PROJECT_ROOT, Settings
from src.models import Briefing
from src.orchestrator import run_review
from src.state import StateStore
from src.tools import _validate_financial_dataset, get_financial_data, get_financial_dataset_months

STATUS_VALUES = {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}
DEFAULT_LIVE_CLIENTS = ["C001", "C004", "C010", "C022", "C025", "C005", "C002"]


def _load_live_expectations() -> dict[str, list[str]]:
    fixture = json.loads((PROJECT_ROOT / "data" / "fixtures" / "expected_findings.json").read_text(encoding="utf-8"))
    by_client = {item["client_id"]: item["expected_triggered_rule_ids"] for item in fixture["clients"]}
    return {client_id: by_client[client_id] for client_id in DEFAULT_LIVE_CLIENTS}


LIVE_EXPECTED = _load_live_expectations()

REQUIRED_TEST_COVERAGE = {
    "R1 boundaries and unavailable salary": (
        "test_r1_exactly_half_salary_does_not_trigger",
        "test_r1_above_half_salary_triggers_purchase_and_transfer",
        "test_r1_without_salary_is_not_evaluated",
    ),
    "History statuses and monthly isolation": (
        "test_early_month_r2_is_insufficient_not_passed",
        "test_future_transfers_do_not_satisfy_r5_history",
        "test_selected_month_excludes_future_transactions",
    ),
    "Factual rule explanations": (
        "test_every_rule_has_label_explanation_and_rule_specific_references",
        "test_r1_equality_and_trigger_explanations_use_personalized_threshold",
        "test_c025_r4_explains_inclusive_window_and_new_to_records_meaning",
        "test_c010_r5_names_both_distinct_strict_thresholds",
    ),
    "Briefing identity, metrics, evidence, and source validation": (
        "test_analyzer_assembles_rule_mechanics_and_references_deterministically",
        "test_future_dated_crm_notes_are_filtered_for_historical_month",
    ),
    "Mocked CRM missing/error behavior": (
        "test_transient_failure_retries_then_returns_not_found",
        "test_api_failure_is_distinct_from_not_found",
    ),
    "Mocked pacing, retries, and truncated output": (
        "test_four_per_minute_spaces_requests_by_fifteen_seconds",
        "test_429_honors_retry_after_and_paces_retry",
        "test_daily_quota_exhaustion_is_not_retried",
        "test_truncated_response_is_rejected",
        "test_model_call_logs_effective_output_cap",
    ),
    "Configured output-cap isolation": (
        "test_offline_environment_tests_preserve_configured_5000_analyzer_cap",
        "test_analyzer_passes_its_output_limit",
    ),
    "Session isolation and approved-memory retrieval": (
        "test_session_and_customer_isolation",
        "test_persistent_retrieval_across_store_instances",
        "test_only_approved_same_customer_earlier_month_is_retrieved",
    ),
    "Draft and revision lifecycle": (
        "test_rejection_does_not_create_memory",
        "test_amendment_preserves_immutable_evidence_and_original",
        "test_duplicate_approval_is_idempotent_and_superseded_memory_is_inactive",
        "test_pending_draft_is_not_approved_memory",
    ),
}


@dataclass
class CheckRow:
    check: str
    month: str
    expected: object
    actual: object
    status: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUS_VALUES:
            raise ValueError(f"Invalid verification status: {self.status}")


def _iter_tests(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def _run_unit_suite() -> tuple[unittest.result.TestResult, set[str], str]:
    suite = unittest.defaultTestLoader.discover(str(PROJECT_ROOT / "tests"), pattern="test*.py")
    test_ids = {test.id() for test in _iter_tests(suite)}
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return result, test_ids, stream.getvalue()


def _file_digest(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protected_snapshot() -> dict[str, str]:
    paths = [PROJECT_ROOT / "state" / "wealth_advisor.sqlite3"]
    paths.extend(sorted((PROJECT_ROOT / "outputs").glob("approved_*.json")))
    return {str(path): _file_digest(path) for path in paths}


def _dataset_and_explanation_checks(rows: list[CheckRow]) -> None:
    dataset_path = PROJECT_ROOT / "data" / "financial_data.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    clients = _validate_financial_dataset(dataset)
    actual_ids = sorted(item["client_id"] for item in clients)
    expected_ids = [f"C{number:03d}" for number in range(1, 26)]
    structure_ok = dataset.get("currency") == "USD" and actual_ids == expected_ids
    rows.append(CheckRow(
        "Financial dataset structure and customer IDs", "All",
        {"currency": "USD", "customer_ids": expected_ids},
        {"currency": dataset.get("currency"), "customer_ids": actual_ids},
        "PASS" if structure_ok else "FAIL",
        "Validated top-level structure, unique records, and each transaction contract." if structure_ok else "Dataset structure or IDs differ.",
    ))

    months = get_financial_dataset_months()
    explanation_failures: list[str] = []
    analyzed = 0
    for client_id in expected_ids:
        result = get_financial_data(client_id)
        if result["status"] != "ok":
            explanation_failures.append(f"{client_id}: source status {result['status']}")
            continue
        for month in months:
            analysis = analyze_financial_data(result["data"], month)
            analyzed += 1
            if analysis["client_id"] != client_id or analysis["analysis_month"] != month:
                explanation_failures.append(f"{client_id}/{month}: identity mismatch")
            if set(analysis["metrics"]) != {
                "selected_month_spending", "selected_month_salary", "r1_debit_threshold",
                "income_minus_spending", "preceding_three_month_average",
            }:
                explanation_failures.append(f"{client_id}/{month}: metric contract mismatch")
            evaluations = {item["rule_id"]: item for item in analysis["rule_evaluations"]}
            if set(evaluations) != {"R1", "R2", "R3", "R4", "R5"}:
                explanation_failures.append(f"{client_id}/{month}: missing rule evaluation")
                continue
            for evaluation in evaluations.values():
                explanation = evaluation["explanation"]
                if not evaluation["rule_label"] or f"({evaluation['rule_id']})" not in explanation:
                    explanation_failures.append(f"{client_id}/{month}/{evaluation['rule_id']}: label/explanation missing")
                if str(int(month[-2:])) not in explanation and month not in explanation and datetime.strptime(month, "%Y-%m").strftime("%B") not in explanation:
                    explanation_failures.append(f"{client_id}/{month}/{evaluation['rule_id']}: selected month not explained")
            for finding in analysis["findings"]:
                evaluation = evaluations[finding["rule_id"]]
                if finding.get("evidence_references") != evaluation["evidence_references"]:
                    explanation_failures.append(f"{client_id}/{month}/{finding['rule_id']}: rule-specific references differ")
                if not finding.get("what_happened") or not finding.get("why_flagged"):
                    explanation_failures.append(f"{client_id}/{month}/{finding['rule_id']}: presentation fields missing")
    rows.append(CheckRow(
        "All customer-month analyses and explanation contract", ", ".join(months),
        len(expected_ids) * len(months), analyzed,
        "PASS" if analyzed == len(expected_ids) * len(months) and not explanation_failures else "FAIL",
        "All deterministic analyses include five factual rule explanations and rule-specific finding references."
        if not explanation_failures else "; ".join(explanation_failures[:5]),
    ))

    all_month_summary = evaluate_all_months()
    rows.append(CheckRow(
        "All-month deterministic evaluation", ", ".join(months),
        {"customers": 25, "months": len(months)},
        {"customers": 25, "months": len(all_month_summary)},
        "PASS" if len(all_month_summary) == len(months) else "FAIL",
        "Used the existing all-month evaluator; no model or network calls.",
    ))

    fixture_result = evaluate_fixture()
    rows.append(CheckRow(
        "Current ruleset fixture", fixture_result["evaluation_month"],
        "All 25 client findings match fixture", fixture_result["triggered"],
        "PASS" if not fixture_result["failures"] else "FAIL",
        "; ".join(fixture_result["failures"]) or f"Ruleset {ANALYSIS_RULESET_VERSION} matched the evaluation fixture.",
    ))

    fixture = json.loads((PROJECT_ROOT / "data" / "fixtures" / "expected_findings.json").read_text(encoding="utf-8"))
    derived_r1 = derive_r1(dataset)
    r1_ok = derived_r1 == fixture["expected_monthly_r1"]
    rows.append(CheckRow(
        "Independent income-relative R1 fixture", ", ".join(months),
        fixture["expected_monthly_r1"], derived_r1,
        "PASS" if r1_ok else "FAIL",
        "Independent Decimal derivation matched all customer-month R1 boundaries." if r1_ok else "R1 derivation differs from fixture.",
    ))


def run_offline_verification() -> tuple[list[CheckRow], dict]:
    rows: list[CheckRow] = []
    before = _protected_snapshot()
    with tempfile.TemporaryDirectory(prefix="wealth-advisor-verify-") as temporary:
        temporary_root = Path(temporary)
        # This explicitly proves the verification state target is isolated and usable.
        StateStore(temporary_root / "state" / "verification.sqlite3", temporary_root / "outputs")
        unit_result, test_ids, unit_output = _run_unit_suite()
        rows.append(CheckRow(
            "Full unit test suite", "All", "All discovered tests pass",
            f"{unit_result.testsRun} run; {len(unit_result.failures)} failures; {len(unit_result.errors)} errors",
            "PASS" if unit_result.wasSuccessful() else "FAIL",
            "Existing tests ran with mocked external calls and temporary state." if unit_result.wasSuccessful() else "See unit_test_output in the JSON report.",
        ))
        for label, required_names in REQUIRED_TEST_COVERAGE.items():
            missing = [name for name in required_names if not any(test_id.endswith(name) for test_id in test_ids)]
            passed = unit_result.wasSuccessful() and not missing
            rows.append(CheckRow(
                label, "Offline", list(required_names), "Present and passed" if not missing else {"missing": missing},
                "PASS" if passed else "FAIL",
                "Covered by the existing offline suite." if passed else "Required test coverage is missing or the suite failed.",
            ))
        try:
            _dataset_and_explanation_checks(rows)
        except Exception as exc:
            rows.append(CheckRow("Dataset and deterministic evaluations", "All", "Successful", type(exc).__name__, "FAIL", str(exc)))

    after = _protected_snapshot()
    protected_ok = before == after
    rows.append(CheckRow(
        "Production state and approved-report isolation", "Offline", before, after,
        "PASS" if protected_ok else "FAIL",
        "Real approval database and approved reports were unchanged." if protected_ok else "A protected production artifact changed.",
    ))
    return rows, {
        "unit_test_output": unit_output,
        "unit_tests_run": unit_result.testsRun,
        "usage_totals": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "thought_tokens": 0, "total_tokens": 0},
        "artifacts": [],
    }


def _read_usage(log_path: Path) -> dict[str, int]:
    totals = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "thought_tokens": 0, "total_tokens": 0}
    if not log_path.exists():
        return totals
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "model_call":
            continue
        totals["model_calls"] += 1
        for key in ("input_tokens", "output_tokens", "thought_tokens", "total_tokens"):
            value = event.get(key)
            if isinstance(value, int):
                totals[key] += value
    return totals


def _terminal_provider_failure(log_path: Path, result_message: str) -> str:
    text = result_message.lower()
    if log_path.exists():
        text += " " + log_path.read_text(encoding="utf-8").lower()
    if any(marker in text for marker in ("authentication", "permission failed", "missing gemini_api_key")):
        return "authentication or permission failure"
    if "daily quota" in text:
        return "identified daily quota exhaustion"
    return ""


def _validate_live_draft(client_id: str, path: Path) -> tuple[list[str] | None, list[str]]:
    failures: list[str] = []
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    if wrapper.get("status") != "pending_review":
        failures.append("draft status is not pending_review")
    if wrapper.get("ruleset_version") != ANALYSIS_RULESET_VERSION:
        failures.append("ruleset version mismatch")
    briefing = Briefing.model_validate(wrapper.get("briefing"))
    financial = get_financial_data(client_id)
    if financial["status"] != "ok":
        return None, [f"financial source status {financial['status']}"]
    deterministic = analyze_financial_data(financial["data"], "2026-06")
    actual_findings = [item.rule_id for item in briefing.findings]
    expected_findings = LIVE_EXPECTED[client_id]
    if actual_findings != expected_findings:
        failures.append(f"expected findings {expected_findings}, got {actual_findings}")
    if briefing.client_id != client_id or briefing.analysis_month != "2026-06":
        failures.append("client identity or month mismatch")
    expected_metrics = {"currency": deterministic["currency"], **deterministic["metrics"]}
    if briefing.financial_metrics.model_dump() != expected_metrics:
        failures.append("financial metrics differ from deterministic analysis")
    if [item.model_dump() for item in briefing.rule_evaluations] != deterministic["rule_evaluations"]:
        failures.append("rule evaluations or explanations differ from deterministic analysis")
    deterministic_findings = {item["rule_id"]: item for item in deterministic["findings"]}
    for finding in briefing.findings:
        expected = deterministic_findings[finding.rule_id]
        if finding.evidence_references != expected["evidence_references"]:
            failures.append(f"{finding.rule_id} evidence references differ")
    expected_crm = "missing" if client_id == "C005" else "available"
    if briefing.source_availability.financial != "available" or briefing.source_availability.crm != expected_crm:
        failures.append(f"expected financial=available and crm={expected_crm}")
    if client_id == "C005":
        crm = briefing.relevant_crm_context
        if not all(value == "Unavailable" for value in (crm.risk_tolerance, crm.financial_goals, crm.advisor_context)):
            failures.append("missing CRM fields are not explicitly Unavailable")
        if not any("crm" in item.lower() for item in briefing.limitations):
            failures.append("missing CRM limitation was not explicit")
    if set(actual_findings).intersection({"R4", "R5"}) and not briefing.human_review_recommended:
        failures.append("suspicious-transfer finding lacks human-review recommendation")
    if client_id == "C022":
        r1 = next(item for item in briefing.rule_evaluations if item.rule_id == "R1")
        if r1.status != "not_triggered" or "equaled the threshold" not in r1.explanation:
            failures.append("C022 R1 equality boundary was not explained")
    if client_id == "C002" and "no configured rules triggered" not in briefing.executive_summary.lower():
        failures.append("normal-case summary does not distinguish no configured triggers")
    return actual_findings, failures


def run_live_verification(clients: list[str], settings: Settings, stamp: str) -> tuple[list[CheckRow], dict]:
    rows: list[CheckRow] = []
    usage_total = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "thought_tokens": 0, "total_tokens": 0}
    artifacts: list[dict] = []
    live_root = PROJECT_ROOT / "outputs" / "verification" / f"live_{stamp}"
    store = StateStore(live_root / "state" / "verification.sqlite3", live_root / "outputs")

    missing_credentials = []
    if not settings.gemini_api_key:
        missing_credentials.append("GEMINI_API_KEY")
    if not os.getenv("HUBSPOT_ACCESS_TOKEN", "").strip():
        missing_credentials.append("HUBSPOT_ACCESS_TOKEN")
    if missing_credentials:
        for client_id in clients:
            rows.append(CheckRow(
                client_id, "2026-06", LIVE_EXPECTED[client_id], None, "BLOCKED",
                "Missing required live credential(s): " + ", ".join(missing_credentials),
            ))
        return rows, {"usage_totals": usage_total, "live_root": str(live_root), "artifacts": artifacts}

    stop_reason = ""
    for position, client_id in enumerate(clients):
        if stop_reason:
            rows.append(CheckRow(client_id, "2026-06", LIVE_EXPECTED[client_id], None, "NOT_RUN", stop_reason))
            continue
        result = run_review(
            client_id,
            month="2026-06",
            settings=settings,
            state_store=store,
            runtime_root=live_root,
        )
        usage = _read_usage(result.log_path)
        for key, value in usage.items():
            usage_total[key] += value
        artifact = {
            "client_id": client_id,
            "status": result.status,
            "draft_paths": [str(path) for path in result.output_paths],
            "draft_ids": list(result.draft_ids),
            "log_path": str(result.log_path),
            "usage": usage,
        }
        artifacts.append(artifact)
        terminal_provider_failure = _terminal_provider_failure(result.log_path, result.message)
        if result.status != "ok" or not result.output_paths:
            status = "BLOCKED" if terminal_provider_failure else "FAIL"
            rows.append(CheckRow(client_id, "2026-06", LIVE_EXPECTED[client_id], None, status, result.message))
            if terminal_provider_failure:
                stop_reason = f"Not run after {client_id}: {terminal_provider_failure}."
            continue
        if terminal_provider_failure:
            rows.append(CheckRow(client_id, "2026-06", LIVE_EXPECTED[client_id], None, "BLOCKED", terminal_provider_failure))
            stop_reason = f"Not run after {client_id}: {terminal_provider_failure}."
            continue
        try:
            actual, failures = _validate_live_draft(client_id, result.output_paths[0])
        except Exception as exc:
            actual, failures = None, [f"Draft validation error: {exc}"]
        rows.append(CheckRow(
            client_id, "2026-06", LIVE_EXPECTED[client_id], actual,
            "PASS" if not failures else "FAIL",
            "Validated code-verifiable evidence; narrative interpretation still requires human inspection."
            if not failures else "; ".join(failures),
        ))
    return rows, {"usage_totals": usage_total, "live_root": str(live_root), "artifacts": artifacts}


def _compact(value: object, limit: int = 90) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _print_table(rows: list[CheckRow]) -> None:
    headers = ("Check or customer", "Month", "Expected", "Actual", "Status", "Reason")
    values = [
        (row.check, row.month, _compact(row.expected), _compact(row.actual), row.status, row.reason)
        for row in rows
    ]
    widths = [min(42, max(len(headers[index]), *(len(str(row[index])) for row in values))) for index in range(len(headers))]

    def line(parts: tuple[str, ...]) -> str:
        rendered = []
        for index, part in enumerate(parts):
            text = str(part).replace("\n", " ")
            if len(text) > widths[index]:
                text = text[: widths[index] - 3] + "..."
            rendered.append(text.ljust(widths[index]))
        return " | ".join(rendered)

    print(line(headers))
    print("-+-".join("-" * width for width in widths))
    for row in values:
        print(line(row))


def _write_reports(*, stamp: str, mode: str, rows: list[CheckRow], settings: Settings, details: dict) -> tuple[Path, Path]:
    report_dir = PROJECT_ROOT / "outputs" / "verification"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"verification_{mode}_{stamp}.json"
    markdown_path = report_dir / f"verification_{mode}_{stamp}.md"
    counts = {status: sum(row.status == status for row in rows) for status in sorted(STATUS_VALUES)}
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "ruleset_version": ANALYSIS_RULESET_VERSION,
        "model_configuration": {
            "model": settings.gemini_model,
            "max_attempts": settings.gemini_max_attempts,
            "max_calls_per_review": settings.gemini_max_calls,
            "requests_per_minute": settings.gemini_requests_per_minute,
            "fetcher_max_output_tokens": settings.gemini_fetcher_max_output_tokens,
            "analyzer_max_output_tokens": settings.gemini_analyzer_max_output_tokens,
            "operation_output_caps": {
                "data_fetcher_function_call": settings.gemini_fetcher_max_output_tokens,
                "analyzer_structured_narrative": settings.gemini_analyzer_max_output_tokens,
            },
        },
        "status_counts": counts,
        "checks": [asdict(row) for row in rows],
        "failures": [asdict(row) for row in rows if row.status != "PASS"],
        "manual_review_required": [
            "Executive-summary prioritization and usefulness to the advisor.",
            "CRM interpretation, uncertainty language, and follow-up question quality.",
            "Suitability or correctness of any eventual financial advice; automated checks do not prove this.",
            "Every generated draft remains pending_review until an authenticated production process is added and a human explicitly acts.",
        ],
        **details,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

    def md(value: object) -> str:
        return _compact(value, 120).replace("|", "\\|").replace("\n", " ")

    lines = [
        f"# Wealth Advisor verification ({mode})",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Ruleset: `{ANALYSIS_RULESET_VERSION}`",
        f"- Model: `{settings.gemini_model}`",
        f"- Model calls per review: {settings.gemini_max_calls}; attempts: {settings.gemini_max_attempts}",
        f"- Request pacing: {settings.gemini_requests_per_minute} requests/minute",
        f"- Output-token caps: fetcher {settings.gemini_fetcher_max_output_tokens}; analyzer {settings.gemini_analyzer_max_output_tokens}",
        f"- Usage totals: {details.get('usage_totals', {})}",
        f"- Status counts: {counts}",
        "",
        "| Check or customer | Month | Expected | Actual | Status | Reason |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {md(row.check)} | {md(row.month)} | {md(row.expected)} | {md(row.actual)} | {row.status} | {md(row.reason)} |")
    lines.extend(["", "## Manual review still required", ""])
    lines.extend(f"- {item}" for item in payload["manual_review_required"])
    if details.get("artifacts"):
        lines.extend(["", "## Live artifacts", "", "```json", json.dumps(details["artifacts"], indent=2), "```"])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run offline-first Wealth Advisor project verification.")
    parser.add_argument("--live", action="store_true", help="After offline checks pass, run isolated live June reviews.")
    parser.add_argument("--clients", nargs="+", metavar="CLIENT_ID", help="Subset of supported live verification clients.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.clients and not args.live:
        raise SystemExit("--clients requires --live.")
    clients = args.clients or DEFAULT_LIVE_CLIENTS
    unknown = [client for client in clients if client not in LIVE_EXPECTED]
    if unknown:
        raise SystemExit(f"Unsupported live verification client(s): {', '.join(unknown)}")
    if len(clients) != len(set(clients)):
        raise SystemExit("--clients must not contain duplicates; successful cases are never rerun implicitly.")

    settings = Settings.from_env()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    print(f"Model: {settings.gemini_model}")
    print(f"Planned live customers: {len(clients) if args.live else 0}")
    print(
        "Effective output caps: "
        f"data_fetcher_function_call={settings.gemini_fetcher_max_output_tokens}; "
        f"analyzer_structured_narrative={settings.gemini_analyzer_max_output_tokens}"
    )
    print("Running required offline verification (no network calls)...")
    offline_rows, details = run_offline_verification()
    settings_after_tests = Settings.from_env()
    stable_fields = (
        "gemini_model", "gemini_max_attempts", "gemini_max_calls", "gemini_requests_per_minute",
        "gemini_fetcher_max_output_tokens", "gemini_analyzer_max_output_tokens",
    )
    before_config = {field: getattr(settings, field) for field in stable_fields}
    after_config = {field: getattr(settings_after_tests, field) for field in stable_fields}
    configuration_stable = before_config == after_config
    offline_rows.append(CheckRow(
        "Configuration isolation across offline tests", "Offline",
        before_config, after_config,
        "PASS" if configuration_stable else "FAIL",
        "Offline tests restored environment changes; live mode will use freshly loaded project configuration."
        if configuration_stable else "Offline tests changed effective model configuration.",
    ))
    settings = settings_after_tests
    rows = list(offline_rows)
    offline_ok = all(row.status == "PASS" for row in offline_rows)

    mode = "live" if args.live else "offline"
    if args.live:
        if offline_ok:
            print("Offline verification passed; starting sequential isolated live reviews.")
            live_rows, live_details = run_live_verification(clients, settings, stamp)
            rows.extend(live_rows)
            details.update(live_details)
        else:
            reason = "Not run because required offline verification failed."
            rows.extend(CheckRow(client, "2026-06", LIVE_EXPECTED[client], None, "NOT_RUN", reason) for client in clients)
            details.update({"usage_totals": {"model_calls": 0}, "artifacts": []})

    json_path, markdown_path = _write_reports(
        stamp=stamp, mode=mode, rows=rows, settings=settings, details=details,
    )
    _print_table(rows)
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    incomplete = any(row.status != "PASS" for row in rows)
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
