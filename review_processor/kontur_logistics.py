"""Клиент API Контур.Логистика (logist-api.kontur.ru).

Auth: заголовок ``x-kontur-apikey``.
Документированные методы (по публичному клиенту magdv/kontur-logistics):
- POST v1/documents/waybill — отправка Т1 эТрН (XML + .sig)
- GET  v1/transportations/{id} — статус перевозки
- GET  v1/organizations/requisites — проверка ключа
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

_log = logging.getLogger(__name__)

DEFAULT_LOGISTICS_URL = "https://logist-api.kontur.ru/"

# Статусы перевозки Contour → человекочитаемые подписи
TRANSPORTATION_STATUS_LABELS: dict[str, str] = {
    "Unknown": "Неизвестно",
    "NewTransportation": "Создаётся на сервере",
    "RequestingMintransId": "Обработка ФГИС Минтранс",
    "WaybillReceptionWaitConsignorSignature": "На подписи грузоотправителя",
    "WaybillReceptionWaitConsignorSignatureDelivery": "Обработка подписи грузоотправителя",
    "WaybillReceptionWaitDriverConfirmation": "На подтверждении водителем",
    "WaybillReceptionWaitConsignorConfirmation": "На подтверждении кладовщиком",
    "WaybillReceptionWaitCarrierSignature": "На подписи перевозчика",
    "WaybillReceptionWaitCarrierSignatureDelivery": "Обработка подписи перевозчика",
    "OnTheWay": "В пути",
    "WaybillDeliveryWaitDriverConfirmation": "На подписи водителем (выгрузка)",
    "WaybillDeliveryWaitConsigneeConfirmation": "На подтверждении кладовщиком (выгрузка)",
    "WaybillDeliveryWaitConsigneeSignature": "На подписи грузополучателя",
    "WaybillDeliveryWaitConsigneeSignatureDelivery": "Обработка подписи грузополучателя",
    "WaybillDeliveryWaitCarrierSignature": "На подписи перевозчика (выгрузка)",
    "WaybillDeliveryWaitCarrierSignatureDelivery": "Обработка подписи перевозчика (выгрузка)",
    "Completed": "Завершён",
    "Revoked": "Аннулирован",
    "WaybillReceptionSignatureReject": "Отказ в подписи на погрузке",
    "WaybillDeliverySignatureReject": "Отказ в подписи на выгрузке",
    "Archived": "В архиве",
    "Invalid": "Ошибка в ТрН",
    "TransferredToAnotherDriver": "Передано другому водителю",
    "TransferredToAnotherConsignee": "Передано другому получателю",
}


def status_label(code: str | None) -> str:
    code = str(code or "").strip()
    if not code:
        return "Нет статуса"
    return TRANSPORTATION_STATUS_LABELS.get(code, code)


@dataclass
class LogisticsResult:
    ok: bool
    status_code: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    raw: str = ""


class KonturLogisticsClient:
    def __init__(self, *, api_url: str, api_key: str, timeout: float = 60.0) -> None:
        url = (api_url or DEFAULT_LOGISTICS_URL).strip()
        if not url.endswith("/"):
            url += "/"
        self.api_url = url
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "x-kontur-apikey": self.api_key,
            "Accept": "application/json",
            "User-Agent": "FeedPilot/1.0",
        }
        if extra:
            h.update(extra)
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> LogisticsResult:
        url = urljoin(self.api_url, path.lstrip("/"))
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers(headers))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                body: dict[str, Any] = {}
                if raw.strip():
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            body = parsed
                    except json.JSONDecodeError:
                        body = {"raw": raw}
                return LogisticsResult(ok=True, status_code=int(resp.status), data=body, raw=raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            err_msg = f"HTTP {exc.code}"
            body: dict[str, Any] = {}
            if raw.strip():
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        body = parsed
                        err = body.get("error") or body
                        if isinstance(err, dict):
                            err_msg = str(err.get("message") or err.get("Message") or err_msg)
                        elif isinstance(err, str):
                            err_msg = err
                except json.JSONDecodeError:
                    err_msg = raw[:500] or err_msg
            return LogisticsResult(ok=False, status_code=int(exc.code), data=body, error=err_msg, raw=raw)
        except Exception as exc:
            _log.exception("kontur logistics request failed: %s %s", method, path)
            return LogisticsResult(ok=False, status_code=0, error=str(exc))

    def ping(self) -> LogisticsResult:
        """Проверка API-ключа через реквизиты организации."""
        return self._request("GET", "v1/organizations/requisites")

    def send_waybill(
        self,
        *,
        xml_bytes: bytes,
        xml_filename: str,
        signature_bytes: bytes,
        signature_filename: str,
    ) -> LogisticsResult:
        """POST multipart v1/documents/waybill — XML + отсоединённая подпись .sig."""
        boundary = "----FeedPilotBoundary7MA4YWxkTrZu0gW"
        parts: list[bytes] = []

        def add_file(name: str, filename: str, content_type: str, content: bytes) -> None:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("utf-8")
                + content
                + b"\r\n"
            )

        add_file("formFiles", xml_filename, "text/xml", xml_bytes)
        add_file("formFiles", signature_filename, "application/octet-stream", signature_bytes)
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        return self._request(
            "POST",
            "v1/documents/waybill",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def get_transportation(self, transportation_id: str) -> LogisticsResult:
        tid = str(transportation_id or "").strip()
        if not tid:
            return LogisticsResult(ok=False, error="Не указан transportationId")
        return self._request("GET", f"v1/transportations/{tid}")

    @staticmethod
    def parse_transportation_status(payload: dict[str, Any]) -> dict[str, Any]:
        info = payload.get("transportationInfo") if isinstance(payload.get("transportationInfo"), dict) else payload
        if not isinstance(info, dict):
            info = {}
        status = str(info.get("status") or "").strip()
        desc = str(info.get("statusDescription") or "").strip() or status_label(status)
        mt = info.get("mintransStatus") if isinstance(info.get("mintransStatus"), dict) else {}
        return {
            "transportation_id": str(info.get("id") or payload.get("transportationId") or "").strip(),
            "status": status,
            "status_label": desc,
            "created": str(info.get("created") or ""),
            "last_modified": str(info.get("lastModified") or ""),
            "mintrans_id": str((mt or {}).get("id") or ""),
            "mintrans_status": str((mt or {}).get("status") or ""),
            "mintrans_status_label": str((mt or {}).get("statusDescription") or ""),
            "mintrans_has_errors": bool((mt or {}).get("hasErrors")),
            "mintrans_errors": str((mt or {}).get("errorsDescription") or ""),
            "reception_address": str(info.get("receptionAddress") or ""),
            "delivery_address": str(info.get("deliveryAddress") or ""),
        }
