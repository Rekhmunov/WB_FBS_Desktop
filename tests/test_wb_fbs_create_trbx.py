"""WB FBS cargo-place (trbx) create validation."""

import pytest

from review_processor.wb_fbs import WbFbsClient, _trbx_box_id
from review_processor.wb_fbs_detail import render_trbx_stickers_html


def test_create_supply_boxes_rejects_bad_amount():
    client = WbFbsClient("dummy-key")
    with pytest.raises(ValueError, match="от 1 до 1000"):
        client.create_supply_boxes("WB-GI-1", 0)
    with pytest.raises(ValueError, match="от 1 до 1000"):
        client.create_supply_boxes("WB-GI-1", 1001)
    with pytest.raises(ValueError, match="ID поставки"):
        client.create_supply_boxes("", 1)


def test_delete_supply_boxes_validation():
    client = WbFbsClient("dummy-key")
    with pytest.raises(ValueError, match="ID поставки"):
        client.delete_supply_boxes("", ["WB-TRBX-1"])
    with pytest.raises(ValueError, match="ID грузомест"):
        client.delete_supply_boxes("WB-GI-1", [])


def test_ui_remaining_boxes_formula():
    # Mirror front-end: remaining = min(1000, max(1, orders+1) - existing)
    def remaining(orders: int, existing: int) -> int:
        max_total = max(1, orders + 1)
        return max(0, min(1000, max_total - existing))

    assert remaining(5, 0) == 6
    assert remaining(5, 2) == 4
    assert remaining(5, 6) == 0
    assert remaining(0, 0) == 1


def test_trbx_box_id_normalization():
    assert _trbx_box_id({"id": "WB-TRBX-1"}) == "WB-TRBX-1"
    assert _trbx_box_id({"trbxId": "WB-TRBX-2"}) == "WB-TRBX-2"
    assert _trbx_box_id("WB-TRBX-3") == "WB-TRBX-3"
    assert _trbx_box_id({"id": "  "}) == ""


def test_render_trbx_stickers_html():
    # Minimal valid base64 for PNG-ish payload (alphabet only matters for sanitize).
    b64 = "aGVsbG8="
    html_doc = render_trbx_stickers_html(
        supply_id="WB-GI-1",
        stickers=[{"barcode": "WB-TRBX-9", "file": b64}],
    )
    assert "WB-TRBX-9" in html_doc
    assert "data:image/png;base64,aGVsbG8=" in html_doc
    assert "window.print()" in html_doc
    with pytest.raises(ValueError, match="не вернул"):
        render_trbx_stickers_html(supply_id="WB-GI-1", stickers=[])


def test_fetch_trbx_stickers_chunks_over_100(monkeypatch):
    """WB allows ≤100 trbxIds per stickers request — we must chunk."""
    from review_processor import wb_fbs as mod

    calls: list[list[str]] = []

    class FakeClient:
        def __init__(self, _key):
            pass

        def get_supply_boxes(self, _sid):
            return [{"id": f"WB-TRBX-{i}"} for i in range(1, 132)]

        def get_box_stickers(self, _sid, box_ids, *, sticker_type="png"):
            ids = list(box_ids)
            assert len(ids) <= mod.TRBX_STICKERS_PER_REQUEST
            calls.append(ids)
            return [{"trbxId": bid, "file": "aGVsbG8="} for bid in ids]

    monkeypatch.setattr(mod, "WbFbsClient", FakeClient)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    stickers = mod.fetch_trbx_stickers(api_key="k", supply_id="WB-GI-1")
    assert len(stickers) == 131
    assert len(calls) == 2
    assert len(calls[0]) == 100
    assert len(calls[1]) == 31


def test_get_box_stickers_rejects_over_limit():
    client = WbFbsClient("dummy-key")
    with pytest.raises(ValueError, match="Не больше 100"):
        client.get_box_stickers("WB-GI-1", [f"id-{i}" for i in range(101)])


def test_list_supply_trbx_skips_stub_upsert(monkeypatch):
    """Failed get_supply must not wipe local name/done via stub upsert."""
    from review_processor import wb_fbs as mod

    calls = {"upsert": 0, "persist": 0}

    class FakeClient:
        def __init__(self, _key):
            pass

        def get_supply(self, _sid):
            raise RuntimeError("wb down")

        def get_supply_boxes(self, _sid):
            return [{"id": "WB-TRBX-1"}]

        def get_supply_order_ids(self, _sid):
            return [11, 22]

    monkeypatch.setattr(mod, "WbFbsClient", FakeClient)
    monkeypatch.setattr(mod, "ensure_wb_fbs_tables", lambda _repo: None)
    monkeypatch.setattr(
        mod,
        "_local_supply_order_ids",
        lambda *_a, **_k: [],
    )

    def fake_upsert(*_a, **_k):
        calls["upsert"] += 1

    def fake_persist(*_a, **_k):
        calls["persist"] += 1

    monkeypatch.setattr(mod, "upsert_supply", fake_upsert)
    monkeypatch.setattr(mod, "_persist_supply_boxes", fake_persist)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)

    class _Detail:
        @staticmethod
        def invalidate_supply_detail_cache(**_k):
            return None

    monkeypatch.setitem(__import__("sys").modules, "review_processor.wb_fbs_detail", _Detail)

    out = mod.list_supply_trbx(
        repo=object(),
        user_id=1,
        source_id=2,
        api_key="k",
        supply_id="WB-GI-1",
    )
    assert out["ok"] is True
    assert out["boxes"] == [{"id": "WB-TRBX-1"}]
    assert out["remaining"] == 2  # max(1, 2+1) - 1 box
    assert calls["upsert"] == 0
    assert calls["persist"] == 1
