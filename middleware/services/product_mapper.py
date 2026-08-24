"""
Mapeador de productos entre Eleventa y Shopify.

Mantiene un caché en memoria respaldado por PostgreSQL para traducir
entre códigos de Eleventa e IDs de Shopify de forma eficiente.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from middleware.services.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)


class ProductMapper:
    """Mapea productos entre Eleventa (código) y Shopify (variant/inventory IDs)."""

    def __init__(self) -> None:
        """Inicializa el mapeador con cachés vacíos."""
        # Caché: eleventa_codigo -> dict con IDs de Shopify
        self._by_eleventa: dict[str, dict[str, Any]] = {}
        # Caché inverso: shopify_variant_id -> eleventa_codigo
        self._by_shopify: dict[int, str] = {}

    async def load_mappings(self, pool: asyncpg.Pool) -> None:
        """
        Carga todos los mapeos desde PostgreSQL al caché en memoria.

        Args:
            pool: Pool de conexiones asyncpg.
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM product_mapping")

        self._by_eleventa.clear()
        self._by_shopify.clear()

        for row in rows:
            mapping = dict(row)
            codigo = mapping["eleventa_codigo"]
            self._by_eleventa[codigo] = mapping
            self._by_shopify[mapping["shopify_variant_id"]] = codigo

        logger.info("Cargados %d mapeos de productos.", len(rows))

    def get_shopify_ids(self, eleventa_codigo: str) -> dict[str, Any] | None:
        """
        Obtiene los IDs de Shopify para un código de Eleventa.

        Args:
            eleventa_codigo: Código del producto en Eleventa.

        Returns:
            Diccionario con shopify_product_id, shopify_variant_id,
            shopify_inventory_item_id, o None si no existe mapeo.
        """
        mapping = self._by_eleventa.get(eleventa_codigo)
        if mapping is None:
            return None

        return {
            "shopify_product_id": mapping["shopify_product_id"],
            "shopify_variant_id": mapping["shopify_variant_id"],
            "shopify_inventory_item_id": mapping["shopify_inventory_item_id"],
            "sku": mapping.get("sku"),
            "titulo": mapping.get("titulo"),
        }

    def get_eleventa_codigo(self, shopify_variant_id: int) -> str | None:
        """
        Obtiene el código de Eleventa para un variant ID de Shopify.

        Args:
            shopify_variant_id: ID numérico de la variante en Shopify.

        Returns:
            Código de Eleventa o None si no existe mapeo.
        """
        return self._by_shopify.get(shopify_variant_id)

    async def create_mapping(
        self,
        pool: asyncpg.Pool,
        eleventa_codigo: str,
        shopify_product_id: int | None,
        shopify_variant_id: int,
        shopify_inventory_item_id: int,
        sku: str | None = None,
        titulo: str | None = None,
    ) -> dict[str, Any]:
        """
        Crea un nuevo mapeo en PostgreSQL y en el caché.

        Args:
            pool: Pool de conexiones asyncpg.
            eleventa_codigo: Código del producto en Eleventa.
            shopify_product_id: ID del producto en Shopify.
            shopify_variant_id: ID de la variante en Shopify.
            shopify_inventory_item_id: ID del inventory item en Shopify.
            sku: SKU de la variante (opcional).
            titulo: Título del producto (opcional).

        Returns:
            Diccionario con el mapeo creado.
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO product_mapping
                    (eleventa_codigo, shopify_product_id, shopify_variant_id,
                     shopify_inventory_item_id, sku, titulo)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (eleventa_codigo) DO UPDATE SET
                    shopify_product_id = EXCLUDED.shopify_product_id,
                    shopify_variant_id = EXCLUDED.shopify_variant_id,
                    shopify_inventory_item_id = EXCLUDED.shopify_inventory_item_id,
                    sku = EXCLUDED.sku,
                    titulo = EXCLUDED.titulo,
                    updated_at = NOW()
                RETURNING *
                """,
                eleventa_codigo,
                shopify_product_id,
                shopify_variant_id,
                shopify_inventory_item_id,
                sku,
                titulo,
            )

        mapping = dict(row)  # type: ignore[arg-type]

        # Actualizar caché
        self._by_eleventa[eleventa_codigo] = mapping
        self._by_shopify[shopify_variant_id] = eleventa_codigo

        logger.info(
            "Mapeo creado/actualizado: %s -> variant=%d, item=%d",
            eleventa_codigo,
            shopify_variant_id,
            shopify_inventory_item_id,
        )
        return mapping

    async def auto_map_products(
        self,
        eleventa_products: list[dict[str, Any]],
        shopify_client: ShopifyClient,
        pool: asyncpg.Pool,
    ) -> dict[str, int]:
        """
        Intenta mapear automáticamente productos de Eleventa con Shopify por SKU.

        Compara los códigos de Eleventa con los SKU de los productos en Shopify.
        Cuando encuentra coincidencia, crea el mapeo automáticamente.

        Args:
            eleventa_products: Lista de productos de Eleventa con campo 'codigo'.
            shopify_client: Cliente de Shopify para obtener productos.
            pool: Pool de conexiones asyncpg.

        Returns:
            Diccionario con conteos: mapped (mapeados), skipped (ya existían),
            unmatched (sin coincidencia).
        """
        stats = {"mapped": 0, "skipped": 0, "unmatched": 0}

        # Obtener todos los productos de Shopify
        shopify_by_sku: dict[str, dict[str, Any]] = {}
        after = None
        has_next = True

        while has_next:
            products_data = await shopify_client.get_products(first=50, after=after)
            page_info = products_data.get("pageInfo", {})
            has_next = page_info.get("hasNextPage", False)
            after = page_info.get("endCursor")

            for edge in products_data.get("edges", []):
                product = edge["node"]
                for var_edge in product.get("variants", {}).get("edges", []):
                    variant = var_edge["node"]
                    sku = variant.get("sku", "")
                    if sku:
                        shopify_by_sku[sku.strip().upper()] = {
                            "product_id": _gid_to_int(product["id"]),
                            "variant_id": _gid_to_int(variant["id"]),
                            "inventory_item_id": _gid_to_int(
                                variant["inventoryItem"]["id"]
                            ),
                            "title": product.get("title", ""),
                            "sku": sku,
                        }

        logger.info("Encontrados %d SKUs en Shopify.", len(shopify_by_sku))

        # Intentar mapear cada producto de Eleventa
        for ep in eleventa_products:
            codigo = ep.get("codigo", "").strip().upper()
            if not codigo:
                continue

            # Verificar si ya está mapeado
            if self.get_shopify_ids(codigo):
                stats["skipped"] += 1
                continue

            # Buscar por SKU
            shopify_match = shopify_by_sku.get(codigo)
            if shopify_match:
                await self.create_mapping(
                    pool=pool,
                    eleventa_codigo=ep["codigo"],
                    shopify_product_id=shopify_match["product_id"],
                    shopify_variant_id=shopify_match["variant_id"],
                    shopify_inventory_item_id=shopify_match["inventory_item_id"],
                    sku=shopify_match["sku"],
                    titulo=shopify_match["title"],
                )
                stats["mapped"] += 1
            else:
                stats["unmatched"] += 1
                logger.debug("Sin coincidencia en Shopify para: %s", codigo)

        logger.info(
            "Auto-mapeo completado: %d mapeados, %d omitidos, %d sin coincidencia.",
            stats["mapped"],
            stats["skipped"],
            stats["unmatched"],
        )
        return stats


def _gid_to_int(gid: str) -> int:
    """
    Extrae el ID numérico de un GID de Shopify.

    Ej: 'gid://shopify/Product/123456' -> 123456

    Args:
        gid: Global ID de Shopify.

    Returns:
        ID numérico.
    """
    return int(gid.split("/")[-1])
