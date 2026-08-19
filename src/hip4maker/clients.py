"""Venue clients exposing read-only market and account data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from hip4maker.adapters.hyperliquid import parse_l2_book
from hip4maker.adapters.kalshi import parse_orderbook as parse_kalshi_orderbook
from hip4maker.adapters.polymarket import parse_orderbook as parse_polymarket_orderbook
from hip4maker.books import BinarySide, CanonicalBook
from hip4maker.transport import JsonResponse, ReadOnlyHttpTransport, TransportError


KALSHI_REST_URL = "https://external-api.kalshi.com/trade-api/v2"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"


class ReadOnlyTransport(Protocol):
    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | bool] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonResponse: ...

    def post_hyperliquid_info(
        self, base_url: str, payload: Mapping[str, Any]
    ) -> JsonResponse: ...


@dataclass(frozen=True, slots=True)
class HyperliquidInfoClient:
    base_url: str
    transport: ReadOnlyTransport

    @classmethod
    def create(
        cls, base_url: str, transport: ReadOnlyTransport | None = None
    ) -> "HyperliquidInfoClient":
        return cls(base_url=base_url.rstrip("/"), transport=transport or ReadOnlyHttpTransport())

    def outcome_meta(self) -> JsonResponse:
        return self.transport.post_hyperliquid_info(self.base_url, {"type": "outcomeMeta"})

    def l2_book(
        self,
        coin: str,
        *,
        queried_side_payout: BinarySide | str,
        mapping_id: str,
    ) -> CanonicalBook:
        response = self.transport.post_hyperliquid_info(
            self.base_url, {"type": "l2Book", "coin": coin}
        )
        if not isinstance(response.payload, dict):
            raise TransportError("Hyperliquid l2Book response must be an object")
        return parse_l2_book(
            response.payload,
            queried_side_payout=queried_side_payout,
            mapping_id=mapping_id,
            received_ts_ms=response.received_ts_ms,
        )

    def spot_state(self, address: str) -> JsonResponse:
        return self.transport.post_hyperliquid_info(
            self.base_url,
            {"type": "spotClearinghouseState", "user": address},
        )

    def open_orders(self, address: str) -> JsonResponse:
        return self.transport.post_hyperliquid_info(
            self.base_url,
            {"type": "openOrders", "user": address},
        )


@dataclass(frozen=True, slots=True)
class KalshiReadOnlyClient:
    rest_url: str
    transport: ReadOnlyTransport

    @classmethod
    def create(
        cls, rest_url: str, transport: ReadOnlyTransport | None = None
    ) -> "KalshiReadOnlyClient":
        return cls(rest_url=rest_url.rstrip("/"), transport=transport or ReadOnlyHttpTransport())

    def event(self, event_ticker: str) -> JsonResponse:
        return self.transport.get_json(
            f"{self.rest_url}/events/{event_ticker}",
            params={"with_nested_markets": "true"},
        )

    def market(self, market_ticker: str) -> JsonResponse:
        return self.transport.get_json(f"{self.rest_url}/markets/{market_ticker}")

    def orderbook(
        self,
        market_ticker: str,
        *,
        canonical_yes_source_side: BinarySide | str,
        mapping_id: str,
    ) -> CanonicalBook:
        response = self.transport.get_json(
            f"{self.rest_url}/markets/{market_ticker}/orderbook"
        )
        if not isinstance(response.payload, dict):
            raise TransportError("Kalshi orderbook response must be an object")
        return parse_kalshi_orderbook(
            response.payload,
            canonical_yes_source_side=canonical_yes_source_side,
            market_ticker=market_ticker,
            mapping_id=mapping_id,
            source_ts_ms=response.received_ts_ms,
            received_ts_ms=response.received_ts_ms,
        )


@dataclass(frozen=True, slots=True)
class PolymarketReadOnlyClient:
    gamma_url: str
    clob_url: str
    transport: ReadOnlyTransport

    @classmethod
    def create(
        cls,
        gamma_url: str,
        clob_url: str,
        transport: ReadOnlyTransport | None = None,
    ) -> "PolymarketReadOnlyClient":
        selected = transport or ReadOnlyHttpTransport()
        return cls(
            gamma_url=gamma_url.rstrip("/"),
            clob_url=clob_url.rstrip("/"),
            transport=selected,
        )

    def event(self, event_slug: str) -> JsonResponse:
        return self.transport.get_json(
            f"{self.gamma_url}/events",
            params={"slug": event_slug},
        )

    def market_by_slug(self, market_slug: str) -> JsonResponse:
        return self.transport.get_json(
            f"{self.gamma_url}/markets", params={"slug": market_slug}
        )

    def orderbook(
        self,
        token_id: str,
        *,
        token_payout_side: BinarySide | str,
        mapping_id: str,
    ) -> CanonicalBook:
        response = self.transport.get_json(
            f"{self.clob_url}/book",
            params={"token_id": token_id},
        )
        if not isinstance(response.payload, dict):
            raise TransportError("Polymarket orderbook response must be an object")
        return parse_polymarket_orderbook(
            response.payload,
            token_payout_side=token_payout_side,
            mapping_id=mapping_id,
            received_ts_ms=response.received_ts_ms,
        )
