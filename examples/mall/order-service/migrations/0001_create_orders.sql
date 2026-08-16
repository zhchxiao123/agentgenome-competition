-- 订单主表。触碰本目录的变更会被影响规则判定为必须跑集成测试，
-- 并被风险评级判定为高风险（迁移不可逆）。
CREATE TABLE orders (
    id            TEXT PRIMARY KEY,
    sku           TEXT NOT NULL,
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    reservation_id TEXT NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
