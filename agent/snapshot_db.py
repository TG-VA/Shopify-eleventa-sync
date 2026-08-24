"""
Base de datos local de snapshots de inventario (SQLite).

Almacena el último estado conocido del inventario de Eleventa
para detectar cambios entre ciclos de sincronización sin
depender de triggers o logs de la base de datos Firebird.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from shared.models import ProductInventory, InventoryChange

logger = logging.getLogger(__name__)


class SnapshotDB:
    """
    Base de datos SQLite local para almacenar snapshots de inventario.

    Compara el estado actual de Eleventa contra el último snapshot
    conocido para detectar cambios (ventas, entradas, ajustes manuales).
    """

    def __init__(self, db_path: str) -> None:
        """
        Inicializa la conexión a la base de datos de snapshots.

        Args:
            db_path: Ruta al archivo SQLite de snapshots.
        """
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._initialize()

    def _initialize(self) -> None:
        """Crea la tabla de snapshots si no existe."""
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")  # Mejor rendimiento
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory_snapshot (
                codigo       TEXT PRIMARY KEY,
                descripcion  TEXT NOT NULL DEFAULT '',
                existencia   REAL NOT NULL DEFAULT 0,
                precio       REAL NOT NULL DEFAULT 0,
                last_updated TEXT NOT NULL
            )
            """
        )
        self._conn.commit()
        logger.info("SnapshotDB inicializada en: %s", self._db_path)

    # ── Operaciones de snapshot ──────────────────────────────────────────

    def save_snapshot(self, products: list[ProductInventory]) -> int:
        """
        Guarda o actualiza el snapshot completo de inventario.

        Utiliza INSERT OR REPLACE para actualizar productos existentes
        y agregar nuevos en una sola operación.

        Args:
            products: Lista de productos con su estado actual.

        Returns:
            Número de productos guardados/actualizados.
        """
        if self._conn is None:
            raise RuntimeError("SnapshotDB no está conectada.")

        now = datetime.now(timezone.utc).isoformat()

        self._conn.executemany(
            """
            INSERT OR REPLACE INTO inventory_snapshot
                (codigo, descripcion, existencia, precio, last_updated)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (p.codigo, p.descripcion, p.existencia, p.precio, now)
                for p in products
            ],
        )
        self._conn.commit()

        logger.info("Snapshot guardado: %d productos actualizados.", len(products))
        return len(products)

    def get_snapshot(self) -> list[ProductInventory]:
        """
        Recupera el snapshot completo almacenado.

        Returns:
            Lista de ProductInventory con el último estado conocido.
        """
        if self._conn is None:
            raise RuntimeError("SnapshotDB no está conectada.")

        cursor = self._conn.execute(
            "SELECT codigo, descripcion, existencia, precio FROM inventory_snapshot"
        )
        rows = cursor.fetchall()

        products = [
            ProductInventory(
                eleventa_id="",  # No se almacena en el snapshot
                codigo=row[0],
                descripcion=row[1],
                existencia=row[2],
                precio=row[3],
            )
            for row in rows
        ]

        logger.debug("Snapshot cargado: %d productos.", len(products))
        return products

    def detect_changes(
        self, current_products: list[ProductInventory]
    ) -> list[InventoryChange]:
        """
        Detecta cambios comparando el estado actual contra el snapshot previo.

        Identifica tres tipos de cambios:
            - Cambios de existencia (ventas, entradas, ajustes)
            - Productos nuevos (no existían en el snapshot anterior)
            - Cambios de precio

        Args:
            current_products: Lista de productos con el estado actual de Eleventa.

        Returns:
            Lista de InventoryChange con los cambios detectados.
        """
        if self._conn is None:
            raise RuntimeError("SnapshotDB no está conectada.")

        # Cargar snapshot anterior como diccionario {codigo: (existencia, precio)}
        cursor = self._conn.execute(
            "SELECT codigo, existencia, precio FROM inventory_snapshot"
        )
        snapshot_map: dict[str, tuple[float, float]] = {
            row[0]: (row[1], row[2]) for row in cursor.fetchall()
        }

        changes: list[InventoryChange] = []
        now = datetime.now(timezone.utc).isoformat()

        for product in current_products:
            prev = snapshot_map.get(product.codigo)

            if prev is None:
                # Producto nuevo — no existía en el snapshot anterior
                changes.append(
                    InventoryChange(
                        codigo=product.codigo,
                        descripcion=product.descripcion,
                        existencia_anterior=0.0,
                        existencia_nueva=product.existencia,
                        delta=product.existencia,
                        precio=product.precio,
                        timestamp=now,
                        source="eleventa",
                    )
                )
                logger.debug(
                    "Producto nuevo detectado: %s (%s) — existencia: %.2f",
                    product.codigo,
                    product.descripcion,
                    product.existencia,
                )
            else:
                prev_existencia, prev_precio = prev

                # Detectar cambio de existencia
                if abs(product.existencia - prev_existencia) > 0.001:
                    delta = product.existencia - prev_existencia
                    changes.append(
                        InventoryChange(
                            codigo=product.codigo,
                            descripcion=product.descripcion,
                            existencia_anterior=prev_existencia,
                            existencia_nueva=product.existencia,
                            delta=delta,
                            precio=product.precio,
                            timestamp=now,
                            source="eleventa",
                        )
                    )
                    logger.debug(
                        "Cambio detectado: %s | %.2f → %.2f (delta: %+.2f)",
                        product.codigo,
                        prev_existencia,
                        product.existencia,
                        delta,
                    )

        if changes:
            logger.info(
                "Detección completada: %d cambios encontrados.", len(changes)
            )
        else:
            logger.debug("Sin cambios detectados en este ciclo.")

        return changes

    # ── Ciclo de vida ───────────────────────────────────────────────────

    def close(self) -> None:
        """Cierra la conexión a la base de datos SQLite."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.info("SnapshotDB cerrada.")
