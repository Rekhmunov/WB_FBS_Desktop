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
            "key", [1, 2], sticker_type="svg", keep_files=False, progress=None
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
        fetch_mock.assert_any_call(
            "key", [1, 2], sticker_type="svg", keep_files=False, progress=None
        )
        fetch_mock.assert_any_call(
            "key", [2], sticker_type="png", keep_files=False, progress=unittest.mock.ANY
        )

    @patch("app.services.print_docs.fetch_stickers_map")
    def test_skips_png_when_svg_has_all_partb(self, fetch_mock):
        fetch_mock.return_value = {
            1: {"partA": "A", "partB": "B", "file_b64": ""},
            2: {"partA": "C", "partB": "D", "file_b64": ""},
        }
        out = _fetch_picking_stickers("key", [1, 2])
        self.assertEqual(out[2]["partB"], "D")
        fetch_mock.assert_called_once_with(
            "key", [1, 2], sticker_type="svg", keep_files=False, progress=None
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

    @patch("app.services.print_docs.copy.deepcopy")
    @patch("app.services.print_docs.WbFbsClient")
    def test_sticker_cache_avoids_deepcopy(self, client_cls, deepcopy_mock):
        from app.services import print_docs

        client = client_cls.return_value
        client.get_order_stickers.return_value = [
            {"orderId": 1, "partA": "A", "partB": "B", "file": "ZmlsZQ=="}
        ]
        print_docs._stickers_cache.clear()
        print_docs.fetch_stickers_map("secret-key", [1])
        deepcopy_mock.assert_not_called()

    @patch("app.services.sticker_png_isolated.fetch_png_ids_isolated")
    def test_cache_only_preload_does_not_duplicate_payload(self, isolated_mock):
        from app.services import print_docs

        isolated_mock.return_value = {
            i: {
                "partA": "A",
                "partB": str(i),
                "file_b64": "",
                "file_path": "/tmp/{}.png".format(i),
            }
            for i in range(1, 4)
        }
        print_docs._stickers_cache.clear()
        print_docs.fetch_stickers_map(
            "secret-key",
            [1, 2, 3],
            cache_only=True,
            persist_supply_id="WB-GI-1",
        )
        cached = print_docs.get_cached_stickers_map("secret-key", [1, 2, 3])
        self.assertEqual(len(cached or {}), 3)
        self.assertEqual(
            print_docs._cache_sticker_count(
                print_docs._stickers_cache_key("secret-key", [1, 2, 3], "png", True)
            ),
            3,
        )
        isolated_mock.assert_called_once()
        for meta in cached.values():
            self.assertEqual(meta.get("file_b64"), "")
            self.assertTrue(meta.get("file_path"))

    @patch("app.services.print_docs.WbFbsClient")
    def test_reports_chunk_progress(self, client_cls):
        from app.services import print_docs

        client = client_cls.return_value
        client.get_order_stickers.return_value = [
            {"orderId": i, "partA": "A", "partB": str(i), "file": "ZmlsZQ=="}
            for i in range(1, 6)
        ]
        print_docs._stickers_cache.clear()
        seen = []

        print_docs.fetch_stickers_map(
            "secret-key",
            list(range(1, 251)),
            progress=lambda done, total: seen.append((done, total)),
        )
        self.assertTrue(seen)
        self.assertEqual(seen[0], (0, 250))
        self.assertEqual(seen[-1], (250, 250))


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


class PrintSupplyStickersOnDemandTests(unittest.TestCase):
    @patch("app.services.print_docs.open_html")
    @patch("app.services.print_docs.fetch_cards", return_value={})
    @patch("app.services.print_docs.fetch_stickers_map")
    @patch("app.services.print_docs.existing_sticker_paths", create=True)
    @patch("app.services.print_docs.ProductService")
    def test_print_resumes_disk_and_fetches_only_missing(
        self, product_svc, _existing, fetch_map, fetch_cards, open_html
    ):
        from app.services import print_docs

        product_svc.return_value.list_all.return_value = []
        open_html.return_value = MagicMock(name="out.html")
        orders_svc = MagicMock()
        orders_svc.orders_in_supply.return_value = [
            {"order_id": 1, "article": "a1", "nm_id": 10, "skus_json": "[]"},
            {"order_id": 2, "article": "a2", "nm_id": 11, "skus_json": "[]"},
        ]
        with patch(
            "app.services.sticker_file_cache.existing_sticker_paths",
            return_value={1: "/tmp/1.png"},
        ):
            fetch_map.return_value = {
                2: {
                    "partA": "A",
                    "partB": "B",
                    "file_b64": "",
                    "file_path": "/tmp/2.png",
                }
            }
            print_docs.print_supply_stickers(
                MagicMock(),
                orders_svc,
                1,
                "api-key",
                "WB-GI-1",
            )
        fetch_map.assert_called_once()
        args, kwargs = fetch_map.call_args
        self.assertEqual(args[1], [2])
        self.assertEqual(kwargs.get("persist_supply_id"), "WB-GI-1")


class OpenHtmlTests(unittest.TestCase):
    @patch("app.services.print_docs.QDesktopServices")
    @patch("app.ui.html_print_dialog.show_html_print_preview")
    @patch("app.ui.html_print_dialog.webengine_status")
    @patch("PyQt5.QtWidgets.QMessageBox")
    def test_webengine_load_fail_does_not_auto_open_browser(
        self, msg_box, webengine_status, show_preview, desktop
    ):
        from app.services import print_docs

        webengine_status.return_value = (True, "")
        show_preview.return_value = False
        msg_box.question.return_value = msg_box.No
        parent = MagicMock()

        path = print_docs.open_html("<html></html>", "test_doc", parent=parent)

        self.assertTrue(path.exists())
        desktop.openUrl.assert_not_called()
        msg_box.question.assert_called_once()

    @patch("app.services.print_docs.QDesktopServices")
    @patch("app.ui.html_print_dialog.webengine_status")
    def test_no_webengine_falls_back_to_browser(self, webengine_status, desktop):
        from app.services import print_docs

        webengine_status.return_value = (False, "PyQtWebEngineWidgets missing")

        print_docs.open_html("<html></html>", "test_doc_no_we", parent=None)

        desktop.openUrl.assert_called_once()


if __name__ == "__main__":
    unittest.main()
