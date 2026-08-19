# -*- coding: utf-8 -*-
"""TLS helpers for WB HTTPS on Windows (esp. Python without system CA store)."""
from __future__ import annotations

import logging
import ssl
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_log = logging.getLogger(__name__)
_SSL_CONTEXT = None  # type: Optional[ssl.SSLContext]


def ssl_context() -> ssl.SSLContext:
    """Prefer certifi CA bundle; fall back to system defaults."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    try:
        import certifi  # type: ignore

        ctx = ssl.create_default_context(cafile=certifi.where())
        _log.debug("TLS: using certifi CA bundle %s", certifi.where())
    except Exception as exc:
        ctx = ssl.create_default_context()
        _log.debug("TLS: system CA store (%s)", exc)
    _SSL_CONTEXT = ctx
    return ctx


def urlopen_https(req: Request, timeout: int = 30) -> Any:
    """urlopen with explicit SSL context + clearer SSL/network errors."""
    try:
        return urlopen(req, timeout=timeout, context=ssl_context())
    except HTTPError:
        raise
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        msg = str(reason or exc)
        lower = msg.lower()
        if (
            "certificate verify failed" in lower
            or "ssl" in lower
            or "tls" in lower
            or isinstance(reason, ssl.SSLError)
        ):
            raise RuntimeError(
                "SSL при подключении к WB API: {}. "
                "Обновите сертификаты (pip install -U certifi) или проверьте "
                "антивирус/прокси, перехватывающий HTTPS.".format(msg)
            ) from exc
        if "timed out" in lower or "timeout" in lower:
            raise RuntimeError("Таймаут подключения к WB API") from exc
        raise RuntimeError("Нет связи с WB API: {}".format(msg)) from exc
    except ssl.SSLError as exc:
        raise RuntimeError(
            "SSL при подключении к WB API: {}. "
            "Обновите сертификаты (pip install -U certifi) или проверьте "
            "антивирус/прокси, перехватывающий HTTPS.".format(exc)
        ) from exc
