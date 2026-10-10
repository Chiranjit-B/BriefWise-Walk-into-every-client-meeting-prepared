# Connected USD Wealth Advisor demonstration data

All records are synthetic. This realistic demonstration dataset deliberately inserts five situations to test application behavior; it does not establish real-world fraud detection accuracy. Suspicious patterns require client verification and human review, not a confirmed-fraud conclusion.

## Run

Python 3.9+, standard library only:

`python generate_dataset.py --source source_contacts.csv --output-dir generated`

Run without `-O`, because assertions implement validation. source_contacts.csv is the exact supplied starting file, preserved for reproducibility and identity comparison. It retains the obsolete original note and is NOT an application input. Use only the final updated contacts file for CRM import. Currency conversion is not performed: USD incomes and transactions are generated directly. Existing dollar goal strings are preserved exactly.

## Inputs for the application

financial_data.json: schema_version, USD currency, period bounds, clients array. Each of 25 clients has client_id, currency, expected recurring monthly_income, opening_balance, closing_balance, transactions. Each transaction has globally unique ID, explicit UTC timestamp, positive numeric amount, direction, type, category, recipient and ordinary description. No dates duplicated or scenario labels added.

hubspot_contacts_updated.csv: 24 contacts, C005 deliberately absent. Join Client ID to financial client_id. Names, email, risk and complete goal strings are unchanged. No transaction history belongs in the CRM import.

Only these two files are production inputs. expected_findings.json contains scenario assignments, calculated evidence and required warnings; it is exclusively for evaluation and must never be passed to agents. validation_summary.csv contains actual recomputed metrics and rule IDs, and is also evaluation-only. No agents or architecture are implemented here.

## Accounting and realism

Integer cents/Decimal arithmetic avoids binary accounting errors. JSON amounts are numbers with at most two decimal places; CSV goals use $. Every credit is salary, once monthly and consistent. Rent recurs at a consistent amount and recipient. All debits, including personal transfers, count as outgoing spending. There are no refunds, loans or own-account transfers. Closing = opening + credits - debits; all running balances are nonnegative. UTC is a dataset convention, not a customer-location claim.

Normal customers vary in salary, cash reserves, regular spending, category habits and monthly counts. Some ordinary customers pay a new recipient once. Goals retain their original target amounts and deadlines. Cash balances are not represented as fully earmarked; retirement targets are contribution milestones, not sufficient retirement wealth. No holdings, debts or ages are invented.

## June demonstration rules

R1: individual debit strictly above 50% of actual June salary credits; do not evaluate when salary is unavailable or zero.
R2: June total debits strictly above 1.5 times March-May mean debits, requiring all three months.
R3: June debits exceed actual June salary credits.
R4: five or more outgoing transfers to one new recipient within an inclusive 30-minute window, with no earlier transfer to that recipient before the first window payment.
R5: transfer to a new recipient strictly above both $5,000 and five times the January-May median outgoing transfer; require five historical transfers.

New means unseen within the observed history, not proof of a first-ever payment. R1 and R5 have separate thresholds and can identify the same transaction while R2 records its monthly impact. Overlapping rules are related evidence, not separate incidents. These thresholds are demo assumptions, not universal financial or fraud rules. Evidence averages retain Decimal precision; displayed summary averages round to cents.

## Deterministic assignment

Before generating financial records, random.Random(42).sample selects five distinct IDs from the source CSV row order. They receive furniture, increasing spending, negative cash flow, transfer burst and large transfer in that order. A separate RNG generates all financial variation. Source row order is therefore part of the reproducibility contract. The remaining 19 CRM customers and C005 are ordinary controls.

## CRM edits

All advisor notes are refreshed with individualized pre-June dates, each existing goal's exact target date, and natural discussion topics. The old C002 INR furniture note is removed. C003 now consistently refers to 2031-12-31. The selected furniture customer gets a May note anticipating approximately $6,000 in June; this is a possible explanation, not authorization proof. Increasing-spending and budget-pressure customers have appropriate review notes. The two suspicious-transfer customers receive ordinary notes without explanations of those specific payments. Final inputs contain no INR references, rule IDs, or test scenario labels.

## HubSpot mapping

| CSV header | Property | Suggested field type |
|---|---|---|
| First name | Standard firstname | Single-line text |
| Last name | Standard lastname | Single-line text |
| Email | Standard email | Standard email |
| Client ID | Custom wealth_client_id | Single-line text |
| Risk tolerance | Custom wealth_risk_tolerance | Dropdown: low, medium, high |
| Financial goals | Custom wealth_financial_goals | Multi-line text |
| Advisor context | Custom wealth_advisor_context | Multi-line text |

Four custom Contact properties only. These are proposed mappings, not statements about current account entitlements. Map each column explicitly when importing and confirm 24 contacts. CSV escaping preserves multiline notes, commas and quotation marks. No live HubSpot calls are made.

## Validation

The detector reads actual transactions without consulting scenario assignments or generation totals. Assertions check scenario matches, counts, unique IDs, positive cents, timestamps, categories, salary/rent recurrence, balances, exactly two suspicious clients, original CRM values, CSV round trip, furniture note alignment, goal deadlines and absence of label leakage. expected_findings evidence is calculated rather than hardcoded. The summary labels derive from the detector, never from assignment labels.

## Scenario-to-client mapping (test only)

| Client | Scenario | Rules |
|---|---|---|
| C022 | expected furniture purchase | R2 |
| C004 | increasing spending | R2 |
| C001 | negative cash flow | R3 |
| C025 | suspicious transfer burst | R4 |
| C010 | suspicious large transfer | R1, R2, R5 |
