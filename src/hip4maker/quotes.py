"""Generate canonical quote ladders and route them onto HIP-4 side tokens."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from hip4maker.account import InventoryState
from hip4maker.books import ONE, ZERO
from hip4maker.constraints import (
    MAX_PRICE,
    MIN_ORDER_NOTIONAL,
    MIN_PRICE,
    valid_order_notional,
)
from hip4maker.risk import (
    ProposedOrder,
    RiskAssessment,
    RiskLimits,
    assess_orders,
)


class QuoteError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QuoteParameters:
    place_threshold: Decimal
    order_size: Decimal
    max_back_levels: int
    rung_threshold_multiplier: Decimal


@dataclass(frozen=True, slots=True)
class QuoteLeg:
    level: int
    effective_side: str
    effective_price: Decimal
    token_side: str
    native_price: Decimal
    is_buy: bool
    size: Decimal

    def __post_init__(self) -> None:
        if not self.effective_price.is_finite():
            raise QuoteError("quote price must be finite")
        if not ZERO <= self.effective_price <= ONE:
            raise QuoteError(
                f"quote price must be within [0, 1], got {self.effective_price}"
            )
        if self.effective_side not in {"bid", "ask"}:
            raise QuoteError("effective side must be bid or ask")
        if self.token_side not in {"yes", "no"}:
            raise QuoteError("token side must be yes or no")
        expected_native_price = (
            self.effective_price if self.token_side == "yes" else ONE - self.effective_price
        )
        if self.native_price != expected_native_price:
            raise QuoteError("native price does not match the canonical effective price")
        expected_buy = (self.effective_side == "bid") == (self.token_side == "yes")
        if self.is_buy != expected_buy:
            raise QuoteError("native side does not match the canonical effective side")
        if not valid_order_notional(self.native_price, self.size):
            raise QuoteError(
                f"native order notional must be at least {MIN_ORDER_NOTIONAL}"
            )

    @property
    def notional(self) -> Decimal:
        return self.native_price * self.size

    def as_order(self) -> ProposedOrder:
        return ProposedOrder(
            is_buy=self.is_buy,
            price=self.native_price,
            size=self.size,
            token_side=self.token_side,
        )


@dataclass(frozen=True, slots=True)
class QuotePlan:
    fair_value: Decimal
    reservation_price: Decimal
    legs: tuple[QuoteLeg, ...]
    risk: RiskAssessment
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.reasons and self.risk.accepted


class QuoteEngine:
    def __init__(self, parameters: QuoteParameters, risk_limits: RiskLimits) -> None:
        self.parameters = parameters
        self.risk_limits = risk_limits

    def plan(self, fair_value: Decimal, inventory: InventoryState) -> QuotePlan:
        if not ZERO <= fair_value <= ONE:
            raise QuoteError("fair value must be within [0, 1]")

        # Half a front-rung threshold per order of inventory imbalance. This
        # makes the inventory-reducing side more aggressive without creating
        # another configuration parameter.
        size_units = inventory.directional_shares / self.parameters.order_size
        skew = size_units * Decimal("0.5") * self.parameters.place_threshold
        reservation = max(ZERO, min(ONE, fair_value - skew))

        position = inventory.directional_shares
        buy_capacity = max(Decimal(0), self.risk_limits.max_position - position)
        sell_capacity = max(Decimal(0), self.risk_limits.max_position + position)
        bought = Decimal(0)
        sold = Decimal(0)
        remaining_quote = inventory.quote.available
        remaining_yes = inventory.yes.available
        remaining_no = inventory.no.available
        legs: list[QuoteLeg] = []
        seen_bid_prices: set[Decimal] = set()
        seen_ask_prices: set[Decimal] = set()

        for level in range(self.parameters.max_back_levels + 1):
            distance = self.parameters.place_threshold * (
                ONE + Decimal(level) * self.parameters.rung_threshold_multiplier
            )
            raw_bid = reservation - distance
            raw_ask = reservation + distance

            effective_bid = _round_probability(raw_bid, ROUND_DOWN)
            if effective_bid not in seen_bid_prices:
                seen_bid_prices.add(effective_bid)
                bid = _route_leg(
                    level=level,
                    effective_side="bid",
                    effective_price=effective_bid,
                    size=self.parameters.order_size,
                )
                if bid is not None and bought + bid.size <= buy_capacity:
                    funded, remaining_quote, remaining_yes, remaining_no = _fund_leg(
                        bid,
                        remaining_quote=remaining_quote,
                        remaining_yes=remaining_yes,
                        remaining_no=remaining_no,
                    )
                    if funded:
                        legs.append(bid)
                        bought += bid.size

            effective_ask = _round_probability(raw_ask, ROUND_UP)
            if (
                effective_ask not in seen_ask_prices
                and sold + self.parameters.order_size <= sell_capacity
            ):
                seen_ask_prices.add(effective_ask)
                ask = _route_leg(
                    level=level,
                    effective_side="ask",
                    effective_price=effective_ask,
                    size=self.parameters.order_size,
                )
                if ask is not None:
                    funded, remaining_quote, remaining_yes, remaining_no = _fund_leg(
                        ask,
                        remaining_quote=remaining_quote,
                        remaining_yes=remaining_yes,
                        remaining_no=remaining_no,
                    )
                    if funded:
                        legs.append(ask)
                        sold += ask.size

        reasons: list[str] = []
        if not legs:
            reasons.append("no funded quote fits within max_position")
        risk = assess_orders(
            inventory,
            (leg.as_order() for leg in legs),
            self.risk_limits,
        )
        return QuotePlan(
            fair_value=fair_value,
            reservation_price=reservation,
            legs=tuple(legs),
            risk=risk,
            reasons=tuple(reasons),
        )


def _round_probability(value: Decimal, rounding: str) -> Decimal:
    """Bound and round to at most five significant and eight decimal places."""

    if value <= MIN_PRICE:
        return MIN_PRICE
    if value >= MAX_PRICE:
        return MAX_PRICE
    exponent = max(value.adjusted() - 4, -8)
    rounded = value.quantize(Decimal(1).scaleb(exponent), rounding=rounding)
    return max(MIN_PRICE, min(MAX_PRICE, rounded))


def _route_leg(
    *, level: int, effective_side: str, effective_price: Decimal, size: Decimal
) -> QuoteLeg | None:
    direct_notional = effective_price * size
    complement_notional = (ONE - effective_price) * size
    if direct_notional >= MIN_ORDER_NOTIONAL:
        token_side = "yes"
    elif complement_notional >= MIN_ORDER_NOTIONAL:
        token_side = "no"
    else:
        return None
    native_price = effective_price if token_side == "yes" else ONE - effective_price
    is_buy = (effective_side == "bid") == (token_side == "yes")
    return QuoteLeg(
        level=level,
        effective_side=effective_side,
        effective_price=effective_price,
        token_side=token_side,
        native_price=native_price,
        is_buy=is_buy,
        size=size,
    )


def _fund_leg(
    leg: QuoteLeg,
    *,
    remaining_quote: Decimal,
    remaining_yes: Decimal,
    remaining_no: Decimal,
) -> tuple[bool, Decimal, Decimal, Decimal]:
    if leg.is_buy:
        if leg.notional > remaining_quote:
            return False, remaining_quote, remaining_yes, remaining_no
        return True, remaining_quote - leg.notional, remaining_yes, remaining_no
    if leg.token_side == "yes":
        if leg.size > remaining_yes:
            return False, remaining_quote, remaining_yes, remaining_no
        return True, remaining_quote, remaining_yes - leg.size, remaining_no
    if leg.size > remaining_no:
        return False, remaining_quote, remaining_yes, remaining_no
    return True, remaining_quote, remaining_yes, remaining_no - leg.size
