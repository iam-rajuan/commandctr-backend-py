from __future__ import annotations

from datetime import timedelta
import json
import re
from typing import Any
from urllib.parse import quote

import httpx
from starlette import status

from app.config import get_settings
from app.crypto_utils import decrypt, encrypt
from app.db import collection, now_utc, serialize
from app.errors import ApiError
from app.modules.amazon.schema import AmazonListingBody, AmazonListingUpdateBody

SANDBOX_SELLER_ID = "AXXXXXXXXXXXX"
CANONICAL_PRODUCT_TYPE = re.compile(r"^[A-Z0-9_]+$")


def amazon_config() -> dict[str, str]:
    settings = get_settings()
    environment = settings.AMAZON_SP_API_ENVIRONMENT or "sandbox"
    values = {
        "environment": environment,
        "endpoint": str(settings.AMAZON_SP_API_ENDPOINT or "").rstrip("/"),
        "marketplaceId": settings.AMAZON_MARKETPLACE_ID or "",
        "currency": settings.AMAZON_CURRENCY or "GBP",
        "sellerId": settings.AMAZON_SELLER_ID or (SANDBOX_SELLER_ID if environment == "sandbox" else ""),
        "clientId": settings.AMAZON_LWA_CLIENT_ID or "",
        "clientSecret": settings.AMAZON_LWA_CLIENT_SECRET or "",
        "refreshToken": settings.AMAZON_LWA_REFRESH_TOKEN or "",
    }
    missing = [
        key
        for key in ("endpoint", "marketplaceId", "sellerId", "clientId", "clientSecret", "refreshToken")
        if not values[key]
    ]
    if missing:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Amazon SP-API credentials are not configured.", {"missing": missing})
    return values


def listing_attributes(body: AmazonListingBody, config: dict[str, str]) -> dict[str, Any]:
    marketplace_id = config["marketplaceId"]
    def localized(value: str) -> dict[str, str]:
        return {
            "value": value,
            "language_tag": "en_GB",
            "marketplace_id": marketplace_id,
        }
    attributes: dict[str, Any] = {
        "item_name": [localized(body.title)],
        "condition_type": [{"value": "new_new", "marketplace_id": marketplace_id}],
        "list_price": [
            {
                "currency": config["currency"],
                "value_with_tax": float(body.price),
                "marketplace_id": marketplace_id,
            }
        ],
        "purchasable_offer": [
            {
                "marketplace_id": marketplace_id,
                "currency": config["currency"],
                "our_price": [{"schedule": [{"value_with_tax": float(body.price)}]}],
            }
        ],
        "fulfillment_availability": [
            {"fulfillment_channel_code": "DEFAULT", "quantity": body.quantity}
        ],
    }
    if body.description:
        attributes["product_description"] = [localized(body.description)]
    if body.brand:
        attributes["brand"] = [localized(body.brand)]
    if body.bulletPoints:
        attributes["bullet_point"] = [
            localized(value) for value in body.bulletPoints if value.strip()
        ]
    if body.backendSearchTerms:
        attributes["generic_keyword"] = [
            localized(" ".join(term.strip() for term in body.backendSearchTerms if term.strip()))
        ]
    if body.imagePath and body.imagePath.lower().startswith(("https://", "http://")):
        attributes["main_product_image_locator"] = [
            {"media_location": body.imagePath, "marketplace_id": marketplace_id}
        ]
    for key, value in body.attributes.items():
        if body.identifierMode == "no_identifier" and key in {"externally_assigned_product_identifier", "merchant_suggested_asin"}:
            continue
        if key in {"category", "product_type"}:
            continue
        if isinstance(value, str) and value.strip().startswith(("[", "{")):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, (list, dict)):
                attributes[key] = parsed
                continue
        if isinstance(value, (str, int, float, bool)):
            attributes[key] = [{"value": value, "marketplace_id": marketplace_id}]
        else:
            attributes[key] = value
    return attributes


def validate_identifier_path(body: AmazonListingBody) -> None:
    identifier = body.attributes.get("externally_assigned_product_identifier")
    if body.identifierMode == "gtin" and not identifier:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "A valid EAN, UPC, or GTIN is required for this identifier option.")
    if body.identifierMode == "asin" and not body.existingAsin:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter the existing Amazon ASIN for this identifier option.")
    if body.identifierMode == "no_identifier":
        if identifier or body.existingAsin:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "Barcode-free products must not include a GTIN or ASIN.")
        if not body.exemptionConfirmed or not body.exemptionBrand or not body.exemptionCategory:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "GTIN exemption confirmation is required for the selected brand and product category.",
                {"requiresSellerCentralApproval": True},
            )


