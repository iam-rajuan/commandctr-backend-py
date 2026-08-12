from fastapi import APIRouter, Depends

from app.auth import current_user
from app.errors import success
from app.modules.auth import service
from app.modules.auth.schema import LoginBody, RefreshBody, RegisterBody

router = APIRouter()

@router.post("/register", status_code=201)
async def register(body: RegisterBody):
    return success("Registration successful", await service.register_user(body))


@router.post("/login")
async def login(body: LoginBody):
    return success("Login successful", await service.login_user(body))


@router.post("/refresh-token")
async def refresh_token(body: RefreshBody):
    return success("Access token refreshed successfully", await service.refresh_access_token(body))


@router.post("/logout")
async def logout():
    return success("Logout successful")


@router.get("/me")
async def me(user=Depends(current_user)):
    return success("Authenticated user fetched successfully", await service.get_me(user["userId"]))
