# Wealth Advisor Assistant

This project is a lightweight multi-agent assistant for a wealth advisor. Given a client ID and an optional analysis month, it retrieves local financial records and a live HubSpot profile, applies deterministic monthly anomaly rules, and creates an evidence-based briefing with follow-up questions.

The system describes suspicious-transfer patterns as signals requiring verification. It never treats them as confirmed fraud, and it does not assume that an account balance is fully allocated to a stated financial goal.

## Architecture

```text
Advisor CLI
    |
    v
Orchestrator -- validates ID, controls flow, saves pending drafts, handles errors
    |
    +--> Data Fetcher Agent (Gemini function calling)
    |       +--> get_financial_data -> local JSON
    |       +--> get_crm_profile    -> live HubSpot API (read only)
    |
    +--> Analyzer Agent (Gemini structured output)
            +--> analyze_financial_data -> deterministic Python rules R1-R5
            +--> interprets rule evidence alongside CRM context
    |
    +--> SQLite state store
            +--> isolated workflow session context
            +--> pending drafts and human-review audit trail
            +--> approved earlier-month summaries
```

Agents and tools have different jobs:

- The **Data Fetcher Agent** is model-driven. Gemini chooses from an explicit two-tool allowlist. The dispatch layer validates arguments, binds calls to the advisor-requested client ID, and permits at most three tool rounds.
- The **Analyzer Agent** is model-driven. It calls the deterministic analysis function and asks Gemini to interpret—not recalculate—the resulting metrics and evidence.
- The **tools** are ordinary, testable Python functions. They read the financial JSON, query HubSpot, and calculate the five rules. Tools do not pretend to be agents.
- The **orchestrator** owns validation, handoffs, stop/continue decisions, output validation, persistence, and graceful failure handling.
- The **SQLite state store** records per-run workflow context, draft revisions, explicit human actions, and a small approved-memory history. It is local persistence, not a vector database or external service.

`data/fixtures/expected_findings.json` and validation summaries are evaluation-only. They are never passed to either agent.

## Analysis contract

The analysis month is selected explicitly with `--month YYYY-MM`, or defaults to the latest month present in the financial dataset. `--all-months` evaluates every dataset month independently. All monetary comparisons use unrounded `Decimal` values and all time windows use actual transaction timestamps.

- **R1:** any individual debit in the selected month strictly greater than 50% of total salary credits in that month. Salary uses only `direction="credit"` and `transaction_type="salary"`; purchases and outgoing transfers are both eligible debits. Equality does not trigger. If salary is zero or unavailable, R1 is `not_evaluated` instead of using a zero threshold.
- **R2:** selected-month debits strictly greater than 1.5 times the average debit total of the three immediately preceding calendar months. All three months must be covered; otherwise the status is `insufficient_history`.
- **R3:** selected-month debits strictly greater than salary credits in that month.
- **R4:** at least five outgoing transfers to the same new recipient in an inclusive 30-minute window whose first transfer is in the selected month. Recipient history uses only earlier transfers. Later transfers inside the qualifying window are included even when the window crosses into the next month, and that attribution is recorded in the evidence.
- **R5:** a selected-month transfer to a new recipient strictly greater than $5,000 and strictly greater than five times the median outgoing-transfer amount from all earlier months. At least five earlier transfers are required; otherwise the status is `insufficient_history`.

Future transactions never enter baselines or recipient history. Each rule has an evaluation record with `triggered`, `not_triggered`, `insufficient_history`, or `not_evaluated`, plus a reason and the relevant baseline period/count. R1 evidence includes its selected-month salary basis, 50% threshold, and triggering transaction IDs and amounts. Triggered findings remain separate and include the transaction IDs, timestamps, amounts, descriptions, recipient IDs, thresholds, and metric references needed to explain them. An insufficient or unevaluated rule is never described as having passed.

## Advisor-facing explanations

