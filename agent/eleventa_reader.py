"""
Lector de la base de datos Firebird de Eleventa.

Responsable de consultar productos e inventario directamente
desde la base de datos PDVDATA.FDB de Eleventa (Firebird 2.5+).
Incluye reconexión automática y manejo de errores.
"""

import logging
from typing import Optional

from firebird.driver import connect, Connection, DatabaseError

from agent.config import settings
from shared.models import ProductInventory

logger = logging.getLogger(__name__)

# ── Consultas SQL ───────────────────────────────────────────────────────
# Consulta principal: obtiene productos activos (no eliminados)
_SQL_ALL_PRODUCTS = """
    SELECT
        p.ID,
        p.CODIGO,
        p.DESCRIPCION,
        p.EXISTENCIA,
        p.PFINAL
    FROM PRODUCTOS p
    WHERE p.ELIMINADO_EN IS NULL
"""

_SQL_PRODUCT_BY_CODE = """
    SELECT
        p.ID,
        p.CODIGO,
        p.DESCRIPCION,
        p.EXISTENCIA,
        p.PFINAL
    FROM PRODUCTOS p
    WHERE p.CODIGO = ?
      AND p.ELIMINADO_EN IS NULL
"""


class EleventaReader:
    """
    Cliente de solo lectura para la base de datos Firebird de Eleventa.

    Características:
        - Reconexión automática ante pérdida de conexión.
        - Conversión de resultados a modelos ProductInventory.
        - Logging detallado para diagnóstico.
    """

    def __init__(self) -> None:
        self._conn: Optional[Connection] = None
        logger.info(
            "EleventaReader inicializado — DB: %s@%s:%d/%s",
            settings.ELEVENTA_DB_USER,
            settings.ELEVENTA_DB_HOST,
            settings.ELEVENTA_DB_PORT,
            settings.ELEVENTA_DB_PATH,
        )

    # ── Conexión ────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Establece conexión con la base de datos Firebird."""
        if self._conn is not None:
            logger.debug("Ya existe una conexión activa, reutilizando.")
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
            logger.info("Conexión a Firebird establecida correctamente.")
        except DatabaseError as exc:
            logger.error("Error al conectar con Firebird: %s", exc)
            self._conn = None
            raise

    def disconnect(self) -> None:
        """Cierra la conexión con la base de datos Firebird."""
        if self._conn is not None:
            try:
                self._conn.close()
                logger.info("Conexión a Firebird cerrada.")
            except DatabaseError as exc:
                logger.warning("Error al cerrar conexión Firebird: %s", exc)
            finally:
                self._conn = None

    def _ensure_connection(self) -> Connection:
        """
        Garantiza que exista una conexión activa.
        Intenta reconectar si la conexión se perdió.
        """
        if self._conn is None:
            logger.warning("Conexión perdida, intentando reconectar...")
            self.connect()

        # Verificar que la conexión sigue viva
        try:
            cur = self._conn.cursor()  # type: ignore[union-attr]
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            cur.fetchone()
            cur.close()
        except (DatabaseError, AttributeError):
            logger.warning("Conexión inactiva detectada, reconectando...")
            self._conn = None
            self.connect()

        return self._conn  # type: ignore[return-value]

    # ── Consultas ───────────────────────────────────────────────────────

    def _row_to_product(self, row: tuple) -> ProductInventory:
        """Convierte una fila de resultados SQL a ProductInventory."""
        return ProductInventory(
            eleventa_id=str(row[0]),
            codigo=str(row[1]).strip(),
            descripcion=str(row[2]).strip() if row[2] else "",
            existencia=float(row[3]) if row[3] is not None else 0.0,
            precio=float(row[4]) if row[4] is not None else 0.0,
        )

    def get_all_products(self) -> list[ProductInventory]:
        """
        Obtiene todos los productos activos de Eleventa.

        Returns:
            Lista de ProductInventory con los datos actuales.

        Raises:
            DatabaseError: Si hay un error en la consulta.
        """
        conn = self._ensure_connection()
        try:
            cur = conn.cursor()
            cur.execute(_SQL_ALL_PRODUCTS)
            rows = cur.fetchall()
            cur.close()

            products = [self._row_to_product(row) for row in rows]
            logger.info(
                "Lectura completada: %d productos activos encontrados.",
                len(products),
            )
            return products

        except DatabaseError as exc:
            logger.error("Error al leer productos: %s", exc)
            # Invalidar conexión para forzar reconexión en el próximo intento
            self._conn = None
            raise

    def get_product_by_code(self, codigo: str) -> Optional[ProductInventory]:
        """
        Busca un producto específico por su código de barras.

        Args:
            codigo: Código de barras / SKU del producto.

        Returns:
            ProductInventory si se encuentra, None si no existe.
        """
        conn = self._ensure_connection()
        try:
            cur = conn.cursor()
            cur.execute(_SQL_PRODUCT_BY_CODE, (codigo,))
            row = cur.fetchone()
            cur.close()

            if row is None:
                logger.debug("Producto con código '%s' no encontrado.", codigo)
                return None

            product = self._row_to_product(row)
            logger.debug("Producto encontrado: %s — %s", codigo, product.descripcion)
            return product

        except DatabaseError as exc:
            logger.error(
                "Error al buscar producto '%s': %s", codigo, exc
            )
            self._conn = None
            raise

    def test_connection(self) -> bool:
        """
        Prueba la conexión a la base de datos.

        Returns:
            True si la conexión es exitosa, False en caso contrario.
        """
        try:
            self.connect()
            conn = self._ensure_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM PRODUCTOS WHERE ELIMINADO_EN IS NULL")
            count = cur.fetchone()[0]
            cur.close()
            logger.info(
                "Prueba de conexión exitosa — %d productos activos en Eleventa.",
                count,
            )
            return True
        except Exception as exc:
            logger.error("Prueba de conexión fallida: %s", exc)
            return False
