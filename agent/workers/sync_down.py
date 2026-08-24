import time
import logging
import threading

from agent.config import settings
from agent.database.firebird import FirebirdClient
from agent.services.middleware_client import MiddlewareClient

logger = logging.getLogger(__name__)


def run_sync_down(stop_event: threading.Event):
    """
    Proceso (Worker) que periódicamente consulta al Middleware por
    ajustes pendientes (ventas de Shopify) y los aplica en Eleventa.
    """
    logger.info("Iniciando Worker Sync-Down (Shopify -> Eleventa)...")
    
    middleware = MiddlewareClient()
    db = FirebirdClient()
    
    while not stop_event.is_set():
        try:
            # 1. Obtener ajustes pendientes del servidor
            adjustments = middleware.get_pending_adjustments()
            
            if adjustments:
                logger.info("Recibidos %d ajustes pendientes del middleware.", len(adjustments))
                
            for adj in adjustments:
                adj_id = adj.get("id")
                sku = adj.get("eleventa_sku")
                qty_change = adj.get("quantity_change")
                
                if not sku or qty_change is None:
                    logger.warning("Ajuste %s inválido: %s", adj_id, adj)
                    middleware.mark_adjustment_failed(adj_id, "Falta SKU o cantidad en payload")
                    continue
                
                # 2. Consultar inventario actual en Eleventa
                current_inv = db.get_product_inventory(sku)
                if current_inv is None:
                    error_msg = f"Producto SKU '{sku}' no existe en Eleventa."
                    logger.error(error_msg)
                    middleware.mark_adjustment_failed(adj_id, error_msg)
                    continue
                
                # 3. Calcular nuevo inventario
                # qty_change suele ser negativo para ventas
                new_inv = current_inv + qty_change
                
                # 4. Actualizar en Eleventa
                success = db.update_inventory(sku, new_inv, reason=f"Shopify Sync {adj_id}")
                
                # 5. Notificar al middleware
                if success:
                    middleware.mark_adjustment_completed(adj_id)
                else:
                    middleware.mark_adjustment_failed(adj_id, "Error actualizando BD local")
                    
        except Exception as e:
            logger.error("Error inesperado en worker sync-down: %s", e)
            
        # Esperar antes del siguiente ciclo, revisando periódicamente el stop_event
        # para poder cerrarse rápido si el usuario apaga el script.
        for _ in range(settings.SYNC_DOWN_INTERVAL_SECONDS):
            if stop_event.is_set():
                break
            time.sleep(1)
            
    logger.info("Worker Sync-Down detenido.")
