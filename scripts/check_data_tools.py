from src.tools import get_crm_profile, get_financial_data

for client_id in ["C001", "C005"]:
    financial = get_financial_data(client_id)
    crm = get_crm_profile(client_id)

    print(f"\nClient: {client_id}")
    print(f"Financial data: {financial['status']}")
    print(f"CRM profile: {crm['status']}")

    for result in [financial, crm]:
        if result["status"] == "error":
            print(f"Error: {result['message']}")