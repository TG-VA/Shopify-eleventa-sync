"""
Configuración compartida de la suite de tests.

Las variables de entorno se fijan aquí (antes de que cualquier test importe
`middleware.config`) para que la suite sea hermética: no depende de un `.env`
real ni de credenciales de Shopify/PostgreSQL.
"""

from __future__ import annotations

import os

TEST_SHOPIFY_STORE_URL = "https://tienda-de-pruebas.myshopify.com"
TEST_SHOPIFY_ACCESS_TOKEN = "shpat_test_access_token"
TEST_SHOPIFY_WEBHOOK_SECRET = "whsec_test_webhook_secret"
TEST_DATABASE_URL = "postgresql://test:test@localhost:5432/test_sync"
TEST_AGENT_API_KEY = "test-agent-key-12345"

os.environ.setdefault("SHOPIFY_STORE_URL", TEST_SHOPIFY_STORE_URL)
os.environ.setdefault("SHOPIFY_ACCESS_TOKEN", TEST_SHOPIFY_ACCESS_TOKEN)
os.environ.setdefault("SHOPIFY_WEBHOOK_SECRET", TEST_SHOPIFY_WEBHOOK_SECRET)
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
# El agente usa la misma clave; se fija para que los tests de /api/agent/*
# no dependan del .env local.
os.environ.setdefault("AGENT_API_KEY", TEST_AGENT_API_KEY)
