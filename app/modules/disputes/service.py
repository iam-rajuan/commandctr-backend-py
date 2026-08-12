from collections import Counter
from typing import Any

from starlette import status

from app.common import regex_or
from app.db import collection, oid, serialize
from app.errors import ApiError


async def list_disputes(
    user_id: str,
    page: str | None = None,
    limit: str | None = None,
    search: str | None = None,
    status_filter: str | None = None,
) -> dict:
    query: dict[str, Any] = {"userId": user_id, "marketplace": "shopify"}
    if status_filter:
        query["status"] = status_filter
    if search:
        query["$or"] = regex_or(
            ["disputeId", "orderName", "shopifyOrderId", "customerName", "customerEmail", "reason", "status"],
            search,
        )
    rows = [row async for row in collection("disputes").find(query).sort([("sourceCreatedAt", -1), ("updatedAt", -1)])]
    all_rows = [row async for row in collection("disputes").find({"userId": user_id, "marketplace": "shopify"})]
    counts = Counter(str(row.get("status", "UNKNOWN")).upper() for row in all_rows)
    page_num = max(int(page or 1), 1)
    limit_num = min(max(int(limit or 25), 1), 100)
    items = [
        row | {"id": str(row["_id"]), "status": str(row.get("status", "UNKNOWN")).upper()}
        for row in rows[(page_num - 1) * limit_num : page_num * limit_num]
    ]
    return {
        "items": serialize(items),
        "meta": {
            "page": page_num,
            "limit": limit_num,
            "total": len(rows),
            "totalPages": max(1, (len(rows) + limit_num - 1) // limit_num),
        },
        "stats": {
            "actionRequired": counts["OPEN"],
            "activeDisputes": counts["OPEN"] + counts["SUBMITTED"],
            "won": counts["WON"],
            "lost": counts["LOST"],
            "submitted": counts["SUBMITTED"],
            "avgResolutionTimeDays": None,
        },
    }


async def get_dispute(user_id: str, dispute_id: str) -> dict:
    row = await collection("disputes").find_one({"_id": oid(dispute_id), "userId": user_id, "marketplace": "shopify"})
    if not row:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Dispute not found")
    related = None
    if row.get("localOrderId"):
        order = await collection("orders").find_one({"_id": row["localOrderId"], "userId": user_id})
        if order:
            related = {
                "id": str(order["_id"]),
                "orderName": order.get("orderName"),
                "shopifyOrderId": order.get("shopifyOrderId"),
                "customerName": order.get("customerName"),
                "customerEmail": order.get("email"),
                "financialStatus": order.get("financialStatus"),
                "fulfillmentStatus": order.get("fulfillmentStatus"),
                "totalPrice": order.get("totalPrice"),
                "currency": order.get("currency"),
                "processedAt": order.get("processedAt"),
            }
    return {
        "dispute": serialize(row | {"id": str(row["_id"]), "status": str(row.get("status", "UNKNOWN")).upper()}),
        "relatedOrder": serialize(related),
    }
