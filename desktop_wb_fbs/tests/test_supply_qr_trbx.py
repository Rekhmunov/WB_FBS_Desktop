# -*- coding: utf-8 -*-
import sys
import unittest
from unittest.mock import MagicMock, patch

from PyQt5.QtWidgets import QApplication

from app.services.supply_qr import render_supply_qr_sticker_png, supply_qr_payload

_APP = QApplication.instance() or QApplication(sys.argv)


class SupplyQrTests(unittest.TestCase):
    def test_payload_strips(self):
        self.assertEqual(supply_qr_payload("  WB-GI-1  "), "WB-GI-1")

    def test_renders_png_bytes(self):
        raw = render_supply_qr_sticker_png(
            "WB-GI-266846035",
            order_count=12,
            city="Москва",
        )
        self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertGreater(len(raw), 500)

    def test_empty_id_raises(self):
        with self.assertRaises(ValueError):
            render_supply_qr_sticker_png("")

    def test_resolve_prefers_wb_barcode(self):
        from app.services.supply_qr import resolve_supply_qr_png

        png = b"\x89PNG\r\n\x1a\n" + b"x" * 300
        with patch(
            "app.services.supply_qr.fetch_supply_barcode_png", return_value=png
        ):
            out, source = resolve_supply_qr_png("token", "WB-GI-1", order_count=2)
        self.assertEqual(source, "wb")
        self.assertEqual(out, png)

    def test_resolve_falls_back_local(self):
        from app.services.supply_qr import resolve_supply_qr_png

        with patch(
            "app.services.supply_qr.fetch_supply_barcode_png",
            side_effect=RuntimeError("fail"),
        ):
            out, source = resolve_supply_qr_png("token", "WB-GI-1", order_count=1)
        self.assertEqual(source, "local")
        self.assertTrue(out.startswith(b"\x89PNG"))


class TrbxDoneGuardTests(unittest.TestCase):
    @patch("app.services.orders.OrdersService")
    def test_create_blocked_when_done(self, orders_cls):
        from app.services.trbx_stickers import TrbxService

        orders_cls.return_value.get_supply.return_value = {
            "done": True,
            "order_ids": [1],
        }
        svc = TrbxService(MagicMock())
        with self.assertRaises(ValueError) as ctx:
            svc.create(1, "key", "WB-GI-1", 1, order_count=1)
        self.assertIn("закрыта", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
