from types import SimpleNamespace

import pytest

from app.errors import ApiError
from app.modules.amazon import service
from app.modules.amazon.schema import AmazonListingBody


def production_settings(*, seller_id: str | None = "SELLER123") -> SimpleNamespace:
    return SimpleNamespace(
        AMAZON_SP_API_ENVIRONMENT="production",
        AMAZON_SP_API_ENDPOINT="https://sellingpartnerapi-eu.amazon.com",
        AMAZON_MARKETPLACE_ID="A1F83G8C2ARO7P",
        AMAZON_CURRENCY="GBP",
        AMAZON_SELLER_ID=seller_id,
        AMAZON_LWA_CLIENT_ID="client-id",
        AMAZON_LWA_CLIENT_SECRET="client-secret",
        AMAZON_LWA_REFRESH_TOKEN="refresh-token",
    )


def listing_body() -> AmazonListingBody:
    return AmazonListingBody(
        title="Example product",
        sku="SKU-123",
        price="19.99",
        quantity=4,
        productType="PRODUCT",
        backendSearchTerms=["example", "product"],
        attributes={"supplier_declared_dg_hz_regulation": "not_applicable"},
    )


def test_production_config_requires_and_uses_real_seller_id(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())

    config = service.amazon_config()

    assert config["environment"] == "production"
    assert config["sellerId"] == "SELLER123"
    assert config["endpoint"] == "https://sellingpartnerapi-eu.amazon.com"


def test_production_config_rejects_missing_seller_id(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings(seller_id=None))

    with pytest.raises(ApiError):
        service.amazon_config()


def test_listing_submission_contains_marketplace_offer_and_inventory(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())

    payload = service.listing_submission(listing_body(), service.amazon_config())

    assert payload["productType"] == "PRODUCT"
    assert payload["requirements"] == "LISTING"
    assert payload["attributes"]["item_name"][0]["marketplace_id"] == "A1F83G8C2ARO7P"
    assert payload["attributes"]["item_name"][0]["language_tag"] == "en_GB"
    assert payload["attributes"]["purchasable_offer"][0]["currency"] == "GBP"
    assert payload["attributes"]["list_price"][0]["currency"] == "GBP"
    assert payload["attributes"]["list_price"][0]["value_with_tax"] == 19.99
    assert payload["attributes"]["fulfillment_availability"][0]["quantity"] == 4
    assert payload["attributes"]["supplier_declared_dg_hz_regulation"][0]["value"] == "not_applicable"
    assert payload["attributes"]["generic_keyword"][0]["value"] == "example product"
    assert "merchant_suggested_asin" not in payload["attributes"]


def test_generic_ai_category_is_not_sent_as_an_amazon_attribute(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())
    body = listing_body().model_copy(update={"attributes": {"category": "Kitchen & Dining"}})

    payload = service.listing_submission(body, service.amazon_config())

    assert "category" not in payload["attributes"]


def test_json_attribute_value_supports_amazon_unit_objects(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())
    body = listing_body().model_copy(
        update={"attributes": {"capacity": '[{"value": 750, "unit": "milliliters"}]'}}
    )

    payload = service.listing_submission(body, service.amazon_config())

    assert payload["attributes"]["capacity"] == [{"value": 750, "unit": "milliliters"}]


def test_barcode_free_generic_payload_omits_identifier(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())
    body = listing_body().model_copy(update={
        "brand": "Generic", "identifierMode": "no_identifier", "exemptionConfirmed": True,
        "exemptionBrand": "Generic", "exemptionCategory": "BOTTLE",
        "attributes": {"supplier_declared_dg_hz_regulation": "not_applicable"},
    })
    payload = service.listing_submission(body, service.amazon_config())
    assert "externally_assigned_product_identifier" not in payload["attributes"]


def test_barcode_free_requires_confirmed_exemption():
    body = listing_body().model_copy(update={"identifierMode": "no_identifier", "attributes": {}})
    with pytest.raises(ApiError):
        service.validate_identifier_path(body)


