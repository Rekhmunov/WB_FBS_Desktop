"""TSD hub progress must match KIZ/pick scan classification (without stickers)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from review_processor.wb_fbs_detail import build_tsd_hub_progress


class TsdHubProgressParityTests(unittest.TestCase):
    def test_splits_by_live_meta_not_raw_json(self) -> None:
        """Live meta is authoritative — raw_json alone must not decide tiles."""
        repo = MagicMock()
        orders = [
            {
                "order_id": 1,
                "raw_json": "{}",  # no requiredMeta locally
                "pick_verified": False,
                "pick_barcode": "",
            },
            {
                "order_id": 2,
                "raw_json": '{"requiredMeta":["sgtin"]}',  # stale raw
                "pick_verified": False,
                "pick_barcode": "",
            },
            {
                "order_id": 3,
                "raw_json": "{}",
                "pick_verified": True,
                "pick_barcode": "4670123456789",
            },
        ]
        kiz_map = {
            # Live says order 1 needs KIZ even though raw is empty.
            1: {
                "kiz_required": True,
                "kiz_codes": ["01046LOCAL"],
                "kiz_status": "pending",
            },
            # Live says order 2 does NOT need KIZ despite stale raw sgtin.
            2: {
                "kiz_required": False,
                "kiz_codes": [],
                "kiz_status": "empty",
            },
            3: {
                "kiz_required": False,
                "kiz_codes": [],
                "kiz_status": "empty",
            },
        }
        with patch(
            "review_processor.wb_fbs_detail._tsd_hub_resolve_order_ids",
            return_value=[1, 2, 3],
        ), patch(
            "review_processor.wb_fbs_detail._tsd_hub_load_order_rows",
            return_value=orders,
        ), patch(
            "review_processor.wb_fbs_detail._fetch_kiz_map",
            return_value=kiz_map,
        ), patch(
            "review_processor.wb_fbs_detail.wb.load_order_kiz_map",
            return_value={1: {"codes": ["01046LOCAL"], "saved_at": "t1"}},
        ), patch(
            "review_processor.wb_fbs_detail.wb.WbFbsClient"
        ):
            out = build_tsd_hub_progress(
                repo,
                user_id=10,
                source_id=7,
                api_key="key",
                supply_id="WB-1",
            )
        self.assertEqual(out["kiz"], {"total": 1, "done": 1})
        self.assertEqual(out["pick"], {"total": 2, "done": 1})
        self.assertEqual(out["order_count"], 3)

    def test_kiz_done_prefers_local_draft_over_wb_codes(self) -> None:
        repo = MagicMock()
        orders = [
            {
                "order_id": 5,
                "raw_json": "{}",
                "pick_verified": False,
                "pick_barcode": "",
            }
        ]
        kiz_map = {
            5: {
                "kiz_required": True,
                "kiz_codes": ["01046WB"],
                "kiz_status": "ok",
            }
        }
        with patch(
            "review_processor.wb_fbs_detail._tsd_hub_resolve_order_ids",
            return_value=[5],
        ), patch(
            "review_processor.wb_fbs_detail._tsd_hub_load_order_rows",
            return_value=orders,
        ), patch(
            "review_processor.wb_fbs_detail._fetch_kiz_map",
            return_value=kiz_map,
        ), patch(
            # Local clear draft → not done (same as scan row with [""]).
            "review_processor.wb_fbs_detail.wb.load_order_kiz_map",
            return_value={5: {"codes": [], "saved_at": "t-clear"}},
        ), patch(
            "review_processor.wb_fbs_detail.wb.WbFbsClient"
        ):
            out = build_tsd_hub_progress(
                repo,
                user_id=1,
                source_id=1,
                api_key="key",
                supply_id="S",
            )
        self.assertEqual(out["kiz"], {"total": 1, "done": 0})

    def test_empty_supply_id(self) -> None:
        repo = MagicMock()
        out = build_tsd_hub_progress(
            repo, user_id=1, source_id=1, api_key="k", supply_id="  "
        )
        self.assertEqual(out["order_count"], 0)


if __name__ == "__main__":
    unittest.main()
