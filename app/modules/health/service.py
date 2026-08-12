from datetime import UTC, datetime

from app.db import now_utc

started_at = datetime.now(UTC)


async def get_health() -> dict:
    uptime = int((datetime.now(UTC) - started_at).total_seconds())
    return {"service": "CommandCtr Backend", "status": "ok", "timestamp": now_utc(), "uptime": uptime}