async def product_type_definition(user_id: str, product_type: str) -> dict[str, Any]:
    """Read the current UK schema only; it never creates or changes listings."""
    config = amazon_config()
    token = await connected_access_token(user_id, config)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{config['endpoint']}/definitions/2020-09-01/productTypes/{quote(product_type, safe='')}",
            params={"marketplaceIds": config["marketplaceId"], "requirements": "LISTING", "locale": "en_GB"},
            headers={"x-amz-access-token": token, "user-agent": "CommandCtr/0.1 (Language=Python)"},
        )
    if response.status_code >= 400:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Amazon could not load the Product Type Definition.", {"statusCode": response.status_code})
    return response.json()


async def connected_access_token(user_id: str, config: dict[str, str]) -> str:
    connection = await collection("marketplaceconnections").find_one(
        {"userId": user_id, "marketplace": "amazon", "environment": config["environment"]},
        sort=[("updatedAt", -1)],
    )
    if not connection or connection.get("status") != "connected" or not connection.get("refreshTokenEncrypted"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, f"Connect Amazon {config['environment']} before publishing a product.")
    token = await exchange_access_token(decrypt(connection["refreshTokenEncrypted"]), config)
    return str(token["access_token"])


async def resolve_product_type(user_id: str, body: AmazonListingBody) -> AmazonListingBody:
    requested = body.productType.strip()
    if CANONICAL_PRODUCT_TYPE.fullmatch(requested):
        return body
    config = amazon_config()
    access_token = await connected_access_token(user_id, config)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{config['endpoint']}/definitions/2020-09-01/productTypes",
                params={
                    "marketplaceIds": config["marketplaceId"],
                    "itemName": body.title,
                    "locale": "en_GB",
                },
                headers={
                    "x-amz-access-token": access_token,
                    "user-agent": "CommandCtr/0.1 (Language=Python)",
                },
            )
    except httpx.HTTPError as exc:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to resolve the Amazon product type.") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    product_types = payload.get("productTypes") if isinstance(payload, dict) else None
    if response.status_code >= 400 or not isinstance(product_types, list) or not product_types:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Amazon could not resolve this product to a supported product type.",
            {"requestedProductType": requested},
        )
    resolved = str(product_types[0].get("name") or "").strip()
    if not resolved:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "Amazon returned an empty product type recommendation.")
    return AmazonListingBody(**(body.model_dump() | {"productType": resolved}))


async def listings_request(
    user_id: str,
    method: str,
    sku: str,
    *,
    body: dict[str, Any] | None = None,
    validation_preview: bool = False,
) -> dict[str, Any]:
    config = amazon_config()
    access_token = await connected_access_token(user_id, config)
    url = f"{config['endpoint']}/listings/2021-08-01/items/{quote(config['sellerId'], safe='')}/{quote(sku, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            params = {"marketplaceIds": config["marketplaceId"], "issueLocale": "en_GB"}
            if validation_preview:
                params["mode"] = "VALIDATION_PREVIEW"
            response = await client.request(
                method,
                url,
                params=params,
                headers={
                    "Content-Type": "application/json",
                    "x-amz-access-token": access_token,
                    "x-amz-date": now_utc().strftime("%Y%m%dT%H%M%SZ"),
                    "user-agent": "CommandCtr/0.1 (Language=Python)",
                },
                json=body,
            )
    except httpx.HTTPError as exc:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to communicate with Amazon SP-API.") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        message = "Amazon SP-API rejected the listing request."
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors and isinstance(errors, list):
            message = errors[0].get("message") or message
        raise ApiError(status.HTTP_502_BAD_GATEWAY, message, {"statusCode": response.status_code, "response": payload})
    return payload if isinstance(payload, dict) else {}


def listing_submission(body: AmazonListingBody, config: dict[str, str]) -> dict[str, Any]:
    return {
        "productType": body.productType,
        "requirements": "LISTING",
        "attributes": listing_attributes(body, config),
    }


async def validate_listing(user_id: str, body: AmazonListingBody) -> dict[str, Any]:
    validate_identifier_path(body)
    config = amazon_config()
    if config["environment"] != "production":
        return {"status": "ACCEPTED", "issues": []}
    preview = await listings_request(
        user_id,
        "PUT",
        body.sku,
        body=listing_submission(body, config),
        validation_preview=True,
    )
    issues = preview.get("issues") or []
    if preview.get("status") == "INVALID" or any(issue.get("severity") == "ERROR" for issue in issues):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Amazon rejected the listing validation preview. Correct the reported attributes before publishing.",
            {"status": preview.get("status", "INVALID"), "issues": issues},
        )
    return preview


