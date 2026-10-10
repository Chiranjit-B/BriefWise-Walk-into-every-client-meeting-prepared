# Implementation report

Date: 2026-10-10 (Asia/Calcutta)

## Advisor product workspace

The project now includes two intentionally focused product features without changing the verified anomaly rules:

1. A meeting-ready presentation layer (`src/presentation.py`) converts a validated `Briefing` into an advisor view containing the monthly financial overview, attention items, rule-specific evidence, uncertainty, other rule observations, CRM limitations, and deduplicated meeting questions. This layer does not recalculate or reclassify a rule.
2. A local Streamlit workspace (`src/advisor_app.py`), exposed through the canonical root entrypoint `streamlit_app.py`, lets an advisor choose a customer and month, prepare a review through the existing orchestrator, open a stored revision without rerunning external services, inspect evidence, and execute the existing approve/reject/amend-and-approve checkpoint. Approval remains explicit and immutable calculated evidence is still enforced by `StateStore`.

Run it from the project root with:

```powershell
.\.venv\Scripts\streamlit.exe run streamlit_app.py
```

The interface preserves the same HubSpot retrieval, Gemini model settings, request pacing, output-token limits, model-call budget, SQLite memory, and audit trail used by the CLI. It is a local single-user demonstration with no authentication or production access-control claim. Transaction descriptions are labeled unverified, CRM context is not treated as authorization evidence, and quiet months explicitly say that no configured rule trigger is not a complete risk assessment.

Offline presentation tests cover attention-item and evidence mapping, quiet-month observations, cash-flow wording, missing CRM limitations, question deduplication, persistent stored-draft loading, and unknown revision errors. The financial client selector is also checked against all 25 dataset customers.

The completed offline verification run on 2026-10-10 passed all 17 required groups: **72 unit tests passed**, all **150 customer-month analyses** were validated, the June findings matched the independent 25-client fixture, and the independent R1 derivation matched all six months. The Streamlit app also completed its headless render smoke test with zero exceptions, and its local server returned HTTP 200. No Gemini or HubSpot request was made. The automated browser surface was unavailable in the execution environment, so no claim is made about a completed browser-based visual inspection.

## Unified verification entry point

`python -m scripts.verify_project` now performs the complete credential-free, network-free verification workflow and writes timestamped JSON and Markdown reports under `outputs/verification/`. It reuses the existing unit tests, June fixture evaluator, all-month evaluator, and independent R1 derivation. Stateful coverage uses temporary SQLite/output paths and checks that the real approval database and approved artifacts retain identical hashes.

`--live` runs the agreed June cases sequentially only after offline success. `--clients` limits that live list. Live runs use `outputs/verification/live_<timestamp>/` for their isolated database, pending drafts, and logs; they never approve drafts. Reports include configured model limits, per-run artifact paths, model calls and available token usage, non-pass cases, and narrative areas requiring human inspection. Authentication or identified daily-quota exhaustion stops later cases as `NOT_RUN`.

The default verifier was run offline on 2026-10-10. The latest configuration-corrected run reports every verification group passing, including the full unit suite, all **150 customer-month analyses**, the 25-client June fixture, and the independently derived R1 fixture. It also confirms that the 5,000-token Analyzer cap survives the environment-mutating offline tests. No protected production artifact changed, and no live verification was run.

## Advisor explanation quality update

New drafts now contain deterministic, advisor-readable explanations for all five rule evaluations. Each one carries a descriptive label, explains the plain-language check, names the selected month and any historical comparison period, supplies customer-specific values, states the exact comparison, and distinguishes `triggered`, `not_triggered`, `insufficient_history`, and `not_evaluated`. Triggered findings separately present what happened, why it was flagged, possible CRM context, uncertainty, a specific follow-up question, and evidence references limited to that rule.

Gemini no longer has to reproduce the full calculation-heavy briefing. It returns a compact `BriefingNarrative` containing the executive summary, CRM interpretation, uncertainty, and advisor questions. The application combines that narrative with immutable deterministic mechanics and validates the assembled `Briefing`. The Analyzer limit is environment-configurable and is now set to 5,000 tokens in the project configuration; no financial rule changed.

CRM and transaction wording was tightened: transaction descriptions are explicitly unverified, CRM notes are possible context rather than authorization evidence, absent notes do not prove unauthorized activity, and current risk tolerance/goals do not establish fraud, affordability, or savings-goal progress. New-recipient wording means absent from available outgoing-transfer records, not necessarily unknown to the customer.

Offline examples from the existing June data:

