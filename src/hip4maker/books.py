"""Canonical outcome-book types and side normalization."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Iterable


ZERO = Decimal("0")
ONE = Decimal("1")


class BookError(ValueError):
    """Raised when a book cannot represent a valid bounded binary market."""


class BinarySide(StrEnum):
    YES = "yes"
    NO = "no"

    @classmethod
    def parse(cls, value: str | "BinarySide") -> "BinarySide":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower())
        except ValueError as exc:
            raise BookError(f"invalid binary side {value!r}; expected yes or no") from exc


def decimal_value(value: Decimal | str | int | float, field: str) -> Decimal:
    if isinstance(value, bool):
        raise BookError(f"{field} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BookError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise BookError(f"{field} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class TopOfBook:
    """A full bid/ask book for one venue token or economic proposition."""

    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    source_ts_ms: int
    received_ts_ms: int
    source: str
    market_id: str

    def __post_init__(self) -> None:
        for field in ("bid", "ask", "bid_size", "ask_size"):
            object.__setattr__(self, field, decimal_value(getattr(self, field), field))
        if not ZERO <= self.bid <= self.ask <= ONE:
            raise BookError(
                f"book must satisfy 0 <= bid <= ask <= 1; got {self.bid} / {self.ask}"
            )
        if self.bid_size < ZERO or self.ask_size < ZERO:
            raise BookError("book sizes must be nonnegative")
        if self.source_ts_ms < 0 or self.received_ts_ms < 0:
            raise BookError("book timestamps must be nonnegative")
        if not self.source or not self.market_id:
            raise BookError("book source and market_id are required")

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    def is_stale(self, now_ms: int, stale_after_ms: int) -> bool:
        return now_ms - self.received_ts_ms > stale_after_ms


@dataclass(frozen=True, slots=True)
class CanonicalBook(TopOfBook):
    """A book normalized to the configured canonical-Yes payout."""

    mapping_id: str

    def __post_init__(self) -> None:
        super(CanonicalBook, self).__post_init__()
        if not self.mapping_id:
            raise BookError("canonical book mapping_id is required")


@dataclass(frozen=True, slots=True)
class BidLevel:
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", decimal_value(self.price, "price"))
        object.__setattr__(self, "size", decimal_value(self.size, "size"))
        if not ZERO <= self.price <= ONE:
            raise BookError(f"bid price must be within [0, 1], got {self.price}")
        if self.size < ZERO:
            raise BookError("bid size must be nonnegative")


def canonicalize_full_book(
    source_book: TopOfBook,
    *,
    source_payout_side: BinarySide | str,
    mapping_id: str,
) -> CanonicalBook:
    """Normalize a full source book to canonical Yes.

    If the source token pays on canonical No, complementation swaps bid/ask:
    canonical_bid = 1 - source_ask and canonical_ask = 1 - source_bid.
    """

    side = BinarySide.parse(source_payout_side)
    if side is BinarySide.YES:
        bid = source_book.bid
        ask = source_book.ask
        bid_size = source_book.bid_size
        ask_size = source_book.ask_size
    else:
        bid = ONE - source_book.ask
        ask = ONE - source_book.bid
        bid_size = source_book.ask_size
        ask_size = source_book.bid_size

    return CanonicalBook(
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        source_ts_ms=source_book.source_ts_ms,
        received_ts_ms=source_book.received_ts_ms,
        source=source_book.source,
        market_id=source_book.market_id,
        mapping_id=mapping_id,
    )


def _best_bid(levels: Iterable[BidLevel]) -> BidLevel:
    levels_tuple = tuple(levels)
    if not levels_tuple:
        return BidLevel(ZERO, ZERO)
    return max(levels_tuple, key=lambda level: level.price)


def canonicalize_binary_bid_books(
    *,
    yes_bids: Iterable[BidLevel],
    no_bids: Iterable[BidLevel],
    canonical_yes_source_side: BinarySide | str,
    source_ts_ms: int,
    received_ts_ms: int,
    source: str,
    market_id: str,
    mapping_id: str,
) -> CanonicalBook:
    """Normalize venues such as Kalshi that publish Yes and No bid books.

    The opposing bid supplies the canonical ask. If venue No is the configured
    canonical-Yes proposition, the two books exchange roles.
    """

    # A missing bid is bounded at zero with no displayed liquidity. Its
    # opposing canonical ask is therefore bounded at one.
    best_yes = _best_bid(yes_bids)
    best_no = _best_bid(no_bids)
    side = BinarySide.parse(canonical_yes_source_side)

    if side is BinarySide.YES:
        bid = best_yes.price
        ask = ONE - best_no.price
        bid_size = best_yes.size
        ask_size = best_no.size
    else:
        bid = best_no.price
        ask = ONE - best_yes.price
        bid_size = best_no.size
        ask_size = best_yes.size

    return CanonicalBook(
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        source_ts_ms=source_ts_ms,
        received_ts_ms=received_ts_ms,
        source=source,
        market_id=market_id,
        mapping_id=mapping_id,
    )
