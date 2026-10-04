"""
Tests de paginación y checkpointing del worker sync-up (Eleventa -> Shopify).

Simulan el escenario de una tienda sin internet que acumula cientos de ventas:
el envío se divide en lotes de `SYNC_UP_BATCH_SIZE` (100 por defecto) y el
cursor se avanza y persiste **después de cada lote confirmado**, de modo que un
lote fallido se reintenta desde su punto exacto sin repetir lo ya enviado.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent.config import settings
from agent.timestamps import format_firebird_timestamp
from agent.workers import sync_up

BASE_TIME = datetime(2026, 10, 4, 9, 0, 0)


def _build_sales(count: int) -> list[dict[str, Any]]:
    """Venta cada segundo, en orden cronológico, con ticket_id creciente."""
    return [
        {
            'ticket_id': f"{1000 + i}",
            'codigo': f"PROD{i % 10:03d}",
            'cantidad': 1.0,
            'timestamp': format_firebird_timestamp(BASE_TIME + timedelta(seconds=i)),
        }
        for i in range(count)
    ]


def _ts(index: int) -> str:
    """Timestamp Firebird de la venta en la posición `index`."""
    return format_firebird_timestamp(BASE_TIME + timedelta(seconds=index))


def _read_cursor_file(cursor_file: Path) -> dict[str, Any]:
    """Contenido persistido del cursor."""
    return json.loads(cursor_file.read_text(encoding='utf-8'))


class _RecordingMiddleware:
    """
    Doble de `MiddlewareClient` que registra cada lote enviado y el cursor
    persistido en el momento de la llamada (es decir, el cursor confirmado por
    los lotes anteriores).
    """

    def __init__(self, cursor_file: Path, fail_on_call: int | None = None) -> None:
        self._cursor_file = cursor_file
        self._fail_on_call = fail_on_call
        self.batches: list[list[dict[str, Any]]] = []
        self.attempt_sizes: list[int] = []
        self.cursor_at_call: list[str | None] = []
        self.closed = False

    def report_eleventa_sales(self, sales: list[dict[str, Any]]) -> bool:
        self.attempt_sizes.append(len(sales))
        persisted = _read_cursor_file(self._cursor_file) if self._cursor_file.exists() else {}
        self.cursor_at_call.append(persisted.get('last_timestamp'))
        self.batches.append(list(sales))

        return not (
            self._fail_on_call is not None and len(self.batches) == self._fail_on_call
        )

    def close(self) -> None:
        self.closed = True

    # -- Introspección para las aserciones --
    @property
    def ticket_ids_sent(self) -> list[str]:
        return [str(sale['ticket_id']) for batch in self.batches for sale in batch]

    @property
    def batch_sizes(self) -> list[int]:
        return [len(batch) for batch in self.batches]


@pytest.fixture
def cursor_file(tmp_path: Path) -> Path:
    return tmp_path / "sync_up_cursor.json"


def _run_batches(
    middleware: _RecordingMiddleware,
    cursor_file: Path,
    sales: list[dict[str, Any]],
    batch_size: int,
    last_check_timestamp: str = "2000-01-01 00:00:00",
):
    """Ejecuta `_report_sales_in_batches` con una ventana de dedup vacía."""
    processed = deque(maxlen=sync_up.MAX_PROCESSED_TICKETS)
    return sync_up._report_sales_in_batches(
        middleware,  # type: ignore[arg-type]
        cursor_file,
        sales,
        last_check_timestamp,
        processed,
        set(processed),
        batch_size,
    )


# ── Configuración del tamaño de lote ─────────────────────────────────────


def test_default_batch_size_is_100():
    assert sync_up.BATCH_SIZE == 100


def test_resolve_batch_size_reads_settings(monkeypatch):
    monkeypatch.setattr(settings, 'SYNC_UP_BATCH_SIZE', 250)
    assert sync_up._resolve_batch_size() == 250


def test_resolve_batch_size_is_never_below_one(monkeypatch):
    monkeypatch.setattr(settings, 'SYNC_UP_BATCH_SIZE', 0)
    assert sync_up._resolve_batch_size() == 1


def test_resolve_batch_size_falls_back_on_invalid_value(monkeypatch):
    monkeypatch.setattr(settings, 'SYNC_UP_BATCH_SIZE', 'no-es-un-numero')
    assert sync_up._resolve_batch_size() == sync_up.BATCH_SIZE


def test_agent_settings_expose_sync_up_batch_size():
    assert settings.SYNC_UP_BATCH_SIZE >= 1


# ── Chunking ──────────────────────────────────────────────────────────────


def test_chunk_sales_splits_250_sales_into_100_100_50():
    chunks = list(sync_up._chunk_sales(_build_sales(250), 100))

    assert [len(c) for c in chunks] == [100, 100, 50]
    assert sum(len(c) for c in chunks) == 250


def test_chunk_sales_preserves_order_and_coverage():
    sales = _build_sales(250)
    flattened = [s for chunk in sync_up._chunk_sales(sales, 100) for s in chunk]

    assert flattened == sales


def test_chunk_sales_with_no_sales_yields_nothing():
    assert list(sync_up._chunk_sales([], 100)) == []


def test_max_timestamp_returns_batch_maximum():
    assert sync_up._max_timestamp(_build_sales(100)) == _ts(99)


def test_max_timestamp_without_timestamps_returns_none():
    assert sync_up._max_timestamp([{'ticket_id': '1', 'codigo': 'A'}]) is None


# ── Escenario principal: 250 ventas en lotes de 100 ───────────────────────


def test_250_sales_are_sent_in_three_batches_of_max_100(
    cursor_file, monkeypatch
):
    monkeypatch.setattr(settings, 'SYNC_UP_BATCH_SIZE', 100)
    middleware = _RecordingMiddleware(cursor_file)
    sales = _build_sales(250)

    final_cursor = _run_batches(middleware, cursor_file, sales, sync_up._resolve_batch_size())

    assert middleware.batch_sizes == [100, 100, 50]
    assert all(size <= 100 for size in middleware.batch_sizes)
    assert len(middleware.ticket_ids_sent) == 250
    assert final_cursor == _ts(249)


def test_cursor_advances_progressively_after_each_batch(cursor_file):
    """Cada lote ve el cursor del lote anterior ya confirmado y persistido."""
    middleware = _RecordingMiddleware(cursor_file)
    sales = _build_sales(250)

    _run_batches(middleware, cursor_file, sales, 100)

    # Cursor visible al iniciar cada lote: ninguno, luego los dos máximos previos.
    assert middleware.cursor_at_call == [None, _ts(99), _ts(199)]

    persisted = _read_cursor_file(cursor_file)
    assert persisted['last_timestamp'] == _ts(249)
    # Solo los tickets confirmados están en la ventana de deduplicación.
    assert len(persisted['processed_tickets']) == 250


def test_each_batch_persists_its_own_cursor_maximum(cursor_file):
    """El archivo de cursor se actualiza tras cada lote, no solo al final."""
    persisted_after_each_call: list[str] = []
    middleware = _RecordingMiddleware(cursor_file)
    original = sync_up._save_cursor_file

    def _tracking_save(path: Path, last_timestamp: str, tickets: list[str]) -> None:
        original(path, last_timestamp, tickets)
        persisted_after_each_call.append(last_timestamp)

    sync_up._save_cursor_file = _tracking_save  # type: ignore[assignment]
    try:
        _run_batches(middleware, cursor_file, _build_sales(250), 100)
    finally:
        sync_up._save_cursor_file = original  # type: ignore[assignment]

    assert persisted_after_each_call == [_ts(99), _ts(199), _ts(249)]


def test_batches_respect_a_custom_batch_size(cursor_file):
    middleware = _RecordingMiddleware(cursor_file)

    _run_batches(middleware, cursor_file, _build_sales(250), 25)

    assert middleware.batch_sizes == [25] * 10
    assert len(middleware.ticket_ids_sent) == 250


def test_single_batch_when_sales_fit_in_one_chunk(cursor_file):
    middleware = _RecordingMiddleware(cursor_file)

    _run_batches(middleware, cursor_file, _build_sales(42), 100)

    assert middleware.batch_sizes == [42]
    assert _read_cursor_file(cursor_file)['last_timestamp'] == _ts(41)


def test_no_sales_sends_nothing_and_keeps_cursor(cursor_file):
    middleware = _RecordingMiddleware(cursor_file)

    result = _run_batches(middleware, cursor_file, [], 100, '2000-01-01 00:00:00')

    assert middleware.batches == []
    assert result == '2000-01-01 00:00:00'
    assert not cursor_file.exists()


# ── Fallo de un lote: reanudar desde el punto exacto ─────────────────────


def test_failed_batch_stops_the_cycle_and_keeps_last_confirmed_cursor(cursor_file):
    middleware = _RecordingMiddleware(cursor_file, fail_on_call=2)
    sales = _build_sales(250)

    final_cursor = _run_batches(middleware, cursor_file, sales, 100)

    # Se intenta el segundo lote y se detiene: el tercero nunca se envía.
    assert middleware.batch_sizes == [100, 100]
    assert len(middleware.ticket_ids_sent) == 200

    # Persistido y devuelto: solo el primer lote confirmado (100 ventas).
    persisted = _read_cursor_file(cursor_file)
    assert persisted['last_timestamp'] == _ts(99)
    assert len(persisted['processed_tickets']) == 100
    assert final_cursor == _ts(99)


def test_resumed_run_only_sends_unconfirmed_sales(cursor_file):
    """Siguiente iteración: reenvía el lote fallido, nunca los confirmados."""
    middleware = _RecordingMiddleware(cursor_file, fail_on_call=2)
    sales = _build_sales(250)

    _run_batches(middleware, cursor_file, sales, 100)
    assert len(middleware.ticket_ids_sent) == 200

    # Reanudación: el middleware ya responde bien y el cursor persistido es el
    # del último lote confirmado.
    resumed = _RecordingMiddleware(cursor_file)
    pending = sales[100:]
    _run_batches(
        resumed,
        cursor_file,
        pending,
        100,
        _read_cursor_file(cursor_file)['last_timestamp'],
    )

    assert len(resumed.ticket_ids_sent) == 150
    assert resumed.ticket_ids_sent == [str(s['ticket_id']) for s in sales[100:]]
    assert _read_cursor_file(cursor_file)['last_timestamp'] == _ts(249)


def test_dedup_window_keeps_processed_tickets_of_confirmed_batches(cursor_file):
    middleware = _RecordingMiddleware(cursor_file, fail_on_call=3)
    processed = deque(maxlen=sync_up.MAX_PROCESSED_TICKETS)
    processed_set: set[str] = set()
    sales = _build_sales(250)

    sync_up._report_sales_in_batches(
        middleware,  # type: ignore[arg-type]
        cursor_file,
        sales,
        '2000-01-01 00:00:00',
        processed,
        processed_set,
        100,
    )

    assert len(processed) == 200
    assert set(processed) == {str(s['ticket_id']) for s in sales[:200]}


# ── Worker completo (loop con cursores y reintentos) ──────────────────────


class _FakeFirebird:
    """Base de datos que devuelve las ventas pendientes del cursor."""

    def __init__(
        self,
        sales_by_cursor: dict[str, list[dict[str, Any]]],
        cursor_file: Path | None = None,
    ) -> None:
        self._sales_by_cursor = sales_by_cursor
        self._cursor_file = cursor_file
        self.queried_cursors: list[str] = []
        self.persisted_cursor_at_query: list[str | None] = []

    def get_recent_sales(self, last_check_timestamp: str) -> list[dict[str, Any]]:
        self.queried_cursors.append(last_check_timestamp)
        if self._cursor_file is not None and self._cursor_file.exists():
            self.persisted_cursor_at_query.append(
                _read_cursor_file(self._cursor_file)['last_timestamp']
            )
        else:
            self.persisted_cursor_at_query.append(None)
        return list(self._sales_by_cursor.get(last_check_timestamp, []))


class _StopAfterCycles:
    """Evento de parada falso que se activa tras `cycles` iteraciones."""

    def __init__(self, cycles: int) -> None:
        self._remaining = cycles
        self.checks = 0

    def is_set(self) -> bool:
        self.checks += 1
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False

    def wait(self, timeout: float | None = None) -> bool:
        return self.is_set()


def test_run_sync_up_checkpoints_each_batch_across_cycles(
    cursor_file, monkeypatch
):
    """
    El worker envía 250 ventas en 3 lotes, falla el último y, en el siguiente
    ciclo, reanuda desde el cursor persistido sin repetir los lotes confirmados.
    """
    sales = _build_sales(250)
    fail_once = {"pending": True}
    middleware = _RecordingMiddleware(cursor_file)
    original_report = middleware.report_eleventa_sales

    def _report(sales_batch: list[dict[str, Any]]) -> bool:
        # El lote final (50 ventas) falla una sola vez, como un timeout HTTP.
        if fail_once["pending"] and len(sales_batch) < 100:
            middleware.attempt_sizes.append(len(sales_batch))
            fail_once["pending"] = False
            return False
        return original_report(sales_batch)

    middleware.report_eleventa_sales = _report  # type: ignore[method-assign]

    db = _FakeFirebird(
        {
            '2000-01-01 00:00:00': sales,
            _ts(199): sales[200:],
        },
        cursor_file=cursor_file,
    )

    # Cursor previo: el worker arranca desde 2000 con las 250 ventas pendientes.
    sync_up._save_cursor_file(cursor_file, '2000-01-01 00:00:00', [])

    monkeypatch.setattr(sync_up, 'MiddlewareClient', lambda: middleware)
    monkeypatch.setattr(sync_up, 'FirebirdClient', lambda: db)
    monkeypatch.setattr(settings, 'SYNC_UP_INTERVAL_SECONDS', 0)
    monkeypatch.setattr(settings, 'SYNC_UP_BATCH_SIZE', 100)

    # Dos iteraciones: la primera falla en el último lote, la segunda reanuda.
    sync_up.run_sync_up(_StopAfterCycles(2), cursor_file=cursor_file)

    # Ciclo 1: se intentan 3 lotes (100 + 100 + 50); el último falla y detiene
    # el ciclo, así que sólo 2 lotes quedan confirmados.
    assert middleware.attempt_sizes[:3] == [100, 100, 50]
    assert middleware.batch_sizes[:2] == [100, 100]
    assert db.queried_cursors[0] == '2000-01-01 00:00:00'

    # Ciclo 2: el cursor persistido quedó en el segundo lote confirmado
    # (_ts(199)), así que se relee desde ahí y sólo viaja el lote pendiente
    # de 50 ventas: ningún ticket confirmado se reenvía.
    assert db.persisted_cursor_at_query[1] == _ts(199)
    assert db.queried_cursors[1] == _ts(199)
    assert middleware.batch_sizes[2:] == [50]
    assert len(middleware.ticket_ids_sent) == 250
    assert len(set(middleware.ticket_ids_sent)) == 250
    assert _read_cursor_file(cursor_file)['last_timestamp'] == _ts(249)
    assert middleware.closed