def local_listing_document(body: AmazonListingBody, response: dict[str, Any], config: dict[str, str]) -> dict[str, Any]:
    return {
        "marketplace": "amazon",
        "environment": config["environment"],
        "externalPersistence": config["environment"] == "production",
        "amazonSellerId": config["sellerId"],
        "amazonMarketplaceId": config["marketplaceId"],
        "amazonSku": body.sku,
        "amazonSubmissionId": response.get("submissionId"),
        "amazonStatus": response.get("status", "ACCEPTED"),
        "amazonIssues": response.get("issues") or [],
        "title": body.title,
        "description": body.description,
        "vendor": body.brand,
        "productType": body.productType,
        "status": str(response.get("status") or "ACCEPTED").lower(),
        "totalInventory": body.quantity,
        "featuredImage": body.imagePath,
        "images": [body.imagePath] if body.imagePath else [],
        "variants": [{"title": "Default Title", "sku": body.sku, "price": body.price, "inventoryQuantity": body.quantity}],
        "amazonPayload": {
            "bulletPoints": body.bulletPoints,
            "backendSearchTerms": body.backendSearchTerms,
            "attributes": body.attributes,
        },
        "lastAmazonSyncAt": now_utc(),
    }


async def create_listing(user_id: str, body: AmazonListingBody) -> dict[str, Any]:
    config = amazon_config()
    body = await resolve_product_type(user_id, body)
    existing = await collection("products").find_one(
        {
            "userId": user_id,
            "marketplace": "amazon",
            "environment": config["environment"],
            "amazonSku": body.sku,
        }
    )
    if existing:
        return await update_listing(user_id, str(existing["_id"]), AmazonListingUpdateBody(**body.model_dump()))
    await validate_listing(user_id, body)
    if config["environment"] == "production" and not body.confirmProductionSubmission:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "Validation passed. Confirm the production submission before CommandCtr sends this listing to Amazon.",
            {"validationPassed": True, "requiresConfirmation": True},
        )
    response = await listings_request(
        user_id,
        "PUT",
        body.sku,
        body=listing_submission(body, config),
    )
    now = now_utc()
    document = local_listing_document(body, response, config) | {"userId": user_id, "createdAt": now, "updatedAt": now}
    result = await collection("products").insert_one(document)
    return serialize(await collection("products").find_one({"_id": result.inserted_id}))


async def update_listing(user_id: str, product_id: str, body: AmazonListingUpdateBody) -> dict[str, Any]:
    from app.db import oid

    config = amazon_config()
    body = AmazonListingUpdateBody(**(await resolve_product_type(user_id, body)).model_dump())
    product = await collection("products").find_one(
        {
            "_id": oid(product_id),
            "userId": user_id,
            "marketplace": "amazon",
            "environment": config["environment"],
        }
    )
    if not product:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"Amazon {config['environment']} product not found.")
    old_sku = product.get("amazonSku")
    if old_sku != body.sku:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Amazon SKU cannot be changed. Create a new product instead.")
    await validate_listing(user_id, body)
    response = await listings_request(
        user_id,
        "PUT",
        body.sku,
        body=listing_submission(body, config),
    )
    update = local_listing_document(body, response, config) | {"updatedAt": now_utc()}
    saved = await collection("products").find_one_and_update(
        {"_id": product["_id"], "userId": user_id}, {"$set": update}, return_document=True
    )
    return serialize(saved)


async def delete_listing(user_id: str, product_id: str) -> dict[str, Any]:
    from app.db import oid

    config = amazon_config()
    product = await collection("products").find_one(
        {
            "_id": oid(product_id),
            "userId": user_id,
            "marketplace": "amazon",
            "environment": config["environment"],
        }
    )
    if not product:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"Amazon {config['environment']} product not found.")
    response = await listings_request(user_id, "DELETE", str(product["amazonSku"]))
    await collection("products").delete_one({"_id": product["_id"], "userId": user_id})
    return {"deleted": True, "sku": product["amazonSku"], "amazonResponse": response}


async def exchange_access_token(refresh_token: str, config: dict[str, str]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.amazon.com/auth/o2/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": config["clientId"],
                    "client_secret": config["clientSecret"],
                },
            )
    except httpx.HTTPError as exc:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to communicate with Amazon authentication.") from exc
    payload = response.json()
    if response.status_code >= 400 or not payload.get("access_token"):
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Amazon authentication failed.")
    return payload


