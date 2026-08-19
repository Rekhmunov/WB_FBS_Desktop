from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ReviewInput:
    """Raw review data received from an external source."""

    review_id: str
    text: str
    author: str | None = None
    rating: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProcessedReview:
    """Review enriched with auto-analysis fields."""

    review_id: str
    normalized_text: str
    sentiment_score: int
    sentiment_label: str
    is_spam: bool
    is_toxic: bool
    priority: str
    tags: list[str]
    recommended_action: str
