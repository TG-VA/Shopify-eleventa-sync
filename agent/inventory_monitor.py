"""
Monitor principal del inventario.

Orquesta el ciclo completo de sincronización:
    1. Leer inventario actual de Eleventa (Firebird)
    2. Detectar cambios contra el snapshot anterior (SQLite)
    3. Enviar cambios al middleware (HTTP)
    4. Consultar ajustes pendientes desde Shopify
    5. Aplicar ajustes en Eleventa
    6. Confirmar ajustes al middleware
    7. Actualizar snapshot local
"""

import logging
import time
from datetime import datetime, timezone

from agent.config import settings
from agent.eleventa_reader import EleventaReader
from agent.eleventa_writer import EleventaWriter
from agent.snapshot_db import SnapshotDB
from agent.middleware_client import MiddlewareClient
from shared.models import AdjustmentConfirmation, PendingAdjustment

logger = logging.getLogger(__name__)


class InventoryMonitor:
    """
    Monitor de sincronización bidireccional de inventario.

    Ejecuta ciclos periódicos de lectura, detección de cambios,
    comunicación con el middleware, y aplicación de ajustes.
    """

    def __init__(
        self,
        reader: EleventaReader,
        writer: EleventaWriter,
        snapshot_db: SnapshotDB,
        middleware: MiddlewareClient,
    ) -> None:
        """
        Inicializa el monitor con todos sus componentes.

        Args:
            reader: Lector de la base de datos de Eleventa.
            writer: Escritor de la base de datos de Eleventa.
            snapshot_db: Base de datos local de snapshots.
            middleware: Cliente HTTP del middleware.
        """
        self._reader = reader
        self._writer = writer
        self._snapshot_db = snapshot_db
        self._middleware = middleware
        self._cycle_count = 0

        logger.info(
            "InventoryMonitor inicializado — intervalo: %ds",
            settings.POLLING_INTERVAL_SECONDS,
        )

    # ── Ciclo principal ─────────────────────────────────────────────────

    def run_cycle(self) -> None:
        """
        Ejecuta un ciclo completo de sincronización.

        Pasos:
            1. Leer inventario actual de Eleventa
            2. Detectar cambios contra el snapshot previo
            3. Enviar cambios al middleware (si los hay)
            4. Consultar y aplicar ajustes pendientes de Shopify
            5. Actualizar el snapshot local
        """
        self._cycle_count += 1
        cycle_start = time.monotonic()
        logger.info(
            "═══ Ciclo de sincronización #%d iniciado ═══", self._cycle_count
        )

        try:
            # Paso 1: Leer inventario actual de Eleventa
            logger.info("Paso 1/5: Leyendo inventario de Eleventa...")
            current_products = self._reader.get_all_products()
            logger.info("  → %d productos leídos.", len(current_products))

            # Paso 2: Detectar cambios contra el snapshot anterior
            logger.info("Paso 2/5: Detectando cambios...")
            changes = self._snapshot_db.detect_changes(current_products)

            # Paso 3: Enviar cambios al middleware (si los hay)
            if changes:
                logger.info(
                    "Paso 3/5: Enviando %d cambios al middleware...",
                    len(changes),
                )
                try:
                    self._middleware.send_inventory_changes(changes)
                except Exception as exc:
                    logger.error(
                        "Error al enviar cambios al middleware: %s. "
                        "Los cambios se reintentarán en el próximo ciclo.",
                        exc,
                    )
            else:
                logger.info("Paso 3/5: Sin cambios para enviar.")

            # Paso 4: Consultar y aplicar ajustes pendientes de Shopify
            logger.info("Paso 4/5: Consultando ajustes pendientes...")
            self._apply_pending_adjustments()

            # Paso 5: Actualizar snapshot local
            logger.info("Paso 5/5: Actualizando snapshot local...")
            # Re-leer después de aplicar ajustes para capturar el estado final
            updated_products = self._reader.get_all_products()
            self._snapshot_db.save_snapshot(updated_products)

            elapsed = time.monotonic() - cycle_start
            logger.info(
                "═══ Ciclo #%d completado en %.2f segundos ═══",
                self._cycle_count,
                elapsed,
            )

        except Exception as exc:
            elapsed = time.monotonic() - cycle_start
            logger.error(
                "═══ Error en ciclo #%d (%.2f s): %s ═══",
                self._cycle_count,
                elapsed,
                exc,
            )

    # ── Aplicación de ajustes pendientes ────────────────────────────────

    def _apply_pending_adjustments(self) -> None:
        """
        Consulta ajustes pendientes del middleware y los aplica en Eleventa.

        Para cada ajuste:
            1. Intenta aplicar el cambio en la base de datos de Eleventa.
            2. Confirma o reporta el error al middleware.
        """
        try:
            adjustments = self._middleware.get_pending_adjustments()
        except Exception as exc:
            logger.error(
                "Error al consultar ajustes pendientes: %s", exc
            )
            return

        if not adjustments:
            logger.debug("No hay ajustes pendientes para aplicar.")
            return

        logger.info(
            "Procesando %d ajustes pendientes de Shopify...",
            len(adjustments),
        )

        for adjustment in adjustments:
            self._apply_single_adjustment(adjustment)

    def _apply_single_adjustment(self, adjustment: PendingAdjustment) -> None:
        """
        Aplica un ajuste individual en Eleventa y confirma al middleware.

        Args:
            adjustment: Ajuste pendiente a aplicar.
        """
        now = datetime.now(timezone.utc).isoformat()

        try:
            # Determinar tipo de ajuste y aplicar
            if adjustment.adjustment_type == "set":
                new_stock = self._writer.set_inventory(
                    adjustment.codigo, adjustment.quantity
                )
            else:
                # Por defecto, tratar como ajuste relativo (delta)
                new_stock = self._writer.adjust_inventory(
                    adjustment.codigo, adjustment.quantity
                )

            # Confirmar ajuste exitoso al middleware
            confirmation = AdjustmentConfirmation(
                adjustment_id=adjustment.adjustment_id,
                agent_id=settings.AGENT_ID,
                codigo=adjustment.codigo,
                applied=True,
                new_stock=new_stock,
                timestamp=now,
                error=None,
            )
            self._middleware.confirm_adjustment(confirmation)

            logger.info(
                "✅ Ajuste aplicado — ID: %s | Código: %s | "
                "Nueva existencia: %.2f",
                adjustment.adjustment_id,
                adjustment.codigo,
                new_stock,
            )

        except Exception as exc:
            # Reportar error al middleware
            logger.error(
                "❌ Error al aplicar ajuste %s para '%s': %s",
                adjustment.adjustment_id,
                adjustment.codigo,
                exc,
            )

            confirmation = AdjustmentConfirmation(
                adjustment_id=adjustment.adjustment_id,
                agent_id=settings.AGENT_ID,
                codigo=adjustment.codigo,
                applied=False,
                new_stock=None,
                timestamp=now,
                error=str(exc),
            )

            try:
                self._middleware.confirm_adjustment(confirmation)
            except Exception as confirm_exc:
                logger.error(
                    "Error al reportar fallo de ajuste %s: %s",
                    adjustment.adjustment_id,
                    confirm_exc,
                )

    # ── Loop principal ──────────────────────────────────────────────────

    def start(self) -> None:
        """
        Inicia el loop de monitoreo continuo.

        Ejecuta ciclos de sincronización con el intervalo configurado.
        Se detiene limpiamente con Ctrl+C (KeyboardInterrupt).
        """
        logger.info(
            "Iniciando monitor de inventario — intervalo: %d segundos",
            settings.POLLING_INTERVAL_SECONDS,
        )

        try:
            while True:
                self.run_cycle()

                logger.info(
                    "Esperando %d segundos hasta el próximo ciclo...",
                    settings.POLLING_INTERVAL_SECONDS,
                )
                time.sleep(settings.POLLING_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info(
                "\n🛑 Monitoreo detenido por el usuario (Ctrl+C). "
                "Ciclos completados: %d",
                self._cycle_count,
            )
