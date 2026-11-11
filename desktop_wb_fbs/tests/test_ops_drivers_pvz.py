# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.services.ozon_pvz_returns import (
    OzonPvzReturnsService,
    extract_return_fields,
)
from app.services.supply_drivers import SupplyDriversService
from app.services.ttn_draft import TtnDraftService


class DriversCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = SupplyDriversService(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_upsert_assign_clear(self) -> None:
        row = self.svc.upsert(
            full_name="Иванов Иван",
            phone="+7900",
            vehicles=[{"number": "A111AA777", "type": "газель"}],
        )
        did = int(row["id"])
        self.assertGreater(did, 0)
        opts = self.svc.options()
        self.assertEqual(len(opts), 1)
        assigned = self.svc.set_assignment("ozon", 1, "OZ-1", did)
        self.assertTrue(assigned["has_driver"])
        self.assertEqual(assigned["vehicle_number"], "A111AA777")
        cleared = self.svc.clear_assignment("ozon", 1, "OZ-1")
        self.assertFalse(cleared["has_driver"])


class TtnDraftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = TtnDraftService(self.db)
        SupplyDriversService(self.db).upsert(
            full_name="Петров Пётр",
            vehicles=[{"number": "B222BB199", "type": ""}],
        )
        did = SupplyDriversService(self.db).options()[0]["id"]
        SupplyDriversService(self.db).set_assignment("wb", 2, "WB-GI-1", did)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prefill_and_save(self) -> None:
        pre = self.svc.build_prefill(
            "wb", 2, "WB-GI-1", supply_name="Поставка 1", cargo_places="3"
        )
        draft = pre["draft"]
        self.assertEqual(draft["driver_name"], "Петров Пётр")
        draft["notes"] = "тест"
        saved = self.svc.save(draft)
        self.assertTrue(saved["ok"])
        again = self.svc.get("wb", 2, "WB-GI-1")
        self.assertEqual(again["notes"], "тест")


class PvzReturnsTest(unittest.TestCase):
    def test_extract_fields(self) -> None:
        item = {
            "id": 99,
            "posting_number": "PN-1",
            "visual": {
                "status": {"sys_name": "ReceivedBySeller"},
                "change_moment": "2026-11-10T12:00:00Z",
            },
            "product": {"offer_id": "ART", "name": "Товар", "quantity": 2},
            "logistic": {"barcode": "460"},
        }
        fields = extract_return_fields(item)
        self.assertIsNotNone(fields)
        self.assertEqual(fields["return_id"], 99)
        self.assertEqual(fields["offer_id"], "ART")

    def test_sync_and_list(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Database(Path(tmp.name) / "t.db")
            db.init_schema()
            svc = OzonPvzReturnsService(db)
            client = OzonFbsClient("1", "k")

            def fake_returns(**_kw):
                return {
                    "returns": [
                        {
                            "id": 7,
                            "posting_number": "PN-7",
                            "visual": {
                                "status": {"sys_name": "ReceivedBySeller"},
                                "change_moment": "2026-11-11T10:00:00+03:00",
                            },
                            "product": {
                                "offer_id": "X",
                                "name": "Товар",
                                "quantity": 1,
                            },
                        }
                    ],
                    "last_id": 7,
                }

            with patch.object(client, "returns_list", side_effect=fake_returns):
                result = svc.sync_source(client, 1)
            self.assertGreaterEqual(int(result["saved"]), 1)
            listed = svc.list_local(1, search="PN-7")
            self.assertEqual(listed["total"], 1)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
