from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator, Any

import fdb

from agent.config import settings

logger = logging.getLogger(__name__)


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
    def get_connection(self) -> Generator[fdb.Connection, None, None]:
        """
        Context manager para obtener una conexión a la base de datos.
        Asegura que la conexión se cierre al terminar.
        """
        conn = None
        try:
            # charset='UTF8' o 'WIN1252' dependiendo de la configuración de Eleventa
            conn = fdb.connect(
                dsn=self.db_path,
                user=self.user,
                password=self.password,
                charset="UTF8",
            )
            yield conn
        except Exception as e:
            logger.error("Error conectando a Firebird: %s", e)
            raise
        finally:
            if conn:
                conn.close()

    def get_product_inventory(self, sku: str) -> float | None:
        """
        Obtiene el inventario actual de un producto por su SKU (Código).
        """
        query = """
            SELECT Existencia 
            FROM Articulos 
            WHERE CodigoArticulo = ?
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (sku,))
            row = cursor.fetchone()
            if row:
                return float(row[0])
            return None

    def update_inventory(self, sku: str, new_quantity: float, reason: str = "Sincronización Shopify") -> bool:
        """
        Actualiza el inventario de un producto.
        En Eleventa, usualmente se actualiza la tabla Articulos (Existencia)
        y se registra el movimiento en otra tabla (opcionalmente).
        """
        # IMPORTANTE: Esta es una consulta simplificada. La estructura real
        # de Eleventa puede requerir actualizar otras tablas como InventarioFisico.
        # Ajustaremos estas consultas cuando probemos con la base real.
        
        query = """
            UPDATE Articulos 
            SET Existencia = ? 
            WHERE CodigoArticulo = ?
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # Verificar que el producto exista primero
                cursor.execute("SELECT IdArticulo FROM Articulos WHERE CodigoArticulo = ?", (sku,))
                if not cursor.fetchone():
                    logger.warning("Producto con SKU %s no encontrado en Eleventa.", sku)
                    return False
                
                cursor.execute(query, (new_quantity, sku))
                conn.commit()
                logger.info("Inventario de %s actualizado a %s", sku, new_quantity)
                return True
        except Exception as e:
            logger.error("Error actualizando inventario para %s: %s", sku, e)
            return False

    def get_recent_sales(self, last_check_timestamp: str) -> list[dict[str, Any]]:
        """
        Busca ventas que hayan ocurrido después del `last_check_timestamp`.
        (Lógica placeholder, dependerá del esquema de Ventas/VentasDetalle).
        """
        # TODO: Implementar la consulta a la tabla de Ventas
        return []