- **C001 — Spending exceeds salary (R3):** June debits were $5,173.85 and salary credits were $4,800.00. Recorded debits exceeded recorded salary by $373.85, so R3 triggered; the explanation also states that this does not establish savings balance, solvency, or other income.
- **C010 — Large debit relative to salary (R1) and large transfer to a new recipient (R5):** salary credits were $14,000.00, the 50% threshold was $7,000.00, and the $8,000.00 debit exceeded it. The same $8,000.00 transfer to C010-R08 exceeded the separate $5,000 threshold and $1,500.00, five times the $300.00 median of eight January-May outgoing transfers.
- **C022 — R1 equality boundary:** salary credits were $12,000.00, the threshold was $6,000.00, and the unverified furniture-store description accompanied a $6,000.00 debit that equaled rather than exceeded the threshold. R1 did not trigger.
- **C025 — Rapid transfers to a new recipient (R4):** five $400.00 transfers totaling $2,000.00 went to C025-R08 from 14:00:00 UTC to 14:24:00 UTC on June 18. The 24-minute span satisfies the inclusive 30-minute condition; the recipient was new to the available records, which does not necessarily mean unknown to the customer.

These are deterministic offline examples, not live model generations. Existing approved reports were not rewritten and no live model batch was run.

Files changed for this update: `src/analysis.py`, `src/models.py`, `src/agents.py`, `tests/test_analysis.py`, `tests/test_agent_limits.py`, `tests/test_monthly_workflow.py`, `tests/test_state.py`, `README.md`, and this report.

Offline verification passed: Python compilation completed; **62 unit tests passed, 0 failed**; independent R1 derivation matched all **25 clients x 6 months**; the June regression matched **25/25** clients; and the January-June evaluator retained the previously documented results. No live API request was made.

## Income-relative R1 update

R1 now flags an individual selected-month debit only when its unrounded `Decimal` amount is strictly greater than 50% of total selected-month salary credits. Salary includes only credit transactions whose type is `salary`; both purchases and outgoing transfers count as debits. Equality does not trigger. If salary is zero or absent, R1 has status `not_evaluated` with an explicit reason and no zero-dollar threshold.

The deterministic evidence and briefing metrics now include the salary basis and personalized R1 threshold. Triggered evidence also includes transaction IDs and amounts. Analyzer instructions explicitly explain the personalized threshold and prohibit describing `not_evaluated` as passed. R2, R3, R4, and R5 are unchanged; R5 retains its independent strict `$5,000` condition.

Approved memory and draft artifacts are tagged with ruleset `2026-10-r1-income-relative-v2`. Unversioned or obsolete approved summaries are excluded from retrieval, and obsolete drafts cannot be approved or amended without regeneration. This prevents older fixed-threshold R1 conclusions from silently entering new reviews.

Independent raw-JSON derivation produced these R1 results across all 25 clients and six months:

- January-May: no R1 triggers and no salary-unavailable clients.
- June: only C010 triggers R1; salary is $14,000, threshold is $7,000, and transaction C010-T0105 is $8,000.
- C022 does not trigger R1: salary is $12,000 and its $6,000 debit is exactly the $6,000 threshold.

Complete June findings under the revised specification:

- C001: R3
- C004: R2
- C010: R1, R2, R5
- C022: R2
- C025: R4
- All other clients: none

