"""Inventory and terminal-payout checks for fixed HIP-4 inventory."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from hip4maker.account import InventoryState


@dataclass(frozen=True, slots=True)
class ProposedOrder:
    is_buy: bool
    price: Decimal
    size: Decimal
    token_side: str = "yes"

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.price <= Decimal(1):
            raise ValueError("order price must be within [0, 1]")
        if self.size <= 0:
            raise ValueError("order size must be positive")
        if self.token_side not in {"yes", "no"}:
            raise ValueError("order token_side must be yes or no")

    @property
    def notional(self) -> Decimal:
        return self.price * self.size

    @property
    def directional_delta(self) -> Decimal:
        token_delta = self.size if self.is_buy else -self.size
        return token_delta if self.token_side == "yes" else -token_delta


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_position: Decimal


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    accepted: bool
    reasons: tuple[str, ...]
    terminal_if_yes: Decimal
    terminal_if_no: Decimal
    resulting_yes: Decimal
    resulting_no: Decimal
    order_notional: Decimal
    worst_case_loss: Decimal


def assess_orders(
    inventory: InventoryState,
    orders: Iterable[ProposedOrder],
    limits: RiskLimits,
) -> RiskAssessment:
    proposed = tuple(orders)
    buy_cost = sum((order.notional for order in proposed if order.is_buy), Decimal(0))
    yes_sold = sum(
        (
            order.size
            for order in proposed
            if order.token_side == "yes" and not order.is_buy
        ),
        Decimal(0),
    )
    no_sold = sum(
        (
            order.size
            for order in proposed
            if order.token_side == "no" and not order.is_buy
        ),
        Decimal(0),
    )
    resulting_yes, resulting_no, terminal_quote = _apply_orders(
        inventory, proposed
    )
    terminal_if_yes = terminal_quote + resulting_yes
    terminal_if_no = terminal_quote + resulting_no
    reasons: list[str] = []

    if yes_sold > inventory.yes.available:
        reasons.append("insufficient available canonical-Yes shares")
    if no_sold > inventory.no.available:
        reasons.append("insufficient available canonical-No shares")
    if buy_cost > inventory.quote.available:
        reasons.append("insufficient available quote tokens for native buys")
    if resulting_yes < 0:
        reasons.append("orders would produce a negative canonical-Yes balance")
    if resulting_no < 0:
        reasons.append("orders would produce a negative canonical-No balance")

    base_terminal_yes = inventory.quote.total + inventory.yes.total
    base_terminal_no = inventory.quote.total + inventory.no.total
    scenario_values: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    positive_orders = tuple(order for order in proposed if order.directional_delta > 0)
    negative_orders = tuple(order for order in proposed if order.directional_delta < 0)
    for fill_positive in (False, True):
        for fill_negative in (False, True):
            selected = (
                (positive_orders if fill_positive else ())
                + (negative_orders if fill_negative else ())
            )
            scenario_yes, scenario_no, scenario_quote = _apply_orders(
                inventory, selected
            )
            scenario_values.append(
                (
                    scenario_yes,
                    scenario_no,
                    scenario_quote + scenario_yes,
                    scenario_quote + scenario_no,
                )
            )

    if any(
        abs(scenario_yes - scenario_no) > limits.max_position
        for scenario_yes, scenario_no, _, _ in scenario_values
    ):
        reasons.append("a fill scenario exceeds max_position")
    worst_case_loss = max(
        Decimal(0),
        *(
            max(base_terminal_yes - terminal_yes, base_terminal_no - terminal_no)
            for _, _, terminal_yes, terminal_no in scenario_values
        ),
    )

    return RiskAssessment(
        accepted=not reasons,
        reasons=tuple(reasons),
        terminal_if_yes=terminal_if_yes,
        terminal_if_no=terminal_if_no,
        resulting_yes=resulting_yes,
        resulting_no=resulting_no,
        order_notional=sum((order.notional for order in proposed), Decimal(0)),
        worst_case_loss=worst_case_loss,
    )


def _apply_orders(
    inventory: InventoryState, orders: Iterable[ProposedOrder]
) -> tuple[Decimal, Decimal, Decimal]:
    yes = inventory.yes.total
    no = inventory.no.total
    quote = inventory.quote.total
    for order in orders:
        token_delta = order.size if order.is_buy else -order.size
        quote_delta = -order.notional if order.is_buy else order.notional
        if order.token_side == "yes":
            yes += token_delta
        else:
            no += token_delta
        quote += quote_delta
    return yes, no, quote


def assess_split(
    inventory: InventoryState,
    amount: Decimal,
    *,
    min_free_quote: Decimal,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if amount <= 0:
        reasons.append("split amount must be positive")
    if inventory.quote.available - amount < min_free_quote:
        reasons.append("split would violate min_free_quote")
    return tuple(reasons)
