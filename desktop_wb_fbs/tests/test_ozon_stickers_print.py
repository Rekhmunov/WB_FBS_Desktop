# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.services import ozon_stickers_print as osp
from app.services.ozon_stickers_print import (
    ensure_pymupdf,
    group_orders_by_article,
    render_stickers_print_html,
)


class EnsurePymupdfTest(unittest.TestCase):
    def tearDown(self) -> None:
        osp._pymupdf_ready = False

    def test_already_installed(self) -> None:
        osp._pymupdf_ready = False
        ensure_pymupdf()
        self.assertTrue(osp._pymupdf_ready)

    def test_skips_when_flag_set(self) -> None:
        osp._pymupdf_ready = True
        with patch.object(osp, "_try_import_pymupdf") as try_imp:
            ensure_pymupdf()
            try_imp.assert_not_called()

    def test_installs_when_missing(self) -> None:
        osp._pymupdf_ready = False
        calls = {"n": 0}

        def _try() -> bool:
            calls["n"] += 1
            return calls["n"] >= 2

        with patch.object(osp, "_try_import_pymupdf", side_effect=_try):
            with patch.object(osp, "pip_install") as install:
                ensure_pymupdf()
        self.assertTrue(osp._pymupdf_ready)
        self.assertEqual(calls["n"], 2)
        install.assert_called_once_with("pymupdf>=1.24.0")

    def test_raises_when_pip_fails(self) -> None:
        osp._pymupdf_ready = False
        with patch.object(osp, "_try_import_pymupdf", return_value=False):
            with patch.object(
                osp, "pip_install", side_effect=RuntimeError("no network")
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_pymupdf()
        self.assertIn("pymupdf", str(ctx.exception).casefold())
        self.assertFalse(osp._pymupdf_ready)


class BuildFileFallbackTest(unittest.TestCase):
    def test_pdf_fallback_when_pymupdf_missing(self) -> None:
        labels = MagicMock()
        labels.fetch_label_paths.return_value = ["/tmp/ozon-labels.pdf"]
        client = MagicMock()
        detail = {"name": "S", "orders": []}
        with patch.object(
            osp, "ensure_pymupdf", side_effect=RuntimeError("no pymupdf")
        ):
            path = osp.build_stickers_print_file(
                labels,
                client,
                detail,
                ["PN-1"],
                source_id=7,
            )
        self.assertEqual(path, "/tmp/ozon-labels.pdf")
        labels.fetch_label_paths.assert_called_once()
        self.assertEqual(labels.fetch_label_paths.call_args[0][1], 7)


class GroupByArticleTest(unittest.TestCase):
    def test_groups_and_sorts(self) -> None:
        orders = [
            {
                "posting_number": "2-1",
                "offer_id": "B",
                "product_name": "Бета",
                "sku": "20",
                "barcodes": ["222"],
                "status": "awaiting_deliver",
                "tab": "awaiting_deliver",
            },
            {
                "posting_number": "1-1",
                "offer_id": "A",
                "product_name": "Альфа",
                "sku": "10",
                "barcodes": ["111"],
                "status": "awaiting_deliver",
                "tab": "awaiting_deliver",
            },
            {
                "posting_number": "1-2",
                "offer_id": "A",
                "product_name": "Альфа",
                "sku": "10",
                "barcodes": ["111"],
                "status": "awaiting_deliver",
                "tab": "awaiting_deliver",
            },
            {
                "posting_number": "X-1",
                "offer_id": "Z",
                "product_name": "Отмена",
                "status": "cancelled",
                "tab": "cancelled",
            },
        ]
        groups = group_orders_by_article(orders)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["article"], "A")
        self.assertEqual(groups[0]["qty"], 2)
        self.assertEqual(
            [o["posting_number"] for o in groups[0]["orders"]], ["1-1", "1-2"]
        )
        self.assertEqual(groups[1]["article"], "B")


class RenderHtmlTest(unittest.TestCase):
    def test_cover_separator_and_sticker(self) -> None:
        detail = {
            "supply_id": "OZ-1",
            "name": "Поставка тест",
            "orders": [
                {
                    "posting_number": "PN-1",
                    "offer_id": "ART-1",
                    "product_name": "Товар один",
                    "sku": "99",
                    "barcodes": ["460123"],
                    "status": "awaiting_deliver",
                    "tab": "awaiting_deliver",
                },
                {
                    "posting_number": "PN-2",
                    "offer_id": "ART-1",
                    "product_name": "Товар один",
                    "sku": "99",
                    "barcodes": ["460123"],
                    "status": "awaiting_deliver",
                    "tab": "awaiting_deliver",
                },
            ],
        }
        # Minimal 1x1 PNG
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        images = {"PN-1": [png_b64], "PN-2": [png_b64]}
        html = render_stickers_print_html(
            detail,
            images,
            source_name="Магазин",
            include_cover_and_separators=True,
        )
        self.assertIn('class="label cover"', html)
        self.assertIn("Поставка тест", html)
        self.assertIn('class="label separator"', html)
        self.assertIn("Артикул: ART-1", html)
        self.assertIn("2 шт.", html)
        self.assertEqual(html.count('class="label sticker"'), 2)
        self.assertIn("data:image/png;base64,", html)
        self.assertNotIn('class="label missing"', html)

    def test_missing_label_placeholder(self) -> None:
        detail = {
            "name": "S",
            "orders": [
                {
                    "posting_number": "PN-9",
                    "offer_id": "X",
                    "product_name": "Y",
                    "status": "awaiting_deliver",
                    "tab": "awaiting_deliver",
                }
            ],
        }
        html = render_stickers_print_html(
            detail, {}, include_cover_and_separators=True
        )
        self.assertIn("Нет этикетки", html)
        self.assertIn("PN-9", html)


if __name__ == "__main__":
    unittest.main()
