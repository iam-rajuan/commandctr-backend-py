from typing import Any

from starlette import status

from app.common import clean, with_timestamps
from app.db import collection, oid, serialize
from app.errors import ApiError
from app.modules.inventory.schema import InventoryUpdateBody, SafetyBufferBody


def inventory_availability(row: dict[str, Any]) -> dict[str, Any]:
    shopify_qty = max(int(row.get("shopifyQuantity", row.get("availableQuantity", 0)) or 0), 0)
    safety = max(int(row.get("safetyBuffer", 0) or 0), 0)
    threshold = max(int(row.get("lowStockThreshold", 5) or 0), 0)
    return row | {
        "shopifyQuantity": shopify_qty,
        "safetyBuffer": safety,
        "lowStockThreshold": threshold,
        "availableQuantity": max(shopify_qty - safety, 0),
    }


async def get_inventory(user_id: str, include_inactive: bool = False) -> list[dict]:
    query: dict[str, Any] = {"userId": user_id}
    if not include_inactive:
        query["isActive"] = {"$ne": False}
    rows = [inventory_availability(r) async for r in collection("inventories").find(query).sort("updatedAt", -1)]
    return serialize(rows)


async def get_inventory_by_id(user_id: str, inventory_id: str) -> dict:
    row = await collection("inventories").find_one({"_id": oid(inventory_id), "userId": user_id})
    if not row:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Inventory item not found")
    return serialize(inventory_availability(row))


async def update_inventory(user_id: str, inventory_id: str, body: InventoryUpdateBody) -> dict:
    row = await collection("inventories").find_one({"_id": oid(inventory_id), "userId": user_id})
    if not row:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Inventory item not found")
    payload = clean(body)
    if "availableQuantity" in payload:
        payload["shopifyQuantity"] = payload["availableQuantity"] + row.get("safetyBuffer", 0)
    updated = await collection("inventories").find_one_and_update(
        {"_id": row["_id"]}, {"$set": with_timestamps(payload)}, return_document=True
    )
    return serialize(inventory_availability(updated))


async def update_safety_buffer(user_id: str, inventory_id: str, body: SafetyBufferBody) -> dict:
    row = await collection("inventories").find_one({"_id": oid(inventory_id), "userId": user_id})
    if not row:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Inventory item not found")
    shopify_qty = row.get("shopifyQuantity", row.get("availableQuantity", 0))
    updated = await collection("inventories").find_one_and_update(
        {"_id": row["_id"]},
        {"$set": with_timestamps({"safetyBuffer": body.safetyBuffer, "availableQuantity": max(shopify_qty - body.safetyBuffer, 0)})},
        return_document=True,
    )
    return serialize(inventory_availability(updated))
