"""Unit tests for Contour.Logistics client helpers (no live API)."""
from __future__ import annotations

from review_processor.kontur_logistics import (
    KonturLogisticsClient,
    status_label,
    TRANSPORTATION_STATUS_LABELS,
)
from review_processor.kontur_diadoc import KonturDiadocClient


def test_status_label_known_and_unknown():
    assert status_label("OnTheWay") == TRANSPORTATION_STATUS_LABELS["OnTheWay"]
    assert status_label("CustomX") == "CustomX"
    assert status_label("") == "Нет статуса"


def test_parse_transportation_status():
    payload = {
        "transportationInfo": {
            "id": "tid-1",
            "status": "OnTheWay",
            "statusDescription": "В пути",
            "mintransStatus": {
                "id": "mt-99",
                "status": "Registered",
                "statusDescription": "Зарегистрирован",
                "hasErrors": False,
            },
            "receptionAddress": "A",
            "deliveryAddress": "B",
        }
    }
    parsed = KonturLogisticsClient.parse_transportation_status(payload)
    assert parsed["transportation_id"] == "tid-1"
    assert parsed["status"] == "OnTheWay"
    assert parsed["status_label"] == "В пути"
    assert parsed["mintrans_id"] == "mt-99"


def test_parse_post_message_ids():
    payload = {
        "MessageId": "msg-1",
        "Entities": [
            {"EntityId": "ent-1", "EntityType": "Attachment", "AttachmentTypeNamedId": "LogisticsOrderRequest"},
        ],
    }
    ids = KonturDiadocClient.parse_post_message_ids(payload)
    assert ids["message_id"] == "msg-1"
    assert ids["entity_id"] == "ent-1"


def test_diadoc_auth_headers_use_diadoc_auth_scheme():
    c = KonturDiadocClient(client_id="cid-1", token="tok-abc")
    headers = c._auth_headers()
    assert headers["Authorization"] == (
        "DiadocAuth ddauth_api_client_id=cid-1,ddauth_token=tok-abc"
    )
    assert "Bearer" not in headers["Authorization"]


def test_parse_document_status_primary_and_gis():
    payload = {
        "DocflowStatus": {
            "PrimaryStatus": {
                "Severity": "Info",
                "StatusText": "Документ отправлен",
            }
        },
        "LastOuterDocflows": [
            {
                "ParentEntityId": "ent-1",
                "OuterDocflow": {
                    "DocflowNamedId": "KlMt",
                    "DocflowFriendlyName": "ГИС ЭПД",
                    "Status": {
                        "NamedId": "4000211000",
                        "FriendlyName": "Принят новый перевозочный документ",
                        "Type": "Success",
                        "Details": [
                            {"Code": "mt-id", "Text": "mt-uuid-1"},
                            {"Code": "mt-rid", "Text": "rid-1"},
                            {"Code": "kl-id", "Text": "kl-uuid-1"},
                        ],
                    },
                },
            }
        ],
    }
    parsed = KonturDiadocClient.parse_document_status(payload)
    assert parsed["status_label"] == "Принят новый перевозочный документ"
    assert parsed["mintrans_id"] == "mt-uuid-1"
    assert parsed["kl_id"] == "kl-uuid-1"


def test_parse_document_status_kimt_case_insensitive():
    payload = {
        "DocflowStatus": {"PrimaryStatus": {"StatusText": "В обработке"}},
        "OuterDocflows": [
            {
                "DocflowNamedId": "KIMt",
                "Status": {
                    "FriendlyName": "ГИС статус",
                    "Details": [{"Code": "mt-id", "Text": "m1"}],
                },
            }
        ],
    }
    parsed = KonturDiadocClient.parse_document_status(payload)
    assert parsed["mintrans_id"] == "m1"
    assert parsed["status_label"] == "ГИС статус"


def test_logistics_client_normalizes_url():
    c = KonturLogisticsClient(api_url="https://logist-api.kontur.ru", api_key="k")
    assert c.api_url.endswith("/")
