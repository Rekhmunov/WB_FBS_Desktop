"""Auto-collect MGT: MSK window + safe decision planner."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from review_processor.repository import ReviewRepository
from review_processor.wb_fbs import (
    _msk_time_in_active_window,
    auto_collect_reason_ru,
    default_mgt_supply_name,
    plan_auto_collect_mgt_decisions,
)

_MSK = ZoneInfo("Europe/Moscow")


def test_msk_window_overnight_active_and_inactive() -> None:
    # 12:00 → 06:00 MSK
    noon = datetime(2026, 8, 7, 12, 0, tzinfo=_MSK)
    evening = datetime(2026, 8, 7, 23, 30, tzinfo=_MSK)
    early = datetime(2026, 8, 8, 5, 59, tzinfo=_MSK)
    at_end = datetime(2026, 8, 8, 6, 0, tzinfo=_MSK)
    morning = datetime(2026, 8, 8, 6, 1, tzinfo=_MSK)
    before_noon = datetime(2026, 8, 8, 11, 59, tzinfo=_MSK)

    assert _msk_time_in_active_window(
        now_msk=noon, active_from="12:00", active_to="06:00"
    )
    assert _msk_time_in_active_window(
        now_msk=evening, active_from="12:00", active_to="06:00"
    )
    assert _msk_time_in_active_window(
        now_msk=early, active_from="12:00", active_to="06:00"
    )
    assert _msk_time_in_active_window(
        now_msk=at_end, active_from="12:00", active_to="06:00"
    )
    assert not _msk_time_in_active_window(
        now_msk=morning, active_from="12:00", active_to="06:00"
    )
    assert not _msk_time_in_active_window(
        now_msk=before_noon, active_from="12:00", active_to="06:00"
    )


def test_auto_plan_add_one_when_single_mgt() -> None:
    preview = {
        "mgt_count": 2,
        "existing_names": ["Поставка МГТ"],
        "groups": [
            {
                "group_key": "non_wh1_cbna",
                "is_b2b": False,
                "mode": "add_one",
                "default_supply_id": "WB-GI-1",
            }
        ],
    }
    open_supplies = [
        {"supply_id": "WB-GI-1", "cargo_type": 1, "order_ids": [1], "name": "Поставка МГТ"}
    ]
    decisions, reason = plan_auto_collect_mgt_decisions(
        preview, open_supplies=open_supplies
    )
    assert reason == ""
    assert decisions == [
        {
            "group_key": "non_wh1_cbna",
            "is_b2b": False,
            "action": "add",
            "supply_id": "WB-GI-1",
        }
    ]


def test_auto_plan_skips_when_several_mgt_or_empty() -> None:
    preview = {
        "mgt_count": 1,
        "existing_names": ["A", "B"],
        "groups": [
            {
                "group_key": "g1",
                "is_b2b": False,
                "mode": "choose",
                "default_supply_id": "",
            }
        ],
    }
    open_supplies = [
        {"supply_id": "S1", "cargo_type": 1, "order_ids": [1], "name": "A"},
        {"supply_id": "S2", "cargo_type": 1, "order_ids": [2], "name": "B"},
    ]
    decisions, reason = plan_auto_collect_mgt_decisions(
        preview, open_supplies=open_supplies
    )
    assert decisions is None
    assert reason == "several_open_supplies"


def test_auto_plan_ignores_non_mgt_and_creates_when_name_free() -> None:
    template = default_mgt_supply_name(is_b2b=False)
    preview = {
        "mgt_count": 1,
        "existing_names": ["SGT поставка"],
        "groups": [
            {
                "group_key": "g1",
                "is_b2b": False,
                "mode": "create",
                "suggested_name": f"{template} (2)",  # must ignore suffix for auto
            }
        ],
    }
    open_supplies = [
        {"supply_id": "SGT1", "cargo_type": 2, "order_ids": [9], "name": "SGT поставка"}
    ]
    decisions, reason = plan_auto_collect_mgt_decisions(
        preview, open_supplies=open_supplies
    )
    assert reason == ""
    assert decisions == [
        {
            "group_key": "g1",
            "is_b2b": False,
            "action": "create",
            "name": template,
        }
    ]


def test_auto_plan_skips_create_when_template_name_exists() -> None:
    template = default_mgt_supply_name(is_b2b=False)
    preview = {
        "mgt_count": 1,
        "existing_names": [template],
        "groups": [
            {
                "group_key": "g1",
                "is_b2b": False,
                "mode": "create",
                "suggested_name": f"{template} (2)",
            }
        ],
    }
    open_supplies = [
        {"supply_id": "X", "cargo_type": 2, "order_ids": [1], "name": template}
    ]
    decisions, reason = plan_auto_collect_mgt_decisions(
        preview, open_supplies=open_supplies
    )
    assert decisions is None
    assert reason == "name_conflict"


def test_auto_plan_skips_choose_mode() -> None:
    preview = {
        "mgt_count": 1,
        "existing_names": [],
        "groups": [
            {
                "group_key": "g1",
                "is_b2b": False,
                "mode": "choose",
            }
        ],
    }
    decisions, reason = plan_auto_collect_mgt_decisions(preview, open_supplies=[])
    assert decisions is None
    assert reason == "needs_choice"


def test_parse_hhmm_accepts_browser_seconds() -> None:
    assert ReviewRepository._parse_hhmm_strict(
        "12:00:00", field="t", default="00:00"
    ) == "12:00"
    assert ReviewRepository._parse_hhmm_strict(
        "06:00:00", field="t", default="00:00"
    ) == "06:00"
    assert ReviewRepository._normalize_hhmm("9:05:00", default="12:00") == "09:05"


def test_auto_collect_reason_ru_human_readable() -> None:
    assert "несколько поставок" in auto_collect_reason_ru("several_open_supplies").lower()
    assert "назван" in auto_collect_reason_ru("name_conflict").lower()
    assert "выбор" in auto_collect_reason_ru("needs_choice").lower()
    assert "новые" in auto_collect_reason_ru("no_mgt").lower()
    assert "источников" in auto_collect_reason_ru("list_jobs_error: boom").lower()
