from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
import socket
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pymongo.errors import DuplicateKeyError
from starlette import status

from app.auth import current_user, make_token
from app.config import get_settings
from app.crypto_utils import b64url, decrypt, encrypt
from app.db import collection, now_utc, oid, serialize
from app.errors import ApiError, success
from app.schemas import (
    EbayInventoryBody,
    EbayPullListingsBody,
    ShopifyInventoryBody,
    ShopifyPriceBody,
    ShopifyProductMutationBody,
)

shopify_router = APIRouter()
ebay_router = APIRouter()
etsy_router = APIRouter()
worker_task: asyncio.Task | None = None
worker_owner_id: str | None = None


def runtime_worker_owner_id() -> str:
    settings = get_settings()
    configured = normalize(settings.EBAY_WORKER_OWNER_ID)
    if configured:
        return configured
    return f"fastapi:{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"


def normalize(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip("\"'")
    return value or None


def frontend_redirect(params: dict[str, str]) -> str | None:
    base = get_settings().FRONTEND_URL
    if not base:
        return None
    return f"{str(base).rstrip('/')}/integration?{urlencode(params)}"


async def safe_connection(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    clean = dict(row)
    clean.pop("accessTokenEncrypted", None)
    clean.pop("refreshTokenEncrypted", None)
    return serialize(clean | {"id": str(clean["_id"])})


async def get_connection(user_id: str, marketplace: str, environment: str | None = None) -> dict[str, Any] | None:
    query: dict[str, Any] = {"userId": user_id, "marketplace": marketplace}
    if environment:
        query["environment"] = environment
    return await collection("marketplaceconnections").find_one(query, sort=[("updatedAt", -1)])


async def upsert_connection(query: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    now = now_utc()
    payload.setdefault("connectedAt", now)
    payload["updatedAt"] = now
    return await collection("marketplaceconnections").find_one_and_update(
        query,
        {"$set": payload, "$setOnInsert": {"createdAt": now}, "$unset": {"disconnectedAt": ""}},
        upsert=True,
        return_document=True,
    )


async def mark_disconnected(user_id: str, marketplace: str, environment: str | None = None) -> dict[str, Any]:
    query: dict[str, Any] = {"userId": user_id, "marketplace": marketplace}
    if environment:
        query["environment"] = environment
    now = now_utc()
    return await collection("marketplaceconnections").find_one_and_update(
        query,
        {
            "$set": {"status": "disconnected", "disconnectedAt": now, "scopes": [], "updatedAt": now},
            "$unset": {"accessTokenEncrypted": "", "refreshTokenEncrypted": "", "accessTokenExpiresAt": "", "refreshTokenExpiresAt": "", "metadata": ""},
            "$setOnInsert": {"createdAt": now},
        },
        upsert=True,
        return_document=True,
    )


async def shopify_access(user_id: str) -> tuple[str, str]:
    settings = get_settings()
    conn = await get_connection(user_id, "shopify")
    if conn and conn.get("accessTokenEncrypted"):
        return conn.get("shopDomain", ""), decrypt(conn["accessTokenEncrypted"])
    if settings.SHOPIFY_SHOP_DOMAIN and settings.SHOPIFY_ADMIN_ACCESS_TOKEN:
        return settings.SHOPIFY_SHOP_DOMAIN, settings.SHOPIFY_ADMIN_ACCESS_TOKEN
    raise ApiError(status.HTTP_400_BAD_REQUEST, "Shopify store is not connected.")


async def shopify_graphql(user_id: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    shop, token = await shopify_access(user_id)
    url = f"https://{shop}/admin/api/{get_settings().SHOPIFY_API_VERSION}/graphql.json"
    response: httpx.Response | None = None
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(3):
            try:
                response = await client.post(
                    url,
                    headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
                    json={"query": query, "variables": variables or {}},
                )
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to communicate with Shopify", {"error": str(exc)}) from exc
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            if response.status_code not in {429} and response.status_code < 500:
                break
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))

    if response is None:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to communicate with Shopify")
    if response.status_code >= 400:
        details: Any
        try:
            details = response.json()
        except ValueError:
            details = response.text[:1000]
        raise ApiError(
            status.HTTP_502_BAD_GATEWAY,
            "Failed to communicate with Shopify",
            {"statusCode": response.status_code, "response": details},
        )
    payload = response.json()
    if payload.get("errors"):
        raise ApiError(status.HTTP_502_BAD_GATEWAY, payload["errors"][0].get("message", "Shopify GraphQL returned an error"), payload["errors"])
    return payload.get("data") or {}


def shopify_status(conn: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "connected": bool(conn and conn.get("status") == "connected" and conn.get("accessTokenEncrypted")),
        "shopDomain": conn.get("shopDomain") if conn else get_settings().SHOPIFY_SHOP_DOMAIN,
        "status": conn.get("status") if conn else "not_connected",
        "scopes": conn.get("scopes", []) if conn else [],
        "connectedAt": conn.get("connectedAt") if conn else None,
        "lastSyncedAt": conn.get("lastSyncedAt") if conn else None,
    }


@shopify_router.get("/connect")
async def shopify_connect(user=Depends(current_user)):
    return RedirectResponse(await shopify_connect_url_value(user["userId"]))


async def shopify_connect_url_value(user_id: str) -> str:
    settings = get_settings()
    if not settings.SHOPIFY_SHOP_DOMAIN or not settings.SHOPIFY_CLIENT_ID or not settings.SHOPIFY_REDIRECT_URI:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Shopify OAuth credentials are not configured.")
    state = make_token({"userId": user_id, "marketplace": "shopify", "nonce": os.urandom(12).hex()}, settings.JWT_ACCESS_SECRET, "10m")
    params = {"client_id": settings.SHOPIFY_CLIENT_ID, "scope": "read_products,write_products,read_orders,write_orders,read_inventory,write_inventory", "redirect_uri": str(settings.SHOPIFY_REDIRECT_URI), "state": state}
    return f"https://{settings.SHOPIFY_SHOP_DOMAIN}/admin/oauth/authorize?{urlencode(params)}"


@shopify_router.get("/connect-url")
async def shopify_connect_url(user=Depends(current_user)):
    return success("Shopify connect URL generated successfully", {"connectUrl": await shopify_connect_url_value(user["userId"])})


@shopify_router.get("/callback")
async def shopify_callback(request: Request):
    settings = get_settings()
    shop = request.query_params.get("shop", "")
    code = request.query_params.get("code", "")
    state_token = request.query_params.get("state", "")
    hmac_value = request.query_params.get("hmac", "")
    if not shop or not code or not state_token:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Missing required Shopify OAuth callback parameters.")
    if settings.SHOPIFY_CLIENT_SECRET and hmac_value:
        pairs = [(k, v) for k, v in request.query_params.multi_items() if k not in {"hmac", "signature"}]
        message = "&".join(f"{k}={v}" for k, v in sorted(pairs))
        digest = hmac.new(settings.SHOPIFY_CLIENT_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, hmac_value.lower()):
            raise ApiError(status.HTTP_400_BAD_REQUEST, "Invalid Shopify OAuth HMAC.")
    try:
        state_payload = jwt.decode(state_token, settings.JWT_ACCESS_SECRET, algorithms=["HS256"])
        user_id = state_payload["userId"]
    except Exception as exc:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Shopify OAuth state is invalid or has expired.") from exc
    async with httpx.AsyncClient(timeout=30) as client:
        token_res = await client.post(f"https://{shop}/admin/oauth/access_token", json={"client_id": settings.SHOPIFY_CLIENT_ID, "client_secret": settings.SHOPIFY_CLIENT_SECRET, "code": code})
    if token_res.status_code >= 400 or not token_res.json().get("access_token"):
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to exchange Shopify OAuth code for access token.")
    token_payload = token_res.json()
    await upsert_connection({"userId": user_id, "marketplace": "shopify", "shopDomain": shop.lower()}, {"userId": user_id, "marketplace": "shopify", "shopDomain": shop.lower(), "accessTokenEncrypted": encrypt(token_payload["access_token"]), "scopes": str(token_payload.get("scope", "")).split(","), "status": "connected", "lastCheckedAt": now_utc()})
    redirect = frontend_redirect({"marketplace": "shopify", "status": "connected"})
    if redirect:
        return RedirectResponse(redirect)
    return {"success": True, "message": "Shopify store connected successfully", "data": {"marketplace": "shopify", "shopDomain": shop, "status": "connected"}}


@shopify_router.get("/product-image")
async def product_image(path: str, response: Response):
    if not path:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Product image path is required.")
    async with httpx.AsyncClient(timeout=30) as client:
        proxied = await client.get(path)
    if proxied.status_code >= 400:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to fetch product image.")
    response.headers["Cache-Control"] = "no-store"
    return Response(proxied.content, media_type=proxied.headers.get("content-type", "application/octet-stream"))


@shopify_router.post("/disconnect")
async def shopify_disconnect(user=Depends(current_user)):
    return success("Shopify store disconnected successfully", await safe_connection(await mark_disconnected(user["userId"], "shopify")))


@shopify_router.get("/status")
async def shopify_get_status(user=Depends(current_user)):
    return success("Shopify connection status fetched successfully", serialize(shopify_status(await get_connection(user["userId"], "shopify"))))


@shopify_router.get("/shop")
async def shopify_shop(user=Depends(current_user)):
    data = await shopify_graphql(user["userId"], "query { shop { id name email myshopifyDomain primaryDomain { url host } currencyCode } }")
    return success("Shopify shop fetched successfully", data.get("shop"))


def map_product(user_id: str, node: dict[str, Any]) -> dict[str, Any]:
    variants = node.get("variants", {}).get("nodes", [])
    images = [m.get("image", {}).get("url") or m.get("preview", {}).get("image", {}).get("url") for m in node.get("media", {}).get("nodes", [])]
    image = (node.get("featuredImage") or {}).get("url") or next((i for i in images if i), "")
    return {"userId": user_id, "marketplace": "shopify", "shopifyProductId": node.get("id"), "title": node.get("title"), "description": node.get("descriptionHtml", ""), "handle": node.get("handle"), "vendor": node.get("vendor", ""), "productType": node.get("productType", ""), "status": node.get("status", ""), "tags": node.get("tags") or [], "totalInventory": node.get("totalInventory") or 0, "featuredImage": image, "images": [i for i in images if i], "variants": [{"shopifyVariantId": v.get("id"), "title": v.get("title"), "sku": v.get("sku", ""), "price": v.get("price", ""), "compareAtPrice": v.get("compareAtPrice", ""), "inventoryQuantity": v.get("inventoryQuantity") or 0, "inventoryItemId": (v.get("inventoryItem") or {}).get("id", "")} for v in variants], "rawShopifyData": node, "lastSyncedAt": now_utc()}


PRODUCT_QUERY = """query { products(first: 100) { nodes { id title descriptionHtml handle vendor productType status tags totalInventory featuredImage { url } media(first: 20) { nodes { image { url } preview { image { url } } } } variants(first: 50) { nodes { id title sku price compareAtPrice inventoryQuantity inventoryItem { id } } } } } }"""


def shopify_product_input(body: ShopifyProductMutationBody, product_id: str | None = None) -> tuple[dict[str, Any], list[str]]:
    raw = body.model_dump(exclude_none=True)
    product_input: dict[str, Any] = {}
    warnings: list[str] = []

    for key in ("title", "vendor", "productType", "handle", "status", "tags"):
        if raw.get(key) is not None:
            product_input[key] = raw[key]

    if raw.get("description"):
        product_input["descriptionHtml"] = raw["description"]
    if product_id:
        product_input["id"] = product_id

    seo = {}
    if raw.get("seoTitle"):
        seo["title"] = raw["seoTitle"]
    if raw.get("seoDescription"):
        seo["description"] = raw["seoDescription"]
    if seo:
        product_input["seo"] = seo

    if raw.get("themeTemplate"):
        product_input["templateSuffix"] = raw["themeTemplate"]

    collection_ids = [value for value in raw.get("collections", []) if isinstance(value, str) and value.startswith("gid://shopify/Collection/")]
    if collection_ids:
        product_input["collectionsToJoin"] = collection_ids
    elif raw.get("collections"):
        warnings.append("Collections were skipped because Shopify expects collection GIDs.")

    if isinstance(raw.get("category"), dict):
        product_input["category"] = raw["category"]
    elif raw.get("category"):
        warnings.append("Category was skipped because Shopify expects a taxonomy category object.")

    if isinstance(raw.get("metafields"), dict):
        metafields = []
        for key, value in raw["metafields"].items():
            if value in {None, ""}:
                continue
            clean_key = re.sub(r"[^a-z0-9_]", "_", str(key).strip().lower()).strip("_")
            if clean_key:
                metafields.append({"namespace": "custom", "key": clean_key[:64], "type": "single_line_text_field", "value": str(value)})
        if metafields:
            product_input["metafields"] = metafields

    skipped = [key for key in ("imagePath", "publishToOnlineStore", "compareAtPrice", "costPerItem", "chargeTax", "barcode", "weight", "weightUnit", "countryOfOrigin", "hsCode", "seoHandle", "variants") if raw.get(key) is not None]
    if skipped:
        warnings.append(f"Skipped fields not handled by Shopify productCreate: {', '.join(skipped)}.")

    return product_input, warnings


@shopify_router.get("/products")
async def shopify_products(user=Depends(current_user)):
    data = await shopify_graphql(user["userId"], PRODUCT_QUERY)
    return success("Shopify products fetched successfully", data.get("products", {}).get("nodes", []))


@shopify_router.post("/import-products")
async def import_products(user=Depends(current_user)):
    data = await shopify_graphql(user["userId"], PRODUCT_QUERY)
    nodes = data.get("products", {}).get("nodes", [])
    imported = updated = 0
    live_keys = []
    for node in nodes:
        mapped = map_product(user["userId"], node)
        live_keys.extend([v.get("inventoryItemId") + "::" for v in mapped["variants"] if v.get("inventoryItemId")])
        result = await collection("products").update_one({"userId": user["userId"], "shopifyProductId": mapped["shopifyProductId"]}, {"$set": mapped | {"updatedAt": now_utc()}, "$setOnInsert": {"createdAt": now_utc()}}, upsert=True)
        imported += int(bool(result.upserted_id))
        updated += int(not result.upserted_id)
        for variant in mapped["variants"]:
            if variant.get("inventoryItemId"):
                await collection("inventories").update_one({"userId": user["userId"], "inventoryItemId": variant["inventoryItemId"], "locationId": ""}, {"$set": {"userId": user["userId"], "marketplace": "shopify", "shopifyProductId": mapped["shopifyProductId"], "shopifyVariantId": variant.get("shopifyVariantId"), "inventoryItemId": variant["inventoryItemId"], "sku": variant.get("sku"), "title": f"{mapped['title']} - {variant.get('title')}", "shopifyQuantity": variant.get("inventoryQuantity", 0), "availableQuantity": variant.get("inventoryQuantity", 0), "isActive": True, "sourceStatus": "active", "lastSyncedAt": now_utc(), "updatedAt": now_utc()}, "$setOnInsert": {"createdAt": now_utc()}}, upsert=True)
    await collection("marketplaceconnections").update_one({"userId": user["userId"], "marketplace": "shopify"}, {"$set": {"lastSyncedAt": now_utc(), "status": "connected", "updatedAt": now_utc()}})
    return success("Shopify products imported successfully", {"count": len(nodes), "importedCount": imported, "updatedCount": updated, "staleMarkedInactiveCount": 0, "activeCount": len(nodes)})


@shopify_router.post("/products", status_code=201)
async def shopify_create_product(body: ShopifyProductMutationBody, user=Depends(current_user)):
    product_input, warnings = shopify_product_input(body)
    data = await shopify_graphql(user["userId"], "mutation productCreate($input: ProductInput!) { productCreate(input: $input) { product { id title } userErrors { field message } } }", {"input": product_input})
    errors = data.get("productCreate", {}).get("userErrors", [])
    if errors:
        raise ApiError(status.HTTP_400_BAD_REQUEST, errors[0].get("message", "Shopify product create failed."))
    product = data.get("productCreate", {}).get("product")
    return success("Shopify product created successfully", {"product": product, "warnings": warnings, "imageUploaded": False, "publishedToOnlineStore": False})


@shopify_router.patch("/products/price")
@shopify_router.patch("/products/{shopifyProductId:path}/price")
async def shopify_update_price(body: ShopifyPriceBody, shopifyProductId: str | None = None, user=Depends(current_user)):
    variables = {
        "productId": shopifyProductId,
        "variants": [{"id": body.variantId, "price": body.price, "compareAtPrice": body.compareAtPrice}],
    }
    data = await shopify_graphql(
        user["userId"],
        "mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) { productVariantsBulkUpdate(productId: $productId, variants: $variants) { productVariants { id price compareAtPrice } userErrors { field message } } }",
        variables,
    )
    errors = data.get("productVariantsBulkUpdate", {}).get("userErrors", [])
    if errors:
        raise ApiError(status.HTTP_400_BAD_REQUEST, errors[0].get("message", "Shopify price update failed."))
    return success("Shopify product price updated successfully", data.get("productVariantsBulkUpdate"))


@shopify_router.patch("/products")
@shopify_router.patch("/products/{shopifyProductId:path}")
async def shopify_update_product(body: ShopifyProductMutationBody, shopifyProductId: str | None = None, id: str | None = None, user=Depends(current_user)):
    product_id = shopifyProductId or id
    if not product_id:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Shopify product id is required.")
    product_input, warnings = shopify_product_input(body, product_id)
    data = await shopify_graphql(user["userId"], "mutation productUpdate($input: ProductInput!) { productUpdate(input: $input) { product { id title } userErrors { field message } } }", {"input": product_input})
    errors = data.get("productUpdate", {}).get("userErrors", [])
    if errors:
        raise ApiError(status.HTTP_400_BAD_REQUEST, errors[0].get("message", "Shopify product update failed."))
    return success("Shopify product updated successfully", {"product": data.get("productUpdate", {}).get("product"), "warnings": warnings, "imageUploaded": False, "publishedToOnlineStore": False})


@shopify_router.get("/orders")
async def shopify_orders(user=Depends(current_user)):
    data = await shopify_graphql(user["userId"], "query { orders(first: 100) { nodes { id name email displayFinancialStatus displayFulfillmentStatus currencyCode processedAt totalPriceSet { shopMoney { amount } } subtotalPriceSet { shopMoney { amount } } totalTaxSet { shopMoney { amount } } customer { firstName lastName } lineItems(first: 50) { nodes { title sku quantity originalUnitPriceSet { shopMoney { amount } } variant { id } product { id } } } shippingAddress { formatted name phone } billingAddress { formatted name phone } } } }")
    return success("Shopify orders fetched successfully", data.get("orders", {}).get("nodes", []))


async def shopify_import_orders_for_user(user_id: str) -> dict[str, int]:
    data = await shopify_graphql(user_id, "query { orders(first: 100, sortKey: PROCESSED_AT, reverse: true) { nodes { id name email displayFinancialStatus displayFulfillmentStatus currencyCode processedAt totalPriceSet { shopMoney { amount } } subtotalPriceSet { shopMoney { amount } } totalTaxSet { shopMoney { amount } } customer { firstName lastName } lineItems(first: 50) { nodes { title sku quantity originalUnitPriceSet { shopMoney { amount } } variant { id } product { id } } } shippingAddress { formatted name phone } billingAddress { formatted name phone } } } }")
    nodes = data.get("orders", {}).get("nodes", [])
    for node in nodes:
        mapped = {"userId": user_id, "marketplace": "shopify", "externalOrderId": node.get("id"), "shopifyOrderId": node.get("id"), "orderName": node.get("name"), "email": node.get("email", ""), "customerName": " ".join(filter(None, [(node.get("customer") or {}).get("firstName"), (node.get("customer") or {}).get("lastName")])), "financialStatus": node.get("displayFinancialStatus", ""), "fulfillmentStatus": node.get("displayFulfillmentStatus", ""), "currency": node.get("currencyCode", ""), "totalPrice": (((node.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount") or "0"), "subtotalPrice": (((node.get("subtotalPriceSet") or {}).get("shopMoney") or {}).get("amount") or "0"), "totalTax": (((node.get("totalTaxSet") or {}).get("shopMoney") or {}).get("amount") or "0"), "processedAt": node.get("processedAt"), "lineItems": [{"title": i.get("title"), "sku": i.get("sku", ""), "quantity": i.get("quantity", 0), "price": (((i.get("originalUnitPriceSet") or {}).get("shopMoney") or {}).get("amount") or "0"), "shopifyVariantId": (i.get("variant") or {}).get("id", ""), "shopifyProductId": (i.get("product") or {}).get("id", "")} for i in (node.get("lineItems") or {}).get("nodes", [])], "shippingAddress": node.get("shippingAddress"), "billingAddress": node.get("billingAddress"), "rawShopifyData": node, "lastSyncedAt": now_utc(), "updatedAt": now_utc()}
        await collection("orders").update_one({"userId": user_id, "shopifyOrderId": node.get("id")}, {"$set": mapped, "$setOnInsert": {"createdAt": now_utc()}}, upsert=True)
    await collection("marketplaceconnections").update_one({"userId": user_id, "marketplace": "shopify"}, {"$set": {"lastSyncedAt": now_utc(), "status": "connected", "updatedAt": now_utc()}})
    await collection("sync_logs").insert_one({"userId": user_id, "entityType": "order", "action": "import-orders", "status": "success", "message": f"Imported {len(nodes)} Shopify orders", "createdAt": now_utc()})
    return {"count": len(nodes), "importedCount": len(nodes), "updatedCount": 0}


@shopify_router.post("/import-orders")
async def import_orders(user=Depends(current_user)):
    return success("Shopify orders imported successfully", await shopify_import_orders_for_user(user["userId"]))


async def shopify_mark_order_as_shipped(user_id: str, shopify_order_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    data = await shopify_graphql(
        user_id,
        "query GetOrderFulfillmentOrders($id: ID!) { order(id: $id) { fulfillmentOrders(first: 20) { nodes { id status supportedActions { action } lineItems(first: 50) { nodes { id remainingQuantity } } } } } }",
        {"id": shopify_order_id},
    )
    fulfillment_orders = (((data.get("order") or {}).get("fulfillmentOrders") or {}).get("nodes") or [])
    fulfillable = []
    for order in fulfillment_orders:
        supports_create = any(action.get("action") == "CREATE_FULFILLMENT" for action in order.get("supportedActions", []))
        remaining = [item for item in ((order.get("lineItems") or {}).get("nodes") or []) if (item.get("remainingQuantity") or 0) > 0]
        if str(order.get("status") or "").upper() != "CLOSED" and supports_create and remaining:
            fulfillable.append({"fulfillmentOrderId": order["id"], "fulfillmentOrderLineItems": [{"id": item["id"], "quantity": item.get("remainingQuantity") or 0} for item in remaining]})
    if not fulfillable:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "No fulfillable Shopify fulfillment orders were found for this order")
    result = await shopify_graphql(
        user_id,
        "mutation CreateFulfillment($fulfillment: FulfillmentInput!) { fulfillmentCreate(fulfillment: $fulfillment) { fulfillment { id status } userErrors { message } } }",
        {"fulfillment": {"lineItemsByFulfillmentOrder": fulfillable, "notifyCustomer": payload.get("notifyCustomer", True), "trackingInfo": {"company": payload.get("carrier"), "number": payload["trackingNumber"], "url": payload.get("trackingUrl")}}},
    )
    errors = result.get("fulfillmentCreate", {}).get("userErrors", [])
    if errors:
        raise ApiError(status.HTTP_400_BAD_REQUEST, errors[0].get("message", "Shopify fulfillment creation failed"))
    await collection("marketplaceconnections").update_one({"userId": user_id, "marketplace": "shopify"}, {"$set": {"lastSyncedAt": now_utc(), "status": "connected", "updatedAt": now_utc()}})
    await collection("sync_logs").insert_one({"userId": user_id, "entityType": "order", "entityId": shopify_order_id, "action": "mark-shipped", "status": "success", "message": "Marked Shopify order as shipped successfully", "createdAt": now_utc()})
    return result.get("fulfillmentCreate", {}).get("fulfillment")


async def shopify_cancel_order(user_id: str, shopify_order_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    result = await shopify_graphql(
        user_id,
        "mutation CancelOrder($orderId: ID!, $notifyCustomer: Boolean, $reason: OrderCancelReason!, $restock: Boolean!, $refundMethod: OrderCancelRefundMethodInput, $staffNote: String) { orderCancel(orderId: $orderId, notifyCustomer: $notifyCustomer, reason: $reason, restock: $restock, refundMethod: $refundMethod, staffNote: $staffNote) { job { id } orderCancelUserErrors { message } userErrors { message } } }",
        {"orderId": shopify_order_id, "notifyCustomer": payload.get("notifyCustomer", False), "reason": payload.get("reason", "OTHER"), "restock": payload.get("restock", True), "refundMethod": {"originalPaymentMethodsRefund": True} if payload.get("refund", True) else None, "staffNote": payload.get("staffNote")},
    )
    cancel = result.get("orderCancel", {})
    errors = cancel.get("orderCancelUserErrors", []) or cancel.get("userErrors", [])
    if errors:
        raise ApiError(status.HTTP_400_BAD_REQUEST, errors[0].get("message", "Shopify order cancellation failed"))
    await collection("marketplaceconnections").update_one({"userId": user_id, "marketplace": "shopify"}, {"$set": {"lastSyncedAt": now_utc(), "status": "connected", "updatedAt": now_utc()}})
    await collection("sync_logs").insert_one({"userId": user_id, "entityType": "order", "entityId": shopify_order_id, "action": "cancel-order", "status": "success", "message": "Cancelled Shopify order successfully", "createdAt": now_utc()})
    return cancel.get("job")


@shopify_router.post("/import-disputes")
async def import_disputes(user=Depends(current_user)):
    conn = await get_connection(user["userId"], "shopify")
    scopes = conn.get("scopes", []) if conn else []
    required_scope = "read_shopify_payments_disputes"
    if not any(scope in scopes for scope in [required_scope, "read_shopify_payments", "read_shopify_payments_payouts"]):
        return success(
            "Shopify disputes import completed",
            {
                "success": False,
                "reason": "missing_scope",
                "requiredScope": required_scope,
                "count": 0,
                "message": "Reconnect Shopify with dispute permissions.",
            },
        )
    query = """
    query GetShopifyPaymentsDisputes {
      disputes(first: 50) {
        nodes {
          id status initiatedAt finalizedOn evidenceDueBy type
          reasonDetails { reason networkReasonCode }
          amount { amount currencyCode }
          order { id name email customer { firstName lastName } }
        }
      }
    }
    """
    try:
        data = await shopify_graphql(user["userId"], query)
        count = 0
        for dispute in data.get("disputes", {}).get("nodes", []):
            dispute_id = dispute.get("id")
            if not dispute_id:
                continue
            order = dispute.get("order") or {}
            customer = order.get("customer") or {}
            local_order = None
            if order.get("id"):
                local_order = await collection("orders").find_one({"userId": user["userId"], "shopifyOrderId": order["id"]})
            mapped = {
                "userId": user["userId"],
                "marketplace": "shopify",
                "disputeId": dispute_id,
                "shopifyDisputeId": dispute_id,
                "shopifyOrderId": order.get("id"),
                "localOrderId": local_order.get("_id") if local_order else None,
                "orderName": order.get("name"),
                "customerName": " ".join(filter(None, [customer.get("firstName"), customer.get("lastName")])),
                "customerEmail": order.get("email", ""),
                "status": str(dispute.get("status") or "UNKNOWN").upper(),
                "reason": ((dispute.get("reasonDetails") or {}).get("reason") or dispute.get("type") or "UNKNOWN"),
                "networkReasonCode": (dispute.get("reasonDetails") or {}).get("networkReasonCode"),
                "amount": (dispute.get("amount") or {}).get("amount"),
                "currency": (dispute.get("amount") or {}).get("currencyCode"),
                "sourceCreatedAt": dispute.get("initiatedAt"),
                "finalizedAt": dispute.get("finalizedOn"),
                "evidenceDueBy": dispute.get("evidenceDueBy"),
                "rawShopifyData": dispute,
                "lastSyncedAt": now_utc(),
                "updatedAt": now_utc(),
            }
            await collection("disputes").update_one(
                {"userId": user["userId"], "marketplace": "shopify", "disputeId": dispute_id},
                {"$set": mapped, "$setOnInsert": {"createdAt": now_utc()}},
                upsert=True,
            )
            count += 1
        await collection("marketplaceconnections").update_one({"userId": user["userId"], "marketplace": "shopify"}, {"$set": {"lastSyncedAt": now_utc(), "status": "connected", "updatedAt": now_utc()}})
        await collection("sync_logs").insert_one({"userId": user["userId"], "entityType": "order", "action": "import-disputes", "status": "success", "message": f"Imported {count} Shopify disputes", "createdAt": now_utc()})
        return success("Shopify disputes import completed", {"success": True, "reason": "imported" if count else "no_disputes", "count": count, "message": "Shopify disputes imported successfully." if count else "No Shopify disputes found."})
    except ApiError as exc:
        return success(
            "Shopify disputes import completed",
            {
                "success": False,
                "reason": "shopify_api_error",
                "count": 0,
                "message": exc.message,
                "details": {"provider": "shopify", "operation": "disputes", "statusCode": exc.status_code, "errorMessage": exc.message},
            },
        )


@shopify_router.get("/locations")
async def locations(user=Depends(current_user)):
    data = await shopify_graphql(user["userId"], "query { locations(first: 100) { nodes { id name isActive } } }")
    return success("Shopify locations fetched successfully", data.get("locations", {}).get("nodes", []))


@shopify_router.get("/inventory")
async def shopify_inventory(user=Depends(current_user)):
    rows = [r async for r in collection("inventories").find({"userId": user["userId"], "marketplace": "shopify", "isActive": {"$ne": False}}).sort("updatedAt", -1)]
    return success("Shopify inventory fetched successfully", serialize(rows))


@shopify_router.patch("/inventory")
@shopify_router.patch("/inventory/{inventoryItemId:path}")
async def shopify_update_inventory(body: ShopifyInventoryBody, inventoryItemId: str | None = None, id: str | None = None, user=Depends(current_user)):
    item_id = inventoryItemId or id
    if not item_id:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Shopify inventory item id is required.")
    data = await shopify_graphql(user["userId"], "mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) { inventorySetQuantities(input: $input) { inventoryAdjustmentGroup { createdAt } userErrors { field message } } }", {"input": {"name": "available", "reason": "correction", "quantities": [{"inventoryItemId": item_id, "locationId": body.locationId, "quantity": body.quantity}]}})
    await collection("inventories").update_one({"userId": user["userId"], "inventoryItemId": item_id, "locationId": body.locationId}, {"$set": {"shopifyQuantity": body.quantity, "availableQuantity": body.quantity, "lastSyncedAt": now_utc(), "updatedAt": now_utc()}}, upsert=False)
    return success("Shopify inventory updated successfully", data.get("inventorySetQuantities"))


def ebay_env() -> str:
    return get_settings().EBAY_ENVIRONMENT or "sandbox"


def ebay_config() -> dict[str, str | None]:
    s = get_settings()
    prod = ebay_env() == "production"
    return {"environment": ebay_env(), "clientId": normalize(s.EBAY_PRODUCTION_CLIENT_ID if prod else s.EBAY_SANDBOX_CLIENT_ID) or normalize(s.EBAY_CLIENT_ID), "clientSecret": normalize(s.EBAY_PRODUCTION_CLIENT_SECRET if prod else s.EBAY_SANDBOX_CLIENT_SECRET) or normalize(s.EBAY_CLIENT_SECRET), "ruName": normalize(s.EBAY_PRODUCTION_RU_NAME if prod else s.EBAY_SANDBOX_RU_NAME) or normalize(s.EBAY_RU_NAME), "authUrl": str((s.EBAY_PRODUCTION_AUTH_URL if prod else s.EBAY_SANDBOX_AUTH_URL) or ("https://auth.ebay.com/oauth2/authorize" if prod else "https://auth.sandbox.ebay.com/oauth2/authorize")), "tokenUrl": str((s.EBAY_PRODUCTION_TOKEN_URL if prod else s.EBAY_SANDBOX_TOKEN_URL) or ("https://api.ebay.com/identity/v1/oauth2/token" if prod else "https://api.sandbox.ebay.com/identity/v1/oauth2/token"))}


def ebay_scopes() -> list[str]:
    raw = normalize(get_settings().EBAY_SCOPES)
    return [scope for scope in raw.replace(",", " ").split() if scope] if raw else ["https://api.ebay.com/oauth/api_scope/sell.account.readonly"]


def ebay_marketplace_currency(marketplace_id: str) -> str:
    return {
        "EBAY_US": "USD",
        "EBAY_GB": "GBP",
        "EBAY_AU": "AUD",
        "EBAY_CA": "CAD",
        "EBAY_DE": "EUR",
        "EBAY_FR": "EUR",
        "EBAY_ES": "EUR",
        "EBAY_IT": "EUR",
        "EBAY_NL": "EUR",
        "EBAY_BE": "EUR",
    }.get(marketplace_id.strip().upper(), "USD")


def ebay_locale(marketplace_id: str) -> str:
    return {
        "EBAY_US": "en-US",
        "EBAY_GB": "en-GB",
        "EBAY_AU": "en-AU",
        "EBAY_CA": "en-CA",
        "EBAY_DE": "de-DE",
        "EBAY_FR": "fr-FR",
        "EBAY_ES": "es-ES",
        "EBAY_IT": "it-IT",
        "EBAY_NL": "nl-NL",
        "EBAY_BE": "nl-BE",
    }.get(marketplace_id.strip().upper(), "en-US")


def ebay_commerce_config() -> dict[str, str]:
    settings = get_settings()
    environment = ebay_env()
    marketplace_id = normalize(settings.EBAY_MARKETPLACE_ID) or ("EBAY_US" if environment == "production" else "EBAY_GB")
    required = {
        "merchantLocationKey": normalize(settings.EBAY_MERCHANT_LOCATION_KEY),
        "paymentPolicyId": normalize(settings.EBAY_PAYMENT_POLICY_ID),
        "returnPolicyId": normalize(settings.EBAY_RETURN_POLICY_ID),
        "fulfillmentPolicyId": normalize(settings.EBAY_FULFILLMENT_POLICY_ID),
        "categoryId": normalize(settings.EBAY_DEFAULT_CATEGORY_ID),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ApiError(status.HTTP_400_BAD_REQUEST, f"Missing required eBay commerce configuration: {missing[0]}.")
    return {
        "environment": environment,
        "apiBaseUrl": str(settings.EBAY_API_BASE_URL or ("https://api.ebay.com" if environment == "production" else "https://api.sandbox.ebay.com")),
        "marketplaceId": marketplace_id,
        "locale": ebay_locale(marketplace_id),
        "currency": normalize(settings.EBAY_DEFAULT_CURRENCY) or ebay_marketplace_currency(marketplace_id),
        "listingFormat": normalize(settings.EBAY_LISTING_FORMAT) or "FIXED_PRICE",
        "listingDuration": normalize(settings.EBAY_LISTING_DURATION) or "GTC",
        "condition": normalize(settings.EBAY_DEFAULT_CONDITION) or "NEW",
        **required,
    }


async def refresh_ebay_access_token(conn: dict[str, Any]) -> str:
    cfg = ebay_config()
    if not cfg["clientId"] or not cfg["clientSecret"] or not cfg["tokenUrl"]:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "eBay OAuth credentials are not configured.")
    if not conn.get("refreshTokenEncrypted"):
        await collection("marketplaceconnections").update_one({"_id": conn["_id"]}, {"$set": {"status": "error", "updatedAt": now_utc()}})
        raise ApiError(status.HTTP_400_BAD_REQUEST, "eBay refresh token is unavailable. Reconnect the seller account.")
    credentials = base64.b64encode(f"{cfg['clientId']}:{cfg['clientSecret']}".encode()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            cfg["tokenUrl"],
            headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": decrypt(conn["refreshTokenEncrypted"]), "scope": " ".join(ebay_scopes())},
        )
    token = res.json()
    if res.status_code >= 400 or not token.get("access_token"):
        await collection("marketplaceconnections").update_one({"_id": conn["_id"]}, {"$set": {"status": "error", "lastCheckedAt": now_utc(), "updatedAt": now_utc()}})
        raise ApiError(status.HTTP_502_BAD_GATEWAY, token.get("error_description") or "Failed to refresh eBay access token.")
    updates = {
        "accessTokenEncrypted": encrypt(token["access_token"]),
        "accessTokenExpiresAt": now_utc() + timedelta(seconds=token.get("expires_in", 0)),
        "scopes": token.get("scope", " ".join(conn.get("scopes") or ebay_scopes())).split(),
        "status": "connected",
        "lastCheckedAt": now_utc(),
        "updatedAt": now_utc(),
    }
    if token.get("refresh_token"):
        updates["refreshTokenEncrypted"] = encrypt(token["refresh_token"])
    if token.get("refresh_token_expires_in"):
        updates["refreshTokenExpiresAt"] = now_utc() + timedelta(seconds=token["refresh_token_expires_in"])
    await collection("marketplaceconnections").update_one({"_id": conn["_id"]}, {"$set": updates})
    return token["access_token"]


async def ebay_access_token(user_id: str, environment: str | None = None) -> str:
    conn = await get_connection(user_id, "ebay", environment or ebay_env())
    if not conn or conn.get("status") != "connected" or not conn.get("accessTokenEncrypted"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "eBay seller account is not connected.")
    expires_at = conn.get("accessTokenExpiresAt")
    if expires_at and expires_at > now_utc() + timedelta(minutes=1):
        return decrypt(conn["accessTokenEncrypted"])
    return await refresh_ebay_access_token(conn)


async def ebay_request(
    user_id: str,
    method: str,
    path: str,
    operation: str,
    body: Any | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = ebay_commerce_config()
    url = f"{cfg['apiBaseUrl'].rstrip('/')}{path}"
    headers = {"Accept": "application/json", "Accept-Language": cfg["locale"]}
    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(3):
        headers["Authorization"] = f"Bearer {await ebay_access_token(user_id, cfg['environment'])}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Language"] = cfg["locale"]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.request(method, url, params=query or {}, headers=headers, json=body)
            try:
                payload = res.json() if res.content else None
            except ValueError:
                payload = res.text or None
            if res.status_code < 400:
                return {"success": True, "marketplace": "ebay", "operation": operation, "statusCode": res.status_code, "retryable": False, "data": payload}
            retryable = res.status_code in retryable_statuses
            message = "eBay " + operation + f" failed with HTTP {res.status_code}."
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("error") or message
                if payload.get("errors"):
                    message = "; ".join(e.get("longMessage") or e.get("message") or "" for e in payload["errors"]).strip("; ") or message
            if retryable and attempt < 2:
                await asyncio.sleep(min(1 * 2**attempt, 8))
                continue
            raise ApiError(res.status_code, message, {"success": False, "marketplace": "ebay", "operation": operation, "statusCode": res.status_code, "retryable": retryable, "error": message, "data": payload})
        except ApiError:
            raise
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            if attempt < 2:
                await asyncio.sleep(min(1 * 2**attempt, 8))
                continue
            raise ApiError(status.HTTP_502_BAD_GATEWAY, f"eBay {operation} failed.", {"success": False, "marketplace": "ebay", "operation": operation, "statusCode": 502, "retryable": False, "error": str(exc)}) from exc
    raise ApiError(status.HTTP_502_BAD_GATEWAY, f"eBay {operation} failed after retries.")


@ebay_router.get("/connect-url")
async def ebay_connect_url(user=Depends(current_user)):
    cfg = ebay_config()
    if not cfg["clientId"] or not cfg["clientSecret"] or not cfg["ruName"]:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "eBay OAuth credentials are not configured.")
    state = make_token({"userId": user["userId"], "nonce": os.urandom(16).hex(), "marketplace": "ebay", "environment": cfg["environment"]}, get_settings().JWT_ACCESS_SECRET, "10m", iss="commandctr", aud="ebay-oauth")
    return success("eBay connect URL generated successfully", {"connectUrl": f"{cfg['authUrl']}?{urlencode({'client_id': cfg['clientId'], 'redirect_uri': cfg['ruName'], 'response_type': 'code', 'scope': ' '.join(ebay_scopes()), 'state': state})}"})


@ebay_router.get("/callback")
async def ebay_callback(request: Request):
    cfg = ebay_config()
    if request.query_params.get("error"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, request.query_params.get("error_description") or request.query_params["error"])
    code, state = request.query_params.get("code"), request.query_params.get("state")
    if not code or not state:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Missing required eBay OAuth callback parameters.")
    payload = jwt.decode(state, get_settings().JWT_ACCESS_SECRET, algorithms=["HS256"], issuer="commandctr", audience="ebay-oauth")
    credentials = base64.b64encode(f"{cfg['clientId']}:{cfg['clientSecret']}".encode()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(cfg["tokenUrl"], headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"}, data={"grant_type": "authorization_code", "code": code, "redirect_uri": cfg["ruName"]})
    token = res.json()
    if res.status_code >= 400 or not token.get("access_token"):
        raise ApiError(status.HTTP_502_BAD_GATEWAY, token.get("error_description") or "Failed to exchange eBay OAuth code for tokens.")
    await upsert_connection({"userId": payload["userId"], "marketplace": "ebay", "environment": payload["environment"]}, {"userId": payload["userId"], "marketplace": "ebay", "environment": payload["environment"], "accessTokenEncrypted": encrypt(token["access_token"]), "refreshTokenEncrypted": encrypt(token["refresh_token"]) if token.get("refresh_token") else None, "accessTokenExpiresAt": now_utc() + timedelta(seconds=token.get("expires_in", 0)), "refreshTokenExpiresAt": now_utc() + timedelta(seconds=token.get("refresh_token_expires_in", 0)) if token.get("refresh_token_expires_in") else None, "scopes": token.get("scope", " ".join(ebay_scopes())).split(), "displayName": "eBay Seller", "status": "connected", "lastCheckedAt": now_utc()})
    redirect = frontend_redirect({"marketplace": "ebay", "status": "connected"})
    if redirect:
        return RedirectResponse(redirect)
    return {"success": True, "message": "eBay seller account connected successfully", "data": {"marketplace": "ebay", "environment": payload["environment"], "status": "connected"}}


@ebay_router.get("/status")
async def ebay_status(user=Depends(current_user)):
    conn = await get_connection(user["userId"], "ebay", ebay_env())
    return success("eBay connection status fetched successfully", serialize({"connected": bool(conn and conn.get("status") == "connected" and conn.get("accessTokenEncrypted")), "marketplace": "ebay", "environment": (conn or {}).get("environment", ebay_env()), "displayName": (conn or {}).get("displayName", "eBay Seller"), "providerAccountRef": (conn or {}).get("providerAccountRef", ""), "scopes": (conn or {}).get("scopes", []), "connectedAt": (conn or {}).get("connectedAt"), "accessTokenExpiresAt": (conn or {}).get("accessTokenExpiresAt"), "status": (conn or {}).get("status", "not_connected")}))


@ebay_router.post("/disconnect")
async def ebay_disconnect(user=Depends(current_user)):
    return success("eBay seller account disconnected successfully", await safe_connection(await mark_disconnected(user["userId"], "ebay", ebay_env())))


@ebay_router.get("/inventory")
async def ebay_inventory(user=Depends(current_user)):
    rows = []
    async for p in collection("products").find({"userId": user["userId"], "$or": [{"ebayInventoryItemId": {"$type": "string"}}, {"ebaySku": {"$type": "string"}}]}).sort("updatedAt", -1):
        rows.append({"productId": str(p["_id"]), "productAiProductId": p.get("productAiProductId"), "title": p.get("title"), "sku": p.get("ebaySku") or p.get("ebayInventoryItemId") or (p.get("variants") or [{}])[0].get("sku", ""), "inventoryItemId": p.get("ebayInventoryItemId") or p.get("ebaySku", ""), "offerId": p.get("ebayOfferId"), "listingId": p.get("ebayListingId"), "status": p.get("ebayStatus", "draft"), "quantity": max((p.get("variants") or [{}])[0].get("inventoryQuantity") or p.get("totalInventory") or 0, 0), "featuredImage": p.get("featuredImage"), "updatedAt": p.get("updatedAt")})
    return success("eBay inventory fetched successfully", serialize(rows))


async def enqueue(user_id: str, operation: str, product_id: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    now = now_utc()
    row = {"userId": user_id, "operation": operation, "productId": product_id, "payload": payload, "status": "pending", "attempts": 0, "maxAttempts": 3, "availableAt": now, "createdAt": now, "updatedAt": now}
    result = await collection("ebaysyncjobs").insert_one(row)
    return serialize(row | {"_id": result.inserted_id})


async def ebay_product(user_id: str, product_id: str) -> dict[str, Any]:
    product = await collection("products").find_one({"_id": oid(product_id), "userId": user_id})
    if not product:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Product not found.")
    return product


def ebay_primary_variant(product: dict[str, Any]) -> dict[str, Any]:
    variants = product.get("variants") or []
    if product.get("ebaySku"):
        return next((variant for variant in variants if variant.get("sku") == product["ebaySku"]), variants[0] if variants else {})
    return next((variant for variant in variants if variant.get("sku")), variants[0] if variants else {})


def ebay_sku(product: dict[str, Any]) -> str:
    variant = ebay_primary_variant(product)
    return product.get("ebaySku") or variant.get("sku") or product.get("shopifyProductId") or str(product["_id"])


def ebay_price(product: dict[str, Any]) -> str:
    return str(ebay_primary_variant(product).get("price") or "0.00")


def ebay_quantity(product: dict[str, Any]) -> int:
    variant_qty = ebay_primary_variant(product).get("inventoryQuantity")
    return max(int(variant_qty if variant_qty is not None else product.get("totalInventory") or 0), 0)


def ebay_images(product: dict[str, Any]) -> list[str]:
    values = [product.get("featuredImage"), *(product.get("images") or [])]
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value.startswith(("http://", "https://"))))


def ebay_description(product: dict[str, Any]) -> str:
    description = (product.get("ebayDescriptionOverride") or product.get("description") or "No description provided.").strip()
    notes = (product.get("ebayListingNotes") or "").strip()
    return (description + (f"\n\n{notes}" if notes else ""))[:5000]


def ebay_condition(product: dict[str, Any]) -> dict[str, str]:
    value = str(product.get("ebayCondition") or ebay_commerce_config()["condition"] or "NEW").strip().upper().replace("-", "_").replace(" ", "_")
    mapped = {
        "NEW": ("NEW", "1000"),
        "1000": ("NEW", "1000"),
        "NEW_OTHER": ("NEW_OTHER", "1500"),
        "1500": ("NEW_OTHER", "1500"),
        "NEW_WITH_DEFECTS": ("NEW_WITH_DEFECTS", "1750"),
        "1750": ("NEW_WITH_DEFECTS", "1750"),
        "LIKE_NEW": ("LIKE_NEW", "2750"),
        "2750": ("LIKE_NEW", "2750"),
        "USED": ("USED_EXCELLENT", "3000"),
        "USED_EXCELLENT": ("USED_EXCELLENT", "3000"),
        "3000": ("USED_EXCELLENT", "3000"),
        "USED_VERY_GOOD": ("USED_VERY_GOOD", "4000"),
        "4000": ("USED_VERY_GOOD", "4000"),
        "USED_GOOD": ("USED_GOOD", "5000"),
        "5000": ("USED_GOOD", "5000"),
        "USED_ACCEPTABLE": ("USED_ACCEPTABLE", "6000"),
        "6000": ("USED_ACCEPTABLE", "6000"),
        "FOR_PARTS_OR_NOT_WORKING": ("FOR_PARTS_OR_NOT_WORKING", "7000"),
        "7000": ("FOR_PARTS_OR_NOT_WORKING", "7000"),
    }.get(value)
    if not mapped:
        raise ApiError(status.HTTP_400_BAD_REQUEST, f'Unsupported eBay inventory condition "{product.get("ebayCondition")}". Use NEW/1000, USED/3000, or a valid eBay Inventory condition enum.')
    return {"conditionEnum": mapped[0], "conditionId": mapped[1]}


def ebay_aspects(product: dict[str, Any]) -> dict[str, list[str]] | None:
    specifics = product.get("ebayItemSpecifics") or {}
    source = specifics if isinstance(specifics, dict) and specifics else {}
    if not source:
        source = {key: value for key, value in {"Brand": product.get("vendor"), "Type": product.get("productType"), "Tags": product.get("tags")}.items() if value}
    aspects: dict[str, list[str]] = {}
    for key, value in source.items():
        values = value if isinstance(value, list) else str(value).split(",")
        cleaned = [str(item).strip()[:65] for item in values if str(item).strip()]
        if cleaned:
            aspects[str(key).strip()[:40]] = list(dict.fromkeys(cleaned))
    return aspects or None


def ebay_inventory_payload(product: dict[str, Any]) -> dict[str, Any]:
    condition = ebay_condition(product)
    payload = {
        "availability": {"shipToLocationAvailability": {"quantity": ebay_quantity(product)}},
        "condition": condition["conditionEnum"],
        "product": {
            "title": product.get("title"),
            "description": ebay_description(product),
            "imageUrls": ebay_images(product),
            "aspects": ebay_aspects(product),
        },
    }
    if condition["conditionEnum"] not in {"NEW", "LIKE_NEW", "NEW_OTHER", "NEW_WITH_DEFECTS"}:
        payload["conditionDescription"] = ebay_description(product)[:1000]
    return payload


def ebay_offer_payload(product: dict[str, Any]) -> dict[str, Any]:
    cfg = ebay_commerce_config()
    marketplace_id = product.get("ebayMarketplaceId") or cfg["marketplaceId"]
    currency = (product.get("ebayCurrency") or "").upper() or ebay_marketplace_currency(marketplace_id)
    if currency != ebay_marketplace_currency(marketplace_id):
        currency = ebay_marketplace_currency(marketplace_id)
    return {
        "sku": ebay_sku(product),
        "marketplaceId": marketplace_id,
        "format": cfg["listingFormat"],
        "availableQuantity": ebay_quantity(product),
        "categoryId": product.get("ebayCategoryId") or cfg["categoryId"],
        "merchantLocationKey": cfg["merchantLocationKey"],
        "listingDescription": ebay_description(product),
        "listingPolicies": {
            "paymentPolicyId": cfg["paymentPolicyId"],
            "returnPolicyId": cfg["returnPolicyId"],
            "fulfillmentPolicyId": cfg["fulfillmentPolicyId"],
        },
        "pricingSummary": {"price": {"value": ebay_price(product), "currency": currency}},
        "listingDuration": cfg["listingDuration"],
    }


async def ebay_create_inventory_item(user_id: str, product_id: str) -> dict[str, Any]:
    product = await ebay_product(user_id, product_id)
    sku = ebay_sku(product)
    payload = ebay_inventory_payload(product)
    result = await ebay_request(user_id, "PUT", f"/sell/inventory/v1/inventory_item/{quote(sku, safe='')}", "create_inventory_item", payload)
    await collection("products").update_one(
        {"_id": product["_id"]},
        {"$set": {"ebaySku": sku, "ebayInventoryItemId": sku, "lastEbaySyncAt": now_utc(), "rawEbayPayload.inventoryItem": payload, "updatedAt": now_utc()}},
    )
    return result | {"data": {"sku": sku, "inventoryItemId": sku}}


async def ebay_update_quantity(user_id: str, product_id: str, quantity: int) -> dict[str, Any]:
    product = await ebay_product(user_id, product_id)
    sku = product.get("ebayInventoryItemId") or product.get("ebaySku")
    if not sku:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "eBay inventory SKU is missing for this product.")
    normalized = max(int(quantity), 0)
    request_payload = {"sku": sku, "shipToLocationAvailability": {"quantity": normalized}}
    if product.get("ebayOfferId"):
        request_payload["offers"] = [{"offerId": product["ebayOfferId"], "availableQuantity": normalized}]
    result = await ebay_request(user_id, "POST", "/sell/inventory/v1/bulk_update_price_quantity", "update_inventory_quantity", {"requests": [request_payload]})
    await collection("products").update_one({"_id": product["_id"]}, {"$set": {"lastEbaySyncAt": now_utc(), "rawEbayPayload.quantityUpdate": {"request": request_payload, "response": result.get("data")}, "updatedAt": now_utc()}})
    return result | {"data": {"sku": sku, "offerId": product.get("ebayOfferId"), "quantity": normalized}}


async def ebay_create_or_update_offer(user_id: str, product_id: str) -> dict[str, Any]:
    product = await ebay_product(user_id, product_id)
    payload = ebay_offer_payload(product)
    if product.get("ebayOfferId"):
        result = await ebay_request(user_id, "PUT", f"/sell/inventory/v1/offer/{quote(product['ebayOfferId'], safe='')}", "update_offer", payload)
        offer_id = product["ebayOfferId"]
    else:
        result = await ebay_request(user_id, "POST", "/sell/inventory/v1/offer", "create_offer", payload)
        offer_id = (result.get("data") or {}).get("offerId")
        if not offer_id:
            raise ApiError(status.HTTP_502_BAD_GATEWAY, "eBay create offer response did not include offerId.")
    await collection("products").update_one({"_id": product["_id"]}, {"$set": {"ebaySku": payload["sku"], "ebayOfferId": offer_id, "ebayMarketplaceId": payload["marketplaceId"], "ebayStatus": product.get("ebayStatus") or "draft", "lastEbaySyncAt": now_utc(), "rawEbayPayload.offer": result.get("data") or payload, "updatedAt": now_utc()}})
    return result | {"data": {"offerId": offer_id, "sku": payload["sku"]}}


async def ebay_sync_product_record(user_id: str, product_id: str) -> dict[str, Any]:
    await ebay_create_inventory_item(user_id, product_id)
    return await ebay_create_or_update_offer(user_id, product_id)


async def ebay_publish_offer(user_id: str, product_id: str) -> dict[str, Any]:
    await ebay_sync_product_record(user_id, product_id)
    product = await ebay_product(user_id, product_id)
    if not product.get("ebayOfferId"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "eBay offer is missing for this product.")
    result = await ebay_request(user_id, "POST", f"/sell/inventory/v1/offer/{quote(product['ebayOfferId'], safe='')}/publish", "publish_offer")
    listing_id = (result.get("data") or {}).get("listingId") or product.get("ebayListingId")
    await collection("products").update_one({"_id": product["_id"]}, {"$set": {"ebayListingId": listing_id, "ebayStatus": "active", "lastEbaySyncAt": now_utc(), "rawEbayPayload.publish": result.get("data"), "updatedAt": now_utc()}})
    return result | {"data": {"offerId": product["ebayOfferId"], "listingId": listing_id, "sku": product.get("ebaySku")}}


async def ebay_end_listing_operation(user_id: str, product_id: str) -> dict[str, Any]:
    product = await ebay_product(user_id, product_id)
    if not product.get("ebayOfferId"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "eBay offer is missing for this product.")
    result = await ebay_request(user_id, "POST", f"/sell/inventory/v1/offer/{quote(product['ebayOfferId'], safe='')}/withdraw", "withdraw_offer")
    await collection("products").update_one({"_id": product["_id"]}, {"$set": {"ebayStatus": "ended", "lastEbaySyncAt": now_utc(), "rawEbayPayload.withdraw": {"offerId": product["ebayOfferId"]}, "updatedAt": now_utc()}})
    return result | {"data": {"offerId": product["ebayOfferId"], "listingId": product.get("ebayListingId"), "status": "ended"}}


@ebay_router.patch("/products/{productId}/inventory")
@ebay_router.patch("/inventory/{productId}")
async def ebay_update_inventory(productId: str, body: EbayInventoryBody, user=Depends(current_user)):
    product = await collection("products").find_one({"_id": oid(productId), "userId": user["userId"]})
    if not product:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Product not found.")
    variants = product.get("variants") or [{"title": "Default Title", "sku": product.get("ebaySku") or product.get("ebayInventoryItemId")}]
    variants[0]["inventoryQuantity"] = body.quantity
    await collection("products").update_one({"_id": product["_id"]}, {"$set": {"variants": variants, "totalInventory": body.quantity, "updatedAt": now_utc()}})
    data = await ebay_update_quantity(user["userId"], productId, body.quantity)
    return success("eBay inventory updated successfully", data.get("data"))


@ebay_router.post("/products/{productId}/sync", status_code=202)
async def ebay_sync_product(productId: str, user=Depends(current_user)):
    return success("eBay product sync queued successfully", await enqueue(user["userId"], "sync_product", productId))


@ebay_router.post("/products/{productId}/sync-inventory", status_code=202)
async def ebay_sync_inventory(productId: str, user=Depends(current_user)):
    return success("eBay inventory sync queued successfully", await enqueue(user["userId"], "sync_inventory", productId))


@ebay_router.post("/products/{productId}/sync-price", status_code=202)
async def ebay_sync_price(productId: str, user=Depends(current_user)):
    return success("eBay price sync queued successfully", await enqueue(user["userId"], "sync_price", productId))


@ebay_router.post("/products/{productId}/publish", status_code=202)
async def ebay_publish(productId: str, user=Depends(current_user)):
    await validate_ebay_publish_readiness(user["userId"], productId)
    return success("eBay publish job queued successfully", await enqueue(user["userId"], "publish_listing", productId))


@ebay_router.post("/products/{productId}/end", status_code=202)
async def ebay_end(productId: str, user=Depends(current_user)):
    return success("eBay end listing job queued successfully", await enqueue(user["userId"], "end_listing", productId))


@ebay_router.get("/products/{productId}/status")
async def ebay_listing_status(productId: str, user=Depends(current_user)):
    p = await collection("products").find_one({"_id": oid(productId), "userId": user["userId"]})
    if not p:
        raise ApiError(status.HTTP_404_NOT_FOUND, "Product not found.")
    if not p.get("ebayOfferId"):
        return success("eBay listing status fetched successfully", {"success": True, "marketplace": "ebay", "operation": "get_listing_status", "statusCode": 200, "retryable": False, "data": {"productId": productId, "sku": p.get("ebaySku"), "listingId": p.get("ebayListingId"), "offerId": p.get("ebayOfferId"), "status": p.get("ebayStatus", "draft")}})
    try:
        offer = await ebay_request(user["userId"], "GET", f"/sell/inventory/v1/offer/{quote(p['ebayOfferId'], safe='')}", "get_offer")
        offer["operation"] = "get_listing_status"
        offer["data"] = {"productId": productId, "sku": p.get("ebaySku"), "listingId": p.get("ebayListingId"), "offerId": p.get("ebayOfferId"), "status": p.get("ebayStatus", "draft"), "offer": offer.get("data")}
        return success("eBay listing status fetched successfully", offer)
    except ApiError as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return success("eBay listing status fetched successfully", {"success": True, "marketplace": "ebay", "operation": "get_listing_status", "statusCode": 200, "retryable": False, "data": {"productId": productId, "sku": p.get("ebaySku"), "listingId": p.get("ebayListingId"), "offerId": p.get("ebayOfferId"), "status": "not_found", "offer": None, "syncError": "This eBay offer is no longer available."}})
        raise


@ebay_router.post("/listings/pull", status_code=202)
async def ebay_pull(body: EbayPullListingsBody, user=Depends(current_user)):
    return success("eBay listing pull job queued successfully", await enqueue(user["userId"], "pull_listings", payload=body.model_dump(exclude_none=True)))


@ebay_router.get("/jobs/{jobId}")
async def ebay_job(jobId: str, user=Depends(current_user)):
    row = await collection("ebaysyncjobs").find_one({"_id": oid(jobId), "userId": user["userId"]})
    if not row:
        raise ApiError(status.HTTP_404_NOT_FOUND, "eBay sync job not found.")
    return success("eBay sync job fetched successfully", serialize(row))


async def validate_ebay_publish_readiness(user_id: str, product_id: str) -> dict[str, Any]:
    product = await ebay_product(user_id, product_id)
    conn = await get_connection(user_id, "ebay", ebay_env())
    cfg = ebay_commerce_config()
    missing = []
    if not conn or conn.get("status") != "connected" or not conn.get("accessTokenEncrypted"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Reconnect eBay with inventory permissions.")
    if not any("sell.inventory" in scope for scope in conn.get("scopes", [])):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Reconnect eBay with inventory permissions.")
    if not (product.get("title") or "").strip():
        missing.append("title")
    if not ebay_description(product).strip():
        missing.append("description")
    if not ebay_sku(product).strip():
        missing.append("sku")
    try:
        if float(ebay_price(product)) < 0:
            missing.append("price")
    except ValueError:
        missing.append("price")
    if ebay_quantity(product) < 0:
        missing.append("inventory quantity")
    if not ebay_images(product):
        missing.append("public image URL")
    if not product.get("ebayCategoryId") and not cfg.get("categoryId"):
        missing.append("categoryId")
    if not cfg.get("merchantLocationKey"):
        missing.append("merchant location key")
    if not cfg.get("paymentPolicyId"):
        missing.append("payment policy ID")
    if not cfg.get("fulfillmentPolicyId"):
        missing.append("fulfillment policy ID")
    if not cfg.get("returnPolicyId"):
        missing.append("return policy ID")
    if missing:
        raise ApiError(status.HTTP_400_BAD_REQUEST, f"eBay sandbox publishing is missing configuration: {', '.join(missing)}.")
    return product


async def ebay_process_job(job: dict[str, Any]) -> dict[str, Any]:
    operation = job.get("operation")
    user_id = job["userId"]
    product_id = job.get("productId")
    if operation == "sync_product":
        return await ebay_sync_product_record(user_id, product_id)
    if operation == "sync_inventory":
        return await ebay_create_inventory_item(user_id, product_id)
    if operation == "sync_price":
        return await ebay_create_or_update_offer(user_id, product_id)
    if operation == "publish_listing":
        return await ebay_publish_offer(user_id, product_id)
    if operation == "end_listing":
        return await ebay_end_listing_operation(user_id, product_id)
    if operation == "pull_listings":
        cfg = ebay_commerce_config()
        payload = job.get("payload") or {}
        return await ebay_request(user_id, "GET", "/sell/inventory/v1/offer", "pull_ebay_listings", query={"limit": int(payload.get("limit") or 50), "offset": int(payload.get("offset") or 0), "marketplace_id": cfg["marketplaceId"]})
    raise ApiError(status.HTTP_400_BAD_REQUEST, f"Unsupported eBay sync operation: {operation}")


def ebay_retryable(exc: Exception) -> bool:
    if isinstance(exc, ApiError):
        details = exc.details if isinstance(exc.details, dict) else {}
        if "retryable" in details:
            return bool(details["retryable"])
        return exc.status_code >= 500 or exc.status_code == 429
    return True


async def worker_loop() -> None:
    owner_id = worker_owner_id or runtime_worker_owner_id()
    lease_seconds = max(get_settings().EBAY_WORKER_LEASE_SECONDS, 5)
    poll_seconds = max(get_settings().EBAY_WORKER_POLL_SECONDS, 0.1)
    while True:
        await asyncio.sleep(poll_seconds)
        if not await acquire_worker_lease(owner_id, lease_seconds):
            continue
        job = await collection("ebaysyncjobs").find_one_and_update(
            {"status": "pending", "availableAt": {"$lte": now_utc()}},
            {"$set": {"status": "processing", "startedAt": now_utc(), "updatedAt": now_utc()}, "$inc": {"attempts": 1}},
            sort=[("createdAt", 1)],
            return_document=True,
        )
        if not job:
            continue
        try:
            result = await ebay_process_job(job)
            stored_result = {"data": result.get("data")} if result.get("data") is not None else {"success": result.get("success", True)}
            await collection("ebaysyncjobs").update_one({"_id": job["_id"]}, {"$set": {"status": "completed", "completedAt": now_utc(), "result": stored_result, "updatedAt": now_utc()}})
            await collection("sync_logs").insert_one({"userId": job["userId"], "marketplace": "ebay", "entityType": "product", "entityId": job.get("productId"), "action": job.get("operation"), "status": "success", "responsePayload": stored_result, "createdAt": now_utc()})
        except Exception as exc:  # noqa: BLE001 - workers must record unexpected job failures instead of dying.
            retryable = ebay_retryable(exc)
            should_retry = retryable and job.get("attempts", 0) < job.get("maxAttempts", 3)
            last_error = str(exc)
            result = {"success": False, "error": last_error, "retryable": retryable}
            await collection("ebaysyncjobs").update_one(
                {"_id": job["_id"]},
                {
                    "$set": {
                        "status": "pending" if should_retry else "failed",
                        "availableAt": now_utc() + timedelta(seconds=min(2 ** max(job.get("attempts", 1), 1), 30)) if should_retry else job.get("availableAt"),
                        "lastError": last_error,
                        "result": result,
                        "updatedAt": now_utc(),
                    }
                },
            )
            await collection("sync_logs").insert_one({"userId": job["userId"], "marketplace": "ebay", "entityType": "product", "entityId": job.get("productId"), "action": job.get("operation"), "status": "pending" if should_retry else "failed", "errorMessage": last_error, "responsePayload": result, "createdAt": now_utc()})


async def acquire_worker_lease(owner_id: str, lease_seconds: int | None = None) -> bool:
    now = now_utc()
    lease_until = now + timedelta(seconds=lease_seconds or get_settings().EBAY_WORKER_LEASE_SECONDS)
    try:
        row = await collection("worker_leases").find_one_and_update(
            {
                "_id": "ebay-sync-worker",
                "$or": [{"ownerId": owner_id}, {"expiresAt": {"$lte": now}}, {"expiresAt": {"$exists": False}}],
            },
            {
                "$set": {
                    "ownerId": owner_id,
                    "heartbeatAt": now,
                    "expiresAt": lease_until,
                    "updatedAt": now,
                },
                "$setOnInsert": {"createdAt": now},
            },
            upsert=True,
            return_document=True,
        )
    except DuplicateKeyError:
        return False
    return bool(row and row.get("ownerId") == owner_id)


def start_worker() -> None:
    global worker_owner_id, worker_task
    if not get_settings().EBAY_WORKER_ENABLED:
        return
    if worker_task is None:
        worker_owner_id = runtime_worker_owner_id()
        worker_task = asyncio.create_task(worker_loop())


def stop_worker() -> None:
    global worker_owner_id, worker_task
    if worker_task is not None:
        worker_task.cancel()
        worker_task = None
    worker_owner_id = None


def etsy_config() -> dict[str, str | None]:
    s = get_settings()
    return {"clientId": normalize(s.ETSY_CLIENT_ID), "redirectUri": str(s.ETSY_REDIRECT_URI) if s.ETSY_REDIRECT_URI else None, "authUrl": str(s.ETSY_AUTH_URL or "https://www.etsy.com/oauth/connect"), "tokenUrl": str(s.ETSY_TOKEN_URL or "https://api.etsy.com/v3/public/oauth/token"), "apiBaseUrl": str(s.ETSY_API_BASE_URL or "https://api.etsy.com/v3/application"), "sharedSecret": normalize(s.ETSY_SHARED_SECRET)}


@etsy_router.get("/connect-url")
async def etsy_connect_url(user=Depends(current_user)):
    cfg = etsy_config()
    if not cfg["clientId"] or not cfg["redirectUri"]:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Etsy OAuth credentials are not configured.")
    verifier = b64url(os.urandom(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = os.urandom(32).hex()
    now = now_utc()
    await collection("etsyoauthstates").insert_one({"state": state, "userId": user["userId"], "codeVerifier": verifier, "marketplace": "etsy", "environment": "production", "expiresAt": now + timedelta(minutes=10), "consumed": False, "createdAt": now, "updatedAt": now})
    params = {"client_id": cfg["clientId"], "redirect_uri": cfg["redirectUri"], "response_type": "code", "scope": normalize(get_settings().ETSY_SCOPES) or "shops_r", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return success("Etsy connect URL generated successfully", {"connectUrl": f"{cfg['authUrl']}?{urlencode(params)}"})


@etsy_router.get("/callback")
async def etsy_callback(request: Request):
    cfg = etsy_config()
    state, code = request.query_params.get("state"), request.query_params.get("code")
    if not state:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Missing Etsy OAuth state.")
    oauth = await collection("etsyoauthstates").find_one_and_update({"state": state, "marketplace": "etsy", "consumed": False, "expiresAt": {"$gt": now_utc()}}, {"$set": {"consumed": True, "consumedAt": now_utc(), "updatedAt": now_utc()}}, return_document=True)
    if not oauth:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Etsy OAuth state is invalid, expired, or already used.")
    if request.query_params.get("error"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, request.query_params.get("error_description") or request.query_params["error"])
    if not code:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Missing required Etsy OAuth callback code.")
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(cfg["tokenUrl"], headers={"Content-Type": "application/x-www-form-urlencoded"}, data={"grant_type": "authorization_code", "client_id": cfg["clientId"], "redirect_uri": cfg["redirectUri"], "code": code, "code_verifier": oauth["codeVerifier"]})
    token = res.json()
    if res.status_code >= 400 or not token.get("access_token") or not token.get("refresh_token"):
        raise ApiError(status.HTTP_502_BAD_GATEWAY, token.get("error_description") or "Failed to exchange Etsy OAuth code for tokens.")
    user_prefix = token["access_token"].split(".")[0]
    await upsert_connection({"userId": oauth["userId"], "marketplace": "etsy", "environment": "production"}, {"userId": oauth["userId"], "marketplace": "etsy", "environment": "production", "accessTokenEncrypted": encrypt(token["access_token"]), "refreshTokenEncrypted": encrypt(token["refresh_token"]), "accessTokenExpiresAt": now_utc() + timedelta(seconds=token.get("expires_in", 0)), "refreshTokenExpiresAt": now_utc() + timedelta(days=90), "scopes": token.get("scope", normalize(get_settings().ETSY_SCOPES) or "shops_r").split(), "providerAccountRef": user_prefix, "displayName": "Etsy Shop", "status": "connected", "lastCheckedAt": now_utc(), "metadata": {"etsyUserId": user_prefix}})
    redirect = frontend_redirect({"marketplace": "etsy", "status": "connected"})
    if redirect:
        return RedirectResponse(redirect)
    return {"success": True, "message": "Etsy shop connected successfully", "data": {"marketplace": "etsy", "environment": "production", "status": "connected"}}


@etsy_router.get("/status")
async def etsy_status(user=Depends(current_user)):
    conn = await get_connection(user["userId"], "etsy", "production")
    return success("Etsy connection status fetched successfully", serialize({"connected": bool(conn and conn.get("status") == "connected" and conn.get("accessTokenEncrypted")), "marketplace": "etsy", "environment": "production", "displayName": (conn or {}).get("displayName", "Etsy Shop"), "providerAccountRef": (conn or {}).get("providerAccountRef", ""), "scopes": (conn or {}).get("scopes", []), "connectedAt": (conn or {}).get("connectedAt"), "accessTokenExpiresAt": (conn or {}).get("accessTokenExpiresAt"), "status": (conn or {}).get("status", "not_connected")}))


@etsy_router.post("/disconnect")
async def etsy_disconnect(user=Depends(current_user)):
    return success("Etsy shop disconnected successfully", await safe_connection(await mark_disconnected(user["userId"], "etsy", "production")))
