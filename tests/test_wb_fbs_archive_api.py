"""WB FBS archive API: year/month windows match official Marketplace docs."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from review_processor.wb_fbs import WbFbsClient, archive_month_windows


class ArchiveMonthWindowsTests(unittest.TestCase):
    def test_newest_first_and_year_rollover(self):
        now = datetime(2026, 2, 10, tzinfo=timezone.utc)
        windows = archive_month_windows(months_back=4, now=now)
        self.assertEqual(
            windows,
            [(2026, 2), (2026, 1), (2025, 12), (2025, 11)],
        )


class GetArchiveOrdersTests(unittest.TestCase):
    def test_sends_official_year_month_params(self):
        client = WbFbsClient(api_key="token")
        with patch.object(client, "_request") as req:
            req.return_value = {"orders": [{"id": 1}], "next": None}
            orders, nxt = client.get_archive_orders(
                year=2026, month=8, limit=100, next_token=0
            )
        self.assertEqual(orders, [{"id": 1}])
        self.assertIsNone(nxt)
        req.assert_called_once_with(
            "GET",
            "/api/marketplace/v3/fbs/orders/archive",
            params={"year": 2026, "month": 8, "limit": 100, "next": 0},
        )

    def test_rejects_invalid_month(self):
        client = WbFbsClient(api_key="token")
        with self.assertRaises(ValueError):
            client.get_archive_orders(year=2026, month=13)

    def test_iter_archive_pages_walks_months(self):
        client = WbFbsClient(api_key="token")
        responses = [
            ([{"id": 1}], None),
            ([{"id": 2}], None),
        ]

        def fake_get(*, year, month, limit=1000, next_token=0):
            return responses.pop(0)

        with patch.object(client, "get_archive_orders", side_effect=fake_get):
            with patch(
                "review_processor.wb_fbs.archive_month_windows",
                return_value=[(2026, 8), (2026, 7)],
            ):
                pages = list(client.iter_archive_pages(months_back=2, max_pages=2))
        self.assertEqual(pages, [[{"id": 1}], [{"id": 2}]])


if __name__ == "__main__":
    unittest.main()
