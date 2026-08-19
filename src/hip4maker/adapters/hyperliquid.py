"""Parse Hyperliquid `l2Book` snapshots for HIP-4 outcome coins."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hip4maker.adapters.common import best_full_book, require_mapping, require_sequence
from hip4maker.books import (
    BinarySide,
    BookError,
    CanonicalBook,
    canonicalize_full_book,
)


def parse_l2_book(
    payload: Mapping[str, Any],
    *,
    queried_side_payout: BinarySide | str,
    mapping_id: str,
    received_ts_ms: int,
) -> CanonicalBook:
    coin = payload.get("coin")
    timestamp = payload.get("time")
    levels = require_sequence(payload.get("levels"), "levels")
    if not isinstance(coin, str) or not coin.startswith("#"):
        raise BookError("Hyperliquid outcome coin must use #<encoding> form")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise BookError("Hyperliquid book time must be a nonnegative integer")
    if len(levels) != 2:
        raise BookError("Hyperliquid levels must contain bid and ask arrays")

    bids = [
        _parse_level(value, "bid")
        for value in require_sequence(levels[0], "levels[0]")
    ]
    asks = [
        _parse_level(value, "ask")
        for value in require_sequence(levels[1], "levels[1]")
    ]
    source_book = best_full_book(
        bids=bids,
        asks=asks,
        source_ts_ms=timestamp,
        received_ts_ms=received_ts_ms,
        source="hyperliquid",
        market_id=coin,
    )
    return canonicalize_full_book(
        source_book,
        source_payout_side=queried_side_payout,
        mapping_id=mapping_id,
    )


def _parse_level(value: Any, side: str) -> tuple[Any, Any]:
    level = require_mapping(value, f"{side} level")
    if "px" not in level or "sz" not in level:
        raise BookError(f"Hyperliquid {side} level requires px and sz")
    return level["px"], level["sz"]
