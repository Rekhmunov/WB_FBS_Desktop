"""Клиент Diadoc API для Заявки (ЭЗЗ, LogisticsOrderRequest).

Контур.Логистика для эТрН принимает XML через logist-api; для Заявки
публично задокументирована отправка через Diadoc ``PostMessage``
(TypeNamedId=LogisticsOrderRequest).

Auth: classic ``V3/Authenticate?type=password`` → ``DiadocAuth`` header
(см. https://developer.kontur.ru/docs/diadoc-api/authentication.html).
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

_log = logging.getLogger(__name__)

DEFAULT_DIADOC_URL = "https://diadoc-api.kontur.ru/"


@dataclass
class DiadocResult:
    ok: bool
    status_code: int = 0
    data: dict[str, Any] | list[Any] | Any = None
    error: str = ""
    raw: str = ""
    token: str = ""


class KonturDiadocClient:
    def __init__(
        self,
        *,
        api_url: str = DEFAULT_DIADOC_URL,
        client_id: str,
        login: str = "",
        password: str = "",
        token: str = "",
        timeout: float = 60.0,
    ) -> None:
        url = (api_url or DEFAULT_DIADOC_URL).strip()
        if not url.endswith("/"):
            url += "/"
        self.api_url = url
        self.client_id = (client_id or "").strip()
        self.login = (login or "").strip()
        self.password = password or ""
        self.token = (token or "").strip()
        self.timeout = timeout

    def authenticate(self) -> DiadocResult:
        """V3 Authenticate (password) → access token (classic DiadocAuth)."""
        if self.token:
            return DiadocResult(ok=True, status_code=200, token=self.token)
        if not self.client_id or not self.login or not self.password:
            return DiadocResult(ok=False, error="Не заданы Diadoc Client ID / логин / пароль")
        body = json.dumps({"login": self.login, "password": self.password}).encode("utf-8")
        headers = {
            "Authorization": f"DiadocAuth ddauth_api_client_id={self.client_id}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/plain, application/json",
            "User-Agent": "FeedPilot/1.0",
        }
        url = urljoin(self.api_url, "V3/Authenticate?type=password")
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                token = resp.read().decode("utf-8", errors="replace").strip().strip('"')
                if not token:
                    return DiadocResult(ok=False, status_code=int(resp.status), error="Пустой токен Diadoc")
                self.token = token
                return DiadocResult(ok=True, status_code=int(resp.status), token=token)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return DiadocResult(ok=False, status_code=int(exc.code), error=raw[:500] or f"HTTP {exc.code}", raw=raw)
        except Exception as exc:
            return DiadocResult(ok=False, error=str(exc))

    def _auth_headers(self) -> dict[str, str]:
        if not self.token:
            auth = self.authenticate()
            if not auth.ok:
                raise RuntimeError(auth.error or "Diadoc auth failed")
        # Classic tokens require DiadocAuth — Bearer is only for OIDC OpenID tokens.
        return {
            "Authorization": (
                f"DiadocAuth ddauth_api_client_id={self.client_id},"
                f"ddauth_token={self.token}"
            ),
            "Accept": "application/json; charset=utf-8",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "FeedPilot/1.0",
        }

    def _request(self, method: str, path: str, *, data: bytes | None = None) -> DiadocResult:
        try:
            headers = self._auth_headers()
        except RuntimeError as exc:
            return DiadocResult(ok=False, error=str(exc))
        url = urljoin(self.api_url, path.lstrip("/"))
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                parsed: Any = {}
                if raw.strip():
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        parsed = {"raw": raw}
                return DiadocResult(ok=True, status_code=int(resp.status), data=parsed, raw=raw, token=self.token)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return DiadocResult(ok=False, status_code=int(exc.code), error=raw[:800] or f"HTTP {exc.code}", raw=raw)
        except Exception as exc:
            _log.exception("diadoc request failed: %s %s", method, path)
            return DiadocResult(ok=False, error=str(exc))

    def send_order_request(
        self,
        *,
        from_box_id: str,
        to_box_id: str,
        xml_bytes: bytes,
        signature_bytes: bytes,
        version: str = "zakzvper_05_01_01",
    ) -> DiadocResult:
        """PostMessage V3 — Т1 Заявки (LogisticsOrderRequest)."""
        payload = {
            "FromBoxId": from_box_id.strip(),
            "ToBoxId": to_box_id.strip(),
            "DocumentAttachments": [
                {
                    "SignedContent": {
                        "Content": base64.b64encode(xml_bytes).decode("ascii"),
                        "Signature": base64.b64encode(signature_bytes).decode("ascii"),
                    },
                    "TypeNamedId": "LogisticsOrderRequest",
                    "Function": "default",
                    "Version": version,
                }
            ],
        }
        return self._request("POST", "V3/PostMessage", data=json.dumps(payload).encode("utf-8"))

    def send_waybill_diadoc(
        self,
        *,
        from_box_id: str,
        to_box_id: str,
        xml_bytes: bytes,
        signature_bytes: bytes,
        version: str = "kl_trn_mt_05_01",
    ) -> DiadocResult:
        """Альтернативная отправка эТрН через Diadoc PostMessage."""
        payload = {
            "FromBoxId": from_box_id.strip(),
            "ToBoxId": to_box_id.strip(),
            "DocumentAttachments": [
                {
                    "SignedContent": {
                        "Content": base64.b64encode(xml_bytes).decode("ascii"),
                        "Signature": base64.b64encode(signature_bytes).decode("ascii"),
                    },
                    "TypeNamedId": "LogisticsWaybill",
                    "Function": "reception",
                    "Version": version,
                }
            ],
        }
        return self._request("POST", "V3/PostMessage", data=json.dumps(payload).encode("utf-8"))

    def get_document(self, *, box_id: str, message_id: str, entity_id: str) -> DiadocResult:
        qs = urlencode({"boxId": box_id, "messageId": message_id, "entityId": entity_id})
        return self._request("GET", f"V3/GetDocument?{qs}")

    @staticmethod
    def parse_post_message_ids(payload: Any) -> dict[str, str]:
        if not isinstance(payload, dict):
            return {}
        message_id = str(payload.get("MessageId") or payload.get("messageId") or "").strip()
        entity_id = ""
        entities = payload.get("Entities") or payload.get("entities") or []
        if isinstance(entities, list):
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                att = ent.get("Attachment") or ent.get("attachment") or {}
                named = str(
                    (att.get("AttachmentTypeNamedId") if isinstance(att, dict) else None)
                    or ent.get("AttachmentTypeNamedId")
                    or ""
                )
                if "Logistics" in named or ent.get("EntityType") in ("Attachment", "attachment", 1):
                    entity_id = str(ent.get("EntityId") or ent.get("entityId") or "").strip()
                    if entity_id:
                        break
            if not entity_id and entities:
                first = entities[0]
                if isinstance(first, dict):
                    entity_id = str(first.get("EntityId") or first.get("entityId") or "").strip()
        return {"message_id": message_id, "entity_id": entity_id}

    @staticmethod
    def _detail_map(status: dict[str, Any]) -> dict[str, str]:
        """Flatten Status.Details[{Code,Text}] (and legacy StatusDetails dict) to {code: text}."""
        out: dict[str, str] = {}
        details = status.get("Details") or status.get("details") or []
        if isinstance(details, list):
            for item in details:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("Code") or item.get("code") or "").strip()
                text = str(item.get("Text") or item.get("text") or "").strip()
                if code:
                    out[code] = text
        legacy = status.get("StatusDetails") or status.get("statusDetails") or {}
        if isinstance(legacy, dict):
            for k, v in legacy.items():
                key = str(k or "").strip()
                if key and key not in out:
                    out[key] = str(v or "").strip()
        return out

    @classmethod
    def parse_document_status(cls, payload: Any) -> dict[str, str]:
        """Extract DocflowStatus + GIS ЭПД (KlMt/KIMt) ids from GetDocument JSON."""
        if not isinstance(payload, dict):
            return {
                "status": "",
                "status_label": "",
                "mintrans_id": "",
                "mintrans_status": "",
                "kl_id": "",
            }

        dfs = payload.get("DocflowStatus") or payload.get("docflowStatus") or {}
        primary: dict[str, Any] = {}
        if isinstance(dfs, dict):
            primary = dfs.get("PrimaryStatus") or dfs.get("primaryStatus") or {}
            if not isinstance(primary, dict):
                primary = {}
        status_label = str(
            primary.get("StatusText")
            or primary.get("statusText")
            or primary.get("StatusNamedId")
            or ""
        ).strip()
        status_code = str(
            primary.get("StatusNamedId")
            or primary.get("statusNamedId")
            or primary.get("Severity")
            or ""
        ).strip()
        if not status_label and isinstance(dfs, str):
            status_label = dfs.strip()

        mt_id = ""
        mt_rid = ""
        kl_id = ""
        gis_label = ""

        # GetDocument V3: LastOuterDocflows[].OuterDocflow
        last_outer = payload.get("LastOuterDocflows") or payload.get("lastOuterDocflows") or []
        outer_candidates: list[dict[str, Any]] = []
        if isinstance(last_outer, list):
            for item in last_outer:
                if not isinstance(item, dict):
                    continue
                info = item.get("OuterDocflow") or item.get("outerDocflow") or item
                if isinstance(info, dict):
                    outer_candidates.append(info)
        # Fallback: some payloads expose OuterDocflows directly
        outer = payload.get("OuterDocflows") or payload.get("outerDocflows") or []
        if isinstance(outer, list):
            for item in outer:
                if isinstance(item, dict):
                    outer_candidates.append(item)

        for od in outer_candidates:
            named = str(od.get("DocflowNamedId") or od.get("docflowNamedId") or "").strip()
            # Docs use both KIMt and KlMt for ГИС ЭПД.
            if named and named.casefold() not in ("kimt", "klmt"):
                continue
            st = od.get("Status") or od.get("status") or {}
            if not isinstance(st, dict):
                continue
            gis_label = str(
                st.get("FriendlyName")
                or st.get("friendlyName")
                or st.get("Description")
                or st.get("NamedId")
                or gis_label
            ).strip()
            details = cls._detail_map(st)
            mt_id = details.get("mt-id") or details.get("mt_id") or mt_id
            mt_rid = details.get("mt-rid") or details.get("mt_rid") or mt_rid
            kl_id = details.get("kl-id") or details.get("kl_id") or kl_id

        return {
            "status": (status_code or "sent")[:120],
            "status_label": (gis_label or status_label or "Отправлено в Diadoc")[:255],
            "mintrans_id": mt_id[:120],
            "mintrans_status": (mt_rid or gis_label)[:120],
            "kl_id": kl_id[:120],
        }
