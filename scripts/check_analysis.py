from src.analysis import analyze_financial_data
from src.tools import get_financial_data

for client_id in ["C001", "C004", "C010", "C022", "C025", "C002"]:
    result = get_financial_data(client_id)

    if result["status"] != "ok":
        print(f"{client_id}: Could not retrieve financial records.")
        continue

    analysis = analyze_financial_data(result["data"], "2026-06")
    rules = [finding["rule_id"] for finding in analysis["findings"]]

    print(f"{client_id}: {rules}")
