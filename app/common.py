from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def clean(model: Any) -> dict[str, Any]:
    return {k: v for k, v in model.model_dump(exclude_unset=True).items() if v is not None}


def with_timestamps(payload: dict[str, Any], insert: bool = False) -> dict[str, Any]:
    now = datetime.now(UTC)
    if insert:
        payload.setdefault("createdAt", now)
    payload["updatedAt"] = now
    return payload


def regex_or(fields: list[str], term: str) -> list[dict[str, Any]]:
    return [{field: {"$regex": term, "$options": "i"}} for field in fields]