@pytest.mark.asyncio
async def test_canonical_product_type_does_not_call_amazon(monkeypatch):
    body = listing_body().model_copy(update={"productType": "BOTTLE"})

    async def unexpected_token(*_args, **_kwargs):
        raise AssertionError("Canonical product types should not require resolution")

    monkeypatch.setattr(service, "connected_access_token", unexpected_token)

    resolved = await service.resolve_product_type("user-1", body)

    assert resolved.productType == "BOTTLE"


@pytest.mark.asyncio
async def test_display_product_type_is_resolved_from_amazon(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())

    async def access_token(*_args, **_kwargs):
        return "access-token"

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"productTypes": [{"name": "BOTTLE", "displayName": "Bottle"}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(service, "connected_access_token", access_token)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **_kwargs: FakeClient())

    resolved = await service.resolve_product_type(
        "user-1", listing_body().model_copy(update={"productType": "Water Bottle"})
    )

    assert resolved.productType == "BOTTLE"


@pytest.mark.asyncio
async def test_production_validation_preview_rejects_errors(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())

    async def invalid_preview(*_args, **kwargs):
        assert kwargs["validation_preview"] is True
        return {"status": "INVALID", "issues": [{"severity": "ERROR", "message": "Missing attribute"}]}

    monkeypatch.setattr(service, "listings_request", invalid_preview)

    with pytest.raises(ApiError):
        await service.validate_listing("user-1", listing_body())


def test_offer_only_listing_submission_uses_existing_asin_and_offer_requirements(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())
    body = AmazonListingBody(
        title="NOTEBOOK BASIC: NOTEBOOK, NOTEBOOK 8.5 X 11",
        sku="SKU-NOTEBOOK-001",
        price="8.99",
        quantity=15,
        productType="ABIS_BOOK",
        identifierMode="asin",
        existingAsin="B09TDW5M2C",
        condition="new_new",
        fulfillmentChannel="DEFAULT",
    )

    payload = service.listing_submission(body, service.amazon_config())

    assert payload["productType"] == "ABIS_BOOK"
    assert payload["requirements"] == "LISTING_OFFER_ONLY"
    assert payload["attributes"]["merchant_suggested_asin"][0]["value"] == "B09TDW5M2C"
    assert payload["attributes"]["merchant_suggested_asin"][0]["marketplace_id"] == "A1F83G8C2ARO7P"
    assert payload["attributes"]["condition_type"][0]["value"] == "new_new"
    assert payload["attributes"]["purchasable_offer"][0]["currency"] == "GBP"
    assert payload["attributes"]["purchasable_offer"][0]["our_price"][0]["schedule"][0]["value_with_tax"] == 8.99
    assert payload["attributes"]["fulfillment_availability"][0]["quantity"] == 15
    assert payload["attributes"]["fulfillment_availability"][0]["fulfillment_channel_code"] == "DEFAULT"
    # Ensure catalogue fields are NOT included in offer-only attributes
    assert "item_name" not in payload["attributes"]
    assert "product_description" not in payload["attributes"]
    assert "brand" not in payload["attributes"]
    assert "bullet_point" not in payload["attributes"]
    assert "list_price" not in payload["attributes"]
    assert "externally_assigned_product_identifier" not in payload["attributes"]


def test_offer_only_validation_requires_valid_10_char_asin():
    body_short = AmazonListingBody.model_construct(
        title="Test", sku="SKU-1", price="10.00", quantity=1,
        identifierMode="asin", existingAsin="B09TDW"
    )
    with pytest.raises(ApiError) as exc_short:
        service.validate_identifier_path(body_short)
    assert exc_short.value.status_code == 422

    body_empty = AmazonListingBody.model_construct(
        title="Test", sku="SKU-1", price="10.00", quantity=1,
        identifierMode="asin", existingAsin=None
    )
    with pytest.raises(ApiError) as exc_empty:
        service.validate_identifier_path(body_empty)
    assert exc_empty.value.status_code == 422


