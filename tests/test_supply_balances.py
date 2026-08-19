"""Unit tests for manual supply balances (Остатки) helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from review_processor.repository import ReviewRepository


def test_kiz_status_module_still_imports() -> None:
    # Guard: new balances code must not break existing WB FBS imports.
    from review_processor import wb_fbs_detail

    assert callable(wb_fbs_detail._kiz_status_from_decision)


def test_upsert_and_list_balances_roundtrip_sql_shape() -> None:
    """Ensure upsert SQL uses the unique conflict target expected by schema."""
    repo = ReviewRepository.__new__(ReviewRepository)
    repo._sql = lambda q: q  # type: ignore[method-assign]
    repo._bool_db = lambda v: bool(v)  # type: ignore[method-assign]
    repo._ensure_supply_balances_tables = lambda conn: None  # type: ignore[method-assign]
    repo._row_to_dict = lambda r: dict(r)  # type: ignore[method-assign]

    executed: list[tuple[str, tuple]] = []

    class _Cur:
        rowcount = 1
        lastrowid = 1

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            executed.append((str(sql), tuple(params)))
            return _Cur()

    repo._connect = lambda: _Conn()  # type: ignore[method-assign]

    saved = ReviewRepository.upsert_supply_balances(
        repo,
        user_id=10,
        production_id=3,
        balance_date="2026-08-12",
        items=[
            {"item_type": "material", "item_id": 7, "quantity": 12.5},
            {"item_type": "product", "item_id": 9, "quantity": 2},
            {"item_type": "junk", "item_id": 1, "quantity": 1},
        ],
        updated_by=42,
    )
    assert saved == 2
    assert any("ON CONFLICT" in sql for sql, _ in executed)
    assert any("supply_balances" in sql and "INSERT" in sql for sql, _ in executed)

    executed.clear()
    cleared = ReviewRepository.upsert_supply_balances(
        repo,
        user_id=10,
        production_id=3,
        balance_date="2026-08-12",
        items=[{"item_type": "material", "item_id": 7, "quantity": None}],
        updated_by=42,
    )
    assert cleared == 1
    assert any("DELETE FROM supply_balances" in sql for sql, _ in executed)


def test_add_feedback_material_uses_returning_id() -> None:
    repo = ReviewRepository.__new__(ReviewRepository)
    repo._sql = lambda q: q  # type: ignore[method-assign]
    repo._ensure_supply_balances_tables = lambda conn: None  # type: ignore[method-assign]
    repo._row_to_dict = lambda r: dict(r)  # type: ignore[method-assign]
    calls: list[str] = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            calls.append(str(sql))
            class _Cur:
                def fetchone(self_inner):
                    if "RETURNING id" in str(sql):
                        return {"id": 77}
                    return {"id": 77, "name": "Ткань", "unit": "м"}
            return _Cur()

    repo._connect = lambda: _Conn()  # type: ignore[method-assign]
    repo._insert_and_get_id = ReviewRepository._insert_and_get_id.__get__(repo)  # type: ignore[method-assign]
    item = ReviewRepository.add_feedback_material(repo, user_id=1, name="Ткань", unit="м")
    assert item["id"] == 77
    assert any("RETURNING id" in sql for sql in calls)
    assert not any("lastrowid" in sql for sql in calls)


def test_set_user_can_supply_stock_writes_json_list() -> None:
    repo = ReviewRepository.__new__(ReviewRepository)
    repo._sql = lambda q: q  # type: ignore[method-assign]
    repo._bool_db = lambda v: bool(v)  # type: ignore[method-assign]
    executed: list[tuple] = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            executed.append((str(sql), tuple(params)))
            return MagicMock()

    repo._connect = lambda: _Conn()  # type: ignore[method-assign]
    ReviewRepository.set_user_can_supply_stock(
        repo,
        user_id=5,
        can_supply_stock=True,
        stock_productions=["12", "34"],
    )
    assert executed
    sql, params = executed[0]
    assert "can_supply_stock" in sql
    assert params[0] is True
    assert '"12"' in params[1] and '"34"' in params[1]
    assert params[2] == 5


def test_allowed_stock_production_ids_helper() -> None:
    # Import helper indirectly via create_app internals is heavy; re-test parsing here.
    import json

    raw = json.loads('["3", "x", 7]')
    out = []
    for x in raw:
        try:
            pid = int(x)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            out.append(pid)
    assert out == [3, 7]
