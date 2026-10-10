import json
from decimal import Decimal
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
file_path = project_root / "data" / "financial_data.json"

with file_path.open(encoding="utf-8") as file:
    data = json.load(file, parse_float=Decimal)

client = next(
    (item for item in data["clients"] if item["client_id"] == "C001"),
    None,
)

if client is None:
    raise SystemExit("C001 was not found in financial_data.json.")

# Analyze the latest month declared by the dataset.
analysis_month = data["period_end"][:7]
monthly_transactions = [
    transaction
    for transaction in client["transactions"]
    if transaction["timestamp"].startswith(analysis_month)
]

monthly_debits = sum(
    (
        Decimal(str(transaction["amount"]))
        for transaction in monthly_transactions
        if transaction["direction"] == "debit"
    ),
    Decimal("0"),
)

income = Decimal(str(client["monthly_income"]))

print("SUCCESS: Retrieved C001's financial records.")
print(f"Currency: {client['currency']}")
print(f"Total transactions: {len(client['transactions'])}")
print(f"Monthly income: {income:.2f}")
print(f"Analysis month: {analysis_month}")
print(f"Selected-month spending: {monthly_debits:.2f}")
print(f"Income minus spending: {income - monthly_debits:.2f}")
