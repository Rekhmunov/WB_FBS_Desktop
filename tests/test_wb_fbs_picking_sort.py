"""Picking-list sort must match WB seller-portal (ЛК) order."""

from review_processor.wb_fbs_detail import _portal_title_sort_key, _sort_groups_like_wb


def test_portal_title_order_from_lk_sample():
    titles = [
        "Наматрасник 180х200 см на резинке, толстый",
        "Наматрасник 160х200 см на резинке, толстый",
        "Наматрасник 180х200 на резинке с бортами, толстый",
    ]
    ordered = sorted(titles, key=_portal_title_sort_key)
    assert ordered == [
        "Наматрасник 160х200 см на резинке, толстый",
        "Наматрасник 180х200 на резинке с бортами, толстый",
        "Наматрасник 180х200 см на резинке, толстый",
    ]


def test_groups_sort_by_wb_title_not_article_or_settings_name():
    groups = [
        {
            "article": "nambambbort180200begbort",
            "product_name": "Наматрасник",  # Settings short name — must not drive order
            "wb_title": "Наматрасник 180х200 см на резинке, толстый",
            "nm_id": 3,
            "orders": [
                {"sticker_part_b": "3640", "sticker_part_a": "1", "order_id": 4},
                {"sticker_part_b": "0169", "sticker_part_a": "1", "order_id": 1},
            ],
        },
        {
            "article": "nambambbort160200begbort",
            "product_name": "Наматрасник",
            "wb_title": "Наматрасник 160х200 см на резинке, толстый",
            "nm_id": 1,
            "orders": [
                {"sticker_part_b": "9959", "sticker_part_a": "1", "order_id": 24},
                {"sticker_part_b": "7392", "sticker_part_a": "1", "order_id": 18},
            ],
        },
        {
            "article": "nambambbort180200greybort18020030",
            "product_name": "Наматрасник",
            "wb_title": "Наматрасник 180х200 на резинке с бортами, толстый",
            "nm_id": 2,
            "orders": [
                {"sticker_part_b": "9735", "sticker_part_a": "1", "order_id": 10},
                {"sticker_part_b": "0039", "sticker_part_a": "1", "order_id": 1},
            ],
        },
    ]
    sorted_groups = _sort_groups_like_wb(groups)
    assert [g["article"] for g in sorted_groups] == [
        "nambambbort160200begbort",
        "nambambbort180200greybort18020030",
        "nambambbort180200begbort",
    ]
    assert [o["sticker_part_b"] for o in sorted_groups[0]["orders"]] == ["7392", "9959"]
    assert [o["sticker_part_b"] for o in sorted_groups[1]["orders"]] == ["0039", "9735"]
    assert [o["sticker_part_b"] for o in sorted_groups[2]["orders"]] == ["0169", "3640"]
