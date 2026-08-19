from __future__ import annotations

import json
import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
import time
from urllib.parse import urlencode, urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_log = logging.getLogger(__name__)

from .config import sync_chats_enabled
from .models import ReviewInput
from .processor import ReviewProcessor
from .repository import ReviewRepository


class MarketplaceClient(Protocol):
    def fetch_reviews(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[ReviewInput]:
        """Load reviews from marketplace API."""

    def fetch_conversations(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        """Load questions/chats from marketplace API."""

    def fetch_questions(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        """Load only questions from marketplace API."""

    def fetch_chats(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        """Load only chats from marketplace API."""

    def send_conversation_reply(
        self,
        *,
        conversation: dict[str, object],
        response_text: str,
    ) -> bool:
        """Send reply for question/chat to marketplace API."""


class MarketplaceSyncError(RuntimeError):
    def __init__(self, source: str, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.details = details or {}


def _raise_if_stop_requested(stop_requested: Callable[[], bool] | None, *, source: str) -> None:
    if stop_requested and stop_requested():
        raise MarketplaceSyncError(source, "Синхронизация остановлена администратором", details={"cancelled": True})


@dataclass(slots=True)
class HTTPMarketplaceClient:
    api_url: str
    api_key: str | None = None
    timeout: int = 15

    def fetch_reviews(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[ReviewInput]:
        _raise_if_stop_requested(stop_requested, source="http")
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.api_url, method="GET", headers=headers)
        payload = _request_json(request=request, timeout=self.timeout, source="http")

        if not isinstance(payload, list):
            raise MarketplaceSyncError("http", "Marketplace API response must be a JSON list")

        return [self._to_review(item) for item in payload]

    def fetch_conversations(self, *, stop_requested: Callable[[], bool] | None = None) -> list[dict[str, object]]:
        _raise_if_stop_requested(stop_requested, source="http")
        return self.fetch_questions(stop_requested=stop_requested) + self.fetch_chats(stop_requested=stop_requested)

    def fetch_questions(self, *, stop_requested: Callable[[], bool] | None = None) -> list[dict[str, object]]:
        _raise_if_stop_requested(stop_requested, source="http")
        return []

    def fetch_chats(self, *, stop_requested: Callable[[], bool] | None = None) -> list[dict[str, object]]:
        _raise_if_stop_requested(stop_requested, source="http")
        return []

    @staticmethod
    def _to_review(item: dict[str, object]) -> ReviewInput:
        review_tags = _extract_review_tags_from_payload(item)
        return ReviewInput(
            review_id=str(item.get("review_id") or item.get("id") or ""),
            text=str(item.get("text") or ""),
            author=str(item["author"]) if item.get("author") is not None else None,
            rating=int(item["rating"]) if item.get("rating") is not None else None,
            metadata={
                **{k: v for k, v in item.items() if k not in {"review_id", "id", "text", "author", "rating"}},
                "review_tags": review_tags,
            },
        )


@dataclass(slots=True)
class OzonMarketplaceClient:
    api_url: str
    client_id: str
    api_key: str
    list_path: str = "/v1/review/list"
    base_payload: dict[str, object] | None = None
    items_keys: tuple[str, ...] = ("reviews", "feedbacks", "items")
    cursor_keys: tuple[str, ...] = ("last_id", "lastId", "next_last_id", "cursor")
    questions_path: str | None = None
    chats_path: str | None = None
    chats_history_path: str = "/v3/chat/history"
    reply_path: str | None = "/v1/review/comment/create"
    reply_review_id_field: str = "review_id"
    reply_text_field: str = "text"
    reply_payload: dict[str, object] | None = None
    page_size: int = 50
    # 500 000 reviews / 50 per page = 10 000 pages max
    max_pages: int = 10000
    timeout: int = 20

    def fetch_reviews(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[ReviewInput]:
        if not self.client_id or not self.api_key:
            raise MarketplaceSyncError("ozon", "Missing Ozon credentials: client_id/api_key")

        last_id: str | None = None
        page = 0
        reviews: list[ReviewInput] = []

        while page < self.max_pages:
            _raise_if_stop_requested(stop_requested, source="ozon")
            # Ozon API: general guideline ≤ 10 req/s. Sleep 150 ms between
            # pages to stay comfortably within the limit.
            if page > 0:
                time.sleep(0.15)
            payload: dict[str, object] = dict(self.base_payload or {})
            payload["limit"] = self.page_size
            if since_date:
                payload.setdefault("date_from", since_date)
                payload.setdefault("dateFrom", since_date)
            if last_id:
                payload["last_id"] = last_id
            body = self._request_json(path=self.list_path, payload=payload)
            result = body.get("result") if isinstance(body.get("result"), dict) else body
            _raise_if_error_payload(result, source="ozon")

            page_items = _extract_sequence(result, keys=self.items_keys)
            if not page_items:
                break
            reviews.extend(self._to_review(item) for item in page_items)

            next_last_id = _extract_str(result, keys=self.cursor_keys)
            has_next = bool(result.get("has_next") or result.get("hasNext"))
            page += 1
            if not has_next and len(page_items) < self.page_size:
                break
            if not next_last_id or next_last_id == last_id:
                break
            last_id = next_last_id

        return [review for review in reviews if review.review_id]

    def fetch_reviews_iter(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ):
        """Generator version of fetch_reviews — yields one ReviewInput per item.

        Pages are fetched and processed one at a time so peak memory
        is O(page_size) regardless of total review count.
        """
        if not self.client_id or not self.api_key:
            raise MarketplaceSyncError("ozon", "Missing Ozon credentials: client_id/api_key")
        last_id: str | None = None
        page = 0
        while page < self.max_pages:
            _raise_if_stop_requested(stop_requested, source="ozon")
            if page > 0:
                time.sleep(0.15)
            payload: dict[str, object] = dict(self.base_payload or {})
            payload["limit"] = self.page_size
            if since_date:
                payload.setdefault("date_from", since_date)
                payload.setdefault("dateFrom", since_date)
            if last_id:
                payload["last_id"] = last_id
            body = self._request_json(path=self.list_path, payload=payload)
            result = body.get("result") if isinstance(body.get("result"), dict) else body
            _raise_if_error_payload(result, source="ozon")
            page_items = _extract_sequence(result, keys=self.items_keys)
            if not page_items:
                break
            for item in page_items:
                rv = self._to_review(item)
                if rv.review_id:
                    yield rv
            next_last_id = _extract_str(result, keys=self.cursor_keys)
            has_next = bool(result.get("has_next") or result.get("hasNext"))
            page += 1
            if not has_next and len(page_items) < self.page_size:
                break
            if not next_last_id or next_last_id == last_id:
                break
            last_id = next_last_id

    def fetch_conversations(self, *, stop_requested: Callable[[], bool] | None = None) -> list[dict[str, object]]:
        return self.fetch_questions(stop_requested=stop_requested) + self.fetch_chats(stop_requested=stop_requested)

    def fetch_questions(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        return self._fetch_conversation_stream(
            path=self.questions_path,
            kind="question",
            since_date=since_date,
            stop_requested=stop_requested,
        )

    def fetch_chats(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        enrich_with_events: bool = False,
        page_progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, object]]:
        """Fetch Ozon buyer-seller chats.

        enrich_with_events=True (manual full sync): for each chat fetch the
        last message via /v3/chat/history to determine last_sender and
        populate message history.  Analogous to WB's _fetch_last_sender_map.

        enrich_with_events=False (60s auto-sync): return chat list only.
        last_sender is derived from unread_count (>0 → buyer wrote last).
        """
        chats = self._fetch_conversation_stream(
            path=self.chats_path,
            kind="chat",
            since_date=since_date,
            stop_requested=stop_requested,
        )
        if not chats or not enrich_with_events:
            return chats

        # Enrich each chat with its last message to determine last_sender.
        # Ozon /v3/chat/history returns messages newest-first (direction=Backward).
        # user.type = "Customer" → buyer, "Seller" → seller.
        total = len(chats)
        for idx, chat in enumerate(chats):
            _raise_if_stop_requested(stop_requested, source="ozon")
            if idx > 0:
                time.sleep(0.12)  # ≤10 req/s limit
            if page_progress_callback:
                try:
                    page_progress_callback(idx + 1, total)
                except Exception:
                    pass
            ext_id = str(chat.get("external_id") or "").strip()
            if not ext_id:
                continue
            try:
                hist_body = self._request_json(
                    path=self.chats_history_path,
                    payload={"chat_id": ext_id, "limit": 20, "direction": "Backward"},
                )
                messages = hist_body.get("messages") or []
                if not isinstance(messages, list):
                    continue
                history_rows: list[dict[str, object]] = []
                last_sender_type: str = ""
                last_msg_ts: str = ""
                buyer_user_id: str = ""
                order_number: str = ""
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    user_info = msg.get("user") or {}
                    user_type = str(user_info.get("type") or "").lower()
                    msg_ts = str(msg.get("created_at") or "")
                    msg_id = str(msg.get("message_id") or "")
                    msg_text = _parse_ozon_message_text(
                        msg.get("data"), bool(msg.get("is_image"))
                    )
                    if not last_sender_type and user_type:
                        last_sender_type = user_type
                        last_msg_ts = msg_ts
                    # Extract buyer user_id and order_number for customer_name
                    if user_type == "customer":
                        uid = str(user_info.get("id") or "").strip()
                        if uid and not buyer_user_id:
                            buyer_user_id = uid
                        ctx = msg.get("context") or {}
                        on = str(ctx.get("order_number") or "").strip()
                        if on and not order_number:
                            order_number = on
                    if msg_id and msg_text:
                        direction = "inbound" if user_type == "customer" else "outbound"
                        operator = "" if user_type == "customer" else "Продавец"
                        history_rows.append({
                            "direction": direction,
                            "message_text": msg_text,
                            "idempotency_key": f"ozon-msg-{msg_id}",
                            "created_at": msg_ts,
                            "operator_name": operator,
                        })
                # Map Ozon user type to our sender labels
                if last_sender_type == "customer":
                    chat["last_sender"] = "client"
                elif last_sender_type in ("seller", "crm"):
                    chat["last_sender"] = "seller"
                if last_msg_ts and not chat.get("last_message_at"):
                    chat["last_message_at"] = last_msg_ts
                # Ozon API does not provide buyer name — use order_number or user_id
                if not chat.get("customer_name"):
                    if order_number:
                        chat["customer_name"] = f"Заказ {order_number}"
                    elif buyer_user_id:
                        chat["customer_name"] = f"Покупатель {buyer_user_id}"
                meta = chat.get("metadata")
                if isinstance(meta, dict) and history_rows:
                    meta["_ozon_history"] = history_rows
            except Exception:
                continue
        return chats

    def _fetch_conversation_stream(
        self,
        *,
        path: str | None,
        kind: str,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        if not path:
            return []

        # For Ozon questions: normalize since_date to YYYY-MM-DD for client-side
        # early exit. Questions are returned newest-first by published_at, so once
        # the last item on a page is older than since_date we can stop paginating.
        # Ozon ignores date_from server-side, but the sort order lets us stop early.
        since_date_ymd: str | None = None
        if kind == "question" and since_date:
            try:
                since_date_ymd = str(since_date).strip()[:10]  # "YYYY-MM-DD"
                if len(since_date_ymd) != 10 or since_date_ymd[4] != "-":
                    since_date_ymd = None
            except Exception:
                since_date_ymd = None

        cursor: str | None = None
        page = 0
        result_items: list[dict[str, object]] = []
        while page < self.max_pages:
            _raise_if_stop_requested(stop_requested, source="ozon")
            if page > 0:
                time.sleep(0.15)
            payload: dict[str, object] = {"limit": self.page_size}
            if since_date:
                # Ozon ignores date_from for questions, but send it anyway
                # in case a future API version starts respecting it.
                payload.setdefault("date_from", since_date)
                payload.setdefault("dateFrom", since_date)
            if cursor:
                # Ozon v3/chat/list uses "cursor" for pagination;
                # older endpoints use "last_id" — send both so it works with either.
                payload["cursor"] = cursor
                payload["last_id"] = cursor
            body = self._request_json(path=path, payload=payload)
            # Ozon v3/chat/list returns items at top level (no "result" wrapper)
            raw = body.get("result") if isinstance(body.get("result"), dict) else body
            _raise_if_error_payload(raw, source="ozon")
            conv_keys = self.items_keys + ("questions", "chats", "dialogs", "messages")
            page_items = _extract_sequence(raw, keys=conv_keys)
            if not page_items:
                break

            # Ozon questions early exit: questions are sorted newest-first by
            # published_at.  Once the last item on a page is older than since_date
            # there cannot be any newer questions on subsequent pages — stop.
            if since_date_ymd and kind == "question":
                last_pub = str(page_items[-1].get("published_at") or "").strip()[:10]
                if last_pub and last_pub < since_date_ymd:
                    # Keep only items within the date range, then stop.
                    page_items = [
                        i for i in page_items
                        if str(i.get("published_at") or "").strip()[:10] >= since_date_ymd
                    ]
                    for item in page_items:
                        mapped = self._to_conversation(item, kind=kind)
                        if mapped:
                            result_items.append(mapped)
                    _log.info(
                        "ozon _fetch_conversation_stream: early exit at page %d"
                        " — last published_at=%s < since_date=%s",
                        page + 1, last_pub, since_date_ymd,
                    )
                    break

            for item in page_items:
                mapped = self._to_conversation(item, kind=kind)
                if mapped:
                    result_items.append(mapped)
            # Ozon v3 returns "cursor" for next page; older APIs return "last_id"
            next_cursor = _extract_str(raw, keys=("cursor",) + self.cursor_keys)
            has_next = bool(raw.get("has_next") or raw.get("hasNext"))
            if not has_next and len(page_items) < self.page_size:
                break
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            page += 1
        return result_items

    def _request_json(self, *, path: str, payload: dict[str, object]) -> dict[str, object]:
        url = _compose_url(self.api_url, path)
        request = Request(
            url,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Client-Id": self.client_id,
                "Api-Key": self.api_key,
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        payload = _request_json(request=request, timeout=self.timeout, source="ozon")
        if not isinstance(payload, dict):
            raise MarketplaceSyncError("ozon", "Ozon API returned non-object payload")
        return payload

    def count_pending(self, *, since_date: str | None = None) -> dict[str, int]:
        """Return approximate counts of items available to sync per channel."""
        counts: dict[str, int] = {"reviews": 0, "questions": 0, "chats": 0}

        # Reviews
        try:
            req_payload: dict[str, object] = {"limit": 1, "sort_dir": "DESC"}
            if since_date:
                req_payload["date_from"] = since_date
                req_payload["dateFrom"] = since_date
            body = self._request_json(path=self.list_path, payload=req_payload)
            raw = body.get("result") if isinstance(body.get("result"), dict) else body
            counts["reviews"] = int(raw.get("total_count") or raw.get("totalCount") or 0)
        except Exception:
            counts["reviews"] = 0

        # Questions: fetch first page to estimate count.
        # Ozon /v1/question/list has no total_count field — use has_next heuristic.
        if self.questions_path:
            try:
                q_body = self._request_json(
                    path=self.questions_path,
                    payload={"page": 1, "page_size": 100},
                )
                q_raw = q_body.get("result") if isinstance(q_body.get("result"), dict) else q_body
                q_items = q_raw.get("questions") if isinstance(q_raw.get("questions"), list) else []
                q_count = len(q_items)
                if bool(q_raw.get("has_next") or q_raw.get("hasNext")):
                    # More pages exist — use conservative estimate
                    q_count = max(q_count, 100)
                counts["questions"] = q_count
            except Exception:
                counts["questions"] = 0

        # Chats: paginate v3/chat/list, count only BUYER_SELLER chats.
        # Ozon API returns max 100 per page with no total_count field.
        # Fetch up to 5 pages (~500 chats) for preview — fast enough.
        if self.chats_path:
            try:
                chat_total = 0
                chat_cursor: str | None = None
                for _pg in range(5):
                    if _pg > 0:
                        time.sleep(0.12)
                    pg_payload: dict[str, object] = {"limit": 100}
                    if chat_cursor:
                        pg_payload["cursor"] = chat_cursor
                    body = self._request_json(path=self.chats_path, payload=pg_payload)
                    raw = body.get("result") if isinstance(body.get("result"), dict) else body
                    chats_page = raw.get("chats") if isinstance(raw.get("chats"), list) else []
                    for ch in chats_page:
                        nested = ch.get("chat") if isinstance(ch.get("chat"), dict) else {}
                        ct = str(nested.get("chat_type") or ch.get("chat_type") or "").upper()
                        if ct in ("BUYER_SELLER", "UNSPECIFIED", ""):
                            chat_total += 1
                    has_next = bool(raw.get("has_next") or raw.get("hasNext"))
                    chat_cursor = str(raw.get("cursor") or "").strip() or None
                    if not has_next or not chat_cursor or len(chats_page) == 0:
                        break
                counts["chats"] = chat_total
            except Exception:
                counts["chats"] = 0

        return counts

    def send_review_reply(self, *, review: ReviewInput, response_text: str) -> bool:
        if not self.client_id or not self.api_key:
            raise MarketplaceSyncError("ozon", "Missing Ozon credentials: client_id/api_key")
        if not self.reply_path:
            return False
        payload: dict[str, object] = dict(self.reply_payload or {})
        payload[self.reply_review_id_field] = review.review_id
        payload[self.reply_text_field] = response_text
        result = self._request_json(path=self.reply_path, payload=payload)
        raw = result.get("result") if isinstance(result.get("result"), Mapping) else result
        _raise_if_error_payload(raw, source="ozon")
        return True

    def send_conversation_reply(self, *, conversation: dict[str, object], response_text: str) -> bool:
        if not self.client_id or not self.api_key:
            raise MarketplaceSyncError("ozon", "Missing Ozon credentials: client_id/api_key")
        external_id = str(conversation.get("external_conversation_id") or conversation.get("external_id") or "").strip()
        if not external_id:
            raise MarketplaceSyncError("ozon", "Missing external conversation id for reply")
        kind = str(conversation.get("kind") or "").lower()
        if kind == "chat":
            # Ozon buyer-seller chats use /v1/chat/send/message
            result = self._request_json(
                path="/v1/chat/send/message",
                payload={"chat_id": external_id, "text": response_text},
            )
            # Success response: {"result": "success"}
            if str(result.get("result") or "").lower() == "success":
                return True
            raw = result.get("result") if isinstance(result.get("result"), Mapping) else result
            _raise_if_error_payload(raw, source="ozon")
            return True
        if kind == "question":
            # Ozon question answer requires question_id + sku + text
            meta = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
            raw_meta = meta.get("raw") if isinstance(meta.get("raw"), dict) else {}
            sku = int(raw_meta.get("sku") or 0)
            if not sku:
                raise MarketplaceSyncError("ozon", "Cannot reply to Ozon question: sku missing in metadata")
            result = self._request_json(
                path="/v1/question/answer/create",
                payload={"question_id": external_id, "sku": sku, "text": response_text},
            )
            raw = result.get("result") if isinstance(result.get("result"), Mapping) else result
            _raise_if_error_payload(raw, source="ozon")
            return True
        # Reviews use the configured reply_path
        if not self.reply_path:
            return False
        payload: dict[str, object] = dict(self.reply_payload or {})
        payload[self.reply_review_id_field] = external_id
        payload[self.reply_text_field] = response_text
        result = self._request_json(path=self.reply_path, payload=payload)
        raw = result.get("result") if isinstance(result.get("result"), Mapping) else result
        _raise_if_error_payload(raw, source="ozon")
        return True

    @staticmethod
    def _to_review(item: dict[str, object]) -> ReviewInput:
        text = str(item.get("text") or item.get("comment") or item.get("content") or "")
        review_tags = _extract_review_tags_from_payload(item)
        return ReviewInput(
            review_id=str(item.get("id") or item.get("review_id") or item.get("uuid") or ""),
            text=text,
            author=str(item.get("author") or item.get("user_name") or item.get("customer_name") or "")
            or None,
            rating=_to_int(item.get("rating") or item.get("score")),
            metadata={"raw": item, "marketplace": "ozon", "review_tags": review_tags},
        )

    @staticmethod
    def _to_conversation(item: dict[str, object], *, kind: str) -> dict[str, object] | None:
        # Ozon v3/chat/list wraps chat info inside a nested "chat" object:
        # {"chat": {"chat_id": "...", "chat_status": "OPENED", "chat_type": "BUYER_SELLER"}, "unread_count": N}
        # Flatten it so the rest of the mapping works uniformly.
        nested_chat = item.get("chat")
        if isinstance(nested_chat, dict):
            item = {**item, **nested_chat}

        # Only process buyer-seller chats.
        # SELLER_SUPPORT = support tickets, SELLER_API_UPDATES = system notifications,
        # UNSPECIFIED = Ozon system notifications (returns, payments, etc.) with
        # user.type=NotificationUser — NOT real buyer messages.
        # Only BUYER_SELLER contains real customer conversations.
        chat_type = str(item.get("chat_type") or "").upper()
        if chat_type and chat_type != "BUYER_SELLER":
            return None

        external_id = str(
            item.get("chat_id")
            or item.get("id")
            or item.get("question_id")
            or ""
        ).strip()
        if not external_id:
            return None
        text = str(item.get("text") or item.get("question") or item.get("message") or item.get("content") or "")
        customer_name = str(item.get("author") or item.get("user_name") or item.get("customer_name") or "") or None

        # Ozon v3: chat_status = "OPENED" / "CLOSED"
        raw_status = str(item.get("chat_status") or item.get("status") or "open").lower()
        if raw_status in ("opened", "open"):
            status = "open"
        elif raw_status in ("closed",):
            status = "closed"
        else:
            status = "open"

        # Ozon questions: status="PROCESSED" or answers_count>0 means the seller
        # has already replied. Map to "answered_manual" so the question moves to
        # the "Processed" bucket and is not shown as unanswered.
        seller_replied_at: str | None = None
        if kind == "question":
            answers_count = _to_positive_int(item.get("answers_count"), default=0)
            if raw_status == "processed" or answers_count > 0:
                status = "answered_manual"
                # Use published_at as the best available answer timestamp proxy.
                seller_replied_at = _normalize_timestamp(
                    item.get("published_at") or item.get("updated_at")
                )

        unread_count = _to_positive_int(item.get("unread_count") or item.get("unread"), default=0)
        # Ozon v3/chat/list does NOT return the last message timestamp —
        # only created_at (chat creation date, can be 2023).
        # Using created_at as last_message_at would cause the date filter
        # to skip old chats that have recent buyer messages.
        # Use updated_at if present; otherwise leave None so the chat is
        # never excluded by the date filter (we rely on unread_count instead).
        # For Ozon questions: published_at is the only reliable timestamp.
        # last_message_at is NOT NULL in DB — use published_at as fallback.
        updated_at = str(
            item.get("updated_at") or item.get("last_message_at")
            or item.get("published_at") or ""
        )
        return {
            "external_id": external_id,
            "kind": kind,
            "customer_name": customer_name,
            "message_text": text,
            "status": status,
            "unread_count": unread_count,
            "last_message_at": updated_at or None,
            "seller_replied_at": seller_replied_at,
            "metadata": {"raw": item, "marketplace": "ozon"},
        }


@dataclass(slots=True)
class WildberriesMarketplaceClient:
    api_url: str
    api_key: str
    list_path: str | None = None
    skip_param: str = "skip"
    take_param: str = "take"
    unanswered_param: str = "isAnswered"
    unanswered_value: str = "false"
    items_keys: tuple[str, ...] = ("feedbacks", "reviews", "items")
    questions_path: str | None = None
    chats_path: str | None = None
    chats_api_url: str | None = None
    chats_events_path: str | None = "/api/v1/seller/events"
    _resume_events_cursor: str | None = None
    _last_sent_add_time: int | None = None  # addTime from last /seller/message response
    _cached_chats_count: int | None = None
    _cached_chats_count_at: float = 0.0
    reply_path: str | None = "/api/v1/feedbacks/answer"
    reply_method: str = "POST"
    reply_review_id_field: str = "id"
    reply_text_field: str = "text"
    reply_payload: dict[str, object] | None = None
    page_size: int = 100
    # 500 000 reviews / 100 per page = 5 000 pages max
    max_pages: int = 5000
    timeout: int = 20

    def fetch_reviews(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[ReviewInput]:
        if not self.api_key:
            raise MarketplaceSyncError("wb", "Missing Wildberries api_key")

        skip = 0
        page = 0
        reviews: list[ReviewInput] = []

        while page < self.max_pages:
            _raise_if_stop_requested(stop_requested, source="wb")
            # WB Feedbacks API: limit 3 req/s (burst 6). Sleep between pages so
            # we stay well within the 333 ms / request budget.
            if page > 0:
                time.sleep(0.4)
            try:
                payload = self._request_json(skip=skip, take=self.page_size, since_date=since_date)
            except TypeError:
                payload = self._request_json(skip=skip, take=self.page_size)
            _raise_if_error_payload(payload, source="wb")
            items = _extract_sequence(payload, keys=self.items_keys)
            if not items and isinstance(payload.get("data"), dict):
                _raise_if_error_payload(payload["data"], source="wb")
                items = _extract_sequence(payload["data"], keys=self.items_keys)
            if not items:
                break
            reviews.extend(self._to_review(item) for item in items)
            if len(items) < self.page_size:
                break
            skip += self.page_size
            page += 1

        return [review for review in reviews if review.review_id]

    def _fetch_reviews_iter_with_answered(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        answered_value: str,
    ):
        """Internal generator: fetch one pass of reviews with a specific isAnswered value."""
        original = self.unanswered_value
        object.__setattr__(self, "unanswered_value", answered_value)
        try:
            skip = 0
            page = 0
            while page < self.max_pages:
                _raise_if_stop_requested(stop_requested, source="wb")
                if page > 0:
                    time.sleep(0.4)
                try:
                    payload = self._request_json(skip=skip, take=self.page_size, since_date=since_date)
                except TypeError:
                    payload = self._request_json(skip=skip, take=self.page_size)
                _raise_if_error_payload(payload, source="wb")
                items = _extract_sequence(payload, keys=self.items_keys)
                if not items and isinstance(payload.get("data"), dict):
                    _raise_if_error_payload(payload["data"], source="wb")
                    items = _extract_sequence(payload["data"], keys=self.items_keys)
                if not items:
                    break
                for item in items:
                    rv = self._to_review(item)
                    if rv.review_id:
                        yield rv
                if len(items) < self.page_size:
                    break
                skip += self.page_size
                page += 1
        finally:
            object.__setattr__(self, "unanswered_value", original)

    def fetch_reviews_iter(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ):
        """Generator: yields all reviews (unanswered + answered) from the given date.

        Two passes — isAnswered=false then isAnswered=true — so reviews answered
        directly on the marketplace portal are also captured and marked answered_manual.
        Pages are fetched one at a time for O(page_size) peak memory.
        """
        if not self.api_key:
            raise MarketplaceSyncError("wb", "Missing Wildberries api_key")
        # Pass 1: unanswered reviews
        yield from self._fetch_reviews_iter_with_answered(
            since_date=since_date,
            stop_requested=stop_requested,
            answered_value="false",
        )
        # Pass 2: answered reviews (replied on portal or via API)
        # A brief pause between passes to respect rate limits
        time.sleep(0.5)
        yield from self._fetch_reviews_iter_with_answered(
            since_date=since_date,
            stop_requested=stop_requested,
            answered_value="true",
        )

    def fetch_conversations(self, *, stop_requested: Callable[[], bool] | None = None) -> list[dict[str, object]]:
        return self.fetch_questions(stop_requested=stop_requested) + self.fetch_chats(stop_requested=stop_requested)

    def fetch_questions(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        if not self.questions_path:
            return []
        # Fetch unanswered questions
        unanswered = self._fetch_conversation_endpoint(
            path=self.questions_path,
            kind="question",
            since_date=since_date,
            stop_requested=stop_requested,
        )
        # Also fetch answered questions so we can mark them as processed
        # Temporarily swap unanswered_value to "true"
        original_unanswered_value = self.unanswered_value
        object.__setattr__(self, "unanswered_value", "true")
        try:
            answered = self._fetch_conversation_endpoint(
                path=self.questions_path,
                kind="question",
                since_date=since_date,
                stop_requested=stop_requested,
            )
        except Exception:
            answered = []
        finally:
            object.__setattr__(self, "unanswered_value", original_unanswered_value)
        return unanswered + answered

    def fetch_chats(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        enrich_with_events: bool = False,
        page_progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, object]]:
        """Fetch the list of seller chats.

        When ``enrich_with_events=True`` (used only during full sync, not during
        capability probes) the events endpoint is also queried to determine the
        last-message sender per chat, which drives the answered/needs-reply
        bucket logic.  This extra step paginates events with a 1.1 s inter-page
        sleep and should NOT be triggered during quick capability checks.
        """
        if not self.chats_path:
            return []
        # WB buyer-chat list returns ALL chats in a single request —
        # no pagination params (skip/take/isAnswered/dateFrom) are supported.
        chats = self._fetch_conversation_endpoint(
            path=self.chats_path,
            kind="chat",
            base_url=self.chats_api_url or self.api_url,
            single_request=True,
            stop_requested=stop_requested,
        )
        if not chats or not enrich_with_events:
            return chats
        # Enrich each chat with last-sender info from the events endpoint.
        # Pass resume_cursor so subsequent syncs only fetch new events.
        try:
            sender_map = self._fetch_last_sender_map(
                since_date=since_date,
                resume_cursor=self._resume_events_cursor,
                stop_requested=stop_requested,
                page_progress_callback=page_progress_callback,
            )
        except MarketplaceSyncError as exc:
            if bool(exc.details.get("cancelled")):
                raise
            sender_map = {}
        except Exception:
            sender_map = {}
        # Extract and store final cursor for next sync.
        cursor_entry = sender_map.pop("_final_cursor", None)
        if isinstance(cursor_entry, dict):
            new_cursor = cursor_entry.get("cursor")
            if new_cursor:
                self._resume_events_cursor = str(new_cursor)
        if sender_map:
            for chat in chats:
                ext_id = str(chat.get("external_id") or "").strip()
                if not ext_id:
                    continue
                sender_info = sender_map.get(ext_id)
                if not sender_info:
                    continue
                meta = chat.get("metadata")
                if isinstance(meta, dict):
                    meta["last_sender"] = sender_info.get("sender")
                    meta["last_sender_ts"] = sender_info.get("ts")
                    # Embed raw events for later storage in sync_chats().
                    meta["_wb_events"] = sender_info.get("events") or []
                if sender_info.get("sender") == "seller":
                    chat["last_sender"] = "seller"
                elif sender_info.get("sender") == "client":
                    chat["last_sender"] = "client"
        return chats

    def _fetch_last_sender_map(
        self,
        *,
        since_date: str | None = None,
        resume_cursor: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        page_progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, dict[str, object]]:
        """Return a map of chatID -> {sender, ts, events, _final_cursor}.

        Paginates through /api/v1/seller/events using the ``next`` cursor.

        ``resume_cursor`` (persisted from last sync): start pagination from
        this position instead of the beginning.  This means subsequent syncs
        only fetch NEW events, not the full history (saves ~90 seconds on the
        first daily re-sync after the initial full load).

        The returned dict includes a special key ``_final_cursor`` with the
        last cursor value so the caller can persist it for next time.

        Events older than ``since_date`` are still skipped client-side.
        """
        if not self.chats_events_path:
            return {}
        base_url = self.chats_api_url or self.api_url
        endpoint = _compose_url(base_url, self.chats_events_path)

        # Convert since_date to a unix-ms cutoff.
        since_ts_ms: int | None = None
        if since_date:
            raw_since = _normalize_timestamp(since_date)
            if raw_since:
                try:
                    dt = datetime.fromisoformat(raw_since.replace("Z", "+00:00"))
                    since_ts_ms = int(dt.timestamp() * 1000)
                except (ValueError, AttributeError):
                    pass

        result: dict[str, dict[str, object]] = {}
        events_by_chat: dict[str, list[dict[str, object]]] = {}
        # Start from resume_cursor if provided (incremental sync), else from start.
        cursor: str | None = resume_cursor or None
        final_cursor: str | None = cursor
        max_pages = 500
        page = 0
        _CHAT_API_BURST = 9
        while page < max_pages:
            _raise_if_stop_requested(stop_requested, source="wb")
            if page > 0 and page % _CHAT_API_BURST == 0:
                time.sleep(10.5)
            url = endpoint if cursor is None else f"{endpoint}?next={cursor}"
            request = Request(url, method="GET", headers={"Authorization": self.api_key})
            try:
                payload = _request_json(request=request, timeout=self.timeout, source="wb")
            except MarketplaceSyncError:
                break
            if not isinstance(payload, dict):
                break
            raw_result = payload.get("result") or {}
            if not isinstance(raw_result, dict):
                break
            events = raw_result.get("events") or []
            if not isinstance(events, list) or not events:
                break
            for event in events:
                if not isinstance(event, dict):
                    continue
                chat_id = str(event.get("chatID") or "").strip()
                sender = str(event.get("sender") or "").strip().lower()
                ts_raw = event.get("addTimestamp")
                try:
                    ts = int(ts_raw) if ts_raw is not None else 0
                except (TypeError, ValueError):
                    ts = 0
                if since_ts_ms is not None and ts > 0 and ts < since_ts_ms:
                    continue
                if not chat_id or not sender:
                    continue
                prev = result.get(chat_id)
                if prev is None or ts > int(prev.get("ts") or 0):
                    result[chat_id] = {"sender": sender, "ts": ts}
                events_by_chat.setdefault(chat_id, []).append(event)
            new_cursor = str(raw_result.get("next") or "").strip() or None
            if new_cursor:
                final_cursor = new_cursor
            cursor = new_cursor
            if not cursor:
                break
            page += 1
            # Report page progress to caller (e.g. for progress bar)
            if page_progress_callback:
                try:
                    page_progress_callback(page, max_pages)
                except Exception:
                    pass

        for chat_id, ev_list in events_by_chat.items():
            entry = result.setdefault(chat_id, {})
            entry["events"] = ev_list
        # Store the final cursor so the caller can persist it.
        result["_final_cursor"] = {"cursor": final_cursor}  # type: ignore[assignment]
        return result

    def _fetch_conversation_endpoint(
        self,
        *,
        path: str,
        kind: str,
        base_url: str | None = None,
        since_date: str | None = None,
        single_request: bool = False,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        """Fetch a WB conversation endpoint.

        ``single_request=True``: one GET with no query params (used for the
        WB buyer-chat list which returns all chats in one response and does not
        support skip/take/isAnswered/dateFrom).

        ``single_request=False`` (default): paginated loop with skip/take and
        optional dateFrom — used for the WB questions endpoint.

        Rate limit: WB Feedbacks/Questions API = 3 req/s → sleep 0.4 s between
        pages for paginated mode.
        """
        conversation_keys = self.items_keys + ("questions", "chats", "dialogs", "messages", "result")
        result: list[dict[str, object]] = []

        if single_request:
            # One-shot fetch — no pagination, no extra query params.
            _raise_if_stop_requested(stop_requested, source="wb")
            endpoint = _compose_url(base_url or self.api_url, path)
            request = Request(endpoint, method="GET", headers={"Authorization": self.api_key})
            payload = _request_json(request=request, timeout=self.timeout, source="wb")
            if not isinstance(payload, dict):
                raise MarketplaceSyncError("wb", "Wildberries API returned non-object payload for conversations")
            _raise_if_error_payload(payload, source="wb")
            rows = _extract_sequence(payload, keys=conversation_keys)
            if not rows:
                for nested_key in ("data", "result", "response"):
                    nested_value = payload.get(nested_key)
                    if not isinstance(nested_value, Mapping | list):
                        continue
                    rows = _extract_sequence(nested_value, keys=conversation_keys)
                    if rows:
                        break
            for item in rows:
                mapped = self._to_conversation(item, kind=kind)
                if mapped:
                    result.append(mapped)
            return result

        skip = 0
        page = 0

        # WB Questions supports dateFrom as unix seconds (same as feedbacks).
        wb_date_from = self._to_wb_unix_timestamp(since_date)

        while page < self.max_pages:
            _raise_if_stop_requested(stop_requested, source="wb")
            if page > 0:
                # WB Feedbacks API: 3 req/s limit → 400 ms between requests.
                time.sleep(0.4)
            endpoint = _compose_url(base_url or self.api_url, path)
            params_dict: dict[str, object] = {
                self.skip_param: skip,
                self.take_param: self.page_size,
                self.unanswered_param: self.unanswered_value,
            }
            if wb_date_from is not None:
                params_dict["dateFrom"] = wb_date_from
            params = urlencode(params_dict)
            url = f"{endpoint}?{params}" if "?" not in endpoint else f"{endpoint}&{params}"
            request = Request(url, method="GET", headers={"Authorization": self.api_key})
            payload = _request_json(request=request, timeout=self.timeout, source="wb")
            if not isinstance(payload, dict):
                raise MarketplaceSyncError("wb", "Wildberries API returned non-object payload for conversations")
            _raise_if_error_payload(payload, source="wb")
            rows = _extract_sequence(payload, keys=conversation_keys)
            if not rows:
                for nested_key in ("data", "result", "response"):
                    nested_value = payload.get(nested_key)
                    if not isinstance(nested_value, Mapping | list):
                        continue
                    rows = _extract_sequence(nested_value, keys=conversation_keys)
                    if rows:
                        break
            if not rows:
                break
            for item in rows:
                mapped = self._to_conversation(item, kind=kind)
                if mapped:
                    result.append(mapped)
            if len(rows) < self.page_size:
                break
            skip += self.page_size
            page += 1

        return result

    def _request_json(self, *, skip: int, take: int, since_date: str | None = None) -> dict[str, object]:
        params_payload: dict[str, object] = {
            self.skip_param: skip,
            self.take_param: take,
            self.unanswered_param: self.unanswered_value,
        }
        wb_date_from = self._to_wb_unix_timestamp(since_date)
        if wb_date_from is not None:
            params_payload["dateFrom"] = wb_date_from
        params = urlencode(params_payload)
        endpoint = _compose_url(self.api_url, self.list_path)
        url = f"{self.api_url}?{params}" if "?" not in self.api_url else f"{self.api_url}&{params}"
        if self.list_path:
            url = f"{endpoint}?{params}" if "?" not in endpoint else f"{endpoint}&{params}"
        request = Request(
            url,
            method="GET",
            headers={"Authorization": self.api_key},
        )
        payload = _request_json(request=request, timeout=self.timeout, source="wb")
        if not isinstance(payload, dict):
            raise MarketplaceSyncError("wb", "Wildberries API returned non-object payload")
        return payload

    @staticmethod
    def _to_wb_unix_timestamp(since_date: str | None) -> int | None:
        raw = str(since_date or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            parsed = int(raw)
            return parsed if parsed > 0 else None
        normalized = raw.replace("Z", "+00:00")
        parsed_dt: datetime | None = None
        try:
            parsed_dt = datetime.fromisoformat(normalized)
        except ValueError:
            # Sync settings keep date in YYYY-MM-DD format.
            date_part = normalized[:10]
            try:
                parsed_dt = datetime.strptime(date_part, "%Y-%m-%d")
            except ValueError:
                return None
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=UTC)
        return int(parsed_dt.astimezone(UTC).timestamp())

    def _fetch_fresh_reply_sign(self, *, chat_id: str) -> str | None:
        """Fetch the current replySign for a specific chat from /seller/chats.

        replySign changes over time.  Calling this before sending ensures
        we use the latest value rather than a potentially stale one from sync.
        """
        if not chat_id or not self.chats_path:
            return None
        try:
            base = self.chats_api_url or self.api_url
            endpoint = _compose_url(base, self.chats_path)
            req = Request(endpoint, method="GET", headers={"Authorization": self.api_key})
            payload = _request_json(request=req, timeout=self.timeout, source="wb")
            if not isinstance(payload, dict):
                return None
            items = _extract_sequence(
                payload,
                keys=self.items_keys + ("questions", "chats", "dialogs", "messages", "result"),
            )
            if not items:
                for nk in ("data", "result", "response"):
                    nv = payload.get(nk)
                    if isinstance(nv, (dict, list)):
                        items = _extract_sequence(nv, keys=self.items_keys + ("chats", "result"))
                        if items:
                            break
            for item in items:
                if str(item.get("chatID") or "") == chat_id:
                    return str(item.get("replySign") or "").strip() or None
        except Exception:
            pass
        return None

    def _find_wb_event_id_for_sent(
        self,
        *,
        chat_id: str,
        add_time_ms: int,
        max_retries: int = 3,
    ) -> str | None:
        """Look up the WB eventID for a message we just sent.

        WB assigns an eventID to every message but doesn't return it in the
        /seller/message response — only addTime. We use addTime as a cursor
        to find the event in /seller/events, then return its eventID.
        Retries up to max_retries times with a short sleep to allow WB to
        process the message.
        """
        if not self.chats_events_path or not chat_id:
            return None
        base_url = self.chats_api_url or self.api_url
        endpoint = _compose_url(base_url, self.chats_events_path)
        # Start cursor 2 seconds before addTime to account for clock skew
        cursor = str(max(add_time_ms - 2000, 0))

        for attempt in range(max_retries):
            if attempt > 0:
                time.sleep(2.0)
            try:
                url = f"{endpoint}?next={cursor}"
                req = Request(url, method="GET", headers={"Authorization": self.api_key})
                payload = _request_json(request=req, timeout=self.timeout, source="wb")
                if not isinstance(payload, dict):
                    continue
                raw_result = payload.get("result") or {}
                events = (raw_result.get("events") or []) if isinstance(raw_result, dict) else []
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    if str(ev.get("chatID") or "") != chat_id:
                        continue
                    ev_sender = str(ev.get("sender") or "").strip().lower()
                    if ev_sender != "seller":
                        continue
                    ev_ts = int(ev.get("addTimestamp") or 0)
                    # Match: event within 5 seconds of our addTime
                    if abs(ev_ts - add_time_ms) <= 5000:
                        ev_id = str(ev.get("eventID") or "").strip()
                        if ev_id:
                            return ev_id
            except Exception:
                continue
        return None

    def count_pending(self, *, since_date: str | None = None) -> dict[str, int]:
        """Return approximate counts of items available to sync per channel.

        Uses lightweight count endpoints where available:
        - Reviews: GET /api/v1/feedbacks/count-unanswered → countUnanswered
        - Questions: GET /api/v1/questions/count          → countUnanswered
        - Chats: GET /api/v1/seller/chats (full list), count items returned

        Returns 0 for any channel that errors (access denied, not configured).
        """
        counts: dict[str, int] = {"reviews": 0, "questions": 0, "chats": 0}
        wb_date_from = self._to_wb_unix_timestamp(since_date)

        # For preview/count endpoints: short timeout, retries=1 so single
        # network hiccup is retried but 429 wait is only 60s × 1 = 60s max.
        # The nginx proxy_read_timeout is now 300s so this is safe.
        _preview_timeout = min(self.timeout, 10)  # max 10s per individual request

        # Reviews count via /api/v1/feedbacks/count-unanswered
        try:
            review_count_path = "/api/v1/feedbacks/count-unanswered"
            endpoint = _compose_url(self.api_url, review_count_path)
            if wb_date_from is not None:
                endpoint = f"{endpoint}?dateFrom={wb_date_from}"
            req = Request(endpoint, method="GET", headers={"Authorization": self.api_key})
            payload = _request_json(request=req, timeout=_preview_timeout, source="wb", retries=1)
            if isinstance(payload, dict):
                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                counts["reviews"] = int(data.get("countUnanswered") or 0)
        except Exception:
            counts["reviews"] = 0

        # Questions count via /api/v1/questions/count
        try:
            q_count_path = "/api/v1/questions/count"
            endpoint = _compose_url(self.api_url, q_count_path)
            if wb_date_from is not None:
                endpoint = f"{endpoint}?dateFrom={wb_date_from}"
            req = Request(endpoint, method="GET", headers={"Authorization": self.api_key})
            payload = _request_json(request=req, timeout=_preview_timeout, source="wb", retries=1)
            if isinstance(payload, dict):
                raw_data = payload.get("data")
                # /api/v1/questions/count returns {"data": <int>} (raw number)
                if isinstance(raw_data, (int, float)):
                    counts["questions"] = int(raw_data)
                elif isinstance(raw_data, dict):
                    counts["questions"] = int(
                        raw_data.get("countUnanswered")
                        or raw_data.get("count")
                        or raw_data.get("total")
                        or 0
                    )
                else:
                    counts["questions"] = int(
                        payload.get("countUnanswered")
                        or payload.get("count")
                        or payload.get("total")
                        or 0
                    )
        except Exception:
            counts["questions"] = 0

        # Chats: use cached count from last sync if fresh (within 5 min),
        # otherwise fetch the chat list (1 request, same as sync list step).
        try:
            cached = getattr(self, "_cached_chats_count", None)
            cached_at = getattr(self, "_cached_chats_count_at", 0.0)
            # Note: cache is on the client object which is recreated per call.
            # Cache will only hit if count_pending is called multiple times
            # on the same client instance (e.g. probes). For preview, it always misses.
            if cached is not None and (time.time() - cached_at) < 600:
                counts["chats"] = int(cached)
            elif self.chats_path:
                base = self.chats_api_url or self.api_url
                endpoint = _compose_url(base, self.chats_path)
                req = Request(endpoint, method="GET", headers={"Authorization": self.api_key})
                payload = _request_json(request=req, timeout=_preview_timeout, source="wb", retries=1)
                if isinstance(payload, dict):
                    items = _extract_sequence(
                        payload,
                        keys=self.items_keys + ("questions", "chats", "dialogs", "messages", "result"),
                    )
                    if not items:
                        for nk in ("data", "result", "response"):
                            nv = payload.get(nk)
                            if isinstance(nv, (dict, list)):
                                items = _extract_sequence(
                                    nv,
                                    keys=self.items_keys + ("questions", "chats", "result"),
                                )
                                if items:
                                    break
                    counts["chats"] = len(items)
                    self._cached_chats_count = counts["chats"]  # type: ignore[attr-defined]
                    self._cached_chats_count_at = time.time()  # type: ignore[attr-defined]
        except MarketplaceSyncError as _e:
            _log.warning("count_pending chats: %s", _e)
            counts["chats"] = 0
        except Exception as _e:
            _log.warning("count_pending chats unexpected: %s", _e)
            counts["chats"] = 0

        return counts

    def send_review_reply(self, *, review: ReviewInput, response_text: str) -> bool:
        if not self.api_key:
            raise MarketplaceSyncError("wb", "Missing Wildberries api_key")
        if not self.reply_path:
            return False
        payload: dict[str, object] = dict(self.reply_payload or {})
        payload[self.reply_review_id_field] = review.review_id
        payload[self.reply_text_field] = response_text
        method = self.reply_method.strip().upper() if self.reply_method else "POST"
        endpoint = _compose_url(self.api_url, self.reply_path)
        if method == "GET":
            query = urlencode(payload)
            url = f"{endpoint}?{query}" if "?" not in endpoint else f"{endpoint}&{query}"
            request = Request(url, method="GET", headers={"Authorization": self.api_key})
        else:
            request = Request(
                endpoint,
                method="POST",
                headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                data=json.dumps(payload).encode("utf-8"),
            )
        raw = _request_json(request=request, timeout=self.timeout, source="wb", retries=1)
        _raise_if_error_payload(raw, source="wb")
        return True

    def send_conversation_reply(self, *, conversation: dict[str, object], response_text: str) -> bool:
        if not self.api_key:
            raise MarketplaceSyncError("wb", "Missing Wildberries api_key")
        # WB Buyer Chat API uses POST /api/v1/seller/message with replySign.
        # replySign is stored in conversation metadata.raw from the chat list.
        meta = conversation.get("metadata") or {}
        raw_data = (meta.get("raw") or {}) if isinstance(meta, Mapping) else {}
        reply_sign = str(raw_data.get("replySign") or "").strip() if isinstance(raw_data, Mapping) else ""

        if not reply_sign and self.chats_api_url:
            # replySign not in stored metadata — fetch fresh from /seller/chats
            try:
                chat_id = str(conversation.get("external_conversation_id") or "").strip()
                if chat_id:
                    reply_sign = self._fetch_fresh_reply_sign(chat_id=chat_id) or ""
            except Exception:
                pass

        if reply_sign and self.chats_api_url:
            # Chat reply via buyer-chat-api
            base_url = self.chats_api_url
            endpoint = _compose_url(base_url, "/api/v1/seller/message")
            request = Request(
                endpoint,
                method="POST",
                headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                data=json.dumps({"replySign": reply_sign, "message": response_text}).encode("utf-8"),
            )
            raw = _request_json(request=request, timeout=self.timeout, source="wb",
                                retries=2, retry_5xx=True)
            _raise_if_error_payload(raw, source="wb")
            # Store the WB addTime so caller can look up the eventID
            if isinstance(raw, dict):
                result_obj = raw.get("result") or {}
                if isinstance(result_obj, dict):
                    wb_add_time = result_obj.get("addTime")
                    if wb_add_time:
                        self._last_sent_add_time = int(wb_add_time)
            return True

        external_id = str(conversation.get("external_conversation_id") or conversation.get("external_id") or "").strip()
        if not external_id:
            raise MarketplaceSyncError("wb", "Missing external conversation id for reply")

        # For WB questions, use PATCH /api/v1/questions with nested answer payload.
        # WB reviews (feedbacks) use POST /api/v1/feedbacks/answer with flat payload.
        kind = str(conversation.get("kind") or "").lower()
        if kind == "question":
            # WB Questions API: PATCH /api/v1/questions
            # Requires "state": "wbRu" (answered by seller) in addition to answer text.
            endpoint = _compose_url(self.api_url, "/api/v1/questions")
            payload_q: dict[str, object] = {
                "id": external_id,
                "state": "wbRu",
                "wasViewed": True,
                "answer": {"text": response_text},
            }
            request = Request(
                endpoint,
                method="PATCH",
                headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                data=json.dumps(payload_q).encode("utf-8"),
            )
            raw = _request_json(request=request, timeout=self.timeout, source="wb", retries=1)
            if isinstance(raw, dict):
                _raise_if_error_payload(raw, source="wb")
            return True

        # Fallback: legacy reply path for other conversation types
        if not self.reply_path:
            return False
        payload: dict[str, object] = dict(self.reply_payload or {})
        payload[self.reply_review_id_field] = external_id
        payload[self.reply_text_field] = response_text
        method = self.reply_method.strip().upper() if self.reply_method else "POST"
        endpoint = _compose_url(self.api_url, self.reply_path)
        if method == "GET":
            query = urlencode(payload)
            url = f"{endpoint}?{query}" if "?" not in endpoint else f"{endpoint}&{query}"
            request = Request(url, method="GET", headers={"Authorization": self.api_key})
        else:
            request = Request(
                endpoint,
                method="POST",
                headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                data=json.dumps(payload).encode("utf-8"),
            )
        raw = _request_json(request=request, timeout=self.timeout, source="wb", retries=1)
        _raise_if_error_payload(raw, source="wb")
        return True

    @staticmethod
    def _to_review(item: dict[str, object]) -> ReviewInput:
        # Combine all text fields so nothing is lost when sent to Yandex GPT.
        # WB uses separate fields: text (main review), pros (достоинства), cons (недостатки).
        # If text is empty but pros/cons exist — treat as review with text (goes through GPT).
        _parts = [str(item.get("text") or ""), str(item.get("pros") or ""), str(item.get("cons") or "")]
        text = " ".join(p.strip() for p in _parts if p.strip())
        review_tags = _extract_review_tags_from_payload(item)
        return ReviewInput(
            review_id=str(item.get("id") or item.get("feedbackId") or item.get("review_id") or ""),
            text=text,
            author=str(item.get("userName") or item.get("author") or "") or None,
            rating=_to_int(item.get("productValuation") or item.get("rating")),
            metadata={"raw": item, "marketplace": "wb", "review_tags": review_tags},
        )

    @staticmethod
    def _to_conversation(item: dict[str, object], *, kind: str) -> dict[str, object] | None:
        external_id = str(
            item.get("id")
            or item.get("chatId")
            or item.get("chatID")
            or item.get("chat_id")
            or item.get("questionId")
            or item.get("questionID")
            or item.get("question_id")
            or item.get("dialogId")
            or item.get("dialog_id")
            or item.get("conversationId")
            or item.get("conversation_id")
            or ""
        ).strip()
        if not external_id:
            return None
        last_message = item.get("lastMessage") if isinstance(item.get("lastMessage"), Mapping) else None
        text = str(
            item.get("text")
            or item.get("message")
            or item.get("question")
            or item.get("lastMessageText")
            or item.get("last_message_text")
            or (last_message.get("text") if isinstance(last_message, Mapping) else "")
            or (last_message.get("message") if isinstance(last_message, Mapping) else "")
            or ""
        )
        customer_name = str(item.get("userName") or item.get("author") or item.get("clientName") or "") or None
        # WB questions: determine answered status from 'answer' field and 'state' field.
        # answer != null means the seller has already replied (via WB portal or API).
        _answer = item.get("answer")
        _state = str(item.get("state") or "").lower()
        _has_answer = bool(_answer and isinstance(_answer, dict) and _answer.get("text"))
        _is_answered_state = _state in ("wbru",)
        if kind == "question" and (_has_answer or _is_answered_state):
            status = "answered_manual"  # moves to Processed bucket
        else:
            status = str(item.get("status") or "open").lower()
        unread_raw = (
            item.get("unread_count")
            if item.get("unread_count") is not None
            else item.get("unreadCount")
            if item.get("unreadCount") is not None
            else item.get("newMessages")
        )
        # WB questions use `createdDate` as the question's timestamp.
        # Include it first so questions sort by creation date, not sync time.
        last_message_at_raw = (
            item.get("createdDate")
            or item.get("updatedAt")
            or item.get("updated_at")
            or item.get("last_message_at")
            or item.get("lastMessageAt")
            or (last_message.get("addTimestamp") if isinstance(last_message, Mapping) else None)
            or (last_message.get("createdAt") if isinstance(last_message, Mapping) else None)
            or (last_message.get("dateTime") if isinstance(last_message, Mapping) else None)
        )
        last_message_at = _normalize_timestamp(last_message_at_raw)
        # last_message_at is NOT NULL in DB — use any available timestamp as fallback
        if not last_message_at:
            last_message_at = _normalize_timestamp(
                item.get("createdAt") or item.get("createTime") or item.get("created_at")
                or item.get("addTime") or item.get("add_time")
            )
        # Final fallback: use a sentinel old date (NOT current time).
        # Using datetime.now() would make last_message_at > last_sent_at,
        # which incorrectly moves manually-answered chats back to "New".
        # A very old date satisfies NOT NULL and never triggers the "buyer wrote last" logic.
        if not last_message_at:
            last_message_at = "2000-01-01T00:00:00+00:00"
        # For questions answered on the WB portal, set seller_replied_at so that
        # last_sent_at is populated → question moves to "Processed" bucket.
        # We use createdDate as the best available timestamp (WB doesn't return
        # the answer timestamp in the question list endpoint).
        seller_replied_at: str | None = None
        if kind == "question" and (_has_answer or _is_answered_state):
            seller_replied_at = _normalize_timestamp(item.get("createdDate")) or _normalize_timestamp(None)
        return {
            "external_id": external_id,
            "kind": kind,
            "customer_name": customer_name,
            "message_text": text,
            # Allow "answered_manual" to pass — it signals portal-answered questions.
            "status": status if status in {"open", "closed", "waiting", "answered_manual"} else "open",
            "unread_count": _to_positive_int(unread_raw, default=0),
            "last_message_at": last_message_at,
            "seller_replied_at": seller_replied_at,
            "metadata": {"raw": item, "marketplace": "wb"},
        }


@dataclass(slots=True)
class YandexMarketClient:
    """Yandex Market Partner API client — reviews + questions (no chats)."""

    api_url: str = "https://api.partner.market.yandex.ru"
    api_key: str = ""
    business_id: str = ""
    page_size: int = 50
    max_pages: int = 2000
    timeout: int = 20
    # Auto-sync reconcile metadata (slots=True forbids dynamic attrs).
    _last_reviews_fetch_truncated: bool = False
    _last_reviews_fetch_from: str | None = None
    _last_questions_fetch_truncated: bool = False
    _last_questions_fetch_from: str | None = None

    def _post(
        self,
        path: str,
        body: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        base_url = _compose_url(self.api_url, path)
        if params:
            qs = urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{base_url}?{qs}"
        else:
            url = base_url
        req = Request(
            url,
            method="POST",
            headers={"Content-Type": "application/json", "Api-Key": self.api_key},
            data=json.dumps(body or {}).encode("utf-8"),
        )
        result = _request_json(request=req, timeout=self.timeout, source="yandex")
        if not isinstance(result, dict):
            raise MarketplaceSyncError("yandex", "ЯМ API вернул не JSON-объект")
        return result

    # ── Reviews ──────────────────────────────────────────────────────────────

    # Official getGoodsFeedbacks filter: reactionStatus = ALL | NEED_REACTION.
    # Docs: https://yandex.ru/dev/market/partner-api/doc/ru/reference/goods-feedback/getGoodsFeedbacks
    YM_REACTION_ALL = "ALL"
    YM_REACTION_NEED = "NEED_REACTION"
    # Default API window without dateTimeFrom is ~6 months; keep the same safety cap.
    YM_FEEDBACK_WINDOW_DAYS = 165
    # getGoodsQuestions: max interval is 1 month when dateFrom/dateTo are used;
    # without dateFrom the API also returns ~1 month. Keep a safe 28-day cap.
    YM_QUESTIONS_WINDOW_DAYS = 28

    def fetch_reviews(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        reaction_status: str | None = None,
    ) -> list[ReviewInput]:
        if not self.api_key or not self.business_id:
            raise MarketplaceSyncError("yandex", "Не заданы Api-Key / business_id")
        return list(
            self.fetch_reviews_iter(
                since_date=since_date,
                stop_requested=stop_requested,
                reaction_status=reaction_status,
            )
        )

    def fetch_reviews_iter(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        reaction_status: str | None = None,
    ):
        """Page-by-page generator so progress bar updates incrementally.

        YM API: limit + page_token are QUERY params; filter fields go in the body.
        ``reaction_status`` maps to body ``reactionStatus`` (ALL | NEED_REACTION).
        """
        if not self.api_key or not self.business_id:
            raise MarketplaceSyncError("yandex", "Не заданы Api-Key / business_id")
        page_token: str | None = None
        page = 0
        path = f"/v2/businesses/{self.business_id}/goods-feedback"
        # Filter body — dateTimeFrom / reactionStatus per Partner API docs.
        # YM API enforces a maximum interval of 6 months between dateTimeFrom
        # and the current date. Cap the date to at most 165 days (5.5 months)
        # ago to stay safely within the limit.
        self._last_reviews_fetch_truncated = False
        filter_body: dict[str, object] = {}
        normalized_reaction = str(reaction_status or "").strip().upper()
        if normalized_reaction in {self.YM_REACTION_ALL, self.YM_REACTION_NEED}:
            filter_body["reactionStatus"] = normalized_reaction
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _max_days = int(self.YM_FEEDBACK_WINDOW_DAYS)
        _earliest_allowed = (_dt.now(_tz.utc) - _td(days=_max_days)).strftime("%Y-%m-%d")
        if since_date:
            _effective_since = max(since_date[:10], _earliest_allowed)
            filter_body["dateTimeFrom"] = f"{_effective_since}T00:00:00+03:00"
            self._last_reviews_fetch_from = _effective_since
            if _effective_since != since_date[:10]:
                _log.info(
                    "YandexMarketClient: dateTimeFrom capped from %s to %s (6-month API limit)",
                    since_date[:10], _effective_since,
                )
        else:
            # API default without dateTimeFrom is ~6 months before dateTimeTo.
            self._last_reviews_fetch_from = _earliest_allowed
        truncated = False
        while page < self.max_pages:
            _raise_if_stop_requested(stop_requested, source="yandex")
            if page > 0:
                time.sleep(0.2)
            # limit and page_token are query parameters
            qparams: dict[str, object] = {"limit": self.page_size}
            if page_token:
                qparams["page_token"] = page_token
            try:
                resp = self._post(path, body=filter_body, params=qparams)
            except Exception as exc:
                raise MarketplaceSyncError("yandex", f"Ошибка API отзывов ЯМ: {exc}") from exc
            status_val = str(resp.get("status") or "").upper()
            result = resp.get("result") or {}
            feedbacks = result.get("feedbacks") or []
            _log.info(
                "YandexMarketClient.fetch_reviews_iter: page=%d status=%s feedbacks=%d reactionStatus=%s errors=%s",
                page,
                status_val,
                len(feedbacks),
                filter_body.get("reactionStatus") or "default",
                resp.get("errors"),
            )
            if status_val != "OK":
                raise MarketplaceSyncError("yandex", f"ЯМ API вернул ошибку: {resp.get('errors') or resp}")
            if not feedbacks:
                break
            for item in feedbacks:
                rv = self._to_review(item)
                if rv.review_id:
                    yield rv
            page += 1
            next_token = (result.get("paging") or {}).get("nextPageToken")
            if not next_token:
                break
            if page >= self.max_pages:
                truncated = True
                break
            page_token = next_token
        # Expose truncation to callers via attribute (auto-reconcile must not run if truncated).
        self._last_reviews_fetch_truncated = truncated

    def count_pending(self, *, since_date: str | None = None) -> dict[str, int]:
        """Estimate pending YM reviews/questions by fetching one page of pending items."""
        counts: dict[str, int] = {"reviews": 0, "questions": 0, "chats": 0}
        try:
            path = f"/v2/businesses/{self.business_id}/goods-feedback"
            body = self._post(
                path,
                body={"reactionStatus": self.YM_REACTION_NEED},
                params={"limit": 1},
            )
            result = body.get("result") or {}
            counts["reviews"] = 1 if result.get("feedbacks") else 0
        except Exception:
            pass
        try:
            path = f"/v1/businesses/{self.business_id}/goods-questions"
            body = self._post(path, body={"needAnswer": True}, params={"limit": 1})
            result = body.get("result") or {}
            counts["questions"] = 1 if result.get("questions") else 0
        except Exception:
            pass
        return counts

    def _to_review(self, item: dict[str, object]) -> ReviewInput:
        review_id = str(item.get("id") or item.get("feedbackId") or "")
        # Text: YM uses description.advantages / description.disadvantages
        desc = item.get("description") or {}
        if isinstance(desc, dict):
            parts = [
                str(desc.get("advantages") or desc.get("positive") or ""),
                str(desc.get("disadvantages") or desc.get("negative") or ""),
                str(desc.get("body") or desc.get("comment") or ""),
            ]
            text = " ".join(p.strip() for p in parts if p.strip()) or str(item.get("text") or "")
        else:
            text = str(item.get("text") or "")
        author = str(item.get("author") or "") or None
        # Rating is nested under statistics.rating
        stats = item.get("statistics") or {}
        rating_raw = stats.get("rating") if isinstance(stats, dict) else item.get("rating")
        rating = int(rating_raw) if rating_raw is not None else None
        # Store under "raw" key matching WB/Ozon metadata conventions
        created_at = str(item.get("createdAt") or "")
        identifiers = item.get("identifiers") or {}
        offer_id = str(identifiers.get("offerId") or "")
        model_id = str(identifiers.get("modelId") or "")
        return ReviewInput(
            review_id=review_id,
            text=text,
            author=author,
            rating=rating,
            metadata={
                "raw": {
                    **item,
                    "createdDate": created_at,      # for sort/date filter
                    "supplierArticle": offer_id,     # артикул → shown in product column
                    "productName": "",               # YM API doesn't return product name
                    "nmId": model_id,                # modelId → used for product link
                },
                "source_marketplace": "yandex",
            },
        )

    # ── Questions ─────────────────────────────────────────────────────────────

    def _fetch_need_answer_question_ids(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> set[str] | None:
        """Return question IDs that still need a seller answer.

        The goods-questions list payload often omits ``answersCount``, so we
        cannot reliably detect answered questions from the full list alone.
        ``needAnswer=true`` is the supported filter for unanswered questions.
        Returns ``None`` when the helper request fails, so callers can fall
        back without marking everything as answered.
        """
        try:
            need_answer_ids: set[str] = set()
            page_token: str | None = None
            page = 0
            path = f"/v1/businesses/{self.business_id}/goods-questions"
            while page < self.max_pages:
                _raise_if_stop_requested(stop_requested, source="yandex")
                if page > 0:
                    time.sleep(0.2)
                qparams: dict[str, object] = {"limit": self.page_size}
                if page_token:
                    qparams["page_token"] = page_token
                body = self._post(path, body={"needAnswer": True}, params=qparams)
                if str(body.get("status") or "").upper() != "OK":
                    return None
                result = body.get("result") or {}
                questions = result.get("questions") or []
                if not questions:
                    break
                for q in questions:
                    if not isinstance(q, dict):
                        continue
                    ids = q.get("questionIdentifiers") or {}
                    external_id = str(
                        (ids.get("id") or ids.get("questionId") if isinstance(ids, dict) else None)
                        or q.get("id")
                        or ""
                    ).strip()
                    if external_id:
                        need_answer_ids.add(external_id)
                page += 1
                next_token = (result.get("paging") or {}).get("nextPageToken")
                if not next_token:
                    break
                page_token = str(next_token)
            return need_answer_ids
        except Exception as exc:
            _log.warning("yandex needAnswer question ids fetch failed: %s", exc)
            return None

    def fetch_questions(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        since_date: str | None = None,
        pending_only: bool = False,
    ) -> list[dict[str, object]]:
        """Fetch YM goods-questions.

        Docs: https://yandex.ru/dev/market/partner-api/doc/ru/reference/goods-questions/getGoodsQuestions
        - ``needAnswer=true`` — only questions waiting for an answer
        - ``needAnswer=false`` (default) — all questions

        Auto-sync uses ``pending_only=True`` (single needAnswer pass).
        Manual sync keeps the full catalog + needAnswer id set for portal-answered detection.
        """
        if not self.api_key or not self.business_id:
            raise MarketplaceSyncError("yandex", "Не заданы Api-Key / business_id")

        self._last_questions_fetch_truncated = False
        filter_body: dict[str, object] = {}
        # Track the effective lower bound used for this fetch (ISO date) so reconcile
        # cannot mark older local open questions as portal-answered.
        self._last_questions_fetch_from = None
        if since_date:
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            _max_days = int(self.YM_QUESTIONS_WINDOW_DAYS)
            _earliest_allowed = (_dt.now(_tz.utc) - _td(days=_max_days)).strftime("%Y-%m-%d")
            _effective_since = max(str(since_date)[:10], _earliest_allowed)
            filter_body["dateFrom"] = _effective_since
            self._last_questions_fetch_from = _effective_since
            if _effective_since != str(since_date)[:10]:
                _log.info(
                    "YandexMarketClient: questions dateFrom capped from %s to %s (1-month API limit)",
                    str(since_date)[:10],
                    _effective_since,
                )
        else:
            # API default window is ~1 month before dateTo.
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            self._last_questions_fetch_from = (
                _dt.now(_tz.utc) - _td(days=int(self.YM_QUESTIONS_WINDOW_DAYS))
            ).strftime("%Y-%m-%d")

        if pending_only:
            # Lightweight auto-sync path: one filtered pass, no full-catalog second walk.
            filter_body["needAnswer"] = True
            items: list[dict[str, object]] = []
            page_token: str | None = None
            page = 0
            truncated = False
            path = f"/v1/businesses/{self.business_id}/goods-questions"
            while page < self.max_pages:
                _raise_if_stop_requested(stop_requested, source="yandex")
                if page > 0:
                    time.sleep(0.2)
                qparams: dict[str, object] = {"limit": self.page_size}
                if page_token:
                    qparams["page_token"] = page_token
                try:
                    body = self._post(path, body=filter_body, params=qparams)
                except Exception as exc:
                    raise MarketplaceSyncError("yandex", f"Ошибка API вопросов ЯМ: {exc}") from exc
                if str(body.get("status") or "").upper() != "OK":
                    raise MarketplaceSyncError(
                        "yandex", f"ЯМ API вопросов вернул ошибку: {body.get('errors') or body}"
                    )
                result = body.get("result") or {}
                questions = result.get("questions") or []
                if not questions:
                    break
                for q in questions:
                    # All rows from needAnswer=true are open / waiting for answer.
                    conv = self._to_question(q, need_answer_ids=None, force_open=True)
                    if conv:
                        items.append(conv)
                page += 1
                next_token = (result.get("paging") or {}).get("nextPageToken")
                if not next_token:
                    break
                if page >= self.max_pages:
                    truncated = True
                    break
                page_token = str(next_token)
            self._last_questions_fetch_truncated = truncated
            return items

        need_answer_ids = self._fetch_need_answer_question_ids(stop_requested=stop_requested)
        items = []
        page_token = None
        page = 0
        truncated = False
        while page < self.max_pages:
            _raise_if_stop_requested(stop_requested, source="yandex")
            if page > 0:
                time.sleep(0.2)
            path = f"/v1/businesses/{self.business_id}/goods-questions"
            qparams = {"limit": self.page_size}
            if page_token:
                qparams["page_token"] = page_token
            try:
                # Full catalog for manual sync (needAnswer defaults to false).
                body = self._post(path, body=filter_body, params=qparams)
            except Exception as exc:
                raise MarketplaceSyncError("yandex", f"Ошибка API вопросов ЯМ: {exc}") from exc
            if str(body.get("status") or "").upper() != "OK":
                raise MarketplaceSyncError("yandex", f"ЯМ API вопросов вернул ошибку: {body.get('errors') or body}")
            result = body.get("result") or {}
            questions = result.get("questions") or []
            if not questions:
                break
            for q in questions:
                conv = self._to_question(q, need_answer_ids=need_answer_ids)
                if conv:
                    items.append(conv)
            page += 1
            next_token = (result.get("paging") or {}).get("nextPageToken")
            if not next_token:
                break
            if page >= self.max_pages:
                truncated = True
                break
            page_token = next_token
        self._last_questions_fetch_truncated = truncated
        return items

    def _to_question(
        self,
        item: dict[str, object],
        *,
        need_answer_ids: set[str] | None = None,
        force_open: bool = False,
    ) -> dict[str, object] | None:
        ids = item.get("questionIdentifiers") or {}
        # YM API returns "id" inside questionIdentifiers (not "questionId")
        external_id = str(
            (ids.get("id") or ids.get("questionId") if isinstance(ids, dict) else None)
            or item.get("id") or ""
        ).strip()
        if not external_id:
            return None
        text = str(item.get("text") or "")
        author_obj = item.get("author") or {}
        customer_name = str(
            (author_obj.get("login") or author_obj.get("name") or "")
            if isinstance(author_obj, dict) else ""
        ) or None
        answers_count = int(item.get("answersCount") or 0)
        # Prefer needAnswer filter: list items usually omit answersCount.
        if force_open:
            status = "open"
        elif need_answer_ids is not None:
            status = "open" if external_id in need_answer_ids else "answered_manual"
        else:
            status = "answered_manual" if answers_count > 0 else "open"
        created_at = str(item.get("createdAt") or "")
        # Populate seller_replied_at for answered questions so upsert sets
        # last_sent_at and the question stays in the Processed bucket.
        seller_replied_at = created_at if status == "answered_manual" and created_at else None
        return {
            "external_id": external_id,
            "text": text,
            "customer_name": customer_name,
            "status": status,
            "seller_replied_at": seller_replied_at,
            "last_message_at": created_at,
            "metadata": {
                "raw": {
                    **item,
                    "text": text,           # question text at raw.text for UI
                    "createdDate": created_at,  # for sort/date filter
                },
            },
        }

    def fetch_question_answers(self, question_id: str) -> list[dict[str, object]]:
        """Fetch existing answers to a YM question for display in processed bucket."""
        try:
            path = f"/v1/businesses/{self.business_id}/goods-questions/answers"
            body = self._post(path, body={"questionId": int(question_id)})
            result = body.get("result") or {}
            return list(result.get("answers") or [])
        except Exception:
            return []

    def fetch_conversations(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        return self.fetch_questions(stop_requested=stop_requested)

    def fetch_chats(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        return []  # Yandex Market has no chat API

    def fetch_review_reply_text(self, feedback_id: str) -> str:
        """Fetch existing seller comment for a feedback. Returns empty string if none."""
        try:
            path = f"/v2/businesses/{self.business_id}/goods-feedback/comments"
            body = self._post(path, body={"feedbackId": int(feedback_id)})
            result = body.get("result") or {}
            comments = result.get("comments") or []
            # Find the BUSINESS (seller) comment
            for c in comments:
                if str(c.get("author", {}).get("type") or "").upper() == "BUSINESS":
                    return str(c.get("text") or "")
        except Exception:
            pass
        return ""

    def send_review_reply(self, *, review: ReviewInput, response_text: str) -> bool:
        """Reply to a YM goods-feedback review."""
        try:
            if not review.review_id:
                return False
            path = f"/v2/businesses/{self.business_id}/goods-feedback/comments/update"
            payload: dict[str, object] = {
                "feedbackId": int(review.review_id),
                "comment": {"text": response_text},
            }
            body = self._post(path, body=payload)
            return str(body.get("status") or "").upper() == "OK"
        except Exception:
            return False

    def send_conversation_reply(
        self,
        *,
        conversation: dict[str, object],
        response_text: str,
    ) -> bool:
        try:
            kind = str(conversation.get("kind") or "question")
            # DB rows use external_conversation_id; sync rows may use external_id
            ext_id = str(
                conversation.get("external_conversation_id")
                or conversation.get("external_id")
                or ""
            ).strip()
            if not ext_id:
                return False
            if kind == "question":
                path = f"/v1/businesses/{self.business_id}/goods-questions/update"
                # questionId must be integer per YM API spec
                try:
                    q_id = int(ext_id)
                except (ValueError, TypeError):
                    return False
                # Current YM Partner API format:
                # CREATE answer under parent question entity.
                payload: dict[str, object] = {
                    "operationType": "CREATE",
                    "parentEntityId": {"id": q_id, "type": "QUESTION"},
                    "text": response_text,
                }
            else:
                # feedback comment — feedbackId must be integer per YM API spec
                path = f"/v2/businesses/{self.business_id}/goods-feedback/comments/update"
                payload = {"feedbackId": int(ext_id), "comment": {"text": response_text}}
            body = self._post(path, body=payload)
            return str(body.get("status") or "").upper() == "OK"
        except Exception as exc:
            _log.warning("yandex send_conversation_reply failed: %s", exc)
            return False


class MockMarketplaceClient:
    """Demo client for local startup without real marketplace credentials."""

    def fetch_reviews(
        self,
        *,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[ReviewInput]:
        _raise_if_stop_requested(stop_requested, source="mock")
        return [
            ReviewInput(
                review_id="mp-1001",
                text="Отличный товар, доставка быстро пришла.",
                author="Покупатель 1",
                rating=5,
                metadata={"marketplace": "mock"},
            ),
            ReviewInput(
                review_id="mp-1002",
                text="Ужасно. Приложение вылетает при оформлении оплаты.",
                author="Покупатель 2",
                rating=1,
                metadata={"marketplace": "mock"},
            ),
            ReviewInput(
                review_id="mp-1003",
                text="Buy now https://spam.example.com",
                author="Bot",
                rating=5,
                metadata={"marketplace": "mock"},
            ),
        ]

    def fetch_conversations(self, *, stop_requested: Callable[[], bool] | None = None) -> list[dict[str, object]]:
        return self.fetch_questions(stop_requested=stop_requested) + self.fetch_chats(stop_requested=stop_requested)

    def fetch_questions(self, *, stop_requested: Callable[[], bool] | None = None) -> list[dict[str, object]]:
        _raise_if_stop_requested(stop_requested, source="mock")
        return [
            {
                "external_id": "mock-q-1",
                "kind": "question",
                "customer_name": "Покупатель 10",
                "message_text": "Подскажите, ткань не садится после стирки?",
                "status": "open",
                "unread_count": 1,
                "last_message_at": None,
                "metadata": {"marketplace": "mock"},
            },
        ]

    def fetch_chats(self, *, stop_requested: Callable[[], bool] | None = None) -> list[dict[str, object]]:
        _raise_if_stop_requested(stop_requested, source="mock")
        return [
            {
                "external_id": "mock-c-1",
                "kind": "chat",
                "customer_name": "Покупатель 11",
                "message_text": "Здравствуйте, можете уточнить срок доставки?",
                "status": "waiting",
                "unread_count": 2,
                "last_message_at": None,
                "metadata": {"marketplace": "mock"},
            },
        ]


class ReviewAutomationService:
    TEXTLESS_GROUP_ID = "textless_ratings"
    # One subgroup per star rating — cannot be deleted by admins or users
    TEXTLESS_SUBGROUPS: tuple[str, ...] = (
        "1 звезда",
        "2 звезды",
        "3 звезды",
        "4 звезды",
        "5 звезд",
    )
    # Legacy constants kept for DB migration compatibility
    TEXTLESS_LOW_SUBGROUP = "1-3 звезды"
    TEXTLESS_HIGH_SUBGROUP = "4-5 звезд"
    GENERAL_SUBGROUP_TITLE = "Общий"
    REVIEW_GROUPS_WITH_GENERAL_SUBGROUP: tuple[str, ...] = (
        "positive",
        "product_dissatisfaction",
        "delivery_problems",
        "wrong_size",
    )
    AI_UNCLASSIFIED_CATEGORY = "ai_unclassified"
    AI_UNCLASSIFIED_NOTE = "ИИ не смог корректно определить категорию."
    REVIEW_GROUP_DEFAULT_SUBGROUPS: dict[str, list[str]] = {
        "positive": [
            GENERAL_SUBGROUP_TITLE,
            "Вкус",
            "Материал",
            "Общий позитив",
            "Позитив доставка",
            "Позитив запах",
            "Позитив конструкция",
            "Позитив упаковка",
            "Позитив цвет",
            "Эффект",
        ],
        "product_dissatisfaction": [
            GENERAL_SUBGROUP_TITLE,
            "Брак и Б/У",
            "Высокая цена",
            "Качество",
            "Негатив запах",
            "Негатив конструкция",
            "Негатив цвет",
            "Не подошел лично мне",
            "Не соответствует фото",
            "Не устраивает эффект",
            "Общий негатив",
            "Побочные эффекты",
            "Подделка",
            "Срок годности",
            "Текстура, консистенция, материал",
        ],
        "delivery_problems": [
            GENERAL_SUBGROUP_TITLE,
            "Долгая доставка",
            "Испорченная упаковка",
            "Наклейка",
            "Недостающая упаковка / грязное / поврежденное и сломанное",
            "Некомплект",
            "Не тот товар",
            "Общие доставка",
        ],
        "wrong_size": [
            GENERAL_SUBGROUP_TITLE,
            "Альтернативные измерения",
            "Большемерит/маломерит",
            "Не подошел размер",
        ],
        TEXTLESS_GROUP_ID: [
            TEXTLESS_LOW_SUBGROUP,
            TEXTLESS_HIGH_SUBGROUP,
        ],
    }
    REVIEW_GROUP_TITLES: dict[str, str] = {
        "positive": "Позитив",
        "product_dissatisfaction": "Недовольство товаром",
        "delivery_problems": "Проблемы при доставке",
        "wrong_size": "Неправильный размер",
        "textless_ratings": "Оценки без текста",
    }
    GROUP_PROCESSING_DEFAULTS: dict[str, str] = {
        "positive": "yandex",
        "product_dissatisfaction": "yandex",
        "delivery_problems": "yandex",
        "wrong_size": "yandex",
        "textless_ratings": "program",
    }

    def __init__(self, repository: ReviewRepository, processor: ReviewProcessor | None = None) -> None:
        self.repository = repository
        self.processor = processor or ReviewProcessor()


    def _ym_created_on_or_after(self, created_raw: object | None, *, from_date: str | None) -> bool:
        """True when created timestamp is on/after from_date (YYYY-MM-DD).

        Unknown/unparseable created times are excluded — safer than false-positive
        portal-answered reconcile.
        """
        lower = str(from_date or "").strip()[:10]
        if not lower:
            return False
        raw = str(created_raw or "").strip()
        if not raw:
            return False
        try:
            from datetime import datetime as _dt, timezone as _tz
            normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
            if "T" in normalized:
                created = _dt.fromisoformat(normalized)
            else:
                created = _dt.fromisoformat(normalized[:10]).replace(tzinfo=_tz.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=_tz.utc)
            return created.astimezone(_tz.utc).date().isoformat() >= lower
        except Exception:
            return False

    def _reconcile_yandex_portal_answered_reviews(
        self,
        *,
        user_id: int,
        account_id: int,
        client: MarketplaceClient,
        seen_pending_ids: set[str],
        existing_states: dict[str, dict[str, object]],
        fetch_from: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> int:
        """Mark local open YM reviews as answered when they disappeared from NEED_REACTION.

        Only considers reviews inside the same date window as the pending fetch.
        """
        del account_id
        effective_from = str(
            fetch_from
            or getattr(client, "_last_reviews_fetch_from", None)
            or ""
        ).strip()[:10]
        if not effective_from:
            return 0
        open_statuses = {"queued_for_operator", "pending", "open", "new", "awaiting", ""}
        reconciled = 0
        seen_uids: set[str] = set()
        for ext_id, state in list(existing_states.items()):
            _raise_if_stop_requested(stop_requested, source="yandex")
            external_id = str(state.get("external_review_id") or ext_id or "").strip()
            review_uid = str(state.get("review_uid") or "").strip()
            if not external_id or not review_uid or review_uid in seen_uids:
                continue
            seen_uids.add(review_uid)
            status = str(state.get("status") or "").strip()
            if status in {"answered_manual", "answered_auto", "ignored"}:
                continue
            if status and status not in open_statuses and status.startswith("answered"):
                continue
            if external_id in seen_pending_ids:
                continue
            created_raw = state.get("marketplace_created") or state.get("created_at")
            if not self._ym_created_on_or_after(created_raw, from_date=effective_from):
                continue
            reply_text = str(state.get("auto_reply") or "").strip()
            if not reply_text:
                try:
                    reply_text = getattr(client, "fetch_review_reply_text", lambda _: "")(external_id) or ""
                except Exception:
                    reply_text = ""
            if self.repository.update_review_processing_result(
                user_id=user_id,
                review_uid=review_uid,
                status="answered_manual",
                auto_reply=reply_text or None,
            ):
                reconciled += 1
        return reconciled

    def _reconcile_yandex_portal_answered_questions(
        self,
        *,
        user_id: int,
        account_id: int,
        client: MarketplaceClient,
        seen_pending_ids: set[str],
        existing_states: dict[str, dict[str, object]],
        fetch_from: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> int:
        """Mark local open YM questions as answered when they left needAnswer=true set.

        Uses the questions API window (~1 month), not the reviews 6-month window.
        Auto-sync only flips status (no answers N+1).
        """
        del account_id
        effective_from = str(
            fetch_from
            or getattr(client, "_last_questions_fetch_from", None)
            or ""
        ).strip()[:10]
        if not effective_from:
            return 0
        reconciled = 0
        for external_id, state in existing_states.items():
            _raise_if_stop_requested(stop_requested, source="yandex")
            ext = str(state.get("external_conversation_id") or external_id or "").strip()
            conversation_uid = str(state.get("conversation_uid") or "").strip()
            if not ext or not conversation_uid:
                continue
            status = str(state.get("status") or "").strip()
            if status in {"answered_manual", "answered_auto", "ignored", "closed"}:
                continue
            if ext in seen_pending_ids:
                continue
            created_raw = state.get("last_message_at") or state.get("created_at")
            if not self._ym_created_on_or_after(created_raw, from_date=effective_from):
                continue
            if self.repository.mark_conversation_answered(
                user_id=user_id,
                conversation_uid=conversation_uid,
            ):
                reconciled += 1
        return reconciled

    def sync_reviews(
        self,
        *,
        user_id: int,
        source: str,
        account_id: int | None,
        client: MarketplaceClient,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        apply_date_filter: bool = False,
    ) -> int:
        # Use streaming (page-by-page) fetcher if available to avoid holding
        # 200k+ reviews in memory at once.  Falls back to bulk fetch_reviews.
        # Auto-sync (apply_date_filter=False) for Yandex uses reactionStatus=NEED_REACTION
        # per Partner API docs — avoids re-paging the full 6-month corpus every minute.
        _fetch_iter = getattr(client, "fetch_reviews_iter", None)
        ym_incremental = (source == "yandex" and not apply_date_filter)
        fetch_kwargs: dict[str, object] = {
            "since_date": since_date,
            "stop_requested": stop_requested,
        }
        if ym_incremental:
            fetch_kwargs["reaction_status"] = getattr(client, "YM_REACTION_NEED", "NEED_REACTION")
        try:
            if callable(_fetch_iter):
                try:
                    reviews_iterable = _fetch_iter(**fetch_kwargs)
                except TypeError:
                    try:
                        reviews_iterable = _fetch_iter(
                            since_date=since_date, stop_requested=stop_requested
                        )
                    except TypeError:
                        reviews_iterable = _fetch_iter()
            else:
                try:
                    reviews_iterable = client.fetch_reviews(**fetch_kwargs)
                except TypeError:
                    try:
                        reviews_iterable = client.fetch_reviews(
                            since_date=since_date, stop_requested=stop_requested
                        )
                    except TypeError:
                        reviews_iterable = client.fetch_reviews()
        except MarketplaceSyncError as exc:
            if not self._is_access_error(exc):
                self.repository.log_review_action(
                    user_id=user_id,
                    review_uid=None,
                    action_type="sync_error",
                    actor="system",
                    details={"source": source, "account_id": account_id, "error": str(exc), **exc.details},
                )
            raise
        reviews = reviews_iterable  # may be a list or a generator

        # ── Auto-retry failed sends ────────────────────────────────────────────
        # Before processing new reviews, retry any previously failed auto-replies
        # that still have auto_reply text saved and haven't exceeded max attempts.
        if user_id:
            try:
                pending_retries = self.repository.get_pending_retry_reviews(
                    user_id=user_id, max_attempts=3
                )
                if pending_retries:
                    _log.info(
                        "sync_reviews: retrying %d failed auto-replies (account_id=%s source=%s)",
                        len(pending_retries), account_id, source,
                    )
                for pr in pending_retries:
                    _raise_if_stop_requested(stop_requested, source=source)
                    pr_uid = str(pr.get("review_uid") or "").strip()
                    pr_text = str(pr.get("auto_reply") or "").strip()
                    pr_account = pr.get("account_id")
                    pr_source = str(pr.get("source") or "").strip()
                    # Only retry if same source/account as current sync run
                    if not pr_uid or not pr_text or pr_source != source:
                        continue
                    if pr_account is not None and account_id is not None and int(pr_account) != int(account_id):
                        continue
                    pr_review = ReviewInput(
                        review_id=str(pr.get("external_review_id") or ""),
                        text=str(pr.get("text") or ""),
                        author=str(pr.get("author") or "") or None,
                        rating=pr.get("rating"),
                        metadata=pr.get("metadata") or {},
                    )
                    if not pr_review.review_id:
                        continue
                    retry_sent, retry_error = self._send_reply_via_client(
                        client=client,
                        source=source,
                        review=pr_review,
                        response_text=pr_text,
                    )
                    if retry_sent:
                        self.repository.clear_review_send_error(user_id=user_id, review_uid=pr_uid)
                        self.repository.update_review_processing_result(
                            user_id=user_id,
                            review_uid=pr_uid,
                            status="answered_auto",
                            auto_reply=pr_text,
                        )
                        _log.info("sync_reviews: retry SUCCESS for %s", pr_uid[:40])
                    else:
                        self.repository.mark_review_send_error(
                            user_id=user_id,
                            review_uid=pr_uid,
                            error_message=str(retry_error or "Retry failed"),
                        )
                        _log.info("sync_reviews: retry FAILED for %s: %s", pr_uid[:40], retry_error)
            except Exception as _retry_exc:
                _log.warning("sync_reviews: retry step error: %s", _retry_exc)
        # ── End auto-retry ────────────────────────────────────────────────────

        settings = self.repository.get_ai_settings(include_secrets=True)
        # Load contradiction rules once for all reviews in this sync
        contradiction_map: dict[str, set[int]] = {}
        if user_id:
            try:
                contradiction_map = self.repository.get_review_contradiction_map(user_id=user_id)
            except Exception:
                contradiction_map = {}
        classification_options = self._list_group_subgroups_for_review_classification(
            repository=self.repository,
            user_id=user_id,
        )
        subgroup_lookup_by_group: dict[str, dict[str, str]] = {}
        for item in classification_options:
            group_id = str(item.get("group_id") or "").strip()
            subgroup_items = item.get("subgroup_items")
            if not group_id or not isinstance(subgroup_items, list):
                continue
            subgroup_lookup: dict[str, str] = {}
            for subgroup_item in subgroup_items:
                if not isinstance(subgroup_item, Mapping):
                    continue
                subgroup_title = str(subgroup_item.get("subgroup") or subgroup_item.get("subgroup_title") or "").strip()
                if not subgroup_title:
                    continue
                normalized = self._normalize_subgroup_name(subgroup_title)
                if normalized and normalized not in subgroup_lookup:
                    subgroup_lookup[normalized] = subgroup_title
            if subgroup_lookup:
                subgroup_lookup_by_group[group_id] = subgroup_lookup

        def _resolve_classified_subgroup(group_id: str, subgroup: str | None) -> str | None:
            clean_group = str(group_id or "").strip()
            clean_subgroup = str(subgroup or "").strip()
            if clean_group not in self.REVIEW_GROUPS_WITH_GENERAL_SUBGROUP:
                return clean_subgroup or None
            lookup = subgroup_lookup_by_group.get(clean_group) or {}
            general_subgroup = lookup.get(
                self._normalize_subgroup_name(self.GENERAL_SUBGROUP_TITLE),
                self.GENERAL_SUBGROUP_TITLE,
            )
            if not clean_subgroup:
                return general_subgroup
            normalized = self._normalize_subgroup_name(clean_subgroup)
            if normalized in lookup:
                return lookup[normalized]
            return general_subgroup

        # Build an ISO cutoff for createdDate filtering.
        # WB dateFrom param filters by update date (not creation date), so it
        # cannot reliably exclude old reviews. We filter client-side by
        # createdDate stored in review.metadata["raw"]["createdDate"].
        since_iso_cutoff: str | None = _normalize_timestamp(since_date) if since_date else None

        # Load already-classified reviews to avoid re-sending to Yandex on repeated syncs.
        existing_classifications: dict[str, tuple[str, str]] = {}
        if user_id:
            try:
                existing_classifications = self.repository.get_existing_classifications(user_id=user_id)
                _log.info("sync_reviews: loaded %d existing classifications (will skip Yandex for these)", len(existing_classifications))
            except Exception as _exc:
                _log.warning("sync_reviews: could not load existing classifications: %s", _exc)

        # Per-account status/reply map — skip N+1 comments API + no-op upserts for YM.
        existing_review_states: dict[str, dict[str, object]] = {}
        if user_id and source == "yandex" and account_id is not None:
            try:
                existing_review_states = self.repository.get_review_sync_states_for_account(
                    user_id=user_id,
                    source=source,
                    account_id=int(account_id),
                )
            except Exception as _exc:
                _log.warning("sync_reviews: could not load YM review sync states: %s", _exc)
                existing_review_states = {}

        loaded_count = 0
        skipped_old = 0
        skipped_already_classified = 0
        skipped_noop_answered = 0
        seen_pending_review_ids: set[str] = set()
        for review in reviews:
            _raise_if_stop_requested(stop_requested, source=source)
            if not review.review_id:
                continue

            # Skip reviews created before since_date (client-side createdDate filter)
            if since_iso_cutoff:
                raw = review.metadata.get("raw") if isinstance(review.metadata, dict) else {}
                created_raw = str(raw.get("createdDate") or "").strip() if isinstance(raw, dict) else ""
                if created_raw and created_raw < since_iso_cutoff:
                    skipped_old += 1
                    continue

            loaded_count += 1
            if ym_incremental:
                seen_pending_review_ids.add(str(review.review_id))

            # Check if this review was already answered on the marketplace portal.
            # WB: replyText or reply.text. Ozon: comment.text or answer.
            # YM: needReaction=False means the seller already replied (or no reply needed).
            # If a reply exists OR needReaction=False → mark answered_manual, skip pipeline.
            _raw = review.metadata.get("raw") if isinstance(review.metadata, dict) else {}
            _raw = _raw if isinstance(_raw, dict) else {}
            _reply_text = (
                str(_raw.get("replyText") or "").strip()
                or str((_raw.get("reply") or {}).get("text") or "").strip()
                or str((_raw.get("answer") or {}).get("text") or "").strip()
                or str((_raw.get("comment") or {}).get("text") or "").strip()
            )
            # YM: needReaction is stored in raw; False = already handled/answered
            _ym_no_reaction = (source == "yandex" and _raw.get("needReaction") is False)
            review_uid = self.repository.make_review_uid(
                user_id or 0, source, account_id, str(review.review_id)
            )
            _existing_state = existing_review_states.get(str(review.review_id)) or existing_review_states.get(review_uid) or {}
            _existing_status = str(_existing_state.get("status") or "").strip()
            _existing_reply = str(_existing_state.get("auto_reply") or "").strip()

            # Already answered in DB: do not re-fetch comments or rewrite the row every poll.
            # Exception: answered_auto + still needReaction=true must fall through so upsert
            # can reset the bot artefact (repository CASE on needReaction=true).
            if source == "yandex" and _existing_status in {"answered_manual", "ignored"}:
                if _ym_no_reaction or _reply_text or _existing_reply:
                    skipped_noop_answered += 1
                    continue
            if source == "yandex" and _existing_status == "answered_auto" and (_ym_no_reaction or _reply_text):
                skipped_noop_answered += 1
                continue

            # For YM already-answered reviews: fetch seller reply only when we do not have it yet.
            if _ym_no_reaction and not _reply_text:
                if _existing_reply:
                    _reply_text = _existing_reply
                else:
                    try:
                        _reply_text = getattr(client, "fetch_review_reply_text", lambda _: "")(review.review_id) or ""
                    except Exception:
                        pass
            if _reply_text or _ym_no_reaction:
                review_metadata = dict(review.metadata) if isinstance(review.metadata, dict) else {}
                # Use cached classification if available and valid; otherwise classify now
                _cached_for_answered = existing_classifications.get(review_uid)
                if _cached_for_answered and _cached_for_answered[0] != self.AI_UNCLASSIFIED_CATEGORY:
                    _answered_category = _cached_for_answered[0]
                    if _cached_for_answered[1]:
                        review_metadata["classified_subgroup"] = _cached_for_answered[1]
                    review_metadata["classified_group_id"] = _answered_category
                else:
                    # Not cached or still ai_unclassified — run Yandex AI now
                    try:
                        _answered_category, _answered_sub = self._classify_category_and_subgroup(
                            review, self.processor.process(review),
                            settings=settings, user_id=user_id,
                        )
                        if _answered_sub:
                            review_metadata["classified_subgroup"] = _answered_sub
                        review_metadata["classified_group_id"] = _answered_category
                    except Exception:
                        _answered_category = str(review_metadata.get("classified_group_id") or self.AI_UNCLASSIFIED_CATEGORY)
                self.repository.upsert_processed_review(
                    user_id=user_id,
                    source=source,
                    account_id=account_id,
                    review=ReviewInput(
                        review_id=review.review_id,
                        text=review.text,
                        author=review.author,
                        rating=review.rating,
                        metadata=review_metadata,
                    ),
                    processed=self.processor.process(review),
                    category=_answered_category,
                    processing_mode="manual",
                    status="answered_manual",
                    auto_reply=_reply_text,
                )
                continue

            processed = self.processor.process(review)
            ai_classification_failed = False
            ai_classification_error = ""
            send_error: str | None = None

            # Check if this review was already classified in a previous sync —
            # reuse the existing result to avoid wasteful Yandex API calls.
            review_uid = self.repository.make_review_uid(
                user_id or 0, source, account_id, str(review.review_id)
            )
            # ai_unclassified reviews are never cached — they must be retried with
            # Yandex on every sync so they get properly classified once the API
            # key is restored.  If Yandex still fails → ai_classification_failed
            # is set below and the review stays in manual queue.
            _cached = existing_classifications.get(review_uid)
            if _cached and _cached[0] != self.AI_UNCLASSIFIED_CATEGORY:
                existing_group, existing_sub = _cached
                category = existing_group
                classified_subgroup: str | None = existing_sub or None
                skipped_already_classified += 1
            else:
                try:
                    category, classified_subgroup = self._classify_category_and_subgroup(
                        review,
                        processed,
                        settings=settings,
                        user_id=user_id,
                    )
                except MarketplaceSyncError as exc:
                    details = exc.details if isinstance(exc.details, Mapping) else {}
                    if str(details.get("scope") or "").strip().lower() == "classification":
                        category = self.AI_UNCLASSIFIED_CATEGORY
                        classified_subgroup = None
                        ai_classification_failed = True
                        ai_classification_error = str(exc)
                    else:
                        raise
            if not ai_classification_failed:
                category = str(category or "").strip()
                if not category:
                    ai_classification_failed = True
                    ai_classification_error = "Яндекс-классификатор не вернул корректную группу и подгруппу."
                    category = self.AI_UNCLASSIFIED_CATEGORY
                    classified_subgroup = None
                else:
                    classified_subgroup = _resolve_classified_subgroup(category, classified_subgroup)
            review_metadata = dict(review.metadata) if isinstance(review.metadata, dict) else {}
            if classified_subgroup:
                review_metadata["classified_subgroup"] = classified_subgroup
            review_metadata["classified_group_id"] = category
            if ai_classification_failed:
                review_metadata["ai_classification_status"] = "failed"
                review_metadata["ai_classification_note"] = self.AI_UNCLASSIFIED_NOTE
                review_metadata["ai_classification_error"] = ai_classification_error
            review_for_processing = ReviewInput(
                review_id=review.review_id,
                text=review.text,
                author=review.author,
                rating=review.rating,
                metadata=review_metadata,
            )
            # Check contradiction rule: if Yandex category + rating matches a
            # user-configured rule, flag for manual review (no auto-reply).
            rating_val = review.rating if review.rating is not None else 0
            contradiction_ratings = contradiction_map.get(category, set())
            has_contradiction = bool(
                contradiction_ratings and int(rating_val) in contradiction_ratings
            )
            if has_contradiction:
                group_title = self.REVIEW_GROUP_TITLES.get(category, category)
                review_metadata["rating_contradiction"] = {
                    "yandex_group": category,
                    "yandex_group_title": group_title,
                    "rating": int(rating_val),
                }

            category_for_template = category
            if category_for_template == self.AI_UNCLASSIFIED_CATEGORY:
                category_for_template = "product_dissatisfaction"
            template = self.repository.get_template(user_id=user_id, category=category_for_template)
            group_id = self._resolve_template_group_id(
                category=category,
                review=review_for_processing,
                sentiment=processed.sentiment_label,
            )
            rule = self.repository.get_processing_rule(user_id=user_id, group_id=group_id) if group_id else None
            mode, auto_send, template_text = self._resolve_processing_mode(processed, template, rule)
            if ai_classification_failed or has_contradiction:
                mode = "manual"
                auto_send = False
                template_text = ""
            # For Yandex Market: never auto-send — operator must reply manually.
            # Template text is still prepared so it pre-fills the reply box.
            if source == "yandex" and auto_send:
                auto_send = False

            if mode == "template":
                group_template = self._pick_group_template_text(
                    user_id=user_id,
                    category=category_for_template,
                    review=review_for_processing,
                    sentiment=processed.sentiment_label,
                    preferred_subgroup=classified_subgroup,
                )
                selected_template = str(group_template or template_text or "").strip()
                if not selected_template:
                    status = "queued_for_operator"
                    auto_reply = None
                    self.repository.log_review_action(
                        user_id=user_id,
                        review_uid=self.repository.make_review_uid(user_id, source, account_id, review.review_id),
                        action_type="send_reply_error",
                        actor="system",
                        details={"source": source, "error": "Не найден шаблон для автоматического ответа"},
                    )
                    self.repository.upsert_processed_review(
                        user_id=user_id,
                        source=source,
                        account_id=account_id,
                        review=review_for_processing,
                        processed=processed,
                        category=category,
                        processing_mode=mode,
                        status=status,
                        auto_reply=auto_reply,
                    )
                    continue
                auto_reply = self._render_template(
                    selected_template,
                    user_id=user_id,
                    review=review_for_processing,
                    category=category_for_template,
                    sentiment=processed.sentiment_label,
                )
                sent, send_error = self._send_reply_via_client(
                    client=client,
                    source=source,
                    review=review_for_processing,
                    response_text=auto_reply,
                )
                if sent:
                    status = "answered_auto"
                else:
                    status = "queued_for_operator"
                    # Keep auto_reply — operator needs to see what was tried;
                    # retry logic also uses this text on next sync
                    review_uid_for_error = self.repository.make_review_uid(
                        user_id, source, account_id, review.review_id
                    )
                    self.repository.log_review_action(
                        user_id=user_id,
                        review_uid=review_uid_for_error,
                        action_type="send_reply_error",
                        actor="system",
                        details={"source": source, "error": send_error or "Не удалось отправить ответ"},
                    )
                    # Will be recorded via mark_review_send_error after upsert
            else:
                status = "queued_for_operator"
                auto_reply = None

            # Upsert review (no sync_review log — would create millions of rows)
            self.repository.upsert_processed_review(
                user_id=user_id,
                source=source,
                account_id=account_id,
                review=review_for_processing,
                processed=processed,
                category=category,
                processing_mode=mode,
                status=status,
                auto_reply=auto_reply,
            )
            # After upsert: if send failed and we have auto_reply, record the error details
            if status == "queued_for_operator" and auto_reply and send_error is not None:
                review_uid_err = self.repository.make_review_uid(
                    user_id, source, account_id, review_for_processing.review_id
                )
                self.repository.mark_review_send_error(
                    user_id=user_id,
                    review_uid=review_uid_err,
                    error_message=str(send_error or "Не удалось отправить ответ"),
                    auto_reply=auto_reply,
                )
            review_uid = self.repository.make_review_uid(user_id, source, account_id, review_for_processing.review_id)
            if ai_classification_failed:
                self.repository.log_review_action(
                    user_id=user_id,
                    review_uid=review_uid,
                    action_type="ai_classification_failed",
                    actor="system",
                    details={
                        "source": source,
                        "error": ai_classification_error,
                        "scope": "classification",
                    },
                )
        if skipped_old:
            _log.info(
                "sync_reviews: skipped %d reviews with createdDate < since_date=%s, saved %d",
                skipped_old, since_date, loaded_count,
            )
        if skipped_already_classified:
            _log.info(
                "sync_reviews: skipped Yandex for %d already-classified reviews (no tokens wasted)",
                skipped_already_classified,
            )
        if skipped_noop_answered:
            _log.info(
                "sync_reviews: skipped %d already-answered YM reviews (no comments API / upsert)",
                skipped_noop_answered,
            )

        # Auto-sync only fetched NEED_REACTION. Local open reviews missing from that
        # set were answered/skipped on the portal — mark them without a full catalog walk.
        if (
            ym_incremental
            and user_id
            and account_id is not None
            and not bool(getattr(client, "_last_reviews_fetch_truncated", False))
        ):
            try:
                reconciled = self._reconcile_yandex_portal_answered_reviews(
                    user_id=user_id,
                    account_id=int(account_id),
                    client=client,
                    seen_pending_ids=seen_pending_review_ids,
                    existing_states=existing_review_states,
                    fetch_from=str(getattr(client, "_last_reviews_fetch_from", None) or "") or None,
                    stop_requested=stop_requested,
                )
                if reconciled:
                    _log.info(
                        "sync_reviews: reconciled %d YM reviews answered on portal",
                        reconciled,
                    )
                    loaded_count += reconciled
            except Exception as _exc:
                _log.warning("sync_reviews: YM portal reconcile failed: %s", _exc)
        return loaded_count

    def sync_conversations(
        self,
        *,
        user_id: int,
        source: str,
        account_id: int | None,
        client: MarketplaceClient,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        apply_date_filter: bool = False,
    ) -> int:
        return self.sync_questions(
            user_id=user_id,
            source=source,
            account_id=account_id,
            client=client,
            since_date=since_date,
            stop_requested=stop_requested,
            apply_date_filter=apply_date_filter,
        ) + self.sync_chats(
            user_id=user_id,
            source=source,
            account_id=account_id,
            client=client,
            since_date=since_date,
            stop_requested=stop_requested,
            apply_date_filter=apply_date_filter,
            full_sync=apply_date_filter,
        )

    def sync_questions(
        self,
        *,
        user_id: int,
        source: str,
        account_id: int | None,
        client: MarketplaceClient,
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        apply_date_filter: bool = False,
    ) -> int:
        fetch_questions = getattr(client, "fetch_questions", None)
        if not callable(fetch_questions):
            return 0

        ym_incremental = (source == "yandex" and not apply_date_filter)
        fetch_kwargs: dict[str, object] = {
            "since_date": since_date,
            "stop_requested": stop_requested,
        }
        if ym_incremental:
            # Official filter: needAnswer=true — only questions waiting for an answer.
            fetch_kwargs["pending_only"] = True

        try:
            try:
                rows = fetch_questions(**fetch_kwargs)
            except TypeError:
                try:
                    rows = fetch_questions(since_date=since_date, stop_requested=stop_requested)
                except TypeError:
                    rows = fetch_questions()
        except MarketplaceSyncError as exc:
            if not self._is_access_error(exc):
                self.repository.log_review_action(
                    user_id=user_id,
                    review_uid=None,
                    action_type="sync_error",
                    actor="system",
                    details={
                        "source": source,
                        "account_id": account_id,
                        "error": str(exc),
                        "scope": "questions",
                    },
                )
            raise

        existing_question_states: dict[str, dict[str, object]] = {}
        if user_id and source == "yandex" and account_id is not None:
            try:
                existing_question_states = self.repository.get_question_sync_states_for_account(
                    user_id=user_id,
                    source=source,
                    account_id=int(account_id),
                )
            except Exception as _exc:
                _log.warning("sync_questions: could not load YM question sync states: %s", _exc)
                existing_question_states = {}

        loaded = 0
        skipped_noop = 0
        seen_pending_question_ids: set[str] = set()
        for row in rows:
            _raise_if_stop_requested(stop_requested, source=source)
            external_id = str(row.get("external_id") or "").strip()
            if not external_id:
                continue
            # "text" is the canonical key from YM _to_question; "message_text" for others
            _msg_text = str(row.get("text") or row.get("message_text") or "")
            row_status = str(row.get("status") or "open")
            if ym_incremental and row_status == "open":
                seen_pending_question_ids.add(external_id)

            _existing = existing_question_states.get(external_id) or {}
            _existing_status = str(_existing.get("status") or "").strip()
            _existing_meta = _existing.get("metadata") if isinstance(_existing.get("metadata"), dict) else {}

            # Already answered in DB and still answered on marketplace: skip N+1 answers API + upsert.
            if (
                source == "yandex"
                and row_status == "answered_manual"
                and _existing_status in {"answered_manual", "answered_auto", "ignored"}
                and (
                    isinstance(_existing_meta, dict)
                    and (_existing_meta.get("_ym_answers") or (_existing_meta.get("raw") or {}).get("_ym_answers"))
                )
            ):
                skipped_noop += 1
                continue

            # For YM answered questions: fetch existing seller answer only when missing locally.
            _row_meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if source == "yandex" and row_status == "answered_manual":
                has_answers = bool(
                    isinstance(_row_meta, dict)
                    and (_row_meta.get("_ym_answers") or ((_row_meta.get("raw") or {}) if isinstance(_row_meta.get("raw"), dict) else {}).get("_ym_answers"))
                )
                if not has_answers and isinstance(_existing_meta, dict):
                    prior = _existing_meta.get("_ym_answers") or (
                        (_existing_meta.get("raw") or {}).get("_ym_answers")
                        if isinstance(_existing_meta.get("raw"), dict)
                        else None
                    )
                    if prior:
                        _row_meta = dict(_row_meta or {})
                        raw = dict(_row_meta.get("raw") or {})
                        raw["_ym_answers"] = prior
                        _row_meta["raw"] = raw
                        _row_meta["_ym_answers"] = prior
                        has_answers = True
                if not has_answers:
                    try:
                        _answers = getattr(client, "fetch_question_answers", lambda _: [])(external_id)
                        if _answers:
                            _row_meta = dict(_row_meta or {})
                            raw = dict(_row_meta.get("raw") or {})
                            raw["_ym_answers"] = _answers
                            _row_meta["raw"] = raw
                            _row_meta["_ym_answers"] = _answers
                    except Exception:
                        pass

            conversation_uid = self.repository.upsert_conversation(
                user_id=user_id,
                source=source,
                account_id=account_id,
                external_conversation_id=external_id,
                kind="question",
                customer_name=str(row.get("customer_name") or "") or None,
                message_text=_msg_text,
                status=row_status,
                unread_count=_to_positive_int(row.get("unread_count"), default=0),
                metadata=_row_meta,
                last_message_at=str(row.get("last_message_at") or "") or None,
                seller_replied_at=str(row.get("seller_replied_at") or "") or None,
            )
            loaded += 1

        if skipped_noop:
            _log.info(
                "sync_questions: skipped %d already-answered YM questions (no answers API / upsert)",
                skipped_noop,
            )

        if (
            ym_incremental
            and user_id
            and account_id is not None
            and not bool(getattr(client, "_last_questions_fetch_truncated", False))
        ):
            try:
                reconciled = self._reconcile_yandex_portal_answered_questions(
                    user_id=user_id,
                    account_id=int(account_id),
                    client=client,
                    seen_pending_ids=seen_pending_question_ids,
                    existing_states=existing_question_states,
                    fetch_from=str(getattr(client, "_last_questions_fetch_from", None) or "") or None,
                    stop_requested=stop_requested,
                )
                if reconciled:
                    _log.info(
                        "sync_questions: reconciled %d YM questions answered on portal",
                        reconciled,
                    )
                    loaded += reconciled
            except Exception as _exc:
                _log.warning("sync_questions: YM portal reconcile failed: %s", _exc)
        return loaded

    def sync_chats(
        self,
        *,
        user_id: int,
        source: str,
        account_id: int | None,
        client: MarketplaceClient,
        since_date: str | None = None,
        apply_date_filter: bool = False,
        full_sync: bool = False,
        stop_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[..., None] | None = None,
    ) -> int:
        """Sync chats from WB.

        full_sync=True  (manual button): fetch chat list + full events history
                        from the beginning (or resume_cursor if set).
                        Correctly assigns Answered/New buckets from the start.

        full_sync=False (60s auto-sync): fetch chat list (1 request) +
                        incremental events since last cursor (1-2 requests).
                        Determines last_sender per chat → correct New/Answered.
                        Fast: only new events since last sync are downloaded.
        """
        # Global kill-switch: does not affect reviews/questions.
        if not sync_chats_enabled():
            _log.info(
                "sync_chats: skipped (FEEDPILOT_SYNC_CHATS_ENABLED=0) source=%s account_id=%s",
                source,
                account_id,
            )
            return 0
        fetch_chats = getattr(client, "fetch_chats", None)
        if not callable(fetch_chats):
            return 0

        # ── Single-phase sync ────────────────────────────────────────────────
        # Fetch chats WITH full event history before saving anything.
        # This guarantees correct Answered/New bucket assignment from the
        # start — no "temporary wrong state" visible to the user.
        # The UI keeps the progress bar visible until this function returns.

        # Build a progress callback for event pages so the progress bar
        # shows "Загрузка событий: страница X из Y" during the events scan.
        _progress_cb = progress_callback if progress_callback else None

        def _events_page_cb(current_page: int, max_p: int) -> None:
            if _progress_cb:
                try:
                    _progress_cb(
                        step="Загрузка событий чатов",
                        channel=f"Чаты (стр. {current_page} из {max_p})",
                    )
                except Exception:
                    pass

        try:
            try:
                enriched_rows = fetch_chats(
                    since_date=since_date,
                    stop_requested=stop_requested,
                    enrich_with_events=full_sync,   # full history only on manual sync
                    page_progress_callback=_events_page_cb if full_sync else None,
                )
            except TypeError:
                enriched_rows = fetch_chats()
        except MarketplaceSyncError as exc:
            if not self._is_access_error(exc):
                self.repository.log_review_action(
                    user_id=user_id,
                    review_uid=None,
                    action_type="sync_error",
                    actor="system",
                    details={
                        "source": source,
                        "account_id": account_id,
                        "error": str(exc),
                        "scope": "chats",
                    },
                )
            raise

        # ── Incremental events fetch for auto-sync ────────────────────────────
        # For 60s auto-sync (full_sync=False) fetch only NEW events since the
        # last saved cursor.  This gives us the exact last_sender per chat
        # so New/Answered bucket assignment is correct.
        # Only 1-2 API calls needed because the cursor skips old events.
        incremental_sender_map: dict[str, dict[str, object]] = {}
        if not full_sync and hasattr(client, "_fetch_last_sender_map"):
            resume_cursor = getattr(client, "_resume_events_cursor", None)
            # Also try loading cursor from DB if not in memory
            if not resume_cursor and account_id is not None:
                try:
                    acct = self.repository.get_marketplace_account(
                        user_id=user_id,
                        account_id=int(account_id),
                        include_secrets=False,
                    )
                    if acct:
                        extra = acct.get("extra_json") or acct.get("extra") or {}
                        if isinstance(extra, str):
                            import json as _json
                            try:
                                extra = _json.loads(extra)
                            except Exception:
                                extra = {}
                        resume_cursor = str(extra.get("_wb_events_cursor") or "").strip() or None
                        if resume_cursor:
                            client._resume_events_cursor = resume_cursor  # type: ignore[attr-defined]
                except Exception:
                    pass
            _log.info(
                "sync_chats auto-sync: fetching incremental events with cursor=%s",
                resume_cursor,
            )
            try:
                incremental_sender_map = client._fetch_last_sender_map(  # type: ignore[attr-defined]
                    since_date=since_date,
                    resume_cursor=resume_cursor,
                    stop_requested=stop_requested,
                )
                # Update in-memory cursor and persist to DB
                cursor_entry = incremental_sender_map.pop("_final_cursor", None)
                if isinstance(cursor_entry, dict):
                    new_cursor = str(cursor_entry.get("cursor") or "").strip()
                    if new_cursor:
                        client._resume_events_cursor = new_cursor  # type: ignore[attr-defined]
                        if account_id is not None:
                            try:
                                self.repository.update_marketplace_account_extra_field(
                                    user_id=user_id,
                                    account_id=int(account_id),
                                    key="_wb_events_cursor",
                                    value=new_cursor,
                                )
                            except Exception:
                                pass
            except Exception as exc:
                _log.warning("sync_chats auto-sync: incremental events fetch failed: %s", exc)
                incremental_sender_map = {}

        # Compute date cutoff for filtering (only chats active on/after since_date)
        since_iso_filter: str | None = None
        if since_date:
            since_iso_filter = _normalize_timestamp(since_date)

        loaded = 0
        _log.debug(
            "sync_chats: full_sync=%s, total rows from WB=%d, since_iso_filter=%s",
            full_sync,
            len(enriched_rows),
            since_iso_filter,
        )
        for row in enriched_rows:
            _raise_if_stop_requested(stop_requested, source=source)
            ext_id = str(row.get("external_id") or "").strip()
            if not ext_id:
                continue
            last_msg_at = str(row.get("last_message_at") or "") or None

            # Apply date filter: skip chats with last activity before since_date.
            # This keeps stale chats out of the DB entirely.
            if since_iso_filter and last_msg_at and last_msg_at < since_iso_filter:
                _log.debug(
                    "sync_chats: SKIP chat %s – last_msg_at=%s < since_filter=%s",
                    ext_id, last_msg_at, since_iso_filter,
                )
                continue

            last_sender = str(row.get("last_sender") or "").strip().lower()
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            wb_events_row: list[dict[str, object]] = []
            ozon_history_row: list[dict[str, object]] = []
            if isinstance(meta, dict):
                wb_events_row = list(meta.pop("_wb_events", None) or [])
                ozon_history_row = list(meta.pop("_ozon_history", None) or [])

            # For auto-sync, override last_sender from incremental events map.
            # This is the reliable source: /seller/events says exactly who sent
            # the most recent message in each chat.
            if incremental_sender_map and ext_id in incremental_sender_map:
                ev_info = incremental_sender_map[ext_id]
                ev_sender = str(ev_info.get("sender") or "").strip().lower()
                if ev_sender:
                    last_sender = ev_sender
                    ev_ts = int(ev_info.get("ts") or 0)
                    if ev_ts:
                        last_msg_at = _normalize_timestamp(ev_ts) or last_msg_at

            # Determine bucket based on last_sender from events
            # seller replied last → Answered; buyer/unknown → New
            seller_replied_at: str | None = None
            if last_sender == "seller":
                seller_replied_at = last_msg_at

            # buyer_has_unread: force "New" when events confirm buyer wrote last
            # OR when WB's unread count says buyer has unseen messages.
            wb_unread = _to_positive_int(row.get("unread_count"), default=0)
            buyer_has_unread = (last_sender == "client") or (wb_unread > 0 and last_sender != "seller")

            _log.debug(
                "sync_chats: chat %s customer=%r last_msg_at=%s last_sender=%s "
                "unread=%d buyer_has_unread=%s seller_replied_at=%s",
                ext_id,
                str(row.get("customer_name") or "")[:30],
                last_msg_at,
                last_sender or "(empty)",
                wb_unread,
                buyer_has_unread,
                seller_replied_at,
            )

            conv_uid = self.repository.upsert_conversation(
                user_id=user_id,
                source=source,
                account_id=account_id,
                external_conversation_id=ext_id,
                kind="chat",
                customer_name=str(row.get("customer_name") or "") or None,
                message_text=str(row.get("message_text") or ""),
                status=str(row.get("status") or "open"),
                unread_count=wb_unread,
                metadata=meta,
                last_message_at=last_msg_at,
                seller_replied_at=seller_replied_at,
                buyer_has_unread=buyer_has_unread,
            )
            loaded += 1

            # For auto-sync: save incremental event texts to conversation_messages.
            # wb_events_row is only populated on full_sync (manual button), but
            # incremental_sender_map has the new events detected by the cursor.
            # Without saving them here, buyer messages like "УУУ" are seen by the
            # cursor (changing bucket) but their text is never stored — only the
            # last message summary in conversation_items.message_text is updated.
            if not wb_events_row and incremental_sender_map and ext_id in incremental_sender_map:
                inc_events = incremental_sender_map[ext_id].get("events") or []
                if inc_events:
                    inc_history: list[dict[str, object]] = []
                    for ev in inc_events:
                        if not isinstance(ev, dict):
                            continue
                        ev_id = str(ev.get("eventID") or "").strip()
                        ev_sender_i = str(ev.get("sender") or "").strip().lower()
                        msg_i = ev.get("message") or {}
                        ev_text_i = str(msg_i.get("text") or "").strip()
                        attachments_i = msg_i.get("attachments") or {}
                        images_i = attachments_i.get("images") or []
                        if not ev_text_i and images_i:
                            img_parts_i = [f"[img:{_wb_image_url(img)}]" for img in images_i if img.get("url") or img.get("downloadID")]
                            ev_text_i = " ".join(img_parts_i) if img_parts_i else f"[Фото: {len(images_i)} шт.]"
                        elif not ev_text_i and attachments_i.get("goodCard"):
                            ev_text_i = f"[Товар: {attachments_i['goodCard'].get('name', '')}]".strip()
                        ev_ts_raw_i = ev.get("addTimestamp")
                        ev_ts_ms_i = int(ev_ts_raw_i) if ev_ts_raw_i is not None else 0
                        # Fallback: use images[0]['date'] if addTimestamp is 0 (photo events)
                        if not ev_ts_ms_i and images_i and images_i[0].get("date"):
                            ev_iso_i = _normalize_timestamp(str(images_i[0]["date"])) or _utc_now()
                        else:
                            ev_iso_i = _normalize_timestamp(ev_ts_ms_i) or _utc_now()
                        client_name_i = str(ev.get("clientName") or "").strip()
                        if not ev_id or not ev_text_i:
                            continue
                        inc_history.append({
                            "direction": "inbound" if ev_sender_i == "client" else "outbound",
                            "message_text": ev_text_i,
                            "idempotency_key": f"wb-event-{ev_id}",
                            "created_at": ev_iso_i,
                            "operator_name": client_name_i if ev_sender_i == "client" else "Продавец",
                        })
                    if inc_history:
                        try:
                            self.repository.bulk_insert_chat_history_messages(
                                user_id=user_id,
                                conversation_uid=conv_uid,
                                messages=inc_history,
                            )
                            # Move chat to "New" if buyer's saved message is newer than our reply
                            self.repository.move_chat_to_new_if_buyer_replied(
                                user_id=user_id,
                                conversation_uid=conv_uid,
                            )
                        except Exception:
                            pass

            # Save message history from events
            if wb_events_row:
                history: list[dict[str, object]] = []
                for ev in wb_events_row:
                    if not isinstance(ev, dict):
                        continue
                    ev_id = str(ev.get("eventID") or "").strip()
                    ev_sender = str(ev.get("sender") or "").strip().lower()
                    msg = ev.get("message") or {}
                    ev_text = str(msg.get("text") or "").strip()
                    attachments = msg.get("attachments") or {}
                    images = attachments.get("images") or []
                    if not ev_text and images:
                        img_parts = [f"[img:{_wb_image_url(img)}]" for img in images if img.get("url") or img.get("downloadID")]
                        ev_text = " ".join(img_parts) if img_parts else f"[Фото: {len(images)} шт.]"
                    elif not ev_text and attachments.get("goodCard"):
                        card = attachments["goodCard"]
                        ev_text = f"[Товар: {card.get('name', '')}]".strip()
                    ev_ts_raw = ev.get("addTimestamp")
                    ev_ts_ms = int(ev_ts_raw) if ev_ts_raw is not None else 0
                    ev_iso = _normalize_timestamp(ev_ts_ms) or ""
                    client_name = str(ev.get("clientName") or "").strip()
                    if not ev_id or not ev_text:
                        continue
                    history.append({
                        "direction": "inbound" if ev_sender == "client" else "outbound",
                        "message_text": ev_text,
                        "idempotency_key": f"wb-event-{ev_id}",
                        "created_at": ev_iso,
                        "operator_name": client_name if ev_sender == "client" else "Продавец",
                    })
                if history:
                    try:
                        self.repository.bulk_insert_chat_history_messages(
                            user_id=user_id,
                            conversation_uid=conv_uid,
                            messages=history,
                        )
                    except Exception:
                        pass

            # Save Ozon message history (from /v3/chat/history enrichment)
            if ozon_history_row:
                try:
                    self.repository.bulk_insert_chat_history_messages(
                        user_id=user_id,
                        conversation_uid=conv_uid,
                        messages=ozon_history_row,
                    )
                except Exception:
                    pass

        # Also delete any stale chats that survived from previous syncs
        if apply_date_filter and since_date and account_id is not None:
            since_iso = _normalize_timestamp(since_date)
            if since_iso:
                try:
                    self.repository.delete_conversations_before_date(
                        user_id=user_id,
                        account_id=int(account_id),
                        kind="chat",
                        before_date=since_iso,
                    )
                except Exception:
                    pass

        # Persist cursor only on full_sync (for manual sync history storage)
        if full_sync and account_id is not None and hasattr(client, "_resume_events_cursor"):
            new_cursor = getattr(client, "_resume_events_cursor", None)
            if new_cursor:
                try:
                    self.repository.update_marketplace_account_extra_field(
                        user_id=user_id,
                        account_id=int(account_id),
                        key="_wb_events_cursor",
                        value=str(new_cursor),
                    )
                except Exception:
                    pass

        # After processing all chats: fix any chats that are in "Answered" bucket
        # but have a buyer message in conversation_messages that is newer than
        # last_sent_at. This handles the case where messages arrived in DB via
        # a refresh but the bucket was never updated (one-shot calls may have failed).
        # Runs once per sync cycle — fast batch SQL, no extra API calls.
        if user_id:
            try:
                moved = self.repository.batch_move_chats_to_new_if_buyer_replied(user_id=user_id)
                if moved:
                    _log.info("sync_chats: batch moved %d chat(s) to New bucket (inbound > last_sent_at)", moved)
                else:
                    _log.debug("sync_chats: batch check — no chats needed moving")
            except Exception as _e:
                _log.warning("sync_chats: batch bucket fix failed: %s", _e)

        _log.info(
            "sync_chats: done — source=%s loaded=%d full_sync=%s",
            source, loaded, full_sync,
        )
        return loaded

    def _is_access_error(self, error: object) -> bool:
        if isinstance(error, MarketplaceSyncError):
            message = str(error).lower()
        else:
            message = str(error or "").lower()
        if " 401" in message or " 403" in message:
            return True
        if "http error 401" in message or "http error 403" in message:
            return True
        if "forbidden" in message or "unauthorized" in message:
            return True
        if "access denied" in message or "permission" in message:
            return True
        if "недостаточно прав" in message or "нет доступа" in message:
            return True
        return False

    def _is_channel_supported(self, *, client: MarketplaceClient, channel: str) -> tuple[bool, str]:
        if channel == "reviews":
            supported = callable(getattr(client, "fetch_reviews", None))
            if not supported:
                return False, "Канал отзывов не поддерживается источником"
            return True, ""
        if channel == "questions":
            method = getattr(client, "fetch_questions", None)
            if not callable(method):
                return False, "Канал вопросов не поддерживается источником"
            if hasattr(client, "questions_path") and not bool(getattr(client, "questions_path")):
                _log.info("_is_channel_supported: questions skipped — questions_path empty for %s", type(client).__name__)
                return False, "Канал вопросов не настроен для этого источника"
            return True, ""
        if channel == "chats":
            method = getattr(client, "fetch_chats", None)
            if not callable(method):
                return False, "Канал чатов не поддерживается источником"
            if hasattr(client, "chats_path") and not bool(getattr(client, "chats_path")):
                return False, "Канал чатов не настроен для этого источника"
            return True, ""
        return False, f"Неизвестный канал: {channel}"

    def count_pending_for_account(
        self,
        *,
        account: dict[str, object],
        since_date: str | None = None,
    ) -> dict[str, object]:
        """Return a preview of how many items will be synced for this account.

        Calls lightweight count endpoints rather than doing a full sync.
        Returns a dict with account_id, account_name, marketplace and
        channel counts: reviews, questions, chats.
        """
        account_id = int(account.get("id") or 0)
        account_name = str(account.get("account_name") or "")
        marketplace = str(account.get("marketplace") or "")
        user_id = int(account.get("user_id") or 0)
        client = self._build_client(account)

        # Load last-known counts from extra_json as fallback for 429 scenarios
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        cached_counts: dict[str, int] = {
            "reviews": int(extra.get("_last_count_reviews") or 0),
            "questions": int(extra.get("_last_count_questions") or 0),
            "chats": int(extra.get("_last_count_chats") or 0),
        }

        counts: dict[str, int] = {"reviews": 0, "questions": 0, "chats": 0}
        if hasattr(client, "count_pending"):
            try:
                counts = client.count_pending(since_date=since_date)  # type: ignore[call-arg]
                # Persist successful counts so next preview can use them as fallback
                if account_id and user_id and any(counts.values()):
                    try:
                        for key, val in counts.items():
                            if val > 0:
                                self.repository.update_marketplace_account_extra_field(
                                    user_id=user_id,
                                    account_id=account_id,
                                    key=f"_last_count_{key}",
                                    value=val,
                                )
                    except Exception:
                        pass
            except Exception as _exc:
                _log.warning(
                    "count_pending_for_account: account_id=%d marketplace=%s error=%s — using cached counts",
                    account_id, marketplace, _exc,
                )
                counts = cached_counts  # use last-known values as fallback
        return {
            "account_id": account_id,
            "account_name": account_name,
            "marketplace": marketplace,
            "reviews": counts.get("reviews", 0),
            "questions": counts.get("questions", 0),
            "chats": counts.get("chats", 0),
            "total": sum(counts.values()),
        }

    def probe_account_channels(
        self,
        *,
        account: dict[str, object],
        since_date: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        account_id = int(account.get("id") or 0)
        marketplace = str(account.get("marketplace") or "")
        account_name = str(account.get("account_name") or "")
        client = self._build_client(account)
        if hasattr(client, "page_size"):
            try:
                setattr(client, "page_size", 1)
            except Exception:
                pass
        if hasattr(client, "max_pages"):
            try:
                setattr(client, "max_pages", 1)
            except Exception:
                pass

        channel_result: dict[str, dict[str, object]] = {}
        available_channels: list[str] = []
        unavailable_channels: list[str] = []
        for channel in ("reviews", "questions", "chats"):
            supported, reason = self._is_channel_supported(client=client, channel=channel)
            if not supported:
                channel_result[channel] = {
                    "available": False,
                    "access_denied": True,
                    "error": reason,
                    "reason": "not_configured",
                }
                unavailable_channels.append(channel)
                continue
            try:
                if channel == "reviews":
                    try:
                        getattr(client, "fetch_reviews")(since_date=since_date, stop_requested=stop_requested)
                    except TypeError:
                        getattr(client, "fetch_reviews")()
                elif channel == "questions":
                    try:
                        getattr(client, "fetch_questions")(stop_requested=stop_requested)
                    except TypeError:
                        getattr(client, "fetch_questions")()
                elif channel == "chats":
                    try:
                        getattr(client, "fetch_chats")(stop_requested=stop_requested)
                    except TypeError:
                        getattr(client, "fetch_chats")()
                channel_result[channel] = {"available": True, "access_denied": False, "error": ""}
                available_channels.append(channel)
            except MarketplaceSyncError as exc:
                if bool(exc.details.get("cancelled")):
                    raise
                access_denied = self._is_access_error(exc)
                channel_result[channel] = {
                    "available": False,
                    "access_denied": bool(access_denied),
                    "error": str(exc),
                    "reason": "access_denied" if access_denied else "temporary_error",
                }
                unavailable_channels.append(channel)
            except Exception as exc:  # pragma: no cover - defensive guard
                channel_result[channel] = {
                    "available": False,
                    "access_denied": False,
                    "error": str(exc),
                    "reason": "temporary_error",
                }
                unavailable_channels.append(channel)
        return {
            "account_id": account_id,
            "marketplace": marketplace,
            "account_name": account_name,
            "channels": channel_result,
            "available_channels": available_channels,
            "unavailable_channels": unavailable_channels,
        }

    def _run_channel_sync(
        self,
        *,
        channel: str,
        user_id: int,
        source: str,
        account_id: int,
        client: MarketplaceClient,
        since_date: str | None,
        stop_requested: Callable[[], bool] | None,
        apply_date_filter: bool = False,
        progress_callback: Callable[..., None] | None = None,
    ) -> dict[str, object]:
        supported, reason = self._is_channel_supported(client=client, channel=channel)
        if not supported:
            return {
                "ok": False,
                "loaded": 0,
                "channel": channel,
                "skipped": True,
                "access_denied": True,
                "error": reason,
            }
        if channel == "reviews":
            loaded = self.sync_reviews(
                user_id=user_id,
                source=source,
                account_id=account_id,
                client=client,
                since_date=since_date,
                stop_requested=stop_requested,
                apply_date_filter=apply_date_filter,
            )
        elif channel == "questions":
            loaded = self.sync_questions(
                user_id=user_id,
                source=source,
                account_id=account_id,
                client=client,
                since_date=since_date,
                stop_requested=stop_requested,
                apply_date_filter=apply_date_filter,
            )
        elif channel == "chats":
            loaded = self.sync_chats(
                user_id=user_id,
                source=source,
                account_id=account_id,
                client=client,
                since_date=since_date,
                apply_date_filter=apply_date_filter,
                full_sync=apply_date_filter,   # full_sync = manual trigger
                stop_requested=stop_requested,
                progress_callback=progress_callback,
            )
        else:
            raise ValueError(f"Unknown channel: {channel}")
        return {"ok": True, "loaded": int(loaded), "channel": channel}


    def sync_all_accounts(
        self,
        *,
        user_id: int,
        since_date: str | None = None,
        account_ids: list[int] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[..., None] | None = None,
        apply_date_filter: bool = False,
    ) -> dict[str, object]:
        loaded_total = 0
        loaded_questions = 0
        loaded_chats = 0
        loaded_conversations = 0
        successful_accounts = 0
        errors: list[dict[str, object]] = []
        capability_warnings: list[dict[str, object]] = []
        account_channel_stats: list[dict[str, object]] = []
        was_cancelled = False
        account_ids_filter: set[int] | None = None
        if account_ids is not None:
            normalized_ids: set[int] = set()
            for value in account_ids:
                try:
                    account_id = int(value)
                except (TypeError, ValueError):
                    continue
                if account_id > 0:
                    normalized_ids.add(account_id)
            account_ids_filter = normalized_ids
        accounts = [
            item
            for item in self.repository.list_marketplace_accounts(user_id, include_secrets=True)
            if item["is_active"]
        ]
        if account_ids_filter is not None:
            accounts = [
                item
                for item in accounts
                if int(item.get("id") or 0) in account_ids_filter
            ]
        selected_account_ids = [
            int(item.get("id") or 0)
            for item in accounts
            if int(item.get("id") or 0) > 0
        ]
        requested_account_ids = sorted(account_ids_filter) if account_ids_filter is not None else list(selected_account_ids)
        skipped_accounts = max(len(requested_account_ids) - len(selected_account_ids), 0)
        since_value = str(since_date or "").strip() or None
        total_accounts = len(accounts)
        if progress_callback:
            try:
                progress_callback(
                    step="Начало синхронизации",
                    total_accounts=total_accounts,
                    current_account=0,
                )
            except Exception:
                pass
        for account_idx, account in enumerate(accounts, start=1):
            if stop_requested and stop_requested():
                was_cancelled = True
                break
            account_id = int(account["id"])
            marketplace = str(account["marketplace"])
            current_account = self.repository.get_marketplace_account(
                user_id=user_id,
                account_id=account_id,
                include_secrets=True,
            )
            if current_account is None or not bool(current_account.get("is_active")):
                continue
            account_name = str(current_account.get("account_name") or f"#{account_id}")
            if progress_callback:
                try:
                    progress_callback(
                        step="Синхронизация кабинета",
                        account=f"{account_name} ({marketplace.upper()})",
                        channel="",
                        current_account=account_idx,
                        total_accounts=total_accounts,
                    )
                except Exception:
                    pass
            try:
                client = self._build_client(current_account)
                channel_names = {"reviews": "Отзывы", "questions": "Вопросы", "chats": "Чаты"}
                channel_outcomes: dict[str, dict[str, object]] = {}
                for channel in ("reviews", "questions", "chats"):
                    if channel == "chats" and not sync_chats_enabled():
                        channel_outcomes[channel] = {
                            "ok": True,
                            "loaded": 0,
                            "channel": channel,
                            "skipped": True,
                            "disabled": True,
                        }
                        continue
                    if stop_requested and stop_requested():
                        was_cancelled = True
                        break
                    if progress_callback:
                        try:
                            progress_callback(
                                step="Загрузка данных",
                                account=f"{account_name} ({marketplace.upper()})",
                                channel=channel_names.get(channel, channel),
                            )
                        except Exception:
                            pass
                    try:
                        ch_result = self._run_channel_sync(
                            channel=channel,
                            user_id=user_id,
                            source=marketplace,
                            account_id=account_id,
                            client=client,
                            since_date=since_value,
                            stop_requested=stop_requested,
                            apply_date_filter=apply_date_filter,
                            progress_callback=progress_callback,
                        )
                        channel_outcomes[channel] = ch_result
                        if progress_callback:
                            try:
                                progress_callback(
                                    loaded=int(ch_result.get("loaded") or 0),
                                )
                            except Exception:
                                pass
                    except MarketplaceSyncError as exc:
                        if bool(exc.details.get("cancelled")):
                            was_cancelled = True
                            break
                        is_access_error = self._is_access_error(exc)
                        _log.error(
                            "sync channel error: marketplace=%s channel=%s account_id=%s error=%s",
                            marketplace, channel, account_id, exc,
                        )
                        details = {
                            "account_id": account_id,
                            "marketplace": marketplace,
                            "channel": channel,
                            "scope": channel,
                            "error": str(exc),
                            "access_denied": bool(is_access_error),
                            **exc.details,
                        }
                        errors.append(details)
                        if is_access_error:
                            capability_warnings.append(
                                {
                                    "account_id": account_id,
                                    "marketplace": marketplace,
                                    "channel": channel,
                                    "message": str(exc),
                                }
                            )
                        channel_outcomes[channel] = {
                            "ok": False,
                            "loaded": 0,
                            "error": str(exc),
                            "access_denied": bool(is_access_error),
                        }
                    except Exception as exc:
                        _log.error(
                            "sync channel unexpected error: marketplace=%s channel=%s account_id=%s error=%s",
                            marketplace, channel, account_id, exc, exc_info=True,
                        )
                        details = {
                            "account_id": account_id,
                            "marketplace": marketplace,
                            "channel": channel,
                            "scope": channel,
                            "error": str(exc),
                        }
                        errors.append(details)
                        self.repository.log_review_action(
                            user_id=user_id,
                            review_uid=None,
                            action_type="sync_error",
                            actor="system",
                            details=details,
                        )
                        channel_outcomes[channel] = {
                            "ok": False,
                            "loaded": 0,
                            "error": str(exc),
                            "access_denied": False,
                        }
                if was_cancelled:
                    break

                account_loaded_reviews = int((channel_outcomes.get("reviews") or {}).get("loaded") or 0)
                account_loaded_questions = int((channel_outcomes.get("questions") or {}).get("loaded") or 0)
                account_loaded_chats = int((channel_outcomes.get("chats") or {}).get("loaded") or 0)
                loaded_total += account_loaded_reviews
                loaded_questions += account_loaded_questions
                loaded_chats += account_loaded_chats
                loaded_conversations = loaded_questions + loaded_chats

                account_success = any(
                    bool((channel_outcomes.get(name) or {}).get("ok")) for name in ("reviews", "questions", "chats")
                )
                if account_success:
                    successful_accounts += 1

                account_channel_stats.append(
                    {
                        "account_id": account_id,
                        "account_name": account_name,
                        "marketplace": marketplace,
                        "reviews": channel_outcomes.get("reviews") or {"ok": False, "loaded": 0},
                        "questions": channel_outcomes.get("questions") or {"ok": False, "loaded": 0},
                        "chats": channel_outcomes.get("chats") or {"ok": False, "loaded": 0},
                    }
                )
            except MarketplaceSyncError as exc:
                if bool(exc.details.get("cancelled")):
                    was_cancelled = True
                    break
                details = {"account_id": account_id, "marketplace": marketplace, "error": str(exc), **exc.details}
                errors.append(details)
            except Exception as exc:
                details = {"account_id": account_id, "marketplace": marketplace, "error": str(exc)}
                errors.append(details)
                self.repository.log_review_action(
                    user_id=user_id,
                    review_uid=None,
                    action_type="sync_error",
                    actor="system",
                    details=details,
                )
        return {
            "accounts": len(accounts),
            "success_accounts": successful_accounts,
            "failed_accounts": len(errors),
            "loaded": loaded_total,
            "loaded_reviews": loaded_total,
            "loaded_questions": loaded_questions,
            "loaded_chats": loaded_chats,
            "loaded_conversations": loaded_conversations,
            "account_ids": selected_account_ids,
            "skipped_accounts": skipped_accounts,
            "errors": errors,
            "capability_warnings": capability_warnings,
            "account_channel_stats": account_channel_stats,
            "cancelled": was_cancelled,
        }

    def repair_all_chat_statuses(self, *, user_id: int) -> int:
        """Convenience wrapper: fix answered status for all user chats."""
        try:
            return self.repository.repair_chat_answered_status(user_id=user_id)
        except Exception:
            return 0

    @staticmethod
    def _build_client(account: dict[str, object]) -> MarketplaceClient:
        marketplace = str(account.get("marketplace") or "")
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        if marketplace == "mock":
            return MockMarketplaceClient()
        if marketplace == "ozon":
            client_id = str(extra.get("client_id") or "")
            api_key = str(account.get("api_key") or "")
            return OzonMarketplaceClient(
                api_url=str(account.get("api_url") or ""),
                client_id=client_id,
                api_key=api_key,
                list_path=str(extra.get("list_path") or "/v1/review/list"),
                base_payload=extra.get("base_payload") if isinstance(extra.get("base_payload"), dict) else None,
                page_size=_to_positive_int(extra.get("page_size"), default=50),
                max_pages=_to_positive_int(extra.get("max_pages"), default=10000),
                questions_path=str(extra.get("questions_path") or "/v1/question/list"),
                chats_path=str(extra.get("chats_path") or "/v3/chat/list"),
                chats_history_path=str(extra.get("chats_history_path") or "/v3/chat/history"),
                reply_path=str(extra.get("reply_path") or "/v1/review/comment/create"),
                reply_review_id_field=str(extra.get("reply_review_id_field") or "review_id"),
                reply_text_field=str(extra.get("reply_text_field") or "text"),
                reply_payload=extra.get("reply_payload") if isinstance(extra.get("reply_payload"), dict) else None,
            )
        if marketplace == "yandex":
            return YandexMarketClient(
                api_url=str(account.get("api_url") or "https://api.partner.market.yandex.ru"),
                api_key=str(account.get("api_key") or ""),
                business_id=str(extra.get("business_id") or ""),
                page_size=_to_positive_int(extra.get("page_size"), default=50),
                max_pages=_to_positive_int(extra.get("max_pages"), default=2000),
            )
        if marketplace == "wb":
            api_key = str(account.get("api_key") or "")
            return WildberriesMarketplaceClient(
                api_url=str(account.get("api_url") or ""),
                api_key=api_key,
                list_path=str(extra.get("list_path")) if extra.get("list_path") else None,
                skip_param=str(extra.get("skip_param") or "skip"),
                take_param=str(extra.get("take_param") or "take"),
                unanswered_param=str(extra.get("unanswered_param") or "isAnswered"),
                unanswered_value=str(extra.get("unanswered_value") or "false"),
                page_size=_to_positive_int(extra.get("page_size"), default=100),
                max_pages=_to_positive_int(extra.get("max_pages"), default=5000),
                questions_path=str(extra.get("questions_path") or "/api/v1/questions"),
                chats_path=str(extra.get("chats_path") or "/api/v1/seller/chats"),
                chats_api_url=str(extra.get("chats_api_url") or "https://buyer-chat-api.wildberries.ru"),
                chats_events_path=str(extra.get("chats_events_path") or "/api/v1/seller/events"),
                _resume_events_cursor=str(extra["_wb_events_cursor"]) if extra.get("_wb_events_cursor") else None,
                reply_path=str(extra.get("reply_path") or "/api/v1/feedbacks/answer"),
                reply_method=str(extra.get("reply_method") or "POST"),
                reply_review_id_field=str(extra.get("reply_review_id_field") or "id"),
                reply_text_field=str(extra.get("reply_text_field") or "text"),
                reply_payload=extra.get("reply_payload") if isinstance(extra.get("reply_payload"), dict) else None,
            )
        return HTTPMarketplaceClient(
            api_url=str(account.get("api_url") or ""),
            api_key=str(account.get("api_key") or "") or None,
        )

    def list_reviews(
        self,
        *,
        user_id: int,
        source: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        statuses: list[str] | None = None,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "newest",
        account_ids: list[int] | None = None,
    ) -> list[dict[str, object]]:
        return self.repository.list_reviews(
            user_id=user_id,
            source=source,
            priority=priority,
            status=status,
            statuses=statuses,
            category=category,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            account_ids=account_ids,
        )

    def list_reviews_paginated(
        self,
        *,
        user_id: int,
        source: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        statuses: list[str] | None = None,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 30,
        bucket: str = "all",
        account_ids: list[int] | None = None,
        product_search: str | None = None,
        has_contradiction: bool = False,
    ) -> dict[str, object]:
        return self.repository.list_reviews_paginated(
            user_id=user_id,
            source=source,
            priority=priority,
            status=status,
            statuses=statuses,
            category=category,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            page=page,
            page_size=page_size,
            bucket=bucket,
            account_ids=account_ids,
            product_search=product_search,
            has_contradiction=has_contradiction,
        )

    def list_review_sources(self, *, user_id: int) -> list[str]:
        return self.repository.list_review_sources(user_id=user_id)

    def apply_processing_rules_to_unprocessed(self, *, user_id: int) -> dict[str, int]:
        rows = self.repository.list_unprocessed_reviews(user_id=user_id)
        updated = 0
        auto_sent = 0
        queued = 0
        ignored = 0
        for row in rows:
            review_uid = str(row.get("review_uid") or "")
            if not review_uid:
                continue
            category = str(row.get("category") or "")
            sentiment = str(row.get("sentiment_label") or "")
            review_metadata = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
            # Skip reviews flagged as contradictions — they must stay in manual review.
            if review_metadata.get("rating_contradiction"):
                ignored += 1
                continue
            review = ReviewInput(
                review_id=str(row.get("external_review_id") or ""),
                text=str(row.get("text") or ""),
                author=str(row.get("author")) if row.get("author") else None,
                rating=int(row["rating"]) if row.get("rating") is not None else None,
                metadata=review_metadata,
            )
            group_id = self._resolve_template_group_id(
                category=category,
                review=review,
                sentiment=sentiment,
            )
            rule = self.repository.get_processing_rule(user_id=user_id, group_id=group_id) if group_id else None
            mode, auto_send, _template_text = self._resolve_processing_mode(review, None, rule)
            if mode == "template":
                group_template = self._pick_group_template_text(
                    user_id=user_id,
                    category=category,
                    review=review,
                    sentiment=sentiment,
                )
                template = self.repository.get_template(user_id=user_id, category=category)
                template_text = ""
                if template and bool(template.get("is_enabled")):
                    template_text = str(template.get("template_text") or "").strip()
                selected_template = str(group_template or template_text).strip()
                if not selected_template:
                    self.repository.log_review_action(
                        user_id=user_id,
                        review_uid=review_uid,
                        action_type="send_reply_error",
                        actor="system",
                        details={
                            "source": str(row.get("source") or ""),
                            "account_id": int(row["account_id"]) if row.get("account_id") is not None else None,
                            "error": "Не найден шаблон для автоматического ответа",
                        },
                    )
                    if self.repository.update_review_processing_result(
                        user_id=user_id,
                        review_uid=review_uid,
                        status="queued_for_operator",
                        auto_reply=None,
                    ):
                        queued += 1
                        updated += 1
                    continue
                reply = self._render_template(
                    selected_template,
                    user_id=user_id,
                    review=review,
                    category=category,
                    sentiment=sentiment,
                )
                sent = self._send_reply_for_saved_review(
                    user_id=user_id,
                    source=str(row.get("source") or ""),
                    account_id=int(row["account_id"]) if row.get("account_id") is not None else None,
                    review=review,
                    response_text=reply,
                    review_uid=review_uid,
                )
                if not sent:
                    if self.repository.update_review_processing_result(
                        user_id=user_id,
                        review_uid=review_uid,
                        status="queued_for_operator",
                        auto_reply=None,
                    ):
                        queued += 1
                        updated += 1
                    continue
                if self.repository.update_review_processing_result(
                    user_id=user_id,
                    review_uid=review_uid,
                    status="answered_auto",
                    auto_reply=reply,
                ):
                    auto_sent += 1
                    updated += 1
                continue

            if self.repository.update_review_processing_result(
                user_id=user_id,
                review_uid=review_uid,
                status="queued_for_operator",
                auto_reply=None,
            ):
                queued += 1
                updated += 1

        return {
            "updated": updated,
            "auto_sent": auto_sent,
            "queued": queued,
            "ignored": ignored,
        }

    def _send_reply_via_client(
        self,
        *,
        client: object,
        source: str,
        review: ReviewInput,
        response_text: str,
    ) -> tuple[bool, str | None]:
        sender = getattr(client, "send_review_reply", None)
        if not callable(sender):
            # Backward compatibility for test/dummy clients without reply API.
            return True, None
        try:
            try:
                sent = sender(review=review, response_text=response_text)
            except TypeError:
                sent = sender(review, response_text)
        except MarketplaceSyncError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, str(exc)
        if sent is False:
            return False, f"{source}: маркетплейс не подтвердил отправку ответа"
        return True, None

    def _send_reply_for_saved_review(
        self,
        *,
        user_id: int,
        source: str,
        account_id: int | None,
        review: ReviewInput,
        response_text: str,
        review_uid: str,
    ) -> bool:
        if source not in {"wb", "ozon"}:
            return True
        if account_id is None:
            return False
        account = self.repository.get_marketplace_account(
            user_id=user_id,
            account_id=account_id,
            include_secrets=True,
        )
        if account is None:
            return False
        client = self._build_client(account)
        sent, error = self._send_reply_via_client(
            client=client,
            source=source,
            review=review,
            response_text=response_text,
        )
        if sent:
            return True
        self.repository.log_review_action(
            user_id=user_id,
            review_uid=review_uid,
            action_type="send_reply_error",
            actor="system",
            details={"source": source, "account_id": account_id, "error": error or "Не удалось отправить ответ"},
        )
        return False

    def _send_conversation_reply_via_client(
        self,
        *,
        client: object,
        source: str,
        conversation: dict[str, object],
        response_text: str,
    ) -> tuple[bool, str | None]:
        sender = getattr(client, "send_conversation_reply", None)
        if not callable(sender):
            # Backward compatibility: clients without conversation reply API
            # should not break manual workflow.
            return True, None
        try:
            try:
                sent = sender(conversation=conversation, response_text=response_text)
            except TypeError:
                sent = sender(conversation, response_text)
        except MarketplaceSyncError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, str(exc)
        if sent is False:
            return False, f"{source}: маркетплейс не подтвердил отправку ответа в диалог"
        return True, None

    def send_conversation_reply(
        self,
        *,
        user_id: int,
        conversation_uid: str,
        response_text: str,
        operator_name: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        conversation = self.repository.get_conversation(user_id=user_id, conversation_uid=conversation_uid)
        if conversation is None:
            raise KeyError("Диалог не найден")
        source = str(conversation.get("source") or "").strip().lower()
        account_id = conversation.get("account_id")
        clean_text = str(response_text or "").strip()
        if not clean_text:
            raise ValueError("Текст ответа не может быть пустым")
        clean_idempotency = str(idempotency_key or "").strip()
        if not clean_idempotency:
            raise ValueError("idempotency_key обязателен")

        existing = self.repository.get_conversation_message_by_idempotency(
            user_id=user_id,
            conversation_uid=conversation_uid,
            idempotency_key=clean_idempotency,
        )
        if existing is not None and str(existing.get("send_status") or "").strip().lower() == "sent":
            return {"ok": True, "status": "sent", "deduplicated": True}

        self.repository.upsert_conversation_outbound_message(
            user_id=user_id,
            conversation_uid=conversation_uid,
            message_text=clean_text,
            operator_name=operator_name,
            idempotency_key=clean_idempotency,
        )

        account_id_value: int | None = None
        try:
            if account_id is not None:
                account_id_value = int(account_id)
        except (TypeError, ValueError):
            account_id_value = None

        # Only mock/unknown sources skip marketplace API.
        # Yandex questions must go through YandexMarketClient.send_conversation_reply.
        if source not in {"wb", "ozon", "yandex"} or account_id_value is None:
            self.repository.mark_conversation_message_send_success(
                user_id=user_id,
                conversation_uid=conversation_uid,
                idempotency_key=clean_idempotency,
            )
            self.repository.log_review_action(
                user_id=user_id,
                review_uid=conversation_uid,
                action_type="conversation_send_success",
                actor=operator_name,
                details={"source": source, "idempotency_key": clean_idempotency, "scope": "conversations"},
            )
            return {"ok": True, "status": "sent", "deduplicated": False}

        account = self.repository.get_marketplace_account(
            user_id=user_id,
            account_id=account_id_value,
            include_secrets=True,
        )
        if account is None:
            error_message = "Кабинет маркетплейса для диалога не найден"
            self.repository.mark_conversation_message_send_failure(
                user_id=user_id,
                conversation_uid=conversation_uid,
                idempotency_key=clean_idempotency,
                error_code="account_not_found",
                error_message=error_message,
            )
            self.repository.log_review_action(
                user_id=user_id,
                review_uid=conversation_uid,
                action_type="conversation_send_error",
                actor=operator_name,
                details={
                    "source": source,
                    "error_code": "account_not_found",
                    "error": error_message,
                    "idempotency_key": clean_idempotency,
                    "scope": "conversations",
                },
            )
            return {"ok": False, "status": "failed", "error": error_message}

        client = self._build_client(account)
        sent, send_error = self._send_conversation_reply_via_client(
            client=client,
            source=source,
            conversation=conversation,
            response_text=clean_text,
        )
        if sent:
            self.repository.mark_conversation_message_send_success(
                user_id=user_id,
                conversation_uid=conversation_uid,
                idempotency_key=clean_idempotency,
            )
            # Move chat to 'answered' bucket
            try:
                self.repository.mark_conversation_answered(
                    user_id=user_id,
                    conversation_uid=conversation_uid,
                )
            except Exception:
                pass
            # For WB chat replies: link our DB record to the WB eventID so
            # subsequent event downloads don't create a duplicate message.
            if source == "wb" and hasattr(client, "_last_sent_add_time"):
                add_time = getattr(client, "_last_sent_add_time", None)
                ext_id = str(conversation.get("external_conversation_id") or "").strip()
                if add_time and ext_id:
                    try:
                        wb_event_id = client._find_wb_event_id_for_sent(  # type: ignore[attr-defined]
                            chat_id=ext_id,
                            add_time_ms=int(add_time),
                        )
                        if wb_event_id:
                            wb_idem_key = f"wb-event-{wb_event_id}"
                            self.repository.update_conversation_message_idempotency_key(
                                user_id=user_id,
                                conversation_uid=conversation_uid,
                                old_key=clean_idempotency,
                                new_key=wb_idem_key,
                            )
                    except Exception:
                        pass  # Non-critical: worst case a duplicate appears
            self.repository.log_review_action(
                user_id=user_id,
                review_uid=conversation_uid,
                action_type="conversation_send_success",
                actor=operator_name,
                details={"source": source, "idempotency_key": clean_idempotency, "scope": "conversations"},
            )
            return {"ok": True, "status": "sent", "deduplicated": False}

        error_text = send_error or "Не удалось отправить ответ в диалог"
        self.repository.mark_conversation_message_send_failure(
            user_id=user_id,
            conversation_uid=conversation_uid,
            idempotency_key=clean_idempotency,
            error_code="send_failed",
            error_message=error_text,
        )
        self.repository.log_review_action(
            user_id=user_id,
            review_uid=conversation_uid,
            action_type="conversation_send_error",
            actor=operator_name,
            details={
                "source": source,
                "error_code": "send_failed",
                "error": error_text,
                "idempotency_key": clean_idempotency,
                "scope": "conversations",
            },
        )
        return {"ok": False, "status": "failed", "error": error_text}

    def queue_for_manual_processing(self, *, user_id: int, review_uid: str) -> bool:
        updated = self.repository.mark_manual_queue(user_id=user_id, review_uid=review_uid)
        if updated:
            self.repository.log_review_action(
                user_id=user_id,
                review_uid=review_uid,
                action_type="queue_manual",
                actor="operator",
                details={},
            )
        return updated

    def queue_for_manual_processing_with_actor(
        self,
        *,
        actor_email: str,
        owner_user_id: int,
        review_uid: str,
    ) -> bool:
        updated = self.repository.mark_manual_queue(user_id=owner_user_id, review_uid=review_uid)
        if updated:
            self.repository.log_review_action(
                user_id=owner_user_id,
                review_uid=review_uid,
                action_type="queue_manual",
                actor=actor_email,
                details={},
            )
        return updated

    def generate_auto_reply(self, *, user_id: int, review_uid: str) -> str:
        review = self.repository.get_review(user_id=user_id, review_uid=review_uid)
        if review is None:
            raise KeyError("Отзыв не найден")
        template = self.repository.get_template(user_id=user_id, category=str(review.get("category")))
        group_template = self._pick_group_template_text(
            user_id=user_id,
            category=str(review.get("category") or ""),
            review=ReviewInput(
                review_id=str(review.get("external_review_id")),
                text=str(review.get("text")),
                author=str(review.get("author")) if review.get("author") else None,
                rating=int(review["rating"]) if review.get("rating") is not None else None,
                metadata=dict(review.get("metadata") or {}) if isinstance(review.get("metadata"), dict) else {},
            ),
            sentiment=str(review.get("sentiment_label") or ""),
        )
        text = group_template or (str(template["template_text"]) if template else self._build_auto_reply(review))
        reply = self._render_template(
            text,
            review=ReviewInput(
                review_id=str(review.get("external_review_id")),
                text=str(review.get("text")),
                author=str(review.get("author")) if review.get("author") else None,
                rating=int(review["rating"]) if review.get("rating") is not None else None,
                metadata=dict(review.get("metadata") or {}) if isinstance(review.get("metadata"), dict) else {},
            ),
            user_id=user_id,
            category=str(review.get("category")),
            sentiment=str(review.get("sentiment_label")),
        )
        sent = self._send_reply_for_saved_review(
            user_id=user_id,
            source=str(review.get("source") or ""),
            account_id=int(review["account_id"]) if review.get("account_id") is not None else None,
            review=ReviewInput(
                review_id=str(review.get("external_review_id")),
                text=str(review.get("text")),
                author=str(review.get("author")) if review.get("author") else None,
                rating=int(review["rating"]) if review.get("rating") is not None else None,
                metadata=dict(review.get("metadata") or {}) if isinstance(review.get("metadata"), dict) else {},
            ),
            response_text=reply,
            review_uid=review_uid,
        )
        if not sent:
            raise MarketplaceSyncError("marketplace", "Не удалось отправить ответ на маркетплейс по API")
        updated = self.repository.mark_auto_replied(user_id=user_id, review_uid=review_uid, response_text=reply)
        if not updated:
            raise KeyError("Отзыв не найден")
        self.repository.log_review_action(
            user_id=user_id,
            review_uid=review_uid,
            action_type="auto_reply",
            actor="system",
            details={"reply": reply},
        )
        return reply

    def save_manual_reply(self, *, user_id: int, review_uid: str, operator_name: str, response_text: str) -> bool:
        updated = self.repository.mark_manual_replied(
            user_id=user_id,
            review_uid=review_uid,
            operator_name=operator_name,
            response_text=response_text,
        )
        if updated:
            self.repository.log_review_action(
                user_id=user_id,
                review_uid=review_uid,
                action_type="manual_reply",
                actor=operator_name,
                details={"reply": response_text},
            )
        return updated

    def save_manual_reply_with_actor(
        self,
        *,
        actor_email: str,
        owner_user_id: int,
        review_uid: str,
        response_text: str,
    ) -> bool:
        updated = self.repository.mark_manual_replied(
            user_id=owner_user_id,
            review_uid=review_uid,
            operator_name=actor_email,
            response_text=response_text,
        )
        if updated:
            self.repository.log_review_action(
                user_id=owner_user_id,
                review_uid=review_uid,
                action_type="manual_reply",
                actor=actor_email,
                details={"reply": response_text},
            )
        return updated

    def _pick_group_template_text(
        self,
        *,
        user_id: int,
        category: str,
        review: ReviewInput,
        sentiment: str,
        preferred_subgroup: str | None = None,
    ) -> str | None:
        group_id = self._resolve_template_group_id(category=category, review=review, sentiment=sentiment)
        if not group_id:
            return None
        subgroup = str(preferred_subgroup or "").strip() or self._resolve_template_subgroup(
            group_id=group_id,
            category=category,
            review=review,
        )
        row = self.repository.get_random_template_variant(
            user_id=user_id,
            group_id=group_id,
            subgroup=subgroup,
        )
        if row is None and subgroup:
            row = self.repository.get_random_template_variant(user_id=user_id, group_id=group_id)
        if row is None:
            return None
        text = str(row.get("template_text") or "").strip()
        return text or None

    @classmethod
    def _resolve_template_subgroup(cls, *, group_id: str, category: str, review: ReviewInput) -> str | None:
        metadata = review.metadata if isinstance(review.metadata, dict) else {}
        classified_subgroup = str(metadata.get("classified_subgroup") or "").strip()
        if classified_subgroup:
            return classified_subgroup
        text = (review.text or "").lower()
        if group_id == "positive":
            if any(word in text for word in ("доставк", "курьер", "пвз", "пункт выдачи")):
                return "Позитив доставка"
            if any(word in text for word in ("запах", "аромат")):
                return "Позитив запах"
            if any(word in text for word in ("конструкц", "форма", "сборк")):
                return "Позитив конструкция"
            if any(word in text for word in ("упаковк", "коробк")):
                return "Позитив упаковка"
            if any(word in text for word in ("цвет", "оттенок")):
                return "Позитив цвет"
            if any(word in text for word in ("материал", "ткан", "состав")):
                return "Материал"
            if any(word in text for word in ("вкус", "вкусн")):
                return "Вкус"
            if any(word in text for word in ("эффект", "результат")):
                return "Эффект"
            return "Общий позитив"
        if group_id == "delivery_problems":
            if any(word in text for word in ("долго", "задерж", "опозд")):
                return "Долгая доставка"
            if any(word in text for word in ("испорчен", "мята", "порван", "поврежден", "сломан", "грязн")):
                return "Недостающая упаковка / грязное / поврежденное и сломанное"
            if any(word in text for word in ("наклейк", "этикетк")):
                return "Наклейка"
            if any(word in text for word in ("не тот", "перепут", "другой товар")):
                return "Не тот товар"
            if any(word in text for word in ("некомплект", "не хватает", "нет в комплекте")):
                return "Некомплект"
            if any(word in text for word in ("упаковк", "коробк")):
                return "Испорченная упаковка"
            return "Общие доставка"
        if group_id == "wrong_size":
            if any(word in text for word in ("большемер", "маломер", "мерит")):
                return "Большемерит/маломерит"
            if any(word in text for word in ("замер", "измер")):
                return "Альтернативные измерения"
            return "Не подошел размер"
        if group_id == "textless_ratings":
            return ReviewAutomationService._textless_subgroup_for_rating(
                review.rating if review.rating is not None else 0
            )
        if group_id == "tagged_reviews":
            return "Общие теги"
        if group_id == "product_dissatisfaction":
            if any(word in text for word in ("подделк", "фейк")):
                return "Подделка"
            if any(word in text for word in ("срок", "годност")):
                return "Срок годности"
            if any(word in text for word in ("запах", "аромат")):
                return "Негатив запах"
            if any(word in text for word in ("конструкц", "сборк", "сломал")):
                return "Негатив конструкция"
            if any(word in text for word in ("цвет", "оттенок")):
                return "Негатив цвет"
            if any(word in text for word in ("брак", "б/у", "бу ")):
                return "Брак и Б/У"
            if any(word in text for word in ("цена", "дорог")):
                return "Высокая цена"
            if any(word in text for word in ("текстур", "консистенц", "материал")):
                return "Текстура, консистенция, материал"
            if any(word in text for word in ("качество", "некачествен")):
                return "Качество"
            if any(word in text for word in ("эффект", "результат")):
                return "Не устраивает эффект"
            if any(word in text for word in ("не подош", "не мое", "лично мне")):
                return "Не подошел лично мне"
            return "Общий негатив"
        return None

    @staticmethod
    def _resolve_template_group_id(*, category: str, review: ReviewInput, sentiment: str) -> str | None:
        normalized = category.strip().lower()
        if normalized in {
            "positive",
            "product_dissatisfaction",
            "delivery_problems",
            "wrong_size",
            "textless_ratings",
        }:
            return normalized
        text = (review.text or "").strip().lower()
        tags = ReviewAutomationService._extract_review_tags(review)
        has_text = bool(text)
        has_tags = bool(tags)

        if not has_text:
            # tagged_reviews group removed — tags (pros/cons) are now combined
            # into review.text before classification, so no review should arrive
            # here with tags but no text. Fall through to textless_ratings.
            return "textless_ratings"

        size_words = ("размер", "маломер", "большемер", "size", "мерит")
        delivery_words = ("доставк", "курьер", "пункт выдачи", "пвз")
        product_words = ("товар", "качество", "брак", "слом", "упаковк", "цвет", "белье", "пододеяльник")

        if normalized in {"positive_quality", "positive_product"}:
            if any(word in text for word in size_words):
                return "wrong_size"
            return "positive"
        if normalized == "negative_delivery":
            return "delivery_problems"
        if normalized in {"neutral_other"}:
            if any(word in text for word in size_words):
                return "wrong_size"
            if any(word in text for word in delivery_words):
                return "delivery_problems"
            if any(word in text for word in product_words):
                return "product_dissatisfaction"
            if sentiment.strip().lower() == "positive":
                return "positive"
            return "product_dissatisfaction"
        if normalized in {"negative_product", "negative_other"}:
            if any(word in text for word in size_words):
                return "wrong_size"
            if any(word in text for word in delivery_words):
                return "delivery_problems"
            return "product_dissatisfaction"
        if sentiment.strip().lower() == "positive":
            if any(word in text for word in size_words):
                return "wrong_size"
            return "positive"
        return None

    @staticmethod
    def _extract_review_tags(review: ReviewInput) -> list[str]:
        metadata = review.metadata if isinstance(review.metadata, dict) else {}
        raw_tags = metadata.get("review_tags")
        result: list[str] = []
        seen: set[str] = set()

        def _push(value: object) -> None:
            text = str(value or "").strip()
            if not text:
                return
            normalized = text.lower()
            if normalized in seen:
                return
            seen.add(normalized)
            result.append(text)

        if isinstance(raw_tags, list):
            for item in raw_tags:
                _push(item)
        elif isinstance(raw_tags, str):
            for part in re.split(r"[,\n;/|]+", raw_tags):
                _push(part)

        # Fallback for unknown provider payload shape.
        if not result:
            raw = metadata.get("raw")
            extracted = _extract_review_tags_from_payload(raw)
            for item in extracted:
                _push(item)
        return result

    @staticmethod
    @staticmethod
    def _textless_subgroup_for_rating(rating: int) -> str:
        """Return the textless-ratings subgroup name for a given star rating."""
        clipped = max(1, min(int(rating or 1), 5))
        return ReviewAutomationService.TEXTLESS_SUBGROUPS[clipped - 1]

    @staticmethod
    def _review_has_media(review: ReviewInput) -> bool:
        metadata = review.metadata if isinstance(review.metadata, dict) else {}

        def _has_media(value: object) -> bool:
            if isinstance(value, str):
                text = value.strip().lower()
                if not text:
                    return False
                markers = ("http://", "https://", ".jpg", ".jpeg", ".png", ".webp", ".gif", "photo", "image", "картин")
                return any(marker in text for marker in markers)
            if isinstance(value, list):
                return any(_has_media(item) for item in value)
            if isinstance(value, Mapping):
                for key, nested in value.items():
                    key_text = str(key).lower()
                    if any(marker in key_text for marker in ("photo", "image", "media", "pictures", "gallery", "фото")):
                        if _has_media(nested):
                            return True
                    if _has_media(nested):
                        return True
                return False
            return False

        return _has_media(metadata)

    def _classify_category_and_subgroup(
        self,
        review: ReviewInput,
        processed: object,
        *,
        settings: dict[str, object],
        user_id: int | None = None,
    ) -> tuple[str, str | None]:
        has_text = bool((review.text or "").strip())
        has_media = self._review_has_media(review)

        # Отзывы без текста и без вложений не отправляем в Яндекс:
        # категория и подгруппа определяются только по оценке.
        if not has_text and not has_media:
            rating = review.rating if review.rating is not None else 0
            subgroup = self._textless_subgroup_for_rating(rating)
            return self.TEXTLESS_GROUP_ID, subgroup

        # Для отзывов с текстом или вложениями обязательно используем Яндекс.
        if user_id is None:
            classified = self._classify_with_yandex(
                review,
                settings=settings,
                strict=True,
                allowed_groups=list(self.REVIEW_GROUP_TITLES.keys()),
            )
            if classified:
                return classified, None
            raise MarketplaceSyncError(
                "yandex",
                "Не удалось определить категорию отзыва через Яндекс. Проверьте настройки и доступность API.",
                details={"scope": "classification", "has_media": has_media, "queue_manual": True},
            )
        return self._classify_with_yandex_target(review=review, settings=settings, user_id=user_id, strict=False)

    def _classify_category(
        self,
        review: ReviewInput,
        processed: object,
        *,
        settings: dict[str, object],
    ) -> str:
        category, _subgroup = self._classify_category_and_subgroup(
            review,
            processed,
            settings=settings,
            user_id=None,
        )
        return category

    @classmethod
    def _is_general_subgroup_for_group(cls, *, group_id: str, subgroup: str) -> bool:
        clean_group = str(group_id or "").strip()
        clean_subgroup = cls._normalize_subgroup_name(subgroup)
        return (
            clean_group in cls.REVIEW_GROUPS_WITH_GENERAL_SUBGROUP
            and clean_subgroup == cls._normalize_subgroup_name(cls.GENERAL_SUBGROUP_TITLE)
        )

    @classmethod
    def _ensure_general_subgroup_first(
        cls,
        *,
        group_id: str,
        subgroup_items: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        clean_group = str(group_id or "").strip()
        if clean_group not in cls.REVIEW_GROUPS_WITH_GENERAL_SUBGROUP:
            return list(subgroup_items)
        result: list[dict[str, str]] = []
        general_row: dict[str, str] | None = None
        for item in subgroup_items:
            subgroup_title = str(item.get("subgroup") or "").strip()
            subgroup_id = str(item.get("subgroup_id") or "").strip()
            if not subgroup_title:
                continue
            resolved_id = subgroup_id or cls._build_subgroup_id(clean_group, subgroup_title)
            normalized_row = {"subgroup_id": resolved_id, "subgroup": subgroup_title}
            if cls._is_general_subgroup_for_group(group_id=clean_group, subgroup=subgroup_title):
                if general_row is None:
                    general_row = normalized_row
                continue
            result.append(normalized_row)
        if general_row is None:
            general_row = {
                "subgroup_id": cls._build_subgroup_id(clean_group, cls.GENERAL_SUBGROUP_TITLE),
                "subgroup": cls.GENERAL_SUBGROUP_TITLE,
            }
        return [general_row, *result]

    @staticmethod
    def _normalize_subgroup_name(value: str) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @classmethod
    def _build_subgroup_id(cls, group_id: str, subgroup: str) -> str:
        clean_group = str(group_id or "").strip().lower().replace(" ", "_").replace("-", "_")
        normalized_subgroup = cls._normalize_subgroup_name(subgroup)
        digest = hashlib.sha1(f"{clean_group}|{normalized_subgroup}".encode("utf-8")).hexdigest()[:12]
        return f"{clean_group}__{digest}"

    @classmethod
    def _list_group_subgroups_for_review_classification(
        cls,
        *,
        repository: ReviewRepository,
        user_id: int,
    ) -> list[dict[str, object]]:
        allowed_group_ids = set(cls.REVIEW_GROUP_TITLES.keys())
        subgroup_items_by_group: dict[str, list[dict[str, str]]] = {group_id: [] for group_id in allowed_group_ids}
        seen_by_group: dict[str, set[str]] = {group_id: set() for group_id in allowed_group_ids}

        def _push(group_id: str, subgroup: str) -> None:
            clean_group = str(group_id or "").strip()
            clean_subgroup = str(subgroup or "").strip()
            if clean_group not in allowed_group_ids or not clean_subgroup:
                return
            normalized = cls._normalize_subgroup_name(clean_subgroup)
            if normalized in seen_by_group[clean_group]:
                return
            seen_by_group[clean_group].add(normalized)
            subgroup_items_by_group[clean_group].append(
                {
                    "subgroup_id": cls._build_subgroup_id(clean_group, clean_subgroup),
                    "subgroup": clean_subgroup,
                }
            )

        default_registry_rows = repository.list_default_template_subgroups()
        has_stored_subgroup_ids = any(str(row.get("subgroup_id") or "").strip() for row in default_registry_rows)
        if default_registry_rows and has_stored_subgroup_ids:
            for row in default_registry_rows:
                clean_group = str(row.get("group_id") or "").strip()
                clean_subgroup = str(row.get("subgroup") or "").strip()
                if clean_group not in allowed_group_ids or not clean_subgroup:
                    continue
                normalized = cls._normalize_subgroup_name(clean_subgroup)
                if normalized in seen_by_group[clean_group]:
                    continue
                seen_by_group[clean_group].add(normalized)
                stored_subgroup_id = str(row.get("subgroup_id") or "").strip()
                subgroup_items_by_group[clean_group].append(
                    {
                        "subgroup_id": stored_subgroup_id or cls._build_subgroup_id(clean_group, clean_subgroup),
                        "subgroup": clean_subgroup,
                    }
                )
        else:
            # Fallback for fresh installations before subgroup registry is initialized.
            for group_id, defaults in cls.REVIEW_GROUP_DEFAULT_SUBGROUPS.items():
                for subgroup in defaults:
                    _push(group_id, subgroup)

        # Fixed structure for textless ratings — one subgroup per star (1-5).
        # These subgroups cannot be deleted by admins or users.
        subgroup_items_by_group[cls.TEXTLESS_GROUP_ID] = [
            {
                "subgroup_id": cls._build_subgroup_id(cls.TEXTLESS_GROUP_ID, sg),
                "subgroup": sg,
                "protected": True,  # marker: cannot be deleted
            }
            for sg in cls.TEXTLESS_SUBGROUPS
        ]

        items: list[dict[str, object]] = []
        ordered_group_ids = [
            "positive",
            "product_dissatisfaction",
            "delivery_problems",
            "wrong_size",
            cls.TEXTLESS_GROUP_ID,
        ]
        for group_id in ordered_group_ids:
            subgroup_items = list(subgroup_items_by_group.get(group_id) or [])
            if group_id != cls.TEXTLESS_GROUP_ID and not subgroup_items:
                continue
            subgroup_items = cls._ensure_general_subgroup_first(group_id=group_id, subgroup_items=subgroup_items)
            items.append(
                {
                    "group_id": group_id,
                    "group_title": cls.REVIEW_GROUP_TITLES.get(group_id, group_id),
                    "subgroup_items": subgroup_items,
                    "subgroups": [str(item.get("subgroup") or "") for item in subgroup_items if str(item.get("subgroup") or "")],
                }
            )
        return items

    @classmethod
    def _parse_yandex_target_response(
        cls,
        raw_text: str,
        *,
        options: list[dict[str, object]],
    ) -> dict[str, str] | None:
        response = str(raw_text or "").strip()
        if not response:
            return None
        normalized_response = response.lower()
        group_aliases: dict[str, str] = {}
        subgroup_ids_by_group: dict[str, dict[str, str]] = {}
        subgroup_titles_by_group: dict[str, dict[str, str]] = {}
        for item in options:
            group_id = str(item.get("group_id") or "").strip()
            group_title = str(item.get("group_title") or "").strip()
            if not group_id:
                continue
            group_aliases[group_id.lower()] = group_id
            if group_title:
                group_aliases[group_title.lower()] = group_id
            id_map: dict[str, str] = {}
            title_map: dict[str, str] = {}
            subgroup_items_raw = item.get("subgroup_items")
            subgroup_items: list[dict[str, str]] = []
            if isinstance(subgroup_items_raw, list) and subgroup_items_raw:
                for subgroup_item in subgroup_items_raw:
                    if not isinstance(subgroup_item, Mapping):
                        continue
                    subgroup_id = str(subgroup_item.get("subgroup_id") or "").strip()
                    subgroup_title = str(subgroup_item.get("subgroup") or subgroup_item.get("subgroup_title") or "").strip()
                    if not subgroup_id or not subgroup_title:
                        continue
                    subgroup_items.append({"subgroup_id": subgroup_id, "subgroup": subgroup_title})
            if not subgroup_items:
                for subgroup_title in item.get("subgroups") or []:
                    clean_subgroup = str(subgroup_title or "").strip()
                    if not clean_subgroup:
                        continue
                    subgroup_items.append(
                        {
                            "subgroup_id": cls._build_subgroup_id(group_id, clean_subgroup),
                            "subgroup": clean_subgroup,
                        }
                    )
            for subgroup_item in subgroup_items:
                subgroup_id = str(subgroup_item.get("subgroup_id") or "").strip()
                subgroup_title = str(subgroup_item.get("subgroup") or "").strip()
                if not subgroup_id or not subgroup_title:
                    continue
                id_map[subgroup_id.lower()] = subgroup_title
                title_map[cls._normalize_subgroup_name(subgroup_title)] = subgroup_title
            subgroup_ids_by_group[group_id] = id_map
            subgroup_titles_by_group[group_id] = title_map

        def _detect_group(candidate: str) -> str | None:
            clean = str(candidate or "").strip().lower()
            if not clean:
                return None
            if clean in group_aliases:
                return group_aliases[clean]
            normalized_clean = clean.replace(" ", "_").replace("-", "_")
            if normalized_clean in group_aliases:
                return group_aliases[normalized_clean]
            for alias, group in group_aliases.items():
                if alias and alias in clean:
                    return group
            return None

        def _detect_subgroup(group_id: str, candidate: str) -> tuple[str, str] | None:
            id_map = subgroup_ids_by_group.get(group_id) or {}
            title_map = subgroup_titles_by_group.get(group_id) or {}
            if not id_map and not title_map:
                return None
            raw_candidate = str(candidate or "").strip()
            clean = raw_candidate.lower()
            if clean in id_map:
                subgroup_title = id_map[clean]
                return clean, subgroup_title
            normalized_candidate = clean.replace(" ", "_").replace("-", "_")
            if normalized_candidate in id_map:
                subgroup_title = id_map[normalized_candidate]
                return normalized_candidate, subgroup_title
            normalized_title_candidate = cls._normalize_subgroup_name(raw_candidate)
            if normalized_title_candidate and normalized_title_candidate in title_map:
                subgroup_title = title_map[normalized_title_candidate]
                return cls._build_subgroup_id(group_id, subgroup_title), subgroup_title
            lowered_candidate = str(candidate or "").strip().lower()
            for subgroup_id, subgroup_title in id_map.items():
                if subgroup_id and subgroup_id in lowered_candidate:
                    return cls._build_subgroup_id(group_id, subgroup_title), subgroup_title
            for normalized_title, subgroup_title in title_map.items():
                if normalized_title and normalized_title in cls._normalize_subgroup_name(lowered_candidate):
                    return cls._build_subgroup_id(group_id, subgroup_title), subgroup_title
            return None

        def _to_result(group_id: str, subgroup_id: str, subgroup_title: str) -> dict[str, str]:
            return {
                "group_id": group_id,
                "subgroup_id": subgroup_id,
                "subgroup": subgroup_title,
            }

        parsed_object: dict[str, object] | None = None
        try:
            maybe = json.loads(response)
            if isinstance(maybe, Mapping):
                parsed_object = dict(maybe)
        except Exception:
            pass
        if parsed_object is None:
            json_match = re.search(r"\{.*\}", response, flags=re.DOTALL)
            if json_match:
                try:
                    maybe_nested = json.loads(json_match.group(0))
                    if isinstance(maybe_nested, Mapping):
                        parsed_object = dict(maybe_nested)
                except Exception:
                    parsed_object = None
        if parsed_object is not None:
            group_candidate = str(
                parsed_object.get("group_id")
                or parsed_object.get("group")
                or parsed_object.get("category")
                or ""
            ).strip()
            subgroup_candidate = str(
                parsed_object.get("subgroup_id")
                or parsed_object.get("subgroup")
                or parsed_object.get("subcategory")
                or ""
            ).strip()
            detected_group = _detect_group(group_candidate)
            if detected_group:
                if subgroup_candidate:
                    detected_subgroup = _detect_subgroup(detected_group, subgroup_candidate)
                    if detected_subgroup:
                        return _to_result(detected_group, detected_subgroup[0], detected_subgroup[1])
                    return {"group_id": detected_group, "subgroup_id": "", "subgroup": ""}
                return {"group_id": detected_group, "subgroup_id": "", "subgroup": ""}

        group_match = re.search(r"group[_\s-]*id\s*[:=]\s*([a-z0-9_:-]+)", normalized_response)
        subgroup_match = re.search(r"subgroup[_\s-]*id\s*[:=]\s*([a-z0-9_:-]+)", normalized_response)
        if group_match and subgroup_match:
            detected_group = _detect_group(group_match.group(1))
            if detected_group:
                detected_subgroup = _detect_subgroup(detected_group, subgroup_match.group(1))
                if detected_subgroup:
                    return _to_result(detected_group, detected_subgroup[0], detected_subgroup[1])

        for separator in ("|", "/", ";"):
            if separator not in response:
                continue
            left, right = response.split(separator, 1)
            detected_group = _detect_group(left)
            if detected_group:
                detected_subgroup = _detect_subgroup(detected_group, right)
                if detected_subgroup:
                    return _to_result(detected_group, detected_subgroup[0], detected_subgroup[1])

        best_group: str | None = None
        for alias, group_id in group_aliases.items():
            if alias and alias in normalized_response:
                best_group = group_id
                break
        if not best_group:
            return None
        detected_subgroup = _detect_subgroup(best_group, response)
        if not detected_subgroup:
            return None
        return _to_result(best_group, detected_subgroup[0], detected_subgroup[1])

    def _classify_with_yandex_target(
        self,
        *,
        review: ReviewInput,
        settings: dict[str, object],
        user_id: int,
        strict: bool = False,
    ) -> tuple[str, str | None]:
        result = self._classify_with_yandex_target_debug(
            review=review,
            settings=settings,
            user_id=user_id,
            strict=strict,
        )
        return (
            str(result.get("group_id") or "").strip(),
            str(result.get("subgroup") or "").strip() or None,
        )

    @classmethod
    def _default_subgroup_for_group(cls, group_id: str, options: list[dict[str, object]]) -> dict[str, str] | None:
        clean_group_id = str(group_id or "").strip()
        if not clean_group_id or clean_group_id not in cls.REVIEW_GROUPS_WITH_GENERAL_SUBGROUP:
            return None
        for item in options:
            candidate_group_id = str(item.get("group_id") or "").strip()
            if candidate_group_id != clean_group_id:
                continue
            subgroup_items = item.get("subgroup_items")
            if not isinstance(subgroup_items, list):
                continue
            for subgroup_item in subgroup_items:
                if not isinstance(subgroup_item, Mapping):
                    continue
                subgroup_title = str(subgroup_item.get("subgroup") or subgroup_item.get("subgroup_title") or "").strip()
                subgroup_id = str(subgroup_item.get("subgroup_id") or "").strip()
                if cls._normalize_subgroup_name(subgroup_title) != cls._normalize_subgroup_name(cls.GENERAL_SUBGROUP_TITLE):
                    continue
                if subgroup_id:
                    return {"subgroup_id": subgroup_id, "subgroup": subgroup_title}
                return {
                    "subgroup_id": cls._build_subgroup_id(clean_group_id, subgroup_title),
                    "subgroup": subgroup_title,
                }
        return None

    def _classify_with_yandex_target_debug(
        self,
        *,
        review: ReviewInput,
        settings: dict[str, object],
        user_id: int,
        strict: bool = False,
    ) -> dict[str, object]:
        api_key = str(settings.get("yandex_api_key") or "")
        folder_id = str(settings.get("yandex_folder_id") or "")
        model_uri = str(settings.get("yandex_model_uri") or "")
        if not api_key or not folder_id:
            if strict:
                raise MarketplaceSyncError(
                    "yandex",
                    "Яндекс-классификатор не настроен: укажите ключ API и идентификатор каталога.",
                    details={"scope": "classification"},
                )
            return {"group_id": "", "subgroup_id": "", "subgroup": None, "raw_response": "", "model_uri": model_uri}
        if not model_uri:
            model_uri = f"gpt://{folder_id}/yandexgpt-lite/latest"

        options = self._list_group_subgroups_for_review_classification(repository=self.repository, user_id=user_id)
        options_for_prompt = [item for item in options if str(item.get("group_id") or "") != self.TEXTLESS_GROUP_ID]
        if not options_for_prompt:
            if strict:
                raise MarketplaceSyncError(
                    "yandex",
                    "Не удалось сформировать список групп/подгрупп для классификации.",
                    details={"scope": "classification"},
                )
            return {"group_id": "", "subgroup_id": "", "subgroup": None, "raw_response": "", "model_uri": model_uri}

        options_lines: list[str] = []
        allowed_pairs: list[dict[str, str]] = []
        for item in options_for_prompt:
            group_id = str(item.get("group_id") or "")
            group_title = str(item.get("group_title") or group_id)
            subgroup_items_raw = item.get("subgroup_items")
            subgroup_items: list[dict[str, str]] = []
            if isinstance(subgroup_items_raw, list):
                for subgroup_item in subgroup_items_raw:
                    if not isinstance(subgroup_item, Mapping):
                        continue
                    subgroup_id = str(subgroup_item.get("subgroup_id") or "").strip()
                    subgroup_title = str(subgroup_item.get("subgroup") or subgroup_item.get("subgroup_title") or "").strip()
                    if not subgroup_id or not subgroup_title:
                        continue
                    subgroup_items.append({"subgroup_id": subgroup_id, "subgroup": subgroup_title})
            if not subgroup_items:
                continue
            options_lines.append(f"- {group_id} ({group_title}):")
            for subgroup_item in subgroup_items:
                subgroup_id = str(subgroup_item.get("subgroup_id") or "").strip()
                subgroup_title = str(subgroup_item.get("subgroup") or "").strip()
                if not subgroup_id or not subgroup_title:
                    continue
                allowed_pairs.append(
                    {
                        "group_id": group_id,
                        "group_title": group_title,
                        "subgroup_id": subgroup_id,
                        "subgroup": subgroup_title,
                    }
                )
                options_lines.append(
                    f"  - {subgroup_id}: {subgroup_title}"
                )
        # System prompt: static instruction + groups list (cached by Yandex on repeated calls)
        system_text = (
            "Ты классификатор отзывов. Определи одну категорию и одну подгруппу.\n"
            "Список допустимых вариантов group_id/subgroup_id:\n"
            f"{chr(10).join(options_lines)}\n"
            "Ответ строго в JSON-формате без пояснений: "
            '{"group_id":"<group_id>","subgroup_id":"<subgroup_id>"}. '
            "Нельзя возвращать значения, которых нет в списке."
        )

        # User prompt: only variable part — review text truncated to 400 chars
        _MAX_TEXT = 400
        review_text = (review.text or "(без текста)")
        if len(review_text) > _MAX_TEXT:
            review_text = review_text[:_MAX_TEXT] + "…"
        user_text = (
            f"Отзыв: {review_text}\n"
            f"Оценка: {review.rating if review.rating is not None else 'неизвестно'}"
        )

        body = {
            "modelUri": model_uri,
            "completionOptions": {"stream": False, "temperature": 0.0, "maxTokens": 80},
            "messages": [
                {"role": "system", "text": system_text},
                {"role": "user", "text": user_text},
            ],
        }
        request = Request(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            method="POST",
            headers={
                "Authorization": f"Api-Key {api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode("utf-8"),
        )
        try:
            payload = _request_json(request=request, timeout=20, source="yandex", retries=1)
        except MarketplaceSyncError as exc:
            if strict:
                raise MarketplaceSyncError(
                    "yandex",
                    f"Ошибка запроса к Яндекс-классификатору: {exc}",
                    details={
                        "scope": "classification",
                        "model_uri": model_uri,
                        "expected_format": '{"group_id":"<group_id>","subgroup_id":"<subgroup_id>"}',
                        "allowed_pairs": allowed_pairs,
                        "prompt_preview": (system_text[:4000] + "\n---\n" + user_text)[:8000],
                    },
                ) from exc
            return {"group_id": "", "subgroup_id": "", "subgroup": None, "raw_response": "", "model_uri": model_uri}

        text = ""
        result = payload.get("result") if isinstance(payload, Mapping) else None
        if isinstance(result, dict):
            # Log token usage for admin statistics
            usage = result.get("usage") or {}
            input_tokens = int(usage.get("inputTextTokens") or 0)
            output_tokens = int(usage.get("completionTokens") or 0)
            if (input_tokens or output_tokens) and user_id:
                try:
                    self.repository.log_ai_usage(
                        user_id=user_id,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        model_uri=model_uri,
                    )
                except Exception:
                    pass
            alternatives = result.get("alternatives")
            if isinstance(alternatives, list) and alternatives:
                first = alternatives[0]
                if isinstance(first, dict):
                    message = first.get("message")
                    if isinstance(message, dict):
                        text = str(message.get("text") or "")
            # Log full request/response for 1-day debug window (after text is extracted)
            if user_id:
                try:
                    self.repository.log_ai_request(
                        user_id=user_id,
                        prompt_system=system_text,
                        prompt_user=user_text,
                        response_text=(text or "")[:1000],
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        model_uri=model_uri,
                        review_rating=review.rating,
                    )
                except Exception:
                    pass

        raw_response = str(text or "").strip()
        parsed = self._parse_yandex_target_response(text, options=options_for_prompt)
        if parsed:
            group_id = str(parsed.get("group_id") or "").strip()
            subgroup_id = str(parsed.get("subgroup_id") or "").strip()
            subgroup_title = str(parsed.get("subgroup") or "").strip()
            if group_id and subgroup_id and subgroup_title:
                return {
                    "group_id": group_id,
                    "subgroup_id": subgroup_id,
                    "subgroup": subgroup_title,
                    "raw_response": raw_response,
                    "model_uri": model_uri,
                }
            if group_id:
                default_subgroup = self._default_subgroup_for_group(group_id, options_for_prompt)
                if default_subgroup:
                    return {
                        "group_id": group_id,
                        "subgroup_id": str(default_subgroup.get("subgroup_id") or ""),
                        "subgroup": str(default_subgroup.get("subgroup") or ""),
                        "raw_response": raw_response,
                        "model_uri": model_uri,
                        "used_default_subgroup": True,
                        "default_subgroup_reason": "missing_or_invalid_subgroup",
                    }
        if strict:
            raise MarketplaceSyncError(
                "yandex",
                "Яндекс-классификатор вернул ответ без корректной группы/подгруппы.",
                details={
                    "scope": "classification",
                    "raw_response": raw_response,
                    "model_uri": model_uri,
                    "expected_format": '{"group_id":"<group_id>","subgroup_id":"<subgroup_id>"}',
                    "allowed_pairs": allowed_pairs,
                    "prompt_preview": prompt[:8000],
                },
            )
        return {
            "group_id": "",
            "subgroup_id": "",
            "subgroup": None,
            "raw_response": raw_response,
            "model_uri": model_uri,
        }

    @staticmethod
    def _normalize_category(text: str, *, allowed_groups: list[str] | None = None) -> str | None:
        allowed = {str(item).strip().lower() for item in (allowed_groups or []) if str(item).strip()}
        if not allowed:
            allowed = {
                "positive",
                "product_dissatisfaction",
                "delivery_problems",
                "wrong_size",
                "textless_ratings",
            }
        aliases = {
            "negative_delivery": "delivery_problems",
            "negative_product": "product_dissatisfaction",
            "negative_other": "product_dissatisfaction",
            "positive_quality": "positive",
            "positive_product": "positive",
            "neutral_other": "product_dissatisfaction",
        }
        cleaned = text.strip().lower().replace(" ", "_").replace("-", "_")
        if cleaned in allowed:
            return cleaned
        if cleaned in aliases and aliases[cleaned] in allowed:
            return aliases[cleaned]
        for alias, mapped in aliases.items():
            if alias in cleaned and mapped in allowed:
                return mapped
        for group_id in allowed:
            if group_id in cleaned:
                return group_id
        return None

    def _classify_with_yandex(
        self,
        review: ReviewInput,
        *,
        settings: dict[str, object],
        strict: bool = False,
        allowed_groups: list[str] | None = None,
    ) -> str | None:
        api_key = str(settings.get("yandex_api_key") or "")
        folder_id = str(settings.get("yandex_folder_id") or "")
        model_uri = str(settings.get("yandex_model_uri") or "")
        if not api_key or not folder_id:
            if strict:
                raise MarketplaceSyncError(
                    "yandex",
                    "Яндекс-классификатор не настроен: укажите ключ API и идентификатор каталога.",
                    details={"scope": "classification"},
                )
            return None
        if not model_uri:
            model_uri = f"gpt://{folder_id}/yandexgpt-lite/latest"

        allowed = [str(item).strip() for item in (allowed_groups or list(self.GROUP_PROCESSING_DEFAULTS.keys())) if str(item).strip()]
        if not allowed:
            if strict:
                raise MarketplaceSyncError(
                    "yandex",
                    "Не заданы группы для Яндекс-классификатора.",
                    details={"scope": "classification"},
                )
            return None

        allowed_list = ", ".join(allowed)

        prompt = (
            "Классифицируй отзыв строго одной группой из списка: "
            f"{allowed_list}.\n"
            f"Отзыв: {review.text}\n"
            f"Оценка: {review.rating if review.rating is not None else 'unknown'}\n"
            "Ответ верни только id группы из списка, без комментариев."
        )

        body = {
            "modelUri": model_uri,
            "completionOptions": {"stream": False, "temperature": 0.0, "maxTokens": 30},
            "messages": [{"role": "user", "text": prompt}],
        }
        request = Request(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            method="POST",
            headers={
                "Authorization": f"Api-Key {api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode("utf-8"),
        )
        try:
            payload = _request_json(request=request, timeout=20, source="yandex", retries=1)
        except MarketplaceSyncError as exc:
            if strict:
                raise MarketplaceSyncError(
                    "yandex",
                    f"Ошибка запроса к Яндекс-классификатору: {exc}",
                    details={"scope": "classification"},
                ) from exc
            return None

        text = ""
        result = payload.get("result") if isinstance(payload, Mapping) else None
        if isinstance(result, dict):
            alternatives = result.get("alternatives")
            if isinstance(alternatives, list) and alternatives:
                first = alternatives[0]
                if isinstance(first, dict):
                    message = first.get("message")
                    if isinstance(message, dict):
                        text = str(message.get("text") or "")
        normalized = self._normalize_category(text, allowed_groups=allowed)
        if normalized:
            return normalized
        if strict:
            raise MarketplaceSyncError(
                "yandex",
                "Яндекс-классификатор вернул ответ без корректной категории.",
                details={"scope": "classification", "raw_response": text[:120]},
            )
        return None

    # ── Contest analysis (оспаривание отзывов) ────────────────────────────────

    _CONTEST_SYSTEM_PROMPT = (
        "Ты модератор отзывов на российских маркетплейсах (Wildberries, OZON, Яндекс Маркет).\n"
        "Проверь каждый отзыв из списка на нарушение правил маркетплейсов.\n\n"
        "Коды нарушений:\n"
        "profanity     — мат, оскорбления, угрозы, агрессия\n"
        "offtopic      — жалоба не о товаре: доставка МП, курьер, ПВЗ, сотрудники площадки\n"
        "personal_data — персональные данные: ФИО, адрес, телефон, email\n"
        "advertising   — реклама, ссылки на сторонние ресурсы, спам-призывы\n"
        "spam          — бессмысленный текст, набор символов, явный дубль\n"
        "false_facts   — явно ложные факты, прямо противоречащие логике покупки\n"
        "wrong_product — отзыв явно о другом товаре (перепутан SKU)\n"
        "fake          — явные признаки фейка: шаблонный текст без деталей, неестественный\n"
        "prohibited    — экстремизм, дискриминация, контент 18+\n\n"
        "Правила:\n"
        "- Отвечай ТОЛЬКО JSON-массивом без пояснений и форматирования\n"
        '- Формат: [{"id":"<id>","violations":["code"],"can_contest":true/false}]\n'
        "- can_contest=true только если есть хотя бы одно нарушение\n"
        "- Добавляй нарушение ТОЛЬКО при явных признаках, не догадывайся\n"
        "- Жалобы на качество, брак, несоответствие товара — НЕ нарушения\n"
        "- Нарушение offtopic: только если жалоба адресована службе МП, а не продавцу\n"
        "- Нарушения false_facts, fake, wrong_product: только при очевидных признаках"
    )

    _CONTEST_VIOLATION_LABELS = {
        "profanity":     "Мат / оскорбления / угрозы",
        "offtopic":      "Не по теме товара (доставка МП)",
        "personal_data": "Персональные данные",
        "advertising":   "Реклама / ссылки",
        "spam":          "Спам",
        "false_facts":   "Ложные факты",
        "wrong_product": "Перепутан товар / SKU",
        "fake":          "Признаки фейка",
        "prohibited":    "Запрещённый контент",
    }

    _CONTEST_APPEAL_TEXTS = {
        "profanity":
            "Отзыв содержит ненормативную лексику, оскорбления или угрозы, что нарушает Правила публикации отзывов площадки. Прошу удалить данный отзыв.",
        "offtopic":
            "Отзыв содержит претензии к работе службы доставки / ПВЗ / курьера маркетплейса, а не оценку товара продавца. Согласно правилам площадки, такой контент не должен влиять на рейтинг карточки товара. Прошу удалить отзыв из карточки товара.",
        "personal_data":
            "Отзыв содержит персональные данные третьих лиц (ФИО, адрес, телефон), что нарушает ФЗ №152 «О персональных данных» и правила площадки. Прошу удалить отзыв.",
        "advertising":
            "Отзыв содержит рекламные материалы и/или ссылки на сторонние ресурсы, что нарушает правила публикации отзывов. Прошу удалить отзыв.",
        "spam":
            "Отзыв является спамом и не содержит информации, связанной с товаром. Прошу удалить отзыв.",
        "false_facts":
            "Отзыв содержит заведомо ложные сведения, не соответствующие фактическим данным заказа. Прошу проверить корректность отзыва и удалить его при подтверждении нарушения.",
        "wrong_product":
            "Отзыв относится к другому товару — покупатель ошибочно привязал его к данной карточке (перепутан SKU). Прошу удалить отзыв из карточки данного товара.",
        "fake":
            "Отзыв имеет признаки фейка: содержание не соответствует реальному опыту покупки данного товара. Прошу проверить наличие подтверждённого заказа у автора отзыва.",
        "prohibited":
            "Отзыв содержит запрещённый контент (экстремизм, дискриминацию, материалы 18+ и т.д.), что нарушает законодательство РФ и правила площадки. Прошу незамедлительно удалить отзыв.",
    }

    def analyze_reviews_for_contest(
        self,
        *,
        run_id: int,
        user_id: int,
        reviews: list[dict[str, Any]],
        api_key: str,
        folder_id: str,
        model_uri: str | None = None,
    ) -> None:
        """Background worker: batch-analyze reviews via Yandex GPT and store results."""
        import json as _j
        if not model_uri:
            model_uri = f"gpt://{folder_id}/yandexgpt-lite/latest"

        batch_size = 5
        total = len(reviews)
        processed = 0
        violations_found = 0

        try:
            for i in range(0, total, batch_size):
                batch = reviews[i: i + batch_size]
                batch_results: list[dict[str, Any]] = []

                # Build user message
                items_json = _j.dumps(
                    [{"id": r["review_uid"], "rating": r.get("rating"), "text": (str(r.get("text") or ""))[:500]}
                     for r in batch],
                    ensure_ascii=False,
                )
                user_text = f"Отзывы:\n{items_json}"

                body = {
                    "modelUri": model_uri,
                    "completionOptions": {"stream": False, "temperature": 0.0, "maxTokens": 512},
                    "messages": [
                        {"role": "system", "text": self._CONTEST_SYSTEM_PROMPT},
                        {"role": "user", "text": user_text},
                    ],
                }
                try:
                    req = Request(
                        "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                        method="POST",
                        headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
                        data=_j.dumps(body).encode("utf-8"),
                    )
                    payload = _request_json(request=req, timeout=45, source="yandex", retries=1)
                    result = payload.get("result") if isinstance(payload, Mapping) else {}
                    alternatives = result.get("alternatives") if isinstance(result, Mapping) else []
                    first = alternatives[0] if isinstance(alternatives, list) and alternatives else {}
                    message = first.get("message") if isinstance(first, Mapping) else {}
                    raw_text = str(message.get("text") or "").strip() if isinstance(message, Mapping) else ""

                    # Parse JSON array from GPT response
                    gpt_items: list[dict[str, Any]] = []
                    try:
                        # Strip markdown code blocks if present
                        clean = raw_text.strip()
                        if clean.startswith("```"):
                            clean = "\n".join(clean.split("\n")[1:])
                            clean = clean.rstrip("`").strip()
                        gpt_items = _j.loads(clean)
                        if not isinstance(gpt_items, list):
                            gpt_items = []
                    except Exception:
                        gpt_items = []

                    # Map results by id
                    gpt_map = {str(item.get("id") or ""): item for item in gpt_items if isinstance(item, Mapping)}
                    for rev in batch:
                        uid = str(rev["review_uid"])
                        gpt_item = gpt_map.get(uid, {})
                        violations = [v for v in (gpt_item.get("violations") or []) if isinstance(v, str)]
                        can_contest = bool(gpt_item.get("can_contest")) or bool(violations)
                        batch_results.append({
                            "review_uid": uid,
                            "violations": violations,
                            "can_contest": can_contest,
                        })

                except Exception as exc:
                    _log.warning("contest batch %d-%d error: %s", i, i + batch_size, exc)
                    # Mark batch reviews as not contestable on error
                    for rev in batch:
                        batch_results.append({
                            "review_uid": str(rev["review_uid"]),
                            "violations": [],
                            "can_contest": False,
                        })

                self.repository.save_contest_results(run_id=run_id, results=batch_results)
                processed += len(batch)
                violations_found += sum(1 for r in batch_results if r["can_contest"])
                self.repository.update_contest_run_progress(
                    run_id=run_id, processed=processed, violations_found=violations_found
                )

            self.repository.update_contest_run_progress(
                run_id=run_id, processed=total, violations_found=violations_found, status="completed"
            )
        except Exception as exc:
            _log.error("contest analysis run %d failed: %s", run_id, exc, exc_info=True)
            self.repository.update_contest_run_progress(
                run_id=run_id, processed=processed, violations_found=violations_found,
                status="error", error=str(exc)[:400],
            )

    def check_yandex_connection(
        self,
        *,
        api_key: str,
        folder_id: str,
        timeout_seconds: int = 12,
    ) -> dict[str, object]:
        clean_api_key = str(api_key or "").strip()
        clean_folder_id = str(folder_id or "").strip()
        if not clean_api_key:
            raise MarketplaceSyncError("yandex", "Укажите API-ключ Yandex Cloud.")
        if not clean_folder_id:
            raise MarketplaceSyncError("yandex", "Укажите ID каталога (folderId).")
        model_uri = f"gpt://{clean_folder_id}/yandexgpt-lite/latest"
        body = {
            "modelUri": model_uri,
            "completionOptions": {"stream": False, "temperature": 0.0, "maxTokens": 8},
            "messages": [{"role": "user", "text": "Ответь одним словом: OK"}],
        }
        request = Request(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            method="POST",
            headers={
                "Authorization": f"Api-Key {clean_api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode("utf-8"),
        )
        payload = _request_json(request=request, timeout=max(int(timeout_seconds), 1), source="yandex", retries=0)
        result = payload.get("result") if isinstance(payload, Mapping) else None
        alternatives = result.get("alternatives") if isinstance(result, Mapping) else None
        first = alternatives[0] if isinstance(alternatives, list) and alternatives else {}
        message = first.get("message") if isinstance(first, Mapping) else {}
        text = str(message.get("text") or "").strip() if isinstance(message, Mapping) else ""
        return {
            "ok": True,
            "message": "Подключение к Yandex GPT успешно.",
            "model_uri": model_uri,
            "response_preview": text[:120],
        }

    def classify_test_review_with_yandex(
        self,
        *,
        user_id: int,
        review_text: str,
        review_rating: int | None = None,
        settings: dict[str, object],
    ) -> dict[str, object]:
        clean_text = str(review_text or "").strip()
        if not clean_text:
            raise MarketplaceSyncError("yandex", "Введите текст тестового отзыва.")

        normalized_rating: int | None = None
        if review_rating is not None:
            try:
                normalized_rating = int(review_rating)
            except (TypeError, ValueError) as exc:
                raise MarketplaceSyncError("yandex", "Оценка тестового отзыва должна быть целым числом от 1 до 5.") from exc
            if normalized_rating < 1 or normalized_rating > 5:
                raise MarketplaceSyncError("yandex", "Оценка тестового отзыва должна быть от 1 до 5.")

        options = self._list_group_subgroups_for_review_classification(repository=self.repository, user_id=user_id)
        result = self._classify_with_yandex_target_debug(
            review=ReviewInput(
                review_id="admin-test-review",
                text=clean_text,
                rating=normalized_rating,
                metadata={"source": "admin_test"},
            ),
            settings=settings,
            user_id=user_id,
            strict=True,
        )
        group_id = str(result.get("group_id") or "").strip()
        subgroup_id = str(result.get("subgroup_id") or "").strip()
        subgroup = str(result.get("subgroup") or "").strip()
        if not group_id or not subgroup_id or not subgroup:
            raise MarketplaceSyncError("yandex", "Яндекс-классификатор не вернул корректную группу и подгруппу.")
        return {
            "ok": True,
            "group_id": group_id,
            "group_title": self.REVIEW_GROUP_TITLES.get(group_id, group_id),
            "subgroup_id": subgroup_id,
            "subgroup": subgroup,
            "used_default_subgroup": bool(result.get("used_default_subgroup")),
            "default_subgroup_reason": str(result.get("default_subgroup_reason") or ""),
            "model_uri": str(result.get("model_uri") or ""),
            "raw_response": str(result.get("raw_response") or ""),
            "available_options": options,
        }

    @classmethod
    def _resolve_group_processors(cls, settings: dict[str, object]) -> dict[str, str]:
        modes: dict[str, str] = dict(cls.GROUP_PROCESSING_DEFAULTS)
        raw = settings.get("group_processors")
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                group_id = str(key or "").strip()
                mode = str(value or "").strip().lower()
                if not group_id:
                    continue
                if mode not in {"yandex", "program"}:
                    continue
                modes[group_id] = mode
        return modes

    def _classify_with_program_groups(
        self,
        *,
        review: ReviewInput,
        processed: object,
        allowed_groups: list[str],
    ) -> str | None:
        allowed = {str(item).strip() for item in allowed_groups if str(item).strip()}
        if not allowed:
            return None
        text = (review.text or "").lower()
        size_words = ("размер", "маломер", "большемер", "size", "мерит")
        delivery_words = ("доставк", "курьер", "пункт выдачи", "пвз")
        sentiment = str(getattr(processed, "sentiment_label", "")).strip().lower()

        if "wrong_size" in allowed and any(word in text for word in size_words):
            return "wrong_size"
        if "delivery_problems" in allowed and any(word in text for word in delivery_words):
            return "delivery_problems"
        if sentiment == "positive" and "positive" in allowed:
            return "positive"
        if "product_dissatisfaction" in allowed:
            return "product_dissatisfaction"
        if "positive" in allowed:
            return "positive"
        return next(iter(allowed), None)

    @staticmethod
    def _resolve_processing_mode(
        processed: object,
        template: dict[str, object] | None,
        rule: dict[str, object] | None,
    ) -> tuple[str, bool, str]:
        if rule:
            mode = str(rule.get("action_mode") or "manual").strip().lower()
            if mode in {"ai", "auto", "template", "ignore"}:
                return "template", True, ""
            if mode == "manual":
                return "manual", False, ""
        if template:
            mode = str(template.get("mode") or "manual")
            text = str(template.get("template_text") or "")
            is_enabled = bool(template.get("is_enabled"))
            if is_enabled and mode in {"auto", "template", "ignore"}:
                return "template", True, text
            if is_enabled and mode == "manual":
                return "manual", False, text
        # По умолчанию система не выполняет автообработку, пока правило не включено.
        return "manual", False, ""

    def _pick_recommendation_for_review(self, *, user_id: int | None, review: ReviewInput) -> str:
        if user_id is None:
            return ""
        source_article = self._extract_product_article(review)
        if not source_article:
            return ""
        recommendation = self.repository.get_random_recommendation(
            user_id=user_id,
            source_article=source_article,
        )
        return str(recommendation or "").strip()

    @staticmethod
    def _extract_product_article(review: ReviewInput) -> str | None:
        def _find_in_mapping(payload: Mapping[str, object]) -> str | None:
            keys = (
                "article",
                "article_id",
                "sku",
                "offer_id",
                "offerId",
                "vendor_code",
                "vendorCode",
                "nmId",
                "nm_id",
                "product_id",
                "productId",
                "item_id",
                "itemId",
            )
            for key in keys:
                if key not in payload:
                    continue
                value = str(payload.get(key) or "").strip()
                if value:
                    return value
            return None

        metadata = review.metadata if isinstance(review.metadata, dict) else {}
        candidates: list[Mapping[str, object]] = []
        if metadata:
            candidates.append(metadata)
        raw = metadata.get("raw")
        if isinstance(raw, Mapping):
            candidates.append(raw)
            nested = raw.get("product")
            if isinstance(nested, Mapping):
                candidates.append(nested)
            nested_item = raw.get("item")
            if isinstance(nested_item, Mapping):
                candidates.append(nested_item)
        for payload in candidates:
            found = _find_in_mapping(payload)
            if found:
                return found
        return None

    @staticmethod
    def _cleanup_rendered_text(text: str) -> str:
        clean = text.strip()
        clean = re.sub(r"\s+([,.;:!?])", r"\1", clean)
        clean = re.sub(r"\s{2,}", " ", clean)
        clean = clean.replace(",!", "!").replace(",?", "?").replace(",.", ".")
        clean = re.sub(r"^[,.;:!?\-\s]+", "", clean)
        clean = clean.replace(" ,", ",").replace(" .", ".").replace(" !", "!").replace(" ?", "?")
        return clean.strip()

    def _render_template(
        self,
        template: str,
        *,
        user_id: int | None,
        review: ReviewInput,
        category: str,
        sentiment: str,
    ) -> str:
        text = template or "Спасибо за отзыв!"
        author_raw = (review.author or "").strip()
        author = author_raw or "клиент"
        rating = review.rating if review.rating is not None else "без оценки"
        category_ru = {
            "negative_delivery": "Негатив: доставка",
            "negative_product": "Негатив: товар",
            "negative_other": "Негатив: прочее",
            "positive_quality": "Позитив: качество",
            "positive_product": "Позитив: товар",
            "neutral_other": "Нейтральный: прочее",
        }.get(category, category)
        sentiment_ru = {
            "negative": "негативная",
            "positive": "позитивная",
            "neutral": "нейтральная",
        }.get(sentiment, sentiment)
        tags_text = ", ".join(self._extract_review_tags(review)) or "без тегов"
        context = {
            "author": author,
            "автор": author,
            "rating": rating,
            "оценка": rating,
            "category": category,
            "категория": category_ru,
            "sentiment": sentiment,
            "тональность": sentiment_ru,
            "tags": tags_text,
            "теги": tags_text,
            "review_id": review.review_id,
            "идентификатор_отзыва": review.review_id,
        }
        for key, value in context.items():
            text = text.replace(f"{{{key}}}", str(value))
        reco = self._pick_recommendation_for_review(user_id=user_id, review=review)
        variables_context = self.repository.build_template_variables_context(
            user_id=user_id,
            review_author=author_raw,
            review_rating=review.rating,
            review_category=category,
            review_sentiment=sentiment,
            review_tags=self._extract_review_tags(review),
            review_metadata=review.metadata if isinstance(review.metadata, dict) else {},
        )
        if not author_raw:
            for _ph in ("%USER%", "%AUTHOR%"):
                text = text.replace(f", {_ph}", "").replace(f" {_ph}", "")
        text = text.replace("%USER%", author_raw)
        text = text.replace("%RECO%", reco)
        text = text.replace("%%RECO%%", reco)
        for key, value in variables_context.items():
            text = text.replace(str(key), str(value or ""))
        # Remove any remaining unreplaced %VARIABLE% placeholders
        import re as _re
        text = _re.sub(r'%[A-Z0-9_]{2,50}%', '', text)
        return self._cleanup_rendered_text(text)

    @staticmethod
    def _build_auto_reply(review: dict[str, object]) -> str:
        sentiment = str(review.get("sentiment_label"))
        priority = str(review.get("priority"))
        is_spam = bool(review.get("is_spam"))

        if is_spam:
            return "Спасибо за отзыв. Комментарий отмечен системой модерации."
        if priority == "high" or sentiment == "negative":
            return (
                "Спасибо за обратную связь. Нам жаль, что вы столкнулись с проблемой. "
                "Мы уже передали отзыв в поддержку и вернемся с решением."
            )
        if sentiment == "positive":
            return "Спасибо за высокую оценку! Очень рады, что вам понравилось."
        return "Спасибо за отзыв! Мы учтем его в дальнейших улучшениях."


def _is_tag_candidate(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if len(text) > 120:
        return False
    if text.isdigit():
        return False
    return any(char.isalpha() for char in text)


def _extract_review_tags_from_payload(payload: object) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def _push(raw_value: object) -> None:
        if isinstance(raw_value, str):
            parts = re.split(r"[,\n;/|]+", raw_value)
            for part in parts:
                clean = part.strip()
                if not _is_tag_candidate(clean):
                    continue
                key = clean.lower()
                if key in seen:
                    continue
                seen.add(key)
                result.append(clean)
            return
        if isinstance(raw_value, (int, float)):
            return
        if isinstance(raw_value, list):
            for item in raw_value:
                _push(item)
            return
        if isinstance(raw_value, Mapping):
            for key, value in raw_value.items():
                key_text = str(key).lower()
                if key_text in {"name", "title", "value", "text", "label", "tag"}:
                    _push(value)
                    continue
                if any(marker in key_text for marker in ("tag", "label", "mark", "pros", "cons", "advantage", "disadvantage")):
                    _push(value)
                    continue
                if isinstance(value, (list, Mapping)):
                    _push(value)

    if not isinstance(payload, Mapping):
        return result
    for candidate_key in (
        "tags",
        "tag",
        "labels",
        "marks",
        "pros",
        "cons",
        "advantages",
        "disadvantages",
        "pluses",
        "minuses",
        "qualities",
        "quality_tags",
    ):
        if candidate_key in payload:
            _push(payload.get(candidate_key))
    for nested_key in ("raw", "details", "product", "item", "attributes", "options"):
        nested_value = payload.get(nested_key)
        if nested_value is not None:
            _push(nested_value)
    return result


def _wb_image_url(img: dict, *, chats_api_url: str = "https://buyer-chat-api.wildberries.ru") -> str:
    """Return a publicly accessible URL for a WB chat image.

    WB events return ``url`` as an internal K8s address (sellers-chat-inner.*)
    that is not reachable from outside WB infrastructure.
    The ``downloadID`` field can be used with the public API endpoint
    ``GET /api/v1/seller/download/{id}`` which requires Authorization header.
    We store this as ``[img:wb-download:downloadID]`` so the proxy endpoint
    can distinguish it from regular public URLs.
    """
    download_id = str(img.get("downloadID") or "").strip()
    if download_id:
        return f"wb-download:{download_id}"
    raw_url = str(img.get("url") or "").strip()
    return raw_url


def _parse_ozon_message_text(data_parts: object, is_image: bool) -> str:
    """Convert Ozon message data list to our internal text format.

    Ozon sends images as Markdown: ``![](https://api-seller.ozon.ru/...)``
    We convert these to ``[img:url]`` tokens so the frontend can render
    them via our proxy endpoint.
    Plain text parts are joined with a space.
    """
    import re as _re
    parts = data_parts if isinstance(data_parts, list) else []
    text_parts: list[str] = []
    img_parts: list[str] = []
    md_img_re = _re.compile(r'!\[.*?\]\((https?://[^)]+)\)')
    for part in parts:
        raw = str(part or "").strip()
        if not raw:
            continue
        md_match = md_img_re.search(raw)
        if md_match:
            img_parts.append(f"[img:{md_match.group(1)}]")
        else:
            text_parts.append(raw)
    if img_parts:
        combined = " ".join(img_parts)
        if text_parts:
            combined += " " + " ".join(text_parts)
        return combined
    if is_image and not text_parts:
        return "[Фото]"
    return " ".join(text_parts)


def _normalize_timestamp(value: object) -> str | None:
    """Convert a timestamp value to an ISO-8601 string.

    WB Buyer Chat API returns ``addTimestamp`` as Unix milliseconds (integer).
    Storing the raw integer as a string would break lexicographic comparisons
    used by the ``processed_by_operator`` SQL clause and date-range filters.
    This helper converts millisecond integers to ISO strings and passes through
    values that are already strings.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts_sec = float(value)
        # Heuristic: values > 1e10 are milliseconds, not seconds
        if ts_sec > 1e10:
            ts_sec = ts_sec / 1000.0
        try:
            return datetime.fromtimestamp(ts_sec, tz=UTC).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    # If the string is a pure integer it may still be a unix timestamp
    if raw.lstrip("-").isdigit():
        try:
            ts_sec = float(raw)
            if ts_sec > 1e10:
                ts_sec = ts_sec / 1000.0
            return datetime.fromtimestamp(ts_sec, tz=UTC).isoformat()
        except (OSError, OverflowError, ValueError):
            pass
    return raw


def _extract_sequence(payload: object, *, keys: tuple[str, ...]) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _extract_str(payload: object, *, keys: tuple[str, ...]) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_positive_int(value: object, *, default: int) -> int:
    parsed = _to_int(value)
    if parsed is None or parsed <= 0:
        return default
    return parsed


def _compose_url(base_url: str, path: str | None) -> str:
    if not path:
        return base_url
    normalized_path = "/" + path.strip("/")
    parsed = urlparse(base_url)
    # If path starts with "/" treat it as absolute path from host root,
    # so we always replace the path component rather than appending to it.
    if path.startswith("/"):
        base_root = f"{parsed.scheme}://{parsed.netloc}"
        return base_root + normalized_path
    normalized_base = base_url.rstrip("/")
    if normalized_base.endswith(normalized_path):
        return normalized_base
    return urljoin(normalized_base + "/", path.lstrip("/"))


def _raise_if_error_payload(payload: object, *, source: str) -> None:
    if not isinstance(payload, Mapping):
        return

    explicit_error = payload.get("error")
    if explicit_error:
        raise MarketplaceSyncError(source, f"{source} error: {explicit_error}")

    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        joined = "; ".join(str(item) for item in errors[:5])
        raise MarketplaceSyncError(source, f"{source} errors: {joined}")
    if isinstance(errors, Mapping) and errors:
        joined = "; ".join(f"{k}:{v}" for k, v in list(errors.items())[:5])
        raise MarketplaceSyncError(source, f"{source} errors: {joined}")

    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"error", "failed", "fail"}:
        message = str(payload.get("message") or payload.get("detail") or "unknown error")
        raise MarketplaceSyncError(source, f"{source} status error: {message}")

    if payload.get("errorText"):
        raise MarketplaceSyncError(source, f"{source} error: {payload.get('errorText')}")


def _request_json(*, request: Request, timeout: int, source: str, retries: int = 2,
                  retry_5xx: bool = False) -> object:
    """Execute an HTTP request and return the parsed JSON response.

    Retryable errors (network, timeout, JSON parse): up to ``retries`` retries
    with exponential backoff starting at 0.4 s.

    HTTP 429 Too Many Requests: retried with a mandatory 60-second wait so we
    respect marketplace rate-limit windows before trying again.  After
    ``retries`` 429 responses the error is re-raised so the caller can handle
    it gracefully (log as access-rate issue, not a hard failure).

    HTTP 5xx (when retry_5xx=True): retried with 2s/4s backoff — handles
    transient WB server errors like 'no free connections available'.

    All other HTTP errors (4xx except 429) are raised immediately.
    """
    attempt = 0
    rate_limit_attempt = 0
    server_error_attempt = 0
    while True:
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read()
                if not body:
                    # Some PATCH/POST endpoints return empty body on success (HTTP 200/204)
                    return {}
                return json.loads(body.decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 429:
                # Rate-limited: wait and retry up to ``retries`` times.
                rate_limit_attempt += 1
                if rate_limit_attempt > retries:
                    message = f"{source} HTTP error 429: rate limit exceeded"
                    raise MarketplaceSyncError(source, message) from exc
                # Back off: 60 s for the first hit, 120 s for the second.
                wait_sec = 60 * rate_limit_attempt
                time.sleep(wait_sec)
                continue
            if retry_5xx and exc.code >= 500:
                # Transient server error (e.g. WB "no free connections") — retry with backoff.
                server_error_attempt += 1
                if server_error_attempt <= retries:
                    _log.warning("%s HTTP %d on attempt %d — retrying in %ds",
                                 source, exc.code, server_error_attempt, 2 * server_error_attempt)
                    time.sleep(2 * server_error_attempt)
                    continue
            message = f"{source} HTTP error {exc.code}"
            try:
                body_text = exc.read().decode("utf-8")
                if body_text:
                    message = f"{message}: {body_text[:400]}"
            except Exception:
                pass
            raise MarketplaceSyncError(source, message) from exc
        except (URLError, TimeoutError, ValueError) as exc:
            if attempt >= retries:
                raise MarketplaceSyncError(source, f"{source} network/parse error: {exc}") from exc
            time.sleep(0.4 * (2**attempt))
            attempt += 1
