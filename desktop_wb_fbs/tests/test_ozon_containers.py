# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.ozon.client import OzonFbsClient
from app.services.ozon_containers import OzonContainersService


class OzonContainerClientBodiesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = OzonFbsClient("cid", "key")

    def test_create_body_shape(self) -> None:
        captured = {}

        def fake_request(method, path, body=None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            return {"container_ids": [101, 102]}

        with patch.object(self.client, "_request", side_effect=fake_request):
            data = self.client.carriage_container_create(
                warehouse_id=555,
                containers_count=2,
                sort_type="sort",
                cargo_type="pallet",
            )
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/v1/carriage/container/create")
        self.assertEqual(
            captured["body"],
            {
                "warehouse_id": 555,
                "containers_count": 2,
                "sort_type": "sort",
                "cargo_type": "pallet",
            },
        )
        self.assertEqual(data.get("container_ids"), [101, 102])

    def test_list_body_shape(self) -> None:
        captured = {}

        def fake_request(method, path, body=None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            return {"containers": [], "cursor": ""}

        body = {
            "filter": {
                "created_from": "2026-01-01T00:00:00Z",
                "created_to": "2026-01-31T00:00:00Z",
                "warehouse_id": 777,
            },
            "sort_dir": 1,
            "limit": 100,
        }
        with patch.object(self.client, "_request", side_effect=fake_request):
            self.client.carriage_container_list(body)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/v1/carriage/container/list")
        self.assertEqual(captured["body"], body)
        self.assertEqual(captured["body"]["filter"]["warehouse_id"], 777)

    def test_fill_body_shape(self) -> None:
        captured = {}

        def fake_request(method, path, body=None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            return {"task_id": 9}

        with patch.object(self.client, "_request", side_effect=fake_request):
            data = self.client.carriage_container_fill(
                container_id=42, posting_numbers=["PN-1", " PN-2 ", ""]
            )
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/v1/carriage/container/fill")
        self.assertEqual(
            captured["body"],
            {"container_id": 42, "posting_numbers": ["PN-1", "PN-2"]},
        )
        self.assertEqual(data.get("task_id"), 9)

    def test_cancel_approve_label_get_task_bodies(self) -> None:
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path, body))
            return {}

        with patch.object(self.client, "_request", side_effect=fake_request):
            self.client.carriage_container_cancel(container_ids=[1, 2])
            self.client.carriage_container_approve(container_ids=[3])
            self.client.carriage_container_label_get(container_ids=[4, 5])
            self.client.carriage_container_task_info(task_id=88)
            self.client.carriage_container_get(container_id=9)
            self.client.carriage_container_remove_postings(
                container_id=9, posting_numbers=["PN-X"]
            )

        paths = [c[1] for c in calls]
        self.assertEqual(
            paths,
            [
                "/v1/carriage/container/cancel",
                "/v1/carriage/container/approve",
                "/v1/carriage/container/label/get",
                "/v1/carriage/container/task/info",
                "/v1/carriage/container/get",
                "/v1/carriage/container/remove-postings",
            ],
        )
        self.assertEqual(calls[0][2], {"container_ids": [1, 2]})
        self.assertEqual(calls[1][2], {"container_ids": [3]})
        self.assertEqual(calls[2][2], {"container_ids": [4, 5]})
        self.assertEqual(calls[3][2], {"task_id": 88})
        self.assertEqual(calls[4][2], {"container_id": 9})
        self.assertEqual(
            calls[5][2], {"container_id": 9, "posting_numbers": ["PN-X"]}
        )


class OzonContainersServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = OzonFbsClient("cid", "key")
        self.svc = OzonContainersService()

    def test_service_create_uses_warehouse_and_amount(self) -> None:
        captured = {}

        def fake_request(method, path, body=None):
            captured["path"] = path
            captured["body"] = body
            return {"container_ids": [11]}

        with patch.object(self.client, "_request", side_effect=fake_request):
            with patch.object(
                self.client, "get_carriage", return_value={"warehouse_id": 900}
            ):
                result = self.svc.create(
                    self.client, 12345, 3, container_type="box"
                )
        self.assertEqual(captured["path"], "/v1/carriage/container/create")
        self.assertEqual(captured["body"]["warehouse_id"], 900)
        self.assertEqual(captured["body"]["containers_count"], 3)
        self.assertEqual(captured["body"]["cargo_type"], "box")
        self.assertEqual(result["container_ids"], [11])
        self.assertEqual(result["created"], 1)

    def test_service_list_builds_filter_body(self) -> None:
        captured = {}

        def fake_request(method, path, body=None):
            captured["path"] = path
            captured["body"] = body
            return {
                "containers": [
                    {
                        "container_id": 5,
                        "container_number": 1,
                        "status": "new",
                        "count_of_postings": 2,
                        "cargo_type": "pallet",
                        "sort_type": "sort",
                    }
                ]
            }

        with patch.object(self.client, "_request", side_effect=fake_request):
            result = self.svc.list_containers(
                self.client, 55, warehouse_id=321
            )
        self.assertEqual(captured["path"], "/v1/carriage/container/list")
        self.assertEqual(captured["body"]["filter"]["warehouse_id"], 321)
        self.assertIn("created_from", captured["body"]["filter"])
        self.assertIn("created_to", captured["body"]["filter"])
        self.assertEqual(captured["body"]["limit"], 100)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["container_id"], 5)

    def test_service_bind_fill_body(self) -> None:
        captured = {}

        def fake_request(method, path, body=None):
            captured["path"] = path
            captured["body"] = body
            return {"task_id": 7}

        with patch.object(self.client, "_request", side_effect=fake_request):
            result = self.svc.bind_posting(
                self.client, 100, 42, "PN-99"
            )
        self.assertEqual(captured["path"], "/v1/carriage/container/fill")
        self.assertEqual(
            captured["body"],
            {"container_id": 42, "posting_numbers": ["PN-99"]},
        )
        self.assertEqual(result["task_id"], 7)


class OzonContainerArchiveReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from app.db import Database

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = OzonContainersService(self.db)
        self.client = OzonFbsClient("cid", "key")
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO ozon_fbs_postings(
                    source_id, posting_number, status, tab, synced_at
                ) VALUES (1, 'PN-1', 'awaiting_deliver', 'awaiting_deliver', 't')
                """
            )
            conn.execute(
                """
                INSERT INTO ozon_fbs_postings(
                    source_id, posting_number, status, tab, synced_at,
                    container_id, container_synced
                ) VALUES (1, 'PN-2', 'awaiting_deliver', 'awaiting_deliver', 't',
                          77, 1)
                """
            )
            conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_archive_filter(self) -> None:
        items = [
            {"container_id": 10, "status": "new", "status_label": "Новое"},
            {"container_id": 20, "status": "new", "status_label": "Новое"},
        ]
        self.svc.archive_locally(1, 10)
        filtered = self.svc.apply_local_archive_filter(1, items, include_archived=False)
        self.assertEqual([r["container_id"] for r in filtered], [20])
        shown = self.svc.apply_local_archive_filter(1, items, include_archived=True)
        self.assertEqual(len(shown), 2)
        self.assertTrue(shown[0]["locally_archived"])
        self.svc.restore_locally(1, 10)
        restored = self.svc.apply_local_archive_filter(1, items, include_archived=False)
        self.assertEqual(len(restored), 2)

    def test_reconcile_adopts_portal_bind(self) -> None:
        def fake_list(_client, _cid, **_kw):
            return {
                "ok": True,
                "warehouse_id": 1,
                "items": [{"container_id": 55, "status": "new"}],
                "total": 1,
            }

        def fake_fetch(_client, cid):
            if int(cid) == 55:
                return (
                    {"container_id": 55, "status": "new"},
                    ["PN-1"],
                    True,
                )
            return {"container_id": cid}, [], True

        with patch.object(self.svc, "list_containers", side_effect=fake_list):
            with patch.object(
                self.svc, "fetch_container_postings", side_effect=fake_fetch
            ):
                result = self.svc.reconcile_binds(
                    self.client, 1, ["PN-1", "PN-2"], warehouse_id=1
                )
        self.assertTrue(result["ok"])
        binds = self.svc.load_bind_map(1, ["PN-1", "PN-2"])
        self.assertEqual(binds["PN-1"]["container_id"], 55)
        self.assertTrue(binds["PN-1"]["container_synced"])


if __name__ == "__main__":
    unittest.main()
