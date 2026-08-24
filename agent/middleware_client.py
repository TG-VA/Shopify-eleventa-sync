"""
Cliente HTTP para comunicación con el middleware en la nube.

Envía cambios de inventario detectados en Eleventa al middleware
y recibe ajustes pendientes provenientes de Shopify para aplicar
localmente.
"""

import logging
import time
from typing import Optional

import httpx

from agent.config import settings
from shared.models import (
    InventoryChange,
    SyncRequest,
    SyncResponse,
    PendingAdjustment,
    AdjustmentConfirmation,
)

logger = logging.getLogger(__name__)

# ── Configuración de reintentos ─────────────────────────────────────────
_MAX_RETRIES = 3
_INITIAL_BACKOFF_SECONDS = 1.0
_BACKOFF_MULTIPLIER = 2.0
_REQUEST_TIMEOUT_SECONDS = 30.0


class MiddlewareClient:
    """
    Cliente HTTP síncrono para el middleware de sincronización.

    Características:
        - Autenticación via X-Agent-Api-Key header.
        - Reintentos con backoff exponencial (3 intentos).
        - Timeouts configurables.
        - Logging detallado de cada operación.
    """

    def __init__(self) -> None:
        self._base_url = settings.MIDDLEWARE_URL.rstrip("/")
        self._headers = {
            "X-Agent-Api-Key": settings.AGENT_API_KEY,
            "Content-Type": "application/json",
            "X-Agent-Id": settings.AGENT_ID,
        }
        self._client: Optional[httpx.Client] = None

        logger.info(
            "MiddlewareClient inicializado — URL: %s | Agente: %s",
            self._base_url,
            settings.AGENT_ID,
        )

    def _get_client(self) -> httpx.Client:
        """Obtiene o crea una instancia del cliente HTTP."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                base_url=self._base_url,
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        return self._client

    def _request_with_retry(
        self,
        method: str,
        path: str,
        json_data: Optional[dict] = None,
    ) -> httpx.Response:
        """
        Ejecuta una petición HTTP con reintentos y backoff exponencial.

        Args:
            method: Método HTTP (GET, POST, PUT, etc.)
            path: Ruta relativa del endpoint.
            json_data: Datos JSON a enviar en el cuerpo (opcional).

        Returns:
            Respuesta HTTP del middleware.

        Raises:
            httpx.HTTPError: Si todos los reintentos fallan.
        """
        client = self._get_client()
        last_exception: Optional[Exception] = None
        backoff = _INITIAL_BACKOFF_SECONDS

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = client.request(method, path, json=json_data)
                response.raise_for_status()

                logger.debug(
                    "Petición exitosa: %s %s (intento %d/%d)",
                    method,
                    path,
                    attempt,
                    _MAX_RETRIES,
                )
                return response

            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exception = exc
                logger.warning(
                    "Error en petición %s %s (intento %d/%d): %s",
                    method,
                    path,
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )

                if attempt < _MAX_RETRIES:
                    logger.info(
                        "Reintentando en %.1f segundos...", backoff
                    )
                    time.sleep(backoff)
                    backoff *= _BACKOFF_MULTIPLIER

        # Todos los reintentos agotados
        logger.error(
            "Todos los reintentos agotados para %s %s", method, path
        )
        raise last_exception  # type: ignore[misc]

    # ── Endpoints del middleware ─────────────────────────────────────────

    def send_inventory_changes(
        self, changes: list[InventoryChange]
    ) -> SyncResponse:
        """
        Envía los cambios de inventario detectados en Eleventa al middleware.

        Args:
            changes: Lista de cambios detectados desde el último snapshot.

        Returns:
            SyncResponse del middleware con el resultado del procesamiento.
        """
        logger.info(
            "Enviando %d cambios de inventario al middleware...", len(changes)
        )

        request = SyncRequest(
            agent_id=settings.AGENT_ID,
            changes=changes,
        )

        response = self._request_with_retry(
            "POST",
            "/api/sync/inventory-changes",
            json_data=request.model_dump(),
        )

        sync_response = SyncResponse.model_validate(response.json())
        logger.info(
            "Respuesta del middleware: %s — %s",
            sync_response.status,
            sync_response.message if hasattr(sync_response, "message") else "OK",
        )
        return sync_response

    def get_pending_adjustments(self) -> list[PendingAdjustment]:
        """
        Consulta al middleware por ajustes pendientes de Shopify
        que deben aplicarse en Eleventa.

        Returns:
            Lista de ajustes pendientes para aplicar localmente.
        """
        logger.info("Consultando ajustes pendientes desde el middleware...")

        response = self._request_with_retry(
            "GET",
            f"/api/sync/pending-adjustments/{settings.AGENT_ID}",
        )

        adjustments = [
            PendingAdjustment.model_validate(item)
            for item in response.json()
        ]

        if adjustments:
            logger.info(
                "Recibidos %d ajustes pendientes del middleware.",
                len(adjustments),
            )
        else:
            logger.debug("No hay ajustes pendientes.")

        return adjustments

    def confirm_adjustment(
        self, confirmation: AdjustmentConfirmation
    ) -> bool:
        """
        Confirma al middleware que un ajuste fue aplicado en Eleventa.

        Args:
            confirmation: Datos de confirmación del ajuste aplicado.

        Returns:
            True si la confirmación fue aceptada por el middleware.
        """
        try:
            self._request_with_retry(
                "POST",
                "/api/sync/confirm-adjustment",
                json_data=confirmation.model_dump(),
            )
            logger.info(
                "Ajuste confirmado exitosamente: %s", confirmation.adjustment_id
            )
            return True
        except Exception as exc:
            logger.error(
                "Error al confirmar ajuste %s: %s",
                confirmation.adjustment_id,
                exc,
            )
            return False

    def health_check(self) -> bool:
        """
        Verifica la conectividad con el middleware.

        Returns:
            True si el middleware responde correctamente.
        """
        try:
            response = self._request_with_retry("GET", "/api/health")
            is_healthy = response.status_code == 200
            logger.info(
                "Health check del middleware: %s",
                "✅ OK" if is_healthy else "❌ Error",
            )
            return is_healthy
        except Exception as exc:
            logger.error("Health check fallido: %s", exc)
            return False

    def close(self) -> None:
        """Cierra el cliente HTTP."""
        if self._client is not None and not self._client.is_closed:
            self._client.close()
            logger.info("MiddlewareClient cerrado.")
