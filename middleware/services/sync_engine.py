"""
Motor de sincronización bidireccional con protección anti-bucle.

Coordina la sincronización de inventario entre Eleventa y Shopify,
usando cooldowns para evitar bucles infinitos de actualización.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from shared.models import InventoryChange, PendingAdjustment
from middleware.services.shopify_client import ShopifyClient
from middleware.services.product_mapper import ProductMapper

logger = logging.getLogger(__name__)


class SyncEngine:
    """Motor de sincronización bidireccional Eleventa ↔ Shopify."""

    def __init__(
        self,
        shopify_client: ShopifyClient,
        product_mapper: ProductMapper,
        pool: asyncpg.Pool,
        cooldown_seconds: int = 30,
    ) -> None:
        """
        Inicializa el motor de sincronización.

        Args:
            shopify_client: Cliente de Shopify configurado.
            product_mapper: Mapeador de productos cargado.
            pool: Pool de conexiones PostgreSQL.
            cooldown_seconds: Segundos de espera entre sincronizaciones
                              del mismo producto para evitar bucles.
        """
        self.shopify = shopify_client
        self.mapper = product_mapper
        self.pool = pool
        self.cooldown_seconds = cooldown_seconds

    # ------------------------------------------------------------------ #
    # Eleventa → Shopify
    # ------------------------------------------------------------------ #

    async def process_eleventa_changes(
        self,
        changes: list[InventoryChange],
    ) -> dict[str, Any]:
        """
        Procesa cambios de inventario de Eleventa y los aplica en Shopify.

        Verifica cooldown para cada producto antes de aplicar el cambio.
        Registra cada operación en sync_log.

        Args:
            changes: Lista de cambios de inventario desde Eleventa.

        Returns:
            Diccionario con processed (int) y errors (list[str]).
        """
        processed = 0
        errors: list[str] = []

        # Obtener primera ubicación de Shopify
        locations = await self.shopify.get_locations()
        if not locations:
            return {"processed": 0, "errors": ["No hay ubicaciones en Shopify."]}

        location_id = locations[0]["id"]

        for change in changes:
            try:
                # Verificar cooldown
                if await self.is_in_cooldown(change.codigo, "shopify"):
                    logger.info(
                        "Cambio omitido por cooldown: %s (fuente: shopify reciente).",
                        change.codigo,
                    )
                    await self._log_sync(
                        change, status="skipped", error_message="Cooldown activo"
                    )
                    continue

                # Obtener mapeo
                shopify_ids = self.mapper.get_shopify_ids(change.codigo)
                if shopify_ids is None:
                    msg = f"Sin mapeo para código: {change.codigo}"
                    logger.warning(msg)
                    errors.append(msg)
                    await self._log_sync(change, status="error", error_message=msg)
                    continue

                inventory_item_gid = (
                    f"gid://shopify/InventoryItem/"
                    f"{shopify_ids['shopify_inventory_item_id']}"
                )

                # Aplicar ajuste en Shopify
                delta = int(change.delta)
                if delta == 0:
                    logger.debug("Delta 0 para %s, omitiendo.", change.codigo)
                    continue

                await self.shopify.adjust_inventory(
                    inventory_item_id=inventory_item_gid,
                    location_id=location_id,
                    delta=delta,
                    reason="correction",
                )

                # Registrar cooldown y log
                await self.set_cooldown(change.codigo, "eleventa")
                await self._log_sync(
                    change,
                    shopify_inventory_item_id=shopify_ids[
                        "shopify_inventory_item_id"
                    ],
                )
                processed += 1

                logger.info(
                    "Sincronizado Eleventa→Shopify: %s, delta=%d",
                    change.codigo,
                    delta,
                )

            except Exception as exc:
                msg = f"Error procesando {change.codigo}: {exc}"
                logger.error(msg)
                errors.append(msg)
                await self._log_sync(change, status="error", error_message=str(exc))

        return {"processed": processed, "errors": errors}

    # ------------------------------------------------------------------ #
    # Shopify → Eleventa (crear ajustes pendientes)
    # ------------------------------------------------------------------ #

    async def process_shopify_order(
        self,
        order_data: dict[str, Any],
    ) -> list[PendingAdjustment]:
        """
        Procesa una orden de Shopify y crea ajustes pendientes para Eleventa.

        Por cada línea de la orden, crea un ajuste pendiente que el agente
        local recogerá y aplicará en la base de datos Firebird.

        Args:
            order_data: Payload del webhook orders/create de Shopify.

        Returns:
            Lista de ajustes pendientes creados.
        """
        adjustments: list[PendingAdjustment] = []
        order_id = str(order_data.get("id", ""))
        order_name = order_data.get("name", order_id)
        line_items = order_data.get("line_items", [])

        for item in line_items:
            variant_id = item.get("variant_id")
            if variant_id is None:
                continue

            codigo = self.mapper.get_eleventa_codigo(int(variant_id))
            if codigo is None:
                logger.warning(
                    "Sin mapeo para variant_id=%s en orden %s.",
                    variant_id,
                    order_name,
                )
                continue

            # Verificar cooldown
            if await self.is_in_cooldown(codigo, "eleventa"):
                logger.info(
                    "Ajuste omitido por cooldown: %s (fuente: eleventa reciente).",
                    codigo,
                )
                continue

            quantity = item.get("quantity", 0)
            adjustment_id = str(uuid.uuid4())

            adjustment = PendingAdjustment(
                id=adjustment_id,
                codigo_eleventa=codigo,
                cantidad_ajuste=-abs(quantity),  # Ventas restan inventario
                motivo=f"Venta Shopify orden {order_name}",
                shopify_order_id=order_id,
            )

            # Guardar en base de datos
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO pending_adjustments
                        (id, codigo_eleventa, cantidad_ajuste, motivo,
                         shopify_order_id, status)
                    VALUES ($1, $2, $3, $4, $5, 'pending')
                    """,
                    adjustment.id,
                    adjustment.codigo_eleventa,
                    adjustment.cantidad_ajuste,
                    adjustment.motivo,
                    adjustment.shopify_order_id,
                )

            await self.set_cooldown(codigo, "shopify")
            adjustments.append(adjustment)

            logger.info(
                "Ajuste pendiente creado: %s para %s (qty=%d).",
                adjustment_id,
                codigo,
                quantity,
            )

        return adjustments

    # ------------------------------------------------------------------ #
    # Gestión de ajustes pendientes
    # ------------------------------------------------------------------ #

    async def get_pending_adjustments(self) -> list[PendingAdjustment]:
        """
        Obtiene todos los ajustes con status 'pending'.

        Returns:
            Lista de ajustes pendientes para el agente local.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, codigo_eleventa, cantidad_ajuste, motivo,
                       shopify_order_id, status, created_at
                FROM pending_adjustments
                WHERE status = 'pending'
                ORDER BY created_at ASC
                """
            )

        return [
            PendingAdjustment(
                id=row["id"],
                codigo_eleventa=row["codigo_eleventa"],
                cantidad_ajuste=row["cantidad_ajuste"],
                motivo=row["motivo"],
                shopify_order_id=row["shopify_order_id"],
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def confirm_adjustment(
        self,
        adjustment_id: str,
        success: bool,
        message: str = "",
    ) -> bool:
        """
        Confirma que un ajuste fue aplicado (o falló) en Eleventa.

        Args:
            adjustment_id: ID del ajuste a confirmar.
            success: True si se aplicó correctamente.
            message: Mensaje adicional o detalle de error.

        Returns:
            True si se actualizó el registro, False si no se encontró.
        """
        new_status = "applied" if success else "failed"

        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE pending_adjustments
                SET status = $1, error_message = $2, updated_at = NOW()
                WHERE id = $3 AND status = 'pending'
                """,
                new_status,
                message,
                adjustment_id,
            )

        updated = result.split()[-1] != "0"  # "UPDATE N" -> verificar N > 0

        if updated:
            logger.info(
                "Ajuste %s confirmado como '%s': %s",
                adjustment_id,
                new_status,
                message,
            )
        else:
            logger.warning(
                "Ajuste %s no encontrado o ya procesado.",
                adjustment_id,
            )

        return updated

    # ------------------------------------------------------------------ #
    # Control de cooldown (anti-bucle)
    # ------------------------------------------------------------------ #

    async def is_in_cooldown(self, codigo: str, source: str) -> bool:
        """
        Verifica si un producto está en período de cooldown.

        Evita bucles de sincronización verificando si el producto fue
        sincronizado recientemente desde la fuente contraria.

        Args:
            codigo: Código del producto en Eleventa.
            source: Fuente contraria a verificar ('eleventa' o 'shopify').

        Returns:
            True si el producto está en cooldown.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self.cooldown_seconds
        )

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT last_sync_at FROM cooldown_tracker
                WHERE codigo_eleventa = $1 AND source = $2
                """,
                codigo,
                source,
            )

        if row is None:
            return False

        last_sync = row["last_sync_at"]
        # Asegurar que sea timezone-aware
        if last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)

        return last_sync > cutoff

    async def set_cooldown(self, codigo: str, source: str) -> None:
        """
        Establece el cooldown para un producto después de sincronizarlo.

        Args:
            codigo: Código del producto en Eleventa.
            source: Fuente de la sincronización actual.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO cooldown_tracker (codigo_eleventa, source, last_sync_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (codigo_eleventa, source)
                DO UPDATE SET last_sync_at = NOW()
                """,
                codigo,
                source,
            )

    # ------------------------------------------------------------------ #
    # Helpers privados
    # ------------------------------------------------------------------ #

    async def _log_sync(
        self,
        change: InventoryChange,
        status: str = "success",
        error_message: str | None = None,
        shopify_inventory_item_id: int | None = None,
    ) -> None:
        """
        Registra una operación de sincronización en sync_log.

        Args:
            change: Cambio de inventario procesado.
            status: Estado de la operación.
            error_message: Mensaje de error (si aplica).
            shopify_inventory_item_id: ID del inventory item en Shopify.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sync_log
                    (codigo_eleventa, source, delta, existencia_anterior,
                     existencia_nueva, shopify_inventory_item_id, status,
                     error_message)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                change.codigo,
                change.source,
                change.delta,
                change.existencia_anterior,
                change.existencia_nueva,
                shopify_inventory_item_id,
                status,
                error_message,
            )
