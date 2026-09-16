from fastapi import APIRouter, Depends

from app.auth import current_user
from app.errors import success
from app.modules.inventory import service
from app.modules.inventory.schema import InventoryUpdateBody, SafetyBufferBody

router = APIRouter()


@router.get("")
@router.get("/")
async def get_inventory(includeInactive: str | None = None, user=Depends(current_user)):
    include_inactive = str(includeInactive).lower() == "true"
    return success("Inventory fetched successfully", await service.get_inventory(user["userId"], include_inactive))


@router.get("/{id}")
async def get_inventory_item(id: str, user=Depends(current_user)):
    return success("Inventory item fetched successfully", await service.get_inventory_by_id(user["userId"], id))


@router.patch("/{id}")
async def update_inventory(id: str, body: InventoryUpdateBody, user=Depends(current_user)):
    return success("Inventory item updated successfully", await service.update_inventory(user["userId"], id, body))


@router.patch("/{id}/safety-buffer")
async def update_safety_buffer(id: str, body: SafetyBufferBody, user=Depends(current_user)):
    return success("Safety buffer updated successfully", await service.update_safety_buffer(user["userId"], id, body))
