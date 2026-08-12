from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, Header
from starlette import status

from app.config import get_settings
from app.db import collection, oid, serialize
from app.errors import ApiError


def parse_exp(value: str) -> timedelta:
    unit = value[-1]
    amount = int(value[:-1]) if value[:-1].isdigit() else int(value)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    return timedelta(seconds=amount)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bool(hashed) and bcrypt.checkpw(password.encode(), hashed.encode())


def make_token(payload: dict[str, Any], secret: str, expires_in: str, **claims: Any) -> str:
    now = datetime.now(UTC)
    return jwt.encode({**payload, **claims, "iat": now, "exp": now + parse_exp(expires_in)}, secret, algorithm="HS256")


def safe_user(user: dict[str, Any]) -> dict[str, Any]:
    return serialize(
        {
            "id": user["_id"],
            "firstName": user.get("firstName"),
            "lastName": user.get("lastName"),
            "email": user.get("email"),
            "role": user.get("role", "user"),
            "status": user.get("status", "active"),
            "avatar": user.get("avatar"),
            "phone": user.get("phone"),
            "lastLoginAt": user.get("lastLoginAt"),
            "createdAt": user.get("createdAt"),
            "updatedAt": user.get("updatedAt"),
        }
    )


async def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "Authorization token is required")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "Authorization token is required")
    try:
        payload = jwt.decode(token, get_settings().JWT_ACCESS_SECRET, algorithms=["HS256"])
        user = await collection("users").find_one({"_id": oid(payload.get("userId", ""))})
    except Exception as exc:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "Invalid or expired access token") from exc
    if not user:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "User not found for this token")
    if user.get("status") == "blocked":
        raise ApiError(status.HTTP_403_FORBIDDEN, "Your account is blocked")
    return {"userId": str(user["_id"]), "email": user["email"], "role": user.get("role", "user")}


def require_roles(*roles: str):
    async def checker(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if user.get("role") not in roles:
            raise ApiError(status.HTTP_403_FORBIDDEN, "You are not authorized to access this resource")
        return user

    return checker
