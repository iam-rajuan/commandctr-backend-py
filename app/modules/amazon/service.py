from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx
from starlette import status

from app.config import get_settings
from app.crypto_utils import decrypt, encrypt
from app.db import collection, now_utc, serialize, to_utc_datetime
from app.errors import ApiError
from app.modules.amazon.schema import AmazonListingBody, AmazonListingUpdateBody, AmazonOfferPatchBody

logger = logging.getLogger(__name__)

SANDBOX_SELLER_ID = "AXXXXXXXXXXXX"
CANONICAL_PRODUCT_TYPE = re.compile(r"^[A-Z0-9_]+$")
OFFER_UPDATE_RETRIES = 3
FBM_FULFILLMENT_CHANNEL = "DEFAULT"
_offer_update_locks: dict[tuple[str, str], asyncio.Lock] = {}


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

    # OFFER-ONLY MODE (Existing ASIN)
    if body.identifierMode == "asin":
        attributes: dict[str, Any] = {
            "merchant_suggested_asin": [{"value": body.existingAsin, "marketplace_id": marketplace_id}],
            "condition_type": [{"value": body.condition or "new_new", "marketplace_id": marketplace_id}],
            "purchasable_offer": [
                {
                    "marketplace_id": marketplace_id,
                    "currency": config["currency"],
                    "our_price": [{"schedule": [{"value_with_tax": float(body.price)}]}],
                }
            ],
            "fulfillment_availability": [
                {"fulfillment_channel_code": body.fulfillmentChannel or "DEFAULT", "quantity": body.quantity}
            ],
        }
        allowed_offer_attrs = {
            "merchant_shipping_group",
            "condition_note",
            "lead_time_to_ship_max_days",
            "max_order_quantity",
            "gift_options",
            "product_tax_code",
            "restock_date",
        }
        for key, value in body.attributes.items():
            if key in allowed_offer_attrs:
                if isinstance(value, (str, int, float, bool)):
                    attributes[key] = [{"value": value, "marketplace_id": marketplace_id}]
                else:
                    attributes[key] = value
        return attributes

    # CATALOGUE PRODUCT CREATION (GTIN or GTIN Exemption)
    attributes: dict[str, Any] = {
        "item_name": [localized(body.title)],
        "condition_type": [{"value": body.condition or "new_new", "marketplace_id": marketplace_id}],
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
            {"fulfillment_channel_code": body.fulfillmentChannel or "DEFAULT", "quantity": body.quantity}
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
    if body.identifierMode == "asin":
        if not body.existingAsin:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter the existing Amazon ASIN for this identifier option.")
        if len(body.existingAsin.strip()) != 10:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "ASIN must be exactly 10 characters.")
    if body.identifierMode == "no_identifier":
        if identifier or body.existingAsin:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "Barcode-free products must not include a GTIN or ASIN.")
        if not body.exemptionConfirmed or not body.exemptionBrand or not body.exemptionCategory:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "GTIN exemption confirmation is required for the selected brand and product category.",
                {"requiresSellerCentralApproval": True},
            )


async def product_type_definition(user_id: str, product_type: str, requirements: str = "LISTING") -> dict[str, Any]:
    """Read the current UK schema; it never creates or changes listings."""
    config = amazon_config()
    token = await connected_access_token(user_id, config)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{config['endpoint']}/definitions/2020-09-01/productTypes/{quote(product_type, safe='')}",
            params={"marketplaceIds": config["marketplaceId"], "requirements": requirements, "locale": "en_GB"},
            headers={"x-amz-access-token": token, "user-agent": "CommandCtr/0.1 (Language=Python)"},
        )
    if response.status_code >= 400:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Amazon could not load the Product Type Definition.", {"statusCode": response.status_code})
    data = response.json()

    # If schema has an S3 pre-signed link, fetch the JSON schema
    schema_link = data.get("schema", {}).get("link", {}).get("resource")
    if schema_link:
        try:
            async with httpx.AsyncClient(timeout=30) as s3_client:
                s3_resp = await s3_client.get(schema_link)
                if s3_resp.status_code == 200:
                    resolved_schema = s3_resp.json()
                    data["resolvedSchema"] = resolved_schema
                    if not data.get("schema", {}).get("properties"):
                        data["schema"]["properties"] = resolved_schema.get("properties", {})
                    if not data.get("schema", {}).get("required"):
                        data["schema"]["required"] = resolved_schema.get("required", [])
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to fetch resolved S3 schema for product type %s: %s", product_type, exc)
    return data


