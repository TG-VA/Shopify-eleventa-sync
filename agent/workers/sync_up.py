from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent.config import settings
from agent.database.firebird import FirebirdClient
from agent.services.middleware_client import MiddlewareClient
from agent.timestamps import format_firebird_timestamp, now_firebird_timestamp, parse_datetime

logger = logging.getLogger(__name__)

# Máximo de tickets recordados para deduplicación.
MAX_PROCESSED_TICKETS = 10000

# Margen aplicado al cursor en el primer arranque: combinado con el operador
# `>=` de la consulta, garantiza no perder tickets creados dentro de esta
# ventana antes del primer escaneo. Los repetidos se filtran por ticket_id.
INITIAL_CURSOR_OVERLAP_SECONDS = 5

# Máximo de ventas por POST hacia el middleware. Una tienda sin internet puede
# acumular cientos de tickets; enviarlos en un único request massivo produce
# timeouts HTTP y, peor aún, pérdida del lote completo. El valor efectivo se
# resuelve con `settings.SYNC_UP_BATCH_SIZE`.
BATCH_SIZE = 100


def _initial_cursor() -> str:
    """Cursor inicial en formato Firebird, con pequeño margen de solapamiento."""
    return format_firebird_timestamp(
        datetime.now() - timedelta(seconds=INITIAL_CURSOR_OVERLAP_SECONDS)
    )


def _load_cursor_file(cursor_file: Path) -> tuple[str | None, list[str]]:
    """
    Lee el cursor local.

    Returns:
        Tupla `(last_timestamp, processed_tickets)`. Ambas partes pueden ser
        None/[] si el archivo no existe o está corrupto.
    """
    if not cursor_file.exists():
        return None, []

    with open(cursor_file, encoding='utf-8') as f:
        data = json.load(f)

    last_timestamp = data.get('last_timestamp')
    tickets = [str(t) for t in (data.get('processed_tickets') or [])]

    if last_timestamp:
        # Normaliza cursores heredados en formato ISO ('T') o con offset.
        last_timestamp = format_firebird_timestamp(last_timestamp)

    return last_timestamp, tickets


def _save_cursor_file(
    cursor_file: Path,
    last_timestamp: str,
    processed_tickets: list[str],
) -> None:
    """Persistencia atómica del cursor (escritura en .tmp + os.replace)."""
    tmp_file = cursor_file.with_suffix('.json.tmp')
    data_to_save = {
        'last_timestamp': last_timestamp,
        'processed_tickets': processed_tickets,
        'updated_at': datetime.now().isoformat(),
    }
    with open(tmp_file, 'w', encoding='utf-8') as f:
        json.dump(data_to_save, f, indent=2)
    os.replace(tmp_file, cursor_file)


def _register_processed(
    ticket_ids_deque: deque[str],
    ticket_ids_set: set[str],
    new_ids: list[str],
) -> None:
    """
    Registra tickets nuevos y mantiene sincronizados deque y set.

    El deque es la fuente de verdad con `maxlen=MAX_PROCESSED_TICKETS`: al
    expulsar el ticket más antiguo, el set se reconstruye desde el deque para
    que nunca conserve IDs que ya no están en la ventana de deduplicación.
    """
    added = False
    for ticket_id in new_ids:
        if ticket_id in ticket_ids_set:
            continue
        ticket_ids_deque.append(ticket_id)
        ticket_ids_set.add(ticket_id)
        added = True

    if added:
        ticket_ids_set.clear()
        ticket_ids_set.update(ticket_ids_deque)


def _resolve_batch_size() -> int:
    """Tamaño efectivo del lote, acotado a un mínimo de una venta."""
    try:
        return max(1, int(getattr(settings, 'SYNC_UP_BATCH_SIZE', BATCH_SIZE)))
    except (TypeError, ValueError):
        logger.warning(
            "SYNC_UP_BATCH_SIZE inválido; se usa el valor por defecto %d.", BATCH_SIZE
        )
        return BATCH_SIZE


