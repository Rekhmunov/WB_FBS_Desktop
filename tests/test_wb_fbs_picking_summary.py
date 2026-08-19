"""Picking list variants: summary vs extended."""

import re

from review_processor.wb_fbs_detail import render_picking_list_html

_PAYLOAD = {
    "detail": {"supply_id": "S1", "created_date": "2026-08-07", "order_count": 3},
    "groups": [
        {
            "product_name": "Товар A",
            "qty": 2,
            "orders": [
                {"order_id": 1, "sticker_part_a": "a", "sticker_part_b": "11"},
                {"order_id": 2, "sticker_part_a": "b", "sticker_part_b": "22"},
            ],
            "barcodes": ["111"],
        },
        {
            "product_name": "Товар A",
            "qty": 1,
            "orders": [
                {"order_id": 3, "sticker_part_a": "c", "sticker_part_b": "33"},
            ],
            "barcodes": ["222"],
        },
        {
            "product_name": "Товар B",
            "qty": 1,
            "orders": [
                {"order_id": 4, "sticker_part_a": "d", "sticker_part_b": "44"},
            ],
            "barcodes": [],
        },
    ],
}


def test_picking_list_summary_only_by_default():
    html = render_picking_list_html(_PAYLOAD)
    assert "summary-page" in html
    assert "detail-page" not in html
    assert "Товар A — 3 шт." in html
    assert "Товар B — 1 шт." in html
    assert "Упаковано" not in html
    assert "Лист подбора S1" in html
    assert "Расширенный лист подбора" not in html

    summary = re.search(r'class="summary-page".*?</section>', html, re.S).group(0)
    assert "Всего 3 заказа" in summary
    assert "Собрано" in summary


def test_picking_list_extended_has_detail_only():
    html = render_picking_list_html(_PAYLOAD, variant="extended")
    assert "summary-page" not in html
    assert "detail-page" in html
    assert "Товар A — 3 шт." not in html
    assert "Упаковано" in html
    assert "Расширенный лист подбора S1" in html
    assert "Заказ: 1" in html
    assert "Стикер WB:" in html
