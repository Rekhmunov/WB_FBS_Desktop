"""Unit tests for supply stock ledger (Поставки → Остатки)."""

from __future__ import annotations

from review_processor.repository import ReviewRepository
from review_processor.wb_fbs import (
    TAB_ASSEMBLY,
    TAB_DELIVERY,
    TAB_FINISHED,
    compute_tab,
)


def test_kiz_and_wb_fbs_still_import_with_ledger() -> None:
    from review_processor import wb_fbs, wb_fbs_detail

    assert callable(wb_fbs.sync_wb_fbs_source)
    assert callable(getattr(ReviewRepository, "reconcile_wb_fbs_stock_orders"))
    assert callable(wb_fbs_detail._kiz_status_from_decision)
    assert compute_tab(supplier_status="complete", wb_status="", is_archive=False) == TAB_DELIVERY
    assert compute_tab(supplier_status="confirm", wb_status="", is_archive=False) == TAB_ASSEMBLY
    # sold wins over complete — must still be shippable via reconcile(finished)
    assert compute_tab(supplier_status="complete", wb_status="sold", is_archive=False) == TAB_FINISHED


def test_add_supply_stock_movements_counts_only_inserted() -> None:
    repo = ReviewRepository.__new__(ReviewRepository)
    repo._sql = lambda q: q  # type: ignore[method-assign]
    repo._ensure_supply_balances_tables = lambda conn: None  # type: ignore[method-assign]
    executed: list[tuple[str, tuple]] = []
    rowcounts = [1, 0]  # second conflicts

    class _Cur:
        def __init__(self, rc):
            self.rowcount = rc

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            executed.append((str(sql), tuple(params)))
            rc = rowcounts.pop(0) if rowcounts else 1
            return _Cur(rc)

    repo._connect = lambda: _Conn()  # type: ignore[method-assign]
    saved = ReviewRepository.add_supply_stock_movements(
        repo,
        user_id=1,
        production_id=9,
        movement_date="2026-08-12",
        kind="receipt",
        source_type="manual_receipt",
        items=[
            {
                "item_type": "product",
                "item_id": 5,
                "qty": 10,
                "source_id": "receipt:1",
            },
            {
                "item_type": "product",
                "item_id": 5,
                "qty": 10,
                "source_id": "receipt:1",
            },
        ],
        created_by=3,
    )
    assert saved == 1
    assert any("ON CONFLICT" in sql for sql, _ in executed)


