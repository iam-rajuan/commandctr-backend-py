# Migration Checklist

## Route Inventory

All original routes are represented under the same `API_PREFIX`.

| Method | Path |
| --- | --- |
| GET | `/` |
| GET | `/api/v1/health` |
| POST | `/api/v1/auth/register` |
| POST | `/api/v1/auth/login` |
| POST | `/api/v1/auth/refresh-token` |
| POST | `/api/v1/auth/logout` |
| GET | `/api/v1/auth/me` |
| GET | `/api/v1/users/profile` |
| PATCH | `/api/v1/users/profile` |
| PATCH | `/api/v1/users/change-password` |
| GET | `/api/v1/admin/users` |
| GET | `/api/v1/admin/users/{id}` |
| PATCH | `/api/v1/admin/users/{id}/block` |
| PATCH | `/api/v1/admin/users/{id}/unblock` |
| DELETE | `/api/v1/admin/users/{id}` |
| GET | `/api/v1/dashboard/overview` |
| GET | `/api/v1/disputes` |
| GET | `/api/v1/disputes/{id}` |
| GET | `/api/v1/ebay/callback` |
| GET | `/api/v1/ebay/connect-url` |
| GET | `/api/v1/ebay/status` |
| POST | `/api/v1/ebay/disconnect` |
| GET | `/api/v1/ebay/inventory` |
| PATCH | `/api/v1/ebay/products/{productId}/inventory` |
| PATCH | `/api/v1/ebay/inventory/{productId}` |
| POST | `/api/v1/ebay/products/{productId}/sync` |
| POST | `/api/v1/ebay/products/{productId}/sync-inventory` |
| POST | `/api/v1/ebay/products/{productId}/sync-price` |
| POST | `/api/v1/ebay/products/{productId}/publish` |
| POST | `/api/v1/ebay/products/{productId}/end` |
| GET | `/api/v1/ebay/products/{productId}/status` |
| POST | `/api/v1/ebay/listings/pull` |
| GET | `/api/v1/ebay/jobs/{jobId}` |
| GET | `/api/v1/etsy/callback` |
| GET | `/api/v1/etsy/connect-url` |
| GET | `/api/v1/etsy/status` |
| POST | `/api/v1/etsy/disconnect` |
| GET | `/api/v1/marketplace-connections` |
| GET | `/api/v1/shopify/callback` |
| GET | `/api/v1/shopify/product-image` |
| GET | `/api/v1/shopify/connect` |
| GET | `/api/v1/shopify/connect-url` |
| POST | `/api/v1/shopify/disconnect` |
| GET | `/api/v1/shopify/status` |
| GET | `/api/v1/shopify/shop` |
| POST | `/api/v1/shopify/import-products` |
| GET | `/api/v1/shopify/products` |
| POST | `/api/v1/shopify/products` |
| PATCH | `/api/v1/shopify/products` |
| PATCH | `/api/v1/shopify/products/{shopifyProductId}/price` |
| PATCH | `/api/v1/shopify/products/{shopifyProductId}` |
| PATCH | `/api/v1/shopify/products/price` |
| GET | `/api/v1/shopify/orders` |
| POST | `/api/v1/shopify/import-orders` |
| POST | `/api/v1/shopify/import-disputes` |
| GET | `/api/v1/shopify/locations` |
| GET | `/api/v1/shopify/inventory` |
| PATCH | `/api/v1/shopify/inventory` |
| PATCH | `/api/v1/shopify/inventory/{inventoryItemId}` |
| POST | `/api/v1/products` |
| GET | `/api/v1/products` |
| GET | `/api/v1/products/{id}` |
| PATCH | `/api/v1/products/{id}` |
| DELETE | `/api/v1/products/{id}` |
| POST | `/api/v1/orders/sandbox/seed` |
| GET | `/api/v1/orders` |
| GET | `/api/v1/orders/stats` |
| GET | `/api/v1/orders/export.csv` |
| GET | `/api/v1/orders/{id}` |
| PATCH | `/api/v1/orders/{id}/status` |
| POST | `/api/v1/orders/{id}/notes` |
| GET | `/api/v1/inventory` |
| GET | `/api/v1/inventory/{id}` |
| PATCH | `/api/v1/inventory/{id}` |
| PATCH | `/api/v1/inventory/{id}/safety-buffer` |
| GET | `/api/v1/wallet/overview` |
| GET | `/api/v1/sync/logs` |
| GET | `/api/v1/sync/logs/{id}` |

## Completed

- API inventory completed from Express router files.
- FastAPI project created with `uv`, Pydantic v2, Motor, centralized settings, errors, auth dependencies, and lifespan startup.
- Refactored into a module-based backend structure: thin `app/main.py`, central `app/api/routes.py`, and `route.py`, `controller.py`, `service.py`, `schema.py`, `model.py` files per domain module.
- Existing `.env` copied into this project and `.env.example` generated with placeholders.
- Mongo collections, fields, timestamps, indexes, and user-facing response envelopes preserved.
- Auth, role checks, JWT access/refresh flows, password hashing, default admin seed, and protected routes ported.
- CORS, rate limiting, request id header, validation errors, and 404 error envelope ported.
- Shopify, eBay, Etsy OAuth and public/protected marketplace routes ported.
- eBay background worker ported with job locking, operation dispatch, eBay API requests, token refresh, retry/backoff, and sync-log creation.
- Verified with `uv run python -m compileall app tests`, `uv run ruff check .`, `uv run pytest -q`, and FastAPI startup smoke probes.