def _chunk_sales(
    sales: list[dict[str, Any]],
    batch_size: int = BATCH_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """
    Divide las ventas en lotes contiguos de máximo `batch_size` elementos.

    Se preserva el orden original (y por tanto el orden temporal) para que el
    cursor pueda avanzar de forma monótona tras cada lote confirmado.
    """
    size = max(1, batch_size)
    for start in range(0, len(sales), size):
        yield sales[start:start + size]


def _max_timestamp(sales: list[dict[str, Any]]) -> str | None:
    """MAX(timestamp) del lote en formato Firebird, o None si no hay ninguno."""
    timestamps = sorted(ts for ts in (s.get('timestamp') for s in sales) if ts)
    if not timestamps:
        return None
    # parse_datetime valida y normaliza a formato Firebird.
    return format_firebird_timestamp(parse_datetime(timestamps[-1]))


def _report_sales_in_batches(
    middleware: MiddlewareClient,
    cursor_file: Path,
    sales: list[dict[str, Any]],
    last_check_timestamp: str,
    processed_ticket_ids_deque: deque[str],
    processed_ticket_ids_set: set[str],
    batch_size: int = BATCH_SIZE,
) -> str:
    """
    Envía las ventas al middleware en lotes, con checkpoint por lote.

    Tras cada lote confirmado actualiza el cursor a MAX(timestamp) de ese lote
    y lo persiste de forma atómica. Si un lote falla, el envío se detiene y el
    cursor queda en el último lote confirmado: el siguiente ciclo vuelve a leer
    desde ese punto y la ventana de deduplicación evita reenviar los tickets
    ya confirmados.

    Args:
        middleware: Cliente HTTP hacia el middleware.
        cursor_file: Archivo de persistencia del cursor.
        sales: Ventas únicas pendientes de reportar.
        last_check_timestamp: Cursor vigente antes de enviar.
        processed_ticket_ids_deque: Ventana de tickets ya procesados.
        processed_ticket_ids_set: Índice de la ventana para deduplicación.
        batch_size: Máximo de ventas por POST.

    Returns:
        El cursor vigente tras el envío: avanzado hasta el último lote
        confirmado, o sin cambios si el primer lote falló.
    """
    if not sales:
        return last_check_timestamp

    total = len(sales)
    batches = list(_chunk_sales(sales, batch_size))
    logger.info(
        "Enviando %d ventas al middleware en %d lote(s) de hasta %d.",
        total, len(batches), batch_size,
    )

    for index, chunk in enumerate(batches, start=1):
        chunk_max_ts = _max_timestamp(chunk)

        if not middleware.report_eleventa_sales(chunk):
            logger.warning(
                "Fallo al reportar el lote %d/%d (%d ventas). Se reintentará "
                "desde este punto en el siguiente ciclo.",
                index, len(batches), len(chunk),
            )
            # El cursor NO avanza: se queda en el último lote confirmado para
            # que la siguiente iteración vuelva a leer desde ahí. Los tickets
            # ya confirmados se descartan por la ventana de deduplicación, así
            # que nunca se reenvían y ninguna venta del lote fallido se pierde.
            return last_check_timestamp

        # Lote confirmado: se avanza el cursor con el MAX del lote y se persiste
        # inmediatamente, sin esperar al resto de la cola. Sin timestamps
        # válidos, se avanza al momento actual para no releer el mismo rango.
        last_check_timestamp = chunk_max_ts or now_firebird_timestamp()

        _register_processed(
            processed_ticket_ids_deque,
            processed_ticket_ids_set,
            [str(sale['ticket_id']) for sale in chunk if sale.get('ticket_id')],
        )

        try:
            _save_cursor_file(
                cursor_file,
                last_check_timestamp,
                list(processed_ticket_ids_deque),
            )
        except Exception as save_e:
            logger.warning("No se pudo guardar cursor local: %s", save_e)

        logger.info(
            "Lote %d/%d confirmado: %d ventas, cursor en %s (%d tickets en ventana).",
            index, len(batches), len(chunk),
            last_check_timestamp, len(processed_ticket_ids_deque),
        )

    return last_check_timestamp


def run_sync_up(stop_event: threading.Event, cursor_file: Path | None = None):
    """
    Proceso (Worker) que periódicamente escanea la base de datos de Eleventa
    buscando ventas recientes y las envía al Middleware para que descuente en Shopify.

    Args:
        stop_event: Evento que termina el ciclo de vida del worker.
        cursor_file: Archivo de persistencia del cursor. Por defecto se usa el
            archivo junto al paquete `agent/`; se sobreescribe en tests.
    """
    logger.info("Iniciando Worker Sync-Up (Eleventa -> Shopify)...")

    middleware = MiddlewareClient()
    db = FirebirdClient()

    # Persistencia local para evitar duplicados
    if cursor_file is None:
        cursor_file = Path(__file__).parent.parent / "sync_up_cursor.json"

    # Cargar último timestamp procesado y tickets ya procesados (lectura única).
    # El cursor SIEMPRE se guarda como 'YYYY-MM-DD HH:MM:SS' (formato Firebird).
    last_check_timestamp = _initial_cursor()
    processed_ticket_ids_deque: deque[str] = deque(maxlen=MAX_PROCESSED_TICKETS)
    processed_ticket_ids_set: set[str] = set()

    try:
        stored_timestamp, stored_tickets = _load_cursor_file(cursor_file)

        if stored_timestamp:
            last_check_timestamp = stored_timestamp
            logger.info("Cursor cargado desde archivo: %s", last_check_timestamp)

        if stored_tickets:
            # Deduplicar preservando orden (más antiguos primero al truncar por maxlen)
            processed_ticket_ids_deque.extend(dict.fromkeys(stored_tickets))
            processed_ticket_ids_set = set(processed_ticket_ids_deque)
            logger.info(
                "Tickets procesados cargados: %d", len(processed_ticket_ids_deque)
            )
    except Exception as e:
        logger.warning("No se pudo cargar cursor local: %s", e)
        last_check_timestamp = _initial_cursor()
        processed_ticket_ids_deque.clear()
        processed_ticket_ids_set.clear()

    try:
        while not stop_event.is_set():
            try:
                # 1. Buscar ventas recientes en Eleventa (cursor normalizado a datetime)
                new_sales = db.get_recent_sales(last_check_timestamp)

                # 2. Filtrar tickets ya procesados
                unique_sales = []
                for sale in new_sales:
                    ticket_id = sale.get('ticket_id')
                    if ticket_id and ticket_id in processed_ticket_ids_set:
                        continue
                    unique_sales.append(sale)

                if unique_sales:
                    logger.info(
                        "Se encontraron %d ventas nuevas en Eleventa.",
                        len(unique_sales),
                    )

                    # 3. Enviar al middleware en lotes, avanzando y persistiendo
                    #    el cursor tras cada lote confirmado. Si un lote falla,
                    #    el ciclo termina aquí y el siguiente retoma desde ese
                    #    punto exacto.
                    last_check_timestamp = _report_sales_in_batches(
                        middleware,
                        cursor_file,
                        unique_sales,
                        last_check_timestamp,
                        processed_ticket_ids_deque,
                        processed_ticket_ids_set,
                        _resolve_batch_size(),
                    )

            except Exception as e:
                logger.error("Error inesperado en worker sync-up: %s", e)

            # Esperar antes del siguiente ciclo
            for _ in range(settings.SYNC_UP_INTERVAL_SECONDS):
                if stop_event.is_set():
                    break
                time.sleep(1)
    finally:
        middleware.close()

    logger.info("Worker Sync-Up detenido.")
