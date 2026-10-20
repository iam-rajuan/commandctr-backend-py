from fastapi import APIRouter, Depends

from app.auth import current_user
from app.errors import success
from app.modules.amazon import service
from app.modules.amazon.schema import AmazonListingBody, AmazonListingUpdateBody

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


@router.post("/disconnect")
async def disconnect_amazon(user=Depends(current_user)):
    connection = await service.disconnect(user["userId"])
    return success("Amazon disconnected successfully", connection)


@router.post("/listings", status_code=201)
async def create_amazon_listing(body: AmazonListingBody, user=Depends(current_user)):
    listing = await service.create_listing(user["userId"], body)
    return success("Amazon accepted the listing submission and it was saved in CommandCtr.", listing)


@router.patch("/listings/{product_id}")
async def update_amazon_listing(product_id: str, body: AmazonListingUpdateBody, user=Depends(current_user)):
    listing = await service.update_listing(user["userId"], product_id, body)
    return success("Amazon accepted the update submission and CommandCtr was updated.", listing)


@router.delete("/listings/{product_id}")
async def delete_amazon_listing(product_id: str, user=Depends(current_user)):
    result = await service.delete_listing(user["userId"], product_id)
    return success("Amazon accepted the delete and the local product was removed.", result)
