# -*- coding: utf-8 -*-
"""Portable data/ dir + legacy AppData migrate (no delete)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import paths


class PortablePathsTest(unittest.TestCase):
    def setUp(self) -> None:
        paths.reset_migrate_state_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.program = self.root / "program"
        self.legacy = self.root / "legacy"
        self.portable = self.program / "data"
        self.program.mkdir(parents=True)
        self.legacy.mkdir(parents=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "FEEDPILOT_PROGRAM_DIR": str(self.program),
                "FEEDPILOT_LEGACY_DATA_DIR": str(self.legacy),
                "FEEDPILOT_DATA_DIR": str(self.portable),
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        paths.reset_migrate_state_for_tests()
        self._tmp.cleanup()

    def _write_legacy_db(self, body: bytes = b"LEGACY-DB") -> Path:
        db = self.legacy / paths.DB_FILENAME
        db.write_bytes(body)
        return db

    def test_migrate_copies_db_and_keeps_legacy(self) -> None:
        self._write_legacy_db(b"KEEP-ME")
        photos = self.legacy / paths.PHOTOS_DIRNAME
        photos.mkdir()
        (photos / "a.jpg").write_bytes(b"img")
        (self.legacy / (paths.DB_FILENAME + "-wal")).write_bytes(b"wal")

        copied = paths.migrate_legacy_to_portable()
        self.assertTrue(copied)
        dest = self.portable / paths.DB_FILENAME
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), b"KEEP-ME")
        self.assertTrue((self.portable / (paths.DB_FILENAME + "-wal")).is_file())
        self.assertTrue((self.portable / paths.PHOTOS_DIRNAME / "a.jpg").is_file())
        # Legacy untouched
        self.assertEqual(
            (self.legacy / paths.DB_FILENAME).read_bytes(), b"KEEP-ME"
        )
        self.assertTrue((self.legacy / paths.PHOTOS_DIRNAME / "a.jpg").is_file())

    def test_migrate_skips_when_portable_db_exists(self) -> None:
        self._write_legacy_db(b"LEGACY")
        self.portable.mkdir(parents=True)
        (self.portable / paths.DB_FILENAME).write_bytes(b"PORTABLE")
        copied = paths.migrate_legacy_to_portable()
        self.assertFalse(copied)
        self.assertEqual(
            (self.portable / paths.DB_FILENAME).read_bytes(), b"PORTABLE"
        )

    def test_deleting_portable_re_migrates_from_legacy(self) -> None:
        self._write_legacy_db(b"AGAIN")
        self.assertTrue(paths.migrate_legacy_to_portable())
        # Simulate user deleting data/
        for child in self.portable.rglob("*"):
            if child.is_file():
                child.unlink()
        for child in sorted(self.portable.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
        if self.portable.exists():
            self.portable.rmdir()
        paths.reset_migrate_state_for_tests()
        root = paths.app_data_dir()
        self.assertEqual(root, self.portable.resolve())
        self.assertEqual(
            (self.portable / paths.DB_FILENAME).read_bytes(), b"AGAIN"
        )
        self.assertTrue((self.legacy / paths.DB_FILENAME).is_file())

    def test_app_data_dir_creates_empty_when_no_legacy(self) -> None:
        root = paths.app_data_dir()
        self.assertTrue(root.is_dir())
        self.assertEqual(root, self.portable.resolve())
        self.assertFalse((root / paths.DB_FILENAME).exists())

    def test_db_path_and_photos_under_portable(self) -> None:
        self._write_legacy_db()
        self.assertEqual(paths.db_path(), self.portable.resolve() / paths.DB_FILENAME)
        photos = paths.photos_dir()
        self.assertEqual(photos, self.portable.resolve() / paths.PHOTOS_DIRNAME)
        self.assertTrue(photos.is_dir())

    def test_photos_merge_does_not_overwrite_existing(self) -> None:
        self._write_legacy_db()
        leg_photos = self.legacy / paths.PHOTOS_DIRNAME
        leg_photos.mkdir()
        (leg_photos / "shared.jpg").write_bytes(b"LEGACY")
        (leg_photos / "only_legacy.jpg").write_bytes(b"L")
        self.portable.mkdir(parents=True)
        # No portable DB yet → migrate will run; seed portable photo first
        # via direct copy_photos path after creating portable DB skip case:
        # Put portable DB missing, but photos dir with existing file, then migrate.
        port_photos = self.portable / paths.PHOTOS_DIRNAME
        port_photos.mkdir(parents=True)
        (port_photos / "shared.jpg").write_bytes(b"PORTABLE")
        paths.migrate_legacy_to_portable()
        self.assertEqual(
            (port_photos / "shared.jpg").read_bytes(), b"PORTABLE"
        )
        self.assertEqual(
            (port_photos / "only_legacy.jpg").read_bytes(), b"L"
        )


if __name__ == "__main__":
    unittest.main()
