import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# Load .env from the project root.
project_root = Path(__file__).resolve().parent.parent
load_dotenv(project_root / ".env")

token = os.getenv("HUBSPOT_ACCESS_TOKEN")

if not token:
    raise SystemExit("Missing HUBSPOT_ACCESS_TOKEN in your .env file.")

properties = [
    "firstname",
    "lastname",
    "email",
    "client_id",
    "risk_tolerance",
    "financial_goals",
    "advisor_context",
]

try:
    response = requests.post(
        "https://api.hubapi.com/crm/v3/objects/contacts/search",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "client_id",
                            "operator": "EQ",
                            "value": "C001",
                        }
                    ]
                }
            ],
            "properties": properties,
            "limit": 2,
        },
        timeout=20,
    )

    if response.status_code == 401:
        raise SystemExit("Authentication failed. Check your HubSpot key.")
    if response.status_code == 403:
        raise SystemExit(
            "Permission denied. Check that your key has "
            "the crm.objects.contacts.read scope."
        )
    if response.status_code == 400:
        raise SystemExit(
            "Invalid request. Check the internal names of your "
            "four custom properties in HubSpot."
        )

    response.raise_for_status()
    data = response.json()
    contacts = data.get("results", [])

    if not contacts:
        print("No CRM profile found for C001.")
    elif data.get("total", len(contacts)) > 1:
        print("Multiple profiles found for C001. Check for duplicates.")
    else:
        profile = contacts[0]["properties"]
        print("SUCCESS: Retrieved C001 from HubSpot.")
        print(json.dumps(profile, indent=2, ensure_ascii=False))

except requests.Timeout:
    print("HubSpot request timed out. Try again.")
except requests.RequestException:
    print("HubSpot request failed. Check your connection and try again.")
except ValueError:
    print("HubSpot returned an unexpected response.")