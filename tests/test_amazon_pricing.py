from copy import deepcopy
from types import SimpleNamespace
import httpx
import pytest
from app.errors import ApiError
from app.modules.amazon import service

ASIN = "B09TDW5M2C"
CONFIG = {"marketplaceId":"A1F83G8C2ARO7P", "currency":"GBP", "sellerId":"ME", "environment":"production"}

def offer(seller, price, **updates):
    return {"SellerId":seller, "SubCondition":"new", "IsFulfilledByAmazon":False,
            "ListingPrice":{"Amount":price, "CurrencyCode":"GBP"},
            "Shipping":{"Amount":0, "CurrencyCode":"GBP"}, **updates}

def payload(offers):
    return {"ASIN":ASIN, "marketplaceId":CONFIG["marketplaceId"], "status":"Success", "Offers":offers}

def test_filters_currency_condition_own_offer_fulfilment_and_duplicates():
    rows = [offer("A",20), offer("B",30), offer("A",1), offer("ME",1),
            offer("USED",1,SubCondition="good"), offer("FBA",1,IsFulfilledByAmazon=True),
            offer("DOLLARS",1, ListingPrice={"Amount":1,"CurrencyCode":"USD"}),
            offer("LATER",1, ShippingTime={"availabilityType":"FUTURE_WITH_DATE"})]
    result = service.amazon_offer_snapshot(payload(rows), CONFIG, ASIN, "new_new", "DEFAULT")
    assert result["recommended_price"] == 25
    assert result["comparable_count"] == 2
    assert result["currency"] == "GBP"
    assert len(result["similar_listings"]) == 2
    assert result["price_basis"] == "item"

def test_insufficient_sandbox_or_mismatched_identity_cannot_produce_live_prices():
    result = service.amazon_offer_snapshot(payload([offer("A",20)]), CONFIG, ASIN, "new_new", "DEFAULT")
    assert result["recommended_price"] is None
    sandbox = service.amazon_offer_snapshot(payload([offer("A",20),offer("B",30)]),
        {**CONFIG,"environment":"sandbox"},ASIN,"new_new","DEFAULT")
    assert sandbox["recommended_price"] is None
    wrong = payload([offer("A",20),offer("B",30)])
    wrong["ASIN"] = "B000000000"
    with pytest.raises(ApiError):
        service.amazon_offer_snapshot(wrong, CONFIG, ASIN, "new_new", "DEFAULT")

def test_used_subcondition_and_fba_are_matched_exactly():
    result = service.amazon_offer_snapshot(payload([
        offer("A",20,SubCondition="good",IsFulfilledByAmazon=True),
        offer("B",30,SubCondition="good",IsFulfilledByAmazon=True),
        offer("C",1,SubCondition="acceptable",IsFulfilledByAmazon=True)]),
        CONFIG,ASIN,"used_good","AMAZON_EU")
    assert result["recommended_price"] == 25

@pytest.mark.asyncio
async def test_live_service_request_contract_uses_amazon_uk_and_connected_token(monkeypatch):
    captured = {}
    async def token(user, config): return "test-token"
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return httpx.Response(200,json={"payload":payload([offer("A",20),offer("B",30)])})
    monkeypatch.setattr(service,"amazon_config",lambda:{**CONFIG,"endpoint":"https://sellingpartnerapi-eu.amazon.com"})
    monkeypatch.setattr(service,"connected_access_token",token)
    monkeypatch.setattr(service.httpx,"AsyncClient",Client)
    result = await service.competitive_pricing("user",ASIN)
    assert result["recommended_price"] == 25
    assert captured["params"]["MarketplaceId"] == CONFIG["marketplaceId"]
    assert captured["params"]["ItemCondition"] == "New"
    assert captured["url"].endswith(f"/items/{ASIN}/offers")

def test_observed_fba_and_own_prices_remain_visible_without_an_fbm_recommendation():
    result = service.amazon_offer_snapshot(payload([
        offer("OTHER",3.68,IsFulfilledByAmazon=True), offer("ME",5,MyOffer=True)
    ]),CONFIG,ASIN,"new_new","DEFAULT")
    assert result["recommended_price"] is None
    assert result["suggested_price_range"] is None
    assert result["comparable_count"] == 0
    assert len(result["reference_offers"]) == 2
    assert result["reference_offers"][0]["price"] == 3.68
    assert result["reference_offers"][0]["fulfilment"] == "FBA"
    assert result["reference_offers"][0]["exclusion_reasons"] == ["fulfilment_mismatch"]
    assert result["reference_offers"][1]["my_offer"] is True
    assert result["reference_price_range"]["minimum"] == 3.68
    assert result["reference_price_range"]["maximum"] == 3.68
    assert result["reference_price_range"]["count"] == 1
    assert result["offer_diagnostics"]["returned_count"] == 2
    assert result["offer_diagnostics"]["qualifying_count"] == 0

def test_reference_prices_do_not_duplicate_sellers_or_invent_missing_delivery():
    result = service.amazon_offer_snapshot(payload([
        offer("OTHER",10,IsFulfilledByAmazon=True),
        offer("OTHER",10,IsFulfilledByAmazon=True),
        offer("MISSING_DELIVERY",12,Shipping={}),
    ]),CONFIG,ASIN,"new_new","DEFAULT")
    assert result["reference_price_range"]["count"] == 1
    assert result["reference_offers"][2]["price"] == 12
    assert result["reference_offers"][2]["delivered_price"] is None
