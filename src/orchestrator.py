"""Workflow orchestration for single-month and all-month advisor reviews."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.agents import AgentError, AnalyzerAgent, DataFetcherAgent
from src.analysis import validate_month
from src.config import PROJECT_ROOT, Settings
from src.gemini_client import GeminiClient, GeminiClientError
from src.models import MultiMonthIndex
from src.run_logging import RunLogger
from src.state import StateError, StateStore
from src.tools import get_financial_dataset_months, normalize_client_id


@dataclass(frozen=True)
class ReviewResult:
    status: str
    run_id: str
    message: str
    output_path: Path | None
    log_path: Path
    model: str
    output_paths: tuple[Path, ...] = field(default_factory=tuple)
    index_path: Path | None = None
    completed_months: tuple[str, ...] = field(default_factory=tuple)
    remaining_months: tuple[str, ...] = field(default_factory=tuple)
    draft_ids: tuple[str, ...] = field(default_factory=tuple)


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


def _source_map(bundle) -> dict:
    return {
        "financial": "available" if bundle.financial_status == "ok" else ("missing" if bundle.financial_status == "not_found" else "error"),
        "crm": "available" if bundle.crm_status == "ok" else ("missing" if bundle.crm_status == "not_found" else "error"),
    }


def _save_multi_month_index(
    *, client_id: str, run_id: str, requested_months: list[str], completed: list[dict],
    remaining: list[str], model, bundle, project_root: Path = PROJECT_ROOT, stop_reason: str = "",
) -> Path:
    completed_by_month = {item["month"]: item for item in completed}
    results = []
    for month in requested_months:
        if month in completed_by_month:
            item = completed_by_month[month]
            results.append({
                "month": month,
                "status": "pending_review",
                "briefing_id": item["briefing_id"],
                "revision_id": item["revision_id"],
                "output_path": item["output_path"],
                "triggered_rule_ids": item["triggered_rule_ids"],
                "message": "Validated draft is awaiting explicit human review.",
            })
        else:
            results.append({
                "month": month,
                "status": "not_completed",
                "message": stop_reason or "Not completed within the shared model-call/provider budget.",
            })
    index = MultiMonthIndex.model_validate({
        "client_id": client_id,
        "run_id": run_id,
        "requested_months": requested_months,
        "completed_months": [item["month"] for item in completed],
        "remaining_months": remaining,
        "model_call_limit": model.settings.gemini_max_calls,
        "model_calls_used": model.calls_used,
        "retrieval_count": 1,
        "source_availability": _source_map(bundle),
        "results": results,
    })
    path = project_root / "outputs" / f"briefing_index_{client_id}_all-months_{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def run_review(
    client_id: str,
    *,
    month: str | None = None,
    all_months: bool = False,
    settings: Settings | None = None,
    state_store: StateStore | None = None,
    runtime_root: Path | None = None,
) -> ReviewResult:
    run_id = _new_run_id()
    active_root = Path(runtime_root) if runtime_root is not None else PROJECT_ROOT
    log_path = active_root / "logs" / f"run_{run_id}.jsonl"
    logger = RunLogger(log_path, run_id)
    active_settings = settings
    model_name = settings.gemini_model if settings else (os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash")
    output_paths: list[Path] = []
    index_path: Path | None = None
    completed_months: list[str] = []
    remaining_months: list[str] = []
    draft_ids: list[str] = []
    store = state_store
    try:
        normalized = normalize_client_id(client_id)
        if month is not None and all_months:
            raise ValueError("--month and --all-months are mutually exclusive.")
        dataset_months = get_financial_dataset_months()
        if not dataset_months:
            raise ValueError("Financial dataset does not cover any calendar months.")
        if month is not None:
            selected = validate_month(month)
            if selected not in dataset_months:
                raise ValueError(
                    f"Analysis month {selected} is outside dataset coverage "
                    f"({dataset_months[0]} through {dataset_months[-1]})."
                )
            requested_months = [selected]
        elif all_months:
            requested_months = dataset_months
        else:
            requested_months = [dataset_months[-1]]

        store = store or StateStore(
            db_path=active_root / "state" / "wealth_advisor.sqlite3",
            artifact_dir=active_root / "outputs",
        )
        store.create_session(run_id, normalized, requested_months)
        store.append_progress(run_id, "orchestrator", "started", detail="Workflow session created.")

        active_settings = active_settings or Settings.from_env()
        model_name = active_settings.gemini_model
        logger.log(
            "run_started",
            client_id=normalized,
            model=model_name,
            analysis_mode="all_months" if all_months else "single_month",
            requested_months=requested_months,
        )
        model = GeminiClient(active_settings, logger)
        fetcher = DataFetcherAgent(model, logger)
        analyzer = AnalyzerAgent(model, logger)
        logger.log("agent_handoff", from_agent="orchestrator", to_agent="data_fetcher", status="delegated")
        store.append_progress(run_id, "data_fetcher", "started")
        bundle = fetcher.fetch(normalized)
        store.update_sources(run_id, bundle.financial_status, bundle.crm_status)
        store.append_progress(run_id, "data_fetcher", "complete")
        if bundle.financial_status != "ok":
            message = bundle.financial_message or "Financial records were not found."
            logger.log("run_stopped", status="error", decision="Essential financial records unavailable.")
            return ReviewResult("error", run_id, message, None, log_path, model_name)
        if bundle.crm_status != "ok":
            logger.log(
                "agent_decision", agent="orchestrator",
                summary="Continuing with explicit CRM limitations.", crm_status=bundle.crm_status,
                source_message=bundle.crm_message,
            )

        if all_months:
            logger.log(
                "multi_month_budget",
                requested_month_count=len(requested_months),
                model_call_limit=active_settings.gemini_max_calls,
                calls_used_after_retrieval=model.calls_used,
                calls_remaining=max(0, active_settings.gemini_max_calls - model.calls_used),
                minimum_analyzer_calls_needed=len(requested_months),
                decision="Use one shared call budget; do not reset between months.",
            )

        completed: list[dict] = []
        stop_reason = ""
        for position, selected_month in enumerate(requested_months):
            if model.calls_used >= active_settings.gemini_max_calls:
                remaining_months = requested_months[position:]
                stop_reason = "Gemini call limit reached before all requested months completed."
                logger.log(
                    "multi_month_stopped", status="partial", reason=stop_reason,
                    completed_months=completed_months, remaining_months=remaining_months,
                )
                break
            logger.log("month_review_started", analysis_month=selected_month, model_calls_used=model.calls_used)
            historical_memory = store.retrieve_approved_memory(normalized, selected_month)
            store.record_historical_memory(run_id, selected_month, historical_memory)
            store.append_progress(
                run_id, "memory", "retrieved", month=selected_month,
                detail=f"{len(historical_memory)} approved earlier-month summary record(s).",
            )
            logger.log(
                "agent_handoff", from_agent="orchestrator", to_agent="analyzer",
                status="delegated", analysis_month=selected_month,
            )
            try:
                briefing = analyzer.analyze(
                    bundle,
                    selected_month,
                    latest_dataset_month=dataset_months[-1],
                    historical_memory=historical_memory,
                )
            except (AgentError, GeminiClientError, ValueError) as exc:
                remaining_months = requested_months[position:]
                stop_reason = str(exc)
                logger.log(
                    "month_review_failed", status="error", analysis_month=selected_month,
                    error=stop_reason, remaining_months=remaining_months,
                )
                break
            draft = store.create_pending_draft(run_id, briefing)
            output_path = Path(draft["output_path"])
            output_paths.append(output_path)
            draft_ids.append(draft["revision_id"])
            completed_months.append(selected_month)
            rules = [finding.rule_id for finding in briefing.findings]
            completed.append({
                "month": selected_month,
                "output_path": str(output_path.relative_to(active_root)),
                "triggered_rule_ids": rules,
                "briefing_id": draft["briefing_id"],
                "revision_id": draft["revision_id"],
            })
            store.append_progress(
                run_id, "orchestrator", "pending_review", month=selected_month,
                detail=draft["revision_id"],
            )
            logger.log(
                "draft_saved", status="pending_review", analysis_month=selected_month,
                path=str(output_path.relative_to(active_root)), rule_ids=rules,
                draft_id=draft["revision_id"],
            )

        if all_months:
            if not remaining_months:
                remaining_months = [month for month in requested_months if month not in completed_months]
            index_path = _save_multi_month_index(
                client_id=normalized,
                run_id=run_id,
                requested_months=requested_months,
                completed=completed,
                remaining=remaining_months,
                model=model,
                bundle=bundle,
                project_root=active_root,
                stop_reason=stop_reason,
            )
            store.set_run_index(run_id, index_path)
            logger.log(
                "multi_month_index_saved", status="ok",
                path=str(index_path.relative_to(active_root)),
                completed_months=completed_months, remaining_months=remaining_months,
            )
            if remaining_months:
                status = "partial" if completed_months else "error"
                message = (
                    f"Completed {len(completed_months)} of {len(requested_months)} months. "
                    f"Remaining: {', '.join(remaining_months)}. {stop_reason}"
                ).strip()
            else:
                status = "ok"
                message = f"Created {len(completed_months)} validated drafts pending human review and a combined index."
        else:
            status = "ok" if completed_months else "error"
            message = "Validated draft created with status pending_review." if completed_months else (stop_reason or "Briefing was not completed.")

        logger.log(
            "run_completed" if status == "ok" else "run_stopped",
            status=status,
            model_calls=model.calls_used,
            completed_months=completed_months,
            remaining_months=remaining_months,
        )
        return ReviewResult(
            status=status,
            run_id=run_id,
            message=message,
            output_path=output_paths[0] if len(output_paths) == 1 else None,
            log_path=log_path,
            model=model_name,
            output_paths=tuple(output_paths),
            index_path=index_path,
            completed_months=tuple(completed_months),
            remaining_months=tuple(remaining_months),
            draft_ids=tuple(draft_ids),
        )
    except (ValueError, AgentError, GeminiClientError, StateError) as exc:
        logger.log("run_failed", status="error", error=str(exc))
        return ReviewResult(
            "error", run_id, str(exc), output_paths[0] if len(output_paths) == 1 else None,
            log_path, model_name, tuple(output_paths), index_path,
            tuple(completed_months), tuple(remaining_months), tuple(draft_ids),
        )
    except Exception:
        message = "Review failed because of an unexpected internal error. See the run log."
        logger.log("run_failed", status="error", error=message)
        return ReviewResult(
            "error", run_id, message, output_paths[0] if len(output_paths) == 1 else None,
            log_path, model_name, tuple(output_paths), index_path,
            tuple(completed_months), tuple(remaining_months), tuple(draft_ids),
        )
