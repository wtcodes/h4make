"""Parse Polymarket CLOB token order books."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hip4maker.adapters.common import best_full_book, require_mapping, require_sequence
from hip4maker.books import BinarySide, BookError, CanonicalBook, canonicalize_full_book


def parse_orderbook(
    payload: Mapping[str, Any],
    *,
    token_payout_side: BinarySide | str,
    mapping_id: str,
    received_ts_ms: int,
) -> CanonicalBook:
    token_id = payload.get("asset_id")
    timestamp_raw = payload.get("timestamp")
    if not isinstance(token_id, str) or not token_id:
        raise BookError("Polymarket asset_id must be a non-empty string")
    try:
        timestamp = int(timestamp_raw)
    except (TypeError, ValueError) as exc:
        raise BookError("Polymarket timestamp must be integer milliseconds") from exc
    if timestamp < 0:
        raise BookError("Polymarket timestamp must be nonnegative")

    bids = [
        _parse_level(level, "bid")
        for level in require_sequence(payload.get("bids"), "bids")
    ]
    asks = [
        _parse_level(level, "ask")
        for level in require_sequence(payload.get("asks"), "asks")
    ]
    source_book = best_full_book(
        bids=bids,
        asks=asks,
        source_ts_ms=timestamp,
        received_ts_ms=received_ts_ms,
        source="polymarket",
        market_id=token_id,
    )
    return canonicalize_full_book(
        source_book,
        source_payout_side=token_payout_side,
        mapping_id=mapping_id,
    )


def _parse_level(value: Any, side: str) -> tuple[Any, Any]:
    level = require_mapping(value, f"{side} level")
    if "price" not in level or "size" not in level:
        raise BookError(f"Polymarket {side} level requires price and size")
    return level["price"], level["size"]

