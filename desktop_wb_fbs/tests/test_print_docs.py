# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from app.services.print_docs import _fetch_picking_stickers, print_picking_list


class FetchPickingStickersTests(unittest.TestCase):
    @patch("app.services.print_docs.fetch_stickers_map")
    def test_uses_svg_without_png_when_codes_present(self, fetch_mock):
        fetch_mock.return_value = {
            1: {"partA": "A", "partB": "B", "file_b64": ""},
            2: {"partA": "C", "partB": "D", "file_b64": ""},
        }
        out = _fetch_picking_stickers("key", [1, 2])
        self.assertEqual(out[1]["partB"], "B")
        self.assertEqual(out[2]["partB"], "D")
        fetch_mock.assert_called_once()
        fetch_mock.assert_called_with(
            "key", [1, 2], sticker_type="svg", keep_files=False
        )

    @patch("app.services.print_docs.fetch_stickers_map")
    def test_falls_back_to_png_only_for_missing_partb(self, fetch_mock):
        fetch_mock.side_effect = [
            {
                1: {"partA": "A1", "partB": "B1", "file_b64": ""},
                2: {"partA": "A2", "partB": "", "file_b64": ""},
            },
            {2: {"partA": "A2", "partB": "B2", "file_b64": ""}},
        ]
        out = _fetch_picking_stickers("key", [1, 2])
        self.assertEqual(out[1]["partB"], "B1")
        self.assertEqual(out[2]["partB"], "B2")
        self.assertEqual(fetch_mock.call_count, 2)
        fetch_mock.assert_any_call("key", [1, 2], sticker_type="svg", keep_files=False)
        fetch_mock.assert_any_call("key", [2], sticker_type="png", keep_files=False)

    @patch("app.services.print_docs.fetch_stickers_map")
    def test_skips_png_when_svg_has_all_partb(self, fetch_mock):
        fetch_mock.return_value = {
            1: {"partA": "A", "partB": "B", "file_b64": ""},
            2: {"partA": "C", "partB": "D", "file_b64": ""},
        }
        out = _fetch_picking_stickers("key", [1, 2])
        self.assertEqual(out[2]["partB"], "D")
        fetch_mock.assert_called_once_with(
            "key", [1, 2], sticker_type="svg", keep_files=False
        )


class FetchStickersCacheTests(unittest.TestCase):
    @patch("app.services.print_docs.WbFbsClient")
    def test_reuses_cached_png_stickers(self, client_cls):
        from app.services import print_docs

        client = client_cls.return_value
        client.get_order_stickers.return_value = [
            {"orderId": 1, "partA": "A", "partB": "B", "file": "ZmlsZQ=="}
        ]
        print_docs._stickers_cache.clear()
        first = print_docs.fetch_stickers_map("secret-key", [1])
        second = print_docs.fetch_stickers_map("secret-key", [1])
        self.assertEqual(first[1]["partB"], "B")
        self.assertEqual(second[1]["partB"], "B")
        client.get_order_stickers.assert_called_once()


class PrintPickingListTests(unittest.TestCase):
    @patch("app.services.print_docs.open_html")
    @patch("app.services.print_docs.fetch_cards")
    @patch("app.services.print_docs._fetch_picking_stickers")
    @patch("app.services.print_docs.ProductService")
    def test_summary_skips_wb_sticker_and_content_calls(
        self, product_svc, fetch_pick, fetch_cards, open_html
    ):
        product_svc.return_value.list_all.return_value = []
        orders_svc = MagicMock()
        orders_svc.get_supply.return_value = {"name": "Test"}
        orders_svc.orders_in_supply.return_value = [
            {"order_id": 1, "article": "art-1", "nm_id": 100, "skus_json": "[]"}
        ]
        open_html.return_value = MagicMock(name="feedpilot.html")

        print_picking_list(
            MagicMock(),
            orders_svc,
            1,
            "api-key",
            "WB-GI-1",
            variant="summary",
        )

        fetch_pick.assert_not_called()
        fetch_cards.assert_not_called()
        orders_svc.orders_in_supply.assert_called_with(1, "WB-GI-1", api_key="")

    @patch("app.services.print_docs.open_html")
    @patch("app.services.print_docs.fetch_cards")
    @patch("app.services.print_docs._fetch_picking_stickers")
    @patch("app.services.print_docs.ProductService")
    def test_extended_skips_content_api(
        self, product_svc, fetch_pick, fetch_cards, open_html
    ):
        product_svc.return_value.list_all.return_value = []
        fetch_pick.return_value = {1: {"partA": "A", "partB": "B", "file_b64": ""}}
        orders_svc = MagicMock()
        orders_svc.get_supply.return_value = {"name": "Test"}
        orders_svc.orders_in_supply.return_value = [
            {"order_id": 1, "article": "art-1", "nm_id": 100, "skus_json": "[]"}
        ]
        open_html.return_value = MagicMock(name="feedpilot.html")

        print_picking_list(
            MagicMock(),
            orders_svc,
            1,
            "api-key",
            "WB-GI-1",
            variant="extended",
        )

        fetch_pick.assert_called_once()
        fetch_cards.assert_not_called()

    @patch("app.services.print_docs.open_html")
    @patch("app.services.print_docs._fetch_picking_stickers")
    @patch("app.services.print_docs.ProductService")
    def test_extended_reuses_preloaded_stickers(
        self, product_svc, fetch_pick, open_html
    ):
        product_svc.return_value.list_all.return_value = []
        orders_svc = MagicMock()
        orders_svc.get_supply.return_value = {"name": "Test"}
        orders_svc.orders_in_supply.return_value = [
            {"order_id": 1, "article": "art-1", "nm_id": 100, "skus_json": "[]"},
            {"order_id": 2, "article": "art-2", "nm_id": 101, "skus_json": "[]"},
        ]
        open_html.return_value = MagicMock(name="feedpilot.html")

        print_picking_list(
            MagicMock(),
            orders_svc,
            1,
            "api-key",
            "WB-GI-1",
            variant="extended",
            preloaded_stickers={
                1: {"partA": "A", "partB": "B", "file_b64": ""},
                2: {"partA": "C", "partB": "D", "file_b64": ""},
            },
        )

        fetch_pick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
