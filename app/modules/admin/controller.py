from fastapi import APIRouter, Depends

from app.auth import require_roles
from app.errors import success
from app.modules.admin import service

router = APIRouter()


@router.get("/users")
async def admin_users(_user=Depends(require_roles("admin", "super_admin"))):
    return success("Users fetched successfully", await service.get_users())


@router.get("/users/{id}")
async def admin_user(id: str, _user=Depends(require_roles("admin", "super_admin"))):
    return success("User fetched successfully", await service.get_user_by_id(id))


@router.patch("/users/{id}/block")
async def block_user(id: str, _user=Depends(require_roles("admin", "super_admin"))):
    return success("User blocked successfully", await service.block_user(id))


@router.patch("/users/{id}/unblock")
async def unblock_user(id: str, _user=Depends(require_roles("admin", "super_admin"))):
    return success("User unblocked successfully", await service.unblock_user(id))


@router.delete("/users/{id}")
async def delete_user(id: str, _user=Depends(require_roles("admin", "super_admin"))):
    await service.delete_user(id)
    return success("User deleted successfully")
