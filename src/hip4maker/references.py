"""Combine independently normalized reference books."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from hip4maker.books import CanonicalBook


class ReferenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WeightedReference:
    book: CanonicalBook
    weight: Decimal

    def __post_init__(self) -> None:
        if self.weight <= 0:
            raise ReferenceError("reference weight must be positive")


@dataclass(frozen=True, slots=True)
class CompositeReference:
    book: CanonicalBook
    sources: tuple[str, ...]


def combine_weighted(references: Iterable[WeightedReference]) -> CompositeReference:
    values = tuple(references)
    if not values:
        raise ReferenceError("at least one healthy reference is required")
    mapping_ids = {value.book.mapping_id for value in values}
    if len(mapping_ids) != 1:
        raise ReferenceError("reference books do not share one mapping identity")
    total_weight = sum((value.weight for value in values), Decimal(0))
    bid = sum((value.book.bid * value.weight for value in values), Decimal(0)) / total_weight
    ask = sum((value.book.ask * value.weight for value in values), Decimal(0)) / total_weight
    bid_size = sum((value.book.bid_size * value.weight for value in values), Decimal(0)) / total_weight
    ask_size = sum((value.book.ask_size * value.weight for value in values), Decimal(0)) / total_weight
    sources = tuple(value.book.source for value in values)
    composite = CanonicalBook(
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        source_ts_ms=min(value.book.source_ts_ms for value in values),
        received_ts_ms=max(value.book.received_ts_ms for value in values),
        source="composite:" + "+".join(sources),
        market_id="+".join(value.book.market_id for value in values),
        mapping_id=values[0].book.mapping_id,
    )
    return CompositeReference(book=composite, sources=sources)
