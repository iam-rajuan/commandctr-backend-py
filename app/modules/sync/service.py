from starlette import status

from app.db import collection, oid, serialize
from app.errors import ApiError


async def list_logs(user_id: str) -> list[dict]:
    rows = [row async for row in collection("sync_logs").find({"userId": user_id}).sort("createdAt", -1)]
    return serialize(rows)


async def get_log(user_id: str, log_id: str) -> dict:
    row = await collection("sync_logs").find_one({"_id": oid(log_id), "userId": user_id})
    if not row:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Sync log not found")
    return serialize(row)
