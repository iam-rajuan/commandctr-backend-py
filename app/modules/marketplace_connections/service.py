from app.db import collection, serialize


async def list_connections(user_id: str) -> list[dict]:
    rows = [
        row
        async for row in collection("marketplaceconnections")
        .find({"userId": user_id}, {"accessTokenEncrypted": 0, "refreshTokenEncrypted": 0})
        .sort("createdAt", -1)
    ]
    return serialize(rows)
