from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .models import ReviewInput
from .processor import ReviewProcessor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automatic review processing module")
    parser.add_argument("--input", required=True, help="Path to input JSON file")
    parser.add_argument("--output", required=True, help="Path to output JSON file")
    return parser


def parse_review_item(item: dict[str, Any]) -> ReviewInput:
    return ReviewInput(
        review_id=str(item.get("review_id") or item.get("id") or ""),
        text=str(item.get("text") or ""),
        author=item.get("author"),
        rating=item.get("rating"),
        metadata={k: v for k, v in item.items() if k not in {"id", "review_id", "text", "author", "rating"}},
    )


def processed_to_dict(processed: Any) -> dict[str, Any]:
    return {
        "review_id": processed.review_id,
        "normalized_text": processed.normalized_text,
        "sentiment_score": processed.sentiment_score,
        "sentiment_label": processed.sentiment_label,
        "is_spam": processed.is_spam,
        "is_toxic": processed.is_toxic,
        "priority": processed.priority,
        "tags": processed.tags,
        "recommended_action": processed.recommended_action,
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Input JSON must be a list of reviews")

    processor = ReviewProcessor()
    reviews = [parse_review_item(item) for item in payload]
    processed = processor.process_batch(reviews)

    result = [processed_to_dict(item) for item in processed]
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
