# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request
import ssl

from app.wb.http import urlopen_https
from app.wb import friendly_sync_error


class UrlopenHttpsTests(unittest.TestCase):
    def test_ssl_error_is_friendly(self):
        req = Request("https://marketplace-api.wildberries.ru/ping")

        def boom(*_a, **_k):
            raise URLError(ssl.SSLError("certificate verify failed"))

        with patch("app.wb.http.urlopen", side_effect=boom):
            with self.assertRaises(RuntimeError) as ctx:
                urlopen_https(req, timeout=5)
        self.assertIn("SSL", str(ctx.exception))
        self.assertIn("certifi", str(ctx.exception).lower())

    def test_friendly_sync_mentions_fix(self):
        msg = friendly_sync_error(
            "проверка ключа",
            RuntimeError("SSL при подключении к WB API: certificate verify failed"),
        )
        self.assertIn("certifi", msg.lower())


if __name__ == "__main__":
    unittest.main()