async def catalog_item_lookup(user_id: str, asin: str) -> dict[str, Any]:
    clean_asin = asin.strip().upper()
    if len(clean_asin) != 10:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "ASIN must be exactly 10 characters.")
    config = amazon_config()
    token = await connected_access_token(user_id, config)
    url = f"{config['endpoint']}/catalog/2022-04-01/items/{clean_asin}"
    params = {
        "marketplaceIds": config["marketplaceId"],
        "includedData": "summaries,attributes,images,productTypes",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url,
                params=params,
                headers={"x-amz-access-token": token, "user-agent": "CommandCtr/0.1 (Language=Python)"},
            )
    except httpx.HTTPError as exc:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to communicate with Amazon Catalog Items API.") from exc

    if response.status_code == 404:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"No Amazon catalogue item found for ASIN '{clean_asin}'.")
    if response.status_code >= 400:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Amazon Catalog Items API rejected the lookup.", {"statusCode": response.status_code, "response": response.text})

    data = response.json()
    summaries = data.get("summaries") or []
    summary = summaries[0] if summaries else {}
    product_types = data.get("productTypes") or []
    resolved_pt = product_types[0].get("productType") if product_types else "PRODUCT"

    images_raw = data.get("images") or []
    image_list: list[dict[str, Any]] = []
    main_image_url: str | None = None
    if images_raw and isinstance(images_raw, list):
        for img_group in images_raw:
            for item in img_group.get("images") or []:
                link = item.get("link")
                if link:
                    variant = item.get("variant", "MAIN")
                    image_list.append({
                        "link": link,
                        "variant": variant,
                        "height": item.get("height"),
                        "width": item.get("width"),
                    })
                    if variant == "MAIN" and not main_image_url:
                        main_image_url = link
        if not main_image_url and image_list:
            main_image_url = image_list[0]["link"]

    return {
        "asin": clean_asin,
        "title": summary.get("itemName") or "Amazon Catalog Product",
        "brand": summary.get("brand") or summary.get("manufacturer"),
        "manufacturer": summary.get("manufacturer"),
        "color": summary.get("color"),
        "itemClassification": summary.get("itemClassification"),
        "productType": resolved_pt,
        "imageUrl": main_image_url,
        "images": image_list,
        "marketplaceId": config["marketplaceId"],
    }


async def check_restrictions(user_id: str, asin: str, condition_type: str = "new_new") -> dict[str, Any]:
    clean_asin = asin.strip().upper()
    if len(clean_asin) != 10:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "ASIN must be exactly 10 characters.")
    config = amazon_config()
    token = await connected_access_token(user_id, config)
    url = f"{config['endpoint']}/listings/2021-08-01/restrictions"
    params = {
        "asin": clean_asin,
        "sellerId": config["sellerId"],
        "marketplaceIds": config["marketplaceId"],
        "conditionType": condition_type,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url,
                params=params,
                headers={"x-amz-access-token": token, "user-agent": "CommandCtr/0.1 (Language=Python)"},
            )
    except httpx.HTTPError as exc:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to communicate with Amazon Listings Restrictions API.") from exc

    if response.status_code >= 400:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Amazon Listings Restrictions API returned an error.", {"statusCode": response.status_code, "response": response.text})

    data = response.json()
    restrictions = data.get("restrictions") or []
    reasons_list: list[dict[str, Any]] = []
    for r in restrictions:
        for reason in r.get("reasons") or []:
            reasons_list.append({
                "reasonCode": reason.get("reasonCode"),
                "message": reason.get("message"),
                "links": reason.get("links") or [],
            })

    is_restricted = len(restrictions) > 0
    return {
        "asin": clean_asin,
        "conditionType": condition_type,
        "restricted": is_restricted,
        "eligible": not is_restricted,
        "restrictions": restrictions,
        "reasons": reasons_list,
    }


