"""Tool abstraction layer for local financial data and live HubSpot data."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FINANCIAL_DATA_PATH = PROJECT_ROOT / "data" / "financial_data.json"
load_dotenv(PROJECT_ROOT / ".env")

CLIENT_ID_PATTERN = re.compile(r"^C\d{3}$")
CRM_PROPERTIES = [
    "firstname", "lastname", "email", "client_id", "risk_tolerance",
    "financial_goals", "advisor_context",
]
RETRYABLE_HUBSPOT_STATUSES = {408, 429, 500, 502, 503, 504}


def normalize_client_id(client_id: Any) -> str:
    """Return a canonical client ID or raise ValueError."""
    if not isinstance(client_id, str):
        raise ValueError("Client ID must be a string such as C001.")
    normalized = client_id.strip().upper()
    if not CLIENT_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Client ID must match C followed by three digits.")
    return normalized


def _validate_financial_dataset(dataset: Any) -> list[dict]:
    if not isinstance(dataset, dict):
        raise ValueError("Financial dataset must be a JSON object.")
    required = {"schema_version", "currency", "period_start", "period_end", "clients"}
    if not required.issubset(dataset):
        raise ValueError("Financial dataset is missing required top-level fields.")
    try:
        period_start = date.fromisoformat(dataset["period_start"])
        period_end = date.fromisoformat(dataset["period_end"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Financial dataset period bounds must be ISO dates.") from exc
    if period_start > period_end:
        raise ValueError("Financial dataset period start is after period end.")
    clients = dataset["clients"]
    if not isinstance(clients, list):
        raise ValueError("Financial dataset clients must be a list.")
    ids: list[str] = []
    for client in clients:
        if not isinstance(client, dict) or not isinstance(client.get("client_id"), str):
            raise ValueError("Every financial client must have a string client_id.")
        ids.append(client["client_id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate financial client IDs.")
    return clients


def get_financial_dataset_months() -> list[str]:
    """Return every calendar month covered by the financial dataset metadata."""
    try:
        with FINANCIAL_DATA_PATH.open(encoding="utf-8") as file:
            dataset = json.load(file)
        _validate_financial_dataset(dataset)
        from src.analysis import month_range

        return month_range(dataset["period_start"][:7], dataset["period_end"][:7])
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Could not read financial dataset coverage.") from exc


def get_financial_client_ids() -> list[str]:
    """Return validated client IDs available in the local financial dataset."""
    try:
        with FINANCIAL_DATA_PATH.open(encoding="utf-8") as file:
            dataset = json.load(file)
        clients = _validate_financial_dataset(dataset)
        return sorted(client["client_id"] for client in clients)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Could not read financial dataset clients.") from exc


def get_financial_data(client_id: str) -> dict:
    """Read and validate one client's financial records from local JSON."""
    try:
        normalized = normalize_client_id(client_id)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    try:
        with FINANCIAL_DATA_PATH.open(encoding="utf-8") as file:
            dataset = json.load(file)
        clients = _validate_financial_dataset(dataset)
        match = next((client for client in clients if client["client_id"] == normalized), None)
        if match is None:
            return {"status": "not_found", "client_id": normalized}
        from src.analysis import validate_client_record

        validate_client_record(
            match,
            period_start=dataset["period_start"],
            period_end=dataset["period_end"],
        )
        return {"status": "ok", "data": match}
    except (OSError, json.JSONDecodeError):
        return {"status": "error", "message": "Could not read financial data. Check the JSON file."}
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "error", "message": f"Invalid financial data: {exc}"}


def _hubspot_error_message(status_code: int) -> str:
    if status_code == 401:
        return "HubSpot authentication failed. Check HUBSPOT_ACCESS_TOKEN."
    if status_code == 403:
        return "HubSpot permission denied. Contact read scope is required."
    if status_code == 429:
        return "HubSpot rate limit exceeded after bounded retries."
    return f"HubSpot returned HTTP {status_code}."


def get_crm_profile(
    client_id: str,
    *,
    timeout_seconds: float = 10.0,
    max_attempts: int = 3,
    retry_callback=None,
) -> dict:
    """Retrieve one client profile from HubSpot with bounded transient retries."""
    try:
        normalized = normalize_client_id(client_id)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        return {"status": "error", "message": "Missing HUBSPOT_ACCESS_TOKEN."}
    attempts = max(1, min(int(max_attempts), 3))

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                "https://api.hubapi.com/crm/v3/objects/contacts/search",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "filterGroups": [{"filters": [{
                        "propertyName": "client_id", "operator": "EQ", "value": normalized,
                    }]}],
                    "properties": CRM_PROPERTIES,
                    "limit": 2,
                },
                timeout=timeout_seconds,
            )
            if response.status_code in RETRYABLE_HUBSPOT_STATUSES and attempt < attempts:
                if retry_callback:
                    retry_callback(attempt, response.status_code)
                time.sleep(0.25 * attempt)
                continue
            if not response.ok:
                return {"status": "error", "message": _hubspot_error_message(response.status_code)}
            result = response.json()
            contacts = result.get("results")
            if not isinstance(contacts, list):
                return {"status": "error", "message": "HubSpot returned an invalid response."}
            if not contacts:
                return {"status": "not_found", "client_id": normalized}
            if result.get("total", len(contacts)) > 1 or len(contacts) > 1:
                return {"status": "error", "message": "Duplicate CRM client IDs."}
            properties = contacts[0].get("properties")
            if not isinstance(properties, dict):
                return {"status": "error", "message": "HubSpot profile is missing properties."}
            profile = {name: properties.get(name) for name in CRM_PROPERTIES}
            if profile.get("client_id") != normalized:
                return {"status": "error", "message": "HubSpot returned a mismatched client ID."}
            return {"status": "ok", "data": profile}
        except requests.Timeout:
            if attempt < attempts:
                if retry_callback:
                    retry_callback(attempt, "timeout")
                time.sleep(0.25 * attempt)
                continue
            return {"status": "error", "message": "HubSpot request timed out after bounded retries."}
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return {"status": "error", "message": "Could not retrieve the HubSpot profile."}
    return {"status": "error", "message": "Could not retrieve the HubSpot profile."}
