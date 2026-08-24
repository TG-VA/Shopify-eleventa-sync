"""
Configuración del middleware.

Carga variables de entorno desde .env y define todos los parámetros
necesarios para conectar con Shopify, PostgreSQL y el agente local.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración centralizada del middleware de sincronización."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Shopify ---
    SHOPIFY_STORE_URL: str
    SHOPIFY_ACCESS_TOKEN: str
    SHOPIFY_API_VERSION: str = "2025-01"
    SHOPIFY_WEBHOOK_SECRET: str

    # --- Base de datos ---
    DATABASE_URL: str  # PostgreSQL connection string

    # --- Agente local ---
    AGENT_API_KEY: str

    # --- Sincronización ---
    SYNC_COOLDOWN_SECONDS: int = 30


# Singleton de configuración (se crea al importar)
settings = Settings()  # type: ignore[call-arg]
