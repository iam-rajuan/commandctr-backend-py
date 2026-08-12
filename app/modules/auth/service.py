import jwt
from starlette import status

from app.auth import hash_password, make_token, safe_user, verify_password
from app.common import with_timestamps
from app.config import get_settings
from app.db import collection, now_utc, oid
from app.errors import ApiError
from app.modules.auth.schema import LoginBody, RefreshBody, RegisterBody

settings = get_settings()


def _tokens(user: dict) -> dict[str, str]:
    payload = {"userId": user["id"], "email": user["email"], "role": user["role"]}
    return {
        "accessToken": make_token(payload, settings.JWT_ACCESS_SECRET, settings.JWT_ACCESS_EXPIRES_IN),
        "refreshToken": make_token(payload, settings.JWT_REFRESH_SECRET, settings.JWT_REFRESH_EXPIRES_IN),
    }


async def register_user(body: RegisterBody) -> dict:
    if await collection("users").find_one({"email": body.email.lower()}):
        raise ApiError(status.HTTP_409_CONFLICT, "User already exists with this email")

    result = await collection("users").insert_one(
        with_timestamps(
            {
                "firstName": body.firstName,
                "lastName": body.lastName,
                "email": body.email.lower(),
                "password": hash_password(body.password),
                "role": "user",
                "status": "active",
            },
            insert=True,
        )
    )
    user = safe_user(await collection("users").find_one({"_id": result.inserted_id}))
    await collection("sync_logs").insert_one(
        {
            "userId": user["id"],
            "marketplace": "shopify",
            "entityType": "auth",
            "entityId": user["id"],
            "action": "register",
            "status": "success",
            "message": "User registered successfully",
            "createdAt": now_utc(),
        }
    )
    return {"user": user, **_tokens(user)}


async def login_user(body: LoginBody) -> dict:
    db_user = await collection("users").find_one({"email": body.email.lower()})
    if not db_user or not verify_password(body.password, db_user.get("password", "")):
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if db_user.get("status") == "blocked":
        raise ApiError(status.HTTP_403_FORBIDDEN, "Your account is blocked")

    await collection("users").update_one(
        {"_id": db_user["_id"]}, {"$set": {"lastLoginAt": now_utc(), "updatedAt": now_utc()}}
    )
    user = safe_user(await collection("users").find_one({"_id": db_user["_id"]}))
    await collection("sync_logs").insert_one(
        {
            "userId": user["id"],
            "marketplace": "shopify",
            "entityType": "auth",
            "entityId": user["id"],
            "action": "login",
            "status": "success",
            "message": "User logged in successfully",
            "createdAt": now_utc(),
        }
    )
    return {"user": user, **_tokens(user)}


async def refresh_access_token(body: RefreshBody) -> dict[str, str]:
    try:
        payload = jwt.decode(body.refreshToken, settings.JWT_REFRESH_SECRET, algorithms=["HS256"])
        db_user = await collection("users").find_one({"_id": oid(payload["userId"])})
        if not db_user or db_user.get("status") == "blocked":
            raise ValueError
    except Exception as exc:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from exc
    user = safe_user(db_user)
    token_payload = {"userId": user["id"], "email": user["email"], "role": user["role"]}
    return {"accessToken": make_token(token_payload, settings.JWT_ACCESS_SECRET, settings.JWT_ACCESS_EXPIRES_IN)}


async def get_me(user_id: str) -> dict:
    db_user = await collection("users").find_one({"_id": oid(user_id)})
    if not db_user:
        raise ApiError(status.HTTP_404_NOT_FOUND, "User not found")
    return safe_user(db_user)