def test_reconcile_ships_reverses_and_recycles() -> None:
    """delivery ships; back to assembly reverses; delivery again ships with new source_id."""
    repo = ReviewRepository.__new__(ReviewRepository)
    repo._sql = lambda q: q  # type: ignore[method-assign]
    repo._ensure_supply_balances_tables = lambda conn: None  # type: ignore[method-assign]
    repo._row_to_dict = lambda r: dict(r)  # type: ignore[method-assign]
    repo.get_product_id_by_article_map = lambda *, user_id: {  # type: ignore[method-assign]
        "ART-1": 44,
        "art-1": 44,
    }

    # Simulated ledger rows for order 111 across reconcile calls.
    ledger: list[dict] = []
    inserts: list[tuple] = []

    class _Cur:
        rowcount = 1

        def __init__(self, rows=None):
            self._rows = rows or []

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            sql_s = str(sql)
            if "supply_stock_fbs_settled" in sql_s and "SELECT" in sql_s:
                return _Cur()  # not settled
            if "SELECT kind, source_type, source_id" in sql_s:
                oid = str(params[1])
                rows = [
                    r
                    for r in ledger
                    if r["oid"] == oid
                ]
                return _Cur(rows)
            if "INSERT INTO supply_stock_movements" in sql_s:
                inserts.append(params)
                # params: user, prod, product_id, qty, date, kind, source_type, source_id, ...
                kind = params[5]
                source_type = params[6]
                source_id = params[7]
                oid = str(source_id).split(":")[0]
                ledger.append(
                    {
                        "oid": oid,
                        "kind": kind,
                        "source_type": source_type,
                        "source_id": source_id,
                    }
                )
                return _Cur()
            return _Cur()

    repo._connect = lambda: _Conn()  # type: ignore[method-assign]

    # 1) Enter delivery → ship
    s1 = ReviewRepository.reconcile_wb_fbs_stock_orders(
        repo,
        user_id=7,
        production_id=3,
        movement_date="2026-08-12",
        orders=[{"order_id": 111, "tab": "delivery", "article": "ART-1", "nm_id": ""}],
    )
    assert s1["shipped"] == 1
    assert any(p[5] == "fbs_ship" and p[3] == -1.0 for p in inserts)

    # 2) Back to assembly → reverse (only because ship exists)
    s2 = ReviewRepository.reconcile_wb_fbs_stock_orders(
        repo,
        user_id=7,
        production_id=3,
        movement_date="2026-08-12",
        orders=[{"order_id": 111, "tab": "assembly", "article": "ART-1", "nm_id": ""}],
    )
    assert s2["reversed"] == 1
    assert any(p[5] == "fbs_reverse" and p[3] == 1.0 for p in inserts)

    # 3) Delivery again → second ship with sequenced source_id
    before = len(inserts)
    s3 = ReviewRepository.reconcile_wb_fbs_stock_orders(
        repo,
        user_id=7,
        production_id=3,
        movement_date="2026-08-13",
        orders=[{"order_id": 111, "tab": "delivery", "article": "ART-1", "nm_id": ""}],
    )
    assert s3["shipped"] == 1
    new_inserts = inserts[before:]
    assert any(p[7] == "111:s:2" for p in new_inserts)

    # 4) finished without prior ship (unknown article skipped earlier path) — ship
    s4 = ReviewRepository.reconcile_wb_fbs_stock_orders(
        repo,
        user_id=7,
        production_id=3,
        movement_date="2026-08-13",
        orders=[{"order_id": 222, "tab": "finished", "article": "ART-1", "nm_id": ""}],
    )
    assert s4["shipped"] == 1

    # 5) assembly with no ship → no reverse
    s5 = ReviewRepository.reconcile_wb_fbs_stock_orders(
        repo,
        user_id=7,
        production_id=3,
        movement_date="2026-08-13",
        orders=[{"order_id": 333, "tab": "assembly", "article": "ART-1", "nm_id": ""}],
    )
    assert s5["reversed"] == 0
    assert s5["ok"] == 1


def test_legacy_migrate_uses_deterministic_source_id() -> None:
    repo = ReviewRepository.__new__(ReviewRepository)
    repo.list_supply_balance_dates = lambda **kw: ["2026-08-10"]  # type: ignore[method-assign]
    repo.list_supply_balances = lambda **kw: [  # type: ignore[method-assign]
        {"item_type": "product", "item_id": 5, "quantity": 3},
        {"item_type": "material", "item_id": 2, "quantity": 0},
    ]
    captured: list[dict] = []

    def _add(**kwargs):
        captured.append(kwargs)
        return len(kwargs["items"])

    repo.add_supply_stock_movements = _add  # type: ignore[method-assign]
    n = ReviewRepository.migrate_legacy_supply_balances_to_movements(
        repo, user_id=1, production_id=2, created_by=9
    )
    assert n == 1
    items = captured[0]["items"]
    assert items[0]["source_id"] == "legacy:2026-08-10:product:5"
    assert all("uuid" not in str(i["source_id"]) for i in items)


def test_sum_supply_stock_balances_sql_shape() -> None:
    repo = ReviewRepository.__new__(ReviewRepository)
    repo._sql = lambda q: q  # type: ignore[method-assign]
    repo._ensure_supply_balances_tables = lambda conn: None  # type: ignore[method-assign]
    repo._row_to_dict = lambda r: dict(r)  # type: ignore[method-assign]

    class _Cur:
        def fetchall(self):
            return [
                {"item_type": "product", "item_id": 5, "balance": 12.0},
                {"item_type": "material", "item_id": 2, "balance": 3.5},
            ]

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            assert "SUM(qty)" in str(sql)
            assert params[-1] == "2026-08-12"
            return _Cur()

    repo._connect = lambda: _Conn()  # type: ignore[method-assign]
    bal = ReviewRepository.sum_supply_stock_balances(
        repo, user_id=1, production_id=2, as_of="2026-08-12"
    )
    assert bal[("product", 5)] == 12.0
    assert bal[("material", 2)] == 3.5


