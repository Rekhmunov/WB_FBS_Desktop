import os
import tempfile
import unittest
from unittest import mock

from review_processor.models import ReviewInput
from review_processor.repository import ReviewRepository
from review_processor.service import MarketplaceSyncError, ReviewAutomationService


class _StubClient:
    def fetch_reviews(self) -> list[ReviewInput]:
        return [
            ReviewInput(
                review_id="ext-1",
                text="Отличный товар, хорошее качество",
                author="Client A",
                rating=5,
            ),
            ReviewInput(
                review_id="ext-2",
                text="Ужасно, задержали доставку и курьер опоздал",
                author="Client B",
                rating=1,
            ),
        ]


class _FailClient:
    def fetch_reviews(self) -> list[ReviewInput]:
        raise MarketplaceSyncError("wb", "invalid token")


class _RecoClient:
    def fetch_reviews(self, *args: object, **kwargs: object) -> list[ReviewInput]:
        _ = args, kwargs
        return [
            ReviewInput(
                review_id="ext-reco-1",
                text="Отличный товар, все понравилось",
                author=None,
                rating=5,
                metadata={"article": "A-1"},
            )
        ]


class _PositiveDeliveryClient:
    def fetch_reviews(self, *args: object, **kwargs: object) -> list[ReviewInput]:
        _ = args, kwargs
        return [
            ReviewInput(
                review_id="ext-delivery-1",
                text="Спасибо, отличная доставка и быстрый курьер",
                author="Client C",
                rating=5,
            )
        ]


class _TextlessClient:
    def fetch_reviews(self, *args: object, **kwargs: object) -> list[ReviewInput]:
        _ = args, kwargs
        return [
            ReviewInput(
                review_id="ext-textless-1",
                text="",
                author="Client D",
                rating=5,
                metadata={},
            )
        ]


class _TextlessLowRatingClient:
    def fetch_reviews(self, *args: object, **kwargs: object) -> list[ReviewInput]:
        _ = args, kwargs
        return [
            ReviewInput(
                review_id="ext-textless-2",
                text="",
                author="Client E",
                rating=2,
                metadata={},
            )
        ]


class _ConversationReplyClient:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    def send_conversation_reply(self, *, conversation: dict[str, object], response_text: str) -> bool:
        _ = conversation, response_text
        if self.should_fail:
            raise MarketplaceSyncError("wb", "reply timeout")
        return True


