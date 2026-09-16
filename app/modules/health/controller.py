from fastapi import APIRouter

from app.errors import success
from app.modules.health import service

router = APIRouter()


@router.get("")
@router.get("/")
async def health():
    return success("CommandCtr API is healthy", await service.get_health())
