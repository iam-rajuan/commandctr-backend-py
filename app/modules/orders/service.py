from typing import Any

from starlette import status

from app.common import clean, regex_or, with_timestamps
from app.db import collection, now_utc, oid, serialize
from app.errors import ApiError
from app.modules.orders.schema import NoteBody, OrderStatusBody
from app.seed import seed_sandbox_orders


def order_status(row: dict[str, Any]) -> str:
    if row.get("cancelledAt") or row.get("statusOverride") == "Cancelled":
        return "Cancelled"
    if row.get("statusOverride") in {"Delivered", "Shipped"}:
        return row["statusOverride"]
    if str(row.get("fulfillmentStatus") or "").upper() in {"FULFILLED", "SHIPPED"}:
        return "Shipped"
    return "Pending"


async def hydrate_order_images(user_id: str, order: dict[str, Any]) -> dict[str, Any]:
    ids = list({item.get("shopifyProductId") for item in order.get("lineItems", []) if item.get("shopifyProductId")})
    if not ids:
        return order
    products = {
        p.get("shopifyProductId"): p
        async for p in collection("products").find(
            {"userId": user_id, "shopifyProductId": {"$in": ids}},
            {"shopifyProductId": 1, "featuredImage": 1},
        )
    }
    order["lineItems"] = [
        item | {"imageUrl": item.get("imageUrl") or products.get(item.get("shopifyProductId"), {}).get("featuredImage", "")}
        for item in order.get("lineItems", [])
    ]
    return order


async def get_order_stats(user_id: str) -> dict[str, int]:
    stats = {"totalOrders": 0, "pending": 0, "shipped": 0, "delivered": 0, "cancelled": 0}
    async for row in collection("orders").find({"userId": user_id}):
        stats["totalOrders"] += 1
        stats[order_status(row).lower()] += 1
    return stats


async def get_orders(
    user_id: str,
    page: str | None = None,
    limit: str | None = None,
    search: str | None = None,
    marketplace: str | None = None,
    status_filter: str | None = None,
) -> dict:
    try:
        from app.modules.marketplace_core.service import shopify_import_orders_for_user

        await shopify_import_orders_for_user(user_id)
    except ApiError as exc:
        if exc.message != "Shopify store is not connected.":
            raise
    query: dict[str, Any] = {"userId": user_id}
    if marketplace and marketplace.lower() != "all":
        query["marketplace"] = marketplace.lower()
    if search:
        query["$or"] = regex_or(
            ["orderName", "externalOrderId", "shopifyOrderId", "customerName", "email", "lineItems.title", "lineItems.sku"],
            search,
        )
    rows = [row async for row in collection("orders").find(query).sort([("processedAt", -1), ("createdAt", -1)])]
    rows = [row | {"status": order_status(row)} for row in rows]
    if status_filter:
        rows = [row for row in rows if row["status"] == status_filter]
    page_num = max(int(page or 1), 1)
    limit_num = min(max(int(limit or 25), 1), 100)
    items = [await hydrate_order_images(user_id, row) for row in rows[(page_num - 1) * limit_num : page_num * limit_num]]
    return {
        "items": serialize(items),
        "meta": {
            "page": page_num,
            "limit": limit_num,
            "total": len(rows),
            "totalPages": max(1, (len(rows) + limit_num - 1) // limit_num),
        },
        "stats": await get_order_stats(user_id),
    }


async def seed_orders(user_id: str) -> dict:
    await seed_sandbox_orders(user_id)
    return await get_orders(user_id, page="1", limit="100")


async def get_order_by_id(user_id: str, order_id: str) -> dict:
    row = await collection("orders").find_one({"_id": oid(order_id), "userId": user_id})
    if not row:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Order not found")
    return serialize(await hydrate_order_images(user_id, row | {"status": order_status(row)}))


async def update_order_status(user_id: str, order_id: str, body: OrderStatusBody) -> dict:
    row = await collection("orders").find_one({"_id": oid(order_id), "userId": user_id})
    if not row:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Order not found")
    updates = clean(body) | {"statusOverride": body.status, "lastSyncedAt": now_utc()}
    updates.pop("status", None)
    if body.status == "Shipped":
        updates["fulfillmentStatus"] = "FULFILLED"
        updates["shippedAt"] = now_utc()
    if body.status == "Cancelled":
        updates["cancelledAt"] = now_utc()
    if body.status == "Cancelled" and row.get("marketplace") == "shopify" and row.get("shopifyOrderId"):
        from app.modules.marketplace_core.service import shopify_cancel_order

        await shopify_cancel_order(
            user_id,
            row["shopifyOrderId"],
            {
                "notifyCustomer": body.notifyCustomer if body.notifyCustomer is not None else False,
                "reason": body.cancelReason or "OTHER",
                "restock": body.restock if body.restock is not None else True,
                "refund": body.refund if body.refund is not None else True,
                "staffNote": body.staffNote,
            },
        )
        updates["fulfillmentStatus"] = "CANCELLED"
    if body.status == "Shipped" and row.get("marketplace") == "shopify" and row.get("shopifyOrderId") and body.trackingNumber:
        from app.modules.marketplace_core.service import shopify_mark_order_as_shipped

        await shopify_mark_order_as_shipped(
            user_id,
            row["shopifyOrderId"],
            {
                "carrier": body.carrier,
                "trackingNumber": body.trackingNumber,
                "trackingUrl": body.trackingUrl,
                "notifyCustomer": body.notifyCustomer if body.notifyCustomer is not None else True,
            },
        )
    updated = await collection("orders").find_one_and_update(
        {"_id": row["_id"]}, {"$set": with_timestamps(updates)}, return_document=True
    )
    return serialize(await hydrate_order_images(user_id, updated | {"status": order_status(updated)}))


async def add_note(user_id: str, order_id: str, body: NoteBody) -> dict:
    updated = await collection("orders").find_one_and_update(
        {"_id": oid(order_id), "userId": user_id},
        {"$push": {"internalNotes": {"message": body.message.strip(), "createdAt": now_utc()}}, "$set": {"updatedAt": now_utc()}},
        return_document=True,
    )
    if not updated:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Order not found")
    return serialize(await hydrate_order_images(user_id, updated | {"status": order_status(updated)}))


async def export_orders_csv(user_id: str) -> str:
    result = await get_orders(user_id, page="1", limit="100")
    rows = [
        [
            "Order ID",
            "Marketplace",
            "Customer",
            "Email",
            "Items",
            "Amount",
            "Currency",
            "Financial Status",
            "Fulfillment Status",
            "Status",
            "Carrier",
            "Tracking Number",
            "Date",
        ]
    ]
    for order in result["items"]:
        rows.append(
            [
                order.get("orderName") or order.get("externalOrderId") or order.get("shopifyOrderId") or "",
                order.get("marketplace", ""),
                order.get("customerName", ""),
                order.get("email", ""),
                sum(item.get("quantity", 0) for item in order.get("lineItems", [])),
                order.get("totalPrice", ""),
                order.get("currency", ""),
                order.get("financialStatus", ""),
                order.get("fulfillmentStatus", ""),
                order.get("status", ""),
                order.get("carrier", ""),
                order.get("trackingNumber", ""),
                order.get("processedAt", ""),
            ]
        )
    return "\n".join(",".join(f'"{str(value).replace(chr(34), chr(34) + chr(34))}"' for value in row) for row in rows)
