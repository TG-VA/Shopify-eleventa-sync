"""
Modelos compartidos entre el agente local y el middleware en la nube.

Estos modelos Pydantic v2 definen el contrato de datos para la sincronización
de inventario entre Shopify y Eleventa.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ProductInventory(BaseModel):
    """Representa el inventario actual de un producto en Eleventa."""

    codigo: str = Field(..., description="Código único del producto en Eleventa")
    descripcion: str = Field(..., description="Descripción del producto")
    existencia: float = Field(..., description="Cantidad en existencia")
    precio: float = Field(..., description="Precio de venta")


class InventoryChange(BaseModel):
    """Registra un cambio de inventario detectado en cualquier fuente."""

    codigo: str = Field(..., description="Código del producto en Eleventa")
    existencia_anterior: float = Field(..., description="Existencia antes del cambio")
    existencia_nueva: float = Field(..., description="Existencia después del cambio")
    delta: float = Field(..., description="Diferencia (nueva - anterior)")
    source: Literal["eleventa", "shopify", "manual"] = Field(
        ..., description="Origen del cambio"
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="Momento en que se detectó el cambio",
    )


class SyncRequest(BaseModel):
    """Solicitud de sincronización enviada por el agente local al middleware."""

    changes: list[InventoryChange] = Field(
        ..., description="Lista de cambios de inventario detectados"
    )
    agent_id: str = Field(..., description="Identificador único del agente local")


class SyncResponse(BaseModel):
    """Respuesta del middleware al agente tras procesar cambios."""

    processed: int = Field(..., description="Cantidad de cambios procesados exitosamente")
    errors: list[str] = Field(
        default_factory=list, description="Lista de errores encontrados"
    )


class PendingAdjustment(BaseModel):
    """Ajuste pendiente que el agente local debe aplicar en Eleventa."""

    id: str = Field(..., description="Identificador único del ajuste")
    codigo_eleventa: str = Field(..., description="Código del producto en Eleventa")
    cantidad_ajuste: float = Field(
        ..., description="Cantidad a ajustar (negativo = reducción)"
    )
    motivo: str = Field(..., description="Razón del ajuste")
    shopify_order_id: str | None = Field(
        None, description="ID de la orden de Shopify que originó el ajuste"
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Fecha de creación del ajuste",
    )
    status: Literal["pending", "applied", "failed"] = Field(
        default="pending", description="Estado actual del ajuste"
    )


class AdjustmentConfirmation(BaseModel):
    """Confirmación del agente sobre la aplicación de un ajuste en Eleventa."""

    adjustment_id: str = Field(..., description="ID del ajuste confirmado")
    success: bool = Field(..., description="Si el ajuste se aplicó correctamente")
    message: str = Field(default="", description="Mensaje adicional o detalle de error")
