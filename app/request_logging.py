from __future__ import annotations

import json
from typing import Any

from fastapi import Request

MAX_LOGGED_BODY_CHARS = 3000
SENSITIVE_KEYS = {
    "access_token",
    "accesstoken",
    "authorization",
    "client_secret",
    "clientsecret",
    "password",
    "refresh_token",
    "refreshtoken",
    "secret",
    "token",
}


def _mask_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).replace("-", "_").lower()
            if normalized_key in SENSITIVE_KEYS or any(part in normalized_key for part in ("password", "secret", "token")):
                masked[str(key)] = "***"
            else:
                masked[str(key)] = _mask_sensitive(item)
        return masked
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    return value


def _truncate(value: Any) -> Any:
    text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text) <= MAX_LOGGED_BODY_CHARS:
        return value
    return f"{text[:MAX_LOGGED_BODY_CHARS]}... <truncated>"


async def read_payload_for_log(request: Request) -> Any:
    body = await request.body()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive  # type: ignore[attr-defined]

    if not body:
        return None

    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in content_type:
        return "<multipart form-data omitted>"
    if "application/octet-stream" in content_type:
        return "<binary payload omitted>"

    if "application/json" in content_type:
        try:
            return _truncate(_mask_sensitive(json.loads(body.decode("utf-8"))))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "<invalid json payload>"

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return "<non-utf8 payload omitted>"
    return text[:MAX_LOGGED_BODY_CHARS]


def print_request_log(
    *,
    service: str,
    method: str,
    path: str,
    status_code: int | str,
    duration_ms: float,
    request_id: str | None,
    client: str,
    query: dict[str, Any],
    payload: Any,
) -> None:
    separator = "=" * 88
    print(
        "\n".join(
            [
                separator,
                f"[{service}] {method} {path} -> {status_code} ({duration_ms:.1f} ms)",
                f"  request_id: {request_id or '-'}",
                f"  client: {client}",
                f"  query: {json.dumps(_mask_sensitive(query), default=str, ensure_ascii=False)}",
                f"  payload: {json.dumps(payload, default=str, ensure_ascii=False)}",
                separator,
            ]
        ),
        flush=True,
    )
