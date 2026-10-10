import csv
import os
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

token = os.getenv("HUBSPOT_ACCESS_TOKEN")
if not token:
    raise SystemExit("Missing HUBSPOT_ACCESS_TOKEN in .env")

csv_path = ROOT / "data" / "crm_import" / "hubspot_contacts_updated.csv"

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {token}"})

base_url = "https://api.hubapi.com/crm/v3/objects/contacts"

mapping = {
    "First name": "firstname",
    "Last name": "lastname",
    "Email": "email",
    "Client ID": "client_id",
    "Risk tolerance": "risk_tolerance",
    "Financial goals": "financial_goals",
    "Advisor context": "advisor_context",
}


def check(response):
    if not response.ok:
        raise SystemExit(
            f"HubSpot error {response.status_code}: {response.text}"
        )
    return response.json()


with csv_path.open(encoding="utf-8-sig", newline="") as file:
    rows = list(csv.DictReader(file))

missing = set(mapping) - set(rows[0] if rows else {})
if missing:
    raise SystemExit(f"CSV is empty or missing columns: {sorted(missing)}")

for row in rows:
    properties = {
        internal: row[column].strip()
        for column, internal in mapping.items()
    }

    email = quote(properties["email"], safe="")
    existing = session.get(
        f"{base_url}/{email}",
        params={"idProperty": "email"},
        timeout=30,
    )

    if existing.status_code == 404:
        response = session.post(
            base_url,
            json={"properties": properties},
            timeout=30,
        )
        action = "Created"
    else:
        contact_id = check(existing)["id"]
        response = session.patch(
            f"{base_url}/{contact_id}",
            json={"properties": properties},
            timeout=30,
        )
        action = "Updated"

    check(response)
    print(f"{action}: {properties['client_id']}")

print(f"Finished: {len(rows)} customer profiles saved to HubSpot.")