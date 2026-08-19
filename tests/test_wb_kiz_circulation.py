"""Unit tests for WB FBS → ЧЗ KIZ circulation (new block)."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from review_processor.chz_true_api import (
    DEMO_BASE,
    PROD_BASE,
    PROD_BASE_V4,
    ChzTrueApiClient,
    build_lk_receipt_document,
    build_lp_return_document,
)
from review_processor import wb_kiz_circulation as circ


def test_event_key_stable() -> None:
    a = circ._event_key(
        srid="s1",
        excise_short="01046",
        operation_type=1,
        fiscal_doc_number="123",
        fiscal_dt="2026-08-01",
    )
    b = circ._event_key(
        srid="s1",
        excise_short="01046",
        operation_type=1,
        fiscal_doc_number="123",
        fiscal_dt="2026-08-01",
    )
    c = circ._event_key(
        srid="s1",
        excise_short="01046",
        operation_type=2,
        fiscal_doc_number="123",
        fiscal_dt="2026-08-01",
    )
    assert a == b
    assert a != c
    assert len(a) == 40


def test_normalize_row_withdraw() -> None:
    row = {
        "operation_type_id": 1,
        "excise_short": "0104670172422724215ABC",
        "srid": "srid-1",
        "fiscal_doc_number": "77",
        "fiscal_dt": "2026-08-10T12:00:00",
        "price": 1990.5,
        "nm_id": 123,
        "currency_name_short": "RUB",
    }
    norm = circ._normalize_row(row)
    assert norm is not None
    assert norm["operation_type"] == 1
    assert norm["fiscal_dt"] == "2026-08-10"
    assert norm["price"] == 1990.5
    assert norm["currency_name"] == "RUB"
    assert norm["event_key"]


def test_normalize_fiscal_number_int() -> None:
    norm = circ._normalize_row(
        {
            "operationTypeId": 1,
            "exciseShort": "X",
            "srid": "s",
            "fiscalDocNumber": 12345,
            "fiscalDt": "2026-01-02",
        }
    )
    assert norm is not None
    assert norm["fiscal_doc_number"] == "12345"


def test_normalize_row_skips_unknown_op() -> None:
    assert circ._normalize_row({"operation_type_id": 9, "excise_short": "x"}) is None
    assert circ._normalize_row({"operation_type_id": 1, "excise_short": ""}) is None


def test_initial_status_return_without_fiscal_is_pending() -> None:
    ret = circ._normalize_row(
        {"operation_type_id": 2, "excise_short": "Y", "srid": "s2"}
    )
    assert ret is not None
    st, reason = circ._initial_status(ret)
    assert st == circ.STATUS_PENDING
    assert reason == ""


def test_initial_status_withdraw_without_fiscal_is_pending_other() -> None:
    w = circ._normalize_row(
        {"operation_type_id": 1, "excise_short": "Y", "srid": "s2"}
    )
    assert w is not None
    st, reason = circ._initial_status(w)
    assert st == circ.STATUS_PENDING
    assert reason == circ.SKIP_NO_FISCAL


def test_is_no_fiscal_reason_accepts_legacy_russian() -> None:
    assert circ._is_no_fiscal_reason("no_fiscal")
    assert circ._is_no_fiscal_reason("нет номера/даты чека")
    assert circ._is_no_fiscal_reason("нет чека")
    assert not circ._is_no_fiscal_reason("other")


def test_analytics_fetch_is_soft_failure() -> None:
    assert circ._analytics_fetch_is_soft_failure(
        RuntimeError("WB excise-report HTTP 504: stream timeout")
    )
    assert circ._analytics_fetch_is_soft_failure(
        RuntimeError("WB excise-report сеть: timed out")
    )
    assert circ._analytics_fetch_is_soft_failure(
        RuntimeError("WB excise-report HTTP 502: Bad Gateway")
    )
    assert not circ._analytics_fetch_is_soft_failure(
        RuntimeError("WB Analytics: слишком много запросов (HTTP 429)")
    )
    assert not circ._analytics_fetch_is_soft_failure(
        RuntimeError("WB excise-report HTTP 401")
    )


def test_build_lk_receipt_and_return() -> None:
    receipt = build_lk_receipt_document(
        inn="7707083893",
        document_number="100",
        document_date="2026-08-10",
        products=[{"cis": "01046", "product_cost": 100.0}],
        kpp="770701001",
    )
    assert receipt["action"] == "DISTANCE"
    assert receipt["kpp"] == "770701001"
    assert len(receipt["products"]) == 1

    ret = build_lp_return_document(
        inn="7707083893",
        products=[{"cis": "01046"}],
    )
    assert ret["return_type"] == "REMOTE_SALE_RETURN"
    assert ret["trade_participant_inn"] == "7707083893"
    assert "inn" not in ret
    assert ret["paid"] is False
    assert ret["products_list"] == [{"ki": "01046"}]
    assert "products" not in ret


def test_chz_client_base_urls() -> None:
    assert ChzTrueApiClient().base == PROD_BASE
    assert ChzTrueApiClient(base_url="demo").base == DEMO_BASE
    assert ChzTrueApiClient(base_url=DEMO_BASE).base == DEMO_BASE


def test_create_document_prefers_signed_b64() -> None:
    client = ChzTrueApiClient()
    signed_doc = build_lk_receipt_document(
        inn="1",
        document_number="11",
        document_date="2026-08-10",
        products=[{"cis": "a", "product_cost": 10.0}],
        kpp="1",
        fias_id="f",
    )
    signed_raw = json.dumps(signed_doc, ensure_ascii=False, separators=(",", ":")).encode()
    signed_b64 = base64.b64encode(signed_raw).decode()

    captured: dict = {}

    def fake_request(method, path, *, params=None, body=None, auth=True):
        captured["body"] = body
        return "doc-1"

    client._request = fake_request  # type: ignore[method-assign]
    # Deliberately different product_document that would break signature if used
    broken = json.loads(json.dumps(signed_doc))
    for p in broken["products"]:
        if isinstance(p.get("product_cost"), float) and p["product_cost"].is_integer():
            p["product_cost"] = int(p["product_cost"])

    doc_id = client.create_document(
        doc_type="LK_RECEIPT",
        product_group="lp",
        product_document=broken,
        product_document_b64=signed_b64,
        signature_b64="SIG",
    )
    assert doc_id == "doc-1"
    assert captured["body"]["product_document"] == signed_b64


def test_parse_true_api_bare_uuid_payload() -> None:
    from review_processor.chz_true_api import _parse_true_api_payload

    # Starts with digits+e — classic json.loads "Extra data" trap
    bare = "123e4567-e89b-12d3-a456-426614174000"
    assert _parse_true_api_payload(bare.encode()) == bare
    assert _parse_true_api_payload(b'"quoted-id"') == "quoted-id"
    assert _parse_true_api_payload(b'{"id":"x"}') == {"id": "x"}
    assert _parse_true_api_payload(b"") == {}
    assert _parse_true_api_payload(b"plain-letter-uuid") == "plain-letter-uuid"


def test_classify_chz_doc_status() -> None:
    assert circ.classify_chz_doc_status("CHECKED_OK") == circ.STATUS_ACCEPTED
    assert circ.classify_chz_doc_status("CHECKED_NOT_OK") == circ.STATUS_ERROR
    assert circ.classify_chz_doc_status("PROCESSING_ERROR") == circ.STATUS_ERROR
    assert circ.classify_chz_doc_status("IN_PROGRESS") == circ.STATUS_SUBMITTED
    assert circ.classify_chz_doc_status("") == circ.STATUS_SUBMITTED
    assert circ.classify_chz_doc_status("SOME_NEW_NOT_OK") == circ.STATUS_ERROR


def test_sgtin_codes_from_meta_row() -> None:
    codes = circ._sgtin_codes_from_meta_row(
        {
            "id": 1,
            "meta": {
                "sgtin": {
                    "value": [
                        "0104670172422632215ABC\u001d91EE12",
                        "0104670172422632215ABC\u001d91EE12",
                    ]
                }
            },
        }
    )
    assert codes == ["0104670172422632215ABC\u001d91EE12"]
    assert circ._sgtin_codes_from_meta_row({"meta": {"sgtin": {"value": None}}}) == []
    assert circ._sgtin_codes_from_meta_row(
        {"metaDetails": [{"key": "sgtin", "value": "01046X"}]}
    ) == ["01046X"]


def test_enrich_norm_from_analytics_copies_fiscal() -> None:
    mp = {
        "operation_type": 1,
        "srid": "eB1.i9bab981f1d9940e298d74b76b8d1bfab.0.0",
        "rid": "eB1.i9bab981f1d9940e298d74b76b8d1bfab.0.0",
        "excise_short": "01046CIS",
        "fiscal_doc_number": "",
        "fiscal_dt": "2026-08-11",
    }
    an_mids = {
        "i9bab981f1d9940e298d74b76b8d1bfab": [
            {
                "operation_type": 1,
                "excise_short": "01046CIS",
                "fiscal_doc_number": "51783",
                "fiscal_dt": "2026-08-14",
                "price": 2712.0,
            }
        ]
    }
    got = circ._enrich_norm_from_analytics(mp, an_mids)
    assert got["fiscal_doc_number"] == "51783"
    assert got["fiscal_dt"] == "2026-08-14"
    assert got["price"] == 2712.0


def test_build_marketplace_period_norms_sold_and_pvz() -> None:
    """Period sync must queue sold→вывод and canceled_by_client→ввод from meta.sgtin."""
    repo = MagicMock()
    client = MagicMock()
    client.get_orders_page.side_effect = [
        (
            [
                {
                    "id": 5462672780,
                    "rid": "eB1.i9bab981f1d9940e298d74b76b8d1bfab.0.0",
                    "orderUid": "i9bab981f1d9940e298d74b76b8d1bfab",
                    "createdAt": "2026-08-11T03:58:26Z",
                    "nmId": 1,
                    "skus": ["467"],
                    "convertedPrice": 219200,
                    "convertedCurrencyCode": 643,
                    "price": 219200,
                    "currencyCode": 643,
                },
                {
                    "id": 5474932440,
                    "rid": "eB0.i68a55ca6dc9f74217c98d9df5384d982.0.0",
                    "orderUid": "i68a55ca6dc9f74217c98d9df5384d982",
                    "createdAt": "2026-08-12T19:16:58Z",
                    "nmId": 2,
                    "skus": ["468"],
                    "convertedPrice": 150050,
                    "convertedCurrencyCode": 643,
                },
            ],
            None,
        )
    ]
    client.get_statuses.return_value = [
        {"id": 5462672780, "wbStatus": "sold", "supplierStatus": "complete"},
        {
            "id": 5474932440,
            "wbStatus": "canceled_by_client",
            "supplierStatus": "complete",
        },
    ]
    client.get_orders_meta.return_value = [
        {
            "id": 5462672780,
            "meta": {"sgtin": {"value": ["01046SOLD"]}},
        },
        {
            "id": 5474932440,
            "meta": {"sgtin": {"value": ["01046PVZ"]}},
        },
    ]
    with patch("review_processor.wb_fbs.WbFbsClient", return_value=client), patch(
        "review_processor.wb_fbs.upsert_order"
    ), patch(
        "review_processor.wb_fbs.load_order_kiz_map", return_value={}
    ):
        norms, index, meta = circ.build_marketplace_period_norms(
            repo,
            user_id=1,
            source_id=13,
            api_key="mp-key",
            date_from="2026-08-01",
            date_to="2026-08-14",
        )
    assert meta["sold"] == 1
    assert meta["returns"] == 1
    assert meta["with_kiz"] == 2
    assert len(norms) == 2
    ops = {int(n["operation_type"]): n["excise_short"] for n in norms}
    assert ops[circ.OP_WITHDRAW] == "01046SOLD"
    assert ops[circ.OP_RETURN] == "01046PVZ"
    sold = next(n for n in norms if int(n["operation_type"]) == circ.OP_WITHDRAW)
    assert sold["price"] == 2192.0
    assert sold["currency_name"] == "RUB"
    assert circ._price_for_chz(sold) == 219200
    assert index[circ._rid_fold("eB1.i9bab981f1d9940e298d74b76b8d1bfab.0.0")][
        "wb_status"
    ] == "sold"


def test_msk_day_windows_chunks_over_30_days() -> None:
    windows = circ._msk_day_windows("2026-07-01", "2026-08-14", max_days=30)
    assert len(windows) == 2
    assert windows[0][0].date().isoformat() == "2026-07-01"
    assert windows[0][1].date().isoformat() == "2026-07-30"
    assert windows[1][0].date().isoformat() == "2026-07-31"
    assert windows[1][1].date().isoformat() == "2026-08-14"


def test_build_marketplace_period_norms_chunks_long_range() -> None:
    """Ranges longer than 30 days must call GET /orders per WB-safe window."""
    repo = MagicMock()
    client = MagicMock()
    client.get_orders_page.side_effect = [
        ([{"id": 1, "rid": "r1", "createdAt": "2026-07-10T00:00:00Z"}], None),
        ([{"id": 2, "rid": "r2", "createdAt": "2026-08-10T00:00:00Z"}], None),
    ]
    client.get_statuses.return_value = [
        {"id": 1, "wbStatus": "sold", "supplierStatus": "complete"},
        {"id": 2, "wbStatus": "sold", "supplierStatus": "complete"},
    ]
    client.get_orders_meta.return_value = [
        {"id": 1, "meta": {"sgtin": {"value": ["010A"]}}},
        {"id": 2, "meta": {"sgtin": {"value": ["010B"]}}},
    ]
    with patch("review_processor.wb_fbs.WbFbsClient", return_value=client), patch(
        "review_processor.wb_fbs.upsert_order"
    ), patch(
        "review_processor.wb_fbs.load_order_kiz_map", return_value={}
    ):
        norms, _index, meta = circ.build_marketplace_period_norms(
            repo,
            user_id=1,
            source_id=13,
            api_key="mp-key",
            date_from="2026-07-01",
            date_to="2026-08-14",
        )
    assert client.get_orders_page.call_count == 2
    assert meta["sold"] == 2
    assert len(norms) == 2
    # Second window starts after first 30-day chunk.
    second_call_kwargs = client.get_orders_page.call_args_list[1].kwargs
    assert second_call_kwargs["date_from"].date().isoformat() == "2026-07-31"


def test_extract_chz_doc_status() -> None:
    assert circ.extract_chz_doc_status({"status": "CHECKED_NOT_OK"}) == "CHECKED_NOT_OK"
    assert circ.extract_chz_doc_status({"docStatus": "CHECKED_OK"}) == "CHECKED_OK"
    assert (
        circ.extract_chz_doc_status({"body": {"status": "PROCESSING_ERROR"}})
        == "PROCESSING_ERROR"
    )
    assert circ.extract_chz_doc_status({}) == ""
    assert circ.extract_chz_doc_status(None) == ""
    # CRPT returns an array; also tolerate legacy {"raw": [...]} wrapper.
    assert (
        circ.extract_chz_doc_status(
            [{"number": "x", "status": "CHECKED_NOT_OK"}]
        )
        == "CHECKED_NOT_OK"
    )
    assert (
        circ.extract_chz_doc_status(
            {"raw": [{"status": "PARSE_ERROR"}]}
        )
        == "PARSE_ERROR"
    )


def test_document_info_uses_v4_base() -> None:
    client = ChzTrueApiClient()
    client.set_token("tok")
    captured: dict = {}

    def fake_request(method, path, *, params=None, body=None, auth=True, base=None):
        captured.update({"method": method, "path": path, "base": base, "auth": auth})
        # True API /doc/{id}/info returns a JSON array of document cards.
        return [
            {
                "number": "8f9a0d25-6ba2-404f-ac82-ef72d0140429",
                "status": "CHECKED_NOT_OK",
                "errors": ["Недопустимый статус кода"],
                "commonErrors": [
                    {
                        "errorCode": "STATUS",
                        "errorMessage": "Недопустимый статус кода идентификации",
                    }
                ],
            }
        ]

    client._request = fake_request  # type: ignore[method-assign]
    info = client.document_info("8f9a0d25-6ba2-404f-ac82-ef72d0140429")
    assert info["status"] == "CHECKED_NOT_OK"
    assert "raw" not in info
    assert captured["method"] == "GET"
    assert captured["path"].endswith("/info")
    assert captured["base"] == PROD_BASE_V4
    assert client.v4_base() == PROD_BASE_V4


def test_reconcile_infers_error_from_common_errors_without_status() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    row = {
        "event_key": "ek-2",
        "chz_doc_id": "doc-2",
        "chz_status": "submitted",
        "status": "submitted",
        "error_text": "",
    }
    cur = MagicMock()
    cur.fetchall.return_value = [row]
    conn.execute.return_value = cur
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {}

    client = MagicMock()
    client.document_info.return_value = {
        "commonErrors": [
            {"errorCode": "X", "errorMessage": "Недопустимое количество символов"}
        ]
    }

    with patch.object(circ, "ensure_kiz_circulation_tables"), patch.object(
        circ, "heal_submitted_terminal_statuses", return_value={"healed": 0}
    ), patch.object(circ, "apply_chz_doc_status", return_value=circ.STATUS_ERROR) as apply:
        out = circ.reconcile_submitted_with_chz(
            repo, client, user_id=1, source_id=13
        )
    assert out["failed"] == 1
    assert apply.call_args.kwargs["chz_status"] == "CHECKED_NOT_OK"
    assert "символов" in apply.call_args.kwargs["error_text"]


def test_reconcile_submitted_applies_checked_not_ok() -> None:
    """Сверить ЧЗ must flip local «отправлен» when True API returns CHECKED_NOT_OK."""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    row = {
        "event_key": "ek-1",
        "chz_doc_id": "8f9a0d25-6ba2-404f-ac82-ef72d0140429",
        "chz_status": "submitted",
        "status": "submitted",
        "error_text": "",
    }
    cur = MagicMock()
    cur.fetchall.return_value = [row]
    conn.execute.return_value = cur
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {}

    client = MagicMock()
    client.document_info.return_value = {
        "status": "CHECKED_NOT_OK",
        "errors": [{"message": "код уже выведен"}],
    }

    with patch.object(circ, "ensure_kiz_circulation_tables"), patch.object(
        circ, "heal_submitted_terminal_statuses", return_value={"healed": 0}
    ), patch.object(circ, "apply_chz_doc_status", return_value=circ.STATUS_ERROR) as apply:
        out = circ.reconcile_submitted_with_chz(
            repo, client, user_id=1, source_id=13
        )
    assert out["docs_checked"] == 1
    assert out["failed"] == 1
    assert out["api_errors"] == 0
    apply.assert_called_once()
    kwargs = apply.call_args.kwargs
    assert kwargs["chz_status"] == "CHECKED_NOT_OK"
    assert "выведен" in kwargs["error_text"]


def test_cis_status_labels_and_kinds() -> None:
    assert circ.cis_status_label("INTRODUCED") == "В обороте"
    assert circ.cis_status_label("RETIRED") == "Выведен"
    assert circ.cis_status_label("WITHDRAWN") == "Выведен"
    assert circ.classify_cis_status("INTRODUCED") == "in_circulation"
    assert circ.classify_cis_status("RETIRED") == "withdrawn"
    assert circ.classify_cis_status("APPLIED") == "pre"
    assert circ.classify_cis_status("") == "unknown"


def test_cis_display_foreign_owner_marked_transferred() -> None:
    """OwnerInn ≠ our INN → gray «Передан» (same kind as выведен)."""
    label, kind = circ.cis_display_for_row(
        status="INTRODUCED",
        owner_inn="7707083893",
        participant_inn="6215034988",
    )
    assert label == "Передан"
    assert kind == "withdrawn"
    assert circ.cis_owner_is_foreign(
        owner_inn="7707083893", participant_inn="6215034988"
    )
    # Same owner → keep True API status.
    label2, kind2 = circ.cis_display_for_row(
        status="INTRODUCED",
        owner_inn="6215034988",
        participant_inn="6215034988",
    )
    assert label2 == "В обороте"
    assert kind2 == "in_circulation"
    # Missing owner/ours → do not invent «Передан».
    label3, kind3 = circ.cis_display_for_row(
        status="RETIRED",
        owner_inn="",
        participant_inn="6215034988",
    )
    assert label3 == "Выведен"
    assert kind3 == "withdrawn"


def test_parse_cises_info_item() -> None:
    parsed = circ.parse_cises_info_item(
        {
            "cisInfo": {
                "cis": "0104670172422458215mC3G",
                "requestedCis": "0104670172422458215mC3G",
                "status": "INTRODUCED",
                "ownerInn": "6215034988",
            },
            "errorMessage": "",
        }
    )
    assert parsed["status"] == "INTRODUCED"
    assert parsed["owner_inn"] == "6215034988"
    assert parsed["cis"].startswith("010467")

    missing = circ.parse_cises_info_item(
        {
            "cisInfo": {"requestedCis": "0104670172422458215mC3G"},
            "errorMessage": "КИ не найден",
            "errorCode": "404",
        }
    )
    assert "не найден" in missing["error"]
    assert missing["error_code"] == "404"
    assert missing["cis"].endswith("5mC3G")


def test_chz_client_has_cises_info_method() -> None:
    """Regression: cises_info must stay on the client class (not nested dead code)."""
    assert hasattr(ChzTrueApiClient, "cises_info")
    client = ChzTrueApiClient()
    client.set_token("tok")
    captured: dict = {}

    def fake_request(method, path, *, params=None, body=None, auth=True, base=None):
        captured.update({"method": method, "path": path, "params": params, "body": body})
        return [
            {
                "cisInfo": {
                    "requestedCis": body[0],
                    "cis": body[0],
                    "status": "INTRODUCED",
                }
            }
        ]

    client._request = fake_request  # type: ignore[method-assign]
    rows = client.cises_info(["010460000000000021LLLLLLLLLLLLL"], product_group="lp")
    assert rows[0]["cisInfo"]["status"] == "INTRODUCED"
    assert captured["path"] == "/cises/info"
    assert captured["params"] == {"pg": "lp"}
    assert captured["body"] == ["010460000000000021LLLLLLLLLLLLL"]


def test_refresh_cis_statuses_updates_rows() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    row = {
        "id": 1,
        "event_key": "ek-cis-1",
        "excise_short": "0104670172422458215mC3G",
        "cis_status": "",
        "cis_owner_inn": "",
        "cis_status_error": "",
        "cis_checked_at": "",
    }
    cur = MagicMock()
    cur.fetchall.return_value = [row]
    conn.execute.return_value = cur
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {}

    client = MagicMock()
    client.cises_info.return_value = [
        {
            "cisInfo": {
                "cis": "0104670172422458215mC3GbyHCO2",
                "status": "RETIRED",
                "ownerInn": "6215034988",
            }
        }
    ]

    with patch.object(circ, "ensure_kiz_circulation_tables"), patch.object(
        circ, "get_chz_settings", return_value={"product_group": "lp"}
    ):
        out = circ.refresh_cis_statuses(
            repo,
            client,
            user_id=1,
            source_id=13,
            event_keys=["ek-cis-1"],
            product_group="lp",
        )
    assert out["updated"] == 1
    assert out["found"] == 1
    assert out["missing"] == 0
    client.cises_info.assert_called_once()
    assert client.cises_info.call_args.kwargs.get("product_group") == "lp"
    # UPDATE must persist RETIRED
    update_calls = [
        c for c in conn.execute.call_args_list if "UPDATE wb_kiz_circulation_events" in str(c)
    ]
    assert update_calls
    args = update_calls[0].args[1]
    assert args[0] == "RETIRED"
    assert args[1] == "6215034988"


def test_price_for_chz_skips_foreign_currency() -> None:
    assert circ._price_for_chz({"price": 10, "currency_name": "AMD"}) is None
    # WB rubles → True API kopecks
    assert circ._price_for_chz({"price": 10, "currency_name": "RUB"}) == 1000
    assert circ._price_for_chz({"price": 21.92, "currency_name": ""}) == 2192
    assert circ._price_for_chz({"price": 2192.0, "currency_name": "RUB"}) == 219200
    assert circ._price_for_chz({"price": 3146, "currency_name": "643"}) == 314600
    assert circ._price_for_chz({"price": 0, "currency_name": "RUB"}) is None
    assert circ._price_for_chz({"price": None, "currency_name": "RUB"}) is None


def test_normalize_cis_for_chz_joins_spurious_gs_in_serial() -> None:
    """GS without 91/92/93 is not end of serial (lp stubs like 5yZ2V → 404)."""
    raw = "0104670172421086215yZ2V\x1drHSdGMe"
    assert circ._normalize_cis_for_chz(raw) == "0104670172421086215yZ2VrHSdGMe"
    assert circ._normalize_cis_for_chz("  abc  ") == "abc"


def test_normalize_cis_for_chz_stops_gs_before_crypto() -> None:
    raw = (
        "0104670172422458215gQCPfVLRo\x1d91EE11\x1d92"
        "Iu6ItDVS0yWEXyXNZUi/O1AvwaZtASBirynzRY4pdOo="
    )
    assert circ._normalize_cis_for_chz(raw) == "0104670172422458215gQCPfVLRo"


def test_normalize_cis_keeps_special_serial_chars() -> None:
    for code in (
        "010467017242257121506dyC>p-MmQh",
        "0104678434671088215QMb-McC(aEQq",
        "0104670172422441215)acfWiao<Def",
        "0104678434671088215g4+(qDeNY",
    ):
        assert circ._normalize_cis_for_chz(code) == code


def test_normalize_cis_keeps_apostrophe_and_underscore_in_serial() -> None:
    """Prod sample: apostrophe was stripped as CSV junk → wrong CIS → 404."""
    raw = (
        "0104670172422458215D0j_Hi<'bO0P\x1d91EE11\x1d92"
        "ToUEc2LJ6s9KGuRutHpM9o7owFpLJ4MRHmCTyauJy3k="
    )
    assert (
        circ._normalize_cis_for_chz(raw)
        == "0104670172422458215D0j_Hi<'bO0P"
    )
    assert "'" in circ._normalize_cis_for_chz(raw)
    assert "_" in circ._normalize_cis_for_chz(raw)
    fmt = circ._format_cis_for_chz_document(raw)
    assert fmt.startswith("0104670172422458215D0j_Hi<'bO0P\x1d91")


def test_gs1_cset82_serial_alphabet() -> None:
    """AI 21 must accept full GS1 CSET 82 (comma included; space/# excluded)."""
    assert "," in circ._GS1_CSET82
    assert "'" in circ._GS1_CSET82
    assert '"' in circ._GS1_CSET82
    assert " " not in circ._GS1_CSET82
    assert "#" not in circ._GS1_CSET82
    assert len(circ._GS1_CSET82) == 82
    # Mid-serial comma is valid and must survive (unlike Excel ``,i"91`` junk).
    code = "0104670172422458215D0j_Hi,bO0P"
    assert circ._normalize_cis_for_chz(code) == code
    assert (
        circ._normalize_cis_for_chz(code + "\x1d91EE11\x1d92" + ("A" * 44)) == code
    )


def test_format_cis_for_chz_document_repairs_csv_junk() -> None:
    """User error: quotes/comma before 91 and trailing period → length reject in ЧЗ."""
    dirty = (
        '0104670172422458215gQCPfVLRo,i"91EE1192'
        "Iu6ItDVS0yWEXyXNZUi/O1AvwaZtASBirynzRY4pdOo=."
    )
    out = circ._format_cis_for_chz_document(dirty)
    assert '"' not in out
    assert "," not in out
    assert not out.endswith(".")
    assert out.startswith("0104670172422458215gQCPfVLRo")
    assert "\x1d91EE11\x1d92" in out
    assert out.endswith("RY4pdOo=")
    # Short form for matching ignores crypto.
    assert circ._normalize_cis_for_chz(dirty) == "0104670172422458215gQCPfVLRo"


def test_format_cis_for_chz_document_keeps_clean_gs() -> None:
    clean = (
        "0104670172422458215gQCPfVLRo\x1d91EE11\x1d92"
        "Iu6ItDVS0yWEXyXNZUi/O1AvwaZtASBirynzRY4pdOo="
    )
    assert circ._format_cis_for_chz_document(clean) == clean


def test_extract_chz_doc_errors() -> None:
    info = {
        "status": "CHECKED_NOT_OK",
        "errors": [
            {"message": "МОД не найдены"},
            {"code": "12", "description": "Недопустимый статус кода"},
        ],
        "commonErrors": [
            {
                "errorCode": "STATUS",
                "errorMessage": "Недопустимый статус кода идентификации",
            }
        ],
    }
    text = circ.extract_chz_doc_errors(info)
    assert "МОД не найдены" in text
    assert "Недопустимый статус" in text
    assert "STATUS" in text


@patch(
    "review_processor.wb_kiz_circulation._close_deduped_prepare_events",
    return_value=0,
)
@patch(
    "review_processor.wb_kiz_circulation._load_sent_cis_identities",
    return_value=set(),
)
@patch(
    "review_processor.wb_kiz_circulation._attach_order_ids_to_events",
)
@patch("review_processor.wb_kiz_circulation.list_events_for_chz")
@patch(
    "review_processor.wb_kiz_circulation.repair_circulation_queue",
    return_value={"returns_fixed": 0, "withdraw_skipped": 0},
)
@patch("review_processor.wb_kiz_circulation.get_chz_settings")
def test_prepare_groups_by_receipt(
    mock_settings, _repair, mock_list, _attach, _sent, _close
) -> None:
    mock_settings.return_value = {
        "is_enabled": True,
        "participant_inn": "7707083893",
        "product_group": "lp",
        "kpp": "770701001",
        "fias_id": "fias-1",
        "return_type": "REMOTE_SALE_RETURN",
        "cert_thumbprint": "",
        "api_base": "prod",
        "api_base_url": PROD_BASE,
    }
    events = [
        {
            "event_key": "k1",
            "operation_type": 1,
            "order_id": 1001,
            "order_wb_status": "sold",
            "excise_short": "cis-a",
            "fiscal_doc_number": "11",
            "fiscal_dt": "2026-08-10",
            "price": 10,
            "currency_name": "RUB",
            "status": "pending",
        },
        {
            "event_key": "k2",
            "operation_type": 1,
            "order_id": 1001,
            "order_wb_status": "sold",
            "excise_short": "cis-b",
            "fiscal_doc_number": "11",
            "fiscal_dt": "2026-08-10",
            "price": 20,
            "currency_name": "RUB",
            "status": "pending",
        },
        {
            "event_key": "k3",
            "operation_type": 2,
            "order_id": 2002,
            "order_wb_status": "canceled_by_client",
            "order_supplier_status": "cancel",
            "excise_short": "cis-c",
            "fiscal_doc_number": "",
            "fiscal_dt": "",
            "status": "pending",
        },
    ]
    mock_list.return_value = list(events)
    out = circ.prepare_chz_batches(repo=object(), user_id=1, source_id=2)
    assert out["counts"]["documents"] == 2
    assert out["counts"]["withdraw_events"] == 2
    assert out["counts"]["return_events"] == 1
    types = {d["doc_type"] for d in out["documents"]}
    assert types == {"LK_RECEIPT", "LP_RETURN"}
    withdraw = next(d for d in out["documents"] if d["doc_type"] == "LK_RECEIPT")
    assert set(withdraw["event_keys"]) == {"k1", "k2"}
    assert withdraw["sign_payload_b64"]
    # Signed payload must keep float encoding stable for whole numbers
    raw = base64.b64decode(withdraw["sign_payload_b64"])
    # product_cost is kopecks: 10 RUB → 1000
    assert b"1000" in raw
    assert b'"product_cost":1000' in raw or b'"product_cost": 1000' in raw


@patch(
    "review_processor.wb_kiz_circulation._close_deduped_prepare_events",
    return_value=0,
)
@patch(
    "review_processor.wb_kiz_circulation._load_sent_cis_identities",
    return_value=set(),
)
@patch(
    "review_processor.wb_kiz_circulation._attach_order_ids_to_events",
)
@patch("review_processor.wb_kiz_circulation.list_events_for_chz")
@patch(
    "review_processor.wb_kiz_circulation.repair_circulation_queue",
    return_value={"returns_fixed": 0, "withdraw_skipped": 0},
)
@patch("review_processor.wb_kiz_circulation.get_chz_settings")
def test_prepare_soft_skips_withdraw_without_kpp_keeps_returns(
    mock_settings, _repair, mock_list, _attach, _sent, _close
) -> None:
    mock_settings.return_value = {
        "is_enabled": True,
        "participant_inn": "7707083893",
        "product_group": "lp",
        "kpp": "",
        "fias_id": "",
        "return_type": "REMOTE_SALE_RETURN",
        "cert_thumbprint": "",
        "api_base": "prod",
        "api_base_url": PROD_BASE,
    }
    mock_list.return_value = [
        {
            "event_key": "k1",
            "operation_type": 1,
            "order_id": 1001,
            "order_wb_status": "sold",
            "excise_short": "cis-a",
            "fiscal_doc_number": "11",
            "fiscal_dt": "2026-08-10",
            "status": "pending",
        },
        {
            "event_key": "r1",
            "operation_type": 2,
            "order_id": 2002,
            "order_wb_status": "canceled_by_client",
            "order_supplier_status": "cancel",
            "excise_short": "cis-r",
            "fiscal_doc_number": "",
            "fiscal_dt": "",
            "status": "pending",
        },
    ]
    out = circ.prepare_chz_batches(repo=object(), user_id=1, source_id=2)
    assert out["counts"]["withdraw_events"] == 0
    assert out["counts"]["return_events"] == 1
    returns = [d for d in out["documents"] if d["doc_type"] == "LP_RETURN"]
    assert len(returns) == 1
    body = returns[0]["product_document"]
    assert body["trade_participant_inn"] == "7707083893"
    assert body["products_list"] == [{"ki": "cis-r"}]
    assert body["paid"] is False
    assert "inn" not in body
    assert out["warnings"]
    assert "юр. лица" in out["warnings"][0]
    assert not any(d["doc_type"] == "LK_RECEIPT" for d in out["documents"])


@patch(
    "review_processor.wb_kiz_circulation._close_deduped_prepare_events",
    return_value=0,
)
@patch(
    "review_processor.wb_kiz_circulation._load_sent_cis_identities",
    return_value=set(),
)
@patch(
    "review_processor.wb_kiz_circulation._attach_order_ids_to_events",
)
@patch("review_processor.wb_kiz_circulation.list_events_for_chz")
@patch(
    "review_processor.wb_kiz_circulation.repair_circulation_queue",
    return_value={"returns_fixed": 0, "withdraw_skipped": 0, "withdraw_requeued": 0},
)
@patch("review_processor.wb_kiz_circulation.get_chz_settings")
def test_prepare_nofiscal_withdraw_uses_other_primary_doc(
    mock_settings, _repair, mock_list, _attach, _sent, _close
) -> None:
    mock_settings.return_value = {
        "is_enabled": True,
        "participant_inn": "7707083893",
        "product_group": "lp",
        "kpp": "770701001",
        "fias_id": "fias-1",
        "return_type": "REMOTE_SALE_RETURN",
        "cert_thumbprint": "",
        "api_base": "prod",
        "api_base_url": PROD_BASE,
    }
    mock_list.return_value = [
        {
            "event_key": "k-nofiscal",
            "operation_type": 1,
            "order_id": 1001,
            "order_wb_status": "sold",
            "excise_short": "cis-nofiscal",
            "fiscal_doc_number": "",
            "fiscal_dt": "",
            "skip_reason": "no_fiscal",
            "status": "pending",
            "price": 15,
            "currency_name": "RUB",
        }
    ]
    out = circ.prepare_chz_batches(repo=object(), user_id=1, source_id=2)
    assert out["counts"]["withdraw_events"] == 1
    assert out["counts"]["skipped"] == 0
    withdraw = next(d for d in out["documents"] if d["doc_type"] == "LK_RECEIPT")
    body = withdraw["product_document"]
    assert body["action"] == "DISTANCE"
    assert body["document_type"] == "OTHER"
    assert body["primary_document_custom_name"] == "Без документа основания"
    assert body["document_number"].startswith("WB-NOFISCAL-")
    assert "OTHER" in withdraw["title"] or "без чека" in withdraw["title"].lower()
    assert body["products"][0]["product_cost"] == 1500


@patch(
    "review_processor.wb_kiz_circulation._close_deduped_prepare_events",
    return_value=0,
)
@patch(
    "review_processor.wb_kiz_circulation._load_sent_cis_identities",
    return_value=set(),
)
@patch(
    "review_processor.wb_kiz_circulation._attach_order_ids_to_events",
)
@patch("review_processor.wb_kiz_circulation.list_events_for_chz")
@patch(
    "review_processor.wb_kiz_circulation.repair_circulation_queue",
    return_value={"returns_fixed": 0, "withdraw_skipped": 0, "withdraw_requeued": 0},
)
@patch("review_processor.wb_kiz_circulation.get_chz_settings")
def test_prepare_nofiscal_skips_without_product_cost(
    mock_settings, _repair, mock_list, _attach, _sent, _close
) -> None:
    mock_settings.return_value = {
        "is_enabled": True,
        "participant_inn": "7707083893",
        "product_group": "lp",
        "kpp": "770701001",
        "fias_id": "fias-1",
        "return_type": "REMOTE_SALE_RETURN",
        "cert_thumbprint": "",
        "api_base": "prod",
        "api_base_url": PROD_BASE,
    }
    mock_list.return_value = [
        {
            "event_key": "k-nofiscal-noprice",
            "operation_type": 1,
            "order_id": 5461937159,
            "order_wb_status": "sold",
            "excise_short": "cis-nofiscal",
            "fiscal_doc_number": "",
            "fiscal_dt": "",
            "skip_reason": "no_fiscal",
            "status": "pending",
            "price": None,
            "currency_name": "",
        }
    ]
    out = circ.prepare_chz_batches(repo=object(), user_id=1, source_id=2)
    assert out["counts"]["documents"] == 0
    assert out["counts"]["skipped"] == 1
    assert any("цены" in str(w).lower() for w in (out.get("warnings") or []))


def test_build_lk_receipt_other_includes_custom_name() -> None:
    from review_processor.chz_true_api import build_lk_receipt_document

    doc = build_lk_receipt_document(
        inn="7707083893",
        document_number="WB-1",
        document_date="2026-08-14",
        primary_document_type="OTHER",
        primary_document_custom_name="Без документа основания",
        products=[{"cis": "X"}],
        kpp="1",
        fias_id="f",
    )
    assert doc["document_type"] == "OTHER"
    assert doc["primary_document_custom_name"] == "Без документа основания"


@patch(
    "review_processor.wb_kiz_circulation._close_deduped_prepare_events",
    return_value=0,
)
@patch(
    "review_processor.wb_kiz_circulation._load_sent_cis_identities",
    return_value=set(),
)
@patch(
    "review_processor.wb_kiz_circulation._attach_order_ids_to_events",
)
@patch("review_processor.wb_kiz_circulation.list_events_for_chz")
@patch(
    "review_processor.wb_kiz_circulation.repair_circulation_queue",
    return_value={"returns_fixed": 0, "withdraw_skipped": 0},
)
@patch("review_processor.wb_kiz_circulation.get_chz_settings")
def test_prepare_chunks_returns(mock_settings, _repair, mock_list, _attach, _sent, _close) -> None:
    mock_settings.return_value = {
        "is_enabled": True,
        "participant_inn": "7707083893",
        "product_group": "lp",
        "kpp": "1",
        "fias_id": "f",
        "return_type": "REMOTE_SALE_RETURN",
        "cert_thumbprint": "",
        "api_base": "prod",
        "api_base_url": PROD_BASE,
    }
    events = [
        {
            "event_key": f"r{i}",
            "operation_type": 2,
            "order_id": 2002,
            "order_wb_status": "canceled_by_client",
            "order_supplier_status": "cancel",
            "excise_short": f"cis-{i}",
            "fiscal_doc_number": "",
            "fiscal_dt": "",
            "status": "pending",
        }
        for i in range(circ.CHZ_PRODUCTS_PER_DOC + 5)
    ]
    mock_list.return_value = events
    out = circ.prepare_chz_batches(repo=object(), user_id=1, source_id=2)
    returns = [d for d in out["documents"] if d["doc_type"] == "LP_RETURN"]
    assert len(returns) == 2
    assert sum(len(d["event_keys"]) for d in returns) == len(events)


def test_upsert_rejects_numeric_pg() -> None:
    repo = MagicMock()
    with patch.object(circ, "ensure_kiz_circulation_tables"):
        with patch.object(
            circ,
            "get_chz_settings",
            return_value={
                "api_base": "prod",
                "kpp": "",
                "fias_id": "",
                "return_type": "REMOTE_SALE_RETURN",
                "cert_thumbprint": "",
            },
        ):
            with pytest.raises(ValueError, match="не число"):
                circ.upsert_chz_settings(
                    repo,
                    user_id=1,
                    product_group="8",
                    participant_inn="1",
                )


def test_resolve_excise_period_uses_exact_dates_no_ceiling() -> None:
    period = circ.resolve_excise_period(
        date_from="2025-01-01", date_to="2026-08-13"
    )
    assert period["date_from"] == "2025-01-01"
    assert period["date_to"] == "2026-08-13"
    assert period["days"] == (date.fromisoformat("2026-08-13") - date.fromisoformat("2025-01-01")).days + 1

    swapped = circ.resolve_excise_period(
        date_from="2026-08-13", date_to="2026-08-01"
    )
    assert swapped["date_from"] == "2026-08-01"
    assert swapped["date_to"] == "2026-08-13"

    with pytest.raises(ValueError, match="Укажите даты"):
        circ.resolve_excise_period(date_from="", date_to="2026-08-13")


def test_format_wb_excise_http_error_429() -> None:
    err = circ.format_wb_excise_http_error(
        code=429, body='{"status":429}', retry_after="1800"
    )
    assert "10 запросов" in str(err)
    assert "30 мин" in str(err)
    err2 = circ.format_wb_excise_http_error(code=403, body="forbidden")
    assert "HTTP 403" in str(err2)
    assert "forbidden" in str(err2)


def test_wb_analytics_key_encrypt_roundtrip_and_mask() -> None:
    from review_processor.security import encrypt_secret, mask_secret

    plain = "eyJhbGciOiJFUzI1NiJ9.analytics-test-token"
    enc = encrypt_secret(plain)
    assert enc and enc != plain
    assert circ._decrypt_wb_analytics_key(
        {"wb_analytics_api_key_encrypted": enc}
    ) == plain
    assert circ._decrypt_wb_analytics_key({"wb_analytics_api_key_encrypted": ""}) == ""
    preview = mask_secret(plain)
    assert preview
    assert plain not in preview


def test_get_wb_analytics_api_key_reads_encrypted() -> None:
    from review_processor.security import encrypt_secret

    plain = "wb-analytics-secret"
    enc = encrypt_secret(plain)
    row = {"wb_analytics_api_key_encrypted": enc}
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.return_value.fetchone.return_value = row
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r
    with patch.object(circ, "ensure_kiz_circulation_tables"):
        assert circ.get_wb_analytics_api_key(repo, user_id=7) == plain


def test_parse_inn_kpp_from_requisites() -> None:
    inn, kpp = circ._parse_inn_kpp_from_text("ИНН 7707083893 КПП 770701001")
    assert inn == "7707083893"
    assert kpp == "770701001"


def test_resolve_chz_place_from_legal() -> None:
    repo = MagicMock()
    repo.list_supply_legal_entities.return_value = [
        {
            "requisites": "ИНН 7707083893 / КПП 770701001",
            "addr_fias": "0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
            "short_name": "ООО Тест",
        }
    ]
    place = circ.resolve_chz_place_details(
        repo, user_id=1, participant_inn="7707083893"
    )
    assert place["kpp"] == "770701001"
    assert place["fias_id"] == "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"


def test_attach_order_ids_to_events_via_srid() -> None:
    events = [
        {"srid": "eAC.abc.0.0", "rid": ""},
        {"srid": "missing", "rid": ""},
        {"srid": "", "rid": "eAC.abc.0.0"},
    ]
    with patch("review_processor.wb_fbs.order_ids_by_srids") as lookup, patch(
        "review_processor.wb_fbs.load_order_status_map"
    ) as status_map, patch(
        "review_processor.wb_fbs.load_order_price_map", return_value={}
    ):
        lookup.return_value = {"eAC.abc.0.0": 3291847561}
        status_map.return_value = {
            3291847561: {
                "supplier_status": "complete",
                "wb_status": "sold",
                "cancel_reason_label": "",
                "order_status_label": "Выкуплен",
            }
        }
        circ._attach_order_ids_to_events(
            MagicMock(), user_id=1, source_id=2, events=events
        )
    assert events[0]["order_id"] == 3291847561
    assert events[0]["order_status_label"] == "Выкуплен"
    assert events[0]["order_wb_status"] == "sold"
    assert events[1]["order_id"] is None
    assert events[1]["order_status_label"] == ""
    assert events[2]["order_id"] == 3291847561
    assert events[2]["order_status_label"] == "Выкуплен"
    called_srids = lookup.call_args.kwargs["srids"]
    assert "eAC.abc.0.0" in called_srids
    assert "missing" in called_srids


def test_attach_backfills_price_from_order_map() -> None:
    events = [
        {
            "event_key": "ek-1",
            "srid": "rid-1",
            "rid": "rid-1",
            "price": None,
            "currency_name": "",
        }
    ]
    with patch("review_processor.wb_fbs.order_ids_by_srids") as lookup, patch(
        "review_processor.wb_fbs.load_order_status_map", return_value={}
    ), patch(
        "review_processor.wb_fbs.load_order_price_map"
    ) as price_map, patch(
        "review_processor.wb_kiz_circulation._persist_event_prices", return_value=1
    ) as persist:
        lookup.return_value = {"rid-1": 5461937159}
        price_map.return_value = {
            5461937159: {"price_rub": 3146.0, "currency_name": "RUB", "currency_code": 643}
        }
        circ._attach_order_ids_to_events(
            MagicMock(), user_id=1, source_id=13, events=events
        )
    assert events[0]["order_id"] == 5461937159
    assert events[0]["price"] == 3146.0
    assert events[0]["currency_name"] == "RUB"
    persist.assert_called_once()


def test_order_portal_status_label() -> None:
    from review_processor.wb_fbs import order_portal_status_label

    assert order_portal_status_label(wb_status="sold") == "Выкуплен"
    assert order_portal_status_label(wb_status="canceled_by_client") == "Отказ на ПВЗ"
    assert order_portal_status_label(supplier_status="confirm") == "На сборке"
    assert order_portal_status_label(supplier_status="complete") == "В доставке"
    assert order_portal_status_label(supplier_status="new") == "Новый"


def test_cis_identity_ignores_fiscal() -> None:
    a = circ._cis_identity(
        srid="s1", rid="", excise_short="CIS", operation_type=1
    )
    b = circ._cis_identity(
        srid="s1", rid="r1", excise_short="CIS", operation_type=1
    )
    # rid is ignored when srid is present
    assert a == b
    assert a != circ._cis_identity(
        srid="s2", rid="", excise_short="CIS", operation_type=1
    )


def test_resolve_sync_upgrade_late_fiscal() -> None:
    nofiscal = {
        "id": 10,
        "event_key": "key-nofiscal",
        "status": circ.STATUS_PENDING,
        "fiscal_doc_number": "",
        "fiscal_dt": "",
        "srid": "s1",
        "excise_short": "CIS",
        "operation_type": 1,
        "order_id": 1001,
        "order_wb_status": "sold",
    }
    incoming = {
        "event_key": "key-with-fiscal",
        "fiscal_doc_number": "99",
        "fiscal_dt": "2026-08-01",
        "srid": "s1",
        "excise_short": "CIS",
        "operation_type": 1,
        "order_id": 1001,
        "order_wb_status": "sold",
    }
    action, target = circ._resolve_sync_action([nofiscal], norm=incoming)
    assert action == "upgrade"
    assert target is nofiscal


def test_resolve_sync_suppress_when_already_submitted() -> None:
    sent = {
        "id": 11,
        "event_key": "key-nofiscal",
        "status": circ.STATUS_SUBMITTED,
        "fiscal_doc_number": "",
        "fiscal_dt": "",
        "chz_doc_id": "doc-1",
    }
    incoming = {
        "event_key": "key-with-fiscal",
        "fiscal_doc_number": "99",
        "fiscal_dt": "2026-08-01",
    }
    action, target = circ._resolve_sync_action([sent], norm=incoming)
    assert action == "suppress"
    assert target is sent


def test_resolve_sync_suppress_nofiscal_when_fiscal_open() -> None:
    fiscal = {
        "id": 12,
        "event_key": "key-fiscal",
        "status": circ.STATUS_PENDING,
        "fiscal_doc_number": "1",
        "fiscal_dt": "2026-08-01",
    }
    incoming = {
        "event_key": "key-nofiscal",
        "fiscal_doc_number": "",
        "fiscal_dt": "",
    }
    action, _ = circ._resolve_sync_action([fiscal], norm=incoming)
    assert action == "suppress"


def test_resolve_sync_upsert_same_key() -> None:
    row = {
        "id": 1,
        "event_key": "same",
        "status": circ.STATUS_PENDING,
        "fiscal_doc_number": "1",
        "fiscal_dt": "2026-08-01",
    }
    action, target = circ._resolve_sync_action(
        [row], norm={"event_key": "same", "fiscal_doc_number": "1", "fiscal_dt": "2026-08-01"}
    )
    assert action == "upsert"
    assert target is row


def test_dedupe_events_prefers_fiscal_and_skips_already_sent() -> None:
    sent = {("s1", "CIS", 1)}
    nofiscal = {
        "event_key": "a",
        "srid": "s1",
        "rid": "",
        "excise_short": "CIS",
        "operation_type": 1,
        "order_id": 1001,
        "order_wb_status": "sold",
        "fiscal_doc_number": "",
        "fiscal_dt": "",
    }
    fiscal = {
        "event_key": "b",
        "srid": "s1",
        "rid": "",
        "excise_short": "CIS",
        "operation_type": 1,
        "order_id": 1001,
        "order_wb_status": "sold",
        "fiscal_doc_number": "7",
        "fiscal_dt": "2026-08-01",
    }
    other = {
        "event_key": "c",
        "srid": "s1",
        "rid": "",
        "excise_short": "CIS",
        "operation_type": 1,
        "order_id": 1001,
        "order_wb_status": "sold",
        "fiscal_doc_number": "8",
        "fiscal_dt": "2026-08-02",
        "status": "pending",
    }
    kept, skipped = circ._dedupe_events_for_prepare(
        [nofiscal, fiscal], sent_identities=set()
    )
    assert len(kept) == 1
    assert kept[0]["event_key"] == "b"
    assert any(s.get("skip_reason") == "duplicate_nofiscal" for s in skipped)

    kept2, skipped2 = circ._dedupe_events_for_prepare([other], sent_identities=sent)
    assert kept2 == []
    assert skipped2[0]["skip_reason"] == "already_sent"


@patch(
    "review_processor.wb_kiz_circulation._close_deduped_prepare_events",
    return_value=0,
)
@patch(
    "review_processor.wb_kiz_circulation._load_sent_cis_identities",
)
@patch(
    "review_processor.wb_kiz_circulation._attach_order_ids_to_events",
)
@patch("review_processor.wb_kiz_circulation.list_events_for_chz")
@patch(
    "review_processor.wb_kiz_circulation.repair_circulation_queue",
    return_value={"returns_fixed": 0, "withdraw_skipped": 0},
)
@patch("review_processor.wb_kiz_circulation.get_chz_settings")
def test_prepare_skips_already_sent_identity(
    mock_settings, _repair, mock_list, _attach, mock_sent, _close
) -> None:
    mock_settings.return_value = {
        "is_enabled": True,
        "participant_inn": "7707083893",
        "product_group": "lp",
        "kpp": "770701001",
        "fias_id": "fias-1",
        "return_type": "REMOTE_SALE_RETURN",
        "cert_thumbprint": "",
        "api_base": "prod",
        "api_base_url": PROD_BASE,
    }
    mock_sent.return_value = {("s1", "cis-a", 1)}
    mock_list.return_value = [
        {
            "event_key": "dup",
            "operation_type": 1,
            "order_id": 1001,
            "order_wb_status": "sold",
            "srid": "s1",
            "rid": "",
            "excise_short": "cis-a",
            "fiscal_doc_number": "11",
            "fiscal_dt": "2026-08-10",
            "price": 10,
            "currency_name": "RUB",
            "status": "pending",
        }
    ]
    out = circ.prepare_chz_batches(repo=object(), user_id=1, source_id=2)
    assert out["counts"]["documents"] == 0
    assert out["counts"]["skipped"] == 1
    assert out["skipped"][0]["skip_reason"] == "already_sent"


def test_retention_cutoff_roughly_six_months() -> None:
    assert circ.EVENT_RETENTION_DAYS == 180
    cutoff = circ._retention_cutoff_iso()
    assert cutoff < datetime.now(timezone.utc).isoformat()
    # ~6 months ago (± a few days)
    from datetime import timedelta

    expected = (datetime.now(timezone.utc) - timedelta(days=180)).date()
    assert cutoff[:10] == expected.isoformat()


def test_cis_anchor_prefers_srid() -> None:
    assert circ._cis_anchor(srid="s1", rid="r1") == "s1"
    assert circ._cis_anchor(srid="", rid="r1") == "r1"


def test_upsert_sent_cis_rows_writes_registry() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    with patch.object(circ, "ensure_kiz_circulation_tables"):
        n = circ.upsert_sent_cis_rows(
            repo,
            user_id=1,
            source_id=2,
            rows=[
                {
                    "excise_short": "CIS1",
                    "operation_type": 1,
                    "order_id": 1001,
                    "order_wb_status": "sold",
                    "srid": "s1",
                    "rid": "",
                    "chz_doc_id": "doc-9",
                    "event_key": "ek",
                    "fiscal_doc_number": "1",
                    "fiscal_dt": "2026-01-01",
                }
            ],
            accepted_at="2026-08-14T00:00:00+00:00",
        )
    assert n == 1
    assert conn.execute.called
    sql = conn.execute.call_args.args[0]
    assert "wb_kiz_sent_cis" in sql


def test_load_sent_cis_merges_registry() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    # First execute → events; second → registry
    ev_rows = [{"srid": "s1", "rid": "", "excise_short": "A", "operation_type": 1}]
    reg_rows = [{"anchor": "s2", "excise_short": "B", "operation_type": 2}]
    ev_result = MagicMock()
    ev_result.fetchall.return_value = ev_rows
    reg_result = MagicMock()
    reg_result.fetchall.return_value = reg_rows
    conn.execute.side_effect = [ev_result, reg_result]
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r
    with patch.object(circ, "ensure_kiz_circulation_tables"):
        out = circ._load_sent_cis_identities(repo, user_id=1, source_id=2)
    assert ("s1", "A", 1) in out
    assert ("s2", "B", 2) in out


def test_maintain_storage_calls_purge_helpers() -> None:
    repo = MagicMock()
    with patch.object(circ, "clear_accepted_raw_json", return_value=3) as c1, patch.object(
        circ, "purge_old_kiz_circulation_events", return_value=5
    ) as c2, patch.object(
        circ, "purge_old_kiz_runs_and_docs", return_value={"runs": 1, "docs": 2}
    ) as c3, patch.object(
        circ, "_mark_storage_maintained"
    ) as c4, patch.object(
        circ, "get_cursor", return_value={"last_storage_at": ""}
    ):
        out = circ.maintain_kiz_circulation_storage(
            repo, user_id=1, source_id=2, force=True
        )
    assert out == {
        "raw_json_cleared": 3,
        "events_purged": 5,
        "runs_purged": 1,
        "docs_purged": 2,
        "skipped": 0,
    }
    c1.assert_called_once()
    c2.assert_called_once()
    c3.assert_called_once()
    c4.assert_called_once()


def test_maintain_storage_throttles_when_recent() -> None:
    repo = MagicMock()
    recent = datetime.now(timezone.utc).isoformat()
    with patch.object(
        circ, "get_cursor", return_value={"last_storage_at": recent}
    ), patch.object(circ, "clear_accepted_raw_json") as clear:
        out = circ.maintain_kiz_circulation_storage(repo, user_id=1, source_id=2)
    assert out["skipped"] == 1
    clear.assert_not_called()


@patch(
    "review_processor.wb_kiz_circulation._close_deduped_prepare_events",
    return_value=0,
)
@patch(
    "review_processor.wb_kiz_circulation._load_sent_cis_identities",
    return_value=set(),
)
@patch(
    "review_processor.wb_kiz_circulation._attach_order_ids_to_events",
)
@patch("review_processor.wb_kiz_circulation.list_events_for_chz")
@patch(
    "review_processor.wb_kiz_circulation.repair_circulation_queue",
    return_value={"returns_fixed": 0, "withdraw_skipped": 0},
)
@patch("review_processor.wb_kiz_circulation.get_chz_settings")
def test_prepare_caps_documents_per_round(
    mock_settings, _repair, mock_list, _attach, _sent, _close, monkeypatch
) -> None:
    """UKЭP signs one doc at a time — prepare must not return huge batches."""
    monkeypatch.setattr(circ, "CHZ_DOCUMENTS_PER_PREPARE", 3)
    mock_settings.return_value = {
        "is_enabled": True,
        "participant_inn": "7707083893",
        "product_group": "lp",
        "kpp": "770701001",
        "fias_id": "fias-1",
        "return_type": "REMOTE_SALE_RETURN",
        "cert_thumbprint": "",
        "api_base": "prod",
        "api_base_url": PROD_BASE,
    }
    mock_list.return_value = [
        {
            "event_key": f"k{i}",
            "operation_type": 1,
            "order_id": 1001,
            "order_wb_status": "sold",
            "excise_short": f"cis-{i}",
            "fiscal_doc_number": str(100 + i),
            "fiscal_dt": "2026-08-10",
            "price": 10,
            "currency_name": "RUB",
            "status": "pending",
        }
        for i in range(10)
    ]
    out = circ.prepare_chz_batches(repo=object(), user_id=1, source_id=2)
    assert out["counts"]["documents_built"] == 10
    assert out["counts"]["documents_cap"] == 3
    assert out["counts"]["documents"] == 3
    assert out["counts"]["withdraw_events"] == 3
    assert len(out["documents"]) == 3
    assert out["has_more"] is True
    # Oldest-first: first three receipt numbers from the batch.
    titles = [d["title"] for d in out["documents"]]
    assert "чек 100" in titles[0]
    assert "чек 101" in titles[1]
    assert "чек 102" in titles[2]


@patch(
    "review_processor.wb_kiz_circulation._close_deduped_prepare_events",
    return_value=0,
)
@patch(
    "review_processor.wb_kiz_circulation._load_sent_cis_identities",
    return_value=set(),
)
@patch(
    "review_processor.wb_kiz_circulation._attach_order_ids_to_events",
)
@patch("review_processor.wb_kiz_circulation.list_events_for_chz")
@patch(
    "review_processor.wb_kiz_circulation.repair_circulation_queue",
    return_value={"returns_fixed": 0, "withdraw_skipped": 0},
)
@patch("review_processor.wb_kiz_circulation.get_chz_settings")
def test_prepare_passes_event_keys_filter(
    mock_settings, _repair, mock_list, _attach, _sent, _close
) -> None:
    mock_settings.return_value = {
        "is_enabled": True,
        "participant_inn": "7707083893",
        "product_group": "lp",
        "kpp": "770701001",
        "fias_id": "fias-1",
        "return_type": "REMOTE_SALE_RETURN",
        "cert_thumbprint": "",
        "api_base": "prod",
        "api_base_url": PROD_BASE,
    }
    mock_list.return_value = [
        {
            "event_key": "only-me",
            "operation_type": 1,
            "order_id": 1001,
            "order_wb_status": "sold",
            "excise_short": "cis-x",
            "fiscal_doc_number": "42",
            "fiscal_dt": "2026-08-10",
            "price": 10,
            "currency_name": "RUB",
            "status": "pending",
        }
    ]
    out = circ.prepare_chz_batches(
        repo=object(),
        user_id=1,
        source_id=2,
        event_keys=["only-me", "ignored-not-returned"],
    )
    mock_list.assert_called_once()
    assert mock_list.call_args.kwargs.get("event_keys") == [
        "only-me",
        "ignored-not-returned",
    ]
    assert out["counts"]["documents"] == 1
    assert out["documents"][0]["event_keys"] == ["only-me"]


def test_withdraw_not_sold_reason() -> None:
    assert circ._withdraw_not_sold_reason({"operation_type": 2}) == ""
    assert circ._withdraw_not_sold_reason(
        {"operation_type": 1, "order_id": None}
    ) == circ.SKIP_NOT_FBS
    assert circ._withdraw_not_sold_reason(
        {
            "operation_type": 1,
            "order_id": 1,
            "order_wb_status": "waiting",
            "order_status_label": "В пути",
        }
    ) == circ.SKIP_NOT_SOLD
    assert (
        circ._withdraw_not_sold_reason(
            {"operation_type": 1, "order_id": 1, "order_wb_status": "sold"}
        )
        == ""
    )
    assert circ._event_is_sold_for_chz({"order_wb_status": "Sold"}) is True
    assert circ._event_is_sold_for_chz({"order_wb_status": "sorted"}) is False


def test_cis_identity_keys_match_suffix_variants() -> None:
    a = circ._cis_identity_keys(
        srid="ord.MIDTOKEN.1.0", rid="", excise_short="CIS", operation_type=1
    )
    b = circ._cis_identity_keys(
        srid="", rid="ord.MIDTOKEN.0.0", excise_short="CIS", operation_type=1
    )
    assert a & b


def test_related_index_matches_rid_suffix_variants() -> None:
    idx: dict = {}
    circ._index_related_event(
        idx,
        {
            "id": 1,
            "event_key": "nofiscal",
            "operation_type": 1,
            "excise_short": "CIS1",
            "srid": "aa.BBCC.1.0",
            "rid": "",
            "status": "pending",
            "fiscal_doc_number": "",
            "fiscal_dt": "",
        },
    )
    found = circ._related_from_index(
        idx,
        norm={
            "operation_type": 1,
            "excise_short": "CIS1",
            "srid": "",
            "rid": "aa.BBCC.0.0",
        },
    )
    assert len(found) == 1
    assert found[0]["event_key"] == "nofiscal"


def test_dedupe_prepare_matches_fold_variants() -> None:
    kept, skipped = circ._dedupe_events_for_prepare(
        [
            {
                "event_key": "a",
                "srid": "x.Y.1.0",
                "rid": "",
                "excise_short": "CIS",
                "operation_type": 1,
                "fiscal_doc_number": "",
                "fiscal_dt": "",
            },
            {
                "event_key": "b",
                "srid": "",
                "rid": "x.Y.0.0",
                "excise_short": "CIS",
                "operation_type": 1,
                "fiscal_doc_number": "9",
                "fiscal_dt": "2026-08-01",
            },
        ],
        sent_identities=set(),
    )
    assert len(kept) == 1
    assert kept[0]["event_key"] == "b"
    assert skipped[0]["skip_reason"] == "duplicate_nofiscal"


def test_repair_stale_submitted_events() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = MagicMock()
    cur.rowcount = 2
    conn.execute.return_value = cur
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    with patch.object(circ, "ensure_kiz_circulation_tables"):
        assert circ.repair_stale_submitted_events(repo, user_id=1, source_id=2) == 2
    sql = conn.execute.call_args.args[0]
    assert circ.SKIP_STALE_SUBMITTED in conn.execute.call_args.args[1]
    assert "updated_at < ?" in sql
    assert "chz_doc_id" in sql


@patch(
    "review_processor.wb_kiz_circulation._close_deduped_prepare_events",
    return_value=0,
)
@patch(
    "review_processor.wb_kiz_circulation._load_sent_cis_identities",
    return_value=set(),
)
@patch(
    "review_processor.wb_kiz_circulation._attach_order_ids_to_events",
)
@patch("review_processor.wb_kiz_circulation.list_events_for_chz")
@patch(
    "review_processor.wb_kiz_circulation.repair_circulation_queue",
    return_value={"returns_fixed": 0, "withdraw_skipped": 0},
)
@patch("review_processor.wb_kiz_circulation.get_chz_settings")
def test_prepare_skips_withdraw_unless_sold(
    mock_settings, _repair, mock_list, _attach, _sent, _close
) -> None:
    mock_settings.return_value = {
        "is_enabled": True,
        "participant_inn": "7707083893",
        "product_group": "lp",
        "kpp": "770701001",
        "fias_id": "fias-1",
        "return_type": "REMOTE_SALE_RETURN",
        "cert_thumbprint": "",
        "api_base": "prod",
        "api_base_url": PROD_BASE,
    }

    def _attach_side_effect(
        repo, *, user_id, source_id, events, api_key="", hydrate=False, refresh_statuses=False
    ):
        for ev in events:
            if ev.get("event_key") == "sold-ok":
                ev["order_id"] = 10
                ev["order_wb_status"] = "sold"
                ev["order_status_label"] = "Выкуплен"
            elif ev.get("event_key") == "in-transit":
                ev["order_id"] = 11
                ev["order_wb_status"] = "sorted"
                ev["order_status_label"] = "В пути"
            elif ev.get("event_key") == "no-order":
                ev["order_id"] = None
                ev["order_wb_status"] = ""
                ev["order_status_label"] = ""

    _attach.side_effect = _attach_side_effect
    mock_list.return_value = [
        {
            "event_key": "sold-ok",
            "operation_type": 1,
            "excise_short": "cis-sold",
            "fiscal_doc_number": "1",
            "fiscal_dt": "2026-08-10",
            "price": 10,
            "currency_name": "RUB",
            "status": "pending",
        },
        {
            "event_key": "in-transit",
            "operation_type": 1,
            "excise_short": "cis-way",
            "fiscal_doc_number": "2",
            "fiscal_dt": "2026-08-10",
            "price": 10,
            "currency_name": "RUB",
            "status": "pending",
        },
        {
            "event_key": "no-order",
            "operation_type": 1,
            "excise_short": "cis-miss",
            "fiscal_doc_number": "3",
            "fiscal_dt": "2026-08-10",
            "price": 10,
            "currency_name": "RUB",
            "status": "pending",
        },
    ]
    out = circ.prepare_chz_batches(
        repo=object(), user_id=1, source_id=2, api_key="mp-key"
    )
    assert out["counts"]["withdraw_events"] == 1
    assert out["counts"]["withdraw_not_sold"] == 2
    assert out["documents"][0]["event_keys"] == ["sold-ok"]
    reasons = {s["event_key"]: s["skip_reason"] for s in out["skipped"]}
    assert reasons["in-transit"] == circ.SKIP_NOT_SOLD
    assert reasons["no-order"] == circ.SKIP_NOT_FBS
    assert any("выкуплен" in w for w in out["warnings"])


def test_return_not_cancelled_reason() -> None:
    assert circ._return_not_cancelled_reason({"operation_type": 1}) == ""
    assert circ._return_not_cancelled_reason(
        {"operation_type": 2, "order_id": None}
    ) == circ.SKIP_NOT_FBS
    # Linked FBS order is enough — Analytics op=2 is the return/PVZ signal.
    assert (
        circ._return_not_cancelled_reason(
            {
                "operation_type": 2,
                "order_id": 1,
                "order_wb_status": "sold",
                "order_status_label": "Выкуплен",
            }
        )
        == ""
    )
    assert (
        circ._return_not_cancelled_reason(
            {
                "operation_type": 2,
                "order_id": 1,
                "order_wb_status": "sorted",
                "order_supplier_status": "complete",
                "order_status_label": "В доставке",
            }
        )
        == ""
    )
    assert (
        circ._return_not_cancelled_reason(
            {
                "operation_type": 2,
                "order_id": 1,
                "order_wb_status": "canceled_by_client",
            }
        )
        == ""
    )


@patch(
    "review_processor.wb_kiz_circulation._close_deduped_prepare_events",
    return_value=0,
)
@patch(
    "review_processor.wb_kiz_circulation._load_sent_cis_identities",
    return_value=set(),
)
@patch(
    "review_processor.wb_kiz_circulation._attach_order_ids_to_events",
)
@patch("review_processor.wb_kiz_circulation.list_events_for_chz")
@patch(
    "review_processor.wb_kiz_circulation.repair_circulation_queue",
    return_value={"returns_fixed": 0, "withdraw_skipped": 0},
)
@patch("review_processor.wb_kiz_circulation.get_chz_settings")
def test_prepare_skips_return_unless_cancelled(
    mock_settings, _repair, mock_list, _attach, _sent, _close
) -> None:
    mock_settings.return_value = {
        "is_enabled": True,
        "participant_inn": "7707083893",
        "product_group": "lp",
        "kpp": "770701001",
        "fias_id": "fias-1",
        "return_type": "REMOTE_SALE_RETURN",
        "cert_thumbprint": "",
        "api_base": "prod",
        "api_base_url": PROD_BASE,
    }

    def _attach_side_effect(
        repo, *, user_id, source_id, events, api_key="", hydrate=False, refresh_statuses=False
    ):
        for ev in events:
            if ev.get("event_key") == "ret-ok":
                ev["order_id"] = 20
                ev["order_wb_status"] = "canceled_by_client"
                ev["order_status_label"] = "Отказ на ПВЗ"
            elif ev.get("event_key") == "ret-sold":
                ev["order_id"] = 21
                ev["order_wb_status"] = "sold"
                ev["order_status_label"] = "Выкуплен"
            elif ev.get("event_key") == "ret-delivery":
                ev["order_id"] = 22
                ev["order_wb_status"] = "sorted"
                ev["order_supplier_status"] = "complete"
                ev["order_status_label"] = "В доставке"

    _attach.side_effect = _attach_side_effect
    mock_list.return_value = [
        {
            "event_key": "ret-ok",
            "operation_type": 2,
            "excise_short": "cis-ret",
            "status": "pending",
        },
        {
            "event_key": "ret-sold",
            "operation_type": 2,
            "excise_short": "cis-sold",
            "status": "pending",
        },
        {
            "event_key": "ret-delivery",
            "operation_type": 2,
            "excise_short": "cis-del",
            "status": "pending",
        },
    ]
    out = circ.prepare_chz_batches(
        repo=object(), user_id=1, source_id=2, api_key="mp-key"
    )
    # All three are FBS-linked Analytics returns — all allowed.
    assert out["counts"]["return_events"] == 3
    assert out["counts"]["return_not_cancelled"] == 0
    all_keys = []
    for d in out["documents"]:
        all_keys.extend(d.get("event_keys") or [])
    assert set(all_keys) == {"ret-ok", "ret-sold", "ret-delivery"}

def test_related_events_index_lookup() -> None:
    idx: dict = {}
    circ._index_related_event(
        idx,
        {
            "id": 1,
            "event_key": "a",
            "operation_type": 1,
            "excise_short": "CIS1",
            "srid": "s1",
            "rid": "",
            "status": "pending",
            "fiscal_doc_number": "",
            "fiscal_dt": "",
        },
    )
    found = circ._related_from_index(
        idx,
        norm={
            "operation_type": 1,
            "excise_short": "CIS1",
            "srid": "s1",
            "rid": "",
        },
    )
    assert len(found) == 1
    assert found[0]["event_key"] == "a"
    assert (
        circ._related_from_index(
            idx,
            norm={"operation_type": 1, "excise_short": "OTHER", "srid": "s1"},
        )
        == []
    )


def test_heal_submitted_terminal_statuses() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur_fail = MagicMock()
    cur_fail.rowcount = 2
    cur_ok = MagicMock()
    cur_ok.rowcount = 1
    conn.execute.side_effect = [cur_fail, cur_ok]
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    with patch.object(circ, "ensure_kiz_circulation_tables"):
        out = circ.heal_submitted_terminal_statuses(repo, user_id=1, source_id=2)
    assert out["to_error"] == 2
    assert out["to_accepted"] == 1
    assert out["healed"] == 3


def test_norm_matches_fbs_by_srid_or_rid() -> None:
    keys = {"fbs-rid-1", "uid-9"}
    assert circ._norm_matches_fbs({"srid": "fbs-rid-1", "rid": ""}, keys)
    assert circ._norm_matches_fbs({"srid": "x", "rid": "uid-9"}, keys)
    assert not circ._norm_matches_fbs({"srid": "fbo-1", "rid": "fbo-1"}, keys)
    assert not circ._norm_matches_fbs({"srid": "fbs-rid-1", "rid": ""}, set())


def test_rid_match_is_case_insensitive() -> None:
    """Analytics often lowercases srid letters; Marketplace keeps ebX vs ebx."""
    index = {
        circ._rid_fold("ebX.r1460dd50de9f44e4beb8fb31a92baa92.0.0"): {
            "order_id": 5462672780,
            "wb_status": "sold",
            "supplier_status": "complete",
        }
    }
    assert (
        circ._norm_eligibility_skip(
            {
                "srid": "ebx.r1460dd50de9f44e4beb8fb31a92baa92.0.0",
                "operation_type": 1,
            },
            index,
        )
        == ""
    )
    assert circ._norm_matches_fbs(
        {"srid": "EBX.R1460DD50DE9F44E4BEB8FB31A92BAA92.0.0", "rid": ""},
        {"ebX.r1460dd50de9f44e4beb8fb31a92baa92.0.0"},
    )


def test_rid_match_keys_ignore_unit_suffix() -> None:
    """Analytics ``.1.0`` and Marketplace ``.0.0`` share mid + stem keys."""
    keys_a = set(circ._rid_match_keys("eI.i0a39f75abc.1.0"))
    keys_b = set(circ._rid_match_keys("eI.i0a39f75abc.0.0"))
    assert "i0a39f75abc" in keys_a & keys_b
    assert "ei.i0a39f75abc" in keys_a & keys_b
    assert circ._rid_mid_token("eI.i0a39f75abc.1.0") == "i0a39f75abc"
    assert circ._rid_stem("eI.i0a39f75abc.1.0") == "ei.i0a39f75abc"


def test_lookup_fbs_order_by_mid_when_suffix_differs() -> None:
    """Sold withdraw must match when only the trailing unit counter differs."""
    index: dict[str, dict] = {}
    for key in circ._rid_match_keys("eI.i0a39f75abc.0.0"):
        index[key] = {
            "order_id": 5462526777,
            "wb_status": "sold",
            "supplier_status": "complete",
        }
    hit = circ._lookup_fbs_order(
        {"srid": "eI.i0a39f75abc.1.0", "rid": "", "operation_type": 1},
        index,
    )
    assert hit is not None
    assert hit["order_id"] == 5462526777
    assert (
        circ._norm_eligibility_skip(
            {"srid": "eI.i0a39f75abc.1.0", "operation_type": 1},
            index,
        )
        == ""
    )
    assert circ._norm_matches_fbs(
        {"srid": "eI.i0a39f75abc.1.0", "rid": ""},
        {"eI.i0a39f75abc.0.0"},
    )


def test_order_ids_by_srids_uses_lower_sql() -> None:
    from review_processor import wb_fbs as wb_fbs_mod

    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    row = {
        "order_id": 5462672780,
        "rid": "ebX.r1460dd50de9f44e4beb8fb31a92baa92.0.0",
    }
    cur = MagicMock()
    cur.fetchall.return_value = [row]
    conn.execute.return_value = cur
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {}
    with patch.object(wb_fbs_mod, "ensure_wb_fbs_tables"):
        got = wb_fbs_mod.order_ids_by_srids(
            repo,
            user_id=1,
            source_id=2,
            srids=["ebx.r1460dd50de9f44e4beb8fb31a92baa92.0.0"],
        )
    assert got["ebx.r1460dd50de9f44e4beb8fb31a92baa92.0.0"] == 5462672780
    sql = conn.execute.call_args_list[0].args[0]
    assert "LOWER(rid)" in sql


def test_order_ids_by_srids_matches_mid_when_suffix_differs() -> None:
    """Full rid miss + mid-token hit: Analytics .1.0 vs Marketplace .0.0."""
    from review_processor import wb_fbs as wb_fbs_mod

    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    empty = MagicMock()
    empty.fetchall.return_value = []
    mid_hit = MagicMock()
    mid_hit.fetchall.return_value = [
        {
            "order_id": 5462526777,
            "order_uid": "i0a39f75abc",
            "rid": "eI.i0a39f75abc.0.0",
            "raw_json": "{}",
        }
    ]
    conn.execute.side_effect = [empty, mid_hit]
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {}
    with patch.object(wb_fbs_mod, "ensure_wb_fbs_tables"):
        got = wb_fbs_mod.order_ids_by_srids(
            repo,
            user_id=1,
            source_id=2,
            srids=["eI.i0a39f75abc.1.0"],
        )
    assert got["eI.i0a39f75abc.1.0"] == 5462526777
    mid_sql = conn.execute.call_args_list[1].args[0]
    assert "SPLIT_PART(rid" in mid_sql
    assert "order_uid" in mid_sql


def test_norm_eligibility_sold_and_cancelled_only() -> None:
    index = {
        "sold-1": {
            "order_id": 1,
            "wb_status": "sold",
            "supplier_status": "complete",
        },
        "ship-1": {
            "order_id": 2,
            "wb_status": "sorted",
            "supplier_status": "complete",
        },
        "cancel-1": {
            "order_id": 3,
            "wb_status": "canceled_by_client",
            "supplier_status": "cancel",
        },
        "pvz-1": {
            "order_id": 4,
            "wb_status": "sold",
            "supplier_status": "complete",
        },
    }
    assert (
        circ._norm_eligibility_skip(
            {"srid": "sold-1", "operation_type": 1}, index
        )
        == ""
    )
    assert (
        circ._norm_eligibility_skip(
            {"srid": "ship-1", "operation_type": 1}, index
        )
        == circ.SKIP_NOT_SOLD
    )
    # Return: any FBS link (Analytics op=2 = PVZ/return), not pre-delivery cancel gate.
    assert (
        circ._norm_eligibility_skip(
            {"srid": "pvz-1", "operation_type": 2}, index
        )
        == ""
    )
    assert (
        circ._norm_eligibility_skip(
            {"srid": "ship-1", "operation_type": 2}, index
        )
        == ""
    )
    assert (
        circ._norm_eligibility_skip(
            {"srid": "missing", "operation_type": 1}, index
        )
        == circ.SKIP_NOT_FBS
    )


def test_return_prepare_only_needs_fbs_link() -> None:
    assert (
        circ._return_not_cancelled_reason(
            {"operation_type": 2, "order_id": 10, "order_wb_status": "sold"}
        )
        == ""
    )
    assert circ._return_not_cancelled_reason(
        {"operation_type": 2, "order_id": None}
    ) == circ.SKIP_NOT_FBS
def test_repair_skip_non_fbs_noop_without_local_orders() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    count_row = {"n": 0}
    conn.execute.return_value.fetchone.return_value = count_row
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {"n": 0}
    with patch.object(circ, "ensure_kiz_circulation_tables"), patch(
        "review_processor.wb_fbs.ensure_wb_fbs_tables"
    ):
        assert circ.repair_skip_non_fbs_events(repo, user_id=1, source_id=2) == 0
    # Only the COUNT query — no mass UPDATE.
    assert conn.execute.call_count == 1


def test_repair_skip_non_fbs_updates_when_orders_exist() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    count_cur = MagicMock()
    count_cur.fetchone.return_value = {"n": 5}
    upd_cur = MagicMock()
    upd_cur.rowcount = 12
    conn.execute.side_effect = [count_cur, upd_cur]
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {"n": 5}
    with patch.object(circ, "ensure_kiz_circulation_tables"), patch(
        "review_processor.wb_fbs.ensure_wb_fbs_tables"
    ):
        assert circ.repair_skip_non_fbs_events(repo, user_id=1, source_id=2) == 12
    assert conn.execute.call_count == 2
    sql = conn.execute.call_args_list[1].args[0]
    assert "wb_fbs_orders" in sql
    assert circ.SKIP_NOT_FBS in conn.execute.call_args_list[1].args[1]

def test_purge_non_fbs_noop_without_local_orders() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.return_value.fetchone.return_value = {"n": 0}
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {"n": 0}
    with patch.object(circ, "ensure_kiz_circulation_tables"), patch(
        "review_processor.wb_fbs.ensure_wb_fbs_tables"
    ):
        assert circ.purge_non_fbs_circulation_events(repo, user_id=1, source_id=2) == 0
    assert conn.execute.call_count == 1


def test_purge_non_fbs_deletes_batches() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    count_cur = MagicMock()
    count_cur.fetchone.return_value = {"n": 10}
    del1 = MagicMock()
    del1.rowcount = circ.PURGE_BATCH_SIZE
    del2 = MagicMock()
    del2.rowcount = 3
    conn.execute.side_effect = [count_cur, del1, del2]
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {"n": 10}
    with patch.object(circ, "ensure_kiz_circulation_tables"), patch(
        "review_processor.wb_fbs.ensure_wb_fbs_tables"
    ):
        assert circ.purge_non_fbs_circulation_events(repo, user_id=1, source_id=2) == (
            circ.PURGE_BATCH_SIZE + 3
        )
    sql = conn.execute.call_args_list[1].args[0]
    assert "DELETE FROM wb_kiz_circulation_events" in sql
    # Only confirmed skipped+not_fbs without FBS match — never open pending via NOT EXISTS alone.
    assert "e.status = ?" in sql or "status = ?" in sql
    assert "updated_at < ?" in sql
    assert "NOT IN" not in sql.split("DELETE FROM wb_kiz_circulation_events")[1][:500]
    params = conn.execute.call_args_list[1].args[1]
    assert circ.STATUS_SKIPPED in params
    assert circ.SKIP_NOT_FBS in params


def test_repair_requeue_skipped_with_product_cost() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = MagicMock()
    cur.rowcount = 4
    conn.execute.return_value = cur
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    with patch.object(circ, "ensure_kiz_circulation_tables"):
        assert (
            circ.repair_requeue_skipped_with_product_cost(repo, user_id=1, source_id=2)
            == 4
        )
    sql = conn.execute.call_args.args[0]
    assert "price IS NOT NULL" in sql
    assert circ.SKIP_NO_PRODUCT_COST in conn.execute.call_args.args[1]


def test_preclose_empty_cis_events() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = MagicMock()
    cur.rowcount = 7
    conn.execute.return_value = cur
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    with patch.object(circ, "ensure_kiz_circulation_tables"):
        assert circ.preclose_empty_cis_events(repo, user_id=1, source_id=2) == 7
    assert circ.SKIP_EMPTY_CIS in conn.execute.call_args.args[1]


def test_request_cancel_excise_sync_sets_flag() -> None:
    circ._clear_sync_cancel(99)
    assert not circ._sync_cancel_requested(99)
    assert circ.request_cancel_excise_sync(99) is True
    assert circ._sync_cancel_requested(99)
    # Re-register must keep the cancel flag (race: Стоп before worker starts).
    circ._register_sync_cancel(99)
    assert circ._sync_cancel_requested(99)
    try:
        circ._check_sync_cancelled(99)
        raise AssertionError("expected SyncCancelled")
    except circ.SyncCancelled:
        pass
    finally:
        circ._clear_sync_cancel(99)


def test_abandon_orphan_closes_zombie_running_without_live_worker() -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    zombie = {
        "id": 13,
        "source_id": 13,
        "created_at": "2026-08-14T10:00:00+00:00",
        "log_text": "old",
    }
    conn.execute.return_value.fetchall.return_value = [zombie]
    conn.execute.return_value.fetchone.return_value = None
    repo = MagicMock()
    repo._connect.return_value = conn
    repo._sql.side_effect = lambda q: q
    repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {}
    circ._clear_sync_cancel(13)
    with patch.object(circ, "ensure_kiz_circulation_tables"), patch.object(
        circ, "_finish_run"
    ) as finish:
        closed = circ.abandon_orphan_excise_sync_runs(
            repo, user_id=1, source_id=13, grace_seconds=0
        )
        assert closed == [13]
        finish.assert_called_once()
        assert finish.call_args.kwargs["status"] == "cancelled"
        assert finish.call_args.kwargs["run_id"] == 13


def test_create_excise_sync_run_ignores_zombie_running() -> None:
    """Stuck DB running without live worker must not block a new sync."""
    circ._clear_sync_cancel(13)
    with patch.object(circ, "ensure_kiz_circulation_tables"), patch.object(
        circ,
        "abandon_orphan_excise_sync_runs",
        return_value=[13],
    ), patch.object(circ, "find_active_excise_sync_run", return_value=None), patch.object(
        circ, "resolve_excise_period", return_value={
            "date_from": "2026-08-01",
            "date_to": "2026-08-14",
            "days": 14,
        }
    ):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        conn.execute.return_value.fetchone.return_value = {"id": 42}
        repo = MagicMock()
        repo._connect.return_value = conn
        repo._sql.side_effect = lambda q: q
        repo._row_to_dict.side_effect = lambda r: r if isinstance(r, dict) else {}
        out = circ.create_excise_sync_run(
            repo,
            user_id=1,
            source_id=13,
            date_from="2026-08-01",
            date_to="2026-08-14",
        )
        assert out["run_id"] == 42
        assert out["status"] == "running"
        circ._clear_sync_cancel(42)


def test_create_excise_sync_run_reattaches_live_worker() -> None:
    """Second click while sync is live should reattach, not 400."""
    circ._register_sync_cancel(28)
    try:
        with patch.object(circ, "ensure_kiz_circulation_tables"), patch.object(
            circ, "abandon_orphan_excise_sync_runs", return_value=[]
        ), patch.object(
            circ,
            "find_active_excise_sync_run",
            return_value={
                "id": 28,
                "status": "running",
                "log_text": "[21:55:44] WB: выгрузка…",
            },
        ), patch.object(
            circ,
            "resolve_excise_period",
            return_value={
                "date_from": "2026-08-10",
                "date_to": "2026-08-16",
                "days": 7,
            },
        ):
            repo = MagicMock()
            out = circ.create_excise_sync_run(
                repo,
                user_id=1,
                source_id=13,
                date_from="2026-08-10",
                date_to="2026-08-16",
            )
        assert out["run_id"] == 28
        assert out["already_running"] is True
        assert out["async"] is True
        assert "выгрузка" in str(out.get("log") or "").lower() or "21:55" in str(
            out.get("log") or ""
        )
    finally:
        circ._clear_sync_cancel(28)


def test_portal_labels_match_seller_cabinet() -> None:
    from review_processor.wb_fbs import order_portal_status_label

    assert order_portal_status_label(wb_status="sold") == "Выкуплен"
    assert order_portal_status_label(wb_status="canceled_by_client") == "Отказ на ПВЗ"
    assert order_portal_status_label(wb_status="defect") == "Найдены дефекты"
