"""Parse QuickBooks webhook payloads (legacy + CloudEvents).

Intuit historically sent ``eventNotifications`` JSON. Newer notifications use
CloudEvents (``specversion`` + ``data.entityName``). We accept both so verified
webhooks are not silently dropped with ``events_stored=0``.
"""
from __future__ import annotations

import json
from typing import Any

_CLOUD_OP_MAP = {
    "created": "Create",
    "updated": "Update",
    "deleted": "Delete",
    "voided": "Void",
    "merged": "Merge",
}


def _capitalize_entity(name: str) -> str:
    if not name:
        return ""
    return name[0].upper() + name[1:]


def operation_from_cloud_type(event_type: str) -> str:
    parts = event_type.lower().split(".")
    if len(parts) >= 3 and parts[0] == "qbo":
        return _CLOUD_OP_MAP.get(parts[2], "")
    return ""


def entity_from_cloud_type(event_type: str) -> str:
    parts = event_type.split(".")
    if len(parts) >= 2 and parts[0] == "qbo":
        return _capitalize_entity(parts[1])
    return ""


def _parse_legacy(payload: dict) -> list[dict]:
    entities: list[dict] = []
    for notification in payload.get("eventNotifications") or []:
        realm_id = str(notification.get("realmId") or "")
        for entity in (notification.get("dataChangeEvent") or {}).get("entities") or []:
            entities.append(
                {
                    "realm_id": realm_id,
                    "entity_type": str(entity.get("name") or ""),
                    "operation": str(entity.get("operation") or ""),
                    "entity_id": str(entity.get("id") or ""),
                }
            )
    return entities


def _parse_cloud_event(event: dict) -> dict | None:
    if not event.get("specversion"):
        return None

    data = event.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {}

    if not isinstance(data, dict):
        data = {}

    event_type = str(event.get("type") or "")
    entity_type = str(data.get("entityName") or entity_from_cloud_type(event_type))
    operation = str(data.get("operation") or operation_from_cloud_type(event_type))
    entity_id = str(
        data.get("entityId")
        or event.get("intuitentityid")
        or data.get("id")
        or ""
    )
    realm_id = str(
        event.get("intuitaccountid")
        or data.get("realmId")
        or event.get("realmId")
        or ""
    )

    if not entity_type and not entity_id:
        return None

    return {
        "realm_id": realm_id,
        "entity_type": entity_type,
        "operation": operation,
        "entity_id": entity_id,
    }


def parse_qbo_webhook_entities(payload: Any) -> list[dict]:
    """Normalize entity changes from legacy or CloudEvents webhook bodies."""
    if isinstance(payload, list):
        entities: list[dict] = []
        for item in payload:
            if isinstance(item, dict):
                entities.extend(parse_qbo_webhook_entities(item))
        return entities

    if not isinstance(payload, dict):
        return []

    legacy = _parse_legacy(payload)
    if legacy:
        return legacy

    single = _parse_cloud_event(payload)
    if single:
        return [single]

    entities: list[dict] = []
    for item in payload.get("events") or []:
        if isinstance(item, dict):
            parsed = _parse_cloud_event(item)
            if parsed:
                entities.append(parsed)
    return entities


def payload_format_hint(payload: Any) -> str:
    if isinstance(payload, list):
        return "array"
    if not isinstance(payload, dict):
        return "unrecognized"
    if payload.get("eventNotifications"):
        return "legacy"
    if payload.get("specversion"):
        return "cloudevents"
    return "unknown:" + ",".join(sorted(payload.keys())[:8])
