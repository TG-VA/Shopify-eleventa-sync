"""
Tests de autenticación de la API del agente (`/api/agent/*`).

Verifican que la API key del agente se valida con comparación en tiempo
constante (`secrets.compare_digest`), que los 401 no exponen detalles internos
y que un header malformado no provoca un 500.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from middleware.config import settings
from middleware.routers import agent as agent_router_module
from middleware.routers.agent import (
    UNAUTHORIZED_DETAIL,
    configure_agent_router,
    verify_agent_key,
)
from middleware.routers.agent import (
    router as agent_router,
)
from middleware.services.product_mapper import ProductMapper
from shared.models import InventoryChange, PendingAdjustment

VALID_KEY = settings.AGENT_API_KEY
WRONG_KEY = "test-agent-key-99999" if VALID_KEY != "test-agent-key-99999" else "otra-clave"

AUTH_HEADERS = {"X-Agent-Api-Key": VALID_KEY}

INVENTORY_REQUEST: dict[str, Any] = {
    "agent_id": "eleventa-agent",
    "changes": [
        {
            "codigo": "PROD001",
            "existencia_anterior": 10.0,
            "existencia_nueva": 8.0,
            "delta": -2.0,
            "source": "eleventa",
            "timestamp": "2026-10-04T17:26:19",
        }
    ],
}

CONFIRMATION_REQUEST: dict[str, Any] = {
    "adjustment_id": "adj-1",
    "success": True,
    "message": "Aplicado",
}

# Endpoints protegidos: (método, ruta, json body o None)
PROTECTED_ENDPOINTS = [
    ("POST", "/api/agent/inventory-changes", INVENTORY_REQUEST),
    ("GET", "/api/agent/pending-adjustments", None),
    ("POST", "/api/agent/confirm-adjustment", CONFIRMATION_REQUEST),
    ("GET", "/api/agent/product-mappings", None),
]


class _FakeSyncEngine:
    """Doble de `SyncEngine` que no toca Shopify ni PostgreSQL."""

    def __init__(self) -> None:
        self.changes_received: list[InventoryChange] = []
        self.confirmations: list[dict[str, Any]] = []

    async def process_eleventa_changes(
        self, changes: list[InventoryChange]
    ) -> dict[str, Any]:
        self.changes_received.extend(changes)
        return {"processed": len(changes), "errors": []}

    async def get_pending_adjustments(self) -> list[PendingAdjustment]:
        return [
            PendingAdjustment(
                id="adj-1",
                codigo_eleventa="PROD001",
                cantidad_ajuste=-2.0,
                motivo="Venta Shopify",
            )
        ]

    async def confirm_adjustment(
        self, adjustment_id: str, success: bool, message: str = ""
    ) -> bool:
        self.confirmations.append(
            {"adjustment_id": adjustment_id, "success": success, "message": message}
        )
        return True


@pytest.fixture
def agent_client():
    """App FastAPI mínima con el router del agente ya configurado."""
    app = FastAPI()
    app.include_router(agent_router)
    engine = _FakeSyncEngine()
    configure_agent_router(engine, ProductMapper())
    with TestClient(app) as client:
        yield client, engine


def _request(client: TestClient, method: str, path: str, payload, headers):
    kwargs: dict[str, Any] = {"headers": headers}
    if payload is not None:
        kwargs["json"] = payload
    return client.request(method, path, **kwargs)


# ── Reacción de la API key ───────────────────────────────────────────────


@pytest.mark.parametrize(("method", "path", "payload"), PROTECTED_ENDPOINTS)
def test_agent_endpoints_reject_missing_api_key_header(agent_client, method, path, payload):
    client, _engine = agent_client

    response = _request(client, method, path, payload, {})

    # Sin header, FastAPI responde 422 por validación de la dependencia; con
    # header vacío o ausente la petición nunca alcanza el handler.
    assert response.status_code in (401, 422)


@pytest.mark.parametrize(("method", "path", "payload"), PROTECTED_ENDPOINTS)
def test_agent_endpoints_reject_invalid_api_key(agent_client, method, path, payload):
    client, _engine = agent_client

    response = _request(
        client, method, path, payload, {"X-Agent-Api-Key": WRONG_KEY}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == UNAUTHORIZED_DETAIL


@pytest.mark.parametrize(("method", "path", "payload"), PROTECTED_ENDPOINTS)
def test_agent_endpoints_accept_valid_api_key(agent_client, method, path, payload):
    client, _engine = agent_client

    response = _request(client, method, path, payload, AUTH_HEADERS)

    assert response.status_code == 200


def test_inventory_changes_payload_is_processed(agent_client):
    client, engine = agent_client

    response = client.post(
        "/api/agent/inventory-changes", json=INVENTORY_REQUEST, headers=AUTH_HEADERS
    )

    assert response.status_code == 200
    assert response.json() == {"processed": 1, "errors": []}
    assert [c.codigo for c in engine.changes_received] == ["PROD001"]


def test_pending_adjustments_payload_is_returned(agent_client):
    client, _engine = agent_client

    response = client.get("/api/agent/pending-adjustments", headers=AUTH_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["adjustments"][0]["codigo_eleventa"] == "PROD001"


def test_product_mappings_are_returned(agent_client):
    client, _engine = agent_client

    response = client.get("/api/agent/product-mappings", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"count": 0, "mappings": {}}


# ── Endurecimiento criptográfico ──────────────────────────────────────────


def test_verify_agent_key_uses_constant_time_comparison(monkeypatch):
    calls: list[tuple[bytes, bytes]] = []
    original = agent_router_module.secrets.compare_digest

    def _spy(provided, expected):  # noqa: ANN001 - firma de compare_digest
        calls.append((provided, expected))
        return original(provided, expected)

    monkeypatch.setattr(agent_router_module.secrets, "compare_digest", _spy)

    assert asyncio.run(verify_agent_key(VALID_KEY)) == VALID_KEY
    assert len(calls) == 1
    provided, expected = calls[0]
    assert provided == VALID_KEY.encode("utf-8")
    assert expected == settings.AGENT_API_KEY.encode("utf-8")


def test_verify_agent_key_returns_key_for_valid_value():
    assert asyncio.run(verify_agent_key(VALID_KEY)) == VALID_KEY


@pytest.mark.parametrize("candidate", [WRONG_KEY, "", VALID_KEY + "x", VALID_KEY[:-1]])
def test_verify_agent_key_rejects_any_other_value(candidate):
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(verify_agent_key(candidate))

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == UNAUTHORIZED_DETAIL


def test_verify_agent_key_with_non_ascii_value_raises_401_not_error():
    """Un header con bytes no ASCII no debe producir un TypeError (500)."""
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(verify_agent_key("cláve-\U0001f512"))

    assert excinfo.value.status_code == 401


def test_401_body_does_not_leak_internal_details(agent_client):
    client, _engine = agent_client

    response = _request(
        client,
        "POST",
        "/api/agent/inventory-changes",
        INVENTORY_REQUEST,
        {"X-Agent-Api-Key": WRONG_KEY},
    )

    assert response.status_code == 401
    body = response.text
    assert WRONG_KEY not in body
    assert VALID_KEY not in body
    assert "AGENT_API_KEY" not in body
    assert "agent/config" not in body
    assert response.json() == {"detail": UNAUTHORIZED_DETAIL}
