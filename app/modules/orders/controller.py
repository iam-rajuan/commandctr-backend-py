from fastapi import APIRouter, Depends, Response
from fastapi.responses import PlainTextResponse

from app.auth import current_user
from app.db import now_utc
from app.errors import success
from app.modules.orders import service
from app.modules.orders.schema import NoteBody, OrderStatusBody

router = APIRouter()


@router.post("/sandbox/seed")
async def seed_sandbox(user=Depends(current_user)):
    return success("Sandbox orders created successfully", await service.seed_orders(user["userId"]))


@router.get("")
@router.get("/")
async def list_orders(
    page: str | None = None,
    limit: str | None = None,
    search: str | None = None,
    marketplace: str | None = None,
    status: str | None = None,
    user=Depends(current_user),
):
    return success(
        "Orders fetched successfully",
        await service.get_orders(user["userId"], page, limit, search, marketplace, status),
    )


@router.get("/stats")
async def get_order_stats(user=Depends(current_user)):
    return success("Order stats fetched successfully", await service.get_order_stats(user["userId"]))


@router.get("/export.csv", response_class=PlainTextResponse)
async def export_orders_csv(response: Response, user=Depends(current_user)):
    response.headers["Content-Disposition"] = f'attachment; filename="orders-{now_utc().date().isoformat()}.csv"'
    return await service.export_orders_csv(user["userId"])


@router.get("/{id}")
async def get_order(id: str, user=Depends(current_user)):
    return success("Order fetched successfully", await service.get_order_by_id(user["userId"], id))


@router.patch("/{id}/status")
async def update_order_status(id: str, body: OrderStatusBody, user=Depends(current_user)):
    return success("Order status updated successfully", await service.update_order_status(user["userId"], id, body))


@router.post("/{id}/notes")
async def add_note(id: str, body: NoteBody, user=Depends(current_user)):
    return success("Order note added successfully", await service.add_note(user["userId"], id, body))
