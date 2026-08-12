from fastapi import APIRouter, Depends

from app.auth import current_user
from app.errors import success
from app.modules.disputes import service

router = APIRouter()


@router.get("/")
async def list_disputes(
    page: str | None = None,
    limit: str | None = None,
    search: str | None = None,
    status: str | None = None,
    user=Depends(current_user),
):
    return success(
        "Disputes retrieved successfully",
        await service.list_disputes(user["userId"], page, limit, search, status),
    )


@router.get("/{id}")
async def get_dispute(id: str, user=Depends(current_user)):
    return success("Dispute retrieved successfully", await service.get_dispute(user["userId"], id))
