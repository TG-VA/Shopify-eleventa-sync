import time
import logging
import threading
from datetime import datetime, timezone

from agent.config import settings
from agent.database.firebird import FirebirdClient
from agent.services.middleware_client import MiddlewareClient

logger = logging.getLogger(__name__)


def run_sync_up(stop_event: threading.Event):
    """
    Proceso (Worker) que periódicamente escanea la base de datos de Eleventa
    buscando ventas recientes y las envía al Middleware para que descuente en Shopify.
    """
    logger.info("Iniciando Worker Sync-Up (Eleventa -> Shopify)...")
    
    middleware = MiddlewareClient()
    db = FirebirdClient()
    
    # Llevar un registro de la última vez que revisamos para no reenviar ventas repetidas.
    # Inicialmente, usamos la hora actual.
    last_check_timestamp = datetime.now(timezone.utc).isoformat()
    
    while not stop_event.is_set():
        try:
            # 1. Buscar ventas recientes en Eleventa
            new_sales = db.get_recent_sales(last_check_timestamp)
            
            if new_sales:
                logger.info("Se encontraron %d ventas nuevas en Eleventa.", len(new_sales))
                
                # 2. Enviar al middleware
                success = middleware.report_eleventa_sales(new_sales)
                
                # 3. Actualizar timestamp solo si el envío fue exitoso
                if success:
                    last_check_timestamp = datetime.now(timezone.utc).isoformat()
                    logger.info("Ventas reportadas, marca de tiempo actualizada.")
                else:
                    logger.warning("Fallo al reportar ventas, se reintentará en el siguiente ciclo.")
            
        except Exception as e:
            logger.error("Error inesperado en worker sync-up: %s", e)
            
        # Esperar antes del siguiente ciclo
        for _ in range(settings.SYNC_UP_INTERVAL_SECONDS):
            if stop_event.is_set():
                break
            time.sleep(1)
            
    logger.info("Worker Sync-Up detenido.")
