#!/usr/bin/env python3
"""
initial_sync.py — Sincronización inicial entre Eleventa y Shopify
=================================================================

Este script realiza el mapeo inicial de productos entre la base de datos
Firebird de Eleventa y el catálogo de Shopify. Genera un reporte de
coincidencias y guarda los mapeos en la base de datos PostgreSQL del middleware.

Uso:
    python scripts/initial_sync.py

Requisitos:
    - Python 3.11+
    - Variables de entorno configuradas en .env (ver .env.example)
    - Acceso de red a Firebird (Eleventa), Shopify API y PostgreSQL
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Dependencias externas
# ---------------------------------------------------------------------------
try:
    import asyncpg
    import httpx
    from dotenv import load_dotenv
    from firebird.driver import connect as fb_connect
except ImportError as exc:
    print(
        f"❌ Error: Falta la dependencia '{exc.name}'.\n"
        "   Instala las dependencias con: pip install -e .\n"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Cargar variables de entorno
# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# --- Shopify ---
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE_NAME", "")
SHOPIFY_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")
SHOPIFY_BASE_URL = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}"

# --- Eleventa (Firebird) ---
FB_HOST = os.getenv("ELEVENTA_DB_HOST", "localhost")
FB_PORT = int(os.getenv("ELEVENTA_DB_PORT", "3050"))
FB_DATABASE = os.getenv("ELEVENTA_DB_PATH", "")
FB_USER = os.getenv("ELEVENTA_DB_USER", "SYSDBA")
FB_PASSWORD = os.getenv("ELEVENTA_DB_PASSWORD", "masterkey")
FB_CHARSET = os.getenv("ELEVENTA_DB_CHARSET", "UTF8")

# --- PostgreSQL ---
DATABASE_URL = os.getenv("DATABASE_URL", "")

# --- Sincronización ---
NAME_MATCH_THRESHOLD = float(os.getenv("SYNC_NAME_MATCH_THRESHOLD", "0.85"))

# ---------------------------------------------------------------------------
# Colores para la terminal
# ---------------------------------------------------------------------------
class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    DIM = "\033[2m"


def _c(text: str, color: str) -> str:
    """Aplica color a un texto para la terminal."""
    return f"{color}{text}{Color.RESET}"


# ---------------------------------------------------------------------------
# Modelos de datos
# ---------------------------------------------------------------------------
@dataclass
class EleventaProduct:
    """Producto leído de la base de datos de Eleventa."""
    id: str
    name: str
    barcode: str
    sku: str
    price: float
    stock: float
    category: str = ""


@dataclass
class ShopifyProduct:
    """Producto leído del catálogo de Shopify."""
    product_id: int
    variant_id: int
    title: str
    barcode: str
    sku: str
    inventory_item_id: int
    inventory_quantity: int


@dataclass
class ProductMatch:
    """Resultado de un emparejamiento entre Eleventa y Shopify."""
    eleventa: EleventaProduct
    shopify: ShopifyProduct
    match_type: str  # "barcode", "sku", "name_similarity"
    similarity_score: float = 1.0
    confirmed: bool = False


@dataclass
class SyncReport:
    """Reporte completo de la sincronización inicial."""
    matched: list[ProductMatch] = field(default_factory=list)
    unmatched_eleventa: list[EleventaProduct] = field(default_factory=list)
    unmatched_shopify: list[ShopifyProduct] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# 1. Lectura de productos de Eleventa (Firebird)
# ---------------------------------------------------------------------------
def read_eleventa_products() -> list[EleventaProduct]:
    """
    Conecta a la base de datos Firebird de Eleventa y lee todos los productos.

    Nota: Los nombres de tablas y columnas pueden variar según la versión de Eleventa.
    Las tablas más comunes son ARTICULOS o PRODUCTOS.
    """
    print(f"\n{_c('📦 Leyendo productos de Eleventa...', Color.CYAN)}")
    print(f"   Host: {FB_HOST}:{FB_PORT}")
    print(f"   Base de datos: {FB_DATABASE}")

    products: list[EleventaProduct] = []

    # Consulta SQL para Eleventa — ajustar según la versión de tu base de datos
    # Variantes comunes de nombres de tabla: ARTICULOS, PRODUCTOS, ITEMS
    query = """
        SELECT
            a.ARTICULOID        AS id,
            a.DESCRIPCION       AS nombre,
            COALESCE(a.CODIGOBARRAS, '')   AS codigo_barras,
            COALESCE(a.CLAVE, '')          AS clave,
            COALESCE(a.PRECIO1, 0)         AS precio,
            COALESCE(a.EXISTENCIA, 0)      AS existencia,
            COALESCE(c.DESCRIPCION, '')    AS categoria
        FROM ARTICULOS a
        LEFT JOIN CATEGORIAS c ON a.CATEGORIAID = c.CATEGORIAID
        WHERE a.ESTATUS = 1
        ORDER BY a.DESCRIPCION
    """

    try:
        con = fb_connect(
            database=f"{FB_HOST}/{FB_PORT}:{FB_DATABASE}",
            user=FB_USER,
            password=FB_PASSWORD,
            charset=FB_CHARSET,
        )
        cursor = con.cursor()
        cursor.execute(query)

        for row in cursor.fetchall():
            products.append(
                EleventaProduct(
                    id=str(row[0]).strip(),
                    name=str(row[1]).strip(),
                    barcode=str(row[2]).strip(),
                    sku=str(row[3]).strip(),
                    price=float(row[4] or 0),
                    stock=float(row[5] or 0),
                    category=str(row[6]).strip(),
                )
            )

        cursor.close()
        con.close()

        print(f"   {_c(f'✅ {len(products)} productos encontrados', Color.GREEN)}")
        return products

    except Exception as e:
        print(f"   {_c(f'❌ Error al conectar con Eleventa: {e}', Color.RED)}")
        print(f"   {_c('Verifica la configuración en .env y que Firebird esté accesible', Color.DIM)}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# 2. Lectura de productos de Shopify
# ---------------------------------------------------------------------------
async def read_shopify_products() -> list[ShopifyProduct]:
    """
    Lee todos los productos del catálogo de Shopify usando la Admin API REST.
    Maneja paginación automáticamente usando el header 'Link'.
    """
    print(f"\n{_c('🛒 Leyendo productos de Shopify...', Color.CYAN)}")
    print(f"   Tienda: {SHOPIFY_STORE}.myshopify.com")

    products: list[ShopifyProduct] = []
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        url: str | None = f"{SHOPIFY_BASE_URL}/products.json?limit=250&fields=id,title,variants"

        page = 0
        while url:
            page += 1
            print(f"   Página {page}...", end=" ", flush=True)

            response = await client.get(url)

            if response.status_code != 200:
                print(f"\n   {_c(f'❌ Error de Shopify API: {response.status_code}', Color.RED)}")
                print(f"   {response.text[:300]}")
                sys.exit(1)

            data = response.json()
            page_products = data.get("products", [])
            print(f"{len(page_products)} productos")

            for product in page_products:
                for variant in product.get("variants", []):
                    products.append(
                        ShopifyProduct(
                            product_id=product["id"],
                            variant_id=variant["id"],
                            title=product["title"],
                            barcode=str(variant.get("barcode") or "").strip(),
                            sku=str(variant.get("sku") or "").strip(),
                            inventory_item_id=variant.get("inventory_item_id", 0),
                            inventory_quantity=variant.get("inventory_quantity", 0),
                        )
                    )

            # Paginación: seguir el enlace 'next' si existe
            url = _get_next_page_url(response.headers.get("Link", ""))

    print(f"   {_c(f'✅ {len(products)} variantes encontradas', Color.GREEN)}")
    return products


def _get_next_page_url(link_header: str) -> str | None:
    """Extrae la URL de la siguiente página del header Link de Shopify."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            url = part.split(";")[0].strip().strip("<>")
            return url
    return None


