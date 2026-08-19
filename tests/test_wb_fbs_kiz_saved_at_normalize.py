"""Optimistic concurrency tokens for kiz_saved_at / pick_verified_at."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from review_processor.wb_fbs import _normalize_kiz_saved_at


def test_normalize_empty() -> None:
    assert _normalize_kiz_saved_at(None) == ""
    assert _normalize_kiz_saved_at("") == ""
    assert _normalize_kiz_saved_at("   ") == ""


def test_normalize_utc_string_and_z_suffix() -> None:
    a = _normalize_kiz_saved_at("2026-08-16T17:56:37.123456+00:00")
    b = _normalize_kiz_saved_at("2026-08-16T17:56:37.123456Z")
    assert a == b
    assert a.endswith("+00:00") or a.endswith("Z") or "+00:00" in a


def test_normalize_msk_offset_same_instant_as_utc() -> None:
    """PG TIMESTAMPTZ often comes back as Europe/Moscow — must match UTC write token."""
    utc = "2026-08-16T17:56:37.123456+00:00"
    msk = "2026-08-16T20:56:37.123456+03:00"
    assert _normalize_kiz_saved_at(utc) == _normalize_kiz_saved_at(msk)


def test_normalize_datetime_objects_across_zones() -> None:
    instant = datetime(2026, 8, 16, 17, 56, 37, 123456, tzinfo=UTC)
    msk = timezone(timedelta(hours=3))
    as_msk = instant.astimezone(msk)
    assert _normalize_kiz_saved_at(instant) == _normalize_kiz_saved_at(as_msk)
    assert _normalize_kiz_saved_at(instant) == _normalize_kiz_saved_at(instant.isoformat())


def test_normalize_naive_datetime_treated_as_utc() -> None:
    naive = datetime(2026, 8, 16, 17, 56, 37, 123456)
    aware = datetime(2026, 8, 16, 17, 56, 37, 123456, tzinfo=UTC)
    assert _normalize_kiz_saved_at(naive) == _normalize_kiz_saved_at(aware)
