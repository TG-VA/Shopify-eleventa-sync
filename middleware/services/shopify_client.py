"""
Cliente asíncrono para la API GraphQL Admin de Shopify.

Maneja autenticación, paginación, rate limiting y las mutaciones
necesarias para sincronizar inventario.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ShopifyClient:
    """Cliente para interactuar con la API GraphQL Admin de Shopify."""

    def __init__(
        self,
        store_url: str,
        access_token: str,
        api_version: str = "2025-01",
    ) -> None:
        """
        Inicializa el cliente de Shopify.

        Args:
            store_url: URL de la tienda (ej. 'mi-tienda.myshopify.com').
            access_token: Token de acceso de la app privada.
            api_version: Versión de la API de Shopify.
        """
        self.store_url = store_url.rstrip("/")
        self.access_token = access_token
        self.api_version = api_version
        self.graphql_url = (
            f"https://{self.store_url}/admin/api/{self.api_version}/graphql.json"
        )
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "X-Shopify-Access-Token": self.access_token,
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        """Cierra el cliente HTTP."""
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Petición GraphQL base con manejo de rate limiting
    # ------------------------------------------------------------------ #

    async def graphql_request(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Ejecuta una petición GraphQL contra la Admin API.

        Maneja automáticamente el rate limiting usando el encabezado
        X-Shopify-Shop-Api-Call-Limit y reintenta con back-off.

        Args:
            query: Consulta o mutación GraphQL.
            variables: Variables para la consulta.

        Returns:
            Cuerpo de la respuesta JSON.

        Raises:
            httpx.HTTPStatusError: Si la respuesta tiene un código de error.
            RuntimeError: Si la respuesta contiene errores de GraphQL.
        """
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        max_retries = 3
        for attempt in range(max_retries):
            response = await self._client.post(self.graphql_url, json=payload)

            # Manejar rate limiting (429 o throttled)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "2.0"))
                logger.warning(
                    "Rate limit alcanzado. Reintentando en %.1f segundos (intento %d/%d).",
                    retry_after,
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(retry_after)
                continue

            response.raise_for_status()

            # Verificar límite de llamadas y hacer back-off preventivo
            call_limit = response.headers.get("X-Shopify-Shop-Api-Call-Limit", "")
            if call_limit:
                used, total = call_limit.split("/")
                usage_ratio = int(used) / int(total)
                if usage_ratio > 0.8:
                    wait = 1.0 if usage_ratio < 0.9 else 2.0
                    logger.info(
                        "Uso de API al %.0f%%, esperando %.1f seg.",
                        usage_ratio * 100,
                        wait,
                    )
                    await asyncio.sleep(wait)

            data = response.json()

            # Verificar errores de GraphQL
            if "errors" in data:
                # Throttling en GraphQL
                error_codes = [
                    e.get("extensions", {}).get("code")
                    for e in data.get("errors", [])
                ]
                if "THROTTLED" in error_codes and attempt < max_retries - 1:
                    logger.warning(
                        "Throttled por GraphQL. Reintentando en 2 seg (intento %d/%d).",
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(2.0)
                    continue

                error_messages = [e.get("message", "") for e in data["errors"]]
                raise RuntimeError(
                    f"Errores de GraphQL: {'; '.join(error_messages)}"
                )

            return data

        raise RuntimeError(
            f"Se agotaron los reintentos ({max_retries}) para la petición GraphQL."
        )

    # ------------------------------------------------------------------ #
    # Productos
    # ------------------------------------------------------------------ #

    async def get_products(
        self,
        first: int = 50,
        after: str | None = None,
    ) -> dict[str, Any]:
        """
        Obtiene productos paginados con sus variantes e inventario.

        Args:
            first: Cantidad de productos a obtener.
            after: Cursor para paginación.

        Returns:
            Datos de productos con información de paginación.
        """
        query = """
        query GetProducts($first: Int!, $after: String) {
            products(first: $first, after: $after) {
                pageInfo {
                    hasNextPage
                    endCursor
                }
                edges {
                    cursor
                    node {
                        id
                        title
                        handle
                        variants(first: 10) {
                            edges {
                                node {
                                    id
                                    title
                                    sku
                                    inventoryItem {
                                        id
                                        inventoryLevels(first: 5) {
                                            edges {
                                                node {
                                                    id
                                                    quantities(names: ["available"]) {
                                                        name
                                                        quantity
                                                    }
                                                    location {
                                                        id
                                                        name
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        variables: dict[str, Any] = {"first": first}
        if after:
            variables["after"] = after

        result = await self.graphql_request(query, variables)
        return result.get("data", {}).get("products", {})

    # ------------------------------------------------------------------ #
    # Niveles de inventario
    # ------------------------------------------------------------------ #

    async def get_inventory_level(
        self,
        inventory_item_id: str,
        location_id: str,
    ) -> dict[str, Any] | None:
        """
        Obtiene el nivel de inventario de un artículo en una ubicación.

        Args:
            inventory_item_id: GID del inventory item.
            location_id: GID de la ubicación.

        Returns:
            Datos del nivel de inventario o None si no existe.
        """
        query = """
        query GetInventoryLevel($inventoryItemId: ID!, $locationId: ID!) {
            inventoryItem(id: $inventoryItemId) {
                id
                inventoryLevel(locationId: $locationId) {
                    id
                    quantities(names: ["available", "on_hand"]) {
                        name
                        quantity
                    }
                    location {
                        id
                        name
                    }
                }
            }
        }
        """
        variables = {
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
        }
        result = await self.graphql_request(query, variables)
        item = result.get("data", {}).get("inventoryItem")
        if item:
            return item.get("inventoryLevel")
        return None

    # ------------------------------------------------------------------ #
    # Ajuste de inventario
    # ------------------------------------------------------------------ #

    async def adjust_inventory(
        self,
        inventory_item_id: str,
        location_id: str,
        delta: int,
        reason: str = "correction",
    ) -> dict[str, Any]:
        """
        Ajusta la cantidad de inventario usando inventoryAdjustQuantities.

        Args:
            inventory_item_id: GID del inventory item.
            location_id: GID de la ubicación.
            delta: Cantidad a ajustar (positivo suma, negativo resta).
            reason: Razón del ajuste para el ledger de Shopify.

        Returns:
            Resultado de la mutación.

        Raises:
            RuntimeError: Si la mutación devuelve userErrors.
        """
        mutation = """
        mutation AdjustInventory(
            $reason: String!,
            $name: String!,
            $changes: [InventoryChangeInput!]!
        ) {
            inventoryAdjustQuantities(
                input: {
                    reason: $reason
                    name: $name
                    changes: $changes
                }
            ) {
                inventoryAdjustmentGroup {
                    createdAt
                    reason
                    changes(first: 5) {
                        edges {
                            node {
                                name
                                delta
                                quantityAfterChange
                            }
                        }
                    }
                }
                userErrors {
                    field
                    message
                    code
                }
            }
        }
        """
        variables = {
            "reason": reason,
            "name": "available",
            "changes": [
                {
                    "delta": delta,
                    "inventoryItemId": inventory_item_id,
                    "locationId": location_id,
                }
            ],
        }

        result = await self.graphql_request(mutation, variables)
        mutation_data = result.get("data", {}).get("inventoryAdjustQuantities", {})

        user_errors = mutation_data.get("userErrors", [])
        if user_errors:
            error_msgs = [
                f"{e.get('field', '?')}: {e.get('message', '')}"
                for e in user_errors
            ]
            raise RuntimeError(
                f"Error al ajustar inventario: {'; '.join(error_msgs)}"
            )

        logger.info(
            "Inventario ajustado: item=%s, location=%s, delta=%d, reason=%s",
            inventory_item_id,
            location_id,
            delta,
            reason,
        )
        return mutation_data

    # ------------------------------------------------------------------ #
    # Ubicaciones
    # ------------------------------------------------------------------ #

    async def get_locations(self) -> list[dict[str, Any]]:
        """
        Obtiene todas las ubicaciones de la tienda.

        Returns:
            Lista de ubicaciones con id, nombre y estado.
        """
        query = """
        query GetLocations {
            locations(first: 50) {
                edges {
                    node {
                        id
                        name
                        isActive
                        address {
                            city
                            country
                        }
                    }
                }
            }
        }
        """
        result = await self.graphql_request(query)
        edges = result.get("data", {}).get("locations", {}).get("edges", [])
        return [edge["node"] for edge in edges]
