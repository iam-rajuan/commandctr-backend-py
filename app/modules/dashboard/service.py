from app.db import collection, serialize


async def get_overview(user_id: str) -> dict:
    conn = await collection("marketplaceconnections").find_one({"userId": user_id, "marketplace": "shopify"})
    return {
        "totalProducts": await collection("products").count_documents({"userId": user_id}),
        "totalOrders": await collection("orders").count_documents({"userId": user_id}),
        "lowStock": await collection("inventories").count_documents(
            {"userId": user_id, "$expr": {"$lte": ["$availableQuantity", "$lowStockThreshold"]}}
        ),
        "syncErrors": await collection("sync_logs").count_documents({"userId": user_id, "status": "failed"}),
        "connectedMarketplace": "shopify",
        "shopifyConnectionStatus": serialize(
            {
                "connected": bool(conn and conn.get("status") == "connected" and conn.get("accessTokenEncrypted")),
                "shopDomain": conn.get("shopDomain") if conn else None,
                "status": conn.get("status") if conn else "not_connected",
            }
        ),
    }
