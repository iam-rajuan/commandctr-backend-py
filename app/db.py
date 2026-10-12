from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure

from app.config import get_settings

client: AsyncIOMotorClient[dict[str, Any]] | None = None
database: AsyncIOMotorDatabase[dict[str, Any]] | None = None


def now_utc() -> datetime:
    return datetime.now(UTC)


def oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise ValueError("Invalid ObjectId") from exc


def serialize(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    return value


def collection(name: str) -> AsyncIOMotorCollection[dict[str, Any]]:
    if database is None:
        raise RuntimeError("MongoDB is not connected")
    return database[name]


def marketplace_connection_unique_index_specs() -> list[dict[str, Any]]:
    return [
        {
            "keys": [("userId", ASCENDING), ("marketplace", ASCENDING), ("shopDomain", ASCENDING)],
            "kwargs": {
                "name": "uniq_shopify_shop_domain_connection",
                "unique": True,
                "partialFilterExpression": {
                    "marketplace": "shopify",
                    "shopDomain": {"$exists": True, "$type": "string"},
                },
            },
        },
        {
            "keys": [("userId", ASCENDING), ("marketplace", ASCENDING), ("environment", ASCENDING)],
            "kwargs": {
                "name": "uniq_marketplace_environment_connection",
                "unique": True,
                "partialFilterExpression": {
                    "marketplace": {"$in": ["ebay", "etsy"]},
                    "environment": {"$exists": True, "$type": "string"},
                },
            },
        },
    ]


async def find_marketplace_connection_duplicates() -> list[dict[str, Any]]:
    pipeline = [
        {
            "$match": {
                "$or": [
                    {"marketplace": "shopify", "shopDomain": {"$exists": True, "$type": "string"}},
                    {
                        "marketplace": {"$in": ["ebay", "etsy"]},
                        "environment": {"$exists": True, "$type": "string"},
                    },
                ]
            }
        },
        {
            "$project": {
                "identity": {
                    "$cond": [
                        {"$eq": ["$marketplace", "shopify"]},
                        {
                            "userId": "$userId",
                            "marketplace": "$marketplace",
                            "shopDomain": "$shopDomain",
                        },
                        {
                            "userId": "$userId",
                            "marketplace": "$marketplace",
                            "environment": "$environment",
                        },
                    ]
                }
            }
        },
        {"$group": {"_id": "$identity", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"_id.userId": 1, "_id.marketplace": 1}},
    ]
    return [row async for row in collection("marketplaceconnections").aggregate(pipeline)]


async def assert_marketplace_connection_indexes_safe() -> None:
    duplicates = await find_marketplace_connection_duplicates()
    if not duplicates:
        return
    identities = [{"identity": row["_id"], "count": row["count"]} for row in duplicates]
    raise RuntimeError(
        "Cannot create marketplace connection unique indexes because duplicate logical connections exist. "
        f"Resolve these sanitized identities first: {identities}"
    )


async def create_indexes() -> None:
    async def safe_create_index(coll: str, keys: list[tuple[str, int]], **kwargs: Any) -> None:
        try:
            await collection(coll).create_index(keys, **kwargs)
        except OperationFailure as exc:
            if exc.code not in {85, 86}:
                raise

    await safe_create_index("users", [("email", ASCENDING)], unique=True)
    await safe_create_index("products", [("userId", ASCENDING)])
    await safe_create_index(
        "products",
        [("userId", ASCENDING), ("shopifyProductId", ASCENDING)],
        unique=True,
        partialFilterExpression={"shopifyProductId": {"$type": "string"}},
        name="userId_1_shopifyProductId_1",
    )
    await safe_create_index(
        "products",
        [("userId", ASCENDING), ("ebaySku", ASCENDING)],
        unique=True,
        partialFilterExpression={"ebaySku": {"$exists": True, "$type": "string"}},
        name="userId_1_ebaySku_1",
    )
    await safe_create_index(
        "inventories",
        [("userId", ASCENDING), ("inventoryItemId", ASCENDING), ("locationId", ASCENDING)],
        unique=True,
    )
    await safe_create_index(
        "orders",
        [("userId", ASCENDING), ("shopifyOrderId", ASCENDING)],
        unique=True,
        partialFilterExpression={"shopifyOrderId": {"$type": "string"}},
        name="userId_1_shopifyOrderId_1",
    )
    await safe_create_index(
        "orders",
        [("userId", ASCENDING), ("externalOrderId", ASCENDING)],
        unique=True,
        partialFilterExpression={"externalOrderId": {"$type": "string"}},
        name="userId_1_externalOrderId_1",
    )
    await safe_create_index("orders", [("userId", ASCENDING), ("processedAt", DESCENDING)])
    await safe_create_index(
        "disputes",
        [("userId", ASCENDING), ("marketplace", ASCENDING), ("disputeId", ASCENDING)],
        unique=True,
    )
    await safe_create_index("marketplaceconnections", [("userId", ASCENDING)])
    await assert_marketplace_connection_indexes_safe()
    for spec in marketplace_connection_unique_index_specs():
        await safe_create_index("marketplaceconnections", spec["keys"], **spec["kwargs"])
    await safe_create_index("sync_logs", [("userId", ASCENDING), ("createdAt", DESCENDING)])
    await safe_create_index(
        "ebaysyncjobs",
        [("status", ASCENDING), ("availableAt", ASCENDING), ("createdAt", ASCENDING)]
    )
    await safe_create_index("worker_leases", [("expiresAt", ASCENDING)], expireAfterSeconds=0)
    await safe_create_index("etsyoauthstates", [("state", ASCENDING)], unique=True)
    await safe_create_index("etsyoauthstates", [("expiresAt", ASCENDING)], expireAfterSeconds=0)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global client, database
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    database = client.get_default_database()
    await create_indexes()
    from app.modules.marketplace_core.service import start_worker, stop_worker
    from app.seed import ensure_default_admin

    await ensure_default_admin()
    if settings.EBAY_WORKER_ENABLED:
        start_worker()
    try:
        yield
    finally:
        stop_worker()
        client.close()
