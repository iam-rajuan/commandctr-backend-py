from fastapi import APIRouter, Depends, Query

from app.auth import current_user
from app.errors import success
from app.modules.amazon import service
from app.modules.amazon.schema import AmazonListingBody, AmazonListingUpdateBody, AmazonOfferPatchBody

router = APIRouter()


@router.post("/connect")
async def connect_amazon(user=Depends(current_user)):
    connection = await service.connect(user["userId"])
    return success("Amazon connected successfully", connection)


@router.get("/status")
async def amazon_status(user=Depends(current_user)):
    return success("Amazon connection status fetched successfully", await service.get_status(user["userId"]))


@router.post("/verify")
async def verify_amazon(user=Depends(current_user)):
    result = await service.verify_connection(user["userId"])
    return success("Amazon connection verified successfully", result)


@router.get("/catalog/items/{asin}")
async def amazon_catalog_lookup(asin: str, user=Depends(current_user)):
    item = await service.catalog_item_lookup(user["userId"], asin)
    return success("Amazon catalog item retrieved successfully", item)


@router.get("/restrictions")
async def amazon_restrictions_check(
    asin: str,
    conditionType: str = Query(default="new_new"),
    user=Depends(current_user),
):
    result = await service.check_restrictions(user["userId"], asin, conditionType)
    return success("Amazon listing restrictions retrieved successfully", result)


@router.get("/product-types/{product_type}/definition")
async def amazon_product_type_definition(
    product_type: str,
    requirements: str = Query(default="LISTING"),
    user=Depends(current_user),
):
    definition = await service.product_type_definition(user["userId"], product_type, requirements)
    return success("Amazon Product Type Definition fetched successfully", definition)


@router.post("/disconnect")
async def disconnect_amazon(user=Depends(current_user)):
    connection = await service.disconnect(user["userId"])
    return success("Amazon disconnected successfully", connection)


@router.post("/listings/validate")
async def validate_amazon_listing(body: AmazonListingBody, user=Depends(current_user)):
    result = await service.validate_listing_preview(user["userId"], body)
    return success("Amazon listing validation completed", result)


@router.post("/listings", status_code=201)
async def create_amazon_listing(body: AmazonListingBody, user=Depends(current_user)):
    listing = await service.create_listing(user["userId"], body)
    return success("Amazon accepted the listing submission and it was saved in CommandCtr.", listing)


@router.patch("/listings/{product_id}/offer")
async def patch_amazon_offer(product_id: str, body: AmazonOfferPatchBody, user=Depends(current_user)):
    listing = await service.patch_listing_offer(user["userId"], product_id, body)
    return success("Amazon offer updated successfully.", listing)


@router.post("/listings/{product_id}/sync")
async def sync_amazon_listing(product_id: str, user=Depends(current_user)):
    return success("Amazon listing synchronized successfully.", await service.sync_listing(user["userId"], product_id))


@router.post("/listings/sync")
async def sync_amazon_listings(user=Depends(current_user)):
    return success("Amazon listing synchronization completed.", await service.sync_listings(user["userId"]))


@router.patch("/listings/{product_id}")
async def update_amazon_listing(product_id: str, body: AmazonListingUpdateBody, user=Depends(current_user)):
    listing = await service.update_listing(user["userId"], product_id, body)
    return success("Amazon accepted the update submission and CommandCtr was updated.", listing)


@router.delete("/listings/{product_id}")
async def delete_amazon_listing(product_id: str, user=Depends(current_user)):
    result = await service.delete_listing(user["userId"], product_id)
    return success("Amazon accepted the delete and the local product was removed.", result)
