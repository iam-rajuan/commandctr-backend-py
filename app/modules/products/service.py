from typing import Any

from starlette import status

from app.common import clean, with_timestamps
from app.db import collection, oid, serialize
from app.errors import ApiError
from app.modules.products.schema import ProductCreateBody, ProductUpdateBody


def normalize_product_payload(payload: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    data = {k: (v.strip() if isinstance(v, str) else v) for k, v in payload.items() if v is not None}
    if not partial:
        data.setdefault("marketplace", "shopify")
        data.setdefault("tags", [])
        data.setdefault("images", [])
    if "variants" in data:
        variants = []
        for idx, variant in enumerate(data.get("variants") or []):
            title = (variant.get("title") or ("Default Title" if idx == 0 else f"Variant {idx + 1}")).strip()
            variants.append({**variant, "title": title, "inventoryQuantity": max(int(variant.get("inventoryQuantity") or 0), 0)})
        data["variants"] = variants
        if "totalInventory" not in data:
            data["totalInventory"] = sum(v.get("inventoryQuantity") or 0 for v in variants)
    if "images" in data or "featuredImage" in data:
        images = []
        if data.get("featuredImage"):
            images.append(data["featuredImage"])
        images.extend(data.get("images") or [])
        data["images"] = list(dict.fromkeys([image for image in images if image]))
        data["featuredImage"] = data.get("featuredImage") or (data["images"][0] if data["images"] else None)
    return data


async def create_product(user_id: str, body: ProductCreateBody) -> dict:
    payload = with_timestamps(normalize_product_payload(body.model_dump()) | {"userId": user_id}, insert=True)
    result = await collection("products").insert_one(payload)
    return serialize(await collection("products").find_one({"_id": result.inserted_id}))


async def get_products(user_id: str) -> list[dict]:
    return serialize([p async for p in collection("products").find({"userId": user_id}).sort("updatedAt", -1)])


async def get_product_by_id(user_id: str, product_id: str) -> dict:
    product = await collection("products").find_one({"_id": oid(product_id), "userId": user_id})
    if not product:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Product not found")
    return serialize(product)


async def update_product(user_id: str, product_id: str, body: ProductUpdateBody) -> dict:
    product = await collection("products").find_one_and_update(
        {"_id": oid(product_id), "userId": user_id},
        {"$set": with_timestamps(normalize_product_payload(clean(body), partial=True))},
        return_document=True,
    )
    if not product:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Product not found")
    return serialize(product)


async def delete_product(user_id: str, product_id: str) -> None:
    result = await collection("products").delete_one({"_id": oid(product_id), "userId": user_id})
    if result.deleted_count == 0:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Product not found")
