"""订单域：下单时通过预占接口向库存域申请占用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ReservePort(Protocol):
    """库存预占接口的消费者视角，契约见 inventory-service/api/reserve.yaml。"""

    def reserve(self, sku: str, quantity: int, order_id: str) -> str: ...


@dataclass
class OrderService:
    inventory: ReservePort
    orders: dict[str, str] = field(default_factory=dict)

    def place(self, order_id: str, sku: str, quantity: int) -> str:
        if order_id in self.orders:
            raise ValueError(f"订单已存在: {order_id}")
        reservation_id = self.inventory.reserve(sku, quantity, order_id)
        self.orders[order_id] = reservation_id
        return reservation_id
