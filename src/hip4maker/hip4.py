"""HIP-4 identifier helpers.

Outcome identifiers deliberately stay separate from ordinary Hyperliquid spot
and perp identifiers.
"""

from __future__ import annotations


OUTCOME_ASSET_OFFSET = 100_000_000


def outcome_encoding(outcome_id: int, side: int) -> int:
    if isinstance(outcome_id, bool) or not isinstance(outcome_id, int):
        raise TypeError("outcome_id must be an integer")
    if outcome_id < 0:
        raise ValueError("outcome_id must be nonnegative")
    if isinstance(side, bool) or not isinstance(side, int):
        raise TypeError("side must be integer 0 or 1")
    if side not in (0, 1):
        raise ValueError("HIP-4 side must be 0 or 1")
    return 10 * outcome_id + side


def outcome_coin(outcome_id: int, side: int) -> str:
    return f"#{outcome_encoding(outcome_id, side)}"


def outcome_token(outcome_id: int, side: int) -> str:
    return f"+{outcome_encoding(outcome_id, side)}"


def outcome_asset_id(outcome_id: int, side: int) -> int:
    return OUTCOME_ASSET_OFFSET + outcome_encoding(outcome_id, side)

