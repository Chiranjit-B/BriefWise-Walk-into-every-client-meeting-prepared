"""Create pending drafts for assignment live cases; never auto-approve them."""

from src.orchestrator import run_review

CLIENT_IDS = ("C022", "C010", "C025", "C005", "C002")


def main() -> int:
    failures = 0
    for client_id in CLIENT_IDS:
        result = run_review(client_id)
        print(
            f"{client_id}: status={result.status}; run_id={result.run_id}; "
            f"draft_ids={list(result.draft_ids)}; output={result.output_path}; "
            f"log={result.log_path}; message={result.message}"
        )
        failures += result.status != "ok"
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
