from starlette import status

from app.auth import hash_password, safe_user, verify_password
from app.common import clean, with_timestamps
from app.db import collection, now_utc, oid
from app.errors import ApiError
from app.modules.users.schema import ChangePasswordBody, ProfileUpdateBody


async def get_by_id(user_id: str) -> dict:
    db_user = await collection("users").find_one({"_id": oid(user_id)})
    if not db_user:
        raise ApiError(status.HTTP_404_NOT_FOUND, "User not found")
    return safe_user(db_user)


async def get_profile(user_id: str) -> dict:
    return await get_by_id(user_id)


async def update_profile(user_id: str, body: ProfileUpdateBody) -> dict:
    db_user = await collection("users").find_one_and_update(
        {"_id": oid(user_id)}, {"$set": with_timestamps(clean(body))}, return_document=True
    )
    if not db_user:
        raise ApiError(status.HTTP_404_NOT_FOUND, "User not found")
    return safe_user(db_user)


async def change_password(user_id: str, body: ChangePasswordBody) -> None:
    db_user = await collection("users").find_one({"_id": oid(user_id)})
    if not db_user:
        raise ApiError(status.HTTP_404_NOT_FOUND, "User not found")
    if not verify_password(body.currentPassword, db_user.get("password", "")):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    await collection("users").update_one(
        {"_id": db_user["_id"]}, {"$set": {"password": hash_password(body.newPassword), "updatedAt": now_utc()}}
    )


async def get_all() -> list[dict]:
    return [safe_user(user) async for user in collection("users").find().sort("createdAt", -1)]


async def update_status(user_id: str, next_status: str) -> dict:
    db_user = await collection("users").find_one_and_update(
        {"_id": oid(user_id)}, {"$set": {"status": next_status, "updatedAt": now_utc()}}, return_document=True
    )
    if not db_user:
        raise ApiError(status.HTTP_404_NOT_FOUND, "User not found")
    return safe_user(db_user)


async def delete_user(user_id: str) -> None:
    result = await collection("users").delete_one({"_id": oid(user_id)})
    if result.deleted_count == 0:
        raise ApiError(status.HTTP_404_NOT_FOUND, "User not found")
