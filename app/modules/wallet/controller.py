from fastapi import APIRouter, Depends

from app.auth import current_user
from app.errors import success
from app.modules.wallet import service

router = APIRouter()


@router.get("/overview")
async def wallet(user=Depends(current_user)):
    return success("Wallet overview retrieved successfully", await service.get_overview(user["userId"]))
