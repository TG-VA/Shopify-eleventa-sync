"""
Router para la API del agente local.

Proporciona endpoints para que el agente de sincronización local
envíe cambios de inventario, recoja ajustes pendientes y confirme
su aplicación en Eleventa.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from shared.models import (
    AdjustmentConfirmation,
    SyncRequest,
    SyncResponse,
)
from middleware.config import settings
from middleware.services.sync_engine import SyncEngine
from middleware.services.product_mapper import ProductMapper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

# Referencias inyectadas desde main.py
_sync_engine: SyncEngine | None = None
_product_mapper: ProductMapper | None = None

# Detalle único para todo fallo de autenticación: nunca se distingue entre
# "API key inexistente", "incorrecta" o "comparación fallida" para no filtrar
# información sobre la configuración del servicio.
UNAUTHORIZED_DETAIL = "No autorizado."


def configure_agent_router(
    sync_engine: SyncEngine,
    product_mapper: ProductMapper,
) -> None:
    """
    Configura el router con las dependencias necesarias.

    Args:
        sync_engine: Motor de sincronización.
        product_mapper: Mapeador de productos.
    """
    global _sync_engine, _product_mapper
    _sync_engine = sync_engine
    _product_mapper = product_mapper


async def verify_agent_key(
    x_agent_api_key: str = Header(..., alias="X-Agent-Api-Key"),
) -> str:
    """
    Verifica la autenticidad del agente mediante su API key.

    La comparación se hace con `secrets.compare_digest` (tiempo constante) en
    lugar de `==`: esta última filtra información por temporización y permite
    reconstruir la API key byte a byte. Se comparan bytes para que un header
    con caracteres no ASCII provoque un 401 en vez de un `TypeError`.

    Args:
        x_agent_api_key: API key enviada en el encabezado.

    Returns:
        La API key validada.

    Raises:
        HTTPException 401: Si la API key es inválida. El cuerpo no revela
            detalles internos ni la clave esperada.
    """
    provided_key = x_agent_api_key.encode('utf-8')
    expected_key = settings.AGENT_API_KEY.encode('utf-8')

    if not secrets.compare_digest(provided_key, expected_key):
        # Nunca se registra la clave recibida: solo el evento de rechazo.
        logger.warning("Intento de acceso con API key inválida.")
        raise HTTPException(status_code=401, detail=UNAUTHORIZED_DETAIL)
    return x_agent_api_key


@router.post("/inventory-changes", response_model=SyncResponse)
async def receive_inventory_changes(
    sync_request: SyncRequest,
    _api_key: str = Depends(verify_agent_key),
) -> SyncResponse:
    """
    Recibe cambios de inventario detectados por el agente en Eleventa.

    Procesa cada cambio y lo sincroniza con Shopify, respetando los
    cooldowns para evitar bucles.

    Args:
        sync_request: Solicitud con los cambios de inventario.

    Returns:
        Respuesta con cantidad procesada y errores encontrados.
    """
    if _sync_engine is None:
        raise HTTPException(status_code=503, detail="Servicio no inicializado.")

    logger.info(
        "Recibidos %d cambios de inventario del agente '%s'.",
        len(sync_request.changes),
        sync_request.agent_id,
    )

    result = await _sync_engine.process_eleventa_changes(sync_request.changes)

    return SyncResponse(
        processed=result["processed"],
        errors=result["errors"],
    )


@router.get("/pending-adjustments")
async def get_pending_adjustments(
    _api_key: str = Depends(verify_agent_key),
) -> dict[str, Any]:
    """
    Devuelve los ajustes pendientes para que el agente aplique en Eleventa.

    Returns:
        Lista de ajustes pendientes con sus detalles.
    """
    if _sync_engine is None:
        raise HTTPException(status_code=503, detail="Servicio no inicializado.")

    adjustments = await _sync_engine.get_pending_adjustments()

    return {
        "count": len(adjustments),
        "adjustments": [a.model_dump() for a in adjustments],
    }


@router.post("/confirm-adjustment")
async def confirm_adjustment(
    confirmation: AdjustmentConfirmation,
    _api_key: str = Depends(verify_agent_key),
) -> dict[str, Any]:
    """
    Confirma que un ajuste fue aplicado (o falló) en Eleventa.

    Args:
        confirmation: Detalles de la confirmación del ajuste.

    Returns:
        Estado de la actualización.
    """
    if _sync_engine is None:
        raise HTTPException(status_code=503, detail="Servicio no inicializado.")

    logger.info(
        "Confirmación de ajuste recibida: %s, éxito=%s",
        confirmation.adjustment_id,
        confirmation.success,
    )

    updated = await _sync_engine.confirm_adjustment(
        adjustment_id=confirmation.adjustment_id,
        success=confirmation.success,
        message=confirmation.message,
    )

    if not updated:
        raise HTTPException(
            status_code=404,
            detail=f"Ajuste '{confirmation.adjustment_id}' no encontrado o ya procesado.",
        )

    return {
        "status": "ok",
        "adjustment_id": confirmation.adjustment_id,
        "new_status": "applied" if confirmation.success else "failed",
    }

@router.get("/product-mappings")
async def get_product_mappings(
    _api_key: str = Depends(verify_agent_key),
) -> dict[str, Any]:
    """
    Devuelve todos los mapeos de productos Eleventa↔Shopify.
    """
    if _product_mapper is None:
        raise HTTPException(status_code=503, detail="Servicio no inicializado.")

    mappings = _product_mapper._by_eleventa

    return {
        "count": len(mappings),
        "mappings": {
            codigo: {
                "shopify_product_id": data.get("shopify_product_id"),
                "shopify_variant_id": data.get("shopify_variant_id"),
                "shopify_inventory_item_id": data.get("shopify_inventory_item_id"),
                "sku": data.get("sku"),
                "titulo": data.get("titulo"),
            }
            for codigo, data in mappings.items()
        },
    }
