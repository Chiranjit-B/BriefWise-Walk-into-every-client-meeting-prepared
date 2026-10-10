"""SQLite-backed workflow context, draft review, and approved memory."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis import ANALYSIS_RULESET_VERSION
from src.config import PROJECT_ROOT
from src.models import Briefing


class StateError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


class StateStore:
    """Local state store. Each method opens its own process-safe SQLite connection."""

    MEMORY_LIMIT = 3
    HUMAN_LABEL_BLOCKLIST = {"ai", "ai reviewer", "assistant", "gemini", "model"}

    def __init__(self, db_path: Path | None = None, artifact_dir: Path | None = None):
        self.db_path = Path(db_path or PROJECT_ROOT / "state" / "wealth_advisor.sqlite3")
        self.artifact_dir = Path(artifact_dir or PROJECT_ROOT / "outputs")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    requested_months_json TEXT NOT NULL,
                    financial_status TEXT NOT NULL DEFAULT 'pending',
                    crm_status TEXT NOT NULL DEFAULT 'pending',
                    agent_progress_json TEXT NOT NULL DEFAULT '[]',
                    calculated_findings_json TEXT NOT NULL DEFAULT '{}',
                    draft_references_json TEXT NOT NULL DEFAULT '[]',
                    historical_memory_json TEXT NOT NULL DEFAULT '{}',
                    index_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    revision_id TEXT PRIMARY KEY,
                    briefing_id TEXT NOT NULL,
                    ruleset_version TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    analysis_month TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    parent_revision_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('pending_review','approved','rejected','superseded')),
                    briefing_json TEXT NOT NULL,
                    advisor_disposition TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    final_output_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    reviewer_label TEXT NOT NULL DEFAULT '',
                    review_reason TEXT NOT NULL DEFAULT '',
                    changed_fields_json TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(briefing_id, revision_number),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_drafts_run ON drafts(run_id, analysis_month);
                CREATE INDEX IF NOT EXISTS idx_drafts_client ON drafts(client_id, analysis_month);
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    briefing_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reviewer_label TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    changed_fields_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approved_memories (
                    memory_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    analysis_month TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    approval_timestamp TEXT NOT NULL,
                    originating_briefing_id TEXT NOT NULL,
                    originating_revision_id TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1,
                    superseded_by_revision_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_lookup
                    ON approved_memories(client_id, analysis_month, active, approval_timestamp);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(drafts)").fetchall()}
            if "ruleset_version" not in columns:
                connection.execute("ALTER TABLE drafts ADD COLUMN ruleset_version TEXT NOT NULL DEFAULT ''")

    def create_session(self, run_id: str, client_id: str, requested_months: list[str]) -> dict:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs(run_id, client_id, requested_months_json, created_at, updated_at) VALUES(?,?,?,?,?)",
                (run_id, client_id, _json(requested_months), timestamp, timestamp),
            )
        return self.get_session(run_id)

    def get_session(self, run_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise StateError(f"Run {run_id} was not found.")
        return {
            "run_id": row["run_id"],
            "client_id": row["client_id"],
            "requested_months": _decode(row["requested_months_json"], []),
            "source_statuses": {"financial": row["financial_status"], "crm": row["crm_status"]},
            "agent_progress": _decode(row["agent_progress_json"], []),
            "calculated_findings": _decode(row["calculated_findings_json"], {}),
            "draft_references": _decode(row["draft_references_json"], []),
            "historical_memory": _decode(row["historical_memory_json"], {}),
            "index_path": row["index_path"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def update_sources(self, run_id: str, financial_status: str, crm_status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET financial_status=?, crm_status=?, updated_at=? WHERE run_id=?",
                (financial_status, crm_status, _now(), run_id),
            )

    def append_progress(self, run_id: str, agent: str, status: str, *, month: str = "", detail: str = "") -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT agent_progress_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"Run {run_id} was not found.")
            progress = _decode(row["agent_progress_json"], [])
            progress.append({"timestamp": _now(), "agent": agent, "status": status, "month": month, "detail": detail})
            connection.execute(
                "UPDATE runs SET agent_progress_json=?, updated_at=? WHERE run_id=?",
                (_json(progress), _now(), run_id),
            )

    def record_historical_memory(self, run_id: str, month: str, summaries: list[dict]) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT historical_memory_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"Run {run_id} was not found.")
            memory = _decode(row["historical_memory_json"], {})
            memory[month] = summaries
            connection.execute(
                "UPDATE runs SET historical_memory_json=?, updated_at=? WHERE run_id=?",
                (_json(memory), _now(), run_id),
            )

    def retrieve_approved_memory(self, client_id: str, before_month: str, limit: int | None = None) -> list[dict]:
        maximum = min(max(limit or self.MEMORY_LIMIT, 1), self.MEMORY_LIMIT)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT summary_json FROM approved_memories
                WHERE client_id=? AND analysis_month < ? AND active=1
                ORDER BY analysis_month DESC, approval_timestamp DESC LIMIT ?
                """,
                (client_id, before_month, 50),
            ).fetchall()
        compatible = [
            summary for row in rows
            if (summary := _decode(row["summary_json"], {})).get("ruleset_version") == ANALYSIS_RULESET_VERSION
        ]
        return compatible[:maximum]

    def create_pending_draft(self, run_id: str, briefing: Briefing) -> dict:
        session = self.get_session(run_id)
        if session["client_id"] != briefing.client_id:
            raise StateError("Draft client does not match its workflow session.")
        revision_id = f"draft_{uuid.uuid4().hex[:12]}"
        timestamp = _now()
        path = self.artifact_dir / f"draft_{briefing.client_id}_{briefing.analysis_month}_{run_id}_{revision_id}.json"
        envelope = {
            "ruleset_version": ANALYSIS_RULESET_VERSION,
            "briefing_id": revision_id,
            "revision_id": revision_id,
            "revision_number": 1,
            "status": "pending_review",
            "advisor_disposition": "",
            "briefing": briefing.model_dump(mode="json"),
        }
        path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO drafts(
                    revision_id, briefing_id, ruleset_version, run_id, client_id, analysis_month, revision_number,
                    status, briefing_json, output_path, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    revision_id, revision_id, ANALYSIS_RULESET_VERSION, run_id, briefing.client_id, briefing.analysis_month, 1,
                    "pending_review", _json(briefing.model_dump(mode="json")), str(path), timestamp,
                ),
            )
            row = connection.execute(
                "SELECT calculated_findings_json, draft_references_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            findings = _decode(row["calculated_findings_json"], {})
            findings[briefing.analysis_month] = [item.rule_id for item in briefing.findings]
            references = _decode(row["draft_references_json"], [])
            references.append({
                "month": briefing.analysis_month,
                "briefing_id": revision_id,
                "revision_id": revision_id,
                "status": "pending_review",
                "path": str(path),
            })
            connection.execute(
                "UPDATE runs SET calculated_findings_json=?, draft_references_json=?, updated_at=? WHERE run_id=?",
                (_json(findings), _json(references), _now(), run_id),
            )
        return self.get_draft(revision_id)

    def get_draft(self, revision_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM drafts WHERE revision_id=?", (revision_id,)).fetchone()
        if row is None:
            raise StateError(f"Draft or revision {revision_id} was not found.")
        return self._draft_record(row)

    def _draft_record(self, row: sqlite3.Row) -> dict:
        return {
            "ruleset_version": row["ruleset_version"],
            "briefing_id": row["briefing_id"],
            "revision_id": row["revision_id"],
            "run_id": row["run_id"],
            "client_id": row["client_id"],
            "analysis_month": row["analysis_month"],
            "revision_number": row["revision_number"],
            "parent_revision_id": row["parent_revision_id"],
            "status": row["status"],
            "briefing": _decode(row["briefing_json"], {}),
            "advisor_disposition": row["advisor_disposition"],
            "output_path": row["output_path"],
            "final_output_path": row["final_output_path"],
            "created_at": row["created_at"],
            "reviewed_at": row["reviewed_at"],
            "reviewer_label": row["reviewer_label"],
            "review_reason": row["review_reason"],
            "changed_fields": _decode(row["changed_fields_json"], []),
        }

    def list_audit(self, briefing_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE briefing_id=? ORDER BY event_id", (briefing_id,)
            ).fetchall()
        return [
            {
                "revision_id": row["revision_id"], "action": row["action"],
                "reviewer_label": row["reviewer_label"], "reason": row["reason"],
                "changed_fields": _decode(row["changed_fields_json"], []), "timestamp": row["created_at"],
            }
            for row in rows
        ]

    @classmethod
    def _reviewer(cls, reviewer_label: str) -> str:
        value = reviewer_label.strip()
        if not value:
            raise StateError("Reviewer label is required.")
        if value.casefold() in cls.HUMAN_LABEL_BLOCKLIST:
            raise StateError("An AI or model label cannot be recorded as human approval.")
        return value

    @staticmethod
    def _approved_summary(draft: dict, approved_at: str) -> dict:
        briefing = draft["briefing"]
        return {
            "ruleset_version": draft["ruleset_version"],
            "client_id": draft["client_id"],
            "analysis_month": draft["analysis_month"],
            "metrics": briefing["financial_metrics"],
            "findings": [
                {"rule_id": item["rule_id"], "summary": item["summary"], "uncertainty": item["uncertainty"]}
                for item in briefing["findings"]
            ],
            "advisor_interpretation": briefing["relevant_crm_context"]["interpretation"],
            "advisor_disposition": draft["advisor_disposition"],
            "source_limitations": [
                briefing["relevant_crm_context"]["history_limitation"], *briefing["limitations"],
            ],
            "approval_timestamp": approved_at,
            "originating_briefing_id": draft["briefing_id"],
            "originating_revision_id": draft["revision_id"],
            "context_label": (
                "Human-approved historical analysis context only; not authoritative current transaction evidence. "
                "Approval may have occurred after the analysis month and does not prove this information was known then."
            ),
        }

    def approve(self, revision_id: str, reviewer_label: str, reason: str = "") -> dict:
        reviewer = self._reviewer(reviewer_label)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM drafts WHERE revision_id=?", (revision_id,)).fetchone()
            if row is None:
                raise StateError(f"Draft or revision {revision_id} was not found.")
            draft = self._draft_record(row)
            if draft["ruleset_version"] != ANALYSIS_RULESET_VERSION:
                raise StateError("This draft uses an obsolete analysis ruleset and must be regenerated before approval.")
            if draft["status"] == "approved":
                return draft
            if draft["status"] != "pending_review":
                raise StateError(f"Only a pending revision can be approved; current status is {draft['status']}.")
            approved_at = _now()
            final_path = self._write_final_artifact(draft, reviewer, approved_at, reason)
            connection.execute(
                "UPDATE drafts SET status='approved', reviewed_at=?, reviewer_label=?, review_reason=?, final_output_path=? WHERE revision_id=?",
                (approved_at, reviewer, reason.strip(), str(final_path), revision_id),
            )
            draft.update(status="approved", reviewed_at=approved_at, reviewer_label=reviewer, review_reason=reason.strip())
            summary = self._approved_summary(draft, approved_at)
            connection.execute(
                """
                UPDATE approved_memories
                SET active=0, superseded_by_revision_id=?
                WHERE client_id=? AND analysis_month=? AND active=1
                """,
                (revision_id, draft["client_id"], draft["analysis_month"]),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO approved_memories(
                    memory_id, client_id, analysis_month, summary_json, approval_timestamp,
                    originating_briefing_id, originating_revision_id, created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    f"memory_{uuid.uuid4().hex[:12]}", draft["client_id"], draft["analysis_month"],
                    _json(summary), approved_at, draft["briefing_id"], revision_id, approved_at,
                ),
            )
            connection.execute(
                "INSERT INTO audit_events(briefing_id,revision_id,action,reviewer_label,reason,changed_fields_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (draft["briefing_id"], revision_id, "approved", reviewer, reason.strip(), "[]", approved_at),
            )
        self._refresh_index(draft["run_id"])
        self._record_review_in_session(draft["run_id"], revision_id, "approved")
        return self.get_draft(revision_id)

    def reject(self, revision_id: str, reviewer_label: str, reason: str) -> dict:
        reviewer = self._reviewer(reviewer_label)
        explanation = reason.strip()
        if not explanation:
            raise StateError("A rejection reason is required.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM drafts WHERE revision_id=?", (revision_id,)).fetchone()
            if row is None:
                raise StateError(f"Draft or revision {revision_id} was not found.")
            draft = self._draft_record(row)
            if draft["status"] == "rejected":
                return draft
            if draft["status"] != "pending_review":
                raise StateError(f"Only a pending revision can be rejected; current status is {draft['status']}.")
            reviewed_at = _now()
            connection.execute(
                "UPDATE drafts SET status='rejected', reviewed_at=?, reviewer_label=?, review_reason=? WHERE revision_id=?",
                (reviewed_at, reviewer, explanation, revision_id),
            )
            connection.execute(
                "INSERT INTO audit_events(briefing_id,revision_id,action,reviewer_label,reason,changed_fields_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (draft["briefing_id"], revision_id, "rejected", reviewer, explanation, "[]", reviewed_at),
            )
        self._refresh_index(draft["run_id"])
        self._record_review_in_session(draft["run_id"], revision_id, "rejected")
        return self.get_draft(revision_id)

    def amend_and_approve(
        self,
        revision_id: str,
        reviewer_label: str,
        *,
        interpretation: str | None = None,
        follow_up_questions: list[str] | None = None,
        advisor_disposition: str | None = None,
        reason: str = "",
    ) -> dict:
        reviewer = self._reviewer(reviewer_label)
        current = self.get_draft(revision_id)
        if current["ruleset_version"] != ANALYSIS_RULESET_VERSION:
            raise StateError("This draft uses an obsolete analysis ruleset and must be regenerated before amendment.")
        if current["status"] == "superseded":
            raise StateError("A superseded revision cannot be amended.")
        changed_fields: list[str] = []
        briefing_data = json.loads(json.dumps(current["briefing"]))
        disposition = current["advisor_disposition"]
        if interpretation is not None:
            value = interpretation.strip()
            if not value:
                raise StateError("Amended interpretation cannot be empty.")
            briefing_data["relevant_crm_context"]["interpretation"] = value
            changed_fields.append("relevant_crm_context.interpretation")
        if follow_up_questions is not None:
            questions = [item.strip() for item in follow_up_questions if item.strip()]
            if not questions:
                raise StateError("At least one nonempty follow-up question is required.")
            briefing_data["advisor_follow_up_questions"] = questions
            changed_fields.append("advisor_follow_up_questions")
        if advisor_disposition is not None:
            disposition = advisor_disposition.strip()
            if not disposition:
                raise StateError("Advisor disposition cannot be empty.")
            changed_fields.append("advisor_disposition")
        if not changed_fields:
            raise StateError("At least one amendable field must be supplied.")
        amended = Briefing.model_validate(briefing_data)
        self._assert_evidence_immutable(current["briefing"], amended.model_dump(mode="json"))
        approved_at = _now()
        new_revision_id = f"rev_{uuid.uuid4().hex[:12]}"
        new_revision = {
            **current,
            "revision_id": new_revision_id,
            "revision_number": current["revision_number"] + 1,
            "parent_revision_id": revision_id,
            "status": "approved",
            "briefing": amended.model_dump(mode="json"),
            "advisor_disposition": disposition,
            "reviewed_at": approved_at,
            "reviewer_label": reviewer,
            "review_reason": reason.strip(),
            "changed_fields": changed_fields,
        }
        final_path = self._write_final_artifact(new_revision, reviewer, approved_at, reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute("SELECT status FROM drafts WHERE revision_id=?", (revision_id,)).fetchone()
            if latest is None or latest["status"] == "superseded":
                raise StateError("The source revision is no longer available for amendment.")
            connection.execute("UPDATE drafts SET status='superseded' WHERE revision_id=?", (revision_id,))
            connection.execute(
                """
                UPDATE approved_memories
                SET active=0, superseded_by_revision_id=?
                WHERE client_id=? AND analysis_month=? AND active=1
                """,
                (new_revision_id, current["client_id"], current["analysis_month"]),
            )
            connection.execute(
                """
                INSERT INTO drafts(
                    revision_id,briefing_id,ruleset_version,run_id,client_id,analysis_month,revision_number,parent_revision_id,
                    status,briefing_json,advisor_disposition,final_output_path,created_at,reviewed_at,
                    reviewer_label,review_reason,changed_fields_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_revision_id, current["briefing_id"], ANALYSIS_RULESET_VERSION, current["run_id"], current["client_id"],
                    current["analysis_month"], current["revision_number"] + 1, revision_id, "approved",
                    _json(amended.model_dump(mode="json")), disposition, str(final_path), approved_at, approved_at,
                    reviewer, reason.strip(), _json(changed_fields),
                ),
            )
            summary = self._approved_summary(new_revision, approved_at)
            connection.execute(
                """
                INSERT INTO approved_memories(
                    memory_id,client_id,analysis_month,summary_json,approval_timestamp,
                    originating_briefing_id,originating_revision_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    f"memory_{uuid.uuid4().hex[:12]}", current["client_id"], current["analysis_month"],
                    _json(summary), approved_at, current["briefing_id"], new_revision_id, approved_at,
                ),
            )
            connection.execute(
                "INSERT INTO audit_events(briefing_id,revision_id,action,reviewer_label,reason,changed_fields_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (current["briefing_id"], new_revision_id, "amended_and_approved", reviewer, reason.strip(), _json(changed_fields), approved_at),
            )
        self._refresh_index(current["run_id"])
        self._record_review_in_session(
            current["run_id"], revision_id, "superseded",
            new_revision={
                "month": current["analysis_month"],
                "briefing_id": current["briefing_id"],
                "revision_id": new_revision_id,
                "status": "approved",
                "path": str(final_path),
            },
        )
        return self.get_draft(new_revision_id)

    def _record_review_in_session(
        self,
        run_id: str,
        revision_id: str,
        status: str,
        *,
        new_revision: dict | None = None,
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT draft_references_json, agent_progress_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return
            references = _decode(row["draft_references_json"], [])
            for reference in references:
                if reference.get("revision_id") == revision_id:
                    reference["status"] = status
            if new_revision:
                references.append(new_revision)
            progress = _decode(row["agent_progress_json"], [])
            progress.append({
                "timestamp": _now(), "agent": "human_review", "status": new_revision["status"] if new_revision else status,
                "month": new_revision["month"] if new_revision else "", "detail": new_revision["revision_id"] if new_revision else revision_id,
            })
            connection.execute(
                "UPDATE runs SET draft_references_json=?, agent_progress_json=?, updated_at=? WHERE run_id=?",
                (_json(references), _json(progress), _now(), run_id),
            )

    @staticmethod
    def _assert_evidence_immutable(original: dict, amended: dict) -> None:
        immutable = (
            "client_id", "analysis_month", "source_availability", "financial_metrics",
            "rule_evaluations", "findings",
        )
        for field in immutable:
            if original[field] != amended[field]:
                raise StateError(f"Calculated or source evidence field '{field}' is immutable.")

    def _write_final_artifact(self, draft: dict, reviewer: str, approved_at: str, reason: str) -> Path:
        path = self.artifact_dir / (
            f"approved_{draft['client_id']}_{draft['analysis_month']}_{draft['briefing_id']}_{draft['revision_id']}.json"
        )
        payload = {
            "ruleset_version": draft["ruleset_version"],
            "briefing_id": draft["briefing_id"],
            "revision_id": draft["revision_id"],
            "revision_number": draft["revision_number"],
            "status": "approved",
            "review": {
                "reviewer_label": reviewer,
                "approval_timestamp": approved_at,
                "reason": reason.strip(),
                "changed_fields": draft.get("changed_fields", []),
                "identity_notice": "Reviewer labels are audit metadata and are not authenticated identities.",
            },
            "advisor_disposition": draft["advisor_disposition"],
            "briefing": draft["briefing"],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def set_run_index(self, run_id: str, index_path: Path) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET index_path=?, updated_at=? WHERE run_id=?", (str(index_path), _now(), run_id)
            )

    def _refresh_index(self, run_id: str) -> None:
        session = self.get_session(run_id)
        if not session["index_path"]:
            return
        path = Path(session["index_path"])
        if not path.exists():
            return
        document = json.loads(path.read_text(encoding="utf-8"))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.* FROM drafts d
                JOIN (SELECT briefing_id, MAX(revision_number) AS revision_number FROM drafts WHERE run_id=? GROUP BY briefing_id) latest
                  ON latest.briefing_id=d.briefing_id AND latest.revision_number=d.revision_number
                WHERE d.run_id=?
                """,
                (run_id, run_id),
            ).fetchall()
        by_briefing = {row["briefing_id"]: self._draft_record(row) for row in rows}
        for result in document.get("results", []):
            draft = by_briefing.get(result.get("briefing_id", ""))
            if draft:
                result["status"] = draft["status"]
                result["revision_id"] = draft["revision_id"]
                result["message"] = f"Latest revision is {draft['status']}."
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

