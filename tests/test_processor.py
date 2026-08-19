import unittest

from review_processor.models import ReviewInput
from review_processor.processor import ReviewProcessor


class ReviewProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.processor = ReviewProcessor()

    def test_positive_review_low_priority(self) -> None:
        review = ReviewInput(
            review_id="1",
            text="Отлично, удобно и быстро. Рекомендую!",
            rating=5,
        )
        processed = self.processor.process(review)

        self.assertEqual(processed.sentiment_label, "positive")
        self.assertEqual(processed.priority, "low")
        self.assertFalse(processed.is_spam)

    def test_negative_review_high_priority(self) -> None:
        review = ReviewInput(
            review_id="2",
            text="Ужасно, приложение вылетает и не работает",
            rating=1,
        )
        processed = self.processor.process(review)

        self.assertEqual(processed.sentiment_label, "negative")
        self.assertEqual(processed.priority, "high")
        self.assertEqual(processed.recommended_action, "escalate_to_support")

    def test_spam_review(self) -> None:
        review = ReviewInput(
            review_id="3",
            text="Buy now: https://spam.example.com",
        )
        processed = self.processor.process(review)

        self.assertTrue(processed.is_spam)
        self.assertEqual(processed.recommended_action, "archive_or_block")

    def test_toxic_review(self) -> None:
        review = ReviewInput(
            review_id="4",
            text="Поддержка тупой и ничего не решает",
            rating=2,
        )
        processed = self.processor.process(review)

        self.assertTrue(processed.is_toxic)
        self.assertEqual(processed.priority, "high")
        self.assertEqual(processed.recommended_action, "route_to_moderation")


if __name__ == "__main__":
    unittest.main()
