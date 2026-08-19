# -*- coding: utf-8 -*-
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.sticker_file_cache import (
    persist_sticker_png,
    read_sticker_b64,
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


if __name__ == "__main__":
    unittest.main()