Offline validation commands:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m scripts.check_r1_expectations
.\.venv\Scripts\python.exe -m scripts.evaluate_offline
.\.venv\Scripts\python.exe -m scripts.evaluate_all_months
```

Final revised-R1 verification: **54 unit tests passed, 0 failed**; independent R1 fixture derivation passed for all **25 clients × 6 months**; and the June fixture matched **25/25** clients.

The older live artifacts listed later in this report were generated under the previous fixed-$5,000 R1 definition. They are retained as historical artifacts and were not rerun or treated as current validation for this change.

## Memory and human checkpoint update

The assistant now has a built-in SQLite workflow-state layer and a real explicit review gate. A generated, deterministically validated briefing is saved as `pending_review`; it is neither final nor eligible for historical memory until a later human CLI action approves it. No CRM records were modified, no credentials were printed, and no live model/API batch was run for this update.

### Storage and behavior

- `state/wealth_advisor.sqlite3` stores isolated workflow sessions, draft revisions, audit actions, and concise approved summaries. It is ignored by Git.
- `outputs/draft_*.json` preserves original pending artifacts.
- `outputs/approved_*.json` contains explicitly approved revisions and review metadata.
- `outputs/briefing_index_*_all-months_*.json` shows each month independently and refreshes its review status after later CLI actions.
- Each run context records run/client/months, source statuses, progress, calculated rule IDs, draft references, and the approved historical summaries supplied to each month.
- Retrieval is capped at three active, approved summaries for the same customer from strictly earlier analysis months. Pending, rejected, future-month, other-client, and superseded entries are excluded.
- Historical summaries are context only, never transaction evidence. The prompt states that later approval does not imply the information was known during the historical month.
- Approval uses an existing validated briefing summary, so no new summarization/model request is added and existing call/token budgets remain unchanged.

Amendments may change only narrative interpretation, follow-up questions, or a separate advisor disposition. Identity, month, source status, metrics, rule evaluations, findings, and evidence references are immutable. An explained or false-positive disposition does not erase the triggering rule. Repeated approval does not duplicate memory. An amended approval creates a new revision, marks the prior revision superseded, and makes only the new summary active for retrieval. A newly approved independent briefing for the same client/month likewise supersedes the older active memory summary without deleting its audit record.

Reviewer labels are required audit metadata, not authenticated identities. An AI/model label is not accepted as a human approver. This is a local single-user demonstration with no production authentication or access-control claim.

### Exact commands

Create one real pending draft:

```powershell
.\.venv\Scripts\python.exe -m src.main --client-id C001 --month 2026-04
```

Then copy its returned `draft_id` into one of these commands:

```powershell
.\.venv\Scripts\python.exe -m src.review_cli inspect --draft-id <DRAFT_ID>
.\.venv\Scripts\python.exe -m src.review_cli approve --draft-id <DRAFT_ID> --reviewer "Advisor A" --reason "Evidence reviewed"
.\.venv\Scripts\python.exe -m src.review_cli reject --draft-id <DRAFT_ID> --reviewer "Advisor A" --reason "Needs correction"
.\.venv\Scripts\python.exe -m src.review_cli amend-approve --draft-id <DRAFT_ID> --reviewer "Advisor A" --reason "Client context reviewed" --interpretation "Updated advisor interpretation" --follow-up-question "Please provide supporting documentation." --disposition "Explained; retain trigger for audit."
```

Review actions work in later processes and do not rerun Gemini or HubSpot. There is no timeout or exit-based implicit approval.

### Offline verification

The full suite includes an explicitly labeled offline demonstration using mocked model/API agents. It creates a January pending draft, approves it, opens another workflow against the same persistent database, and confirms that the approved January summary reaches an April analysis while the draft itself did not enter memory before approval.

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_monthly_workflow.OrchestratorMonthTests.test_offline_mocked_draft_approval_becomes_later_month_memory -v
```

The complete offline suite and June regression commands are:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src scripts tests
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m scripts.evaluate_offline
```

Memory/checkpoint milestone result: **48 unit tests passed, 0 failed**. The later R1 update expands and supersedes the rule-specific validation results above.

## Monthly analysis update

The assistant now supports `--month YYYY-MM`, `--all-months`, and a dataset-latest default when neither option is supplied. Month selection is carried through deterministic analysis, the Analyzer prompt and schema, logs, and filenames. The dataset, customer IDs, CRM records, and scenario assignments were not changed. The later income-relative R1 update above supersedes this milestone's original threshold statement.

Examples:

```powershell
python -m src.main --client-id C001 --month 2026-04
python -m src.main --client-id C001 --all-months
python -m src.main --client-id C001  # latest dataset month
```

One live monthly review can be run with:

```powershell
.\.venv\Scripts\python.exe -m src.main --client-id C001 --month 2026-04
```

No live six-month model batch was run for this update.

### Changed behavior

- R1 and R3 use only the selected month. R1's current threshold is defined in the update above.
- R2 uses the three immediately preceding calendar months and reports `insufficient_history` unless all three months are covered.
- R4 requires the first transfer in its inclusive 30-minute window to occur in the selected month. Earlier transfers determine whether the recipient is new; transfers later in the same window are included even if they cross into the next month.
- R5 uses outgoing transfers from all available earlier months, requires at least five, and never includes a current- or future-month transfer in its baseline or recipient history.
- Every rule has a separate `triggered`, `not_triggered`, `insufficient_history`, or `not_evaluated` evaluation with its reason and relevant period/count. Only triggered rules become findings.
- Historical CRM interpretation removes clearly dated notes later than the selected month. Current CRM goals and risk tolerance are explicitly identified as current context rather than a historical snapshot; no historical values are invented.
- An all-month run retrieves financial and CRM sources once, reuses the bundle, saves one briefing per completed month, and writes a combined index. Its Gemini maximum-call budget is shared across the entire run. If calls or quota run out, completed files remain and the index lists unfinished months and the stopping reason.

### Offline verification

Commands run with the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src scripts tests
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m scripts.evaluate_offline
.\.venv\Scripts\python.exe -m scripts.evaluate_all_months
```

