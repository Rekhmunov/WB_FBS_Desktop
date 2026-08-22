# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from app.ozon import (
    TAB_ASSEMBLY,
    TAB_CANCELLED,
    TAB_FINISHED,
    TAB_NEW,
    carriage_is_done,
    compute_tab,
)


class OzonFbsHelpersTest(unittest.TestCase):
    def test_compute_tab_new_packaging(self) -> None:
        self.assertEqual(compute_tab(status="awaiting_packaging"), TAB_NEW)

    def test_compute_tab_assembly_deliver(self) -> None:
        self.assertEqual(compute_tab(status="awaiting_deliver"), TAB_ASSEMBLY)

    def test_compute_tab_carriage_forces_assembly(self) -> None:
        self.assertEqual(
            compute_tab(status="awaiting_packaging", carriage_id="123"),
            TAB_ASSEMBLY,
        )

    def test_compute_tab_cancelled(self) -> None:
        self.assertEqual(compute_tab(status="cancelled"), TAB_CANCELLED)

    def test_compute_tab_finished(self) -> None:
        self.assertEqual(compute_tab(status="delivered"), TAB_FINISHED)

    def test_compute_tab_client_arbitration(self) -> None:
        self.assertEqual(compute_tab(status="client_arbitration"), TAB_ASSEMBLY)

    def test_carriage_is_done(self) -> None:
        self.assertTrue(carriage_is_done("approved"))
        self.assertTrue(carriage_is_done("cancelled"))
        self.assertFalse(carriage_is_done("new"))
        self.assertFalse(carriage_is_done("formed"))
        self.assertFalse(carriage_is_done("confirmed"))


class OzonCarriageClientParseTest(unittest.TestCase):
    def test_v2_delivery_list_shape(self) -> None:
        """v2/carriage/delivery/list returns methods[], not result[]."""
        payload = {
            "methods": [
                {
                    "delivery_method_id": 42,
                    "carriages": [{"id": 99, "status": "new", "postings_count": 3}],
                }
            ],
            "cursor": "next",
            "has_next": False,
        }
        methods = payload.get("methods") or []
        self.assertEqual(len(methods), 1)
        carriage = methods[0]["carriages"][0]
        self.assertEqual(str(carriage.get("id")), "99")
        self.assertEqual(carriage.get("postings_count"), 3)


if __name__ == "__main__":
    unittest.main()
