from datetime import datetime

from shared import models


def test_product_inventory_model():
    product = models.ProductInventory(
        codigo="PROD001",
        descripcion="Test Product",
        existencia=10.5,
        precio=25.99,
    )
    assert product.codigo == "PROD001"
    assert product.descripcion == "Test Product"
    assert product.existencia == 10.5
    assert product.precio == 25.99


def test_inventory_change_model():
    change = models.InventoryChange(
        codigo="PROD001",
        existencia_anterior=10.0,
        existencia_nueva=15.0,
        delta=5.0,
        source="eleventa",
    )
    assert change.codigo == "PROD001"
    assert change.existencia_anterior == 10.0
    assert change.existencia_nueva == 15.0
    assert change.delta == 5.0
    assert change.source == "eleventa"
    assert isinstance(change.timestamp, datetime)


def test_sync_request_model():
    change = models.InventoryChange(
        codigo="PROD001",
        existencia_anterior=10.0,
        existencia_nueva=15.0,
        delta=5.0,
        source="eleventa",
    )
    request = models.SyncRequest(changes=[change], agent_id="agent-001")
    assert len(request.changes) == 1
    assert request.agent_id == "agent-001"


def test_sync_response_model():
    response = models.SyncResponse(processed=5, errors=["error1"])
    assert response.processed == 5
    assert response.errors == ["error1"]

    response_no_errors = models.SyncResponse(processed=3)
    assert response_no_errors.processed == 3
    assert response_no_errors.errors == []


def test_pending_adjustment_model():
    adjustment = models.PendingAdjustment(
        id="adj-001",
        codigo_eleventa="PROD001",
        cantidad_ajuste=-5.0,
        motivo="Venta Shopify",
        shopify_order_id="ord-123",
    )
    assert adjustment.id == "adj-001"
    assert adjustment.codigo_eleventa == "PROD001"
    assert adjustment.cantidad_ajuste == -5.0
    assert adjustment.motivo == "Venta Shopify"
    assert adjustment.shopify_order_id == "ord-123"
    assert adjustment.status == "pending"
    assert isinstance(adjustment.created_at, datetime)


def test_pending_adjustment_without_shopify_order():
    adjustment = models.PendingAdjustment(
        id="adj-001",
        codigo_eleventa="PROD001",
        cantidad_ajuste=10.0,
        motivo="Ajuste manual",
    )
    assert adjustment.shopify_order_id is None
    assert adjustment.status == "pending"


def test_adjustment_confirmation_model():
    confirmation = models.AdjustmentConfirmation(
        adjustment_id="adj-001",
        success=True,
        message="Ajustado correctamente",
    )
    assert confirmation.adjustment_id == "adj-001"
    assert confirmation.success is True
    assert confirmation.message == "Ajustado correctamente"

    confirmation_fail = models.AdjustmentConfirmation(
        adjustment_id="adj-002", success=False, message="Error al aplicar"
    )
    assert confirmation_fail.success is False
