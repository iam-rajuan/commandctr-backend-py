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
