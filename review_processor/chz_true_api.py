"""Chestny Znak (ГИС МТ) True API client — auth challenge + documents."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode


PROD_BASE = "https://markirovka.crpt.ru/api/v3/true-api"
DEMO_BASE = "https://markirovka.sandbox.crpt.tech/api/v3/true-api"
# Document info (and some newer methods) moved off v3 — v3 returns HTTP 410.
PROD_BASE_V4 = "https://markirovka.crpt.ru/api/v4/true-api"
DEMO_BASE_V4 = "https://markirovka.sandbox.crpt.tech/api/v4/true-api"


def _parse_true_api_payload(payload: bytes) -> Any:
    """Parse True API response body.

    ``/lk/documents/create`` often returns a bare document UUID as plain text
    (not a JSON string). ``json.loads("123e4567-e89b-...")`` then fails with
    ``Extra data`` because the leading digits are read as a JSON number.
    """
    if not payload:
        return {}
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class ChzTrueApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ChzTrueApiClient:
    def __init__(self, *, base_url: str = "", timeout: int = 45) -> None:
        raw = str(base_url or "").strip().rstrip("/")
        if raw.endswith("/api/v3/true-api"):
            self.base = raw
        elif "sandbox" in raw or "demo" in raw:
            self.base = DEMO_BASE
        elif raw:
            self.base = raw
        else:
            self.base = PROD_BASE
        self.timeout = timeout
        self.token = ""

    def set_token(self, token: str) -> None:
        self.token = str(token or "").strip()

    def v4_base(self) -> str:
        """True API v4 host for methods retired on v3 (e.g. ``/doc/{id}/info``)."""
        base = str(self.base or "").strip().rstrip("/")
        if "/api/v4/true-api" in base:
            return base
        if "/api/v3/true-api" in base:
            return base.replace("/api/v3/true-api", "/api/v4/true-api")
        if "sandbox" in base or "crpt.tech" in base:
            return DEMO_BASE_V4
        return PROD_BASE_V4

    def _url(
        self,
        path: str,
        params: dict[str, object] | None = None,
        *,
        base: str | None = None,
    ) -> str:
        root = str(base or self.base).rstrip("/")
        p = path if path.startswith("/") else f"/{path}"
        qs = ""
        if params:
            qs = "?" + urlencode({k: v for k, v in params.items() if v is not None})
        return f"{root}{p}{qs}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        body: dict[str, object] | list[object] | None = None,
        auth: bool = True,
        base: str | None = None,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": "FeedPilot-CHZ/1.0",
        }
        if auth:
            if not self.token:
                raise ChzTrueApiError("Нет токена True API — сначала авторизуйтесь")
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(
            self._url(path, params, base=base),
            method=method.upper(),
            headers=headers,
            data=data,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
                return _parse_true_api_payload(payload)
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                pass
            raise ChzTrueApiError(
                f"ЧЗ True API HTTP {exc.code}: {err_body or exc.reason}",
                status=int(exc.code),
                body=err_body,
            ) from exc
        except urllib.error.URLError as exc:
            raise ChzTrueApiError(f"ЧЗ True API сеть: {exc.reason}") from exc

    def auth_key(self) -> dict[str, str]:
        data = self._request("GET", "/auth/key", auth=False)
        if not isinstance(data, dict):
            raise ChzTrueApiError("Некорректный ответ /auth/key")
        uuid = str(data.get("uuid") or "").strip()
        challenge = str(data.get("data") or "").strip()
        if not uuid or not challenge:
            raise ChzTrueApiError("В ответе /auth/key нет uuid/data")
        return {"uuid": uuid, "data": challenge}

    def simple_sign_in(
        self,
        *,
        uuid: str,
        signature_b64: str,
        inn: str = "",
        united_token: bool = False,
    ) -> str:
        body: dict[str, object] = {
            "uuid": str(uuid or "").strip(),
            "data": str(signature_b64 or "").strip().replace("\n", "").replace("\r", ""),
        }
        inn_s = str(inn or "").strip()
        if inn_s:
            body["inn"] = inn_s
        if united_token:
            body["unitedToken"] = True
        data = self._request("POST", "/auth/simpleSignIn", body=body, auth=False)
        if not isinstance(data, dict):
            raise ChzTrueApiError("Некорректный ответ /auth/simpleSignIn")
        token = str(data.get("token") or data.get("access_token") or "").strip()
        if not token:
            raise ChzTrueApiError("Токен не получен из /auth/simpleSignIn")
        self.set_token(token)
        return token

    def create_document(
        self,
        *,
        doc_type: str,
        product_group: str,
        product_document: dict[str, Any] | None = None,
        signature_b64: str,
        product_document_b64: str = "",
        document_format: str = "MANUAL",
    ) -> str:
        """Create LK_RECEIPT / LP_RETURN. Returns document id.

        Prefer ``product_document_b64`` — exact bytes that were signed (detached CAdES).
        Re-serializing ``product_document`` can break the signature (e.g. 10.0 → 10).
        """
        b64_raw = str(product_document_b64 or "").strip().replace("\n", "").replace("\r", "")
        if b64_raw:
            try:
                base64.b64decode(b64_raw)
            except Exception as exc:
                raise ChzTrueApiError("Некорректный product_document_b64") from exc
            product_b64 = b64_raw
        else:
            raw = json.dumps(
                product_document or {}, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            product_b64 = base64.b64encode(raw).decode("ascii")
        body = {
            "document_format": document_format,
            "product_document": product_b64,
            "type": str(doc_type or "").strip(),
            "signature": str(signature_b64 or "").strip().replace("\n", "").replace("\r", ""),
        }
        pg = str(product_group or "").strip()
        params: dict[str, object] = {"type": body["type"]}
        if pg:
            params["pg"] = pg
        data = self._request(
            "POST",
            "/lk/documents/create",
            params=params,
            body=body,
            auth=True,
        )
        if isinstance(data, str) and data.strip():
            return data.strip()
        if isinstance(data, dict):
            for key in ("id", "documentId", "document_id", "number"):
                val = data.get(key)
                if val:
                    return str(val)
            # Some stands return bare uuid as JSON string already handled above.
            if len(data) == 1:
                only = next(iter(data.values()))
                if only:
                    return str(only)
        raise ChzTrueApiError(f"Не удалось разобрать id документа: {data!r}")

    def document_info(self, document_id: str) -> dict[str, Any]:
        """Fetch document processing status from True API.

        Uses **v4** ``GET /doc/{id}/info`` — v3 returns HTTP 410
        (``Устаревшее API``), which left Вывод КИЗ rows stuck on «отправлен».

        True API returns a **JSON array** of document cards (see CRPT docs
        example with ``status`` / ``commonErrors``). Older code treated a list
        as ``{"raw": [...]}`` and never saw ``status``, so reconcile left rows
        on «отправлен» forever.
        """
        doc_id = str(document_id or "").strip()
        if not doc_id:
            raise ChzTrueApiError("Пустой document_id")
        data = self._request(
            "GET",
            f"/doc/{doc_id}/info",
            auth=True,
            base=self.v4_base(),
        )
        return _unwrap_doc_info_payload(data, document_id=doc_id)

    def cises_info(
        self,
        codes: list[str],
        *,
        product_group: str = "",
    ) -> list[dict[str, Any]]:
        """Public CIS card(s): status INTRODUCED / RETIRED / … (needs Bearer token).

        True API: ``POST /cises/info?pg=…`` with a JSON **array** of short CISes
        (``01``+GTIN+``21``+serial, no brackets), length 18–74. Wrong ``pg`` often
        yields ``КИ не найден`` even when the code exists in another group.
        """
        cleaned = [str(c or "").strip() for c in codes if str(c or "").strip()]
        if not cleaned:
            return []
        params: dict[str, object] | None = None
        pg = str(product_group or "").strip()
        if pg:
            params = {"pg": pg}
        data = self._request(
            "POST", "/cises/info", params=params, body=cleaned, auth=True
        )
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            # Whole-response auth/contract errors (not per-CIS).
            err = str(
                data.get("error_message")
                or data.get("errorMessage")
                or data.get("message")
                or ""
            ).strip()
            if err and not (
                data.get("result") or data.get("cises") or data.get("data")
            ):
                raise ChzTrueApiError(err)
            rows = data.get("result") or data.get("cises") or data.get("data")
            if isinstance(rows, list):
                return [x for x in rows if isinstance(x, dict)]
        return []


def _unwrap_doc_info_payload(
    data: Any, *, document_id: str = ""
) -> dict[str, Any]:
    """Normalize ``/doc/{id}/info`` payload to a single document dict."""
    doc_id = str(document_id or "").strip()

    def _pick_from_list(rows: list[Any]) -> dict[str, Any] | None:
        dicts = [x for x in rows if isinstance(x, dict)]
        if not dicts:
            return None
        if doc_id:
            for item in dicts:
                num = str(
                    item.get("number")
                    or item.get("id")
                    or item.get("documentId")
                    or item.get("document_id")
                    or ""
                ).strip()
                if num == doc_id:
                    return item
        return dicts[0]

    if isinstance(data, list):
        picked = _pick_from_list(data)
        return picked if picked is not None else {"raw": data}
    if isinstance(data, dict):
        # Already a document card.
        if any(
            k in data
            for k in ("status", "docStatus", "commonErrors", "errors", "number", "type")
        ):
            return data
        # Nested wrappers seen in the wild.
        for key in ("result", "document", "data", "body"):
            nested = data.get(key)
            if isinstance(nested, dict) and (
                "status" in nested or "docStatus" in nested or "number" in nested
            ):
                return nested
            if isinstance(nested, list):
                picked = _pick_from_list(nested)
                if picked is not None:
                    return picked
        raw = data.get("raw")
        if isinstance(raw, list):
            picked = _pick_from_list(raw)
            if picked is not None:
                return picked
        return data
    return {"raw": data}


def build_lk_receipt_document(
    *,
    inn: str,
    action: str = "DISTANCE",
    document_number: str,
    document_date: str,
    primary_document_type: str = "RECEIPT",
    primary_document_custom_name: str = "",
    products: list[dict[str, Any]],
    kpp: str = "",
    fias_id: str = "",
) -> dict[str, Any]:
    """Build product_document JSON for withdrawal (LK_RECEIPT).

    ``primary_document_type``: RECEIPT / SALES_RECEIPT / OTHER (True API).
    For OTHER, ``primary_document_custom_name`` is required by ЧЗ.
    """
    doc_type = str(primary_document_type or "RECEIPT").strip() or "RECEIPT"
    doc: dict[str, Any] = {
        "inn": str(inn or "").strip(),
        "action": str(action or "DISTANCE").strip() or "DISTANCE",
        "action_date": str(document_date or "").strip(),
        "document_type": doc_type,
        "document_number": str(document_number or "").strip(),
        "document_date": str(document_date or "").strip(),
        "products": products,
    }
    custom = str(primary_document_custom_name or "").strip()
    if doc_type.upper() == "OTHER" and custom:
        doc["primary_document_custom_name"] = custom
    if kpp:
        doc["kpp"] = str(kpp).strip()
    if fias_id:
        doc["fias_id"] = str(fias_id).strip()
    return doc


def build_lp_return_document(
    *,
    inn: str,
    return_type: str = "REMOTE_SALE_RETURN",
    products: list[dict[str, Any]],
    paid: bool | None = None,
) -> dict[str, Any]:
    """Build product_document JSON for return to circulation (LP_RETURN).

    True API schema (MANUAL): ``trade_participant_inn``, ``return_type``,
    ``products_list`` with ``ki`` — not the LK_RECEIPT-style ``inn`` /
    ``products`` / ``cis`` fields. Missing ``trade_participant_inn`` yields
    ``LP_RETURN_ERROR 01: Не заполнено поле "ИНН участника оборота"``.
    """
    ret_type = str(return_type or "REMOTE_SALE_RETURN").strip() or "REMOTE_SALE_RETURN"
    products_list: list[dict[str, Any]] = []
    for raw in products or []:
        if not isinstance(raw, dict):
            continue
        ki = str(
            raw.get("ki") or raw.get("cis") or raw.get("uit_code") or ""
        ).strip()
        if not ki:
            continue
        item: dict[str, Any] = {"ki": ki}
        if "paid" in raw and raw.get("paid") is not None:
            item["paid"] = bool(raw.get("paid"))
        products_list.append(item)
    doc: dict[str, Any] = {
        "trade_participant_inn": str(inn or "").strip(),
        "return_type": ret_type,
        "products_list": products_list,
    }
    # REMOTE_SALE_RETURN requires ``paid`` at document or product level.
    # PVZ refusal / unpaid remote return → false; paid buyer return needs true
    # (+ primary document fields — caller must supply those separately).
    if paid is not None:
        doc["paid"] = bool(paid)
    elif ret_type == "REMOTE_SALE_RETURN":
        doc["paid"] = False
    return doc