@pytest.mark.asyncio
async def test_catalog_item_lookup_parses_summaries_and_images(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())

    async def fake_token(*_args, **_kwargs):
        return "fake-token"

    class FakeCatalogResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "summaries": [
                    {
                        "itemName": "NOTEBOOK BASIC: NOTEBOOK, NOTEBOOK 8.5 X 11",
                        "brand": "Notebooks Co",
                        "manufacturer": "Independently published",
                        "itemClassification": "BASE_PRODUCT",
                    }
                ],
                "productTypes": [{"productType": "ABIS_BOOK"}],
                "images": [
                    {
                        "images": [
                            {"variant": "MAIN", "link": "https://m.media-amazon.com/images/I/example.jpg", "height": 500, "width": 500}
                        ]
                    }
                ],
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *_args, **_kwargs):
            assert "catalog/2022-04-01/items/B09TDW5M2C" in url
            return FakeCatalogResponse()

    monkeypatch.setattr(service, "connected_access_token", fake_token)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **_kwargs: FakeClient())

    result = await service.catalog_item_lookup("user-1", "B09TDW5M2C")

    assert result["asin"] == "B09TDW5M2C"
    assert result["title"] == "NOTEBOOK BASIC: NOTEBOOK, NOTEBOOK 8.5 X 11"
    assert result["productType"] == "ABIS_BOOK"
    assert result["imageUrl"] == "https://m.media-amazon.com/images/I/example.jpg"
    assert result["marketplaceId"] == "A1F83G8C2ARO7P"


@pytest.mark.asyncio
async def test_check_restrictions_parses_eligibility(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())

    async def fake_token(*_args, **_kwargs):
        return "fake-token"

    class FakeRestrictionsResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"restrictions": []}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *_args, **_kwargs):
            assert "listings/2021-08-01/restrictions" in url
            return FakeRestrictionsResponse()

    monkeypatch.setattr(service, "connected_access_token", fake_token)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **_kwargs: FakeClient())

    result = await service.check_restrictions("user-1", "B09TDW5M2C", "new_new")

    assert result["asin"] == "B09TDW5M2C"
    assert result["restricted"] is False
    assert result["eligible"] is True
    assert result["restrictions"] == []


@pytest.mark.asyncio
async def test_validate_listing_preview_returns_structured_result(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())
    body = AmazonListingBody(
        title="NOTEBOOK BASIC",
        sku="SKU-NOTEBOOK-001",
        price="8.99",
        quantity=5,
        productType="ABIS_BOOK",
        identifierMode="asin",
        existingAsin="B09TDW5M2C",
    )

    async def fake_request(*_args, **kwargs):
        assert kwargs.get("validation_preview") is True
        return {"status": "ACCEPTED", "issues": []}

    monkeypatch.setattr(service, "listings_request", fake_request)

    res = await service.validate_listing_preview("user-1", body)

    assert res["valid"] is True
    assert res["status"] == "ACCEPTED"
    assert res["issues"] == []
    assert res["sku"] == "SKU-NOTEBOOK-001"
    assert res["productType"] == "ABIS_BOOK"


@pytest.mark.asyncio
async def test_validate_listing_allows_warnings_and_does_not_raise_422(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())
    body = AmazonListingBody(
        title="NOTEBOOK BASIC",
        sku="SKU-NOTEBOOK-001",
        price="8.99",
        quantity=5,
        productType="ABIS_BOOK",
        identifierMode="asin",
        existingAsin="B09TDW5M2C",
    )

    warning_issue = {
        "code": "90000900",
        "severity": "WARNING",
        "message": "Attribute [list_price] is not applicable for this listing mode and will be ignored.",
        "attributeNames": ["list_price"],
    }

    async def fake_request(*_args, **kwargs):
        assert kwargs.get("validation_preview") is True
        return {"status": "ACCEPTED", "issues": [warning_issue]}

    monkeypatch.setattr(service, "listings_request", fake_request)

    # 1. validate_listing must NOT raise HTTP 422 for a WARNING!
    preview = await service.validate_listing("user-1", body)
    assert preview["status"] == "ACCEPTED"
    assert len(preview["issues"]) == 1

    # 2. validate_listing_preview returns valid=True and categorizes warningIssues
    preview_res = await service.validate_listing_preview("user-1", body)
    assert preview_res["valid"] is True
    assert preview_res["status"] == "ACCEPTED"
    assert len(preview_res["warningIssues"]) == 1
    assert preview_res["warningIssues"][0]["code"] == "90000900"
    assert len(preview_res["errorIssues"]) == 0


