import re
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

password_re = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z\d]).{8,}$")


class RegisterBody(BaseModel):
    firstName: str = Field(min_length=1)
    lastName: str = Field(min_length=1)
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        if not password_re.match(value):
            raise ValueError(
                "Password must contain uppercase, lowercase, number, special character, and be at least 8 characters."
            )
        return value


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class RefreshBody(BaseModel):
    refreshToken: str = Field(min_length=1)


class ProfileUpdateBody(BaseModel):
    firstName: str | None = Field(default=None, min_length=1)
    lastName: str | None = Field(default=None, min_length=1)
    avatar: str | None = None
    phone: str | None = Field(default=None, min_length=5)


class ChangePasswordBody(BaseModel):
    currentPassword: str = Field(min_length=8)
    newPassword: str

    @field_validator("newPassword")
    @classmethod
    def strong_password(cls, value: str) -> str:
        if not password_re.match(value):
            raise ValueError(
                "Password must contain uppercase, lowercase, number, special character, and be at least 8 characters."
            )
        return value


class ProductVariantBody(BaseModel):
    shopifyVariantId: str | None = None
    title: str = Field(min_length=1)
    sku: str | None = None
    price: str | None = None
    compareAtPrice: str | None = None
    inventoryQuantity: int | None = Field(default=None, ge=0)
    inventoryItemId: str | None = None


class ProductCreateBody(BaseModel):
    marketplace: Literal["shopify", "ebay", "etsy"] | None = None
    shopifyProductId: str | None = None
    productAiProductId: str | None = None
    title: str = Field(min_length=1)
    description: str | None = None
    handle: str | None = None
    vendor: str | None = None
    productType: str | None = None
    status: str | None = None
    tags: list[str] | None = None
    totalInventory: int | None = Field(default=None, ge=0)
    featuredImage: str | None = None
    images: list[str] | None = None
    variants: list[ProductVariantBody] = Field(min_length=1)
    ebaySku: str | None = None
    ebayMarketplaceId: str | None = None
    ebayCurrency: str | None = None
    ebayCondition: str | None = None
    ebayCategoryId: str | None = None
    ebayItemSpecifics: dict[str, str | list[str]] | None = None
    ebayListingNotes: str | None = None
    ebayDescriptionOverride: str | None = None


class ProductUpdateBody(ProductCreateBody):
    title: str | None = Field(default=None, min_length=1)
    variants: list[ProductVariantBody] | None = None


class InventoryUpdateBody(BaseModel):
    availableQuantity: int | None = Field(default=None, ge=0)
    lowStockThreshold: int | None = Field(default=None, ge=0)
    locationName: str | None = None


class SafetyBufferBody(BaseModel):
    safetyBuffer: int = Field(ge=0)


class OrderStatusBody(BaseModel):
    status: Literal["Pending", "Shipped", "Delivered", "Cancelled"]
    carrier: str | None = None
    shippingService: str | None = None
    trackingNumber: str | None = None
    trackingUrl: str | None = None
    labelUrl: str | None = None
    notifyCustomer: bool | None = None
    cancelReason: Literal["CUSTOMER", "DECLINED", "FRAUD", "INVENTORY", "STAFF", "OTHER"] | None = None
    restock: bool | None = None
    refund: bool | None = None
    staffNote: str | None = Field(default=None, max_length=255)


class NoteBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ShopifyProductMutationBody(BaseModel):
    model_config = {"extra": "allow"}

    title: str | None = None
    description: str | None = None
    vendor: str | None = None
    productType: str | None = None
    category: str | None = None
    handle: str | None = None
    status: str | None = None
    tags: list[str] | None = None
    variants: list[dict[str, Any]] | None = None


class ShopifyPriceBody(BaseModel):
    variantId: str = Field(min_length=1)
    price: str = Field(min_length=1)
    compareAtPrice: str | None = None


class ShopifyInventoryBody(BaseModel):
    locationId: str = Field(min_length=1)
    quantity: int = Field(ge=0)


class EbayInventoryBody(BaseModel):
    quantity: int = Field(ge=0)


class EbayPullListingsBody(BaseModel):
    limit: int | None = Field(default=None, gt=0, le=200)
    offset: int | None = Field(default=None, ge=0)
