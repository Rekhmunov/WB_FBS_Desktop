"""Unit tests for supply balances visibility sort_order."""

from review_processor.repository import ReviewRepository


def test_supply_balance_item_sort_key_prefers_explicit_order() -> None:
    sort_map = {("product", 2): 0, ("product", 1): 1}
    a = ReviewRepository.supply_balance_item_sort_key(
        item_type="product", item_id=2, name="Beta", sort_map=sort_map
    )
    b = ReviewRepository.supply_balance_item_sort_key(
        item_type="product", item_id=1, name="Alpha", sort_map=sort_map
    )
    c = ReviewRepository.supply_balance_item_sort_key(
        item_type="product", item_id=9, name="Zulu", sort_map=sort_map
    )
    assert a < b < c


def test_supply_balance_item_sort_key_falls_back_to_name() -> None:
    sort_map: dict = {}
    a = ReviewRepository.supply_balance_item_sort_key(
        item_type="material", item_id=1, name="Абрикос", sort_map=sort_map
    )
    b = ReviewRepository.supply_balance_item_sort_key(
        item_type="material", item_id=2, name="Яблоко", sort_map=sort_map
    )
    assert a < b