class _QuestionFailClient:
    def fetch_reviews(self, *args: object, **kwargs: object) -> list[ReviewInput]:
        _ = args, kwargs
        return []

    def fetch_questions(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
        _ = args, kwargs
        raise MarketplaceSyncError("wb", "wb HTTP error 403: forbidden for questions")

    def fetch_chats(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
        _ = args, kwargs
        return [
            {
                "external_id": "chat-only-1",
                "kind": "chat",
                "customer_name": "User",
                "message_text": "Здравствуйте",
                "status": "open",
                "unread_count": 1,
                "metadata": {},
                "last_message_at": None,
            }
        ]


def _fake_yandex_category(review: ReviewInput) -> str:
    text = (review.text or "").lower()
    has_negative = any(word in text for word in ("ужас", "плох", "брак", "некачеств", "слом", "задерж", "опозд"))
    if any(word in text for word in ("размер", "маломер", "большемер", "мерит")):
        return "wrong_size"
    if has_negative and any(word in text for word in ("курьер", "достав", "пвз")):
        return "delivery_problems"
    if has_negative:
        return "product_dissatisfaction"
    return "positive"


class ReviewAutomationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = temp.name
        temp.close()
        self.addCleanup(lambda: os.path.exists(self.db_path) and os.unlink(self.db_path))
        self.repository = ReviewRepository(db_path=self.db_path)
        self.service = ReviewAutomationService(repository=self.repository)
        self.service._classify_with_yandex = mock.Mock(
            side_effect=lambda review, settings, strict=False, allowed_groups=None: _fake_yandex_category(review)
        )
        self.service._classify_with_yandex_target = mock.Mock(
            side_effect=lambda review, settings, user_id, strict=False: (_fake_yandex_category(review), "Общий позитив")
        )
        self.user = self.repository.create_user(email="owner@example.com", password_hash="hash", role="admin")

    def test_sync_and_list_reviews(self) -> None:
        loaded = self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_StubClient(),
        )
        self.assertEqual(loaded, 2)

        reviews = self.service.list_reviews(user_id=int(self.user["id"]))
        self.assertEqual(len(reviews), 2)
        categories = {row["category"] for row in reviews}
        self.assertIn("delivery_problems", categories)
        self.assertIn("positive", categories)

    def test_queue_manual_and_manual_reply(self) -> None:
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_StubClient(),
        )
        review = self.repository.list_reviews(user_id=int(self.user["id"]), category="delivery_problems", limit=1)[0]

        queued = self.service.queue_for_manual_processing(user_id=int(self.user["id"]), review_uid=review["review_uid"])
        self.assertTrue(queued)

        updated = self.service.save_manual_reply(
            user_id=int(self.user["id"]),
            review_uid=review["review_uid"],
            operator_name="operator-1",
            response_text="Проблему решили, проверьте заказ.",
        )
        self.assertTrue(updated)

        updated_review = self.repository.get_review(user_id=int(self.user["id"]), review_uid=review["review_uid"])
        self.assertIsNotNone(updated_review)
        self.assertEqual(updated_review["status"], "answered_manual")
        self.assertEqual(updated_review["operator_name"], "operator-1")

    def test_auto_reply_marks_review(self) -> None:
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="positive",
            action_mode="template",
            auto_send=True,
        )
        self.repository.replace_subgroup_templates(
            user_id=int(self.user["id"]),
            group_id="positive",
            subgroup="Общий позитив",
            templates=["Спасибо, {author}! Рады, что товар понравился."],
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_StubClient(),
        )
        positive = self.repository.list_reviews(user_id=int(self.user["id"]), category="positive", limit=1)[0]
        self.assertEqual(positive["status"], "answered_auto")
        self.assertIn("Спасибо", positive["auto_reply"] or "")

        reply = self.service.generate_auto_reply(user_id=int(self.user["id"]), review_uid=positive["review_uid"])
        self.assertIn("Спасибо", reply)

    def test_manual_template_routes_negative_to_operator(self) -> None:
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="delivery_problems",
            action_mode="manual",
            auto_send=False,
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_StubClient(),
        )
        negative = self.repository.list_reviews(user_id=int(self.user["id"]), category="delivery_problems", limit=1)[0]
        self.assertEqual(negative["status"], "queued_for_operator")

    def test_template_mode_without_matching_template_falls_back_to_new_reviews(self) -> None:
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="delivery_problems",
            action_mode="template",
            auto_send=True,
        )
        self.repository.replace_subgroup_templates(
            user_id=int(self.user["id"]),
            group_id="delivery_problems",
            subgroup="Долгая доставка",
            templates=[],
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_StubClient(),
        )
        negative = self.repository.list_reviews(user_id=int(self.user["id"]), category="delivery_problems", limit=1)[0]
        self.assertEqual(negative["status"], "queued_for_operator")
        self.assertFalse(bool(negative.get("auto_reply")))

    def test_sync_all_accounts_collects_errors_and_logs(self) -> None:
        self.repository.create_marketplace_account(
            user_id=int(self.user["id"]),
            marketplace="mock",
            account_name="ok-account",
            api_url="https://example.local/api/reviews",
            api_key=None,
            extra={},
        )
        self.repository.create_marketplace_account(
            user_id=int(self.user["id"]),
            marketplace="wb",
            account_name="bad-account",
            api_url="https://feedbacks-api.wildberries.ru/api/v1/feedbacks",
            api_key="bad-token",
            extra={},
        )

        def _client_for(account: dict[str, object]) -> object:
            if str(account["marketplace"]) == "mock":
                return _StubClient()
            return _FailClient()

        with mock.patch.object(self.service, "_build_client", side_effect=_client_for):
            result = self.service.sync_all_accounts(user_id=int(self.user["id"]))

        self.assertEqual(result["accounts"], 2)
        self.assertEqual(result["success_accounts"], 1)
        self.assertEqual(result["failed_accounts"], 1)
        self.assertEqual(result["loaded"], 2)
        self.assertEqual(len(result["errors"]), 1)

        actions, _total = self.repository.list_recent_actions(user_id=int(self.user["id"]), limit=20)
        self.assertTrue(any(item["action_type"] == "sync_error" for item in actions))

    def test_sync_all_accounts_can_use_specific_account_ids(self) -> None:
        first = self.repository.create_marketplace_account(
            user_id=int(self.user["id"]),
            marketplace="mock",
            account_name="first-account",
            api_url="https://example.local/api/reviews",
            api_key=None,
            extra={},
        )
        second = self.repository.create_marketplace_account(
            user_id=int(self.user["id"]),
            marketplace="mock",
            account_name="second-account",
            api_url="https://example.local/api/reviews",
            api_key=None,
            extra={},
        )
        target_ids = [int(first["id"])]

        def _client_for(_account: dict[str, object]) -> object:
            return _StubClient()

        with mock.patch.object(self.service, "_build_client", side_effect=_client_for):
            result = self.service.sync_all_accounts(user_id=int(self.user["id"]), account_ids=target_ids)

        self.assertEqual(result["accounts"], 1)
        self.assertEqual(result["success_accounts"], 1)
        self.assertEqual(result["failed_accounts"], 0)
        self.assertEqual(result["loaded"], 2)
        self.assertEqual(result["loaded_conversations"], 0)
        self.assertEqual(result["account_ids"], target_ids)
        self.assertEqual(result["skipped_accounts"], 0)
        self.assertEqual(int(second["id"]) in result["account_ids"], False)

    def test_sync_all_accounts_fail_soft_by_channels(self) -> None:
        account = self.repository.create_marketplace_account(
            user_id=int(self.user["id"]),
            marketplace="wb",
            account_name="wb-chat-only",
            api_url="https://feedbacks-api.wildberries.ru/api/v1/feedbacks",
            api_key="token",
            extra={"questions_path": "/api/v1/questions", "chats_path": "/api/v1/chats"},
        )
        full_account = self.repository.get_marketplace_account(
            user_id=int(self.user["id"]),
            account_id=int(account["id"]),
            include_secrets=True,
        )
        self.assertIsNotNone(full_account)
        with mock.patch.object(self.service, "_build_client", return_value=_QuestionFailClient()):
            result = self.service.sync_all_accounts(user_id=int(self.user["id"]), account_ids=[int(account["id"])])

        self.assertEqual(result["accounts"], 1)
        self.assertEqual(result["success_accounts"], 1)
        self.assertEqual(result["loaded_reviews"], 0)
        self.assertEqual(result["loaded_questions"], 0)
        self.assertEqual(result["loaded_chats"], 1)
        self.assertEqual(result["loaded_conversations"], 1)
        self.assertGreaterEqual(result["failed_accounts"], 1)
        errors = result.get("errors") or []
        self.assertTrue(any(str(item.get("channel")) == "questions" for item in errors if isinstance(item, dict)))
        stats = result.get("account_channel_stats") or []
        self.assertEqual(len(stats), 1)
        first = stats[0]
        self.assertTrue(bool((first.get("chats") or {}).get("ok")))
        self.assertFalse(bool((first.get("questions") or {}).get("ok")))

    def test_build_wb_client_sets_questions_endpoint_by_default(self) -> None:
        account = self.repository.create_marketplace_account(
            user_id=int(self.user["id"]),
            marketplace="wb",
            account_name="wb-defaults",
            api_url="https://feedbacks-api.wildberries.ru/api/v1/feedbacks",
            api_key="token",
            extra={},
        )
        full_account = self.repository.get_marketplace_account(
            user_id=int(self.user["id"]),
            account_id=int(account["id"]),
            include_secrets=True,
        )
        self.assertIsNotNone(full_account)
        client = self.service._build_client(full_account or {})
        self.assertEqual(getattr(client, "questions_path", None), "/api/v1/questions")
        self.assertEqual(getattr(client, "chats_path", None), "/api/v1/seller/chats")
        self.assertEqual(getattr(client, "chats_api_url", None), "https://buyer-chat-api.wildberries.ru")

    def test_template_placeholders_user_reco_brand(self) -> None:
        self.repository.replace_all_recommendations(
            user_id=int(self.user["id"]),
            rows=[{"source_article": "A-1", "target_articles": ["R-55"]}],
        )
        self.repository.upsert_template(
            user_id=int(self.user["id"]),
            category="positive",
            mode="auto",
            template_text="%USER%, спасибо за отзыв! Попробуйте %RECO%. С уважением, %BRAND%.",
            is_enabled=True,
        )
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="positive",
            action_mode="template",
            auto_send=True,
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_RecoClient(),
        )
        review = self.repository.list_reviews(user_id=int(self.user["id"]), category="positive", limit=1)[0]
        reply = self.service.generate_auto_reply(user_id=int(self.user["id"]), review_uid=review["review_uid"])
        self.assertNotIn("%USER%", reply)
        self.assertNotIn("%RECO%", reply)
        self.assertNotIn("%BRAND%", reply)
        self.assertIn("R-55", reply)
        self.assertIn("VarFabric", reply)
        self.assertFalse(reply.startswith(","))
        self.assertNotIn(",!", reply)

    def test_manual_mode_queues_new_review(self) -> None:
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="positive",
            action_mode="manual",
            auto_send=False,
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_PositiveDeliveryClient(),
        )
        queued = self.repository.list_reviews(user_id=int(self.user["id"]), limit=1)[0]
        self.assertEqual(queued["status"], "queued_for_operator")

    def test_positive_delivery_uses_delivery_subgroup_template(self) -> None:
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="positive",
            action_mode="template",
            auto_send=True,
        )
        self.repository.replace_subgroup_templates(
            user_id=int(self.user["id"]),
            group_id="positive",
            subgroup="Позитив доставка",
            templates=["ТЕСТ: спасибо за быструю доставку!"],
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_PositiveDeliveryClient(),
        )
        review = self.repository.list_reviews(user_id=int(self.user["id"]), limit=1)[0]
        self.assertEqual(review["status"], "answered_auto")
        self.assertIn("быструю доставку", str(review.get("auto_reply") or ""))

    def test_classification_group_without_subgroup_falls_back_to_general(self) -> None:
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="wrong_size",
            action_mode="template",
            auto_send=True,
        )
        self.repository.replace_subgroup_templates(
            user_id=int(self.user["id"]),
            group_id="wrong_size",
            subgroup="Общий",
            templates=["ТЕСТ: общий шаблон wrong_size"],
        )
        self.service._classify_with_yandex_target = mock.Mock(
            return_value=("wrong_size", "Несуществующая подгруппа")
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_StubClient(),
        )
        review = self.repository.list_reviews(user_id=int(self.user["id"]), category="wrong_size", limit=1)[0]
        self.assertEqual(review["status"], "answered_auto")
        self.assertIn("общий шаблон wrong_size", str(review.get("auto_reply") or ""))
        self.assertEqual(str((review.get("metadata") or {}).get("classified_subgroup") or ""), "Общий")

    def test_textless_without_tags_routes_to_textless_group(self) -> None:
        review = ReviewInput(review_id="r-empty", text="", rating=5, metadata={})
        processed = self.service.processor.process(review)
        category = self.service._classify_category(review, processed, settings={"provider": "rules"})
        group_id = self.service._resolve_template_group_id(category=category, review=review, sentiment=processed.sentiment_label)
        subgroup = self.service._resolve_template_subgroup(group_id=group_id or "", category=category, review=review)
        self.assertEqual(group_id, "textless_ratings")
        self.assertEqual(subgroup, "5 звезд")

    def test_textless_with_tags_routes_to_rating_group(self) -> None:
        review = ReviewInput(
            review_id="r-tags",
            text="",
            rating=4,
            metadata={"review_tags": ["Быстрая доставка", "Хорошее качество"]},
        )
        processed = self.service.processor.process(review)
        category = self.service._classify_category(review, processed, settings={"provider": "rules"})
        group_id = self.service._resolve_template_group_id(category=category, review=review, sentiment=processed.sentiment_label)
        subgroup = self.service._resolve_template_subgroup(group_id=group_id or "", category=category, review=review)
        self.assertEqual(group_id, "textless_ratings")
        self.assertEqual(subgroup, "4 звезды")

    def test_neutral_text_with_size_hint_routes_to_wrong_size(self) -> None:
        review = ReviewInput(
            review_id="r-size",
            text="Классное белье, но плохо застегается молния и мломерит пододеяльник.",
            rating=4,
        )
        processed = self.service.processor.process(review)
        category = self.service._classify_category(review, processed, settings={"provider": "rules"})
        group_id = self.service._resolve_template_group_id(category=category, review=review, sentiment=processed.sentiment_label)
        subgroup = self.service._resolve_template_subgroup(group_id=group_id or "", category=category, review=review)
        self.assertIn(category, {"wrong_size", "delivery_problems", "product_dissatisfaction", "positive"})
        self.assertEqual(group_id, "wrong_size")
        self.assertEqual(subgroup, "Большемерит/маломерит")

    def test_text_review_without_yandex_configuration_routes_to_manual_queue(self) -> None:
        raw_service = ReviewAutomationService(repository=self.repository)
        loaded = raw_service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_StubClient(),
        )
        self.assertEqual(loaded, 2)
        reviews = self.repository.list_reviews(user_id=int(self.user["id"]), category="ai_unclassified", limit=10)
        self.assertEqual(len(reviews), 2)
        for row in reviews:
            metadata = row.get("metadata") or {}
            self.assertEqual(str(row.get("status") or ""), "queued_for_operator")
            self.assertEqual(str(metadata.get("ai_classification_status") or ""), "failed")

    def test_textless_rating_uses_5_stars_subgroup_template(self) -> None:
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="textless_ratings",
            action_mode="template",
            auto_send=True,
        )
        self.repository.replace_subgroup_templates(
            user_id=int(self.user["id"]),
            group_id="textless_ratings",
            subgroup="5 звезд",
            templates=["ТЕСТ: шаблон для 5 звезд"],
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_TextlessClient(),  # rating=5
        )
        review = self.repository.list_reviews(user_id=int(self.user["id"]), category="textless_ratings", limit=1)[0]
        self.assertEqual(review["status"], "answered_auto")
        self.assertIn("5 звезд", str((review.get("metadata") or {}).get("classified_subgroup") or ""))
        self.assertIn("5", str(review.get("auto_reply") or ""))

    def test_textless_rating_uses_2_stars_subgroup_template(self) -> None:
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="textless_ratings",
            action_mode="template",
            auto_send=True,
        )
        self.repository.replace_subgroup_templates(
            user_id=int(self.user["id"]),
            group_id="textless_ratings",
            subgroup="2 звезды",
            templates=["ТЕСТ: шаблон для 2 звезд"],
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_TextlessLowRatingClient(),  # rating=2
        )
        review = self.repository.list_reviews(user_id=int(self.user["id"]), category="textless_ratings", limit=1)[0]
        self.assertEqual(review["status"], "answered_auto")
        self.assertIn("2 звезды", str((review.get("metadata") or {}).get("classified_subgroup") or ""))
        self.assertIn("2", str(review.get("auto_reply") or ""))

    def test_group_without_subgroup_fallbacks_to_general_subgroup(self) -> None:
        self.service._classify_with_yandex_target = mock.Mock(return_value=("delivery_problems", None))
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="delivery_problems",
            action_mode="template",
            auto_send=True,
        )
        self.repository.replace_subgroup_templates(
            user_id=int(self.user["id"]),
            group_id="delivery_problems",
            subgroup="Общий",
            templates=["ТЕСТ: общий шаблон для доставки"],
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_StubClient(),
        )
        review = self.repository.list_reviews(user_id=int(self.user["id"]), category="delivery_problems", limit=1)[0]
        metadata = review.get("metadata") or {}
        self.assertEqual(review["status"], "answered_auto")
        self.assertEqual(str(metadata.get("classified_subgroup") or ""), "Общий")
        self.assertIn("общий шаблон", str(review.get("auto_reply") or "").lower())

    def test_unclassified_ai_result_routes_review_to_manual_with_note(self) -> None:
        self.service._classify_with_yandex_target = mock.Mock(return_value=("", None))
        self.repository.upsert_processing_rule(
            user_id=int(self.user["id"]),
            group_id="delivery_problems",
            action_mode="template",
            auto_send=True,
        )
        self.service.sync_reviews(
            user_id=int(self.user["id"]),
            source="test-market",
            account_id=None,
            client=_StubClient(),
        )
        review = self.repository.list_reviews(user_id=int(self.user["id"]), category="ai_unclassified", limit=1)[0]
        metadata = review.get("metadata") or {}
        self.assertEqual(review["status"], "queued_for_operator")
        self.assertEqual(str(metadata.get("ai_classification_status") or ""), "failed")
        self.assertIn("ИИ не смог корректно определить категорию", str(metadata.get("ai_classification_note") or ""))

    def test_send_conversation_reply_success_marks_waiting_and_logs(self) -> None:
        account = self.repository.create_marketplace_account(
            user_id=int(self.user["id"]),
            marketplace="wb",
            account_name="WB chat",
            api_url="https://feedbacks-api.wildberries.ru/api/v1/feedbacks",
            api_key="token",
            extra={},
        )
        conv_uid = self.repository.upsert_conversation(
            user_id=int(self.user["id"]),
            source="wb",
            account_id=int(account["id"]),
            external_conversation_id="chat-success-1",
            kind="chat",
            customer_name="Buyer Chat",
            message_text="Есть в наличии?",
            status="open",
            unread_count=1,
            metadata={},
        )
        with mock.patch.object(self.service, "_build_client", return_value=_ConversationReplyClient(should_fail=False)):
            result = self.service.send_conversation_reply(
                user_id=int(self.user["id"]),
                conversation_uid=conv_uid,
                response_text="Да, есть в наличии.",
                operator_name="operator@example.com",
                idempotency_key="idem-success-1",
            )
        self.assertTrue(bool(result.get("ok")))
        self.assertEqual(str(result.get("status")), "sent")
        conv = self.repository.get_conversation(user_id=int(self.user["id"]), conversation_uid=conv_uid)
        self.assertIsNotNone(conv)
        self.assertEqual(str(conv.get("status")), "waiting")
        self.assertEqual(int(conv.get("send_attempts") or 0), 0)
        messages = self.repository.list_conversation_messages(
            user_id=int(self.user["id"]),
            conversation_uid=conv_uid,
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(str(messages[0].get("send_status")), "sent")
        actions, _ = self.repository.list_recent_actions(user_id=int(self.user["id"]), limit=20)
        self.assertTrue(any(str(item.get("action_type")) == "conversation_send_success" for item in actions))

    def test_send_conversation_reply_failure_keeps_open_and_records_error(self) -> None:
        account = self.repository.create_marketplace_account(
            user_id=int(self.user["id"]),
            marketplace="wb",
            account_name="WB chat fail",
            api_url="https://feedbacks-api.wildberries.ru/api/v1/feedbacks",
            api_key="token",
            extra={},
        )
        conv_uid = self.repository.upsert_conversation(
            user_id=int(self.user["id"]),
            source="wb",
            account_id=int(account["id"]),
            external_conversation_id="chat-fail-1",
            kind="chat",
            customer_name="Buyer Chat 2",
            message_text="Почему задержка?",
            status="open",
            unread_count=2,
            metadata={},
        )
        with mock.patch.object(self.service, "_build_client", return_value=_ConversationReplyClient(should_fail=True)):
            result = self.service.send_conversation_reply(
                user_id=int(self.user["id"]),
                conversation_uid=conv_uid,
                response_text="Проверяем информацию.",
                operator_name="operator@example.com",
                idempotency_key="idem-fail-1",
            )
        self.assertFalse(bool(result.get("ok")))
        self.assertEqual(str(result.get("status")), "failed")
        conv = self.repository.get_conversation(user_id=int(self.user["id"]), conversation_uid=conv_uid)
        self.assertIsNotNone(conv)
        self.assertEqual(str(conv.get("status")), "open")
        self.assertEqual(int(conv.get("send_attempts") or 0), 1)
        self.assertTrue(str(conv.get("send_error_message") or "").strip())
        messages = self.repository.list_conversation_messages(
            user_id=int(self.user["id"]),
            conversation_uid=conv_uid,
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(str(messages[0].get("send_status")), "failed")
        actions, _ = self.repository.list_recent_actions(user_id=int(self.user["id"]), limit=20)
        self.assertTrue(any(str(item.get("action_type")) == "conversation_send_error" for item in actions))


if __name__ == "__main__":
    unittest.main()
