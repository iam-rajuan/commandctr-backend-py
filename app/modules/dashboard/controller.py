from fastapi import APIRouter, Depends

from app.auth import current_user
from app.errors import success
from app.modules.dashboard import service

router = APIRouter()


@router.get("/overview")
async def dashboard(user=Depends(current_user)):
    return success("Dashboard overview fetched successfully", await service.get_overview(user["userId"]))
