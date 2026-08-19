"""Small read-only JSON transport.

This module intentionally has no generic request method. Hyperliquid POSTs are
restricted to the read-only `/info` endpoint; `/exchange` cannot be addressed
through this transport.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class TransportError(RuntimeError):
    pass


READ_ONLY_HYPERLIQUID_INFO_TYPES = {
    "outcomeMeta",
    "l2Book",
    "spotClearinghouseState",
    "openOrders",
    "frontendOpenOrders",
}


@dataclass(frozen=True, slots=True)
class JsonResponse:
    payload: Any
    received_ts_ms: int


class ReadOnlyHttpTransport:
    def __init__(self, *, timeout_s: float = 10.0, user_agent: str = "hip4maker/0.1") -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.timeout_s = timeout_s
        self.user_agent = user_agent

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | bool] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonResponse:
        self._validate_http_url(url)
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"
        request_headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)
        request = Request(url, method="GET", headers=request_headers)
        return self._send(request)

    def post_hyperliquid_info(self, base_url: str, payload: Mapping[str, Any]) -> JsonResponse:
        self._validate_http_url(base_url)
        info_type = payload.get("type")
        if info_type not in READ_ONLY_HYPERLIQUID_INFO_TYPES:
            raise TransportError(f"Hyperliquid info type {info_type!r} is not allowlisted")
        url = f"{base_url.rstrip('/')}/info"
        if urlparse(url).path != "/info":
            raise TransportError("Hyperliquid read-only transport can only address /info")
        body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        request = Request(
            url,
            method="POST",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        return self._send(request)

    def _send(self, request: Request) -> JsonResponse:
        import time

        try:
            with urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310 - URLs validated
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read(512).decode("utf-8", errors="replace")
            raise TransportError(f"HTTP {exc.code} for {request.full_url}: {detail}") from exc
        except URLError as exc:
            raise TransportError(f"request failed for {request.full_url}: {exc.reason}") from exc
        received_ts_ms = time.time_ns() // 1_000_000
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TransportError(f"non-JSON response from {request.full_url}") from exc
        return JsonResponse(payload=payload, received_ts_ms=received_ts_ms)

    @staticmethod
    def _validate_http_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise TransportError("read-only network URLs must be absolute HTTPS URLs")
        if parsed.username or parsed.password:
            raise TransportError("credentials must not be embedded in URLs")

