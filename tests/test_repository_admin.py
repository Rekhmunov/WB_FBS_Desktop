import os
import tempfile
import unittest

from review_processor.models import ProcessedReview, ReviewInput
from review_processor.repository import ReviewRepository


class RepositoryAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = temp.name
        temp.close()
        self.addCleanup(lambda: os.path.exists(self.db_path) and os.unlink(self.db_path))
        self.repository = ReviewRepository(db_path=self.db_path)
        self.user = self.repository.create_user(email="admin@example.com", password_hash="h", role="admin")
        self.user_id = int(self.user["id"])

    def test_account_api_key_is_encrypted_at_rest(self) -> None:
        account = self.repository.create_marketplace_account(
            user_id=self.user_id,
            marketplace="ozon",
            account_name="Ozon main",
            api_url="https://api-seller.ozon.ru",
            api_key="plain-secret-token",
            extra={"client_id": "123"},
        )
        self.assertTrue(account["has_api_key"])
        self.assertNotEqual(account["api_key_preview"], "")

        db_rows = self.repository.raw_fetch(
            "SELECT api_key_encrypted FROM marketplace_accounts WHERE id = ?",
            (int(account["id"]),),
        )
        self.assertEqual(len(db_rows), 1)
        encrypted = str(db_rows[0]["api_key_encrypted"])
        self.assertNotEqual(encrypted, "plain-secret-token")

        with_secret = self.repository.get_marketplace_account(
            user_id=self.user_id,
            account_id=int(account["id"]),
            include_secrets=True,
        )
        self.assertIsNotNone(with_secret)
        self.assertEqual(with_secret["api_key"], "plain-secret-token")

    def test_ai_settings_secret_and_metrics(self) -> None:
        self.repository.update_ai_settings(
            provider="yandex",
            yandex_api_key="yandex-secret",
            yandex_folder_id="folder-1",
            yandex_model_uri="gpt://folder-1/yandexgpt-lite/latest",
        )

        public = self.repository.get_ai_settings()
        self.assertTrue(public["has_yandex_api_key"])
        self.assertNotIn("yandex_api_key", public)

        secret = self.repository.get_ai_settings(include_secrets=True)
        self.assertEqual(secret["yandex_api_key"], "yandex-secret")
        self.assertEqual(public["yandex_api_key_preview"], "ya*********et")

        processed = ProcessedReview(
            review_id="1",
            normalized_text="bad delivery",
            sentiment_score=-3,
            sentiment_label="negative",
            is_spam=False,
            is_toxic=False,
            priority="high",
            tags=["sentiment:negative", "priority:high"],
            recommended_action="queue_for_manual_review",
        )
        self.repository.upsert_processed_review(
            user_id=self.user_id,
            source="wb",
            account_id=None,
            review=ReviewInput(review_id="ext-1", text="Плохая доставка", rating=1),
            processed=processed,
            category="negative_delivery",
            processing_mode="manual",
            status="queued_for_operator",
        )
        review_uid = self.repository.make_review_uid(self.user_id, "wb", None, "ext-1")
        self.repository.log_review_action(
            user_id=self.user_id,
            review_uid=review_uid,
            action_type="queue_manual",
            actor="operator",
            details={"reason": "negative_delivery"},
        )

        metrics = self.repository.get_sla_metrics(user_id=self.user_id)
        self.assertEqual(metrics["total_reviews"], 1)
        self.assertEqual(metrics["status_counts"].get("queued_for_operator"), 1)

        actions, total = self.repository.list_recent_actions(user_id=self.user_id, limit=10)
        self.assertEqual(total, 1)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action_type"], "queue_manual")

    def test_default_template_variables_and_user_values(self) -> None:
        variables = self.repository.list_template_variables(only_active=True)
        keys = {str(item.get("var_key") or "") for item in variables}
        self.assertNotIn("%BRAND%", keys)
        self.assertNotIn("%NAME%", keys)

        self.repository.upsert_template_variable(
            var_key="%STORE_NAME%",
            title="Название магазина",
            is_user_editable=True,
            source_type="manual",
            source_path=None,
            default_value="StoreFallback",
            is_active=True,
        )
        self.repository.upsert_template_variable(
            var_key="%AUTHOR_NAME%",
            title="Имя автора",
            is_user_editable=False,
            source_type="review_field",
            source_path="author_name",
            default_value="",
            is_active=True,
        )
        updated = self.repository.save_user_template_variable_values(
            user_id=self.user_id,
            values={"%STORE_NAME%": "MyStore", "%AUTHOR_NAME%": "ShouldNotPersist"},
        )
        self.assertEqual(updated, 1)

        user_rows = self.repository.list_user_template_variable_values(user_id=self.user_id)
        by_key = {str(item.get("var_key") or ""): item for item in user_rows}
        self.assertEqual(str(by_key["%STORE_NAME%"].get("value") or ""), "MyStore")
        self.assertEqual(str(by_key["%AUTHOR_NAME%"].get("value") or ""), "")

        context = self.repository.build_template_variables_context(
            user_id=self.user_id,
            review_author="Анна",
            review_rating=5,
            review_category="positive",
            review_sentiment="positive",
            review_tags=["доставка", "качество"],
            review_metadata={"brand": "MetaBrand"},
        )
        self.assertEqual(context.get("%STORE_NAME%"), "MyStore")
        self.assertEqual(context.get("%AUTHOR_NAME%"), "Анна")

    def test_recent_actions_filters_and_search(self) -> None:
        self.repository.log_review_action(
            user_id=self.user_id,
            review_uid="rv-1",
            action_type="sync_review",
            actor="alpha@example.com",
            details={"source": "wb", "status": "open"},
        )
        self.repository.log_review_action(
            user_id=self.user_id,
            review_uid="rv-2",
            action_type="manual_reply",
            actor="beta@example.com",
            details={"reply": "ok", "kind": "question"},
        )
        all_rows, all_total = self.repository.list_recent_actions(user_id=self.user_id, limit=20, offset=0)
        self.assertEqual(all_total, 2)
        self.assertEqual(len(all_rows), 2)

        filtered_by_type, total_by_type = self.repository.list_recent_actions(
            user_id=self.user_id,
            limit=20,
            offset=0,
            action_type="manual_reply",
        )
        self.assertEqual(total_by_type, 1)
        self.assertEqual(len(filtered_by_type), 1)
        self.assertEqual(filtered_by_type[0]["review_uid"], "rv-2")

        filtered_by_actor, total_by_actor = self.repository.list_recent_actions(
            user_id=self.user_id,
            limit=20,
            offset=0,
            actor="alpha@",
        )
        self.assertEqual(total_by_actor, 1)
        self.assertEqual(len(filtered_by_actor), 1)
        self.assertEqual(filtered_by_actor[0]["actor"], "alpha@example.com")

        filtered_by_search, total_by_search = self.repository.list_recent_actions(
            user_id=self.user_id,
            limit=20,
            offset=0,
            search="question",
        )
        self.assertEqual(total_by_search, 1)
        self.assertEqual(len(filtered_by_search), 1)
        self.assertEqual(filtered_by_search[0]["review_uid"], "rv-2")

        options = self.repository.list_action_filter_options(user_id=self.user_id)
        self.assertIn("sync_review", options.get("action_types", []))
        self.assertIn("manual_reply", options.get("action_types", []))
        self.assertIn("alpha@example.com", options.get("actors", []))
        self.assertIn("beta@example.com", options.get("actors", []))

    def test_payment_record_can_be_deleted(self) -> None:
        payment = self.repository.save_payment_record(
            owner_user_id=self.user_id,
            amount=1499.0,
            currency="RUB",
            status="paid",
            details={"note": "test payment"},
        )
        payment_id = int(payment["id"])
        existing = self.repository.list_billing_records(owner_user_id=self.user_id, limit=50)
        self.assertTrue(any(int(row.get("id") or 0) == payment_id for row in existing))

        deleted = self.repository.delete_payment_record(payment_id=payment_id)
        self.assertTrue(deleted)
        deleted_again = self.repository.delete_payment_record(payment_id=payment_id)
        self.assertFalse(deleted_again)

        remaining = self.repository.list_billing_records(owner_user_id=self.user_id, limit=50)
        self.assertFalse(any(int(row.get("id") or 0) == payment_id for row in remaining))

    def test_recreate_user_with_same_email_after_soft_delete(self) -> None:
        first = self.repository.create_user(
            email="duplicate@example.com",
            password_hash="h1",
            role="user",
            plan_code="starter",
        )
        first_id = int(first["id"])
        self.assertTrue(self.repository.soft_delete_user(user_id=first_id))

        second = self.repository.create_user(
            email="duplicate@example.com",
            password_hash="h2",
            role="user",
            plan_code="starter",
        )
        second_id = int(second["id"])
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(str(second["email"]), "duplicate@example.com")

    def test_conversation_storage_and_user_analytics(self) -> None:
        conv_uid = self.repository.upsert_conversation(
            user_id=self.user_id,
            source="ozon",
            account_id=None,
            external_conversation_id="chat-1",
            kind="chat",
            customer_name="Buyer A",
            message_text="Когда отправите заказ?",
            status="open",
            unread_count=2,
            metadata={"topic": "delivery"},
        )
        self.assertTrue(conv_uid.startswith(f"{self.user_id}:ozon"))

        question_uid = self.repository.upsert_conversation(
            user_id=self.user_id,
            source="wb",
            account_id=None,
            external_conversation_id="q-1",
            kind="question",
            customer_name="Buyer B",
            message_text="Какой состав ткани?",
            status="waiting",
            unread_count=1,
            metadata={"topic": "material"},
        )
        self.assertTrue(question_uid.startswith(f"{self.user_id}:wb"))

        rows = self.repository.list_conversations(user_id=self.user_id)
        self.assertEqual(len(rows), 2)

        closed = self.repository.update_conversation_status(
            user_id=self.user_id,
            conversation_uid=conv_uid,
            status="closed",
        )
        self.assertTrue(closed)
        rows_after = self.repository.list_conversations(user_id=self.user_id, status="closed")
        self.assertEqual(len(rows_after), 1)
        self.assertEqual(rows_after[0]["unread_count"], 0)

        # Add one positive processed review for analytics percentages.
        processed_positive = ProcessedReview(
            review_id="2",
            normalized_text="great quality",
            sentiment_score=4,
            sentiment_label="positive",
            is_spam=False,
            is_toxic=False,
            priority="low",
            tags=["sentiment:positive", "priority:low"],
            recommended_action="auto_close_with_thanks",
        )
        self.repository.upsert_processed_review(
            user_id=self.user_id,
            source="ozon",
            account_id=None,
            review=ReviewInput(review_id="ext-2", text="Отличное качество", rating=5),
            processed=processed_positive,
            category="positive_product",
            processing_mode="auto",
            status="answered_auto",
            auto_reply="Спасибо!",
        )

        analytics = self.repository.get_user_analytics(user_id=self.user_id)
        self.assertEqual(analytics["total_reviews"], 1)
        self.assertEqual(analytics["processed_reviews"], 1)
        self.assertEqual(analytics["positive_count"], 1)
        self.assertEqual(analytics["questions_count"], 1)
        self.assertEqual(analytics["chats_count"], 1)

    def test_conversation_reply_tracking_with_idempotency(self) -> None:
        conversation_uid = self.repository.upsert_conversation(
            user_id=self.user_id,
            source="wb",
            account_id=None,
            external_conversation_id="conversation-42",
            kind="question",
            customer_name="Buyer C",
            message_text="Подходит ли на диван 200x220?",
            status="open",
            unread_count=1,
            metadata={},
        )
        idempotency_key = "conversation-42:1"
        message = self.repository.upsert_conversation_outbound_message(
            user_id=self.user_id,
            conversation_uid=conversation_uid,
            message_text="Да, размер подходит.",
            operator_name="operator@example.com",
            idempotency_key=idempotency_key,
        )
        self.assertEqual(message["send_status"], "pending")

        fetched = self.repository.get_conversation_message_by_idempotency(
            user_id=self.user_id,
            conversation_uid=conversation_uid,
            idempotency_key=idempotency_key,
        )
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched["idempotency_key"], idempotency_key)

        failed = self.repository.mark_conversation_message_send_failure(
            user_id=self.user_id,
            conversation_uid=conversation_uid,
            idempotency_key=idempotency_key,
            error_code="timeout",
            error_message="API timeout",
        )
        self.assertTrue(failed)
        after_fail = self.repository.get_conversation(user_id=self.user_id, conversation_uid=conversation_uid)
        self.assertIsNotNone(after_fail)
        assert after_fail is not None
        self.assertEqual(after_fail["status"], "open")
        self.assertEqual(after_fail.get("send_error_code"), "timeout")
        self.assertEqual(after_fail.get("send_error_message"), "API timeout")
        self.assertEqual(int(after_fail.get("send_attempts") or 0), 1)
        self.assertTrue(str(after_fail.get("last_send_attempt_at") or "").strip())

        success = self.repository.mark_conversation_message_send_success(
            user_id=self.user_id,
            conversation_uid=conversation_uid,
            idempotency_key=idempotency_key,
            external_message_id="ext-msg-100",
        )
        self.assertTrue(success)
        after_success = self.repository.get_conversation(user_id=self.user_id, conversation_uid=conversation_uid)
        self.assertIsNotNone(after_success)
        assert after_success is not None
        self.assertEqual(after_success["status"], "waiting")
        self.assertEqual(int(after_success.get("send_attempts") or 0), 0)
        self.assertFalse(str(after_success.get("send_error_code") or "").strip())
        self.assertTrue(str(after_success.get("last_sent_at") or "").strip())

        messages = self.repository.list_conversation_messages(
            user_id=self.user_id,
            conversation_uid=conversation_uid,
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["send_status"], "sent")
        self.assertEqual(messages[0]["external_message_id"], "ext-msg-100")

    def test_template_flags_and_pagination(self) -> None:
        self.repository.upsert_template(
            user_id=self.user_id,
            category="negative_delivery",
            mode="manual",
            template_text="",
            is_enabled=True,
        )
        tpl = self.repository.get_template(user_id=self.user_id, category="negative_delivery")
        self.assertIsNotNone(tpl)
        assert tpl is not None
        self.assertTrue(tpl["is_enabled"])

        self.repository.upsert_template(
            user_id=self.user_id,
            category="negative_delivery",
            mode="manual",
            template_text="",
            is_enabled=False,
        )
        tpl_disabled = self.repository.get_template(user_id=self.user_id, category="negative_delivery")
        self.assertIsNotNone(tpl_disabled)
        assert tpl_disabled is not None
        self.assertFalse(tpl_disabled["is_enabled"])

        deleted = self.repository.delete_template(user_id=self.user_id, category="negative_delivery")
        self.assertTrue(deleted)
        self.assertIsNone(self.repository.get_template(user_id=self.user_id, category="negative_delivery"))

        processed_negative = ProcessedReview(
            review_id="10",
            normalized_text="bad",
            sentiment_score=-2,
            sentiment_label="negative",
            is_spam=False,
            is_toxic=False,
            priority="high",
            tags=["sentiment:negative"],
            recommended_action="queue_for_manual_review",
        )
        processed_positive = ProcessedReview(
            review_id="11",
            normalized_text="good",
            sentiment_score=3,
            sentiment_label="positive",
            is_spam=False,
            is_toxic=False,
            priority="low",
            tags=["sentiment:positive"],
            recommended_action="auto_close_with_thanks",
        )
        self.repository.upsert_processed_review(
            user_id=self.user_id,
            source="wb",
            account_id=None,
            review=ReviewInput(review_id="ext-p-1", text="Плохо", rating=1),
            processed=processed_negative,
            category="negative_delivery",
            processing_mode="manual",
            status="queued_for_operator",
        )
        self.repository.upsert_processed_review(
            user_id=self.user_id,
            source="wb",
            account_id=None,
            review=ReviewInput(review_id="ext-p-2", text="Хорошо", rating=5),
            processed=processed_positive,
            category="positive_product",
            processing_mode="auto",
            status="answered_auto",
            auto_reply="Спасибо",
        )
        self.repository.upsert_processed_review(
            user_id=self.user_id,
            source="wb",
            account_id=None,
            review=ReviewInput(review_id="ext-p-3", text="Пропустить", rating=2),
            processed=processed_negative,
            category="negative_other",
            processing_mode="ignore",
            status="ignored",
        )

        page = self.repository.list_reviews_paginated(user_id=self.user_id, bucket="new", page=1, page_size=10)
        self.assertEqual(page["new_count"], 1)
        self.assertEqual(page["processed_count"], 2)
        self.assertEqual(page["total"], 1)

        processed_page = self.repository.list_reviews_paginated(user_id=self.user_id, bucket="processed", page=1, page_size=10)
        self.assertEqual(processed_page["total"], 2)

        deleted_reviews = self.repository.clear_reviews(user_id=self.user_id)
        self.assertEqual(deleted_reviews, 3)

    def test_processing_rules_and_template_variants(self) -> None:
        created = self.repository.add_template_variant(
            user_id=self.user_id,
            group_id="positive",
            subgroup="Вкус",
            template_text="Шаблон 1",
        )
        self.assertEqual(created["group_id"], "positive")
        variants = self.repository.list_template_variants(user_id=self.user_id, group_id="positive", subgroup="Вкус")
        self.assertGreaterEqual(len(variants), 1)

        random_tpl = self.repository.get_random_template_variant(user_id=self.user_id, group_id="positive")
        self.assertIsNotNone(random_tpl)

        self.repository.upsert_processing_rule(
            user_id=self.user_id,
            group_id="positive",
            action_mode="template",
            auto_send=True,
        )
        rule = self.repository.get_processing_rule(user_id=self.user_id, group_id="positive")
        self.assertIsNotNone(rule)
        assert rule is not None
        self.assertEqual(rule["action_mode"], "template")
        self.assertTrue(rule["auto_send"])

        self.repository.replace_processing_rules(
            user_id=self.user_id,
            rules=[{"group_id": "wrong_size", "action_mode": "manual", "auto_send": False}],
        )
        listed = self.repository.list_processing_rules(user_id=self.user_id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["group_id"], "wrong_size")

    def test_default_template_pool_can_seed_and_copy_for_user(self) -> None:
        inserted = self.repository.seed_default_template_variants(
            [
                {"group_id": "positive", "subgroup": "Вкус", "template_text": "Шаблон по умолчанию A"},
                {"group_id": "positive", "subgroup": "Вкус", "template_text": "Шаблон по умолчанию B"},
            ]
        )
        self.assertEqual(inserted, 2)

        defaults = self.repository.list_default_template_variants(group_id="positive", subgroup="Вкус")
        self.assertEqual(len(defaults), 2)

        copied = self.repository.copy_default_templates_to_user(user_id=self.user_id, only_if_empty=True)
        self.assertEqual(copied, 2)

        user_variants = self.repository.list_template_variants(user_id=self.user_id, group_id="positive", subgroup="Вкус")
        self.assertEqual(len(user_variants), 2)

        first_default_id = int(defaults[0]["id"])
        deleted = self.repository.delete_default_template_variant(template_id=first_default_id)
        self.assertTrue(deleted)
        remaining = self.repository.list_default_template_variants(group_id="positive", subgroup="Вкус")
        self.assertEqual(len(remaining), 1)

    def test_reviews_date_filter_and_sorting(self) -> None:
        processed = ProcessedReview(
            review_id="20",
            normalized_text="ok",
            sentiment_score=1,
            sentiment_label="neutral",
            is_spam=False,
            is_toxic=False,
            priority="medium",
            tags=["sentiment:neutral"],
            recommended_action="queue_for_manual_review",
        )
        self.repository.upsert_processed_review(
            user_id=self.user_id,
            source="wb",
            account_id=None,
            review=ReviewInput(review_id="ext-date-1", text="Отзыв 1", rating=5),
            processed=processed,
            category="positive_product",
            processing_mode="manual",
            status="queued_for_operator",
        )
        self.repository.upsert_processed_review(
            user_id=self.user_id,
            source="wb",
            account_id=None,
            review=ReviewInput(review_id="ext-date-2", text="Отзыв 2", rating=1),
            processed=processed,
            category="negative_delivery",
            processing_mode="manual",
            status="queued_for_operator",
        )
        self.repository.upsert_processed_review(
            user_id=self.user_id,
            source="wb",
            account_id=None,
            review=ReviewInput(review_id="ext-date-3", text="Отзыв 3", rating=3),
            processed=processed,
            category="neutral_other",
            processing_mode="manual",
            status="queued_for_operator",
        )
        self.repository.upsert_processed_review(
            user_id=self.user_id,
            source="ozon",
            account_id=None,
            review=ReviewInput(review_id="ext-date-4", text="Отзыв 4", rating=4),
            processed=processed,
            category="positive_quality",
            processing_mode="auto",
            status="answered_manual",
        )
        with self.repository._connect() as conn:
            conn.execute(
                """
                UPDATE review_items
                SET updated_at = ?
                WHERE user_id = ? AND external_review_id = ?
                """,
                ("2026-03-10T10:00:00+00:00", self.user_id, "ext-date-1"),
            )
            conn.execute(
                """
                UPDATE review_items
                SET updated_at = ?
                WHERE user_id = ? AND external_review_id = ?
                """,
                ("2026-03-11T10:00:00+00:00", self.user_id, "ext-date-2"),
            )
            conn.execute(
                """
                UPDATE review_items
                SET updated_at = ?
                WHERE user_id = ? AND external_review_id = ?
                """,
                ("2026-03-12T10:00:00+00:00", self.user_id, "ext-date-3"),
            )

        date_filtered = self.repository.list_reviews_paginated(
            user_id=self.user_id,
            date_from="2026-03-11",
            date_to="2026-03-12",
            sort="newest",
            page=1,
            page_size=10,
        )
        self.assertEqual(date_filtered["total"], 2)
        self.assertEqual(date_filtered["items"][0]["external_review_id"], "ext-date-3")
        self.assertEqual(date_filtered["items"][1]["external_review_id"], "ext-date-2")

        by_oldest = self.repository.list_reviews_paginated(
            user_id=self.user_id,
            sort="oldest",
            page=1,
            page_size=10,
        )
        self.assertEqual(by_oldest["items"][0]["external_review_id"], "ext-date-1")

        by_low_rating = self.repository.list_reviews_paginated(
            user_id=self.user_id,
            sort="rating_asc",
            page=1,
            page_size=10,
        )
        self.assertEqual(by_low_rating["items"][0]["external_review_id"], "ext-date-2")

        by_high_rating = self.repository.list_reviews_paginated(
            user_id=self.user_id,
            sort="rating_desc",
            page=1,
            page_size=10,
        )
        self.assertEqual(by_high_rating["items"][0]["external_review_id"], "ext-date-1")

        by_category = self.repository.list_reviews_paginated(
            user_id=self.user_id,
            sort="category",
            page=1,
            page_size=10,
        )
        self.assertEqual(by_category["items"][0]["category"], "negative_delivery")

        by_source = self.repository.list_reviews_paginated(
            user_id=self.user_id,
            source="ozon",
            page=1,
            page_size=10,
        )
        self.assertEqual(by_source["total"], 1)
        self.assertEqual(by_source["items"][0]["source"], "ozon")

        by_statuses = self.repository.list_reviews_paginated(
            user_id=self.user_id,
            statuses=["answered_manual"],
            page=1,
            page_size=10,
        )
        self.assertEqual(by_statuses["total"], 1)
        self.assertEqual(by_statuses["items"][0]["external_review_id"], "ext-date-4")

        sources = self.repository.list_review_sources(user_id=self.user_id)
        self.assertEqual(sources, ["ozon", "wb"])

    def test_recommendations_storage_and_random_pick(self) -> None:
        inserted = self.repository.replace_all_recommendations(
            user_id=self.user_id,
            rows=[
                {"source_article": "A-1", "target_articles": ["B-1", "B-2", "B-1"]},
                {"source_article": "A-2", "target_articles": ["C-1"]},
            ],
        )
        self.assertEqual(inserted, 3)

        listed = self.repository.list_recommendations(user_id=self.user_id)
        self.assertEqual(len(listed), 2)
        first = next(item for item in listed if item["source_article"] == "A-1")
        self.assertEqual(first["target_articles"], ["B-1", "B-2"])

        picked = self.repository.get_random_recommendation(user_id=self.user_id, source_article="A-1")
        self.assertIn(picked, {"B-1", "B-2"})

    def test_reviews_include_send_error_message(self) -> None:
        processed = ProcessedReview(
            review_id="err-1",
            normalized_text="ok",
            sentiment_score=0,
            sentiment_label="neutral",
            is_spam=False,
            is_toxic=False,
            priority="medium",
            tags=["sentiment:neutral"],
            recommended_action="queue_for_manual_review",
        )
        self.repository.upsert_processed_review(
            user_id=self.user_id,
            source="wb",
            account_id=None,
            review=ReviewInput(review_id="ext-err-1", text="Тест", rating=4),
            processed=processed,
            category="neutral_other",
            processing_mode="manual",
            status="queued_for_operator",
        )
        review_uid = self.repository.make_review_uid(self.user_id, "wb", None, "ext-err-1")
        self.repository.log_review_action(
            user_id=self.user_id,
            review_uid=review_uid,
            action_type="send_reply_error",
            actor="system",
            details={"error": "Сервис API недоступен"},
        )
        page = self.repository.list_reviews_paginated(user_id=self.user_id, page=1, page_size=10)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["send_error_message"], "Сервис API недоступен")


if __name__ == "__main__":
    unittest.main()
