"""Reconnectable, read-only Hyperliquid account WebSocket subscriptions."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from queue import Empty, SimpleQueue
from typing import Any

import websocket


@dataclass(frozen=True, slots=True)
class AccountStreamEvent:
    kind: str
    payload: Any


class HyperliquidAccountStream:
    """Subscribe to order updates and fills without any request-posting path."""

    REQUIRED_SUBSCRIPTIONS = frozenset({"orderUpdates", "userFills"})
    HEARTBEAT_INTERVAL_S = 50.0

    def __init__(self, base_url: str, user: str) -> None:
        self.ws_url = "ws" + base_url.removesuffix("/")[len("http") :] + "/ws"
        self.user = user
        self._events: SimpleQueue[AccountStreamEvent] = SimpleQueue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._subscriptions: set[str] = set()
        self._socket: websocket.WebSocketApp | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="hip4maker-account-stream",
            daemon=True,
        )

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def start(self) -> None:
        self._thread.start()

    def wait_ready(self, timeout_s: float) -> bool:
        return self._ready.wait(timeout_s)

    def drain(self) -> tuple[AccountStreamEvent, ...]:
        values: list[AccountStreamEvent] = []
        while True:
            try:
                values.append(self._events.get_nowait())
            except Empty:
                return tuple(values)

    def stop(self) -> None:
        self._stop.set()
        socket = self._socket
        if socket is not None:
            socket.keep_running = False
            if socket.sock is not None:
                socket.sock.shutdown()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        reconnect_delay_s = 1.0
        while not self._stop.is_set():
            self._ready.clear()
            with self._lock:
                self._subscriptions.clear()
            self._socket = websocket.WebSocketApp(
                self.ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
            )
            connection_done = threading.Event()
            heartbeat = threading.Thread(
                target=self._send_heartbeats,
                args=(self._socket, connection_done),
                name="hip4maker-account-heartbeat",
                daemon=True,
            )
            heartbeat.start()
            self._socket.run_forever()
            connection_done.set()
            heartbeat.join(timeout=5)
            self._ready.clear()
            if self._stop.is_set():
                break
            self._events.put(
                AccountStreamEvent("stream_status", {"status": "disconnected"})
            )
            if self._stop.wait(reconnect_delay_s):
                break
            reconnect_delay_s = min(10.0, reconnect_delay_s * 2)

    def _send_heartbeats(
        self,
        socket: websocket.WebSocketApp,
        connection_done: threading.Event,
    ) -> None:
        while not connection_done.wait(self.HEARTBEAT_INTERVAL_S):
            if self._stop.is_set() or not socket.keep_running:
                return
            try:
                socket.send(json.dumps({"method": "ping"}, separators=(",", ":")))
            except Exception as exc:  # websocket-client exposes several send errors
                if not self._stop.is_set():
                    self._events.put(
                        AccountStreamEvent(
                            "stream_error",
                            {"error": f"heartbeat send failed: {exc}"},
                        )
                    )
                socket.keep_running = False
                return

    def _on_open(self, socket: websocket.WebSocketApp) -> None:
        self._events.put(AccountStreamEvent("stream_status", {"status": "connected"}))
        for subscription_type in sorted(self.REQUIRED_SUBSCRIPTIONS):
            socket.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "subscription": {
                            "type": subscription_type,
                            "user": self.user,
                        },
                    },
                    separators=(",", ":"),
                )
            )

    def _on_message(self, _socket: websocket.WebSocketApp, raw: str) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._events.put(
                AccountStreamEvent("stream_error", {"error": "non-JSON message"})
            )
            return
        if not isinstance(message, dict):
            return
        channel = message.get("channel")
        if channel == "pong":
            return
        if channel == "subscriptionResponse":
            data = message.get("data")
            subscription = data.get("subscription") if isinstance(data, dict) else None
            subscription_type = (
                subscription.get("type") if isinstance(subscription, dict) else None
            )
            if subscription_type in self.REQUIRED_SUBSCRIPTIONS:
                with self._lock:
                    self._subscriptions.add(subscription_type)
                    ready = self._subscriptions == set(self.REQUIRED_SUBSCRIPTIONS)
                self._events.put(
                    AccountStreamEvent(
                        "subscription_ack",
                        {"subscription": subscription_type},
                    )
                )
                if ready:
                    self._ready.set()
            return
        if channel == "orderUpdates":
            self._events.put(AccountStreamEvent("order_updates", message.get("data")))
        elif channel == "userFills":
            self._events.put(AccountStreamEvent("user_fills", message.get("data")))

    def _on_error(self, _socket: websocket.WebSocketApp, error: Any) -> None:
        if not self._stop.is_set():
            self._events.put(
                AccountStreamEvent("stream_error", {"error": str(error)})
            )
