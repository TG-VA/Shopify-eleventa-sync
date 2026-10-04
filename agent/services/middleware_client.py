"""
Cliente HTTP único para la comunicación agente ↔ middleware.

Características:
    - Autenticación mediante el header `X-Agent-Api-Key`.
    - Reintentos con backoff exponencial para errores de red y respuestas
      transitorias (429, 5xx); los errores de cliente (4xx) no se reintentan.
    - Connection pool reutilizable (httpx.Client) y cierre explícito.
    - Payloads validados contra `shared.models` (Pydantic v2).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from types import TracebackType
from typing import Any

import httpx
from pydantic import ValidationError

from agent.config import settings
from agent.timestamps import parse_datetime
from shared.models import (
    AdjustmentConfirmation,
    InventoryChange,
    PendingAdjustment,
    SyncRequest,
    SyncResponse,
)

logger = logging.getLogger(__name__)

# ── Configuración de reintentos ─────────────────────────────────────────
_MAX_RETRIES = 3
_INITIAL_BACKOFF_SECONDS = 1.0
_BACKOFF_MULTIPLIER = 2.0
_BACKOFF_MAX_SECONDS = 30.0
_REQUEST_TIMEOUT_SECONDS = 30.0
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class MiddlewareClient:
    """
    Cliente HTTP síncrono del middleware de sincronización.

    Es la única implementación de acceso al middleware: los workers
    `sync_up` y `sync_down` comparten esta clase.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._base_url = (base_url or settings.MIDDLEWARE_URL).rstrip("/")
        self._api_key = api_key or settings.AGENT_API_KEY
        self._max_retries = max(1, max_retries)
        self._headers = {
            "X-Agent-Api-Key": self._api_key,
            "X-Agent-Id": settings.AGENT_ID,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=timeout,
        )

        logger.info(
            "MiddlewareClient inicializado — URL: %s | Agente: %s",
            self._base_url,
            settings.AGENT_ID,
        )

    # ── Ciclo de vida ────────────────────────────────────────────────────

    def close(self) -> None:
        """Cierra el cliente HTTP de forma idempotente."""
        if not self._client.is_closed:
            self._client.close()
            logger.debug("MiddlewareClient cerrado.")

    def __enter__(self) -> MiddlewareClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ── Transporte ───────────────────────────────────────────────────────

    def _request_with_retry(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """
        Ejecuta una petición HTTP con reintentos y backoff exponencial.

        Args:
            method: Método HTTP (GET, POST, ...).
            path: Ruta del endpoint.
            json_data: Cuerpo JSON serializable (opcional).

        Returns:
            La respuesta HTTP, incluidos los errores de cliente (4xx) que no
            se consideran reintentables.

        Raises:
            httpx.HTTPStatusError: Tras agotar reintentos o ante un 4xx no
                reintentable.
            httpx.RequestError: Si todos los intentos fallan por red.
        """
        backoff = _INITIAL_BACKOFF_SECONDS
        last_error: httpx.HTTPError | None = None

        for attempt in range(1, self._max_retries + 1):
            is_last_attempt = attempt == self._max_retries

            try:
                response = self._client.request(method, path, json=json_data)
            except httpx.RequestError as exc:
                last_error = exc
                logger.warning(
                    "Error de red en %s %s (intento %d/%d): %s",
                    method, path, attempt, self._max_retries, exc,
                )
            else:
                if response.status_code < 400:
                    logger.debug(
                        "Petición exitosa: %s %s (intento %d/%d)",
                        method, path, attempt, self._max_retries,
                    )
                    return response

                status_error = httpx.HTTPStatusError(
                    f"{response.status_code} en {method} {path}",
                    request=response.request,
                    response=response,
                )

                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    logger.warning(
                        "Error no reintentable en %s %s: %s",
                        method, path, response.status_code,
                    )
                    raise status_error

                last_error = status_error
                logger.warning(
                    "Error transitorio en %s %s (intento %d/%d): %s",
                    method, path, attempt, self._max_retries, response.status_code,
                )

            if not is_last_attempt:
                logger.info("Reintentando en %.1f segundos...", backoff)
                time.sleep(backoff)
                backoff = min(backoff * _BACKOFF_MULTIPLIER, _BACKOFF_MAX_SECONDS)

        logger.error("Reintentos agotados para %s %s", method, path)
        raise last_error if last_error else httpx.RequestError(f"Fallo en {method} {path}")

    # ── Endpoints del middleware ─────────────────────────────────────────

    def send_inventory_changes(
        self, changes: list[InventoryChange]
    ) -> SyncResponse:
        """
        Envía cambios de inventario Eleventa → Shopify al middleware.

        Args:
            changes: Cambios validados como `InventoryChange`.

        Returns:
            `SyncResponse` con la cantidad procesada y los errores reportados.

        Raises:
            httpx.HTTPError: Si la llamada falla tras los reintentos.
            ValidationError: Si la respuesta no cumple el contrato.
        """
        request = SyncRequest(changes=changes, agent_id=settings.AGENT_ID)
        # mode="json" serializa los datetimes de InventoryChange; model_dump()
        # en modo python dejaría objetos datetime que httpx no puede serializar.
        response = self._request_with_retry(
            "POST",
            "/api/agent/inventory-changes",
            json_data=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        return SyncResponse.model_validate(response.json())

    def get_pending_adjustments(self) -> list[PendingAdjustment]:
        """
        Consulta los ajustes pendientes que el agente debe aplicar en Eleventa.

        Returns:
            Ajustes válidos; lista vacía si hay error de red o de contrato.
        """
        try:
            response = self._request_with_retry("GET", "/api/agent/pending-adjustments")
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Error obteniendo ajustes pendientes. Status: %s, Body: %s",
                exc.response.status_code,
                exc.response.text,
            )
            return []
        except httpx.HTTPError as exc:
            logger.error("Error de red conectando al middleware: %s", exc)
            return []

        if response.status_code != 200:
            logger.warning(
                "Respuesta inesperada al pedir ajustes: %s", response.status_code
            )
            return []

        try:
            payload = response.json()
        except ValueError:
            logger.error("Respuesta no JSON al pedir ajustes pendientes.")
            return []

        raw_adjustments = (
            payload.get("adjustments", []) if isinstance(payload, dict) else payload
        )

        adjustments: list[PendingAdjustment] = []
        for item in raw_adjustments or []:
            try:
                adjustments.append(PendingAdjustment.model_validate(item))
            except ValidationError as exc:
                logger.warning("Ajuste inválido descartado: %s", exc)

        logger.debug("Ajustes pendientes recibidos: %d", len(adjustments))
        return adjustments

    def confirm_adjustment(
        self,
        adjustment_id: str | int,
        success: bool,
        message: str = "",
    ) -> bool:
        """
        Confirma al middleware si un ajuste fue aplicado en Eleventa.

        Args:
            adjustment_id: Identificador del ajuste.
            success: True si se aplicó correctamente.
            message: Detalle del resultado o del error.

        Returns:
            True si el middleware aceptó la confirmación.
        """
        confirmation = AdjustmentConfirmation(
            adjustment_id=str(adjustment_id),
            success=success,
            message=message,
        )
        try:
            self._request_with_retry(
                "POST",
                "/api/agent/confirm-adjustment",
                json_data=confirmation.model_dump(mode="json"),
            )
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Error confirmando ajuste %s. Status: %s, Body: %s",
                adjustment_id, exc.response.status_code, exc.response.text,
            )
            return False
        except httpx.HTTPError as exc:
            logger.error("Error de red confirmando ajuste %s: %s", adjustment_id, exc)
            return False

        logger.info(
            "Ajuste %s confirmado (success=%s).", adjustment_id, success
        )
        return True

    def mark_adjustment_completed(self, adjustment_id: str | int) -> bool:
        """Marca un ajuste como aplicado correctamente."""
        return self.confirm_adjustment(
            adjustment_id, True, "Aplicado exitosamente en Eleventa"
        )

    def mark_adjustment_failed(
        self, adjustment_id: str | int, error_message: str
    ) -> bool:
        """Marca un ajuste como fallido."""
        return self.confirm_adjustment(adjustment_id, False, error_message)

    def health_check(self) -> bool:
        """
        Verifica la conectividad con el middleware.

        Returns:
            True si `/health` responde correctamente.
        """
        try:
            response = self._request_with_retry("GET", "/health")
        except httpx.HTTPError as exc:
            logger.error("Health check del middleware fallido: %s", exc)
            return False

        is_healthy = response.status_code == 200
        logger.info("Health check del middleware: %s", "OK" if is_healthy else "ERROR")
        return is_healthy

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_inventory_changes(
        sales: list[dict[str, Any]],
    ) -> list[InventoryChange]:
        """
        Convierte las ventas de Eleventa en `InventoryChange` válidos.

        Para ventas el campo autoritativo es `delta` (negativo = resta);
        `existencia_anterior`/`existencia_nueva` no son calculables desde el
        ticket y quedan en 0.0. Las ventas sin código se descartan con aviso.

        Args:
            sales: Ventas devueltas por `FirebirdClient.get_recent_sales`.

        Returns:
            Lista de cambios validados contra el contrato compartido.
        """
        changes: list[InventoryChange] = []

        for sale in sales:
            codigo = str(sale.get("codigo") or "").strip()
            if not codigo:
                logger.warning(
                    "Venta %s sin código de producto, se omite.",
                    sale.get("ticket_id"),
                )
                continue

            try:
                cantidad = abs(float(sale.get("cantidad") or 1.0))
            except (TypeError, ValueError):
                logger.warning(
                    "Cantidad inválida en la venta %s, se asume 1.0.",
                    sale.get("ticket_id"),
                )
                cantidad = 1.0

            raw_timestamp = sale.get("timestamp")
            try:
                timestamp = (
                    parse_datetime(raw_timestamp) if raw_timestamp else datetime.now()
                )
            except ValueError:
                logger.warning(
                    "Timestamp inválido en la venta %s, se usa la hora actual.",
                    sale.get("ticket_id"),
                )
                timestamp = datetime.now()

            changes.append(
                InventoryChange(
                    codigo=codigo,
                    existencia_anterior=0.0,
                    existencia_nueva=0.0,
                    delta=-cantidad,
                    source="eleventa",
                    timestamp=timestamp,
                )
            )

        return changes

    def report_eleventa_sales(self, sales: list[dict[str, Any]]) -> bool:
        """
        Envía las ventas físicas de Eleventa al middleware para que
        descuente el inventario en Shopify.

        Args:
            sales: Ventas a reportar (contrato de `get_recent_sales`).

        Returns:
            True si el lote fue aceptado, para que el worker avance el cursor.
            Un envío fallido devuelve False y el lote se reintentará en el
            siguiente ciclo.
        """
        if not sales:
            return True

        changes = self._build_inventory_changes(sales)
        if not changes:
            logger.warning("Ninguna venta válida para reportar; lote descartado.")
            return True

        try:
            result = self.send_inventory_changes(changes)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Error reportando ventas. Status: %s, Body: %s",
                exc.response.status_code,
                exc.response.text,
            )
            return False
        except (httpx.HTTPError, ValidationError) as exc:
            logger.error("Error reportando ventas al middleware: %s", exc)
            return False

        if result.errors:
            # El cursor avanza igualmente: los cambios ya aceptados no deben
            # reenviarse. Se registran para revisión manual.
            logger.error(
                "El middleware reportó %d errores sobre %d cambios: %s",
                len(result.errors),
                len(changes),
                result.errors,
            )

        logger.info(
            "Ventas reportadas: %d cambios enviados, %d procesados.",
            len(changes),
            result.processed,
        )
        return True
