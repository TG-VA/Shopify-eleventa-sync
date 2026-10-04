"""
Tests de verificación HMAC de webhooks de Shopify.

Cubren dos capas:

1. `verify_shopify_webhook` como unidad (firma válida, payload alterado,
   firma inválida, entradas vacías o malformadas).
2. Los endpoints `/webhooks/*` vía TestClient: el body crudo debe llegar
   intacto hasta la verificación y cualquier manipulación debe terminar en 401.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.routers.webhooks import (
    configure_webhooks,
    verify_shopify_webhook,
)
from middleware.routers.webhooks import (
    router as webhooks_router,
)

WEBHOOK_SECRET = "whsec_test_webhook_secret"
OTHER_SECRET = "whsec_secreto_del_atacante"

ORDER_PAYLOAD: dict[str, Any] = {
    "id": 820982911946154500,
    "name": "#1001",
    "line_items": [
        {"id": 1, "sku": "PROD001", "quantity": 2, "price": "25.00"},
        {"id": 2, "sku": "PROD002", "quantity": 1, "price": "10.50"},
    ],
}

INVENTORY_PAYLOAD: dict[str, Any] = {
    "inventory_item_id": 808950810,
    "location_id": 655441591,
    "available": 42,
}


class _FakeAdjustment:
    """Ajuste pendiente mínimo con el atributo `id` usado por el router."""

    def __init__(self, adjustment_id: str) -> None:
        self.id = adjustment_id


class _FakeSyncEngine:
    """Doble de `SyncEngine` que no toca Shopify ni PostgreSQL."""

    def __init__(self) -> None:
        self.received_orders: list[dict[str, Any]] = []

    async def process_shopify_order(
        self, order_data: dict[str, Any]
    ) -> list[_FakeAdjustment]:
        self.received_orders.append(order_data)
        return [_FakeAdjustment("adj-1"), _FakeAdjustment("adj-2")]


def _sign(payload: dict[str, Any] | bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Genera el header X-Shopify-Hmac-Sha256 sobre los bytes exactos."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


@pytest.fixture
def webhook_env():
    """App FastAPI mínima con solo el router de webhooks configurado."""
    app = FastAPI()
    app.include_router(webhooks_router)
    engine = _FakeSyncEngine()
    configure_webhooks(engine, WEBHOOK_SECRET)
    with TestClient(app) as client:
        yield client, engine


# ── Unidad: verify_shopify_webhook ───────────────────────────────────────


def test_verify_webhook_accepts_valid_signature():
    body = json.dumps(ORDER_PAYLOAD).encode("utf-8")
    assert verify_shopify_webhook(body, _sign(body), WEBHOOK_SECRET) is True


def test_verify_webhook_rejects_tampered_payload():
    original = json.dumps(ORDER_PAYLOAD).encode("utf-8")
    tampered = original.replace(b'"quantity": 2', b'"quantity": 9')
    assert tampered != original

    assert verify_shopify_webhook(tampered, _sign(original), WEBHOOK_SECRET) is False


def test_verify_webhook_rejects_signature_from_wrong_secret():
    body = json.dumps(ORDER_PAYLOAD).encode("utf-8")
    forged = _sign(body, OTHER_SECRET)
    assert verify_shopify_webhook(body, forged, WEBHOOK_SECRET) is False


def test_verify_webhook_rejects_random_signature():
    body = json.dumps(ORDER_PAYLOAD).encode("utf-8")
    assert verify_shopify_webhook(body, "ZGVjb2RlZC1zaWduYXR1cmU=", WEBHOOK_SECRET) is False


def test_verify_webhook_requires_raw_bytes_not_reserialized_json():
    """El HMAC se calcula sobre bytes: reserializar rompe la firma."""
    body = json.dumps(ORDER_PAYLOAD, separators=(",", ":")).encode("utf-8")
    assert verify_shopify_webhook(body, _sign(body), WEBHOOK_SECRET) is True

    reserialized = json.dumps(ORDER_PAYLOAD).encode("utf-8")
    assert reserialized != body
    assert verify_shopify_webhook(reserialized, _sign(body), WEBHOOK_SECRET) is False


def test_verify_webhook_rejects_empty_inputs():
    body = json.dumps(ORDER_PAYLOAD).encode("utf-8")
    assert verify_shopify_webhook(b"", _sign(body), WEBHOOK_SECRET) is False
    assert verify_shopify_webhook(body, "", WEBHOOK_SECRET) is False
    assert verify_shopify_webhook(body, _sign(body), "") is False


def test_verify_webhook_rejects_non_ascii_header_without_raising():
    body = json.dumps(ORDER_PAYLOAD).encode("utf-8")
    assert verify_shopify_webhook(body, "firma-éñ-\U0001f512", WEBHOOK_SECRET) is False


# ── Integración: endpoints /webhooks/* ───────────────────────────────────


def test_orders_create_accepts_valid_signature(webhook_env):
    client, engine = webhook_env
    body = json.dumps(ORDER_PAYLOAD).encode("utf-8")

    response = client.post(
        "/webhooks/orders-create",
        content=body,
        headers={
            "X-Shopify-Hmac-Sha256": _sign(body),
            "X-Shopify-Topic": "orders/create",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["order"] == "#1001"
    assert payload["adjustments_created"] == 2
    assert payload["adjustment_ids"] == ["adj-1", "adj-2"]
    # El motor recibió el payload íntegro: el body crudo no fue alterado.
    assert engine.received_orders == [ORDER_PAYLOAD]


def test_orders_create_rejects_tampered_payload_with_401(webhook_env):
    client, engine = webhook_env
    original = json.dumps(ORDER_PAYLOAD).encode("utf-8")
    tampered = original.replace(b'"PROD001"', b'"PROD999"')

    response = client.post(
        "/webhooks/orders-create",
        content=tampered,
        headers={
            "X-Shopify-Hmac-Sha256": _sign(original),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "HMAC inválido."
    # Nada llegó al motor: el rechazo ocurre antes de procesar la orden.
    assert engine.received_orders == []


def test_orders_create_rejects_invalid_hmac_with_401(webhook_env):
    client, engine = webhook_env
    body = json.dumps(ORDER_PAYLOAD).encode("utf-8")

    response = client.post(
        "/webhooks/orders-create",
        content=body,
        headers={
            "X-Shopify-Hmac-Sha256": _sign(body, OTHER_SECRET),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert engine.received_orders == []


def test_orders_create_without_hmac_header_is_rejected(webhook_env):
    client, engine = webhook_env
    body = json.dumps(ORDER_PAYLOAD).encode("utf-8")

    response = client.post(
        "/webhooks/orders-create",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    # FastAPI marca el header obligatorio como inválido (422) y nunca ejecuta
    # el handler; en cualquier caso la orden no debe procesarse.
    assert response.status_code in (401, 422)
    assert engine.received_orders == []


def test_inventory_update_accepts_valid_signature(webhook_env):
    client, _engine = webhook_env
    body = json.dumps(INVENTORY_PAYLOAD).encode("utf-8")

    response = client.post(
        "/webhooks/inventory-update",
        content=body,
        headers={
            "X-Shopify-Hmac-Sha256": _sign(body),
            "X-Shopify-Topic": "inventory_levels/update",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["inventory_item_id"] == 808950810
    assert payload["available"] == 42


def test_inventory_update_rejects_invalid_hmac_with_401(webhook_env):
    client, _engine = webhook_env
    body = json.dumps(INVENTORY_PAYLOAD).encode("utf-8")

    response = client.post(
        "/webhooks/inventory-update",
        content=body,
        headers={
            "X-Shopify-Hmac-Sha256": _sign(body, OTHER_SECRET),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "HMAC inválido."


def test_inventory_update_rejects_tampered_payload_with_401(webhook_env):
    client, _engine = webhook_env
    original = json.dumps(INVENTORY_PAYLOAD).encode("utf-8")
    tampered = original.replace(b'"available": 42', b'"available": 0')

    response = client.post(
        "/webhooks/inventory-update",
        content=tampered,
        headers={
            "X-Shopify-Hmac-Sha256": _sign(original),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401


def test_webhook_401_does_not_leak_internal_details(webhook_env):
    client, _engine = webhook_env
    body = json.dumps(ORDER_PAYLOAD).encode("utf-8")

    response = client.post(
        "/webhooks/orders-create",
        content=body,
        headers={
            "X-Shopify-Hmac-Sha256": _sign(body, OTHER_SECRET),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    raw_body = response.text
    assert WEBHOOK_SECRET not in raw_body
    assert OTHER_SECRET not in raw_body
