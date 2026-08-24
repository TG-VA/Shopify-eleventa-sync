"""
Esquemas de base de datos.

Modelos Pydantic que representan filas de las tablas PostgreSQL.
Se usan para serializar/deserializar registros de la base de datos.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ProductMappingRow(BaseModel):
    """Fila de la tabla product_mapping."""

    id: int | None = None
    eleventa_codigo: str
    shopify_product_id: int | None = None
    shopify_variant_id: int
    shopify_inventory_item_id: int
    sku: str | None = None
    titulo: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SyncLogRow(BaseModel):
    """Fila de la tabla sync_log."""

    id: int | None = None
    codigo_eleventa: str
    source: Literal["eleventa", "shopify", "manual"]
    delta: float
    existencia_anterior: float | None = None
    existencia_nueva: float | None = None
    shopify_inventory_item_id: int | None = None
    status: Literal["success", "error", "skipped"] = "success"
    error_message: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PendingAdjustmentRow(BaseModel):
    """Fila de la tabla pending_adjustments."""

    id: str
    codigo_eleventa: str
    cantidad_ajuste: float
    motivo: str
    shopify_order_id: str | None = None
    status: Literal["pending", "applied", "failed"] = "pending"
    error_message: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CooldownTrackerRow(BaseModel):
    """Fila de la tabla cooldown_tracker."""

    codigo_eleventa: str
    source: Literal["eleventa", "shopify", "manual"]
    last_sync_at: datetime = Field(default_factory=datetime.utcnow)
