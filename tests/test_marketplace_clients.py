import unittest

from review_processor.service import MarketplaceSyncError, OzonMarketplaceClient, WildberriesMarketplaceClient


class _TestOzonClient(OzonMarketplaceClient):
    def __init__(self) -> None:
        super().__init__(
            api_url="https://api-seller.ozon.ru",
            client_id="cid-1",
            api_key="key-1",
            page_size=2,
            max_pages=5,
        )
        self.calls = 0

    def _request_json(self, *, path: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            return {
                "result": {
                    "reviews": [
                        {"id": "r1", "text": "good", "rating": 5},
                        {"id": "r2", "text": "bad", "rating": 1},
                    ],
                    "last_id": "cursor-1",
                    "has_next": True,
                }
            }
        return {
            "result": {
                "reviews": [
                    {"id": "r3", "text": "ok", "rating": 3},
                ],
                "last_id": "cursor-2",
                "has_next": False,
            }
        }


class _TestWbClient(WildberriesMarketplaceClient):
    def __init__(self) -> None:
        super().__init__(
            api_url="https://feedbacks-api.wildberries.ru/api/v1/feedbacks",
            api_key="token-1",
            page_size=2,
            max_pages=5,
        )
        self.calls = 0

    def _request_json(self, *, skip: int, take: int) -> dict[str, object]:
        self.calls += 1
        if skip == 0:
            return {
                "data": {
                    "feedbacks": [
                        {"id": "w1", "text": "доставка ок", "productValuation": 5},
                        {"id": "w2", "text": "товар брак", "productValuation": 1},
                    ]
                }
            }
        return {"data": {"feedbacks": [{"id": "w3", "text": "норм", "productValuation": 4}]}}


class MarketplaceClientsTests(unittest.TestCase):
    def test_ozon_client_paginates(self) -> None:
        client = _TestOzonClient()
        reviews = client.fetch_reviews()
        self.assertEqual(len(reviews), 3)
        self.assertEqual(reviews[0].review_id, "r1")
        self.assertEqual(reviews[-1].review_id, "r3")

    def test_wildberries_client_paginates(self) -> None:
        client = _TestWbClient()
        reviews = client.fetch_reviews()
        self.assertEqual(len(reviews), 3)
        self.assertEqual(reviews[1].review_id, "w2")
        self.assertEqual(reviews[1].rating, 1)

    def test_ozon_client_detects_error_payload(self) -> None:
        class _ErrorOzon(_TestOzonClient):
            def _request_json(self, *, path: str, payload: dict[str, object]) -> dict[str, object]:
                return {"result": {"error": "token expired"}}

        with self.assertRaises(MarketplaceSyncError):
            _ErrorOzon().fetch_reviews()

    def test_wb_client_detects_error_payload(self) -> None:
        class _ErrorWb(_TestWbClient):
            def _request_json(self, *, skip: int, take: int) -> dict[str, object]:
                return {"error": "forbidden"}

        with self.assertRaises(MarketplaceSyncError):
            _ErrorWb().fetch_reviews()

    def test_wb_date_from_is_converted_to_unix_timestamp(self) -> None:
        self.assertEqual(WildberriesMarketplaceClient._to_wb_unix_timestamp("2026-04-27"), 1777248000)
        self.assertEqual(WildberriesMarketplaceClient._to_wb_unix_timestamp("1745712000"), 1745712000)
        self.assertIsNone(WildberriesMarketplaceClient._to_wb_unix_timestamp("bad-date"))

    def test_wb_to_conversation_supports_chat_id_uppercase(self) -> None:
        mapped = WildberriesMarketplaceClient._to_conversation(
            {
                "chatID": "chat-upper-1",
                "clientName": "Клиент",
                "lastMessage": {"text": "Здравствуйте", "addTimestamp": "2026-05-03T21:30:00Z"},
            },
            kind="chat",
        )
        self.assertIsNotNone(mapped)
        assert mapped is not None
        self.assertEqual(mapped["external_id"], "chat-upper-1")
        self.assertEqual(mapped["customer_name"], "Клиент")
        self.assertEqual(mapped["message_text"], "Здравствуйте")

    def test_wb_chat_payload_with_dialog_keys_is_mapped(self) -> None:
        payload = {
            "result": {
                "chats": [
                    {
                        "dialogId": "dlg-1",
                        "lastMessage": {"text": "Добрый день", "createdAt": "2026-05-03T10:00:00Z"},
                        "unreadCount": 2,
                    }
                ]
            }
        }

        class _ChatClient(WildberriesMarketplaceClient):
            def __init__(self) -> None:
                super().__init__(
                    api_url="https://feedbacks-api.wildberries.ru/api/v1/feedbacks",
                    api_key="token",
                    chats_api_url="https://buyer-chat-api.wildberries.ru",
                    chats_path="/api/v1/seller/chats",
                )

            def _fetch_conversation_endpoint(
                self,
                *,
                path: str,
                kind: str,
                base_url: str | None = None,
                since_date: str | None = None,
                single_request: bool = False,
                stop_requested: object = None,
            ) -> list[dict[str, object]]:
                _ = path, kind, base_url, stop_requested
                rows = payload["result"]["chats"]
                mapped: list[dict[str, object]] = []
                for row in rows:
                    item = self._to_conversation(row, kind="chat")
                    if item:
                        mapped.append(item)
                return mapped

        client = _ChatClient()
        rows = client.fetch_chats()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "dlg-1")
        self.assertEqual(rows[0]["message_text"], "Добрый день")
        self.assertEqual(rows[0]["unread_count"], 2)


if __name__ == "__main__":
    unittest.main()
