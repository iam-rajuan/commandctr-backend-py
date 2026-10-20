from __future__ import annotations

from types import SimpleNamespace

import pytest
from pymongo.errors import DuplicateKeyError

from app import db, main
from app.modules.marketplace_core import service


class FakeLeaseCollection:
    def __init__(self, existing_owner: str | None = None, expired: bool = False, duplicate_on_upsert: bool = False):
        self.existing_owner = existing_owner
        self.expired = expired
        self.duplicate_on_upsert = duplicate_on_upsert

    async def find_one_and_update(self, _query, update, **_kwargs):
        if self.duplicate_on_upsert:
            raise DuplicateKeyError("duplicate lease")
        if self.existing_owner and not self.expired and self.existing_owner != update["$set"]["ownerId"]:
            return None
        self.existing_owner = update["$set"]["ownerId"]
        return {"_id": "ebay-sync-worker", "ownerId": self.existing_owner}


class FakeIndexCursor:
    def __init__(self, indexes):
        self.indexes = indexes

    async def to_list(self, *, length):
        assert length is None
        return self.indexes


class FakeIndexCollection:
    def __init__(self, indexes):
        self.indexes = indexes
        self.dropped = []

    def list_indexes(self):
        return FakeIndexCursor(self.indexes)

    async def drop_index(self, name):
        self.dropped.append(name)


def test_marketplace_connection_unique_indexes_match_node_reference():
    specs = db.marketplace_connection_unique_index_specs()

    assert specs[0]["keys"] == [("userId", 1), ("marketplace", 1), ("shopDomain", 1)]
    assert specs[0]["kwargs"]["unique"] is True
    assert specs[0]["kwargs"]["partialFilterExpression"] == {
        "marketplace": "shopify",
        "shopDomain": {"$exists": True, "$type": "string"},
    }

    assert specs[1]["keys"] == [("userId", 1), ("marketplace", 1), ("environment", 1)]
    assert specs[1]["kwargs"]["unique"] is True
    assert specs[1]["kwargs"]["partialFilterExpression"] == {
        "marketplace": {"$in": ["amazon", "ebay", "etsy"]},
        "environment": {"$exists": True, "$type": "string"},
    }


@pytest.mark.asyncio
async def test_drops_only_known_unsafe_legacy_marketplace_index(monkeypatch):
    legacy_name = "userId_1_marketplace_1_shopDomain_1"
    fake = FakeIndexCollection(
        [
            {
                "name": legacy_name,
                "key": {"userId": 1, "marketplace": 1, "shopDomain": 1},
                "unique": True,
            },
            {
                "name": "uniq_shopify_shop_domain_connection",
                "key": {"userId": 1, "marketplace": 1, "shopDomain": 1},
                "unique": True,
                "partialFilterExpression": {"marketplace": "shopify"},
            },
        ]
    )
    monkeypatch.setattr(db, "collection", lambda _name: fake)

    dropped = await db.drop_unsafe_legacy_marketplace_connection_indexes()

    assert dropped == [legacy_name]
    assert fake.dropped == [legacy_name]


def test_cors_uses_explicit_origins_when_configured(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(CORS_ALLOWED_ORIGINS="https://app.example.com, http://localhost:3000", FRONTEND_URL=None, NODE_ENV="production"),
    )

    assert main.cors_allowed_origins() == ["https://app.example.com", "http://localhost:3000"]


def test_cors_does_not_default_to_wildcard_in_production(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(CORS_ALLOWED_ORIGINS=None, FRONTEND_URL=None, NODE_ENV="production"),
    )

    assert main.cors_allowed_origins() == []


def test_start_worker_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(EBAY_WORKER_ENABLED=False, EBAY_WORKER_OWNER_ID=None),
    )
    service.stop_worker()

    service.start_worker()

    assert service.worker_task is None
    assert service.worker_owner_id is None


@pytest.mark.asyncio
async def test_worker_lease_allows_single_active_owner(monkeypatch):
    fake = FakeLeaseCollection(existing_owner="node-worker", expired=False)
    monkeypatch.setattr(service, "collection", lambda name: fake)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(EBAY_WORKER_LEASE_SECONDS=30))

    assert await service.acquire_worker_lease("fastapi-worker") is False


@pytest.mark.asyncio
async def test_worker_lease_can_take_expired_owner(monkeypatch):
    fake = FakeLeaseCollection(existing_owner="node-worker", expired=True)
    monkeypatch.setattr(service, "collection", lambda name: fake)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(EBAY_WORKER_LEASE_SECONDS=30))

    assert await service.acquire_worker_lease("fastapi-worker") is True


@pytest.mark.asyncio
async def test_worker_lease_duplicate_race_is_not_authorized(monkeypatch):
    fake = FakeLeaseCollection(duplicate_on_upsert=True)
    monkeypatch.setattr(service, "collection", lambda name: fake)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(EBAY_WORKER_LEASE_SECONDS=30))

    assert await service.acquire_worker_lease("fastapi-worker") is False


def test_runtime_worker_owner_id_is_instance_specific_when_not_configured(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(EBAY_WORKER_OWNER_ID=None),
    )

    first = service.runtime_worker_owner_id()
    second = service.runtime_worker_owner_id()

    assert first.startswith("fastapi:")
    assert second.startswith("fastapi:")
    assert first != second


def test_worker_retry_treats_unexpected_exception_as_retryable():
    assert service.ebay_retryable(RuntimeError("temporary")) is True
    assert service.ebay_retryable(Exception("unexpected")) is True
