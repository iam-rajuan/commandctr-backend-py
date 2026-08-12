from fastapi import APIRouter

from app.modules.admin.route import router as admin_router
from app.modules.auth.route import router as auth_router
from app.modules.dashboard.route import router as dashboard_router
from app.modules.disputes.route import router as dispute_router
from app.modules.ebay.route import router as ebay_router
from app.modules.etsy.route import router as etsy_router
from app.modules.health.route import router as health_router
from app.modules.inventory.route import router as inventory_router
from app.modules.marketplace_connections.route import router as marketplace_connection_router
from app.modules.orders.route import router as order_router
from app.modules.products.route import router as product_router
from app.modules.shopify.route import router as shopify_router
from app.modules.sync.route import router as sync_log_router
from app.modules.users.route import router as user_router
from app.modules.wallet.route import router as wallet_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health")
api_router.include_router(auth_router, prefix="/auth")
api_router.include_router(user_router, prefix="/users")
api_router.include_router(admin_router, prefix="/admin")
api_router.include_router(dashboard_router, prefix="/dashboard")
api_router.include_router(dispute_router, prefix="/disputes")
api_router.include_router(ebay_router, prefix="/ebay")
api_router.include_router(etsy_router, prefix="/etsy")
api_router.include_router(marketplace_connection_router, prefix="/marketplace-connections")
api_router.include_router(shopify_router, prefix="/shopify")
api_router.include_router(product_router, prefix="/products")
api_router.include_router(order_router, prefix="/orders")
api_router.include_router(inventory_router, prefix="/inventory")
api_router.include_router(wallet_router, prefix="/wallet")
api_router.include_router(sync_log_router, prefix="/sync")
