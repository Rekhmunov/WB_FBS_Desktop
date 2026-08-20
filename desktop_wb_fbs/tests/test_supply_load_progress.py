# -*- coding: utf-8 -*-
"""Supply open progress must stay enabled after worker restart."""
from __future__ import annotations

import unittest


class SupplyLoadProgressFlagTests(unittest.TestCase):
    def test_stop_load_worker_does_not_clear_loading_flag(self) -> None:
        import inspect

        from app.ui import supply_detail as mod

        src = inspect.getsource(mod.SupplyDetailDialog._stop_load_worker)
        self.assertNotIn("self._loading = False", src)
        begin = inspect.getsource(mod.SupplyDetailDialog._begin_load)
        stop_at = begin.find("self._stop_load_worker()")
        loading_at = begin.find("self._loading = True")
        self.assertGreater(stop_at, 0)
        self.assertGreater(loading_at, stop_at)


if __name__ == "__main__":
    unittest.main()
