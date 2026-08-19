"""Read-only HIP-4 inventory and open-order reconciliation."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hip4maker.books import decimal_value
from hip4maker.hip4 import outcome_token


class AccountStateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TokenBalance:
    total: Decimal = Decimal(0)
    hold: Decimal = Decimal(0)

    @property
    def available(self) -> Decimal:
        return max(Decimal(0), self.total - self.hold)


@dataclass(frozen=True, slots=True)
class InventoryState:
    quote: TokenBalance
    yes: TokenBalance
    no: TokenBalance

    @property
    def directional_shares(self) -> Decimal:
        return self.yes.total - self.no.total

    @property
    def complete_sets(self) -> Decimal:
        return min(self.yes.total, self.no.total)

    @classmethod
    def empty(cls) -> "InventoryState":
        empty = TokenBalance()
        return cls(quote=empty, yes=empty, no=empty)


@dataclass(frozen=True, slots=True)
class OpenOrder:
    coin: str
    oid: int
    side: str
    price: Decimal
    size: Decimal
    cloid: str | None


def parse_spot_inventory(
    payload: Mapping[str, Any],
    *,
    outcome_id: int,
    canonical_yes_side_index: int,
    canonical_no_side_index: int,
    quote_token: str,
) -> InventoryState:
    balances = payload.get("balances")
    if not isinstance(balances, list):
        raise AccountStateError("spotClearinghouseState.balances must be an array")
    by_coin: dict[str, TokenBalance] = {}
    for index, value in enumerate(balances):
        if not isinstance(value, dict):
            raise AccountStateError(f"balance {index} must be an object")
        coin = value.get("coin")
        if not isinstance(coin, str) or not coin:
            raise AccountStateError(f"balance {index} coin must be a string")
        total = decimal_value(value.get("total", "0"), f"balance {coin} total")
        hold = decimal_value(value.get("hold", "0"), f"balance {coin} hold")
        if total < 0 or hold < 0:
            raise AccountStateError(f"balance {coin} cannot be negative")
        by_coin[coin] = TokenBalance(total=total, hold=hold)
    return InventoryState(
        quote=by_coin.get(quote_token, TokenBalance()),
        yes=by_coin.get(outcome_token(outcome_id, canonical_yes_side_index), TokenBalance()),
        no=by_coin.get(outcome_token(outcome_id, canonical_no_side_index), TokenBalance()),
    )


def parse_open_orders(
    payload: Any, *, managed_client_order_ids: Collection[str]
) -> tuple[OpenOrder, ...]:
    """Parse only orders whose CLOIDs belong to the selected market mapping."""

    if not isinstance(payload, list):
        raise AccountStateError("openOrders response must be an array")
    managed_cloids = frozenset(managed_client_order_ids)
    result: list[OpenOrder] = []
    for index, value in enumerate(payload):
        if not isinstance(value, dict):
            raise AccountStateError(f"open order {index} must be an object")
        cloid_value = value.get("cloid")
        cloid = cloid_value if isinstance(cloid_value, str) else None
        if cloid is None or cloid not in managed_cloids:
            continue
        oid = value.get("oid")
        if isinstance(oid, bool) or not isinstance(oid, int):
            raise AccountStateError(f"open order {index} oid must be an integer")
        coin = value.get("coin")
        side = value.get("side")
        if not isinstance(coin, str) or side not in {"A", "B"}:
            raise AccountStateError(f"open order {index} has invalid coin or side")
        result.append(
            OpenOrder(
                coin=coin,
                oid=oid,
                side=side,
                price=decimal_value(value.get("limitPx"), "open order price"),
                size=decimal_value(value.get("sz"), "open order size"),
                cloid=cloid,
            )
        )
    return tuple(result)
