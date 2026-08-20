# -*- coding: utf-8 -*-
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.sticker_file_cache import (
    existing_sticker_paths,
    persist_sticker_png,
    read_sticker_b64,
    sticker_img_src,
    supply_sticker_dir,
)


class StickerFileCacheTests(unittest.TestCase):
    def test_persist_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "app.services.sticker_file_cache.app_data_dir", return_value=root
            ):
                with patch(
                    "app.services.sticker_file_cache.supply_sticker_dir",
                    return_value=root / "stickers",
                ):
                    (root / "stickers").mkdir(parents=True)
                    payload = base64.b64encode(b"png-bytes").decode("ascii")
                    path = persist_sticker_png("key", "WB-GI-1", 42, payload)
                    self.assertTrue(Path(path).is_file())
                    b64 = read_sticker_b64({"file_path": path})
                    self.assertEqual(b64, payload)

    def test_read_inline_b64(self):
        self.assertEqual(read_sticker_b64({"file_b64": "abc"}), "abc")

    def test_sticker_img_src_prefers_file_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1.png"
            path.write_bytes(b"png")
            src = sticker_img_src({"file_path": str(path), "file_b64": "ignored"})
            self.assertTrue(src.startswith("file:"))
            self.assertIn("1.png", src)

    def test_sticker_img_src_falls_back_to_data_uri(self):
        src = sticker_img_src({"file_b64": "abc123"})
        self.assertEqual(src, "data:image/png;base64,abc123")

    def test_existing_sticker_paths_skips_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "stickers"
            root.mkdir(parents=True)
            (root / "10.png").write_bytes(b"x")
            (root / "11.png").write_bytes(b"")
            with patch(
                "app.services.sticker_file_cache.supply_sticker_dir",
                return_value=root,
            ):
                found = existing_sticker_paths("key", "WB-GI-1", [10, 11, 12])
            self.assertEqual(list(found.keys()), [10])
            self.assertTrue(found[10].endswith("10.png"))


if __name__ == "__main__":
    unittest.main()
