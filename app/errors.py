from datetime import datetime
from typing import Any

from bson import ObjectId
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status
from starlette.exceptions import HTTPException as StarletteHTTPException


def encode(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    return value


def success(message: str, data: Any = None) -> dict[str, Any]:
    return {"success": True, "message": message, "data": encode(data)}


def failure(message: str, errors: Any = None) -> dict[str, Any]:
    return {"success": False, "message": message, "errors": encode(errors)}


class ApiError(Exception):
    def __init__(self, status_code: int, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=failure(exc.message, {"details": exc.details} if exc.details else None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=failure("Validation failed", {"details": exc.errors()}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=failure(f"Route not found: {request.method} {request.url.path}"),
            )
        return JSONResponse(status_code=exc.status_code, content=failure(str(exc.detail)))
