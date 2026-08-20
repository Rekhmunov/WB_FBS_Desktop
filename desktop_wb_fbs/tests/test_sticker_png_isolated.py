# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services import sticker_png_isolated


class PersistChunkWorkerTests(unittest.TestCase):
    @patch("app.wb.client.WbFbsClient")
    @patch("app.services.sticker_file_cache.persist_sticker_png")
    def test_worker_writes_disk_meta_without_base64(self, persist_png, client_cls):
        client = client_cls.return_value
        client.get_order_stickers.return_value = [
            {
                "orderId": 11,
                "partA": "A",
                "partB": "B",
                "file": "aGVsbG8=",
            }
        ]
        persist_png.return_value = "/tmp/11.png"

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "out.json"
            sticker_png_isolated._persist_chunk_worker(
                "key", "WB-GI-1", [11], str(out_path)
            )
            data = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertTrue(data["ok"])
        self.assertEqual(data["stickers"]["11"]["file_path"], "/tmp/11.png")
        self.assertEqual(data["stickers"]["11"]["file_b64"], "")
        self.assertEqual(data["stickers"]["11"]["partB"], "B")
        persist_png.assert_called_once()


class FetchPngChunkIsolatedTests(unittest.TestCase):
    @patch("app.services.sticker_png_isolated.subprocess.run")
    def test_parent_reads_child_result(self, run_mock):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            def _fake_run(cmd, **kwargs):
                code = cmd[2]
                # out path is the second arg to run_job_file('job', 'out')
                marker = "run_job_file("
                start = code.index(marker) + len(marker)
                # job!r, out!r
                parts = code[start:].split(",", 1)
                out_literal = parts[1].strip().rstrip(")")
                out_file = Path(eval(out_literal, {}, {}))
                out_file.write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "stickers": {
                                "7": {
                                    "partA": "P",
                                    "partB": "Q",
                                    "file_b64": "",
                                    "file_path": "/x/7.png",
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                return MagicMock(returncode=0)

            run_mock.side_effect = _fake_run

            with patch("app.paths.app_data_dir", return_value=tmp_path), patch(
                "app.diag_log.write"
            ):
                out = sticker_png_isolated.fetch_png_chunk_isolated(
                    "key", "WB-1", [7]
                )

        self.assertEqual(out[7]["file_path"], "/x/7.png")
        self.assertEqual(out[7]["file_b64"], "")
        run_mock.assert_called_once()

    @patch("app.services.sticker_png_isolated.fetch_png_chunk_isolated")
    def test_retries_singles_after_empty_chunk(self, chunk_mock):
        chunk_mock.side_effect = [
            {},  # first multi-id chunk fails
            {1: {"partA": "", "partB": "", "file_b64": "", "file_path": "/1.png"}},
            {2: {"partA": "", "partB": "", "file_b64": "", "file_path": "/2.png"}},
        ]
        out = sticker_png_isolated.fetch_png_ids_isolated(
            "key", "WB-1", [1, 2], chunk_size=5
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(chunk_mock.call_count, 3)


class FetchStickersIsolatedPathTests(unittest.TestCase):
    @patch("app.services.sticker_png_isolated.fetch_png_ids_isolated")
    def test_persist_disk_uses_isolated_fetch(self, isolated_mock):
        from app.services import print_docs

        isolated_mock.return_value = {
            1: {
                "partA": "A",
                "partB": "B",
                "file_b64": "",
                "file_path": "/tmp/1.png",
            }
        }
        print_docs._stickers_cache.clear()
        print_docs.fetch_stickers_map(
            "secret-key",
            [1],
            cache_only=True,
            persist_supply_id="WB-GI-1",
        )
        isolated_mock.assert_called_once()
        cached = print_docs.get_cached_stickers_map("secret-key", [1])
        self.assertEqual(cached[1]["file_path"], "/tmp/1.png")


if __name__ == "__main__":
    unittest.main()
