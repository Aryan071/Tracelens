CREATE SCHEMA orders;
CREATE SCHEMA inventory;
CREATE SCHEMA payments;

CREATE TABLE orders.orders (
  id         BIGSERIAL PRIMARY KEY,
  user_id    INT NOT NULL,
  sku        TEXT NOT NULL,
  qty        INT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Intentionally indexed in the baseline; the "missing index" bad deployment drops it (Phase 4).
CREATE INDEX orders_user_id_idx ON orders.orders (user_id);

CREATE TABLE inventory.stock (sku TEXT PRIMARY KEY, qty INT NOT NULL);
INSERT INTO inventory.stock SELECT 'sku-' || g, 1000000 FROM generate_series(1, 5) g;

CREATE TABLE payments.charges (
  id           BIGSERIAL PRIMARY KEY,
  order_id     BIGINT NOT NULL,
  amount_cents INT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
