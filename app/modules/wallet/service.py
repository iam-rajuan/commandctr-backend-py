from collections import Counter, defaultdict

from app.db import collection, serialize


async def get_overview(user_id: str) -> dict:
    orders = [row async for row in collection("orders").find({"userId": user_id}).sort([("processedAt", -1), ("createdAt", -1)])]
    currency = Counter((order.get("currency") or "USD") for order in orders).most_common(1)[0][0] if orders else "USD"
    selected = [order for order in orders if (order.get("currency") or "USD") == currency]
    paid = pending = 0.0
    paid_count = pending_count = 0
    amounts: dict[str, float] = defaultdict(float)
    activity = []
    for order in selected:
        amount = float(order.get("totalPrice") or 0)
        financial = str(order.get("financialStatus") or "").upper()
        platform = str(order.get("marketplace") or "shopify").lower()
        typ = "ORDER_CREATED"
        if financial == "PAID":
            paid += amount
            paid_count += 1
            amounts[platform] += amount
            typ = "ORDER_PAID"
        elif financial in {"PENDING", "PAYMENT_PENDING", "PARTIALLY_PAID", "AUTHORIZED"}:
            pending += amount
            pending_count += 1
            typ = "ORDER_PENDING"
        elif financial in {"REFUNDED", "PARTIALLY_REFUNDED", "VOIDED", "CANCELLED"}:
            typ = "ORDER_REFUNDED"
        if len(activity) < 10:
            label = {"amazon": "Amazon", "ebay": "eBay", "tiktok": "TikTok", "etsy": "Etsy"}.get(platform, "Shopify")
            activity.append(
                {
                    "id": str(order.get("_id") or order.get("shopifyOrderId") or ""),
                    "type": typ,
                    "title": f"{label} order {typ.split('_')[-1].lower()}",
                    "subtitle": order.get("orderName")
                    or order.get("shopifyOrderId")
                    or order.get("externalOrderId")
                    or f"{label} order",
                    "amount": 0 if typ == "ORDER_REFUNDED" else round(amount, 2),
                    "currency": currency,
                    "status": financial.lower() or "created",
                    "occurredAt": serialize(order.get("processedAt") or order.get("createdAt")),
                }
            )
    conns = [conn async for conn in collection("marketplaceconnections").find({"userId": user_id})]
    connected = {(conn.get("marketplace"), conn.get("status")) for conn in conns}
    balances = []
    for platform, label in [
        ("shopify", "Shopify Revenue"),
        ("amazon", "Amazon Seller"),
        ("ebay", "eBay Payout"),
        ("tiktok", "TikTok Shop"),
        ("etsy", "Etsy Revenue"),
    ]:
        is_connected = (platform, "connected") in connected or (platform in {"amazon", "tiktok"} and amounts[platform] > 0)
        balances.append(
            {
                "platform": platform,
                "label": label,
                "amount": round(amounts[platform], 2),
                "currency": currency,
                "status": "Connected"
                if is_connected
                else ("Coming Soon" if platform in {"amazon", "tiktok"} else "Not Connected"),
                "isConnected": is_connected,
            }
        )
    notice = "Wallet totals are based on imported Shopify orders. Shopify payouts, fees, refunds, and bank balance are not included yet."
    if len({order.get("currency") or "USD" for order in orders}) > 1:
        notice += f" Multiple order currencies were detected, so totals currently reflect {currency} orders only."
    return {
        "currency": currency,
        "totalRevenue": round(paid, 2),
        "paidRevenue": round(paid, 2),
        "pendingRevenue": round(pending, 2),
        "refundedRevenue": 0,
        "orderCount": len(orders),
        "paidOrderCount": paid_count,
        "pendingOrderCount": pending_count,
        "platformBalances": balances,
        "recentActivity": activity,
        "notice": notice,
    }