- Compilation passed.
- Unit tests at the monthly-analysis milestone: **39 passed, 0 failed**. The later memory/review update expands this suite; see its verification section above.
- June regression: **25/25 clients matched** the unchanged fixture.
- Current June findings after the R1 update: C001 R3; C004 R2; C010 R1/R2/R5; C022 R2; C025 R4; all other clients none.

Actual offline January-June results:

| Month | Triggered findings | History status |
|---|---|---|
| 2026-01 | None | R2 insufficient for 25/25; R5 insufficient for 25/25 |
| 2026-02 | None | R2 insufficient for 25/25; R5 insufficient for 25/25 |
| 2026-03 | None | R2 insufficient for 25/25; R5 insufficient for 25/25 |
| 2026-04 | None | R2 fully evaluated for 25/25; R5 insufficient for 16/25 |
| 2026-05 | None | R2 fully evaluated for 25/25; R5 insufficient for 1/25 |
| 2026-06 | C001 R3; C004 R2; C010 R1/R2/R5; C022 R2; C025 R4 | R1, R2, and R5 fully evaluated for 25/25 |

The January-March R2 results are intentionally `insufficient_history`, not passes and not zero-filled baselines. These results are deterministic offline evaluations; they were not forced to resemble June.

## Outcome

All four requested implementation steps are complete. Offline validation passed, Gemini access was verified, and all six requested live end-to-end reviews completed using the live HubSpot API and Gemini. No CRM records were modified or reimported.

## Files created or changed

Changed:

- `src/analysis.py` — input validation, exact deterministic rules, and richer evidence.
- `src/tools.py` — validated local-data tool, read-only HubSpot tool, bounded retries, and distinct missing/error statuses.

Created:

- `src/config.py` — bounded environment-driven model settings.
- `src/models.py` — Pydantic bundle and briefing schemas.
- `src/gemini_client.py` — official SDK wrapper, bounded calls, safe errors, structured output, and token logging.
- `src/agents.py` — Data Fetcher Agent, allowlisted dispatch, and Analyzer Agent.
- `src/orchestrator.py` — handoffs, stop/continue decisions, validation, persistence, and graceful errors.
- `src/run_logging.py` — concise structured JSONL execution logs.
- `src/main.py` — runnable CLI.
- `tests/test_analysis.py`, `tests/test_tools.py`, `tests/__init__.py` — offline contract and boundary tests.
- `scripts/evaluate_offline.py` — all-client evaluation using the evaluation-only fixture.
- `scripts/check_gemini.py` — one small model access check.
- `scripts/run_live_cases.py` — one-time runner for the five remaining assignment cases.
- `requirements.txt` — runtime dependencies.
- `.env.example` — placeholders and safe defaults only.
- `README.md` — product, architecture, setup, operation, safety, and tradeoffs.
- `docs/implementation_report.md` — this report.

Generated artifacts:

- Six validated briefing JSON files under `outputs/`.
- Successful and diagnostic JSONL run logs under `logs/`.

## Exact setup and run commands

```powershell
cd "C:\Users\chiranjit usa\fintech_agent"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Replace placeholders in .env locally.
python -m scripts.check_gemini
python -m unittest discover -v
python -m scripts.evaluate_offline
python -m src.main --client-id C001
```

The existing `.env` was preserved. Its values were never printed, copied, or included in reports.

## Offline checks

- Python compilation: passed.
- Unit tests: **24 passed, 0 failed** after the rate-limit/output-bound update.
- Full deterministic evaluation: **25/25 clients matched** `data/fixtures/expected_findings.json`.
- Current triggered clients only: C001 (R3), C004 (R2), C010 (R1/R2/R5), C022 (R2), C025 (R4).
- Control C002: no configured rules triggered.
- Output schema revalidation: all six generated briefings passed Pydantic validation.

Boundary checks passed for:

- exactly 50% of selected-month salary not triggering R1, while an amount above it does;
- exactly 1.5 times baseline not triggering strict R2;
- spending equal to salary not triggering strict R3;
- an R4 fifth transfer exactly at 30 minutes triggering, but 30 minutes and one second not triggering;
- previously known recipients not triggering R4;
- R5 equality at five times the median not triggering;
- fewer than five historical transfers not triggering R5;
- transient HubSpot retry followed by `not_found`;
- HubSpot authentication failure remaining distinct from `not_found`.

