"""
Router para webhooks de Shopify.

Recibe y procesa webhooks de Shopify (órdenes, actualizaciones de inventario).
Incluye verificación HMAC para seguridad.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from middleware.services.sync_engine import SyncEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Referencia al sync engine (se inyecta desde main.py via app.state)
_sync_engine: SyncEngine | None = None
_webhook_secret: str = ""


def configure_webhooks(sync_engine: SyncEngine, webhook_secret: str) -> None:
    """
    Configura el router con las dependencias necesarias.

    Args:
        sync_engine: Motor de sincronización.
        webhook_secret: Secreto para verificar webhooks de Shopify.
    """
    global _sync_engine, _webhook_secret
    _sync_engine = sync_engine
    _webhook_secret = webhook_secret


def verify_shopify_webhook(data: bytes, hmac_header: str, secret: str) -> bool:
    """
    Verifica la autenticidad de un webhook de Shopify usando HMAC-SHA256.

    `data` debe ser el cuerpo CRUDO de la petición (`await request.body()`),
    nunca el JSON ya parseado: cualquier reserialización cambia bytes y
    rompe la firma. La comparación usa `hmac.compare_digest`, de tiempo
    constante, para no filtrar la firma por temporización.

    Args:
        data: Cuerpo crudo de la petición.
        hmac_header: Valor del encabezado X-Shopify-Hmac-Sha256.
        secret: Secreto del webhook configurado en Shopify.

    Returns:
        True si el HMAC es válido, False en caso contrario (incluye headers
        ausentes o malformados).
    """
    if not data or not hmac_header or not secret:
        logger.warning("Verificación de webhook rechazada: payload o header vacío.")
        return False

    computed_hmac = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            data,
            hashlib.sha256,
        ).digest()
    )

    try:
        # Se comparan bytes: compare_digest lanza TypeError con caracteres no
        # ASCII en str, y un TypeError en un header hostil no debe ser un 500.
        return hmac.compare_digest(computed_hmac, hmac_header.encode("utf-8"))
    except (AttributeError, TypeError) as exc:
        logger.warning("Header HMAC malformado: %s", exc)
        return False


@router.post("/orders-create")
async def handle_order_create(
    request: Request,
    x_shopify_hmac_sha256: str = Header(..., alias="X-Shopify-Hmac-Sha256"),
    x_shopify_topic: str = Header(None, alias="X-Shopify-Topic"),
) -> dict[str, Any]:
    """
    Procesa el webhook orders/create de Shopify.

    Verifica HMAC, extrae los line items de la orden y crea ajustes
    pendientes para que el agente los aplique en Eleventa.

    Args:
        request: Petición HTTP con el payload del webhook.
        x_shopify_hmac_sha256: HMAC para verificación.
        x_shopify_topic: Tipo de evento del webhook.

    Returns:
        Resumen de ajustes creados.
    """
    if _sync_engine is None:
        raise HTTPException(status_code=503, detail="Servicio no inicializado.")

    # Verificar HMAC sobre el cuerpo CRUDO: `request.body()` cachea los bytes
    # originales, así que el `request.json()` posterior no los altera.
    body = await request.body()
    if not verify_shopify_webhook(body, x_shopify_hmac_sha256, _webhook_secret):
        logger.warning("Webhook rechazado: HMAC inválido.")
        raise HTTPException(status_code=401, detail="HMAC inválido.")

    # Procesar la orden
    order_data = await request.json()
    order_name = order_data.get("name", order_data.get("id", "desconocida"))

    logger.info("Webhook recibido: orders/create - Orden %s", order_name)

    try:
        adjustments = await _sync_engine.process_shopify_order(order_data)
        return {
            "status": "ok",
            "order": order_name,
            "adjustments_created": len(adjustments),
            "adjustment_ids": [a.id for a in adjustments],
        }
    except Exception as exc:
        logger.error("Error procesando orden %s: %s", order_name, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Error procesando orden: {exc}",
        ) from exc


@router.post("/inventory-update")
async def handle_inventory_update(
    request: Request,
    x_shopify_hmac_sha256: str = Header(..., alias="X-Shopify-Hmac-Sha256"),
    x_shopify_topic: str = Header(None, alias="X-Shopify-Topic"),
) -> dict[str, Any]:
    """
    Procesa el webhook inventory_levels/update de Shopify.

    Registra la actualización de inventario para auditoría. No genera
    ajustes pendientes directamente (esos se generan desde orders/create).

    Args:
        request: Petición HTTP con el payload del webhook.
        x_shopify_hmac_sha256: HMAC para verificación.
        x_shopify_topic: Tipo de evento del webhook.

    Returns:
        Confirmación de recepción.
    """
    if _sync_engine is None:
        raise HTTPException(status_code=503, detail="Servicio no inicializado.")

    # Verificar HMAC sobre el cuerpo CRUDO: `request.body()` cachea los bytes
    # originales, así que el `request.json()` posterior no los altera.
    body = await request.body()
    if not verify_shopify_webhook(body, x_shopify_hmac_sha256, _webhook_secret):
        logger.warning("Webhook rechazado: HMAC inválido.")
        raise HTTPException(status_code=401, detail="HMAC inválido.")

    inventory_data = await request.json()
    inventory_item_id = inventory_data.get("inventory_item_id")
    available = inventory_data.get("available")
    location_id = inventory_data.get("location_id")

    logger.info(
        "Webhook recibido: inventory_levels/update - item=%s, available=%s, location=%s",
        inventory_item_id,
        available,
        location_id,
    )

    # Por ahora solo registramos; la lógica de sync se maneja via orders/create
    return {
        "status": "ok",
        "inventory_item_id": inventory_item_id,
        "available": available,
        "message": "Actualización de inventario registrada.",
    }
