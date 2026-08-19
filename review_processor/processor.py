from __future__ import annotations

import re
from typing import Iterable

from .models import ProcessedReview, ReviewInput


class ReviewProcessor:
    """Rule-based processor for automatic review triage."""

    POSITIVE_WORDS = {
        "great",
        "good",
        "excellent",
        "perfect",
        "awesome",
        "love",
        "быстро",
        "удобно",
        "отлично",
        "супер",
        "класс",
        "рекомендую",
    }

    NEGATIVE_WORDS = {
        "bad",
        "awful",
        "terrible",
        "hate",
        "broken",
        "slow",
        "bug",
        "crash",
        "не",
        "ужасно",
        "плохо",
        "ошибка",
        "баг",
        "медленно",
        "вылетает",
        "сломано",
    }

    PROFANITY_WORDS = {
        "идиот",
        "дурак",
        "тупой",
        "stupid",
        "idiot",
    }

    HIGH_PRIORITY_HINTS = {
        "payment",
        "refund",
        "security",
        "data loss",
        "не работает",
        "ошибка оплаты",
        "вылетает",
        "не запускается",
    }

    _SPACE_RE = re.compile(r"\s+")
    _URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
    _TOKEN_RE = re.compile(r"[a-zA-Zа-яА-Я0-9]+", re.UNICODE)
    _REPEATED_CHAR_RE = re.compile(r"(.)\1{5,}")

    def process(self, review: ReviewInput) -> ProcessedReview:
        normalized_text = self.normalize_text(review.text)
        tokens = self.tokenize(normalized_text)
        sentiment_score = self.calculate_sentiment(tokens, review.rating)
        sentiment_label = self.sentiment_label(sentiment_score)
        is_spam = self.detect_spam(normalized_text, tokens)
        is_toxic = self.detect_toxicity(tokens)
        priority = self.calculate_priority(
            normalized_text=normalized_text,
            sentiment_score=sentiment_score,
            is_spam=is_spam,
            is_toxic=is_toxic,
        )
        tags = self.build_tags(sentiment_label, is_spam, is_toxic, priority)
        recommended_action = self.recommend_action(priority, is_spam, is_toxic)

        return ProcessedReview(
            review_id=review.review_id,
            normalized_text=normalized_text,
            sentiment_score=sentiment_score,
            sentiment_label=sentiment_label,
            is_spam=is_spam,
            is_toxic=is_toxic,
            priority=priority,
            tags=tags,
            recommended_action=recommended_action,
        )

    def process_batch(self, reviews: Iterable[ReviewInput]) -> list[ProcessedReview]:
        return [self.process(review) for review in reviews]

    @classmethod
    def normalize_text(cls, text: str) -> str:
        return cls._SPACE_RE.sub(" ", text.strip().lower())

    @classmethod
    def tokenize(cls, text: str) -> list[str]:
        return cls._TOKEN_RE.findall(text)

    @classmethod
    def calculate_sentiment(cls, tokens: list[str], rating: int | None) -> int:
        score = 0
        for token in tokens:
            if token in cls.POSITIVE_WORDS:
                score += 1
            if token in cls.NEGATIVE_WORDS:
                score -= 1

        if rating is not None:
            if rating >= 4:
                score += 2
            elif rating <= 2:
                score -= 2

        return score

    @staticmethod
    def sentiment_label(score: int) -> str:
        if score >= 2:
            return "positive"
        if score <= -2:
            return "negative"
        return "neutral"

    @classmethod
    def detect_spam(cls, text: str, tokens: list[str]) -> bool:
        if not text:
            return True
        if cls._URL_RE.search(text):
            return True
        if cls._REPEATED_CHAR_RE.search(text):
            return True
        if len(tokens) <= 2 and any(token in {"buy", "promo", "sale", "скидка"} for token in tokens):
            return True
        return False

    @classmethod
    def detect_toxicity(cls, tokens: list[str]) -> bool:
        return any(token in cls.PROFANITY_WORDS for token in tokens)

    @classmethod
    def calculate_priority(
        cls,
        normalized_text: str,
        sentiment_score: int,
        is_spam: bool,
        is_toxic: bool,
    ) -> str:
        if is_spam:
            return "low"
        if is_toxic:
            return "high"
        if sentiment_score <= -2:
            return "high"
        if any(hint in normalized_text for hint in cls.HIGH_PRIORITY_HINTS):
            return "high"
        if sentiment_score == 0:
            return "medium"
        return "low" if sentiment_score > 0 else "medium"

    @staticmethod
    def build_tags(
        sentiment_label: str,
        is_spam: bool,
        is_toxic: bool,
        priority: str,
    ) -> list[str]:
        tags = [f"sentiment:{sentiment_label}", f"priority:{priority}"]
        if is_spam:
            tags.append("spam")
        if is_toxic:
            tags.append("toxic")
        return tags

    @staticmethod
    def recommend_action(priority: str, is_spam: bool, is_toxic: bool) -> str:
        if is_spam:
            return "archive_or_block"
        if is_toxic:
            return "route_to_moderation"
        if priority == "high":
            return "escalate_to_support"
        if priority == "medium":
            return "queue_for_manual_review"
        return "auto_close_with_thanks"
