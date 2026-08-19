"""Authenticated, reconnecting Kalshi order-book WebSocket stream."""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Any

import websocket
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from hip4maker.books import (
    ONE,
    ZERO,
    BinarySide,
    BookError,
    CanonicalBook,
    TopOfBook,
    canonicalize_full_book,
)


KALSHI_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
KALSHI_WS_PATH = "/trade-api/ws/v2"


class KalshiStreamError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KalshiStreamEvent:
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class KalshiCredentials:
    api_key_id: str
    private_key: rsa.RSAPrivateKey

    @classmethod
    def from_file(cls, path: str | Path) -> "KalshiCredentials":
        credentials_path = Path(path).expanduser()
        try:
            payload = json.loads(credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KalshiStreamError(
                f"cannot load Kalshi credentials file {credentials_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise KalshiStreamError("Kalshi credentials JSON must be an object")
        api_key_id = payload.get("api_key_id")
        key_id = payload.get("key_id")
        private_key_path = payload.get("private_key_path")
        private_key_file = payload.get("private_key_file")
        if api_key_id is not None and key_id is not None and api_key_id != key_id:
            raise KalshiStreamError(
                "Kalshi credentials api_key_id and key_id values do not match"
            )
        api_key_id = api_key_id if api_key_id is not None else key_id
        if (
            private_key_path is not None
            and private_key_file is not None
            and private_key_path != private_key_file
        ):
            raise KalshiStreamError(
                "Kalshi credentials private_key_path and private_key_file values do not match"
            )
        private_key_path = (
            private_key_path if private_key_path is not None else private_key_file
        )
        if not isinstance(api_key_id, str) or not api_key_id:
            raise KalshiStreamError(
                "Kalshi credentials JSON must contain an api_key_id or key_id string"
            )
        if not isinstance(private_key_path, str) or not private_key_path:
            raise KalshiStreamError(
                "Kalshi credentials JSON must contain a private_key_path string"
            )
        key_path = Path(private_key_path).expanduser()
        if not key_path.is_absolute():
            key_path = credentials_path.parent / key_path
        try:
            loaded = serialization.load_pem_private_key(
                key_path.read_bytes(),
                password=None,
            )
        except (OSError, ValueError, TypeError) as exc:
            raise KalshiStreamError(
                f"cannot load Kalshi RSA private key {key_path}: {exc}"
            ) from exc
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise KalshiStreamError("Kalshi private key must be an RSA private key")
        return cls(api_key_id=api_key_id, private_key=loaded)

    def headers(self) -> list[str]:
        timestamp = str(time.time_ns() // 1_000_000)
        message = f"{timestamp}GET{KALSHI_WS_PATH}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return [
            f"KALSHI-ACCESS-KEY: {self.api_key_id}",
            f"KALSHI-ACCESS-SIGNATURE: {base64.b64encode(signature).decode('ascii')}",
            f"KALSHI-ACCESS-TIMESTAMP: {timestamp}",
        ]


class KalshiOrderbookStream:
    """Maintain one Kalshi book from a snapshot and sequenced deltas."""

    def __init__(
        self,
        market_ticker: str,
        credentials: KalshiCredentials,
        *,
        ws_url: str = KALSHI_WS_URL,
    ) -> None:
        self.market_ticker = market_ticker
        self.credentials = credentials
        self.ws_url = ws_url
        self._events: SimpleQueue[KalshiStreamEvent] = SimpleQueue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._yes_levels: dict[Decimal, Decimal] = {}
        self._no_levels: dict[Decimal, Decimal] = {}
        self._last_seq: int | None = None
        self._source_ts_ms = 0
        self._socket: websocket.WebSocketApp | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="hip4maker-kalshi-stream",
            daemon=True,
        )

    @classmethod
    def from_credentials_file(
        cls,
        market_ticker: str,
        credentials_file: str | Path,
    ) -> "KalshiOrderbookStream":
        return cls(
            market_ticker,
            KalshiCredentials.from_file(credentials_file),
        )

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def start(self) -> None:
        self._thread.start()

    def wait_ready(self, timeout_s: float) -> bool:
        return self._ready.wait(timeout_s)

    def drain(self) -> tuple[KalshiStreamEvent, ...]:
        values: list[KalshiStreamEvent] = []
        while True:
            try:
                values.append(self._events.get_nowait())
            except Empty:
                return tuple(values)

    def book(
        self,
        *,
        canonical_yes_source_side: BinarySide | str,
        mapping_id: str,
    ) -> CanonicalBook | None:
        if not self._ready.is_set():
            return None
        with self._lock:
            if not self._ready.is_set():
                return None
            best_bid_price = max(self._yes_levels) if self._yes_levels else ZERO
            best_ask_price = min(self._no_levels) if self._no_levels else ONE
            best_bid_size = self._yes_levels.get(best_bid_price, ZERO)
            best_ask_size = self._no_levels.get(best_ask_price, ZERO)
            now_ms = time.time_ns() // 1_000_000
            try:
                venue_yes_book = TopOfBook(
                    bid=best_bid_price,
                    ask=best_ask_price,
                    bid_size=best_bid_size,
                    ask_size=best_ask_size,
                    source_ts_ms=self._source_ts_ms,
                    received_ts_ms=now_ms,
                    source="kalshi_ws",
                    market_id=self.market_ticker,
                )
                return canonicalize_full_book(
                    venue_yes_book,
                    source_payout_side=canonical_yes_source_side,
                    mapping_id=mapping_id,
                )
            except BookError as exc:
                self._ready.clear()
                self._events.put(
                    KalshiStreamEvent(
                        "book_invalid",
                        {"market_ticker": self.market_ticker, "error": str(exc)},
                    )
                )
                return None

    def stop(self) -> None:
        self._stop.set()
        socket = self._socket
        if socket is not None:
            socket.keep_running = False
            try:
                socket.close()
            except Exception:
                pass
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        reconnect_delay_s = 1.0
        while not self._stop.is_set():
            self._clear_book()
            try:
                headers = self.credentials.headers()
            except Exception as exc:
                self._events.put(
                    KalshiStreamEvent(
                        "stream_error",
                        {"error": f"cannot sign connection: {type(exc).__name__}: {exc}"},
                    )
                )
                if self._stop.wait(reconnect_delay_s):
                    break
                reconnect_delay_s = min(10.0, reconnect_delay_s * 2)
                continue
            self._socket = websocket.WebSocketApp(
                self.ws_url,
                header=headers,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
            )
            # websocket-client adds a browser-style Origin by default. Kalshi's
            # authenticated Trade API rejects that handshake with HTTP 403.
            self._socket.run_forever(
                ping_interval=30,
                ping_timeout=10,
                suppress_origin=True,
            )
            had_snapshot = self._ready.is_set()
            self._clear_book()
            if self._stop.is_set():
                break
            self._events.put(
                KalshiStreamEvent(
                    "stream_status",
                    {"status": "disconnected", "market_ticker": self.market_ticker},
                )
            )
            reconnect_delay_s = 1.0 if had_snapshot else min(10.0, reconnect_delay_s * 2)
            if self._stop.wait(reconnect_delay_s):
                break

    def _on_open(self, socket: websocket.WebSocketApp) -> None:
        self._events.put(
            KalshiStreamEvent(
                "stream_status",
                {"status": "connected", "market_ticker": self.market_ticker},
            )
        )
        socket.send(
            json.dumps(
                {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_ticker": self.market_ticker,
                        "use_yes_price": True,
                    },
                },
                separators=(",", ":"),
            )
        )

    def _on_message(self, socket: websocket.WebSocketApp, raw: str) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._invalidate(socket, "received a non-JSON message")
            return
        if not isinstance(message, dict):
            return
        message_type = message.get("type")
        if message_type == "subscribed":
            msg = message.get("msg")
            channel = msg.get("channel") if isinstance(msg, dict) else None
            if channel == "orderbook_delta":
                self._events.put(
                    KalshiStreamEvent(
                        "subscription_ack",
                        {
                            "channel": channel,
                            "market_ticker": self.market_ticker,
                            "sid": msg.get("sid"),
                        },
                    )
                )
            return
        if message_type == "error":
            self._invalidate(socket, f"subscription error: {message.get('msg')}")
            return
        if message_type == "orderbook_snapshot":
            self._apply_snapshot(socket, message)
        elif message_type == "orderbook_delta":
            self._apply_delta(socket, message)

    def _apply_snapshot(
        self, socket: websocket.WebSocketApp, message: dict[str, Any]
    ) -> None:
        msg = message.get("msg")
        try:
            seq = _sequence_number(message.get("seq"))
        except KalshiStreamError as exc:
            self._invalidate(socket, str(exc))
            return
        if not isinstance(msg, dict) or msg.get("market_ticker") != self.market_ticker:
            self._invalidate(socket, "snapshot market ticker did not match subscription")
            return
        try:
            yes_levels = _snapshot_levels(msg.get("yes_dollars_fp"), "yes")
            no_levels = _snapshot_levels(msg.get("no_dollars_fp"), "no")
        except KalshiStreamError as exc:
            self._invalidate(socket, str(exc))
            return
        received_ms = time.time_ns() // 1_000_000
        with self._lock:
            self._yes_levels = yes_levels
            self._no_levels = no_levels
            self._last_seq = seq
            self._source_ts_ms = received_ms
        self._ready.set()
        self._events.put(
            KalshiStreamEvent(
                "snapshot",
                {
                    "market_ticker": self.market_ticker,
                    "seq": seq,
                    "yes_levels": len(yes_levels),
                    "no_levels": len(no_levels),
                    "ready": True,
                },
            )
        )

    def _apply_delta(
        self, socket: websocket.WebSocketApp, message: dict[str, Any]
    ) -> None:
        msg = message.get("msg")
        try:
            seq = _sequence_number(message.get("seq"))
        except KalshiStreamError as exc:
            self._invalidate(socket, str(exc))
            return
        if not isinstance(msg, dict) or msg.get("market_ticker") != self.market_ticker:
            self._invalidate(socket, "delta market ticker did not match subscription")
            return
        with self._lock:
            expected = None if self._last_seq is None else self._last_seq + 1
        if expected is None or seq != expected:
            self._invalidate(
                socket,
                f"order-book sequence gap: expected {expected}, received {seq}",
            )
            return
        side = msg.get("side", msg.get("outcome_side"))
        try:
            price = _decimal(msg.get("price_dollars"), "delta price")
            delta = _decimal(msg.get("delta_fp"), "delta size")
            if side not in {"yes", "no"}:
                raise KalshiStreamError("delta side must be yes or no")
            if not ZERO <= price <= ONE:
                raise KalshiStreamError("delta price must be within [0, 1]")
        except KalshiStreamError as exc:
            self._invalidate(socket, str(exc))
            return
        with self._lock:
            levels = self._yes_levels if side == "yes" else self._no_levels
            updated = levels.get(price, ZERO) + delta
            if updated < ZERO:
                invalid = True
            else:
                invalid = False
                if updated == ZERO:
                    levels.pop(price, None)
                else:
                    levels[price] = updated
                self._last_seq = seq
                source_ts = msg.get("ts_ms")
                self._source_ts_ms = (
                    source_ts
                    if isinstance(source_ts, int) and not isinstance(source_ts, bool)
                    else time.time_ns() // 1_000_000
                )
        if invalid:
            self._invalidate(socket, "delta reduced a price level below zero")
            return
        self._ready.set()

    def _invalidate(self, socket: websocket.WebSocketApp, reason: str) -> None:
        self._events.put(
            KalshiStreamEvent(
                "stream_error",
                {"market_ticker": self.market_ticker, "error": reason},
            )
        )
        self._clear_book()
        socket.keep_running = False
        try:
            socket.close()
        except Exception:
            pass

    def _clear_book(self) -> None:
        self._ready.clear()
        with self._lock:
            self._yes_levels = {}
            self._no_levels = {}
            self._last_seq = None
            self._source_ts_ms = 0

    def _on_error(self, _socket: websocket.WebSocketApp, error: Any) -> None:
        if not self._stop.is_set():
            self._events.put(
                KalshiStreamEvent("stream_error", {"error": str(error)})
            )


def _snapshot_levels(value: Any, side: str) -> dict[Decimal, Decimal]:
    if not isinstance(value, list):
        raise KalshiStreamError(f"snapshot {side}_dollars_fp must be an array")
    result: dict[Decimal, Decimal] = {}
    for index, level in enumerate(value):
        if not isinstance(level, list) or len(level) < 2:
            raise KalshiStreamError(f"snapshot {side} level {index} is invalid")
        price = _decimal(level[0], f"snapshot {side} price")
        size = _decimal(level[1], f"snapshot {side} size")
        if not ZERO <= price <= ONE:
            raise KalshiStreamError(f"snapshot {side} price must be within [0, 1]")
        if size < ZERO:
            raise KalshiStreamError(f"snapshot {side} size cannot be negative")
        if size > ZERO:
            result[price] = size
    return result


def _sequence_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KalshiStreamError("WebSocket message sequence must be a nonnegative integer")
    return value


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise KalshiStreamError(f"{field} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise KalshiStreamError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise KalshiStreamError(f"{field} must be finite")
    return result