@pytest.mark.asyncio
async def test_validate_listing_blocks_on_error_severity_or_invalid_status(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())
    body = AmazonListingBody(
        title="NOTEBOOK BASIC",
        sku="SKU-NOTEBOOK-001",
        price="8.99",
        quantity=5,
        productType="ABIS_BOOK",
        identifierMode="asin",
        existingAsin="B09TDW5M2C",
    )

    error_issue = {
        "code": "4000001",
        "severity": "ERROR",
        "message": "Missing required offer attribute [purchasable_offer].",
        "attributeNames": ["purchasable_offer"],
    }

    async def fake_request(*_args, **kwargs):
        assert kwargs.get("validation_preview") is True
        return {"status": "INVALID", "issues": [error_issue]}

    monkeypatch.setattr(service, "listings_request", fake_request)

    # 1. validate_listing MUST raise HTTP 422 with Amazon issue details
    with pytest.raises(ApiError) as exc:
        await service.validate_listing("user-1", body)
    assert exc.value.status_code == 422
    assert "Code 4000001" in exc.value.message
    assert "purchasable_offer" in exc.value.message
    assert exc.value.details["status"] == "INVALID"
    assert len(exc.value.details["errorIssues"]) == 1

    # 2. validate_listing_preview returns valid=False without raising ApiError
    preview_res = await service.validate_listing_preview("user-1", body)
    assert preview_res["valid"] is False
    assert preview_res["status"] == "INVALID"
    assert len(preview_res["errorIssues"]) == 1
    assert preview_res["errorIssues"][0]["code"] == "4000001"


def test_local_listing_document_stores_amazon_asin_and_condition(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: production_settings())
    body = AmazonListingBody(
        title="NOTEBOOK BASIC",
        sku="SKU-NOTEBOOK-001",
        price="8.99",
        quantity=5,
        productType="ABIS_BOOK",
        identifierMode="asin",
        existingAsin="B09TDW5M2C",
        condition="new_new",
        fulfillmentChannel="DEFAULT",
    )

    doc = service.local_listing_document(body, {"submissionId": "sub-123", "status": "ACCEPTED"}, service.amazon_config())

    assert doc["marketplace"] == "amazon"
    assert doc["amazonSku"] == "SKU-NOTEBOOK-001"
    assert doc["amazonAsin"] == "B09TDW5M2C"
    assert doc["amazonCondition"] == "new_new"
    assert doc["amazonFulfillmentChannel"] == "DEFAULT"
    assert doc["amazonListingUrl"] == "https://www.amazon.co.uk/dp/B09TDW5M2C"
    assert doc["amazonSubmissionId"] == "sub-123"
    assert doc["amazonPayload"]["identifierMode"] == "asin"
    assert doc["amazonPayload"]["existingAsin"] == "B09TDW5M2C"


