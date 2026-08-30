# CommandCtr FastAPI Backend

FastAPI migration of the original `commandctr-backend` Express/Mongoose API.

## Setup

```powershell
uv sync
Copy-Item ..\commandctr-backend\.env .\.env
```

The migration keeps the same environment variable names as the Node backend. Use `.env.example` for safe placeholders.

## Run

```powershell
uv run uvicorn app.main:app --host 0.0.0.0 --port 9000 --reload
```

## Test And Validate

```powershell
uv run ruff check .
uv run pytest
uv run python -m compileall app
```

## API Contract

The API is mounted at `API_PREFIX`, default `/api/v1`. Success responses use:

```json
{ "success": true, "message": "Message", "data": null }
```

Error responses use:

```json
{ "success": false, "message": "Message", "errors": null }
```

Bearer JWT auth is preserved via the `Authorization: Bearer <token>` header. MongoDB collections and document fields are kept compatible with the original Mongoose models.

## Architecture

The app now follows the same thin-entrypoint style as `commandctr-ai-agent` while keeping the domain-module shape of `commandctr-backend`.

```text
app/
  main.py                 # FastAPI app bootstrap, middleware, root route
  api/routes.py           # Central API router aggregator
  config.py               # Pydantic settings
  db.py                   # Motor client, Mongo indexes, serialization
  auth.py                 # JWT/password/auth dependencies
  errors.py               # Central response/error envelope
  common.py               # Shared route/service helpers
  modules/
    <domain>/
      route.py             # Router export
      controller.py        # HTTP handlers
      service.py           # Business logic
      schema.py            # Request schemas
      model.py             # Mongo collection metadata
    marketplace_core/
      service.py           # Shared Shopify/eBay/Etsy OAuth/API worker logic
```

## Migrated Areas

- Health, auth, user profile, admin users
- Products, inventory, orders, disputes, sync logs
- Dashboard overview and wallet overview
- Marketplace connection listing
- Shopify OAuth, product/order/inventory endpoints, image proxy
- eBay OAuth, inventory surface, live API client, token refresh, sync job queue, retrying worker
- Etsy OAuth with PKCE, status, disconnect
- Default admin seed on startup

## Latest Validation

```powershell
uv run python -m compileall app tests
uv run ruff check .
uv run pytest -q
```

Startup smoke:

```powershell
uv run python -c "from app.main import app; print(app.title)"
```
