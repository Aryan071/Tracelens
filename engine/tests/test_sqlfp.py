from __future__ import annotations

import pytest

from tracelens.sqlfp import fingerprint, normalize, statement_kind

CASES = [
    # literals
    ("SELECT * FROM t WHERE id = 42", "select * from t where id = ?"),
    ("select * from t where name = 'bob'", "select * from t where name = ?"),
    ("select * from t where name = 'o''brien'", "select * from t where name = ?"),
    ("select * from t where n = E'a\\'b'", "select * from t where n = ?"),
    (
        "select * from t where p = 3.14 and q = 1e-3 and r = .5",
        "select * from t where p = ? and q = ? and r = ?",
    ),
    ("select * from t where h = 0xFF", "select * from t where h = ?"),
    ("select * from t where x = -5", "select * from t where x = ?"),
    ("select * from t where x = a - 5", "select * from t where x = a - ?"),
    ("select * from t limit -1", "select * from t limit ?"),
    ("select $$a'b$$, $q$x$q$", "select ?, ?"),
    # parameters of every style
    ("select * from t where a = $1 and b = $2", "select * from t where a = ? and b = ?"),
    ("select * from t where a = ? and b = ?", "select * from t where a = ? and b = ?"),
    ("select * from t where a = %s", "select * from t where a = ?"),
    ("select * from t where a = %(name)s", "select * from t where a = ?"),
    ("select * from t where a = :name", "select * from t where a = ?"),
    ("select * from t where a = @p1", "select * from t where a = ?"),
    # casts are kept, not mistaken for a :name parameter
    ("select a::int, $1::text from t", "select a::int, ?::text from t"),
    # whitespace, case, comments, semicolon
    ("  SELECT   id\n\tFROM\n t ;", "select id from t"),
    ("select id -- trailing\nfrom t /* hint */ where x=1", "select id from t where x = ?"),
    ("SELECT a,b FROM s.t WHERE a=$1", "select a, b from s.t where a = ?"),
    # identifiers: quoted keep their case, digits inside names are not literals
    ('select "Id", t1.c2 from "My Table" t1', 'select "Id", t1.c2 from "My Table" t1'),
    # lists collapse
    ("select * from t where id in (1, 2, 3)", "select * from t where id in (?)"),
    ("select * from t where id in ($1)", "select * from t where id in (?)"),
    ("select * from t where id not in (1,2)", "select * from t where id not in (?)"),
    (
        "select * from t where id = any(array[1, 2, 3])",
        "select * from t where id = any (array [?])",
    ),
    ("insert into t(a, b) values (1, 2), (3, 4), (5, 6)", "insert into t (a, b) values (?, ?)"),
    (
        "insert into t(a, b) values ($1, $2) returning id",
        "insert into t (a, b) values (?, ?) returning id",
    ),
    # an IN with a subquery is not a literal list
    (
        "select * from t where id in (select id from u)",
        "select * from t where id in (select id from u)",
    ),
    ("", ""),
]


@pytest.mark.parametrize(("sql", "expected"), CASES)
def test_normalize(sql: str, expected: str) -> None:
    assert normalize(sql) == expected


def test_normalize_is_idempotent() -> None:
    for sql, _ in CASES:
        once = normalize(sql)
        assert normalize(once) == once


def test_same_shape_same_fingerprint() -> None:
    a = "SELECT id, sku FROM orders.orders WHERE id = 7"
    b = "select id,sku from orders.orders where id=$1 -- n+1"
    assert fingerprint(a) == fingerprint(b)


def test_different_shape_different_fingerprint() -> None:
    assert fingerprint("select a from t where x = 1") != fingerprint("select a from t where y = 1")
    assert fingerprint("select a from t") != fingerprint("select b from t")
    # a different number of columns is a different statement, so VALUES tuples are not folded away
    assert fingerprint("insert into t(a) values ($1)") != fingerprint(
        "insert into t(a, b) values ($1, $2)"
    )


def test_fingerprint_format() -> None:
    fp = fingerprint("select 1")
    assert len(fp) == 16
    assert int(fp, 16) >= 0


def test_unterminated_string_does_not_crash() -> None:
    once = normalize("select 'abc")
    assert once.startswith("select")
    assert normalize(once) == once


def test_statement_kind() -> None:
    assert statement_kind("  UPDATE t SET a = 1") == "update"
    assert statement_kind("") == ""
