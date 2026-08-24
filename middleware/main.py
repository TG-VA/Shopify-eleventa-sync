"""
Aplicación FastAPI principal del middleware de sincronización.

Punto de entrada del servidor que coordina la sincronización bidireccional
de inventario entre Shopify y Eleventa. Ejecuta migraciones, inicializa
conexiones y carga mapeos al arrancar.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from middleware.config import settings
from middleware.database.connection import get_pool, close_pool
from middleware.database.migrations import run_migrations
from middleware.services.shopify_client import ShopifyClient
from middleware.services.product_mapper import ProductMapper
from middleware.services.sync_engine import SyncEngine
from middleware.routers.webhooks import router as webhooks_router, configure_webhooks
from middleware.routers.agent import router as agent_router, configure_agent_router

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Gestiona el ciclo de vida de la aplicación.

    Al iniciar:
    1. Crea el pool de conexiones PostgreSQL.
    2. Ejecuta migraciones de base de datos.
    3. Inicializa el cliente de Shopify.
    4. Carga mapeos de productos.
    5. Configura el motor de sincronización.
    6. Inyecta dependencias en los routers.

    Al cerrar:
    1. Cierra el cliente de Shopify.
    2. Cierra el pool de PostgreSQL.
    """
    logger.info("🚀 Iniciando middleware de sincronización Shopify-Eleventa...")

    # 1. Pool de conexiones
    pool = await get_pool()

    # 2. Migraciones
    await run_migrations(pool)

    # 3. Cliente de Shopify
    shopify_client = ShopifyClient(
        store_url=settings.SHOPIFY_STORE_URL,
        access_token=settings.SHOPIFY_ACCESS_TOKEN,
        api_version=settings.SHOPIFY_API_VERSION,
    )

    # 4. Mapeador de productos
    product_mapper = ProductMapper()
    await product_mapper.load_mappings(pool)

    # 5. Motor de sincronización
    sync_engine = SyncEngine(
        shopify_client=shopify_client,
        product_mapper=product_mapper,
        pool=pool,
        cooldown_seconds=settings.SYNC_COOLDOWN_SECONDS,
    )

    # 6. Configurar routers
    configure_webhooks(sync_engine, settings.SHOPIFY_WEBHOOK_SECRET)
    configure_agent_router(sync_engine, product_mapper)

    # Guardar referencias en app.state para acceso directo
    app.state.pool = pool
    app.state.shopify_client = shopify_client
    app.state.product_mapper = product_mapper
    app.state.sync_engine = sync_engine

    logger.info("✅ Middleware inicializado correctamente.")

    yield

    # Limpieza al cerrar
    logger.info("🛑 Cerrando middleware...")
    await shopify_client.close()
    await close_pool()
    logger.info("👋 Middleware cerrado.")


# ------------------------------------------------------------------ #
# Crear aplicación FastAPI
# ------------------------------------------------------------------ #

app = FastAPI(
    title="Shopify-Eleventa Sync Middleware",
    description=(
        "Middleware de sincronización bidireccional de inventario "
        "entre Shopify y Eleventa (POS mexicano con Firebird DB)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS para acceso desde dashboards o herramientas de monitoreo
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registrar routers
app.include_router(webhooks_router)
app.include_router(agent_router)


# ------------------------------------------------------------------ #
# Endpoints de utilidad
# ------------------------------------------------------------------ #


@app.get("/health")
async def health_check() -> dict[str, str]:
    """
    Verificación de salud del servicio.

    Returns:
        Estado del servicio y timestamp.
    """
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/status")
async def service_status() -> dict:
    """
    Estado detallado del servicio incluyendo conteos de base de datos.

    Returns:
        Información detallada del estado del middleware.
    """
    pool = app.state.pool

    async with pool.acquire() as conn:
        mappings_count = await conn.fetchval(
            "SELECT COUNT(*) FROM product_mapping"
        )
        pending_count = await conn.fetchval(
            "SELECT COUNT(*) FROM pending_adjustments WHERE status = 'pending'"
        )
        syncs_today = await conn.fetchval(
            """
            SELECT COUNT(*) FROM sync_log
            WHERE created_at >= CURRENT_DATE
            """
        )
        errors_today = await conn.fetchval(
            """
            SELECT COUNT(*) FROM sync_log
            WHERE created_at >= CURRENT_DATE AND status = 'error'
            """
        )

    return {
        "status": "running",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": "connected",
        "stats": {
            "product_mappings": mappings_count,
            "pending_adjustments": pending_count,
            "syncs_today": syncs_today,
            "errors_today": errors_today,
        },
        "config": {
            "shopify_store": settings.SHOPIFY_STORE_URL,
            "api_version": settings.SHOPIFY_API_VERSION,
            "cooldown_seconds": settings.SYNC_COOLDOWN_SECONDS,
        },
    }
