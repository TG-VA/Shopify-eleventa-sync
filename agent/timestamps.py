"""
Utilidades de normalización de timestamps para Firebird.

Firebird TIMESTAMP no acepta el separador 'T' del formato ISO 8601 ni
información de zona horaria: un valor como ``2026-10-04T17:26:19`` provoca
``conversion error from string``. Todas las consultas del agente deben
recibir un ``datetime`` nativo (naive) o la cadena
``YYYY-MM-DD HH:MM:SS``.
"""

from __future__ import annotations

from datetime import datetime

FIREBIRD_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_FALLBACK_FORMATS: tuple[str, ...] = (
    FIREBIRD_TIMESTAMP_FORMAT,
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d",
)


def _strip_timezone(value: datetime) -> datetime:
    """Convierte a naive; los valores con offset se pasan a hora local."""
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


def parse_datetime(value: str | datetime) -> datetime:
    """
    Convierte un ``str`` o ``datetime`` a ``datetime`` naive.

    Acepta ISO 8601 con separador 'T', con o sin fracciones y con offset
    UTC; también el formato nativo de Firebird ``YYYY-MM-DD HH:MM:SS``.

    Args:
        value: Timestamp a interpretar.

    Returns:
        ``datetime`` naive listo para bindear contra Firebird.

    Raises:
        ValueError: Si el valor no representa una fecha/hora válida.
    """
    if isinstance(value, datetime):
        return _strip_timezone(value)

    text = str(value).strip()
    if not text:
        raise ValueError("Timestamp vacío.")

    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"

    try:
        return _strip_timezone(datetime.fromisoformat(text))
    except ValueError:
        pass

    for fmt in _FALLBACK_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    raise ValueError(f"Formato de timestamp no reconocido: {value!r}")


def to_firebird_datetime(value: str | datetime) -> datetime:
    """Devuelve un ``datetime`` naive para usar como parámetro de Firebird."""
    return parse_datetime(value)


def format_firebird_timestamp(value: str | datetime) -> str:
    """
    Serializa un timestamp al formato ``YYYY-MM-DD HH:MM:SS``.

    El resultado es ordenable lexicográficamente y compatible con
    ``CAST(? AS TIMESTAMP)`` en Firebird 2.5/3.0.
    """
    return parse_datetime(value).strftime(FIREBIRD_TIMESTAMP_FORMAT)


def now_firebird_timestamp() -> str:
    """Timestamp actual local en formato Firebird."""
    return format_firebird_timestamp(datetime.now())
