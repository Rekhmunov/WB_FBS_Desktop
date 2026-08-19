"""Unit tests for live cancelled-orders check in a WB FBS supply."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from review_processor.wb_fbs_detail import list_supply_cancelled_orders


def test_list_supply_cancelled_orders_finds_canceled_by_client() -> None:
    client = MagicMock()
    client.get_supply_order_ids.return_value = [5440959209, 11, 5443002750]
    client.get_statuses.return_value = [
        {"id": 5440959209, "supplierStatus": "confirm", "wbStatus": "canceled_by_client"},
        {"id": 11, "supplierStatus": "confirm", "wbStatus": "waiting"},
        {"id": 5443002750, "supplierStatus": "confirm", "wbStatus": "canceled_by_client"},
    ]
    local_orders = [
        {
            "order_id": 5440959209,
            "article": "ART-1",
            "product_name": "Товар 1",
            "product_photo": "https://img/1.jpg",
            "barcodes": ["2000000000001"],
            "created_at_wb": "2026-08-01T10:00:00Z",
            "brand": "",
            "nm_id": 100,
        },
        {
            "order_id": 5443002750,
            "article": "ART-2",
            "product_name": "Товар 2",
            "product_photo": "",
            "barcodes": ["2000000000002"],
            "created_at_wb": "2026-08-02T10:00:00Z",
            "brand": "Brand",
            "nm_id": 200,
        },
    ]
    with (
        patch("review_processor.wb_fbs_detail.wb.WbFbsClient", return_value=client),
        patch("review_processor.wb_fbs_detail._cache_get_detail", return_value=None),
        patch(
            "review_processor.wb_fbs_detail._local_order_ids_for_supply",
            return_value=[],
        ),
        patch(
            "review_processor.wb_fbs_detail._load_local_orders",
            return_value=local_orders,
        ),
        patch(
            "review_processor.wb_fbs_detail._fetch_stickers_map",
            return_value={
                5440959209: {"partA": "111", "partB": "2222"},
                5443002750: {"partA": "333", "partB": "4444"},
            },
        ),
        patch(
            "review_processor.wb_fbs_detail.wb.update_order_wb_statuses",
            return_value=2,
        ) as mock_persist,
    ):
        payload = list_supply_cancelled_orders(
            MagicMock(),
            user_id=1,
            source_id=2,
            api_key="key",
            supply_id="S1",
        )

    assert payload["ok"] is True
    assert payload["order_count"] == 3
    assert payload["cancelled_count"] == 2
    assert [r["order_id"] for r in payload["rows"]] == [5440959209, 5443002750]
    assert payload["rows"][0]["cancel_reason_label"] == "Отказ на ПВЗ"
    assert payload["rows"][0]["sticker_number"] == "1112222"
    assert payload["rows"][0]["barcodes"] == ["2000000000001"]
    assert payload["rows"][1]["brand"] == "Brand"
    client.get_statuses.assert_called_once_with([5440959209, 11, 5443002750])
    mock_persist.assert_called_once()
    persisted = mock_persist.call_args.kwargs["statuses"]
    assert 5440959209 in persisted
    assert 5443002750 in persisted
    assert 11 not in persisted


def test_list_supply_cancelled_orders_empty_when_none() -> None:
    client = MagicMock()
    client.get_supply_order_ids.return_value = [11]
    client.get_statuses.return_value = [
        {"id": 11, "supplierStatus": "confirm", "wbStatus": "waiting"},
    ]
    with (
        patch("review_processor.wb_fbs_detail.wb.WbFbsClient", return_value=client),
        patch("review_processor.wb_fbs_detail._cache_get_detail", return_value=None),
        patch(
            "review_processor.wb_fbs_detail._local_order_ids_for_supply",
            return_value=[],
        ),
        patch(
            "review_processor.wb_fbs_detail._load_local_orders",
            return_value=[],
        ),
        patch(
            "review_processor.wb_fbs_detail._fetch_stickers_map",
            return_value={},
        ),
        patch(
            "review_processor.wb_fbs_detail.wb.update_order_wb_statuses",
            return_value=0,
        ) as mock_persist,
    ):
        payload = list_supply_cancelled_orders(
            MagicMock(),
            user_id=1,
            source_id=2,
            api_key="key",
            supply_id="S1",
        )
    assert payload["cancelled_count"] == 0
    assert payload["rows"] == []
    mock_persist.assert_not_called()


def test_list_supply_cancelled_orders_raises_on_status_failure() -> None:
    client = MagicMock()
    client.get_supply_order_ids.return_value = [11]
    client.get_statuses.side_effect = RuntimeError("wb down")
    with (
        patch("review_processor.wb_fbs_detail.wb.WbFbsClient", return_value=client),
        patch("review_processor.wb_fbs_detail._cache_get_detail", return_value=None),
        patch(
            "review_processor.wb_fbs_detail._local_order_ids_for_supply",
            return_value=[],
        ),
        pytest.raises(RuntimeError, match="Не удалось проверить статусы"),
    ):
        list_supply_cancelled_orders(
            MagicMock(),
            user_id=1,
            source_id=2,
            api_key="key",
            supply_id="S1",
        )


def test_list_supply_cancelled_orders_raises_on_partial_statuses() -> None:
    client = MagicMock()
    client.get_supply_order_ids.return_value = [11, 22]
    client.get_statuses.return_value = [
        {"id": 11, "supplierStatus": "confirm", "wbStatus": "waiting"},
    ]
    with (
        patch("review_processor.wb_fbs_detail.wb.WbFbsClient", return_value=client),
        patch("review_processor.wb_fbs_detail._cache_get_detail", return_value=None),
        patch(
            "review_processor.wb_fbs_detail._local_order_ids_for_supply",
            return_value=[],
        ),
        pytest.raises(RuntimeError, match="не вернул статусы для 1"),
    ):
        list_supply_cancelled_orders(
            MagicMock(),
            user_id=1,
            source_id=2,
            api_key="key",
            supply_id="S1",
        )


def test_list_supply_cancelled_orders_chunks_statuses() -> None:
    client = MagicMock()
    ids = list(range(1, 1002))
    client.get_supply_order_ids.return_value = ids

    def _statuses(chunk: list[int]) -> list[dict]:
        return [
            {"id": oid, "supplierStatus": "confirm", "wbStatus": "waiting"}
            for oid in chunk
        ]

    client.get_statuses.side_effect = _statuses
    with (
        patch("review_processor.wb_fbs_detail.wb.WbFbsClient", return_value=client),
        patch("review_processor.wb_fbs_detail._cache_get_detail", return_value=None),
        patch(
            "review_processor.wb_fbs_detail._local_order_ids_for_supply",
            return_value=[],
        ),
        patch(
            "review_processor.wb_fbs_detail._load_local_orders",
            return_value=[],
        ),
        patch(
            "review_processor.wb_fbs_detail._fetch_stickers_map",
            return_value={},
        ),
        patch("review_processor.wb_fbs_detail.time.sleep"),
        patch(
            "review_processor.wb_fbs_detail.wb.update_order_wb_statuses",
            return_value=0,
        ),
    ):
        payload = list_supply_cancelled_orders(
            MagicMock(),
            user_id=1,
            source_id=2,
            api_key="key",
            supply_id="S1",
        )
    assert payload["cancelled_count"] == 0
    assert client.get_statuses.call_count == 2
    assert len(client.get_statuses.call_args_list[0].args[0]) == 1000
    assert len(client.get_statuses.call_args_list[1].args[0]) == 1