Every rule evaluation now includes a descriptive label, such as `Spending exceeds salary (R3)`, and a deterministic plain-language explanation. Each explanation states what the rule checks, the customer's month-specific evidence, the exact comparison, and why the rule triggered, did not trigger, or could not be evaluated. Dollar values are formatted for reading, strict equality is distinguished from greater-than comparisons, and historical periods are named.

Triggered findings separate `what_happened`, `why_flagged`, relevant CRM context, remaining uncertainty, a specific advisor question, and rule-specific evidence references. Gemini supplies only the concise CRM interpretation and advisor-action narrative; it does not calculate thresholds, counts, elapsed time, or monetary differences. The Analyzer cap is configured through `GEMINI_ANALYZER_MAX_OUTPUT_TOKENS`.

Transaction descriptions are shown as unverified descriptions. CRM notes are possible context, not proof of authorization; missing notes do not prove unauthorized activity. Risk tolerance and goals are planning context, not fraud evidence, and the briefing does not infer affordability or savings-goal progress without allocation and contribution data. A recipient absent from available outgoing-transfer history is described as new to the records, not necessarily unknown to the customer.

## Memory model

Each run maintains structured workflow session context containing its run ID, client, requested months, source statuses, agent progress, calculated rule IDs, draft references, and retrieved historical memory. Runs and customers are isolated. In an all-month run the same retrieved financial/CRM bundle is reused, while each month retains independent calculations and memory retrieval. This is workflow session memory, not conversational chatbot memory.

Long-term memory consists only of concise summaries from explicitly human-approved revisions. A summary contains the client/month, ruleset version, metrics, findings, advisor interpretation and disposition, source limitations, approval timestamp, and originating briefing/revision IDs. Before analyzing a month, at most the three most recent active summaries for the same client and earlier analysis months are supplied to the Analyzer. Future-month, pending, rejected, other-client, superseded, and obsolete-ruleset summaries are excluded. Drafts created under an obsolete ruleset must be regenerated before approval or amendment.

Historical summaries are labeled as non-authoritative analysis context. They cannot alter deterministic transaction evidence. Because a retrospective summary may have been approved after the month it describes, its presence does not mean the information was known during that historical month. No extra summarization model call is used.

## Setup on Windows PowerShell

Python 3.12 is used in the existing environment.