# ---------------------------------------------------------------------------
# 3. Motor de emparejamiento (matching)
# ---------------------------------------------------------------------------
def match_products(
    eleventa_products: list[EleventaProduct],
    shopify_products: list[ShopifyProduct],
) -> SyncReport:
    """
    Empareja productos entre Eleventa y Shopify usando tres estrategias:
    1. Coincidencia exacta por código de barras
    2. Coincidencia exacta por SKU/clave
    3. Similitud de nombre (difflib.SequenceMatcher)
    """
    print(f"\n{_c('🔗 Emparejando productos...', Color.CYAN)}")

    report = SyncReport()

    # Índices para búsqueda rápida
    shopify_by_barcode: dict[str, ShopifyProduct] = {}
    shopify_by_sku: dict[str, ShopifyProduct] = {}

    for sp in shopify_products:
        if sp.barcode:
            shopify_by_barcode[sp.barcode.lower()] = sp
        if sp.sku:
            shopify_by_sku[sp.sku.lower()] = sp

    matched_shopify_ids: set[int] = set()

    for ep in eleventa_products:
        match: ProductMatch | None = None

        # Estrategia 1: Código de barras exacto
        if ep.barcode:
            sp = shopify_by_barcode.get(ep.barcode.lower())
            if sp and sp.variant_id not in matched_shopify_ids:
                match = ProductMatch(
                    eleventa=ep,
                    shopify=sp,
                    match_type="barcode",
                    similarity_score=1.0,
                )

        # Estrategia 2: SKU/Clave exacto
        if match is None and ep.sku:
            sp = shopify_by_sku.get(ep.sku.lower())
            if sp and sp.variant_id not in matched_shopify_ids:
                match = ProductMatch(
                    eleventa=ep,
                    shopify=sp,
                    match_type="sku",
                    similarity_score=1.0,
                )

        # Estrategia 3: Similitud de nombre
        if match is None and ep.name:
            best_score = 0.0
            best_sp: ShopifyProduct | None = None

            for sp in shopify_products:
                if sp.variant_id in matched_shopify_ids:
                    continue

                score = SequenceMatcher(
                    None,
                    ep.name.lower().strip(),
                    sp.title.lower().strip(),
                ).ratio()

                if score > best_score:
                    best_score = score
                    best_sp = sp

            if best_sp and best_score >= NAME_MATCH_THRESHOLD:
                match = ProductMatch(
                    eleventa=ep,
                    shopify=best_sp,
                    match_type="name_similarity",
                    similarity_score=round(best_score, 4),
                )

        if match:
            report.matched.append(match)
            matched_shopify_ids.add(match.shopify.variant_id)
        else:
            report.unmatched_eleventa.append(ep)

    # Productos de Shopify sin emparejar
    for sp in shopify_products:
        if sp.variant_id not in matched_shopify_ids:
            report.unmatched_shopify.append(sp)

    # Resumen
    print(f"   {_c(f'✅ Emparejados:', Color.GREEN)} {len(report.matched)}")
    print(f"   {_c(f'⚠️  Sin emparejar (Eleventa):', Color.YELLOW)} {len(report.unmatched_eleventa)}")
    print(f"   {_c(f'⚠️  Sin emparejar (Shopify):', Color.YELLOW)} {len(report.unmatched_shopify)}")

    # Desglose por tipo de match
    by_type: dict[str, int] = {}
    for m in report.matched:
        by_type[m.match_type] = by_type.get(m.match_type, 0) + 1
    for mtype, count in by_type.items():
        label = {
            "barcode": "Código de barras",
            "sku": "SKU/Clave",
            "name_similarity": "Similitud de nombre",
        }.get(mtype, mtype)
        print(f"     └─ {label}: {count}")

    return report