@pytest.mark.asyncio
async def test_patch_listing_offer_builds_patches_and_updates_db(monkeypatch):
    from bson import ObjectId

    from app.modules.amazon.schema import AmazonOfferPatchBody

    monkeypatch.setattr(service, "get_settings", lambda: production_settings())

    fake_id = ObjectId()
    fake_product = {
        "_id": fake_id,
        "userId": "user-1",
        "marketplace": "amazon",
        "environment": "production",
        "amazonSku": "SKU-NOTEBOOK-001",
        "productType": "ABIS_BOOK",
        "amazonFulfillmentChannel": "DEFAULT",
        "price": "8.99",
        "totalInventory": 5,
        "variants": [{"title": "Default Title", "sku": "SKU-NOTEBOOK-001", "price": "8.99", "inventoryQuantity": 5}],
    }

    recorded_patches = []

    async def fake_listings_request(_user_id, method, sku, body=None, **_kwargs):
        assert sku == "SKU-NOTEBOOK-001"
        if method == "GET":
            return {
                "summaries": [{"asin": "B09TDW5M2C", "itemName": "NOTEBOOK BASIC", "status": ["BUYABLE"]}],
                "offers": [{"price": {"amount": "10.50"}}],
                "fulfillmentAvailability": [{"fulfillmentChannelCode": "DEFAULT", "quantity": 25}],
                "issues": [],
            }
        assert method == "PATCH"
        assert body["productType"] == "ABIS_BOOK"
        recorded_patches.extend(body["patches"])
        return {"status": "ACCEPTED", "issues": []}

    class FakeCollection:
        async def find_one(self, *args, **kwargs):
            return fake_product

        async def find_one_and_update(self, query, update, **kwargs):
            for k, v in update["$set"].items():
                if k == "price":
                    fake_product["price"] = v
                elif k == "totalInventory":
                    fake_product["totalInventory"] = v
                elif k == "variants.0.price":
                    fake_product["variants"][0]["price"] = v
                elif k == "variants.0.inventoryQuantity":
                    fake_product["variants"][0]["inventoryQuantity"] = v
                else:
                    fake_product[k] = v
            return fake_product.copy()

    monkeypatch.setattr(service, "listings_request", fake_listings_request)
    monkeypatch.setattr(service, "collection", lambda name: FakeCollection())

    body = AmazonOfferPatchBody(price="10.50", quantity=25)
    res = await service.patch_listing_offer("user-1", str(fake_id), body)

    assert len(recorded_patches) == 2
    # Check purchasable_offer patch
    offer_patch = next(p for p in recorded_patches if p["path"] == "/attributes/purchasable_offer")
    assert offer_patch["value"][0]["our_price"][0]["schedule"][0]["value_with_tax"] == 10.50
    # Check fulfillment_availability patch
    inv_patch = next(p for p in recorded_patches if p["path"] == "/attributes/fulfillment_availability")
    assert inv_patch["value"][0]["quantity"] == 25
    assert inv_patch["value"][0]["fulfillment_channel_code"] == "DEFAULT"
    assert res["price"] == "10.50"
    assert res["totalInventory"] == 25


@pytest.mark.asyncio
async def test_patch_listing_offer_rejects_fba_quantity_updates(monkeypatch):
    from bson import ObjectId

    from app.modules.amazon.schema import AmazonOfferPatchBody

    monkeypatch.setattr(service, "get_settings", lambda: production_settings())
    product = {"_id": ObjectId(), "userId": "user-1", "marketplace": "amazon", "environment": "production", "amazonSku": "FBA-SKU", "amazonFulfillmentChannel": "AMAZON_UK"}

    class FakeCollection:
        async def find_one(self, *_args, **_kwargs):
            return product

    monkeypatch.setattr(service, "collection", lambda _name: FakeCollection())
    with pytest.raises(ApiError) as exc:
        await service.patch_listing_offer("user-1", str(product["_id"]), AmazonOfferPatchBody(quantity=2))
    assert exc.value.status_code == 422


def test_remote_listing_update_uses_seller_offer_and_fbm_availability():
    product = {"amazonMarketplaceId": "A1F83G8C2ARO7P", "amazonAsin": "B09TDW5M2C"}
    remote = {
        "summaries": [{"asin": "B09TDW5M2C", "itemName": "NOTEBOOK BASIC", "status": ["BUYABLE"]}],
        "offers": [{"marketplaceId": "A1F83G8C2ARO7P", "price": {"amount": "9.00"}}],
        "fulfillmentAvailability": [{"fulfillmentChannelCode": "DEFAULT", "quantity": 12}],
        "issues": [],
    }
    update = service._remote_listing_update(product, remote)
    assert update["amazonStatus"] == "ACTIVE"
    assert update["price"] == "9.00"
    assert update["totalInventory"] == 12


