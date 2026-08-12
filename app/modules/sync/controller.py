from fastapi import APIRouter, Depends

from app.auth import current_user
from app.errors import success
from app.modules.sync import service

router = APIRouter()


@router.get("/logs")
async def sync_logs(user=Depends(current_user)):
    return success("Sync logs fetched successfully", await service.list_logs(user["userId"]))


@router.get("/logs/{id}")
async def sync_log(id: str, user=Depends(current_user)):
    return success("Sync log fetched successfully", await service.get_log(user["userId"], id))