# ---------------------------------------------------------------------------
# 4. Confirmación interactiva
# ---------------------------------------------------------------------------
def confirm_matches_interactive(report: SyncReport) -> SyncReport:
    """
    Muestra los emparejamientos al usuario para confirmación interactiva.
    Los matches por código de barras y SKU se auto-confirman.
    Los matches por similitud de nombre requieren confirmación manual.
    """
    print(f"\n{_c('👤 Confirmación interactiva de emparejamientos', Color.BOLD)}")
    print("=" * 70)

    # Auto-confirmar matches exactos
    auto_confirmed = 0
    for match in report.matched:
        if match.match_type in ("barcode", "sku"):
            match.confirmed = True
            auto_confirmed += 1

    print(f"\n   {_c(f'✅ {auto_confirmed} matches exactos auto-confirmados', Color.GREEN)}"
          f" (código de barras / SKU)")

    # Confirmar matches por nombre
    name_matches = [m for m in report.matched if m.match_type == "name_similarity"]

    if not name_matches:
        print(f"   No hay matches por similitud de nombre para confirmar.")
        return report

    print(f"\n   {_c(f'📝 {len(name_matches)} matches por similitud de nombre requieren confirmación:', Color.YELLOW)}")
    print(f"   {'─' * 66}")
    print(f"   Opciones: [s] Sí  |  [n] No  |  [t] Confirmar todos  |  [q] Salir")
    print(f"   {'─' * 66}")

    confirm_all = False

    for i, match in enumerate(name_matches, 1):
        similarity_pct = f"{match.similarity_score * 100:.1f}%"

        print(f"\n   [{i}/{len(name_matches)}] Similitud: {_c(similarity_pct, Color.CYAN)}")
        print(f"   Eleventa: {_c(match.eleventa.name, Color.BOLD)}")
        print(f"            Barcode: {match.eleventa.barcode or '—'} | SKU: {match.eleventa.sku or '—'}")
        print(f"   Shopify:  {_c(match.shopify.title, Color.BOLD)}")
        print(f"            Barcode: {match.shopify.barcode or '—'} | SKU: {match.shopify.sku or '—'}")

        if confirm_all:
            match.confirmed = True
            print(f"   → {_c('Auto-confirmado', Color.GREEN)}")
            continue

        while True:
            try:
                resp = input(f"   ¿Confirmar? [s/n/t/q]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(f"\n   {_c('Cancelado por el usuario.', Color.RED)}")
                return report

            if resp in ("s", "si", "sí", "y", "yes"):
                match.confirmed = True
                print(f"   → {_c('Confirmado ✓', Color.GREEN)}")
                break
            elif resp in ("n", "no"):
                match.confirmed = False
                # Mover a no emparejados
                report.unmatched_eleventa.append(match.eleventa)
                report.unmatched_shopify.append(match.shopify)
                print(f"   → {_c('Rechazado ✗', Color.RED)}")
                break
            elif resp in ("t", "todos", "all"):
                match.confirmed = True
                confirm_all = True
                print(f"   → {_c('Confirmado ✓ (y todos los siguientes)', Color.GREEN)}")
                break
            elif resp in ("q", "quit", "salir"):
                print(f"   {_c('Saliendo de la confirmación...', Color.YELLOW)}")
                return report
            else:
                print(f"   Opción no válida. Usa: s, n, t, q")

    # Eliminar matches no confirmados
    report.matched = [m for m in report.matched if m.confirmed]

    confirmed_total = sum(1 for m in report.matched if m.confirmed)
    print(f"\n   {_c(f'Resultado final: {confirmed_total} productos emparejados confirmados', Color.GREEN)}")

    return report


# ---------------------------------------------------------------------------
# 5. Guardar mapeos en PostgreSQL
# ---------------------------------------------------------------------------
async def save_mappings_to_db(report: SyncReport) -> None:
    """
    Guarda los emparejamientos confirmados en la tabla product_mappings
    de la base de datos PostgreSQL del middleware.
    """
    confirmed = [m for m in report.matched if m.confirmed]

    if not confirmed:
        print(f"\n{_c('⚠️  No hay mapeos confirmados para guardar.', Color.YELLOW)}")
        return

    print(f"\n{_c('💾 Guardando mapeos en PostgreSQL...', Color.CYAN)}")

    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    except Exception as e:
        print(f"   {_c(f'❌ Error de conexión a PostgreSQL: {e}', Color.RED)}")
        print(f"   {_c('Verifica DATABASE_URL en .env', Color.DIM)}")
        return

    async with pool.acquire() as conn:
        # Crear tabla si no existe
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS product_mappings (
                id                  SERIAL PRIMARY KEY,
                eleventa_id         VARCHAR(100) NOT NULL,
                eleventa_name       VARCHAR(500),
                eleventa_barcode    VARCHAR(100),
                eleventa_sku        VARCHAR(100),
                shopify_product_id  BIGINT NOT NULL,
                shopify_variant_id  BIGINT NOT NULL UNIQUE,
                shopify_title       VARCHAR(500),
                shopify_barcode     VARCHAR(100),
                shopify_sku         VARCHAR(100),
                shopify_inventory_item_id BIGINT,
                match_type          VARCHAR(50) NOT NULL,
                similarity_score    REAL DEFAULT 1.0,
                is_active           BOOLEAN DEFAULT TRUE,
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                updated_at          TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_mappings_eleventa_id
                ON product_mappings (eleventa_id);
            CREATE INDEX IF NOT EXISTS idx_mappings_shopify_variant
                ON product_mappings (shopify_variant_id);
            CREATE INDEX IF NOT EXISTS idx_mappings_barcode
                ON product_mappings (eleventa_barcode)
                WHERE eleventa_barcode IS NOT NULL AND eleventa_barcode != '';
        """)

        # Insertar mapeos
        insert_query = """
            INSERT INTO product_mappings (
                eleventa_id, eleventa_name, eleventa_barcode, eleventa_sku,
                shopify_product_id, shopify_variant_id, shopify_title,
                shopify_barcode, shopify_sku, shopify_inventory_item_id,
                match_type, similarity_score
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (shopify_variant_id)
            DO UPDATE SET
                eleventa_id    = EXCLUDED.eleventa_id,
                eleventa_name  = EXCLUDED.eleventa_name,
                match_type     = EXCLUDED.match_type,
                similarity_score = EXCLUDED.similarity_score,
                updated_at     = NOW()
        """

        saved = 0
        for match in confirmed:
            try:
                await conn.execute(
                    insert_query,
                    match.eleventa.id,
                    match.eleventa.name,
                    match.eleventa.barcode or None,
                    match.eleventa.sku or None,
                    match.shopify.product_id,
                    match.shopify.variant_id,
                    match.shopify.title,
                    match.shopify.barcode or None,
                    match.shopify.sku or None,
                    match.shopify.inventory_item_id,
                    match.match_type,
                    match.similarity_score,
                )
                saved += 1
            except Exception as e:
                print(f"   {_c(f'⚠️  Error al guardar {match.eleventa.name}: {e}', Color.YELLOW)}")

        print(f"   {_c(f'✅ {saved}/{len(confirmed)} mapeos guardados correctamente', Color.GREEN)}")

    await pool.close()


# ---------------------------------------------------------------------------
# 6. Generar reporte
# ---------------------------------------------------------------------------
def generate_report(report: SyncReport) -> str:
    """Genera un reporte detallado en formato JSON y CSV."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    reports_dir = Path(__file__).resolve().parent.parent / "sync_reports"
    reports_dir.mkdir(exist_ok=True)

    # --- Reporte JSON ---
    json_path = reports_dir / f"sync_report_{timestamp}.json"
    json_data: dict[str, Any] = {
        "timestamp": report.timestamp,
        "summary": {
            "total_eleventa": len(report.matched) + len(report.unmatched_eleventa),
            "total_shopify": len(report.matched) + len(report.unmatched_shopify),
            "matched": len(report.matched),
            "unmatched_eleventa": len(report.unmatched_eleventa),
            "unmatched_shopify": len(report.unmatched_shopify),
        },
        "matches": [
            {
                "eleventa_id": m.eleventa.id,
                "eleventa_name": m.eleventa.name,
                "eleventa_barcode": m.eleventa.barcode,
                "eleventa_sku": m.eleventa.sku,
                "eleventa_stock": m.eleventa.stock,
                "shopify_product_id": m.shopify.product_id,
                "shopify_variant_id": m.shopify.variant_id,
                "shopify_title": m.shopify.title,
                "shopify_barcode": m.shopify.barcode,
                "shopify_sku": m.shopify.sku,
                "shopify_quantity": m.shopify.inventory_quantity,
                "match_type": m.match_type,
                "similarity_score": m.similarity_score,
            }
            for m in report.matched
        ],
        "unmatched_eleventa": [
            {"id": p.id, "name": p.name, "barcode": p.barcode, "sku": p.sku}
            for p in report.unmatched_eleventa
        ],
        "unmatched_shopify": [
            {"product_id": p.product_id, "variant_id": p.variant_id, "title": p.title,
             "barcode": p.barcode, "sku": p.sku}
            for p in report.unmatched_shopify
        ],
    }

    json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- Reporte CSV ---
    csv_path = reports_dir / f"sync_report_{timestamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Estado", "Tipo Match", "Similitud",
            "Eleventa ID", "Eleventa Nombre", "Eleventa Barcode", "Eleventa SKU", "Eleventa Stock",
            "Shopify Product ID", "Shopify Variant ID", "Shopify Título", "Shopify Barcode",
            "Shopify SKU", "Shopify Cantidad",
        ])

        for m in report.matched:
            writer.writerow([
                "EMPAREJADO", m.match_type, f"{m.similarity_score:.2%}",
                m.eleventa.id, m.eleventa.name, m.eleventa.barcode, m.eleventa.sku, m.eleventa.stock,
                m.shopify.product_id, m.shopify.variant_id, m.shopify.title, m.shopify.barcode,
                m.shopify.sku, m.shopify.inventory_quantity,
            ])

        for p in report.unmatched_eleventa:
            writer.writerow([
                "SIN EMPAREJAR (Eleventa)", "", "",
                p.id, p.name, p.barcode, p.sku, p.stock,
                "", "", "", "", "", "",
            ])

        for p in report.unmatched_shopify:
            writer.writerow([
                "SIN EMPAREJAR (Shopify)", "", "",
                "", "", "", "", "",
                p.product_id, p.variant_id, p.title, p.barcode, p.sku, p.inventory_quantity,
            ])

    print(f"\n{_c('📊 Reportes generados:', Color.CYAN)}")
    print(f"   JSON: {json_path}")
    print(f"   CSV:  {csv_path}")

    return str(json_path)


# ---------------------------------------------------------------------------
# 7. Punto de entrada principal
# ---------------------------------------------------------------------------
async def main() -> None:
    """Flujo principal de la sincronización inicial."""
    print("=" * 70)
    print(f"{_c('  🔄 SINCRONIZACIÓN INICIAL: Eleventa ↔ Shopify', Color.BOLD)}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)

    # Validar configuración mínima
    missing: list[str] = []
    if not SHOPIFY_STORE:
        missing.append("SHOPIFY_STORE_NAME")
    if not SHOPIFY_TOKEN:
        missing.append("SHOPIFY_ACCESS_TOKEN")
    if not FB_DATABASE:
        missing.append("ELEVENTA_DB_PATH")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")

    if missing:
        print(f"\n{_c('❌ Variables de entorno faltantes:', Color.RED)}")
        for var in missing:
            print(f"   • {var}")
        print(f"\n   Copia .env.example a .env y completa los valores.")
        sys.exit(1)

    # Paso 1: Leer productos de ambas fuentes
    eleventa_products = read_eleventa_products()
    shopify_products = await read_shopify_products()

    if not eleventa_products:
        print(f"\n{_c('❌ No se encontraron productos en Eleventa. Verifica la conexión.', Color.RED)}")
        sys.exit(1)

    if not shopify_products:
        print(f"\n{_c('❌ No se encontraron productos en Shopify. Verifica el token de acceso.', Color.RED)}")
        sys.exit(1)

    # Paso 2: Emparejar productos
    report = match_products(eleventa_products, shopify_products)

    # Paso 3: Confirmación interactiva
    report = confirm_matches_interactive(report)

    # Paso 4: Generar reporte
    generate_report(report)

    # Paso 5: Guardar en PostgreSQL
    confirmed_count = sum(1 for m in report.matched if m.confirmed)
    if confirmed_count > 0:
        print(f"\n{_c('¿Guardar los mapeos confirmados en la base de datos?', Color.BOLD)}")
        try:
            resp = input("   [s/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            resp = "n"

        if resp in ("s", "si", "sí", "y", "yes"):
            await save_mappings_to_db(report)
        else:
            print(f"   {_c('Los mapeos NO se guardaron en la base de datos.', Color.YELLOW)}")
            print(f"   Puedes consultarlos en el reporte generado.")

    # Resumen final
    print(f"\n{'=' * 70}")
    print(f"{_c('  📋 RESUMEN FINAL', Color.BOLD)}")
    print(f"{'=' * 70}")
    print(f"   Productos Eleventa:       {len(eleventa_products)}")
    print(f"   Variantes Shopify:        {len(shopify_products)}")
    print(f"   Emparejados confirmados:  {_c(str(confirmed_count), Color.GREEN)}")
    print(f"   Sin emparejar (Eleventa): {_c(str(len(report.unmatched_eleventa)), Color.YELLOW)}")
    print(f"   Sin emparejar (Shopify):  {_c(str(len(report.unmatched_shopify)), Color.YELLOW)}")
    print(f"{'=' * 70}")
    print(f"\n{_c('✅ Sincronización inicial completada.', Color.GREEN)}")
    print(f"   Ahora puedes iniciar el middleware para sincronización continua.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
