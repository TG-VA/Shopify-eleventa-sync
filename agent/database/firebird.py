from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Any

# firebird-driver >= 2.0 expone el paquete `firebird`; el alias `fdb` de 1.x ya no existe.
from firebird.driver import Connection, connect as fb_connect

from agent.config import settings
from agent.timestamps import format_firebird_timestamp, to_firebird_datetime

logger = logging.getLogger(__name__)

# ── Conflicto de bloqueo (Firebird 2.5/3.0) ────────────────────────────
# Eleventa sigue vendiendo mientras sincronizamos: dos transacciones sobre la
# misma fila producen "lock conflict on update" / "deadlock". Son errores
# transitorios que se resuelven en milisegundos, así que se reintentan con una
# espera breve antes de devolver fallo al worker.
LOCK_CONFLICT_MARKERS = (
    "lock conflict",
    "deadlock",
    "update conflict",
    "lock timeout",
    "conflito de bloqueo",
)
LOCK_RETRY_ATTEMPTS = 3
LOCK_RETRY_DELAY_SECONDS = 0.5


def _is_lock_conflict(error: BaseException) -> bool:
    """Indica si la excepción corresponde a un conflicto de bloqueo de Firebird."""
    message = str(error).lower()
    return any(marker in message for marker in LOCK_CONFLICT_MARKERS)


