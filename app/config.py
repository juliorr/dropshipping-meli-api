"""Meli-API configuration using Pydantic Settings."""

import os
from typing import List

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.meli",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- General ---
    environment: str = "development"
    log_level: str = "INFO"

    # --- Database (meli-api own DB) ---
    database_url: str = "postgresql+asyncpg://meli:meli_dev_2026@meli-postgres:5432/meli_db"

    # --- JWT (same secret as backend so tokens are interoperable) ---
    jwt_secret: str = "dev_jwt_secret_key_change_in_production_789def"
    jwt_algorithm: str = "HS256"

    # --- Service-to-service API Key (backend calls meli-api) ---
    meli_api_key: str = "dev_meli_api_key_change_in_production"

    # --- CORS ---
    cors_origins: str = "https://local.dropshopingsps.com,http://localhost:3000"

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.cors_origins.split(",")]

    # --- Mercado Libre OAuth ---
    meli_client_id: str = ""
    meli_client_secret: str = ""
    meli_redirect_uri: str = "https://local.api.milisps.dropshopingsps.com/meli/callback"
    meli_site_id: str = "MLM"

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.environment.lower() != "production":
            return self
        errors: list[str] = []
        if not self.meli_client_id:
            errors.append("MELI_CLIENT_ID is required in production")
        if not self.meli_client_secret:
            errors.append("MELI_CLIENT_SECRET is required in production")
        if self.meli_api_key == "dev_meli_api_key_change_in_production":
            errors.append("MELI_API_KEY must be changed in production")
        if errors:
            raise ValueError(
                "Production environment validation failed:\n  - " + "\n  - ".join(errors)
            )
        return self


settings = Settings()
