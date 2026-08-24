import sys
import time
import signal
import logging
import threading

# Asegurar que el directorio raíz está en el path para las importaciones
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from agent.config import settings
from agent.workers.sync_down import run_sync_down
from agent.workers.sync_up import run_sync_up

logger = logging.getLogger(__name__)

# Evento para indicar a los hilos que deben detenerse
stop_event = threading.Event()

def handle_signal(sig, frame):
    """
    Maneja las señales de interrupción (Ctrl+C, SIGTERM) para un cierre limpio.
    """
    logger.info("Señal de detención recibida. Cerrando el agente de Eleventa...")
    stop_event.set()


def main():
    logger.info("=== Iniciando Agente de Sincronización Eleventa-Shopify ===")
    logger.info("Middleware URL: %s", settings.MIDDLEWARE_URL)
    logger.info("Eleventa DB Path: %s", settings.ELEVENTA_DB_PATH)
    
    # Registrar manejadores de señales para cierre limpio
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    # Iniciar hilos (Workers)
    threads = []
    
    # 1. Hilo de bajada (Shopify -> Eleventa)
    t_down = threading.Thread(
        target=run_sync_down,
        args=(stop_event,),
        name="SyncDownWorker",
        daemon=True
    )
    threads.append(t_down)
    
    # 2. Hilo de subida (Eleventa -> Shopify)
    t_up = threading.Thread(
        target=run_sync_up,
        args=(stop_event,),
        name="SyncUpWorker",
        daemon=True
    )
    threads.append(t_up)
    
    # Arrancar hilos
    for t in threads:
        t.start()
        
    logger.info("Agente iniciado correctamente. Presiona Ctrl+C para detener.")
    
    # Bucle principal que mantiene vivo el programa principal
    # y permite atrapar el KeyboardInterrupt de forma responsiva en Windows.
    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Interrupción por teclado detectada.")
        stop_event.set()
        
    logger.info("Esperando a que los hilos terminen...")
    for t in threads:
        t.join(timeout=max(settings.SYNC_DOWN_INTERVAL_SECONDS, settings.SYNC_UP_INTERVAL_SECONDS) + 2)
        
    logger.info("=== Agente detenido ===")


if __name__ == "__main__":
    main()
