"""Command-line entry point for the Wealth Advisor Assistant."""

from __future__ import annotations

import argparse
import json

from src.analysis import validate_month
from src.orchestrator import run_review


def _month_argument(value: str) -> str:
    try:
        return validate_month(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create evidence-based monthly client briefings.")
    parser.add_argument("--client-id", required=True, help="Client ID such as C001")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--month",
        type=_month_argument,
        help="Analysis month in YYYY-MM format; defaults to the dataset's latest month.",
    )
    selection.add_argument(
        "--all-months",
        action="store_true",
        help="Create one briefing for every month covered by the dataset plus a combined index.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_review(args.client_id, month=args.month, all_months=args.all_months)
    print(json.dumps({
        "status": result.status,
        "run_id": result.run_id,
        "message": result.message,
        "model": result.model,
        "output": str(result.output_path) if result.output_path else None,
        "outputs": [str(path) for path in result.output_paths],
        "index": str(result.index_path) if result.index_path else None,
        "completed_months": list(result.completed_months),
        "remaining_months": list(result.remaining_months),
        "draft_ids": list(result.draft_ids),
        "review_notice": "Drafts require an explicit later human review command; reviewer labels are unauthenticated audit metadata.",
        "log": str(result.log_path),
    }, indent=2))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