## Live checks and generated artifacts

The small Gemini access request succeeded with `gemini-3.8-flash`. The six live reviews below then completed successfully:

| Client | Expected case | Result | Briefing | Log |
|---|---|---|---|---|
| C001 | Spending exceeds salary (R3) | Success | `outputs/briefing_C001_20261010T055010Z_d8081c6c.json` | `logs/run_20261010T055010Z_d8081c6c.jsonl` |
| C022 | Historical run under the former fixed R1 definition | Success at that milestone; not current R1 validation | `outputs/briefing_C022_20261010T055139Z_97a161df.json` | `logs/run_20261010T055139Z_97a161df.jsonl` |
| C010 | Large new-recipient transfer (R1/R2/R5) | Success | `outputs/briefing_C010_20261010T055206Z_2215df53.json` | `logs/run_20261010T055206Z_2215df53.jsonl` |
| C025 | Rapid transfers to a new recipient (R4) | Success | `outputs/briefing_C025_20261010T055220Z_684b3f1b.json` | `logs/run_20261010T055220Z_684b3f1b.jsonl` |
| C005 | CRM profile intentionally missing | Success with explicit CRM limitation | `outputs/briefing_C005_20261010T055238Z_b2098121.json` | `logs/run_20261010T055238Z_b2098121.jsonl` |
| C002 | No configured rule triggered | Success | `outputs/briefing_C002_20261010T055318Z_9d7c49ab.json` | `logs/run_20261010T055318Z_9d7c49ab.jsonl` |

No simulated result is listed as a live success.

## Model configuration and blockers

- SDK: official `google-genai` package, installed version 2.29.0 during verification.
- Default model: `gemini-3.8-flash`.
- Override: set `GEMINI_MODEL` in `.env`.
- Default bounds: 30-second Gemini timeout, two application attempts, eight model calls per review; 10-second HubSpot timeout and three attempts for transient failures.
- Token usage is recorded in model-call log events when returned by the SDK.

An initial structured-output smoke request returned HTTP 400 because the SDK's OpenAPI-style `response_schema` conversion did not accept the nested briefing schema for this model/API combination. The implementation was changed to the SDK's documented `response_json_schema` path while retaining strict Pydantic validation after receipt. The schema diagnostic and subsequent six live reviews all succeeded. There are no remaining environment blockers.

## Security and operating notes

- `.env` remains directly under the project root and is listed in `.gitignore`.
- `.env.example` contains placeholders only.
- Logs contain no keys, authorization headers, raw SDK exception dumps, chain-of-thought, or bulk client-data dumps.
- HubSpot usage is search/read-only. The contact import script was not run.
- Expected findings remain confined to offline evaluation code and tests; they are never included in agent prompts.

## Rate limiting and output bounds update

The Gemini client now validates and supports these application settings:

- `GEMINI_REQUESTS_PER_MINUTE=4`
- `GEMINI_FETCHER_MAX_OUTPUT_TOKENS=512`
- `GEMINI_ANALYZER_MAX_OUTPUT_TOKENS=5000`

All physical attempts, including retries, pass through one locked limiter shared by Gemini clients in the Python process. At the default rate, requests are spaced 15 seconds apart and the first request may begin immediately. Retryable HTTP 429 responses honor provider retry guidance when available, otherwise using bounded exponential backoff with jitter. Provider-identified daily exhaustion is not repeatedly retried. Pacing, retries, finish reasons, and available token counts are logged without credentials or raw client data.

The configured `gemini-3.8-flash` model uses medium thinking by default. Google documents that `max_output_tokens` includes both thought and visible response tokens and acts as a hard cutoff without changing thinking allocation. The application therefore detects `MAX_TOKENS`/incomplete output and rejects truncated or invalid JSON instead of saving it.

This is a one-process application limiter. Separate CLI processes, other applications, and API keys on the same Google project can consume shared provider quota, so the limiter cannot guarantee avoidance of every 429 response.

Offline tests use mocked monotonic clocks, sleepers, and SDK/network responses. They verify 15-second spacing at four requests per minute, concurrent-call serialization, shared limiter state across client instances, provider `Retry-After`, exponential backoff with deterministic jitter, daily-quota short-circuiting, maximum-call enforcement, both agent output caps, and rejection of truncated or invalid structured output. No live API request was made for this update.
