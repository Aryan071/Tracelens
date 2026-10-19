from __future__ import annotations

from tracelens.ddl import (
    DdlEvent,
    added_events,
    event_table,
    events,
    index_tables,
    same_table,
    tables_of,
)
from tracelens.sqlfp import normalize, split_statements

INIT = """
CREATE SCHEMA orders;
CREATE TABLE orders.orders (id BIGSERIAL PRIMARY KEY, user_id INT NOT NULL);
-- indexed in the baseline
CREATE INDEX orders_user_id_idx ON orders.orders (user_id);
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS payments_order_idx ON payments.charges(order_id);
"""


def test_split_statements_respects_strings_comments_and_dollar_quotes() -> None:
    sql = "select 'a;b'; -- x; y\nselect 2; /* ; */ do $$ begin perform 1; end $$;\nselect 3"
    parts = split_statements(sql)
    assert [normalize(s) for _, s in parts] == [
        "select ?",
        "select ?",
        "do ?",
        "select ?",
    ]
    assert [line for line, _ in parts] == [1, 2, 2, 3]


def test_events_from_a_script() -> None:
    got = events(INIT, "init.sql")
    assert [(e.kind, e.object, e.table, e.line) for e in got] == [
        ("create_table", "orders.orders", "orders.orders", 3),
        ("create_index", "orders_user_id_idx", "orders.orders", 5),
        ("create_index", "payments_order_idx", "payments.charges", 6),
    ]


def test_drop_index_and_alter_table() -> None:
    mig = 'DROP INDEX CONCURRENTLY IF EXISTS orders.orders_user_id_idx;\nALTER TABLE "Orders".orders DROP COLUMN sku;'
    got = events(mig, "m.sql")
    assert got[0] == DdlEvent("drop_index", "orders.orders_user_id_idx", None, "m.sql", 1)
    assert got[1].kind == "alter_table" and got[1].object == "orders.orders"


def test_added_events_only_reports_new_statements() -> None:
    old = INIT
    new = INIT + "\nDROP INDEX orders_user_id_idx;\n"
    assert [e.kind for e in added_events(old, new, "init.sql")] == ["drop_index"]
    # reformatting or reordering an existing script adds nothing
    reformatted = ";\n\n".join(reversed([s for _, s in split_statements(INIT)])) + ";"
    assert added_events(old, reformatted, "init.sql") == []
    # a brand new file: everything in it is new
    assert len(added_events(None, "DROP TABLE old_stuff; CREATE TABLE t (a int);", "n.sql")) == 2
    # non-DDL statements are ignored
    assert added_events(None, "INSERT INTO t VALUES (1); SELECT 1;", "n.sql") == []


def test_index_resolves_to_its_table_from_the_old_tree() -> None:
    idx = index_tables([INIT])
    assert idx["orders_user_id_idx"] == "orders.orders"
    drop = events("DROP INDEX orders_user_id_idx;", "m.sql")[0]
    assert event_table(drop, idx) == "orders.orders"
    assert event_table(events("DROP INDEX unknown_idx;", "m.sql")[0], idx) is None
    # a qualified drop of an index created unqualified (or vice versa) still resolves
    q = events("DROP INDEX orders.orders_user_id_idx;", "m.sql")[0]
    assert event_table(q, idx) == "orders.orders"


def test_tables_of_normalized_queries() -> None:
    q = normalize(
        "SELECT o.id FROM orders.orders o JOIN inventory.stock s ON s.sku = o.sku WHERE o.user_id = $1"
    )
    assert tables_of(q) == {"orders.orders", "inventory.stock"}
    assert tables_of(normalize("UPDATE inventory.stock SET qty = qty - $1 WHERE sku = $2")) == {
        "inventory.stock"
    }
    assert tables_of(normalize("INSERT INTO orders.orders(user_id) VALUES ($1)")) == {
        "orders.orders"
    }
    assert tables_of("select 1") == set()


def test_same_table_matching() -> None:
    assert same_table("orders.orders", "orders.orders")
    assert same_table("orders", "public.orders")
    assert not same_table("orders", "orders_archive")
    assert not same_table("a.orders", "b.orders")
