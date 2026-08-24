"""
Escritor de la base de datos Firebird de Eleventa.

╔══════════════════════════════════════════════════════════════════════╗
║  ⚠️  ¡ADVERTENCIA CRÍTICA — RIESGO DE DATOS!  ⚠️                   ║
║                                                                      ║
║  Este módulo ESCRIBE DIRECTAMENTE en la base de datos de Eleventa.  ║
║  Una operación incorrecta puede causar:                              ║
║                                                                      ║
║    • Inventario incorrecto en el punto de venta                      ║
║    • Discrepancias contables / fiscales                              ║
║    • Pérdida de datos irrecuperable                                  ║
║                                                                      ║
║  RECOMENDACIONES:                                                    ║
║    1. SIEMPRE respaldar PDVDATA.FDB antes de activar escrituras.    ║
║    2. Probar primero con ELEVENTA_WRITE_MODE=False (solo lectura).  ║
║    3. Validar cambios con un subconjunto pequeño de productos.      ║
║    4. Monitorear logs después de cada ciclo de sincronización.      ║
║                                                                      ║
║  El flag ELEVENTA_WRITE_MODE debe ser True para permitir cambios.   ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import logging
from typing import Optional

from firebird.driver import connect, Connection, DatabaseError

from agent.config import settings

logger = logging.getLogger(__name__)

# ── Consultas SQL de escritura ──────────────────────────────────────────

_SQL_GET_STOCK = """
    SELECT p.EXISTENCIA
    FROM PRODUCTOS p
    WHERE p.CODIGO = ?
      AND p.ELIMINADO_EN IS NULL
"""

_SQL_UPDATE_STOCK = """
    UPDATE PRODUCTOS
    SET EXISTENCIA = ?
    WHERE CODIGO = ?
      AND ELIMINADO_EN IS NULL
