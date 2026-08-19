# -*- coding: utf-8 -*-
import unittest

from app.wb import friendly_sync_error, is_marketplace_scope_error, normalize_api_key


class NormalizeApiKeyTests(unittest.TestCase):
    def test_strips_bearer_and_quotes(self):
        self.assertEqual(normalize_api_key('  Bearer abc.def  '), "abc.def")
        self.assertEqual(normalize_api_key('"token-value"'), "token-value")


class FriendlySyncErrorTests(unittest.TestCase):
    def test_http401_shows_detail(self):
        err = RuntimeError(
            'WB FBS HTTP 401: {"detail":"empty Authorization header","origin":"s2s-api-auth-marketplace"}'
        )
        self.assertFalse(is_marketplace_scope_error(err))
        msg = friendly_sync_error("new", err)
        self.assertIn("empty Authorization header", msg)

    def test_timeout_message(self):
        msg = friendly_sync_error("orders", TimeoutError("timed out"))
        self.assertIn("таймаут", msg.lower())


if __name__ == "__main__":
    unittest.main()
