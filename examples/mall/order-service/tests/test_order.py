import pytest
from order.app import OrderService


class StubInventory:
    def __init__(self):
        self.calls = []

    def reserve(self, sku, quantity, order_id):
        self.calls.append((sku, quantity, order_id))
        return f"rsv-{order_id}"


def test_place_reserves_inventory_and_records_the_order():
    service = OrderService(inventory=StubInventory())

    reservation_id = service.place("ord-1", "sku-1", 2)

    assert reservation_id == "rsv-ord-1"
    assert service.orders["ord-1"] == "rsv-ord-1"


def test_place_rejects_duplicate_order():
    service = OrderService(inventory=StubInventory())
    service.place("ord-1", "sku-1", 2)

    with pytest.raises(ValueError):
        service.place("ord-1", "sku-1", 2)
