"""
Migraciones de base de datos.

Crea las tablas necesarias para el funcionamiento del middleware:
product_mapping, sync_log, pending_adjustments y cooldown_tracker.
Usa CREATE IF NOT EXISTS para ser idempotente.
"""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)

SQL_CREATE_TABLES = """
-- Mapeo entre códigos de Eleventa y IDs de Shopify
CREATE TABLE IF NOT EXISTS product_mapping (
    id              SERIAL PRIMARY KEY,
    eleventa_codigo TEXT NOT NULL UNIQUE,
    shopify_product_id   BIGINT,
    shopify_variant_id   BIGINT NOT NULL,
    shopify_inventory_item_id BIGINT NOT NULL,
    sku             TEXT,
    titulo          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pm_eleventa_codigo
    ON product_mapping (eleventa_codigo);
CREATE INDEX IF NOT EXISTS idx_pm_shopify_variant
    ON product_mapping (shopify_variant_id);

-- Registro de sincronizaciones realizadas
CREATE TABLE IF NOT EXISTS sync_log (
    id              SERIAL PRIMARY KEY,
    codigo_eleventa TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('eleventa', 'shopify', 'manual')),
    delta           DOUBLE PRECISION NOT NULL,
    existencia_anterior DOUBLE PRECISION,
    existencia_nueva    DOUBLE PRECISION,
    shopify_inventory_item_id BIGINT,
    status          TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'error', 'skipped')),
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sl_codigo ON sync_log (codigo_eleventa);
CREATE INDEX IF NOT EXISTS idx_sl_created ON sync_log (created_at);

-- Ajustes pendientes para que el agente local aplique en Eleventa
CREATE TABLE IF NOT EXISTS pending_adjustments (
    id              TEXT PRIMARY KEY,
    codigo_eleventa TEXT NOT NULL,
    cantidad_ajuste DOUBLE PRECISION NOT NULL,
    motivo          TEXT NOT NULL,
    shopify_order_id TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'applied', 'failed')),
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pa_status ON pending_adjustments (status);
CREATE INDEX IF NOT EXISTS idx_pa_codigo ON pending_adjustments (codigo_eleventa);

-- Control de cooldown para evitar bucles de sincronización
CREATE TABLE IF NOT EXISTS cooldown_tracker (
    codigo_eleventa TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('eleventa', 'shopify', 'manual')),
    last_sync_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (codigo_eleventa, source)
);
"""


async def run_migrations(pool: asyncpg.Pool) -> None:
    """
    Ejecuta las migraciones de base de datos.

    Crea todas las tablas necesarias si no existen. Es seguro ejecutar
    múltiples veces gracias a IF NOT EXISTS.

    Args:
        pool: Pool de conexiones asyncpg.
    """
    logger.info("Ejecutando migraciones de base de datos...")

    async with pool.acquire() as conn:
        await conn.execute(SQL_CREATE_TABLES)

    logger.info("Migraciones completadas exitosamente.")
