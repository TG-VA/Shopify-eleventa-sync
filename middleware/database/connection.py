"""
Conexión asíncrona a PostgreSQL mediante asyncpg.

Gestiona un pool de conexiones como singleton a nivel de módulo
para reutilizar conexiones de forma eficiente.
"""

from __future__ import annotations

import logging

import asyncpg

from middleware.config import settings

logger = logging.getLogger(__name__)

# Pool singleton a nivel de módulo
_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """
    Obtiene el pool de conexiones, creándolo si no existe.

    Returns:
        Pool de conexiones asyncpg listo para usar.

    Raises:
        ConnectionError: Si no se puede conectar a PostgreSQL.
    """
    global _pool

    if _pool is None:
        try:
            _pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_URL,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            logger.info("Pool de conexiones PostgreSQL creado exitosamente.")
        except Exception as exc:
            logger.error("Error al crear pool de conexiones: %s", exc)
            raise ConnectionError(
                f"No se pudo conectar a PostgreSQL: {exc}"
            ) from exc

    return _pool


async def close_pool() -> None:
    """
    Cierra el pool de conexiones y libera recursos.

    Debe llamarse al apagar el servidor para cerrar conexiones limpiamente.
    """
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Pool de conexiones PostgreSQL cerrado.")
