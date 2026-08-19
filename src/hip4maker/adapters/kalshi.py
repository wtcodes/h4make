"""Parse Kalshi's Yes-bids/No-bids binary order book."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hip4maker.adapters.common import require_mapping, require_sequence
from hip4maker.books import (
    BidLevel,
    BinarySide,
    BookError,
    CanonicalBook,
    canonicalize_binary_bid_books,
)


def parse_orderbook(
    payload: Mapping[str, Any],
    *,
    canonical_yes_source_side: BinarySide | str,
    market_ticker: str,
    mapping_id: str,
    source_ts_ms: int,
    received_ts_ms: int,
) -> CanonicalBook:
    orderbook = require_mapping(payload.get("orderbook_fp"), "orderbook_fp")
    yes_raw = orderbook.get("yes_dollars")
    no_raw = orderbook.get("no_dollars")
    if yes_raw is None or no_raw is None:
        raise BookError("Kalshi orderbook_fp requires yes_dollars and no_dollars")
    yes_bids = [_parse_level(level, "yes") for level in require_sequence(yes_raw, "yes_dollars")]
    no_bids = [_parse_level(level, "no") for level in require_sequence(no_raw, "no_dollars")]
    return canonicalize_binary_bid_books(
        yes_bids=yes_bids,
        no_bids=no_bids,
        canonical_yes_source_side=canonical_yes_source_side,
        source_ts_ms=source_ts_ms,
        received_ts_ms=received_ts_ms,
        source="kalshi",
        market_id=market_ticker,
        mapping_id=mapping_id,
    )


def _parse_level(value: Any, side: str) -> BidLevel:
    level = require_sequence(value, f"{side} level")
    if len(level) < 2:
        raise BookError(f"Kalshi {side} level requires price and size")
    return BidLevel(level[0], level[1])