class FirebirdClient:
    """
    Cliente para interactuar con la base de datos Firebird (Eleventa).
    """

    def __init__(
        self,
        db_path: str = settings.ELEVENTA_DB_PATH,
        user: str = settings.ELEVENTA_DB_USER,
        password: str = settings.ELEVENTA_DB_PASS,
    ):
        self.db_path = db_path
        self.user = user
        self.password = password

    @contextmanager
    def get_connection(self) -> Generator[Connection, None, None]:
        """
        Context manager para obtener una conexión a la base de datos.
        Asegura que la conexión se cierre al terminar.
        """
        conn = None
        try:
            # charset='UTF8' o 'WIN1252' dependiendo de la configuración de Eleventa
            conn = fb_connect(
                dsn=self.db_path,
                user=self.user,
                password=self.password,
                charset="WIN1252",
            )
            yield conn
        except Exception as e:
            logger.error("Error conectando a Firebird: %s", e)
            raise
        finally:
            if conn is not None:
                conn.close()

    def get_product_inventory(self, sku: str) -> float | None:
        """
        Obtiene el inventario actual de un producto por su SKU (Código).
        """
        query = """
            SELECT EXISTENCIA
            FROM PRODUCTOS
            WHERE CODIGO = ?
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (sku,))
            row = cursor.fetchone()
            if row:
                return float(row[0])
            return None

    def update_inventory(
        self,
        sku: str,
        new_quantity: float,
        reason: str = "Sincronización Shopify",
    ) -> bool:
        """
        Actualiza el inventario de un producto.

        Garantiza dos cosas críticas cuando la caja está cobrando en otra
        terminal:

        1. `rollback()` explícito ante cualquier fallo antes del `commit()`,
           para liberar de inmediato los locks de fila/página que Eleventa
           bloquearía con la siguiente venta.
        2. Reintento breve ante conflictos de bloqueo transitorios
           (`lock conflict` / `deadlock`), que de otro modo abortarían el
           ajuste válido.

        Returns:
            True si la actualización quedó confirmada; False si el producto no
            existe o si el error persiste tras los reintentos.
        """
        query = """
            UPDATE PRODUCTOS
            SET EXISTENCIA = ?
            WHERE CODIGO = ?
        """

        for attempt in range(1, LOCK_RETRY_ATTEMPTS + 1):
            try:
                with self.get_connection() as conn:
                    try:
                        cursor = conn.cursor()
                        # Verificar que el producto exista primero
                        cursor.execute("SELECT ID FROM PRODUCTOS WHERE CODIGO = ?", (sku,))
                        if not cursor.fetchone():
                            logger.warning("Producto con SKU %s no encontrado en Eleventa.", sku)
                            return False

                        cursor.execute(query, (new_quantity, sku))
                        conn.commit()
                    except Exception:
                        # Rollback explícito: se ejecuta con la conexión aún
                        # abierta (el context manager la cierra después).
                        self._rollback(conn, sku)
                        raise

                logger.info("Inventario de %s actualizado a %s", sku, new_quantity)
                return True
            except Exception as e:
                if _is_lock_conflict(e) and attempt < LOCK_RETRY_ATTEMPTS:
                    delay = LOCK_RETRY_DELAY_SECONDS * attempt
                    logger.warning(
                        "Conflicto de bloqueo actualizando %s (intento %d/%d), "
                        "reintentando en %.1fs: %s",
                        sku, attempt, LOCK_RETRY_ATTEMPTS, delay, e,
                    )
                    time.sleep(delay)
                    continue

                logger.error("Error actualizando inventario para %s: %s", sku, e)
                return False

        return False

    @staticmethod
    def _rollback(conn: Connection, sku: str) -> None:
        """
        Ejecuta `rollback()` sin propagar fallos secondary.

        Una transacción abortada puede dejar la conexión en estado que impide
        el rollback; el error real ya se registra en el llamador.
        """
        try:
            conn.rollback()
        except Exception as rollback_error:
            logger.error(
                "Rollback fallido para %s: %s", sku, rollback_error
            )
        else:
            logger.debug("Rollback ejecutado para %s (locks liberados).", sku)

    def get_recent_sales(self, last_check_timestamp: str | datetime) -> list[dict[str, Any]]:
        """
        Busca ventas que hayan ocurrido en o después de `last_check_timestamp`.

        El parámetro se convierte a `datetime` nativo antes de ejecutar la
        consulta: Firebird 2.5/3.0 rechaza el separador 'T' del formato ISO.
        Se usa `>=` en lugar de `>` para no perder tickets registrados dentro
        del mismo segundo del cursor; la deduplicación por `ticket_id` en el
        worker descarta los repetidos.

        Consulta sobre VENTATICKETS y VENTATICKETS_ARTICULOS para extraer:
        - codigo (SKU del producto en Eleventa)
        - cantidad (unidades vendidas a descontar)
        - timestamp (fecha/hora de la transacción, formato Firebird)
        - ticket_id (identificador único del ticket)
        """
        sales = []

        # Normaliza el cursor a datetime naive: nunca se envía 'T' a Firebird.
        cursor_value = to_firebird_datetime(last_check_timestamp)
        logger.debug("Buscando ventas con FECHA_HORA >= %s", cursor_value)

        # Query principal sobre VENTATICKETS y VENTATICKETS_ARTICULOS
        query = """
            SELECT 
                VT.ID_TICKET AS TICKET_ID,
                COALESCE(VTA.CODIGO, P.CODIGO) AS CODIGO,
                COALESCE(VTA.CANTIDAD, 1) AS CANTIDAD,
                VT.FECHA_HORA AS TIMESTAMP
            FROM VENTATICKETS VT
            LEFT JOIN VENTATICKETS_ARTICULOS VTA ON VTA.ID_TICKET = VT.ID_TICKET
            LEFT JOIN PRODUCTOS P ON P.ID = VTA.ID_PRODUCTO OR P.CODIGO = VTA.CODIGO
            WHERE VT.FECHA_HORA >= ?
            ORDER BY VT.FECHA_HORA ASC
        """
        
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, (cursor_value,))
                rows = cursor.fetchall()
                
                for row in rows:
                    if row[1]:  # Solo si hay código de producto
                        sales.append({
                            'ticket_id': str(row[0]),
                            'codigo': str(row[1]).strip(),
                            'cantidad': float(row[2]) if row[2] else 1.0,
                            'timestamp': format_firebird_timestamp(row[3]),
                        })
        except Exception:
            logger.exception("Error al obtener ventas recientes")
            # Intentar fallback con HISTORIAL_INVENTARIO si las tablas principales no existen
            try:
                fallback_query = """
                    SELECT 
                        HI.ID AS MOVIMIENTO_ID,
                        HI.CODIGO,
                        HI.CANTIDAD,
                        HI.FECHA
                    FROM HISTORIAL_INVENTARIO HI
                    WHERE HI.FECHA >= ?
                    AND HI.CANTIDAD < 0
                    ORDER BY HI.FECHA ASC
                """
                with self.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(fallback_query, (cursor_value,))
                    rows = cursor.fetchall()
                    
                    for row in rows:
                        if row[1]:
                            sales.append({
                                'ticket_id': str(row[0]),
                                'codigo': str(row[1]).strip(),
                                'cantidad': abs(float(row[2])) if row[2] else 1.0,
                                'timestamp': format_firebird_timestamp(row[3]),
                            })
            except Exception:
                logger.exception("Error en fallback de ventas")
        
        logger.debug("Ventas recientes encontradas: %d", len(sales))
        return sales
