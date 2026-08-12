from fastapi import APIRouter, Depends

from app.auth import current_user
from app.errors import success
from app.modules.marketplace_connections import service

router = APIRouter()


@router.get("/")
async def marketplace_connections(user=Depends(current_user)):
    return success("Marketplace connections fetched successfully", await service.list_connections(user["userId"]))
