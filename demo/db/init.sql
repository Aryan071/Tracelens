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
-- Historical orders, so that a query on user_id has something to scan: with 300k rows an indexed
-- lookup takes under a millisecond and a sequential scan tens of milliseconds.
INSERT INTO orders.orders (user_id, sku, qty, created_at)
SELECT (g % 1000) + 1, 'sku-' || (g % 5 + 1), 1 + (g % 3), now() - (g || ' seconds')::interval
FROM generate_series(1, 300000) g;
-- Intentionally indexed in the baseline; the "missing index" bad deployment drops it (Phase 5).
CREATE INDEX orders_user_id_idx ON orders.orders (user_id);
ANALYZE orders.orders;

CREATE TABLE inventory.stock (sku TEXT PRIMARY KEY, qty INT NOT NULL);
INSERT INTO inventory.stock SELECT 'sku-' || g, 1000000 FROM generate_series(1, 5) g;

CREATE TABLE payments.charges (
  id           BIGSERIAL PRIMARY KEY,
  order_id     BIGINT NOT NULL,
  amount_cents INT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
