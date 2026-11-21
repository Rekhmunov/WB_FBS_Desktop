# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import runtime_deps as rd


class MissingSoftDepsTest(unittest.TestCase):
    def test_empty_when_all_present(self) -> None:
        with patch.object(rd, "_can_import", return_value=True):
            self.assertEqual(rd.missing_soft_deps(), [])

    def test_lists_missing_only(self) -> None:
        def _can(name: str) -> bool:
            return name != "pymupdf"

        with patch.object(rd, "_can_import", side_effect=_can):
            missing = rd.missing_soft_deps(
                [("pymupdf", "pymupdf>=1.24.0"), ("certifi", "certifi>=2024.2.2")]
            )
        self.assertEqual(missing, [("pymupdf", "pymupdf>=1.24.0")])


class EnsureRuntimeDepsTest(unittest.TestCase):
    def test_noop_when_nothing_missing(self) -> None:
        with patch.object(rd, "missing_soft_deps", return_value=[]):
            with patch.object(rd, "pip_install") as install:
                self.assertEqual(rd.ensure_runtime_deps(), [])
                install.assert_not_called()

    def test_installs_missing_once(self) -> None:
        status_msgs = []
        with patch.object(
            rd, "missing_soft_deps", return_value=[("pymupdf", "pymupdf>=1.24.0")]
        ):
            with patch.object(rd, "pip_install") as install:
                with patch.object(rd, "_can_import", return_value=True):
                    got = rd.ensure_runtime_deps(status=status_msgs.append)
        install.assert_called_once_with("pymupdf>=1.24.0")
        self.assertEqual(got, ["pymupdf"])
        self.assertTrue(any("pymupdf" in m for m in status_msgs))


if __name__ == "__main__":
    unittest.main()