def test_apply_transitions_wrapper_delegates_to_reconcile() -> None:
    repo = ReviewRepository.__new__(ReviewRepository)
    seen: dict = {}

    def _rec(**kwargs):
        seen.update(kwargs)
        return {"shipped": 1, "reversed": 0, "skipped": 0, "ok": 0}

    repo.reconcile_wb_fbs_stock_orders = _rec  # type: ignore[method-assign]
    out = ReviewRepository.apply_wb_fbs_stock_tab_transitions(
        repo,
        user_id=1,
        production_id=2,
        movement_date="2026-08-12",
        transitions=[
            {
                "order_id": 9,
                "old_tab": "assembly",
                "new_tab": "delivery",
                "article": "A",
                "nm_id": "1",
            }
        ],
    )
    assert out["shipped"] == 1
    assert seen["orders"][0]["tab"] == "delivery"
    assert seen["orders"][0]["order_id"] == 9


def test_reconcile_skips_settled_fbs_orders() -> None:
    """After adjustment, open delivery orders are settled and must not ship."""
    repo = ReviewRepository.__new__(ReviewRepository)
    repo._sql = lambda q: q  # type: ignore[method-assign]
    repo._ensure_supply_balances_tables = lambda conn: None  # type: ignore[method-assign]
    repo._row_to_dict = lambda r: dict(r)  # type: ignore[method-assign]
    repo.get_product_id_by_article_map = lambda *, user_id: {"ART": 7}  # type: ignore[method-assign]
    inserts: list[tuple] = []

    class _Cur:
        rowcount = 1

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            if "INSERT INTO supply_stock_movements" in str(sql):
                inserts.append(params)
            return _Cur()

    repo._connect = lambda: _Conn()  # type: ignore[method-assign]
    repo.is_wb_fbs_order_stock_settled = (  # type: ignore[method-assign]
        lambda conn, *, user_id, order_id: int(order_id) == 111
    )
    repo._fbs_stock_ship_counts = lambda conn, *, user_id, order_id: (0, 0)  # type: ignore[method-assign]

    stats = ReviewRepository.reconcile_wb_fbs_stock_orders(
        repo,
        user_id=1,
        production_id=2,
        movement_date="2026-08-12",
        orders=[
            {"order_id": 111, "tab": "delivery", "article": "ART", "nm_id": ""},
            {"order_id": 222, "tab": "delivery", "article": "ART", "nm_id": ""},
        ],
    )
    assert stats["settled"] == 1
    assert stats["shipped"] == 1
    assert len(inserts) == 1
    assert str(inserts[0][7]).startswith("222:")


def test_settle_open_wb_fbs_orders_inserts_unique() -> None:
    repo = ReviewRepository.__new__(ReviewRepository)
    repo._sql = lambda q: q  # type: ignore[method-assign]
    repo._ensure_supply_balances_tables = lambda conn: None  # type: ignore[method-assign]
    repo._row_to_dict = lambda r: dict(r)  # type: ignore[method-assign]
    executed: list[str] = []

    class _Cur:
        rowcount = 1

        def fetchall(self):
            return [{"order_id": 5}, {"order_id": 6}]

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            executed.append(str(sql))
            return _Cur()

    repo._connect = lambda: _Conn()  # type: ignore[method-assign]
    n = ReviewRepository.settle_open_wb_fbs_orders_for_stock(
        repo, user_id=1, production_id=3, reason="opening"
    )
    assert n == 2
    assert any("supply_stock_fbs_settled" in s and "INSERT" in s for s in executed)


def test_parse_supply_balance_min_qty() -> None:
    parse = ReviewRepository._parse_supply_balance_min_qty
    assert parse(None) is None
    assert parse("") is None
    assert parse("  ") is None
    assert parse("abc") is None
    assert parse(-1) is None
    assert parse(0) == 0.0
    assert parse("12.5") == 12.5
    assert parse(3) == 3.0
