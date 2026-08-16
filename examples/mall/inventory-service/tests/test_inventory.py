import pytest
from inventory.app import InventoryService, OutOfStock


def test_reserve_deducts_available_stock():
    service = InventoryService(stock={"sku-1": 10})

    reservation_id = service.reserve("sku-1", 3, order_id="ord-1")

    assert reservation_id == "rsv-ord-1"
    assert service.stock["sku-1"] == 7


def test_reserve_rejects_more_than_available():
    service = InventoryService(stock={"sku-1": 2})

    with pytest.raises(OutOfStock):
        service.reserve("sku-1", 5, order_id="ord-1")


def test_release_returns_stock():
    service = InventoryService(stock={"sku-1": 10})
    reservation_id = service.reserve("sku-1", 4, order_id="ord-1")

    service.release(reservation_id)

    assert service.stock["sku-1"] == 10


def test_reserve_batch_reserves_every_sku():
    service = InventoryService(stock={"sku-1": 10, "sku-2": 5})

    reservations = service.reserve_batch(["sku-1", "sku-2"], [3, 2], order_id="ord-1")

    assert reservations == ["rsv-ord-1-0", "rsv-ord-1-1"]
    assert service.stock == {"sku-1": 7, "sku-2": 3}
