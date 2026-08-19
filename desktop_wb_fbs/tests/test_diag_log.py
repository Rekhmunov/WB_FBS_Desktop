# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.diag_log import init, log_file_path, write


class DiagLogTests(unittest.TestCase):
    def test_writes_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.paths.logs_dir", return_value=root):
                import app.diag_log as diag_mod

                diag_mod._initialized = False
                diag_mod._file = None
                diag_mod._log_path = None
                path = init()
                write("test.event", foo="bar", sync=True)
                self.assertEqual(path, log_file_path())
                lines = path.read_text(encoding="utf-8").strip().splitlines()
                self.assertGreaterEqual(len(lines), 2)
                last = json.loads(lines[-1])
                self.assertEqual(last["event"], "test.event")
                self.assertEqual(last["foo"], "bar")


if __name__ == "__main__":
    unittest.main()
