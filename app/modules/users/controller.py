from fastapi import APIRouter, Depends

from app.auth import current_user
from app.errors import success
from app.modules.users import service
from app.modules.users.schema import ChangePasswordBody, ProfileUpdateBody

router = APIRouter()


@router.get("/profile")
async def get_profile(user=Depends(current_user)):
    return success("Profile fetched successfully", await service.get_profile(user["userId"]))


@router.patch("/profile")
async def update_profile(body: ProfileUpdateBody, user=Depends(current_user)):
    return success("Profile updated successfully", await service.update_profile(user["userId"], body))


@router.patch("/change-password")
async def change_password(body: ChangePasswordBody, user=Depends(current_user)):
    await service.change_password(user["userId"], body)
    return success("Password changed successfully")
