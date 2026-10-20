from typing import Any

from pydantic import BaseModel, Field, field_validator


class AmazonListingBody(BaseModel):
    title: str = Field(min_length=1)
    sku: str = Field(min_length=1)
    price: str = Field(min_length=1)
    quantity: int = Field(default=0, ge=0)
    description: str | None = None
    brand: str | None = None
    productType: str = Field(default="PRODUCT", min_length=1)
    imagePath: str | None = None
    bulletPoints: list[str] = Field(default_factory=list)
    backendSearchTerms: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: str) -> str:
        try:
            numeric = float(value)
        except ValueError as exc:
            raise ValueError("Price must be a valid number.") from exc
        if numeric < 0:
            raise ValueError("Price must be greater than or equal to 0.")
        return f"{numeric:.2f}"


class AmazonListingUpdateBody(AmazonListingBody):
    pass