"""


class EleventaWriter:
    """
    Cliente de escritura para la base de datos Firebird de Eleventa.

    ⚠️  PRECAUCIÓN: Este módulo modifica datos en producción.
    Solo opera si ELEVENTA_WRITE_MODE está habilitado en la configuración.

    Características:
        - Verificación de modo de escritura antes de cada operación.
        - Validación de existencia mínima (no permite negativos).
        - Rollback automático ante errores.
        - Logging detallado de cada operación.
    """

    def __init__(self) -> None:
        self._conn: Optional[Connection] = None

        if settings.ELEVENTA_WRITE_MODE:
            logger.warning(
                "⚠️  EleventaWriter inicializado en MODO ESCRITURA. "
                "Los cambios se aplicarán directamente a la base de datos."
            )
        else:
            logger.info(
                "EleventaWriter inicializado en MODO SOLO LECTURA. "
                "Las escrituras serán bloqueadas."
            )

    # ── Conexión ────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Establece conexión con la base de datos Firebird para escritura."""
        if self._conn is not None:
            return

        try:
            dsn = (
                f"{settings.ELEVENTA_DB_HOST}/{settings.ELEVENTA_DB_PORT}"
                f":{settings.ELEVENTA_DB_PATH}"
            )
            self._conn = connect(
                dsn,
                user=settings.ELEVENTA_DB_USER,
                password=settings.ELEVENTA_DB_PASSWORD,
            )
            logger.info("Conexión de escritura a Firebird establecida.")
        except DatabaseError as exc:
            logger.error("Error al conectar (escritura) con Firebird: %s", exc)
            self._conn = None
            raise

    def disconnect(self) -> None:
        """Cierra la conexión de escritura con Firebird."""
        if self._conn is not None:
            try:
                self._conn.close()
                logger.info("Conexión de escritura a Firebird cerrada.")
            except DatabaseError as exc:
                logger.warning("Error al cerrar conexión de escritura: %s", exc)
            finally:
                self._conn = None

    def _ensure_connection(self) -> Connection:
        """Garantiza una conexión activa para escritura."""
        if self._conn is None:
            self.connect()
        return self._conn  # type: ignore[return-value]

    def _check_write_mode(self) -> None:
        """
        Verifica que el modo de escritura esté habilitado.

        Raises:
            PermissionError: Si ELEVENTA_WRITE_MODE es False.
        """
        if not settings.ELEVENTA_WRITE_MODE:
            msg = (
                "Escritura bloqueada: ELEVENTA_WRITE_MODE está desactivado. "
                "Active esta opción en .env o variables de entorno para "
                "permitir modificaciones a la base de datos de Eleventa."
            )
            logger.error(msg)
            raise PermissionError(msg)

    # ── Operaciones de escritura ────────────────────────────────────────

    def adjust_inventory(self, codigo: str, delta: float) -> float:
        """
        Ajusta el inventario de un producto sumando/restando una cantidad.

        ⚠️  Esta operación MODIFICA la base de datos de Eleventa.

        Args:
            codigo: Código de barras / SKU del producto.
            delta: Cantidad a ajustar (positivo = entrada, negativo = salida).

        Returns:
            La nueva existencia después del ajuste.

        Raises:
            PermissionError: Si ELEVENTA_WRITE_MODE es False.
            ValueError: Si el producto no existe en Eleventa.
            DatabaseError: Si hay un error en la operación SQL.
        """
        self._check_write_mode()
        conn = self._ensure_connection()

        try:
            cur = conn.cursor()

            # 1. Verificar existencia actual del producto
            cur.execute(_SQL_GET_STOCK, (codigo,))
            row = cur.fetchone()

            if row is None:
                cur.close()
                raise ValueError(
                    f"Producto con código '{codigo}' no encontrado en Eleventa."
                )

            existencia_actual = float(row[0]) if row[0] is not None else 0.0

            # 2. Calcular nueva existencia (mínimo 0, no se permiten negativos)
            nueva_existencia = max(0.0, existencia_actual + delta)

            if existencia_actual + delta < 0:
                logger.warning(
                    "⚠️  Ajuste para '%s' resultaría en negativo "
                    "(actual: %.2f, delta: %.2f). Se ajusta a 0.",
                    codigo,
                    existencia_actual,
                    delta,
                )

            # 3. Aplicar actualización
            cur.execute(_SQL_UPDATE_STOCK, (nueva_existencia, codigo))
            conn.commit()
            cur.close()

            logger.info(
                "✅ Inventario ajustado — Código: %s | Anterior: %.2f | "
                "Delta: %+.2f | Nuevo: %.2f",
                codigo,
                existencia_actual,
                delta,
                nueva_existencia,
            )
            return nueva_existencia

        except (DatabaseError, ValueError):
            # Rollback ante cualquier error de base de datos
            try:
                conn.rollback()
                logger.warning("Rollback ejecutado para ajuste de '%s'.", codigo)
            except DatabaseError:
                logger.error("Error durante rollback para '%s'.", codigo)
            raise
        except Exception as exc:
            # Captura genérica para errores inesperados
            try:
                conn.rollback()
            except DatabaseError:
                pass
            logger.error(
                "Error inesperado al ajustar inventario de '%s': %s",
                codigo,
                exc,
            )
            raise

    def set_inventory(self, codigo: str, nueva_existencia: float) -> float:
        """
        Establece el inventario de un producto a un valor específico.

        ⚠️  Esta operación MODIFICA la base de datos de Eleventa.
        ⚠️  Sobrescribe la existencia actual sin verificar el valor previo.

        Args:
            codigo: Código de barras / SKU del producto.
            nueva_existencia: Nuevo valor de existencia a establecer.

        Returns:
            La nueva existencia establecida.

        Raises:
            PermissionError: Si ELEVENTA_WRITE_MODE es False.
            ValueError: Si la existencia es negativa o el producto no existe.
            DatabaseError: Si hay un error en la operación SQL.
        """
        self._check_write_mode()

        if nueva_existencia < 0:
            raise ValueError(
                f"No se permite establecer existencia negativa ({nueva_existencia}) "
                f"para el producto '{codigo}'."
            )

        conn = self._ensure_connection()

        try:
            cur = conn.cursor()

            # Verificar que el producto existe
            cur.execute(_SQL_GET_STOCK, (codigo,))
            row = cur.fetchone()

            if row is None:
                cur.close()
                raise ValueError(
                    f"Producto con código '{codigo}' no encontrado en Eleventa."
                )

            existencia_anterior = float(row[0]) if row[0] is not None else 0.0

            # Aplicar el nuevo valor
            cur.execute(_SQL_UPDATE_STOCK, (nueva_existencia, codigo))
            conn.commit()
            cur.close()

            logger.info(
                "✅ Inventario establecido — Código: %s | Anterior: %.2f | "
                "Nuevo: %.2f",
                codigo,
                existencia_anterior,
                nueva_existencia,
            )
            return nueva_existencia

        except (DatabaseError, ValueError):
            try:
                conn.rollback()
                logger.warning(
                    "Rollback ejecutado para set_inventory de '%s'.", codigo
                )
            except DatabaseError:
                logger.error("Error durante rollback para '%s'.", codigo)
            raise
        except Exception as exc:
            try:
                conn.rollback()
            except DatabaseError:
                pass
            logger.error(
                "Error inesperado al establecer inventario de '%s': %s",
                codigo,
                exc,
            )
            raise
