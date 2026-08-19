"""Regression tests for «Собрать все МГТ» supply naming / conflicts."""

from __future__ import annotations

from review_processor.wb_fbs import (
    _plan_mgt_group,
    _unique_supply_name,
    default_mgt_supply_name,
)


def test_unique_supply_name_leaves_free_title() -> None:
    assert _unique_supply_name("Поставка от 07.08.2026", set()) == "Поставка от 07.08.2026"


def test_unique_supply_name_suffixes_when_taken() -> None:
    existing = {"Поставка от 07.08.2026"}
    assert _unique_supply_name("Поставка от 07.08.2026", existing) == (
        "Поставка от 07.08.2026 (2)"
    )


def test_plan_mgt_group_name_has_no_warehouse_suffix() -> None:
    existing: set[str] = set()
    group = _plan_mgt_group(
        is_b2b=False,
        order_ids=[101, 102],
        mgt_matching=[],
        empties=[],
        existing_names=existing,
        warehouse_id=1943422,
        cross_border_type=None,
    )
    assert group["mode"] == "create"
    name = str(group["suggested_name"])
    assert "склад" not in name.lower()
    assert "1943422" not in name
    assert name.startswith("Поставка от ")
    # Suggested title is reserved in the working set, but not marked as conflict.
    assert group["name_conflict"] is False
    assert name in existing


def test_plan_mgt_group_second_bucket_gets_suffix_not_false_conflict() -> None:
    """Two create-buckets share the date title — second gets (2), no conflict flag."""
    reserved: set[str] = set()
    g1 = _plan_mgt_group(
        is_b2b=False,
        order_ids=[1],
        mgt_matching=[],
        empties=[],
        existing_names=reserved,
        warehouse_id=111,
    )
    g2 = _plan_mgt_group(
        is_b2b=False,
        order_ids=[2],
        mgt_matching=[],
        empties=[],
        existing_names=reserved,
        warehouse_id=222,
    )
    assert g1["suggested_name"] != g2["suggested_name"]
    assert g1["name_conflict"] is False
    assert g2["name_conflict"] is False
    assert "склад" not in str(g1["suggested_name"]).lower()
    assert "склад" not in str(g2["suggested_name"]).lower()


def test_preview_existing_names_must_not_include_suggested(monkeypatch) -> None:
    """FE checks existing_names.has(suggested) — suggested must not be listed."""
    from review_processor import wb_fbs as wb

    def fake_orders(repo, *, user_id, source_id):
        return [
            {
                "order_id": 10,
                "is_b2b": False,
                "warehouse_id": 1943422,
                "cross_border_type": None,
            }
        ]

    def fake_supplies(repo, *, user_id, source_id, only_open=True):
        return []

    monkeypatch.setattr(wb, "_load_new_mgt_orders", fake_orders)
    monkeypatch.setattr(wb, "list_supplies", fake_supplies)
    monkeypatch.setattr(wb, "ensure_wb_fbs_tables", lambda repo: None)

    preview = wb.preview_collect_mgt(object(), user_id=1, source_id=2)
    assert preview["ok"] is True
    assert preview["groups"]
    suggested = str(preview["groups"][0]["suggested_name"])
    existing = set(preview["existing_names"] or [])
    assert suggested not in existing
    assert "склад" not in suggested.lower()
    assert suggested == default_mgt_supply_name(is_b2b=False) or suggested.startswith(
        "Поставка от "
    )
