from fastapi import APIRouter, Depends

from app.auth import current_user
from app.errors import success
from app.modules.products import service
from app.modules.products.schema import ProductCreateBody, ProductUpdateBody

router = APIRouter()


@router.post("", status_code=201)
async def create_product(body: ProductCreateBody, user=Depends(current_user)):
    return success("Product created successfully", await service.create_product(user["userId"], body))


@router.get("/")
async def get_products(user=Depends(current_user)):
    return success("Products fetched successfully", await service.get_products(user["userId"]))


@router.get("/{id}")
async def get_product(id: str, user=Depends(current_user)):
    return success("Product fetched successfully", await service.get_product_by_id(user["userId"], id))


@router.patch("/{id}")
async def update_product(id: str, body: ProductUpdateBody, user=Depends(current_user)):
    return success("Product updated successfully", await service.update_product(user["userId"], id, body))


@router.delete("/{id}")
async def delete_product(id: str, user=Depends(current_user)):
    await service.delete_product(user["userId"], id)
    return success("Product deleted successfully")
