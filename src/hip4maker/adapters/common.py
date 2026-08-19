from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from hip4maker.books import BookError, TopOfBook, decimal_value


def require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BookError(f"{field} must be an object")
    return value


def require_sequence(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise BookError(f"{field} must be an array")
    return value


def best_full_book(
    *,
    bids: Iterable[tuple[Any, Any]],
    asks: Iterable[tuple[Any, Any]],
    source_ts_ms: int,
    received_ts_ms: int,
    source: str,
    market_id: str,
) -> TopOfBook:
    """Build a bounded binary top of book, using 0/1 for missing sides."""

    parsed_bids = [
        (decimal_value(price, "bid price"), decimal_value(size, "bid size"))
        for price, size in bids
    ]
    parsed_asks = [
        (decimal_value(price, "ask price"), decimal_value(size, "ask size"))
        for price, size in asks
    ]
    bid, bid_size = (
        max(parsed_bids, key=lambda level: level[0])
        if parsed_bids
        else (decimal_value(0, "bid price"), decimal_value(0, "bid size"))
    )
    ask, ask_size = (
        min(parsed_asks, key=lambda level: level[0])
        if parsed_asks
        else (decimal_value(1, "ask price"), decimal_value(0, "ask size"))
    )
    return TopOfBook(
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        source_ts_ms=source_ts_ms,
        received_ts_ms=received_ts_ms,
        source=source,
        market_id=market_id,
    )