def test_remote_listing_update_marks_suppressed_listing_with_issues():
    update = service._remote_listing_update(
        {"amazonAsin": "B09TDW5M2C"},
        {"summaries": [{"status": ["BUYABLE"]}], "issues": [{"severity": "WARNING", "enforcementActions": ["SEARCH_SUPPRESSED"]}]},
    )
    assert update["amazonStatus"] == "SUPPRESSED"
    assert len(update["amazonIssues"]) == 1


def test_to_utc_datetime_normalizes_naive_and_aware():
    from datetime import UTC, datetime, timedelta, timezone

    from app.db import to_utc_datetime

    # 1. Naive datetime (e.g. from MongoDB legacy or default BSON decode)
    naive_dt = datetime(2026, 11, 16, 12, 0, 0)  # noqa: DTZ001
    aware_res = to_utc_datetime(naive_dt)
    assert aware_res is not None
    assert aware_res.tzinfo == UTC
    assert aware_res.year == 2026 and aware_res.hour == 12

    # 2. Aware UTC datetime
    utc_dt = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
    res_utc = to_utc_datetime(utc_dt)
    assert res_utc == utc_dt

    # 3. Aware non-UTC datetime (e.g. UTC+6)
    tz_plus_6 = timezone(timedelta(hours=6))
    dt_plus_6 = datetime(2026, 11, 16, 18, 0, 0, tzinfo=tz_plus_6)
    res_converted = to_utc_datetime(dt_plus_6)
    assert res_converted is not None
    assert res_converted.tzinfo == UTC
    assert res_converted.hour == 12

    # 4. ISO string
    res_str = to_utc_datetime("2026-11-16T12:00:00Z")
    assert res_str is not None
    assert res_str.tzinfo == UTC
    assert res_str.hour == 12

    # 5. None / Invalid
    assert to_utc_datetime(None) is None
    assert to_utc_datetime("invalid-date") is None
    assert to_utc_datetime(12345) is None


@pytest.mark.asyncio
async def test_connected_access_token_handles_naive_and_aware_datetimes(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from bson import ObjectId

    from app.crypto_utils import encrypt

    config = {
        "environment": "production",
        "endpoint": "https://sellingpartnerapi-eu.amazon.com",
        "marketplaceId": "A1F83G8C2ARO7P",
        "currency": "GBP",
        "sellerId": "A1TESTSELLER",
        "clientId": "client-id",
        "clientSecret": "client-secret",
        "refreshToken": "refresh-token",
    }

    # Case A: Naive future expiration from MongoDB -> Reuses cached token without TypeError
    naive_future = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    fake_conn_naive = {
        "_id": ObjectId(),
        "userId": "user-1",
        "status": "connected",
        "refreshTokenEncrypted": encrypt("my-refresh-token"),
        "accessTokenEncrypted": encrypt("cached-valid-token"),
        "accessTokenExpiresAt": naive_future,  # Naive datetime!
    }

    class FakeConnCollection:
        def __init__(self, doc):
            self.doc = doc
            self.updated = False

        async def find_one(self, *args, **kwargs):
            return self.doc

        async def update_one(self, *args, **kwargs):
            self.updated = True

    coll_naive = FakeConnCollection(fake_conn_naive)
    monkeypatch.setattr(service, "collection", lambda name: coll_naive)

    token = await service.connected_access_token("user-1", config)
    assert token == "cached-valid-token"
    assert coll_naive.updated is False

    # Case B: Expired naive datetime -> Triggers refresh
    naive_past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    fake_conn_expired = {
        "_id": ObjectId(),
        "userId": "user-1",
        "status": "connected",
        "refreshTokenEncrypted": encrypt("my-refresh-token"),
        "accessTokenEncrypted": encrypt("cached-expired-token"),
        "accessTokenExpiresAt": naive_past,
    }
    coll_expired = FakeConnCollection(fake_conn_expired)
    monkeypatch.setattr(service, "collection", lambda name: coll_expired)

    async def fake_exchange(*args, **kwargs):
        return {"access_token": "newly-refreshed-token", "expires_in": 3600}

    monkeypatch.setattr(service, "exchange_access_token", fake_exchange)

    new_token = await service.connected_access_token("user-1", config)
    assert new_token == "newly-refreshed-token"
    assert coll_expired.updated is True

