from __future__ import annotations

import json
import re
from typing import Any

from fastapi import Request

MAX_LOGGED_BODY_CHARS = 3000
RESET = "\033[0m"
BOLD = "\033[1m"
BLUE = "\033[94m"
CYAN = "\033[96m"
GREEN = "\033[92m"
MAGENTA = "\033[95m"
YELLOW = "\033[93m"
GRAY = "\033[90m"
RED = "\033[91m"
SENSITIVE_KEYS = {
    "access_token",
    "accesstoken",
    "authorization",
    "client_secret",
    "clientsecret",
    "code",
    "hmac",
    "password",
    "refresh_token",
    "refreshtoken",
    "secret",
    "signature",
    "state",
    "token",
}
JSON_TOKEN_PATTERN = re.compile(
    r'("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false)\b|\bnull\b|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b'
)


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


def _color_json(value: Any) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False, indent=2)

    def replace(match: re.Match[str]) -> str:
        json_string, key_suffix, boolean = match.group(1), match.group(2), match.group(3)
        token = match.group(0)
        if json_string and key_suffix:
            return f"{CYAN}{json_string}{RESET}{key_suffix}"
        if json_string:
            return f"{GREEN}{json_string}{RESET}"
        if boolean:
            return f"{MAGENTA}{boolean}{RESET}"
        if token == "null":
            return f"{GRAY}{token}{RESET}"
        return f"{YELLOW}{token}{RESET}"

    return JSON_TOKEN_PATTERN.sub(replace, text)


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
    separator = f"{BLUE}{'-' * 88}{RESET}"
    print(
        "\n".join(
            [
                separator,
                f"{BOLD}[{service}]{RESET} {CYAN}{method}{RESET} {path} -> {GREEN if str(status_code).startswith('2') else RED}{status_code}{RESET} ({duration_ms:.1f} ms)",
                f"{GRAY}request_id:{RESET} {request_id or '-'}",
                f"{GRAY}client:{RESET} {client}",
                f"{GRAY}query:{RESET}\n{_color_json(_mask_sensitive(query))}",
                f"{GRAY}payload:{RESET}\n{_color_json(_mask_sensitive(payload))}",
                separator,
            ]
        ),
        flush=True,
    )
