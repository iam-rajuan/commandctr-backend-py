from datetime import timedelta

from app.auth import hash_password
from app.db import collection, now_utc


async def ensure_default_admin() -> None:
    users = collection("users")
    if await users.find_one({"email": {"$in": ["admin@twinstr.com", "admin@commandctr.com"]}}):
        return
    now = now_utc()
    await users.insert_one(
        {
            "firstName": "Twinstr",
            "lastName": "Admin",
            "email": "admin@twinstr.com",
            "password": hash_password("admin@123"),
            "role": "admin",
            "status": "active",
            "createdAt": now,
            "updatedAt": now,
        }
    )


async def seed_sandbox_orders(user_id: str) -> None:
    now = now_utc()
    rows = [
        ("amazon", "SANDBOX-AMZ-1001", "#SBX-1001", "Jane Cooper", "jane.cooper@example.com", "PAID", "UNFULFILLED", "Pending", "129.97", 35, [{"title": "Performance Golf Glove", "sku": "SBX-GLOVE-M", "quantity": 2, "price": "24.99"}, {"title": "Alignment Putting Mirror", "sku": "SBX-MIRROR-01", "quantity": 1, "price": "79.99"}]),
        ("ebay", "SANDBOX-EBY-1002", "#SBX-1002", "Wade Warren", "wade.warren@example.com", "PAID", "FULFILLED", "Shipped", "59.50", 300, [{"title": "Golf Ball Retriever", "sku": "SBX-RETRIEVE-12", "quantity": 1, "price": "55.00"}]),
        ("tiktok", "SANDBOX-TTK-1003", "#SBX-1003", "Jenny Wilson", "jenny.wilson@example.com", "PAID", "DELIVERED", "Delivered", "214.25", 1440, [{"title": "Adjustable Golf Practice Net", "sku": "SBX-NET-PRO", "quantity": 1, "price": "198.00"}]),
        ("etsy", "SANDBOX-ETS-1004", "#SBX-1004", "Leslie Alexander", "leslie.alexander@example.com", "PAID", "UNFULFILLED", "Pending", "78.50", 720, [{"title": "Premium Leather Headcover", "sku": "SBX-COVER-LH", "quantity": 1, "price": "72.00"}]),
    ]
    for marketplace, external_id, name, customer, email, financial, fulfillment, override, total, minutes, items in rows:
        doc = {
            "userId": user_id,
            "marketplace": marketplace,
            "externalOrderId": external_id,
            "shopifyOrderId": external_id,
            "orderName": name,
            "email": email,
            "customerName": customer,
            "financialStatus": financial,
            "fulfillmentStatus": fulfillment,
            "statusOverride": override,
            "currency": "USD",
            "totalPrice": total,
            "subtotalPrice": total,
            "totalTax": "0",
            "processedAt": now - timedelta(minutes=minutes),
            "lineItems": items,
            "shippingAddress": {"name": customer, "country": "United States", "formatted": [customer, "United States"]},
            "billingAddress": {"name": customer, "country": "United States", "formatted": [customer, "United States"]},
            "isSandbox": True,
            "lastSyncedAt": now,
            "updatedAt": now,
        }
        await collection("orders").update_one(
            {"userId": user_id, "externalOrderId": external_id},
            {"$set": doc, "$setOnInsert": {"createdAt": now}},
            upsert=True,
        )