```powershell
cd "C:\Users\chiranjit usa\fintech_agent"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set the real values locally in `.env`:

```dotenv
HUBSPOT_ACCESS_TOKEN=replace_with_hubspot_private_app_token
GEMINI_API_KEY=replace_with_gemini_api_key
GEMINI_MODEL=gemini-3.8-flash
GEMINI_REQUESTS_PER_MINUTE=4
GEMINI_FETCHER_MAX_OUTPUT_TOKENS=512
GEMINI_ANALYZER_MAX_OUTPUT_TOKENS=5000
```

`.env` is ignored by Git. Never paste keys into source files, logs, reports, or command output. `.env.example` contains placeholders only.

## Run

### Advisor workspace

The primary product experience is a local advisor workspace. It prepares a monthly review with the existing orchestrator, then turns the validated briefing into a meeting-ready view: a financial snapshot, items needing attention, supporting evidence, uncertainty, suggested client questions, CRM context, and other non-triggered or unevaluated rule observations.

```powershell
streamlit run streamlit_app.py
```

For example, a June review for C010 shows the recorded salary, debits, and cash-flow position, then groups R1, R2, and R5 as attention items. Each item explains what happened, why the existing rule highlighted it, what the CRM may contextualize, what remains unknown, and which evidence references support it. R3 and R4 remain visible as other financial observations rather than disappearing simply because they did not trigger.

The interface does not implement a second analysis engine and does not change R1-R5. It adapts the already validated `Briefing` produced by the orchestrator. Preparing a new review uses the configured Gemini and HubSpot integrations; opening and reviewing a stored draft does not rerun either service. The advisor can approve, reject, or amend permitted narrative fields from the workspace. No timeout, page close, or missing input causes approval.

The workspace is a local single-user demonstration. Reviewer labels are audit metadata, not authenticated identities, and it makes no production access-control claim. The CLI workflows below remain available for automation and debugging.

First make one small model access check:

```powershell
python -m scripts.check_gemini
```

Create a briefing for a specific month:

```powershell
python -m src.main --client-id C001 --month 2026-04
```

Omit the month to use the latest month in the dataset (not the computer's current month):

```powershell
python -m src.main --client-id C001
```

Review every dataset month:

```powershell
python -m src.main --client-id C001 --all-months
```

`--month` and `--all-months` are mutually exclusive. Months must use `YYYY-MM` and fall inside the dataset coverage. The CLI prints a run ID, pending draft IDs, and paths. A validated model result is stored as `outputs/draft_<client>_<month>_<run-id>_<draft-id>.json` with status `pending_review`; it is not automatically final or added to memory. An all-month review creates one pending draft per completed month and `outputs/briefing_index_<client>_all-months_<run-id>.json`. The index is updated as individual months are approved, rejected, or superseded. Structured logs are stored as `logs/run_<run-id>.jsonl`.

An all-month review retrieves the customer's financial data and live HubSpot profile once, then reuses that source bundle while calculating each month independently. The configured Gemini call maximum is shared across the entire run and is never reset between months. With the default eight-call limit, six months require at least one fetcher call and six analyzer calls; retries or extra tool rounds can consume the remaining capacity. If the budget or provider quota stops the run, completed drafts and the index are retained, and the index identifies all remaining months.

## Human review checkpoint

Use the `draft_ids` returned by `src.main`. Review actions run in a later process and do not call Gemini or HubSpot.

Inspect a draft and its audit trail:

```powershell
python -m src.review_cli inspect --draft-id draft_abc123
```

Approve an unchanged pending draft:

```powershell
python -m src.review_cli approve --draft-id draft_abc123 --reviewer "Advisor A" --reason "Evidence reviewed"
```

Reject it:

```powershell
python -m src.review_cli reject --draft-id draft_abc123 --reviewer "Advisor A" --reason "Narrative needs correction"
```

Amend allowed narrative fields and approve a new revision:

```powershell
python -m src.review_cli amend-approve --draft-id draft_abc123 --reviewer "Advisor A" --reason "Client supplied context" --interpretation "The purchase was planned, but cash-flow impact remains." --follow-up-question "Please retain the invoice." --disposition "Explained by client; original rule trigger retained for audit."
```

Only the narrative interpretation, follow-up questions, and separately stored advisor disposition are amendable. Client/month identity, source statuses, metrics, rule evaluations, findings, and evidence references remain immutable. A false-positive or explained-transaction decision belongs in the disposition; it never deletes the original trigger. The original draft and every audit action remain stored. Repeated approval is idempotent. An amended approval—or a newly approved briefing for the same client/month—supersedes the prior active memory entry, which remains in the audit database but is excluded from future retrieval.

There is no implicit approval on timeout, exit, or absent input. Reviewer labels are required audit metadata but are not authenticated identities. Model/AI labels are rejected as human approvers.

State is stored in `state/wealth_advisor.sqlite3`; pending and approved JSON artifacts are under `outputs/`. The SQLite database is ignored by Git.

## Tests and evaluation

The project has one offline-first verification entry point:

```powershell
python -m scripts.verify_project
```

The default command requires no credentials and makes no network calls. It runs the complete unit suite, validates the financial dataset and all 25 customers across all six months, compares current findings with the independent fixtures, checks explanation/evidence contracts, and confirms that the real approval database and approved reports were unchanged. Stateful tests use temporary SQLite databases and output directories. Timestamped JSON and Markdown reports are written to `outputs/verification/`.

After the offline checks pass, run the seven sequential June live cases with:

```powershell
python -m scripts.verify_project --live
```

Run only selected live cases with:

```powershell
python -m scripts.verify_project --live --clients C010 C022 C025
```

Live verification preserves the configured model, request pacing, call limits, retries, and output-token caps. It uses an isolated database, logs, and draft directory below `outputs/verification/live_<timestamp>/`; generated briefings remain `pending_review` and never enter production memory. Authentication failure or identified daily-quota exhaustion stops later requests and marks them `NOT_RUN`. A failed, blocked, or incomplete requested check produces a nonzero exit code. Automated validation covers identities, findings, metrics, rule evaluations, evidence references, CRM limitations, and required review flags, but narrative usefulness and the correctness of financial advice still require human judgment.

The individual lower-level commands remain available:

```powershell
python -m unittest discover -v
python -m scripts.evaluate_offline
python -m scripts.evaluate_all_months
```

The unit suite covers malformed records, missing fields, duplicate client and transaction IDs, selected-month isolation, future-history exclusion, rolling three-month comparisons, early-month insufficiency, R1 equality/above-threshold/multiple-salary/missing-salary behavior, strict R2/R3 boundaries, the inclusive R4 30-minute boundary and month-crossing attribution, previously known recipients, the R5 strict median boundary and history minimum, CLI validation, one-time retrieval reuse, shared all-month call budgeting, HubSpot retry behavior, and the distinction between a missing CRM contact and an API failure. Explanation tests verify customer-specific figures, equality wording, missing coverage and salary wording, new-to-records caveats, R5's two separate thresholds, deterministic finding mechanics, and per-rule evidence references.

It also covers session/customer isolation, persistence across store instances, approved-only and earlier-month memory filters, pending/rejected exclusion, immutable evidence, idempotent approval, amended/superseded revisions, individual all-month status updates, and an offline mocked draft-to-approval-to-memory workflow. Run that demonstration test alone with:

```powershell
python -m unittest tests.test_monthly_workflow.OrchestratorMonthTests.test_offline_mocked_draft_approval_becomes_later_month_memory -v
```

The June evaluator checks all 25 clients against the revised evaluation-only fixture. Current June results are: C001 triggers R3; C004 R2; C010 R1/R2/R5; C022 R2; C025 R4; every other client has no June finding. C022's $6,000 debit is exactly 50% of its $12,000 salary and therefore does not trigger R1. The all-month evaluator reports actual January-June deterministic results without model or network calls. `scripts.check_r1_expectations` independently derives R1 expectations directly from raw JSON without importing the production analyzer.

## Model configuration

The default is `gemini-3.8-flash`, selected because Google's current model catalog describes it as a stable Flash model for agentic and enterprise workflows. It supports the two capabilities used here: function calling and structured outputs. Override it without code changes by setting `GEMINI_MODEL`.

The implementation uses the official `google-genai` SDK. Function calling follows Google's client-side tool flow, and final briefings use JSON Schema structured output plus independent Pydantic validation. Relevant official references:

- [Gemini models](https://ai.google.dev/gemini-api/docs/models)
- [Function calling](https://ai.google.dev/gemini-api/docs/function-calling)
- [Structured outputs](https://ai.google.dev/gemini-api/docs/structured-output)
- [Google Gen AI Python SDK](https://googleapis.github.io/python-genai/)

Gemini calls default to a 30-second request timeout, at most two application attempts, and at most eight calls per review. The application also defaults to four Gemini requests per minute, spacing physical requests 15 seconds apart. The first request can start immediately; retries must wait for both their retry delay and the shared rate-limiter slot. Four requests per minute is this application's conservative initial setting, not a statement about Google's quota.

The rate limiter is shared by all `GeminiClient` instances in one Python process and is protected against concurrent callers. It cannot coordinate separate CLI processes. Other processes, applications, or API keys associated with the same Google project may also consume provider quota, so this limiter cannot guarantee that every HTTP 429 response will be avoided. For retryable 429 responses, the client honors provider retry guidance when available; otherwise it uses bounded exponential backoff with jitter. A provider-identified daily quota exhaustion stops immediately instead of being retried repeatedly.

Fetcher output is capped at 512 tokens and Analyzer output at 5,000 tokens by default. These are configurable with `GEMINI_FETCHER_MAX_OUTPUT_TOKENS` and `GEMINI_ANALYZER_MAX_OUTPUT_TOKENS`; both must be positive integers. Configuration is read afresh from the project-root `.env` for each `Settings.from_env()` call, with an explicitly supplied process environment variable taking precedence. The connectivity check deliberately retains its tiny five-token cap.

Google's current documentation says `gemini-3.8-flash` uses medium thinking by default and supports low, medium, and high thinking levels. This application does not override that model behavior. `max_output_tokens` is a hard cap covering both internal thought tokens and visible response tokens; it does not resize the model's thinking allocation. A low cap can therefore produce `MAX_TOKENS`, truncated, or empty output. The client detects that condition and rejects the result rather than saving an incomplete briefing. See [Gemini thinking and token limits](https://ai.google.dev/gemini-api/docs/thinking).

HubSpot uses a 10-second timeout and at most three attempts for transient HTTP statuses or timeouts. All limits can be adjusted through `.env`.

## Output validation and safety

Briefings are rejected unless they match the required schema and the deterministic evidence:

- client ID, analysis month, source status, and financial metrics must match;
- all five labels, statuses, reasons, explanations, periods, counts, and references must exactly match the Python analysis;
- finding rule IDs, labels, explanations, mechanics, and rule-specific evidence references must exactly match the Python analysis;
- R4 or R5 always requires human review.

Internal Analyzer instructions treat transaction descriptions and CRM notes strictly as data, never instructions. Advisor-facing prose calls transaction descriptions unverified and avoids implementation-security terminology. Logs record run IDs, handoffs, tool names, statuses, durations, rule IDs, bounded retry events, decision summaries, and token usage when supplied by the SDK. They omit API keys, authorization headers, raw exception objects, hidden chain-of-thought, and unnecessary client-data dumps.

## Error behavior

- Invalid client IDs fail before external calls.
- Invalid month formats and months outside dataset coverage fail before model or CRM calls.
- Missing or malformed financial records stop the workflow because they are essential.
- A missing CRM profile (`not_found`) is distinct from a HubSpot/API error. Either condition allows analysis to continue with an explicit CRM limitation.
- Duplicate financial or CRM client IDs return errors rather than choosing one record.
- Authentication, quota, timeout, invalid model output, and tool-dispatch errors return a concise CLI error and a run log without an unhandled traceback.
- Truncated or empty structured output is rejected and is never saved as a draft.
- Valid model output remains `pending_review` until an explicit human command approves, rejects, or amends it.
- The program never silently changes providers or reports a simulated run as live success.

## Assumptions, limitations, and tradeoffs

- Financial data is synthetic USD data covering January-June 2026. `client_id` is the only join key.
- HubSpot is read-only at runtime; the CSV is only an import seed.
- HubSpot is a current profile, not a historical snapshot. For a historical analysis month, clearly dated notes after that month are excluded from interpretation. Undated notes may be retained but are identified as current context; the system does not invent historical goals or risk tolerance.
- The configured rules are demonstration rules, not a complete risk or fraud system. “No configured rules triggered” does not mean “no financial risk.”
- CRM notes may contextualize a transaction (for example, C022's planned furniture purchase) but do not prove authorization or remove cash-flow impact.
- Deterministic Python performs all monetary calculations. The model is reserved for tool selection and evidence interpretation, improving auditability at the cost of a less flexible detector.
- SQLite persistence is a local single-user demonstration. It has no login, authentication, role enforcement, encryption layer, multi-user locking policy beyond SQLite transactions, or production access-control claims.
- Reviewer labels identify audit entries only; they do not prove who operated the CLI.
- The memory layer is compact structured retrieval, not RAG, embeddings, a vector database, or general conversational memory.

See [docs/implementation_report.md](docs/implementation_report.md) for the completed checks and live run artifacts.

