"""Protocol-level constraints shared by planning and execution."""

from __future__ import annotations

from decimal import Decimal


MIN_ORDER_NOTIONAL = Decimal("10")
# HIP-4 probabilities are mathematically bounded by 0 and 1, but Hyperliquid
# rejects an order at exactly 1.  One five-significant-figure tick inward at
# either probability boundary keeps both direct and complement routes valid.
MIN_PRICE = Decimal("0.00001")
MAX_PRICE = Decimal("0.99999")


def valid_order_price(price: Decimal) -> bool:
    return price.is_finite() and MIN_PRICE <= price <= MAX_PRICE


def valid_order_notional(price: Decimal, size: Decimal) -> bool:
    return (
        valid_order_price(price)
        and size.is_finite()
        and size > 0
        and price * size >= MIN_ORDER_NOTIONAL
    )
