"""库存域：对外提供预占能力，契约见 api/reserve.yaml。"""

from __future__ import annotations

from dataclasses import dataclass, field


class OutOfStock(Exception):
    """可用库存不足以满足本次预占。"""


@dataclass
class InventoryService:
    stock: dict[str, int] = field(default_factory=dict)
    _reservations: dict[str, tuple[str, int]] = field(default_factory=dict)

    def reserve(self, sku: str, quantity: int, order_id: str) -> str:
        if quantity < 1:
            raise ValueError("预占数量必须为正")
        available = self.stock.get(sku, 0)
        if available < quantity:
            raise OutOfStock(f"{sku} 可用 {available}，需要 {quantity}")
        self.stock[sku] = available - quantity
        reservation_id = f"rsv-{order_id}"
        self._reservations[reservation_id] = (sku, quantity)
        return reservation_id

    def reserve_batch(self, skus: list[str], quantities: list[int], order_id: str) -> list[str]:
        """一张订单里的多个 sku 一起预占。"""
        reservations = []
        for index in range(len(skus)):
            reservations.append(
                self.reserve(skus[index], quantities[index], f"{order_id}-{index}")
            )
        return reservations

    def release(self, reservation_id: str) -> None:
        sku, quantity = self._reservations.pop(reservation_id)
        self.stock[sku] = self.stock.get(sku, 0) + quantity
