"""
Tests de robustez transaccional del cliente Firebird (`update_inventory`).

Cubren los dos riesgos de la auditoría:

1. Ausencia de `rollback()` explícito ante fallos previos al `commit()`, que
   deja locks de fila/página retenidos mientras la caja cobra.
2. Conflictos de bloqueo transitorios (`lock conflict` / `deadlock`) tratados
   como fallo definitivo, perdiendo un ajuste válido.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest

from agent.database import firebird as firebird_module
from agent.database.firebird import FirebirdClient, _is_lock_conflict

LOCK_CONFLICT = Exception("Lock conflict on update of table PRODUCTOS")
DEADLOCK = Exception("deadlock / deadlock victim")
GENERIC_ERROR = Exception("validation error for column EXISTENCIA")


UPDATE_MARKER = "UPDATE PRODUCTOS"


class _FakeCursor:
    """
    Cursor mínimo que replica la API usada por `update_inventory`.

    Los fallos se declaran por sentencia (`fail_on_update`) y se reproducen en
    cada intento, igual que un bloqueo real de Firebird.
    """

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row
        self._update_error: Exception | None = None
        self._update_failures_left = 0
        self.executed: list[str] = []

    def fail_on_update(self, error: Exception, times: int | None = 1) -> None:
        """
        Hace fallar el `UPDATE PRODUCTOS`.

        Args:
            error: Excepción a lanzar.
            times: Número de fallos; `None` para fallar siempre.
        """
        self._update_error = error
        self._update_failures_left = -1 if times is None else times

    def execute(self, query: str, params: Any = None) -> None:
        self.executed.append(query)
        if self._update_error is None or UPDATE_MARKER not in query:
            return
        if self._update_failures_left != 0:
            self._update_failures_left -= 1
            raise self._update_error

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [self._row] if self._row else []

    def close(self) -> None:
        pass


class _FakeConnection:
    """Conexión que registra commits, rollbacks y cierres."""

    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def client_factory(monkeypatch):
    """
    Instala un `get_connection` falso y devuelve una fábrica de clientes junto
    con la lista de conexiones creadas (una por intento).
    """
    connections: list[_FakeConnection] = []

    def _install(cursor: _FakeCursor) -> FirebirdClient:
        @contextmanager
        def _get_connection(self) -> Generator[_FakeConnection, None, None]:
            conn = _FakeConnection(cursor)
            connections.append(conn)
            try:
                yield conn  # type: ignore[misc]
            finally:
                conn.close()

        monkeypatch.setattr(FirebirdClient, 'get_connection', _get_connection)
        monkeypatch.setattr(firebird_module, 'LOCK_RETRY_DELAY_SECONDS', 0)
        return FirebirdClient(db_path='test.fdb', user='SYSDBA', password='masterkey')

    return _install, connections


# ── Detección de conflictos ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        "Lock conflict on update of table PRODUCTOS",
        "deadlock / deadlock victim",
        "Update conflict: concurrent update",
        "lock timeout exceeded",
        "LOCK CONFLICT",
    ],
)
def test_is_lock_conflict_detects_transient_errors(error):
    assert _is_lock_conflict(Exception(error)) is True


@pytest.mark.parametrize(
    "error",
    [
        "validation error for column EXISTENCIA",
        "Table unknown PRODUCTOS",
        "I/O error for file pdv.fdb",
    ],
)
def test_is_lock_conflict_ignores_other_errors(error):
    assert _is_lock_conflict(Exception(error)) is False


# ── Camino feliz ──────────────────────────────────────────────────────────


def test_successful_update_commits_once(client_factory):
    install, connections = client_factory
    cursor = _FakeCursor(row=(1,))

    assert install(cursor).update_inventory('PROD001', 8.0) is True

    assert len(connections) == 1
    assert connections[0].commits == 1
    assert connections[0].rollbacks == 0
    assert connections[0].closed is True
    assert len(cursor.executed) == 2  # SELECT de existencia + UPDATE


def test_missing_product_returns_false_without_commit(client_factory):
    install, connections = client_factory
    cursor = _FakeCursor(row=None)

    assert install(cursor).update_inventory('NOEXISTE', 8.0) is False

    assert connections[0].commits == 0
    assert connections[0].rollbacks == 0
    assert len(cursor.executed) == 1  # nunca se intenta el UPDATE


# ── Rollback explícito ────────────────────────────────────────────────────


def test_update_error_rolls_back_and_closes_connection(client_factory):
    install, connections = client_factory
    cursor = _FakeCursor(row=(1,))
    cursor.fail_on_update(GENERIC_ERROR)

    assert install(cursor).update_inventory('PROD001', 8.0) is False

    conn = connections[0]
    assert conn.rollbacks == 1
    assert conn.commits == 0
    # La conexión se cierra siempre: los locks no sobreviven al error.
    assert conn.closed is True


def test_rollback_survives_a_broken_connection(client_factory):
    """Si el propio rollback falla, `update_inventory` no debe propagarlo."""
    _install, connections = client_factory
    cursor = _FakeCursor(row=(1,))
    cursor.fail_on_update(GENERIC_ERROR)

    @contextmanager
    def _get_connection():
        conn = _FakeConnection(cursor)
        connections.append(conn)

        def _broken_rollback() -> None:
            raise Exception("connection is closed")

        conn.rollback = _broken_rollback  # type: ignore[method-assign]
        try:
            yield conn  # type: ignore[misc]
        finally:
            conn.close()

    client = FirebirdClient()
    client.get_connection = _get_connection  # type: ignore[method-assign]

    assert client.update_inventory('PROD001', 8.0) is False
    assert connections[0].commits == 0
    assert connections[0].closed is True


# ── Reintento ante conflicto de bloqueo ───────────────────────────────────


def test_lock_conflict_is_retried_and_succeeds(client_factory):
    install, connections = client_factory
    cursor = _FakeCursor(row=(1,))
    cursor.fail_on_update(LOCK_CONFLICT)

    assert install(cursor).update_inventory('PROD001', 8.0) is True

    assert len(connections) == 2
    assert connections[0].rollbacks == 1  # intento fallido: rollback
    assert connections[0].commits == 0
    assert connections[1].commits == 1  # segundo intento: commit
    assert connections[1].rollbacks == 0
    assert connections[1].closed is True


def test_deadlock_is_retried_and_succeeds(client_factory):
    install, connections = client_factory
    cursor = _FakeCursor(row=(1,))
    cursor.fail_on_update(DEADLOCK)

    assert install(cursor).update_inventory('PROD001', 8.0) is True
    assert connections[1].commits == 1


def test_persistent_lock_conflict_gives_up_after_max_attempts(client_factory):
    install, connections = client_factory
    cursor = _FakeCursor(row=(1,))
    cursor.fail_on_update(LOCK_CONFLICT, times=None)  # la caja no libera el lock

    assert install(cursor).update_inventory('PROD001', 8.0) is False

    assert len(connections) == firebird_module.LOCK_RETRY_ATTEMPTS == 3
    assert all(conn.rollbacks == 1 for conn in connections)
    assert all(conn.commits == 0 for conn in connections)
    assert all(conn.closed for conn in connections)


def test_non_lock_error_is_not_retried(client_factory):
    install, connections = client_factory
    cursor = _FakeCursor(row=(1,))
    cursor.fail_on_update(GENERIC_ERROR)

    assert install(cursor).update_inventory('PROD001', 8.0) is False

    # Un error de validación no se reintenta: un solo intento.
    assert len(connections) == 1


def test_connection_failure_is_not_retried(client_factory, monkeypatch):
    """Sin conexión no hay transacción: el fallo se reporta sin reintentos."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise Exception("connection refused")

    monkeypatch.setattr(FirebirdClient, 'get_connection', _boom)

    assert FirebirdClient().update_inventory('PROD001', 8.0) is False