async def connected_access_token(user_id: str, config: dict[str, str]) -> str:
    connection = await collection("marketplaceconnections").find_one(
        {"userId": user_id, "marketplace": "amazon", "environment": config["environment"]},
        sort=[("updatedAt", -1)],
    )
    if not connection or connection.get("status") != "connected" or not connection.get("refreshTokenEncrypted"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, f"Connect Amazon {config['environment']} before publishing a product.")

    expires_at = to_utc_datetime(connection.get("accessTokenExpiresAt"))
    cached_token_enc = connection.get("accessTokenEncrypted")
    if expires_at and cached_token_enc and expires_at > (now_utc() + timedelta(seconds=60)):
        try:
            return decrypt(cached_token_enc)
        except (ValueError, KeyError) as exc:
            logger.warning("Failed to decrypt cached access token: %s", exc)

    token = await exchange_access_token(decrypt(connection["refreshTokenEncrypted"]), config)
    now = now_utc()
    await collection("marketplaceconnections").update_one(
        {"_id": connection["_id"]},
        {
            "$set": {
                "accessTokenEncrypted": encrypt(token["access_token"]),
                "accessTokenExpiresAt": now + timedelta(seconds=int(token.get("expires_in", 3600))),
                "lastCheckedAt": now,
            }
        },
    )
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
    included_data: str | None = None,
) -> dict[str, Any]:
    config = amazon_config()
    access_token = await connected_access_token(user_id, config)
    url = f"{config['endpoint']}/listings/2021-08-01/items/{quote(config['sellerId'], safe='')}/{quote(sku, safe='')}"
    params = {"marketplaceIds": config["marketplaceId"], "issueLocale": "en_GB"}
    if validation_preview:
        params["mode"] = "VALIDATION_PREVIEW"
    if included_data:
        params["includedData"] = included_data
    response: httpx.Response | None = None
    for attempt in range(OFFER_UPDATE_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(
                    method, url, params=params,
                    headers={"Content-Type": "application/json", "x-amz-access-token": access_token, "x-amz-date": now_utc().strftime("%Y%m%dT%H%M%SZ"), "user-agent": "CommandCtr/0.1 (Language=Python)"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            if attempt + 1 == OFFER_UPDATE_RETRIES:
                raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to communicate with Amazon SP-API.") from exc
            await asyncio.sleep(0.25 * (2 ** attempt))
            continue
        if response.status_code not in {429, 500, 503} or attempt + 1 == OFFER_UPDATE_RETRIES:
            break
        await asyncio.sleep(0.25 * (2 ** attempt))
    if response is None:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "Failed to communicate with Amazon SP-API.")
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


def _first_mapping(values: Any) -> dict[str, Any]:
    return values[0] if isinstance(values, list) and values and isinstance(values[0], dict) else {}


def _remote_listing_status(summary: dict[str, Any], issues: list[dict[str, Any]]) -> str:
    actions = {str(action).upper() for issue in issues for action in issue.get("enforcementActions") or []}
    if "SEARCH_SUPPRESSED" in actions:
        return "SUPPRESSED"
    if any(str(issue.get("severity", "")).upper() == "ERROR" for issue in issues):
        return "ISSUES"
    statuses = {str(value).upper() for value in summary.get("status") or []}
    if "BUYABLE" in statuses:
        return "ACTIVE"
    if "INACTIVE" in statuses:
        return "INACTIVE"
    return "PROCESSING"


def _remote_listing_update(product: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    summary = _first_mapping(remote.get("summaries"))
    issues = [issue for issue in remote.get("issues") or [] if isinstance(issue, dict)]
    offers = remote.get("offers") or []
    offer = next((candidate for candidate in offers if isinstance(candidate, dict) and candidate.get("marketplaceId") == product.get("amazonMarketplaceId")), _first_mapping(offers))
    price = (offer.get("price") or {}).get("amount") if isinstance(offer, dict) else None
    availability = next((candidate for candidate in remote.get("fulfillmentAvailability") or [] if isinstance(candidate, dict) and candidate.get("fulfillmentChannelCode") == FBM_FULFILLMENT_CHANNEL), {})
    quantity = availability.get("quantity") if isinstance(availability, dict) else None
    asin = summary.get("asin") or product.get("amazonAsin")
    listing_status = _remote_listing_status(summary, issues)
    update: dict[str, Any] = {"amazonAsin": asin, "amazonStatus": listing_status, "amazonIssues": issues, "amazonRemoteSummary": summary, "amazonLastSyncError": None, "amazonOfferUpdatePending": False, "lastAmazonSyncAt": now_utc(), "updatedAt": now_utc(), "status": listing_status.lower()}
    if summary.get("itemName"):
        update["title"] = summary["itemName"]
    if asin:
        update["amazonListingUrl"] = f"https://www.amazon.co.uk/dp/{asin}"
    if price is not None:
        normalized_price = f"{float(price):.2f}"
        update["price"] = normalized_price
        update["variants.0.price"] = normalized_price
    if quantity is not None:
        update["totalInventory"] = int(quantity)
        update["variants.0.inventoryQuantity"] = int(quantity)
    return update


async def _sync_listing_document(user_id: str, product: dict[str, Any]) -> dict[str, Any]:
    remote = await listings_request(user_id, "GET", str(product["amazonSku"]), included_data="summaries,attributes,issues,offers,fulfillmentAvailability")
    saved = await collection("products").find_one_and_update({"_id": product["_id"], "userId": user_id}, {"$set": _remote_listing_update(product, remote)}, return_document=True)
    return serialize(saved)


def listing_submission(body: AmazonListingBody, config: dict[str, str]) -> dict[str, Any]:
    requirements = "LISTING_OFFER_ONLY" if body.identifierMode == "asin" else "LISTING"
    return {
        "productType": body.productType,
        "requirements": requirements,
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
    error_issues = [i for i in issues if str(i.get("severity", "")).upper() == "ERROR"]
    has_blocking_error = preview.get("status") == "INVALID" or len(error_issues) > 0
    if has_blocking_error:
        issue_details = []
        for iss in (error_issues or issues):
            code = iss.get("code") or "UNKNOWN"
            sev = iss.get("severity") or "ERROR"
            msg = iss.get("message") or "Unknown validation issue"
            attrs = iss.get("attributeNames") or []
            attr_str = f" (attribute: {', '.join(attrs)})" if attrs else ""
            issue_details.append(f"[{sev}] Code {code}{attr_str}: {msg}")
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Amazon rejected the listing validation preview. Correct the reported errors before publishing: {'; '.join(issue_details)}",
            {
                "status": preview.get("status", "INVALID"),
                "issues": issues,
                "errorIssues": error_issues,
                "warningIssues": [i for i in issues if str(i.get("severity", "")).upper() == "WARNING"],
                "infoIssues": [i for i in issues if str(i.get("severity", "")).upper() == "INFO"],
            },
        )
    return preview


async def validate_listing_preview(user_id: str, body: AmazonListingBody) -> dict[str, Any]:
    validate_identifier_path(body)
    config = amazon_config()
    resolved_body = await resolve_product_type(user_id, body)
    if config["environment"] != "production":
        return {
            "valid": True,
            "status": "ACCEPTED",
            "issues": [],
            "errorIssues": [],
            "warningIssues": [],
            "infoIssues": [],
            "sku": resolved_body.sku,
            "productType": resolved_body.productType,
            "environment": config["environment"],
        }
    preview = await listings_request(
        user_id,
        "PUT",
        resolved_body.sku,
        body=listing_submission(resolved_body, config),
        validation_preview=True,
    )
    issues = preview.get("issues") or []
    error_issues = [i for i in issues if str(i.get("severity", "")).upper() == "ERROR"]
    warning_issues = [i for i in issues if str(i.get("severity", "")).upper() == "WARNING"]
    info_issues = [i for i in issues if str(i.get("severity", "")).upper() == "INFO"]
    has_blocking_error = preview.get("status") == "INVALID" or len(error_issues) > 0
    return {
        "valid": not has_blocking_error,
        "status": preview.get("status", "ACCEPTED" if not has_blocking_error else "INVALID"),
        "issues": issues,
        "errorIssues": error_issues,
        "warningIssues": warning_issues,
        "infoIssues": info_issues,
        "sku": resolved_body.sku,
        "productType": resolved_body.productType,
        "environment": config["environment"],
    }


def local_listing_document(body: AmazonListingBody, response: dict[str, Any], config: dict[str, str]) -> dict[str, Any]:
    asin = body.existingAsin if body.identifierMode == "asin" else None
    listing_url = f"https://www.amazon.co.uk/dp/{asin}" if asin else None
    return {
        "marketplace": "amazon",
        "environment": config["environment"],
        "externalPersistence": config["environment"] == "production",
        "amazonSellerId": config["sellerId"],
        "amazonMarketplaceId": config["marketplaceId"],
        "amazonSku": body.sku,
        "amazonAsin": asin,
        "amazonCondition": body.condition,
        "amazonFulfillmentChannel": body.fulfillmentChannel,
        "amazonListingUrl": listing_url,
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
            "identifierMode": body.identifierMode,
            "existingAsin": body.existingAsin,
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


async def patch_listing_offer(user_id: str, product_id: str, body: AmazonOfferPatchBody) -> dict[str, Any]:
    from app.db import oid

    config = amazon_config()
    lock = _offer_update_locks.setdefault((user_id, product_id), asyncio.Lock())
    if lock.locked():
        raise ApiError(status.HTTP_409_CONFLICT, "An Amazon offer update is already in progress for this SKU.")
    async with lock:
        product = await collection("products").find_one({"_id": oid(product_id), "userId": user_id, "marketplace": "amazon", "environment": config["environment"]})
        if not product:
            raise ApiError(status.HTTP_404_NOT_FOUND, f"Amazon {config['environment']} product not found.")
        if body.quantity is not None and str(product.get("amazonFulfillmentChannel") or FBM_FULFILLMENT_CHANNEL).upper() != FBM_FULFILLMENT_CHANNEL:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "Only seller-fulfilled (FBM) inventory can be edited from CommandCtr.")
        patches: list[dict[str, Any]] = []
        if body.price is not None:
            patches.append({"op": "replace", "path": "/attributes/purchasable_offer", "value": [{"marketplace_id": config["marketplaceId"], "currency": config["currency"], "our_price": [{"schedule": [{"value_with_tax": float(body.price)}]}]}]})
        if body.quantity is not None:
            patches.append({"op": "replace", "path": "/attributes/fulfillment_availability", "value": [{"fulfillment_channel_code": FBM_FULFILLMENT_CHANNEL, "quantity": body.quantity}]})
        if not patches:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "At least one of price or quantity must be provided.")
        response = await listings_request(user_id, "PATCH", str(product["amazonSku"]), body={"productType": str(product.get("productType") or "PRODUCT"), "patches": patches})
        update: dict[str, Any] = {"amazonStatus": response.get("status", "ACCEPTED"), "amazonIssues": response.get("issues") or [], "amazonLastSyncError": None, "updatedAt": now_utc()}
        if body.price is not None:
            update["price"] = body.price
            update["variants.0.price"] = body.price
        if body.quantity is not None:
            update["totalInventory"] = body.quantity
            update["variants.0.inventoryQuantity"] = body.quantity
        saved = await collection("products").find_one_and_update({"_id": product["_id"], "userId": user_id}, {"$set": update}, return_document=True)
        try:
            synchronized = await _sync_listing_document(user_id, saved or product)
            synced_variant = (synchronized.get("variants") or [{}])[0]
            remote_price = str(synced_variant.get("price") or "")
            remote_quantity = synced_variant.get("inventoryQuantity")
            price_pending = body.price is not None and remote_price != body.price
            quantity_pending = body.quantity is not None and remote_quantity != body.quantity
            if not price_pending and not quantity_pending:
                return synchronized
            pending_fields = {
                "amazonOfferUpdatePending": True,
                "amazonLastSyncError": "Amazon accepted the update request but has not confirmed it yet. The displayed offer remains Amazon's current value.",
                "updatedAt": now_utc(),
            }
            pending = await collection("products").find_one_and_update({"_id": product["_id"], "userId": user_id}, {"$set": pending_fields}, return_document=True)
            return serialize(pending)
        except ApiError as exc:
            logger.warning("Amazon accepted an offer update for %s but remote confirmation is pending: %s", product["amazonSku"], exc.message)
            fallback = await collection("products").find_one_and_update({"_id": product["_id"], "userId": user_id}, {"$set": {"amazonLastSyncError": "Amazon accepted the update; remote confirmation is pending.", "updatedAt": now_utc()}}, return_document=True)
            return serialize(fallback)


async def sync_listing(user_id: str, product_id: str) -> dict[str, Any]:
    from app.db import oid

    config = amazon_config()
    product = await collection("products").find_one({"_id": oid(product_id), "userId": user_id, "marketplace": "amazon", "environment": config["environment"]})
    if not product:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"Amazon {config['environment']} product not found.")
    return await _sync_listing_document(user_id, product)


async def sync_listings(user_id: str, limit: int = 50) -> dict[str, Any]:
    config = amazon_config()
    products = await collection("products").find({"userId": user_id, "marketplace": "amazon", "environment": config["environment"]}).limit(limit).to_list(length=limit)
    synced = 0
    failed: list[dict[str, str]] = []
    for product in products:
        try:
            await _sync_listing_document(user_id, product)
            synced += 1
        except ApiError as exc:
            failed.append({"sku": str(product.get("amazonSku") or "unknown"), "message": exc.message})
    return {"requested": len(products), "synced": synced, "failed": failed, "lastSyncAt": now_utc()}


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
