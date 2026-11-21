# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.db import Database
from app.services.ozon_labels import (
    OzonLabelService,
    format_label_error,
    package_label_task_is_ready,
    parse_package_label_create_tasks,
    parse_package_label_get_result,
    select_small_label_task,
)


class ParseHelpersTest(unittest.TestCase):
    def test_select_small_label(self) -> None:
        tasks = parse_package_label_create_tasks(
            {
                "tasks": [
                    {"task_id": 1, "task_type": "big_label"},
                    {"task_id": 2, "task_type": "small_label"},
                ]
            }
        )
        self.assertEqual(select_small_label_task(tasks)["task_id"], 2)

    def test_nested_status_code(self) -> None:
        parsed = parse_package_label_get_result(
            {
                "file_url": "https://ir.ozone.ru/x.pdf",
                "status": {"code": "completed"},
                "error": None,
            }
        )
        self.assertEqual(parsed["status_code"], "completed")
        self.assertTrue(package_label_task_is_ready(parsed))


class FetchLabelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = OzonLabelService(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_v3_only_no_obsolete_fallback(self) -> None:
        client = MagicMock()
        client.package_label_create_v3.return_value = [
            {"task_id": 10, "task_type": "big_label"},
            {"task_id": 11, "task_type": "small_label"},
        ]
        client.package_label_get_v2.return_value = {
            "file_url": "https://example/x.pdf",
            "status": {"code": "completed"},
        }
        client.fetch_url_bytes.return_value = b"%PDF-1.4 mock"
        path = self.svc.fetch_labels(
            client, 1, ["A-1", "B-2"], use_cache=False
        )
        self.assertTrue(path.endswith(".pdf"))
        client.package_label_create_v3.assert_called_once()
        client.package_label_get_v2.assert_called_with(11)
        client.package_label_fetch.assert_not_called()
        client.package_label_create.assert_not_called()

    def test_batches_of_200(self) -> None:
        client = MagicMock()
        client.package_label_create_v3.return_value = [
            {"task_id": 1, "task_type": "small_label"}
        ]
        client.package_label_get_v2.return_value = {
            "file_url": "https://example/x.pdf",
            "status": {"code": "completed"},
        }
        client.fetch_url_bytes.return_value = b"%PDF-1.4 mock"
        nums = ["P-{}".format(i) for i in range(450)]
        messages = []  # type: list
        paths = self.svc.fetch_label_paths(
            client,
            1,
            nums,
            use_cache=False,
            progress=messages.append,
        )
        self.assertEqual(len(paths), 3)
        self.assertEqual(client.package_label_create_v3.call_count, 3)
        for call in client.package_label_create_v3.call_args_list:
            self.assertLessEqual(len(call[0][0]), 200)
        self.assertTrue(any("партия" in str(m) for m in messages))

    def test_cache_returns_all_batch_parts(self) -> None:
        client = MagicMock()
        client.package_label_create_v3.return_value = [
            {"task_id": 1, "task_type": "small_label"}
        ]
        client.package_label_get_v2.return_value = {
            "file_url": "https://example/x.pdf",
            "status": {"code": "completed"},
        }
        client.fetch_url_bytes.return_value = b"%PDF-1.4 mock"
        nums = ["P-{}".format(i) for i in range(450)]
        first = self.svc.fetch_label_paths(client, 1, nums, use_cache=True)
        self.assertEqual(len(first), 3)
        client.reset_mock()
        client.package_label_create_v3.return_value = [
            {"task_id": 1, "task_type": "small_label"}
        ]
        client.package_label_get_v2.return_value = {
            "file_url": "https://example/x.pdf",
            "status": {"code": "completed"},
        }
        client.fetch_url_bytes.return_value = b"%PDF-1.4 mock"
        cached = self.svc.fetch_label_paths(client, 1, nums, use_cache=True)
        self.assertEqual(len(cached), 3)
        self.assertEqual(cached, first)
        client.package_label_create_v3.assert_not_called()

    def test_format_label_error_obsolete(self) -> None:
        msg = format_label_error(RuntimeError("API method is obsolete"))
        self.assertIn("устарел", msg)
        self.assertIn("v3", msg)

    def test_format_label_error_invalid_argument(self) -> None:
        msg = format_label_error(RuntimeError("INVALID_ARGUMENT: bad status"))
        self.assertIn("статус", msg.casefold())


if __name__ == "__main__":
    unittest.main()