async def marketplace_participations(access_token: str, config: dict[str, str]) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{config['endpoint']}/sellers/v1/marketplaceParticipations",
                headers={
                    "x-amz-access-token": access_token,
                    "x-amz-date": now_utc().strftime("%Y%m%dT%H%M%SZ"),
                    "user-agent": "CommandCtr/0.1 (Language=Python)",
                },
            )
    except httpx.HTTPError as exc:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to communicate with Amazon SP-API.") from exc
    if response.status_code >= 400:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Amazon SP-API denied the connection.", {"statusCode": response.status_code})
    return list(response.json().get("payload") or [])


def status_payload(connection: dict[str, Any] | None, config: dict[str, str]) -> dict[str, Any]:
    connected = bool(connection and connection.get("status") == "connected" and connection.get("refreshTokenEncrypted"))
    default_name = "Amazon UK" if config["environment"] == "production" else "Amazon UK Sandbox"
    return serialize(
        {
            "connected": connected,
            "marketplace": "amazon",
            "environment": config["environment"],
            "displayName": (connection or {}).get("displayName", default_name),
            "providerAccountRef": (connection or {}).get("providerAccountRef", config["marketplaceId"]),
            "marketplaceId": config["marketplaceId"],
            "currency": config["currency"],
            "status": (connection or {}).get("status", "not_connected"),
            "connectedAt": (connection or {}).get("connectedAt"),
            "lastCheckedAt": (connection or {}).get("lastCheckedAt"),
        }
    )


async def connect(user_id: str) -> dict[str, Any]:
    config = amazon_config()
    token = await exchange_access_token(config["refreshToken"], config)
    participations = await marketplace_participations(token["access_token"], config)
    now = now_utc()
    connection = await collection("marketplaceconnections").find_one_and_update(
        {"userId": user_id, "marketplace": "amazon", "environment": config["environment"]},
        {
            "$set": {
                "userId": user_id,
                "marketplace": "amazon",
                "environment": config["environment"],
                "displayName": "Amazon UK" if config["environment"] == "production" else "Amazon UK Sandbox",
                "providerAccountRef": config["sellerId"],
                "accessTokenEncrypted": encrypt(token["access_token"]),
                "refreshTokenEncrypted": encrypt(config["refreshToken"]),
                "accessTokenExpiresAt": now + timedelta(seconds=int(token.get("expires_in", 3600))),
                "status": "connected",
                "lastCheckedAt": now,
                "metadata": {
                    "sellerId": config["sellerId"],
                    "marketplaceId": config["marketplaceId"],
                    "currency": config["currency"],
                    "participationCount": len(participations),
                },
                "updatedAt": now,
            },
            "$setOnInsert": {"createdAt": now, "connectedAt": now},
            "$unset": {"disconnectedAt": ""},
        },
        upsert=True,
        return_document=True,
    )
    return status_payload(connection, config)


async def get_status(user_id: str) -> dict[str, Any]:
    config = amazon_config()
    connection = await collection("marketplaceconnections").find_one(
        {"userId": user_id, "marketplace": "amazon", "environment": config["environment"]},
        sort=[("updatedAt", -1)],
    )
    return status_payload(connection, config)


async def verify_connection(user_id: str) -> dict[str, Any]:
    config = amazon_config()
    connection = await collection("marketplaceconnections").find_one(
        {"userId": user_id, "marketplace": "amazon", "environment": config["environment"]},
        sort=[("updatedAt", -1)],
    )
    if not connection or not connection.get("refreshTokenEncrypted") or connection.get("status") != "connected":
        raise ApiError(status.HTTP_400_BAD_REQUEST, f"Amazon {config['environment']} is not connected.")
    token = await exchange_access_token(decrypt(connection["refreshTokenEncrypted"]), config)
    participations = await marketplace_participations(token["access_token"], config)
    now = now_utc()
    await collection("marketplaceconnections").update_one(
        {"_id": connection["_id"]},
        {"$set": {"accessTokenEncrypted": encrypt(token["access_token"]), "accessTokenExpiresAt": now + timedelta(seconds=int(token.get("expires_in", 3600))), "lastCheckedAt": now, "updatedAt": now, "metadata.participationCount": len(participations)}},
    )
    connection["lastCheckedAt"] = now
    return status_payload(connection, config) | {"participationCount": len(participations)}


async def disconnect(user_id: str) -> dict[str, Any]:
    config = amazon_config()
    now = now_utc()
    connection = await collection("marketplaceconnections").find_one_and_update(
        {"userId": user_id, "marketplace": "amazon", "environment": config["environment"]},
        {
            "$set": {"status": "disconnected", "disconnectedAt": now, "updatedAt": now},
            "$unset": {"accessTokenEncrypted": "", "refreshTokenEncrypted": "", "accessTokenExpiresAt": "", "metadata": ""},
            "$setOnInsert": {"createdAt": now},
        },
        upsert=True,
        return_document=True,
    )
    return status_payload(connection, config)
