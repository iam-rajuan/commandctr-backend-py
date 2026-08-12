from functools import lru_cache
from typing import Literal

from pydantic import AnyUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    NODE_ENV: Literal["development", "test", "production"] = "development"
    TRUST_PROXY: str | bool | int | None = None
    PORT: int = 5000
    API_PREFIX: str = "/api/v1"
    MONGODB_URI: str
    MONGODB_DNS_SERVERS: str | None = None
    JWT_ACCESS_SECRET: str
    JWT_REFRESH_SECRET: str
    JWT_ACCESS_EXPIRES_IN: str = "15m"
    JWT_REFRESH_EXPIRES_IN: str = "7d"
    RATE_LIMIT_WINDOW_MS: int = 900000
    RATE_LIMIT_MAX: int = 100
    LOG_LEVEL: str = "info"
    AWS_REGION: str
    AWS_S3_BUCKET: str
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    RESEND_API_KEY: str
    RESEND_FROM_EMAIL: str
    SHOPIFY_SHOP_DOMAIN: str | None = None
    SHOPIFY_API_VERSION: str = "2026-04"
    SHOPIFY_CLIENT_ID: str | None = None
    SHOPIFY_CLIENT_SECRET: str | None = None
    SHOPIFY_APP_URL: AnyUrl | None = None
    SHOPIFY_REDIRECT_URI: AnyUrl | None = None
    SHOPIFY_ADMIN_ACCESS_TOKEN: str | None = None
    EBAY_ENVIRONMENT: Literal["sandbox", "production"] | None = None
    EBAY_CLIENT_ID: str | None = None
    EBAY_CLIENT_SECRET: str | None = None
    EBAY_RU_NAME: str | None = None
    EBAY_REDIRECT_URI: AnyUrl | None = None
    EBAY_SCOPES: str | None = None
    EBAY_API_BASE_URL: AnyUrl | None = None
    EBAY_MARKETPLACE_ID: str | None = None
    EBAY_MERCHANT_LOCATION_KEY: str | None = None
    EBAY_PAYMENT_POLICY_ID: str | None = None
    EBAY_RETURN_POLICY_ID: str | None = None
    EBAY_FULFILLMENT_POLICY_ID: str | None = None
    EBAY_DEFAULT_CATEGORY_ID: str | None = None
    EBAY_DEFAULT_CURRENCY: str | None = None
    EBAY_LISTING_FORMAT: str | None = None
    EBAY_LISTING_DURATION: str | None = None
    EBAY_DEFAULT_CONDITION: str | None = None
    EBAY_SANDBOX_CLIENT_ID: str | None = None
    EBAY_SANDBOX_CLIENT_SECRET: str | None = None
    EBAY_SANDBOX_RU_NAME: str | None = None
    EBAY_SANDBOX_REDIRECT_URI: AnyUrl | None = None
    EBAY_SANDBOX_AUTH_URL: AnyUrl | None = None
    EBAY_SANDBOX_TOKEN_URL: AnyUrl | None = None
    EBAY_PRODUCTION_CLIENT_ID: str | None = None
    EBAY_PRODUCTION_CLIENT_SECRET: str | None = None
    EBAY_PRODUCTION_RU_NAME: str | None = None
    EBAY_PRODUCTION_REDIRECT_URI: AnyUrl | None = None
    EBAY_PRODUCTION_AUTH_URL: AnyUrl | None = None
    EBAY_PRODUCTION_TOKEN_URL: AnyUrl | None = None
    ETSY_CLIENT_ID: str | None = None
    ETSY_REDIRECT_URI: AnyUrl | None = None
    ETSY_SCOPES: str | None = None
    ETSY_AUTH_URL: AnyUrl | None = None
    ETSY_TOKEN_URL: AnyUrl | None = None
    ETSY_API_BASE_URL: AnyUrl | None = None
    ETSY_SHARED_SECRET: str | None = None
    FRONTEND_URL: AnyUrl | None = None
    ENCRYPTION_SECRET: str = Field(min_length=32)

    @field_validator("*", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
