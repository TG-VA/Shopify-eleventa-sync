from __future__ import annotations

import logging
from typing import Any

import httpx

from agent.config import settings

logger = logging.getLogger(__name__)


class MiddlewareClient:
    """
    Cliente para comunicarse con el Middleware (API FastAPI) en la nube o local.
    """

    def __init__(
        self,
        base_url: str = settings.MIDDLEWARE_URL,
        api_key: str = settings.AGENT_API_KEY,
    ):
        # Asegurar que no termine en /
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def get_pending_adjustments(self) -> list[dict[str, Any]]:
        """
        Consulta al middleware por ajustes pendientes que deban aplicarse en Eleventa.
        """
        url = f"{self.base_url}/agent/pending-adjustments"
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, headers=self.headers)
                
                if response.status_code == 200:
                    data = response.json()
                    return data.get("adjustments", [])
                else:
                    logger.warning(
                        "Error obteniendo ajustes pendientes. Status: %s, Body: %s",
                        response.status_code,
                        response.text,
                    )
                    return []
        except httpx.RequestError as e:
            logger.error("Error de red conectando al middleware: %s", e)
            return []

    def mark_adjustment_completed(self, adjustment_id: int) -> bool:
        """
        Informa al middleware que un ajuste fue aplicado exitosamente en Eleventa.
        """
        url = f"{self.base_url}/agent/adjustments/{adjustment_id}/complete"
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, headers=self.headers)
                
                if response.status_code == 200:
                    logger.info("Ajuste %s marcado como completado.", adjustment_id)
                    return True
                else:
                    logger.warning(
                        "Error marcando ajuste %s como completado. Status: %s, Body: %s",
                        adjustment_id,
                        response.status_code,
                        response.text,
                    )
                    return False
        except httpx.RequestError as e:
            logger.error("Error de red conectando al middleware: %s", e)
            return False

    def mark_adjustment_failed(self, adjustment_id: int, error_message: str) -> bool:
        """
        Informa al middleware que hubo un error al aplicar un ajuste.
        """
        url = f"{self.base_url}/agent/adjustments/{adjustment_id}/fail"
        payload = {"error": error_message}
        
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, json=payload, headers=self.headers)
                
                if response.status_code == 200:
                    logger.info("Ajuste %s reportado con error.", adjustment_id)
                    return True
                else:
                    logger.warning(
                        "Error marcando ajuste %s como fallido. Status: %s",
                        adjustment_id,
                        response.status_code,
                    )
                    return False
        except httpx.RequestError as e:
            logger.error("Error de red conectando al middleware: %s", e)
            return False

    def report_eleventa_sales(self, sales: list[dict[str, Any]]) -> bool:
        """
        Envía las nuevas ventas físicas de Eleventa al Middleware para que
        descuente el inventario en Shopify.
        """
        if not sales:
            return True

        url = f"{self.base_url}/agent/eleventa-sales"
        payload = {"sales": sales}

        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.post(url, json=payload, headers=self.headers)
                
                if response.status_code == 200:
                    logger.info("Ventas reportadas exitosamente al Middleware.")
                    return True
                else:
                    logger.warning(
                        "Error reportando ventas. Status: %s, Body: %s",
                        response.status_code,
                        response.text,
                    )
                    return False
        except httpx.RequestError as e:
            logger.error("Error de red conectando al middleware: %s", e)
            return False
