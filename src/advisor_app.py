"""Local Streamlit workspace for preparing and reviewing advisor briefings."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

# Streamlit executes a file entrypoint with that file's directory on sys.path.
# Keep the documented root entrypoint, but also support the legacy/direct command
# `streamlit run src/advisor_app.py` by making the project package importable.
if __package__ in {None, ""}:
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.models import Briefing
from src.orchestrator import run_review
from src.presentation import AdvisorReview, build_advisor_review
from src.state import StateError, StateStore
from src.tools import get_financial_client_ids, get_financial_data, get_financial_dataset_months


IDENTITY_NOTICE = (
    "Reviewer labels are audit metadata for this local demonstration; they are not authenticated identities."
)


class AdvisorAppError(RuntimeError):
    """A safe, user-facing workspace error."""


def load_draft_review(revision_id: str, store: StateStore) -> tuple[dict[str, Any], AdvisorReview]:
    """Load one stored revision and adapt it for the meeting-ready presentation."""
    try:
        draft = store.get_draft(revision_id.strip())
        briefing = Briefing.model_validate(draft["briefing"])
    except (StateError, ValidationError, KeyError) as exc:
        raise AdvisorAppError(f"Could not open that draft: {exc}") from exc

    financial_result = get_financial_data(briefing.client_id)
    financial_data = financial_result.get("data") if financial_result.get("status") == "ok" else None
    review = build_advisor_review(
        briefing,
        financial_data=financial_data,
        review_status=draft["status"],
        revision_id=draft["revision_id"],
    )
    return draft, review


def _money(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "Unavailable"
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{value:,.2f}"


def _source_label(status: str) -> str:
    return {"available": "Available", "missing": "Missing", "error": "Error"}.get(status, status.title())


def _render_review(st: Any, store: StateStore, draft: dict[str, Any], review: AdvisorReview) -> None:
    st.markdown(f"### {review.client_id} · {review.month_label}")
    status_col, financial_col, crm_col = st.columns([1.2, 1, 1])
    status_col.caption("REVIEW STATUS")
    status_col.write(review.review_status.replace("_", " ").title())
    financial_col.caption("FINANCIAL SOURCE")
    financial_col.write(_source_label(review.source_availability["financial"]))
    crm_col.caption("CRM SOURCE")
    crm_col.write(_source_label(review.source_availability["crm"]))
    st.caption(f"Revision {review.revision_id} · Evidence calculated for {review.analysis_month}")

    overview = review.financial_overview
    salary, debits, cash_flow, baseline = st.columns(4)
    salary.metric("Recorded salary", _money(overview.salary, overview.currency))
    debits.metric("Recorded debits", _money(overview.total_debits, overview.currency))
    cash_flow.metric(overview.cash_flow_label, _money(overview.cash_flow_amount, overview.currency))
    baseline.metric("Prior 3-month average", _money(overview.preceding_three_month_average, overview.currency))

    st.markdown("#### Advisor summary")
    st.write(review.executive_summary)

    st.markdown("#### Items needing attention")
    if not review.attention_items:
        st.success("No configured rules triggered for this month. This is not a statement that the client has no financial risk.")
    for item in review.attention_items:
        with st.container(border=True):
            st.markdown(f"##### {item.title}")
            st.write(item.what_happened)
            st.caption("WHY IT WAS HIGHLIGHTED")
            st.write(item.why_highlighted)
            context_col, uncertainty_col = st.columns(2)
            with context_col:
                st.caption("POSSIBLE CRM CONTEXT")
                st.write(item.crm_context)
            with uncertainty_col:
                st.caption("WHAT REMAINS UNCERTAIN")
                st.write(item.uncertainty)
            st.info(f"Ask the client: {item.advisor_question}")
            with st.expander("View supporting evidence"):
                for evidence in item.evidence:
                    if evidence.kind == "transaction":
                        details = [evidence.timestamp, evidence.transaction_type.replace("_", " ")]
                        if evidence.recipient_id:
                            details.append(f"recipient {evidence.recipient_id}")
                        st.write(f"**{evidence.reference}** · {_money(evidence.amount, overview.currency)} · {' · '.join(filter(None, details))}")
                        if evidence.description:
                            st.caption(f"Unverified transaction description: {evidence.description}")
                    else:
                        st.write(f"**{evidence.reference}** · {evidence.kind.title()} reference")

    st.markdown("#### Other financial observations")
    for observation in review.financial_observations:
        label = observation.status.replace("_", " ").title()
        with st.expander(f"{observation.title} · {label}"):
            st.write(observation.explanation)

    left, right = st.columns(2)
    with left:
        st.markdown("#### Suggested meeting questions")
        for question in review.meeting_questions:
            st.write(f"• {question}")
    with right:
        st.markdown("#### Planning context")
        st.write(f"**Risk tolerance:** {review.crm_context['risk_tolerance']}")
        st.write(f"**Financial goals:** {review.crm_context['financial_goals']}")
        st.write(review.crm_context["interpretation"])
        st.caption(review.crm_context["history_limitation"])

    if review.suspicious_transfer_review_recommended:
        st.warning("A transfer-pattern rule triggered. Human review is recommended before using this briefing in a client conversation.")
    with st.expander("Data and interpretation limitations"):
        for limitation in review.limitations:
            st.write(f"• {limitation}")

    st.markdown("#### Human review checkpoint")
    st.caption(IDENTITY_NOTICE)
    if draft["status"] == "pending_review":
        approve_tab, reject_tab, amend_tab = st.tabs(["Approve", "Reject", "Amend and approve"])
        with approve_tab:
            with st.form(f"approve-{draft['revision_id']}"):
                reviewer = st.text_input("Reviewer label", key=f"approve-reviewer-{draft['revision_id']}")
                reason = st.text_area("Review note (optional)", key=f"approve-reason-{draft['revision_id']}")
                submitted = st.form_submit_button("Approve unchanged", type="primary")
            if submitted:
                try:
                    store.approve(draft["revision_id"], reviewer, reason)
                    st.success("Briefing approved and added to approved historical memory.")
                    st.rerun()
                except StateError as exc:
                    st.error(str(exc))
        with reject_tab:
            with st.form(f"reject-{draft['revision_id']}"):
                reviewer = st.text_input("Reviewer label", key=f"reject-reviewer-{draft['revision_id']}")
                reason = st.text_area("Reason for rejection", key=f"reject-reason-{draft['revision_id']}")
                submitted = st.form_submit_button("Reject draft")
            if submitted:
                try:
                    store.reject(draft["revision_id"], reviewer, reason)
                    st.success("Draft rejected. It was not added to approved memory.")
                    st.rerun()
                except StateError as exc:
                    st.error(str(exc))
        with amend_tab:
            with st.form(f"amend-{draft['revision_id']}"):
                reviewer = st.text_input("Reviewer label", key=f"amend-reviewer-{draft['revision_id']}")
                interpretation = st.text_area(
                    "Advisor interpretation",
                    value=draft["briefing"]["relevant_crm_context"]["interpretation"],
                    key=f"amend-interpretation-{draft['revision_id']}",
                )
                questions = st.text_area(
                    "Follow-up questions (one per line)",
                    value="\n".join(draft["briefing"]["advisor_follow_up_questions"]),
                    key=f"amend-questions-{draft['revision_id']}",
                )
                disposition = st.text_area(
                    "Advisor disposition",
                    placeholder="Example: Explained by client; original rule trigger retained for audit.",
                    key=f"amend-disposition-{draft['revision_id']}",
                )
                reason = st.text_area("Reason for amendment", key=f"amend-reason-{draft['revision_id']}")
                submitted = st.form_submit_button("Save amended revision and approve", type="primary")
            if submitted:
                try:
                    original_interpretation = draft["briefing"]["relevant_crm_context"]["interpretation"]
                    original_questions = draft["briefing"]["advisor_follow_up_questions"]
                    amended_questions = [item.strip() for item in questions.splitlines() if item.strip()]
                    amended = store.amend_and_approve(
                        draft["revision_id"],
                        reviewer,
                        interpretation=interpretation if interpretation.strip() != original_interpretation else None,
                        follow_up_questions=amended_questions if amended_questions != original_questions else None,
                        advisor_disposition=disposition or None,
                        reason=reason,
                    )
                    st.session_state.active_revision_id = amended["revision_id"]
                    st.success("Amended revision approved. The original evidence remains unchanged.")
                    st.rerun()
                except StateError as exc:
                    st.error(str(exc))
    else:
        st.info(f"This revision is {draft['status'].replace('_', ' ')}. No pending action is available.")

    audit = store.list_audit(draft["briefing_id"])
    with st.expander(f"Audit trail ({len(audit)})"):
        if not audit:
            st.caption("No human review action has been recorded yet.")
        for event in audit:
            st.write(f"**{event['action'].replace('_', ' ').title()}** by {event['reviewer_label']} · {event['timestamp']}")
            if event["reason"]:
                st.caption(event["reason"])
    with st.expander("Raw validated briefing JSON"):
        st.json(draft["briefing"])


def main() -> None:
    """Render the local advisor workspace."""
    import streamlit as st

    st.set_page_config(page_title="Wealth Advisor Workspace", page_icon="◈", layout="wide")
    st.markdown(
        """
        <style>
        .stApp { background: #f6f4ef; color: #17252a; }
        [data-testid="stSidebar"] { background: #172f35; }
        [data-testid="stSidebar"] * { color: #f7f4ec; }
        [data-testid="stMetric"] { background: #ffffff; border: 1px solid #dce2df; padding: 1rem; border-radius: .6rem; }
        [data-testid="stMetricLabel"] { color: #52646a; }
        [data-testid="stMetricValue"], [data-testid="stMetricValue"] * {
            color: #12363d !important;
            opacity: 1 !important;
            font-weight: 700;
        }
        h1, h2, h3, h4, h5 { color: #173f46; letter-spacing: -.02em; }
        .stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] { background: #176b64; border-color: #176b64; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    store = StateStore()
    clients = get_financial_client_ids()
    months = get_financial_dataset_months()

    with st.sidebar:
        st.markdown("## Review setup")
        st.caption("Prepare one evidence-based monthly client review.")
        client_id = st.selectbox("Client", clients)
        month = st.selectbox("Analysis month", list(reversed(months)))
        fresh_review = st.toggle(
            "Generate a fresh review",
            value=False,
            help="Off opens the latest stored pending or approved review immediately when one exists.",
        )
        if st.button("Prepare client review", type="primary", width="stretch"):
            existing_review = None if fresh_review else store.find_latest_review(client_id, month)
            if existing_review is not None:
                st.session_state.active_revision_id = existing_review["revision_id"]
                st.success("Stored review opened instantly. Turn on fresh review to regenerate it.")
            else:
                with st.spinner("Retrieving sources, calculating rules, and generating advisor context…"):
                    result = run_review(client_id, month=month, state_store=store)
                if result.status == "ok" and result.draft_ids:
                    st.session_state.active_revision_id = result.draft_ids[0]
                    st.success("Draft ready for review.")
                else:
                    st.error(result.message)
        st.divider()
        existing = st.text_input("Open an existing draft ID")
        if st.button("Open draft", width="stretch"):
            if existing.strip():
                st.session_state.active_revision_id = existing.strip()
            else:
                st.error("Enter a draft or revision ID.")
        st.caption("Stored reviews open without external calls. A fresh review uses HubSpot plus one Gemini Analyzer request.")

    st.title("Wealth Advisor Workspace")
    st.write("Turn verified monthly signals into a focused client conversation, then record an explicit human decision.")
    active_revision_id = st.session_state.get("active_revision_id")
    if not active_revision_id:
        st.info("Choose a client and month to prepare a review, or open a stored draft from the sidebar.")
        st.markdown("#### What this workspace does")
        one, two, three = st.columns(3)
        one.markdown("**1 · Prepare**\n\nRetrieves financial and CRM context and applies the existing verified rule set.")
        two.markdown("**2 · Discuss**\n\nPresents attention items, evidence, uncertainty, and questions in meeting-ready language.")
        three.markdown("**3 · Decide**\n\nLets a human approve, reject, or amend narrative fields while preserving calculated evidence.")
        return

    try:
        draft, review = load_draft_review(active_revision_id, store)
        _render_review(st, store, draft, review)
    except AdvisorAppError as exc:
        st.error(str(exc))


if __name__ == "__main__":
    main()
