"""Human review commands for persisted briefing drafts."""

from __future__ import annotations

import argparse
import json

from src.state import StateError, StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and explicitly review persisted Wealth Advisor briefing drafts."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="Inspect a draft/revision and its audit trail.")
    inspect.add_argument("--draft-id", required=True)

    approve = commands.add_parser("approve", help="Approve an unchanged pending draft.")
    approve.add_argument("--draft-id", required=True)
    approve.add_argument("--reviewer", required=True, help="Unauthenticated reviewer label for audit metadata.")
    approve.add_argument("--reason", default="")

    reject = commands.add_parser("reject", help="Reject a pending draft without creating approved memory.")
    reject.add_argument("--draft-id", required=True)
    reject.add_argument("--reviewer", required=True, help="Unauthenticated reviewer label for audit metadata.")
    reject.add_argument("--reason", required=True)

    amend = commands.add_parser(
        "amend-approve",
        help="Create and approve a narrative-only revision while retaining immutable evidence.",
    )
    amend.add_argument("--draft-id", required=True)
    amend.add_argument("--reviewer", required=True, help="Unauthenticated reviewer label for audit metadata.")
    amend.add_argument("--reason", default="")
    amend.add_argument("--interpretation")
    amend.add_argument("--follow-up-question", action="append", dest="follow_up_questions")
    amend.add_argument("--disposition", help="Advisor disposition recorded separately from rule evidence.")
    return parser


def execute(args: argparse.Namespace, store: StateStore | None = None) -> dict:
    active = store or StateStore()
    if args.command == "inspect":
        draft = active.get_draft(args.draft_id)
        return {
            "status": "ok",
            "identity_notice": "Reviewer labels are audit metadata and are not authenticated identities.",
            "draft": draft,
            "audit_trail": active.list_audit(draft["briefing_id"]),
        }
    if args.command == "approve":
        draft = active.approve(args.draft_id, args.reviewer, args.reason)
    elif args.command == "reject":
        draft = active.reject(args.draft_id, args.reviewer, args.reason)
    else:
        draft = active.amend_and_approve(
            args.draft_id,
            args.reviewer,
            interpretation=args.interpretation,
            follow_up_questions=args.follow_up_questions,
            advisor_disposition=args.disposition,
            reason=args.reason,
        )
    return {
        "status": "ok",
        "revision_id": draft["revision_id"],
        "briefing_id": draft["briefing_id"],
        "review_status": draft["status"],
        "final_output": draft["final_output_path"] or None,
        "identity_notice": "Reviewer labels are audit metadata and are not authenticated identities.",
    }


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = execute(args)
    except (StateError, ValueError) as exc:
        result = {"status": "error", "message": str(exc)}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
