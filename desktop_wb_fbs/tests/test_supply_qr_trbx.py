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
