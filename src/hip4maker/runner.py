"""HIP-4 market-maker orchestration for dry-run, testnet, and mainnet."""

from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from hip4maker.account import (
    InventoryState,
    OpenOrder,
    TokenBalance,
    parse_open_orders,
    parse_spot_inventory,
)
from hip4maker.actions import (
    ActionIntent,
    ActionKind,
    ActionSubmissionError,
    DryRunActionSink,
    HyperliquidActionSink,
    implied_account_address_from_environment,
)
from hip4maker.account_stream import AccountStreamEvent, HyperliquidAccountStream
from hip4maker.basis import BasisEstimator, SampleStatus
from hip4maker.books import CanonicalBook, ONE, shift_canonical_book
from hip4maker.clients import (
    KALSHI_REST_URL,
    POLYMARKET_CLOB_URL,
    POLYMARKET_GAMMA_URL,
    HyperliquidInfoClient,
    KalshiReadOnlyClient,
    PolymarketReadOnlyClient,
    ReadOnlyTransport,
)
from hip4maker.config import basis_parameters_from_config, validate_config
from hip4maker.hip4 import outcome_asset_id, outcome_coin
from hip4maker.metadata import (
    VerifiedHip4Contract,
    discover_kalshi,
    verify_hip4_contract,
    verify_kalshi_reference,
    verify_polymarket_reference,
)
from hip4maker.kalshi_stream import (
    KalshiOrderbookStream,
    KalshiStreamError,
)
from hip4maker.quotes import QuoteEngine, QuoteLeg, QuoteParameters
from hip4maker.recording import JsonlRecorder
from hip4maker.references import CompositeReference, WeightedReference, combine_weighted
from hip4maker.risk import RiskLimits, assess_split
from hip4maker.transport import ReadOnlyHttpTransport


class RunnerError(RuntimeError):
    pass


HYPERLIQUID_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
HYPERLIQUID_MAINNET_URL = "https://api.hyperliquid.xyz"
CLIENT_ORDER_ID_PREFIX = "0x48346d6d"
RECONCILE_INTERVAL_MS = 10_000
METADATA_REFRESH_INTERVAL_MS = 300_000
ORDER_ACK_TIMEOUT_MS = 10_000
LIVE_CLEANUP_TIMEOUT_MS = 10_000
BASIS_LOG_INTERVAL_MS = 30_000
BASIS_LOG_QUANTUM = Decimal("0.00001")
SPLIT_CONFIRM_TIMEOUT_MS = 15_000


class RunMode(StrEnum):
    DRY_RUN = "dry-run"
    TESTNET = "testnet"
    MAINNET = "mainnet"


@dataclass(slots=True)
class ReferenceRuntime:
    venue: str
    mapping: Mapping[str, Any]
    client: KalshiReadOnlyClient | PolymarketReadOnlyClient
    canonical_yes_locator: str
    stream: KalshiOrderbookStream | None = None
    active_transport: str | None = None


@dataclass(frozen=True, slots=True)
class EffectiveOpenOrder:
    order: OpenOrder
    effective_side: str
    effective_price: Decimal
    token_side: str


class MarketMakerBot:
    def __init__(
        self,
        config: Mapping[str, Any],
        recorder: JsonlRecorder,
        *,
        basis_recorder: JsonlRecorder | None = None,
        mode: RunMode | str = RunMode.DRY_RUN,
        transport: ReadOnlyTransport | None = None,
        kalshi_credentials: str | Path | None = None,
        require_ready: bool = True,
    ) -> None:
        report = validate_config(config, require_ready=require_ready)
        if not report.structurally_valid:
            details = "; ".join(f"{issue.path}: {issue.message}" for issue in report.errors)
            raise RunnerError(f"configuration is not run-ready: {details}")
        self.config = config
        self.trader = config["trader"]
        self.risk = config["risk"]
        self.recorder = recorder
        self.basis_recorder = basis_recorder
        self.instance_started_ms = _now_ms()
        # Hyperliquid CLOIDs are 16-byte hex values. Keep the last three
        # characters human-readable while the full start time in the digest
        # prevents separate runs of the same market from sharing order IDs.
        self.cloid_instance_suffix = f"{self.instance_started_ms % 1000:03d}"
        try:
            self.mode = RunMode(mode)
        except ValueError as exc:
            raise RunnerError(f"unsupported run mode {mode!r}") from exc
        self.base_url = (
            HYPERLIQUID_MAINNET_URL
            if self.mode is RunMode.MAINNET
            else HYPERLIQUID_TESTNET_URL
        )
        self.sink = DryRunActionSink(recorder)
        self.live_sink: HyperliquidActionSink | None = None
        self.transport = transport or ReadOnlyHttpTransport()
        self.hyperliquid = HyperliquidInfoClient.create(self.base_url, self.transport)
        self.mapping = config["market"]
        self.kalshi_credentials = (
            Path(kalshi_credentials) if kalshi_credentials is not None else None
        )
        self.kalshi_stream: KalshiOrderbookStream | None = None
        self.account_address: str | None = None
        self.contract: VerifiedHip4Contract | None = None
        self.reference_runtimes: list[ReferenceRuntime] = []
        self.basis = BasisEstimator(basis_parameters_from_config(config))
        self.quote_engine = QuoteEngine(
            _quote_parameters(self.trader),
            _risk_limits(self.risk),
        )
        self.risk_limits = _risk_limits(self.risk)
        self.inventory = InventoryState.empty()
        self.owned_orders: tuple[OpenOrder, ...] = ()
        self.simulated_orders: dict[str, OpenOrder] = {}
        self.next_simulated_oid = -1
        self.pending_market_cancels: dict[str, int] = {}
        self.market_cloids: frozenset[str] = frozenset()
        self.oid_to_cloid: dict[int, str] = {}
        self.oid_identity_order: deque[int] = deque()
        self.account_stream: HyperliquidAccountStream | None = None
        self.seen_fill_tids: set[int] = set()
        self.fill_tid_order: deque[int] = deque()
        self.unreconciled_directional_delta = Decimal(0)
        self.last_reconcile_ms = 0
        self.last_metadata_check_ms = 0
        self.last_basis_log_ms = 0
        self.cycle_number = 0
        self.closed = False
        self.startup_inventory_checked = False
        self.simulated_startup_split = Decimal(0)

    @property
    def is_live(self) -> bool:
        return self.mode in {RunMode.TESTNET, RunMode.MAINNET}

    @property
    def poll_interval_ms(self) -> int:
        return int(self.trader["poll_interval_ms"])

    def initialize(self) -> None:
        if self.account_address is None:
            try:
                self.account_address = implied_account_address_from_environment()
            except ActionSubmissionError as exc:
                raise RunnerError(str(exc)) from exc
        if self.is_live and self.account_address is None:
            raise RunnerError(
                "live mode requires HL_CREDENTIALS_FILE to name a credentials JSON file"
            )
        meta_response = self.hyperliquid.outcome_meta()
        if not isinstance(meta_response.payload, dict):
            raise RunnerError("Hyperliquid outcomeMeta response must be an object")
        self.contract = verify_hip4_contract(meta_response.payload, self.mapping)
        self.market_cloids = self._derive_market_cloids()
        self.reference_runtimes = []
        verified_references: list[dict[str, str]] = []
        for venue, reference in self.mapping["references"].items():
            if not reference["enabled"]:
                continue
            if venue == "kalshi":
                client = KalshiReadOnlyClient.create(KALSHI_REST_URL, self.transport)
                market = client.market(reference["market_ticker"])
                verified = verify_kalshi_reference(market.payload, reference, self.mapping)
                # Fail fast if a tie-aware translation names an unknown market.
                tie_ticker = reference.get("tie_market_ticker")
                if tie_ticker:
                    discover_kalshi(
                        client.market(tie_ticker).payload, market_ticker=tie_ticker
                    )
                if self.kalshi_credentials is not None and self.kalshi_stream is None:
                    try:
                        self.kalshi_stream = KalshiOrderbookStream.from_credentials_file(
                            reference["market_ticker"],
                            self.kalshi_credentials,
                        )
                    except KalshiStreamError as exc:
                        raise RunnerError(str(exc)) from exc
                    self.kalshi_stream.start()
                    ready = self.kalshi_stream.wait_ready(10)
                    self._drain_reference_stream_events()
                    self.recorder.record(
                        "kalshi_stream_initialization",
                        {
                            "market_ticker": reference["market_ticker"],
                            "ready": ready,
                            "fallback": None if ready else "public_rest",
                        },
                    )
            elif venue == "polymarket":
                client = PolymarketReadOnlyClient.create(
                    POLYMARKET_GAMMA_URL, POLYMARKET_CLOB_URL, self.transport
                )
                market = client.market_by_slug(reference["market_slug"])
                verified = verify_polymarket_reference(market.payload, reference)
            else:  # Config validation should make this unreachable.
                raise RunnerError(f"unsupported reference venue {venue!r}")
            self.reference_runtimes.append(
                ReferenceRuntime(
                    venue=venue,
                    mapping=reference,
                    client=client,
                    canonical_yes_locator=verified.canonical_yes_locator,
                    stream=self.kalshi_stream if venue == "kalshi" else None,
                )
            )
            verified_references.append(
                {
                    "name": verified.name,
                    "venue": verified.venue,
                    "market_id": verified.market_id,
                }
            )
        if self.kalshi_credentials is not None and self.kalshi_stream is None:
            raise RunnerError(
                "--kalshi-credentials requires an enabled Kalshi reference"
            )
        self.last_metadata_check_ms = _now_ms()
        self._reconcile(force=True)
        self._configure_execution()
        self._start_account_stream()
        if not self.startup_inventory_checked:
            self._emit_startup_split_intent()
            self.startup_inventory_checked = True
        self.recorder.record(
            "initialized",
            {
                "mode": self.mode.value,
                "environment": (
                    "mainnet" if self.mode is RunMode.MAINNET else "testnet"
                ),
                "mapping_id": self.contract.mapping_id,
                "hip4_outcome_id": self.contract.outcome_id,
                "cloid_instance_suffix": self.cloid_instance_suffix,
                "references": verified_references,
                "execution_sink": (
                    "hyperliquid_sdk" if self.is_live else "dry_run_only"
                ),
            },
        )

    def run(self, *, cycles: int = 0) -> None:
        if self.contract is None:
            self.initialize()
        completed = 0
        while cycles <= 0 or completed < cycles:
            started = _now_ms()
            try:
                self.cycle()
            except Exception as exc:
                self.recorder.record(
                    "cycle_error",
                    {"cycle": self.cycle_number, "error": f"{type(exc).__name__}: {exc}"},
                )
                try:
                    self._emit_reconciliation_intents(())
                except Exception as cleanup_exc:
                    self.recorder.record(
                        "cleanup_error",
                        {"error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"},
                    )
                if self.is_live:
                    raise
            completed += 1
            remaining_ms = self.poll_interval_ms - (_now_ms() - started)
            if (cycles <= 0 or completed < cycles) and remaining_ms > 0:
                time.sleep(remaining_ms / 1000)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.contract is not None:
            try:
                self._emit_reconciliation_intents(())
            except Exception as exc:
                self.recorder.record(
                    "cleanup_error",
                    {"stage": "cancel_submission", "error": f"{type(exc).__name__}: {exc}"},
                )
            if self.is_live:
                try:
                    self._wait_for_live_cleanup()
                except Exception as exc:
                    self.recorder.record(
                        "cleanup_error",
                        {"stage": "cancel_confirmation", "error": f"{type(exc).__name__}: {exc}"},
                    )
        if self.account_stream is not None:
            self.account_stream.stop()
            self.account_stream = None
        if self.kalshi_stream is not None:
            self.kalshi_stream.stop()
            self.kalshi_stream = None

    def cycle(self) -> None:
        if self.contract is None:
            raise RunnerError("bot is not initialized")
        self.cycle_number += 1
        now_ms = _now_ms()
        reconcile_for_event = self._drain_account_events()
        if reconcile_for_event:
            self._reconcile(force=True)
        if self.account_stream is not None and not self.account_stream.ready:
            self.recorder.record(
                "paused",
                {"cycle": self.cycle_number, "reason": "account_stream_unhealthy"},
            )
            self._emit_reconciliation_intents(())
            return
        self._reconcile(force=False)
        self._refresh_metadata_if_due(now_ms)

        local_coin = outcome_coin(
            self.contract.outcome_id, self.contract.canonical_yes_side_index
        )
        no_coin = outcome_coin(
            self.contract.outcome_id, self.contract.canonical_no_side_index
        )
        local = self.hyperliquid.l2_book(
            local_coin,
            queried_side_payout="yes",
            mapping_id=self.contract.mapping_id,
        )
        # The No token trades on its own separate order book. Fetch it too (in
        # canonical-Yes terms) so a quote routed through either native token can
        # be checked against the resting liquidity it would actually cross. Each
        # entry is that coin's canonical (best_bid, best_ask); an empty side
        # reads as 0/1, i.e. nothing to cross.
        local_no = self.hyperliquid.l2_book(
            no_coin,
            queried_side_payout="no",
            mapping_id=self.contract.mapping_id,
        )
        cross_guard = {
            "yes": (local.bid, local.ask),
            "no": (local_no.bid, local_no.ask),
        }
        composite = self._reference_book()

        sample = self.basis.sample(local, composite.book, now_ms=now_ms)
        basis_payload: dict[str, Any] = {
            "local_mid": _basis_log_value(local.midpoint),
            "reference_mid": _basis_log_value(composite.book.midpoint),
            "basis_estimate": _basis_log_value(sample.estimate),
            "fair_value": None,
            "yes_total": self.inventory.yes.total,
            "no_total": self.inventory.no.total,
            "quote_total": self.inventory.quote.total,
        }
        if not self.basis.ready:
            self._maybe_record_basis_state(now_ms, basis_payload)
            self._emit_reconciliation_intents(())
            return
        if sample.status not in {SampleStatus.ACCEPTED, SampleStatus.TOO_SOON}:
            self._maybe_record_basis_state(now_ms, basis_payload)
            self._emit_reconciliation_intents(())
            return

        fair_value = self.basis.fair_value(composite.book)
        planning_inventory = self._planning_inventory()
        plan = self.quote_engine.plan(fair_value, planning_inventory)
        basis_payload["fair_value"] = _basis_log_value(fair_value)
        self._maybe_record_basis_state(now_ms, basis_payload)
        if plan.accepted:
            self._emit_reconciliation_intents(
                plan.legs,
                reservation_price=plan.reservation_price,
                cross_guard=cross_guard,
            )
        else:
            self._emit_reconciliation_intents(())

    def _maybe_record_basis_state(
        self,
        now_ms: int,
        payload: dict[str, Any],
    ) -> None:
        if self.basis_recorder is None:
            return
        if now_ms - self.last_basis_log_ms < BASIS_LOG_INTERVAL_MS:
            return
        self.last_basis_log_ms = now_ms
        self.basis_recorder.record_snapshot(payload)

    def _reference_book(self) -> CompositeReference:
        if self.contract is None:
            raise RunnerError("bot is not initialized")
        self._drain_reference_stream_events()
        values: list[WeightedReference] = []
        for runtime in self.reference_runtimes:
            reference = runtime.mapping
            if runtime.venue == "kalshi":
                assert isinstance(runtime.client, KalshiReadOnlyClient)
                book = (
                    runtime.stream.book(
                        canonical_yes_source_side=reference["canonical_yes_side"],
                        mapping_id=self.contract.mapping_id,
                    )
                    if runtime.stream is not None
                    else None
                )
                transport = "websocket" if book is not None else "public_rest"
                if book is None:
                    book = runtime.client.orderbook(
                        reference["market_ticker"],
                        canonical_yes_source_side=reference["canonical_yes_side"],
                        mapping_id=self.contract.mapping_id,
                    )
                if runtime.active_transport != transport:
                    runtime.active_transport = transport
                    self.recorder.record(
                        "reference_transport",
                        {
                            "venue": "kalshi",
                            "market_ticker": reference["market_ticker"],
                            "transport": transport,
                        },
                    )
                # Tie-aware translation: a HIP-4 outcome that settles a draw at
                # a fixed payout per side is worth win + fraction * P(tie), but the
                # win-only reference market omits that premium. Fold in P(tie) from
                # the sibling market by shifting the whole book up by that amount.
                tie_ticker = reference.get("tie_market_ticker")
                if tie_ticker:
                    fraction = Decimal(
                        str(reference.get("tie_settlement_fraction", "0.5"))
                    )
                    tie_book = runtime.client.orderbook(
                        tie_ticker,
                        canonical_yes_source_side="yes",
                        mapping_id=self.contract.mapping_id,
                    )
                    book = shift_canonical_book(book, fraction * tie_book.midpoint)
            else:
                assert isinstance(runtime.client, PolymarketReadOnlyClient)
                book = runtime.client.orderbook(
                    runtime.canonical_yes_locator,
                    token_payout_side="yes",
                    mapping_id=self.contract.mapping_id,
                )
            values.append(WeightedReference(book=book, weight=Decimal(str(reference["weight"]))))
        return combine_weighted(values)

    def _drain_reference_stream_events(self) -> None:
        stream = self.kalshi_stream
        if stream is None:
            return
        for event in stream.drain():
            self.recorder.record(f"kalshi_stream_{event.kind}", event.payload)

    def _start_account_stream(self) -> None:
        if self.account_stream is not None:
            return
        address = self.account_address
        if not address:
            return
        stream = HyperliquidAccountStream(self.base_url, address)
        stream.start()
        self.account_stream = stream
        if not stream.wait_ready(10):
            self.close()
            raise RunnerError("Hyperliquid account WebSocket subscriptions were not acknowledged")
        self.recorder.record(
            "account_stream_ready",
            {
                "subscriptions": ["orderUpdates", "userFills"],
                "transport": "websocket",
            },
        )

    def _configure_execution(self) -> None:
        if not self.is_live or self.live_sink is not None:
            return
        if self.contract is None:
            raise RunnerError("contract must be verified before configuring execution")
        if self.account_address is None:
            raise RunnerError("Hyperliquid account address was not derived")
        coin_assets = {
            outcome_coin(self.contract.outcome_id, side_index): outcome_asset_id(
                self.contract.outcome_id, side_index
            )
            for side_index in (
                self.contract.canonical_yes_side_index,
                self.contract.canonical_no_side_index,
            )
        }
        try:
            sink = HyperliquidActionSink.from_environment(
                self.recorder,
                base_url=self.base_url,
                coin_assets=coin_assets,
            )
        except ActionSubmissionError as exc:
            raise RunnerError(str(exc)) from exc
        self.live_sink = sink
        self.sink = sink

    def _wait_for_live_cleanup(self) -> bool:
        deadline = _now_ms() + LIVE_CLEANUP_TIMEOUT_MS
        while _now_ms() < deadline:
            self._drain_account_events()
            self._reconcile(force=True)
            if not self.owned_orders:
                self.recorder.record("cleanup_confirmed", {"bot_open_orders": 0})
                return True
            time.sleep(0.25)
        self.recorder.record(
            "cleanup_unconfirmed",
            {"bot_open_orders": len(self.owned_orders)},
        )
        return False

    def _drain_account_events(self) -> bool:
        if self.account_stream is None:
            return False
        reconcile = False
        for event in self.account_stream.drain():
            if event.kind in {"stream_status", "stream_error", "subscription_ack"}:
                self.recorder.record(event.kind, dict(event.payload))
                if event.kind == "stream_status" and event.payload.get("status") == "connected":
                    reconcile = True
            elif event.kind == "order_updates":
                self._handle_order_updates(event)
            elif event.kind == "user_fills":
                reconcile = self._handle_user_fills(event) or reconcile
        return reconcile

    def _handle_order_updates(self, event: AccountStreamEvent) -> None:
        updates = event.payload
        if not isinstance(updates, list):
            self.recorder.record(
                "account_stream_message_error",
                {"channel": "orderUpdates", "reason": "data is not an array"},
            )
            return
        for update in updates:
            if not isinstance(update, dict) or not isinstance(update.get("order"), dict):
                self.recorder.record(
                    "account_stream_message_error",
                    {"channel": "orderUpdates", "reason": "invalid order update"},
                )
                continue
            order = update["order"]
            oid = order.get("oid")
            cloid = order.get("cloid")
            if not isinstance(cloid, str):
                cloid = self._cloid_for_oid(oid)
            if not isinstance(cloid, str) or cloid not in self.market_cloids:
                continue
            try:
                parsed = OpenOrder(
                    coin=_required_event_string(order.get("coin"), "order.coin"),
                    oid=_required_event_integer(oid, "order.oid"),
                    side=_required_event_side(order.get("side")),
                    price=Decimal(str(order.get("limitPx"))),
                    size=Decimal(str(order.get("sz"))),
                    cloid=cloid,
                )
            except (RunnerError, ValueError) as exc:
                self.recorder.record(
                    "account_stream_message_error",
                    {"channel": "orderUpdates", "reason": str(exc)},
                )
                continue
            status = update.get("status")
            if not isinstance(status, str) or not status:
                self.recorder.record(
                    "account_stream_message_error",
                    {"channel": "orderUpdates", "reason": "status is missing"},
                )
                continue
            self._remember_order_identity(parsed.oid, cloid)
            market_orders = {item.cloid: item for item in self.owned_orders if item.cloid}
            if status == "open":
                market_orders[cloid] = parsed
            else:
                market_orders.pop(cloid, None)
                self.pending_market_cancels.pop(cloid, None)
            self.owned_orders = tuple(market_orders.values())
            self.simulated_orders.pop(cloid, None)
            self.recorder.record(
                "market_order_update",
                {
                    "source": "hyperliquid",
                    "cloid": cloid,
                    "oid": parsed.oid,
                    "coin": parsed.coin,
                    "side": parsed.side,
                    "price": parsed.price,
                    "remaining_size": parsed.size,
                    "status": status,
                    "status_ts_ms": update.get("statusTimestamp"),
                },
            )

    def _handle_user_fills(self, event: AccountStreamEvent) -> bool:
        data = event.payload
        if not isinstance(data, dict) or not isinstance(data.get("fills"), list):
            self.recorder.record(
                "account_stream_message_error",
                {"channel": "userFills", "reason": "invalid fills message"},
            )
            return False
        fills = data["fills"]
        if data.get("isSnapshot") is True:
            self.recorder.record(
                "fill_snapshot",
                {"count": len(fills), "applied": False, "reason": "REST state is authoritative"},
            )
            return True
        applied = False
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            tid = fill.get("tid")
            if isinstance(tid, bool) or not isinstance(tid, int):
                self.recorder.record(
                    "account_stream_message_error",
                    {"channel": "userFills", "reason": "fill tid is invalid"},
                )
                continue
            if tid in self.seen_fill_tids:
                continue
            self._remember_fill_tid(tid)
            try:
                coin = _required_event_string(fill.get("coin"), "fill.coin")
                side = _required_event_side(fill.get("side"))
                price = Decimal(str(fill.get("px")))
                size = Decimal(str(fill.get("sz")))
                fee = Decimal(str(fill.get("fee", "0")))
            except (RunnerError, ValueError) as exc:
                self.recorder.record(
                    "account_stream_message_error",
                    {"channel": "userFills", "reason": str(exc), "tid": tid},
                )
                continue
            canonical_side, directional_delta = self._map_fill_exposure(coin, side, size)
            self.unreconciled_directional_delta += directional_delta
            gross_quote_delta = price * size * (Decimal(-1) if side == "B" else Decimal(1))
            fee_token = fill.get("feeToken")
            quote_delta = (
                gross_quote_delta - fee
                if self.contract is not None and fee_token == self.contract.quote_token
                else gross_quote_delta
            )
            oid = fill.get("oid")
            self.recorder.record(
                "fill",
                {
                    "source": "hyperliquid",
                    "tid": tid,
                    "oid": oid,
                    "cloid": self._cloid_for_oid(oid),
                    "bot_owned": self._cloid_for_oid(oid) is not None,
                    "coin": coin,
                    "canonical_side": canonical_side,
                    "side": side,
                    "price": price,
                    "size": size,
                    "fee": fee,
                    "fee_token": fee_token,
                    "directional_delta": directional_delta,
                    "projected_directional_shares": (
                        self.inventory.directional_shares
                        + self.unreconciled_directional_delta
                    ),
                    "quote_delta": quote_delta,
                    "time_ms": fill.get("time"),
                    "hash": fill.get("hash"),
                },
            )
            applied = True
        return applied

    def _map_fill_exposure(
        self, coin: str, side: str, size: Decimal
    ) -> tuple[str, Decimal]:
        assert self.contract is not None
        yes_coin = outcome_coin(
            self.contract.outcome_id, self.contract.canonical_yes_side_index
        )
        no_coin = outcome_coin(
            self.contract.outcome_id, self.contract.canonical_no_side_index
        )
        token_delta = size if side == "B" else -size
        if coin == yes_coin:
            return "yes", token_delta
        if coin == no_coin:
            return "no", -token_delta
        return "other", Decimal(0)

    def _remember_fill_tid(self, tid: int) -> None:
        maximum = 10_000
        if len(self.fill_tid_order) >= maximum:
            removed = self.fill_tid_order.popleft()
            self.seen_fill_tids.discard(removed)
        self.fill_tid_order.append(tid)
        self.seen_fill_tids.add(tid)

    def _cloid_for_oid(self, oid: Any) -> str | None:
        if isinstance(oid, bool) or not isinstance(oid, int):
            return None
        known = self.oid_to_cloid.get(oid)
        if known is not None:
            return known
        for order in (*self.owned_orders, *self.simulated_orders.values()):
            if order.oid == oid:
                return order.cloid
        return None

    def _remember_order_identity(self, oid: int, cloid: str) -> None:
        if cloid not in self.market_cloids:
            raise RunnerError("cannot remember an order outside this market's CLOID set")
        maximum = 10_000
        if oid not in self.oid_to_cloid:
            if len(self.oid_identity_order) >= maximum:
                removed = self.oid_identity_order.popleft()
                self.oid_to_cloid.pop(removed, None)
            self.oid_identity_order.append(oid)
        self.oid_to_cloid[oid] = cloid

    def _reconcile(self, *, force: bool) -> None:
        now_ms = _now_ms()
        if not force and now_ms - self.last_reconcile_ms < RECONCILE_INTERVAL_MS:
            return
        address = self.account_address
        if address:
            previous_inventory = (
                self.inventory.yes.total,
                self.inventory.no.total,
                self.inventory.quote.total,
                self.inventory.directional_shares,
            )
            previous_orders = {
                (order.cloid, order.oid, order.price, order.size)
                for order in self.owned_orders
            }
            if self.contract is None:
                raise RunnerError("contract must be initialized before account reconciliation")
            state = self.hyperliquid.spot_state(address)
            if not isinstance(state.payload, dict):
                raise RunnerError("Hyperliquid spot state response must be an object")
            self.inventory = parse_spot_inventory(
                state.payload,
                outcome_id=self.contract.outcome_id,
                canonical_yes_side_index=self.contract.canonical_yes_side_index,
                canonical_no_side_index=self.contract.canonical_no_side_index,
                quote_token=self.contract.quote_token,
            )
            orders = self.hyperliquid.open_orders(address)
            reconciled_orders = parse_open_orders(
                orders.payload,
                managed_client_order_ids=self.market_cloids,
            )
            self.owned_orders = reconciled_orders
            reconciled_cloids = {
                order.cloid for order in reconciled_orders if order.cloid is not None
            }
            self.pending_market_cancels = {
                cloid: sent_ms
                for cloid, sent_ms in self.pending_market_cancels.items()
                if cloid in reconciled_cloids
            }
            for order in reconciled_orders:
                if order.cloid:
                    self._remember_order_identity(order.oid, order.cloid)
                    self.simulated_orders.pop(order.cloid, None)
            self.unreconciled_directional_delta = Decimal(0)
            current_inventory = (
                self.inventory.yes.total,
                self.inventory.no.total,
                self.inventory.quote.total,
                self.inventory.directional_shares,
            )
            current_orders = {
                (order.cloid, order.oid, order.price, order.size)
                for order in self.owned_orders
            }
            inventory_changed = current_inventory != previous_inventory
            orders_changed = current_orders != previous_orders
            if inventory_changed or orders_changed:
                self.recorder.record(
                    "account_reconciled",
                    {
                        "inventory_changed": inventory_changed,
                        "orders_changed": orders_changed,
                        "bot_open_orders": len(self.owned_orders),
                    },
                )
        else:
            self.inventory = InventoryState.empty()
            self.owned_orders = ()
            if self.last_reconcile_ms == 0:
                self.recorder.record(
                    "account_unavailable",
                    {
                        "reason": "Hyperliquid credentials file is unset",
                        "env": "HL_CREDENTIALS_FILE",
                    },
                )
        self.last_reconcile_ms = now_ms

    def _refresh_metadata_if_due(self, now_ms: int) -> None:
        if now_ms - self.last_metadata_check_ms < METADATA_REFRESH_INTERVAL_MS:
            return
        previous_mapping = self.contract.mapping_id if self.contract else None
        self.initialize()
        if self.contract and self.contract.mapping_id != previous_mapping:
            self.basis.reset()
            self.recorder.record(
                "mapping_changed",
                {"previous": previous_mapping, "current": self.contract.mapping_id},
            )

    def _planning_inventory(self) -> InventoryState:
        """Make this bot's resting-order holds available to its desired plan."""

        assert self.contract is not None
        yes_coin = outcome_coin(
            self.contract.outcome_id, self.contract.canonical_yes_side_index
        )
        no_coin = outcome_coin(
            self.contract.outcome_id, self.contract.canonical_no_side_index
        )
        own_yes_hold = sum(
            (
                order.size
                for order in self.owned_orders
                if order.coin == yes_coin and order.side == "A"
            ),
            Decimal(0),
        )
        own_quote_hold = sum(
            (
                order.price * order.size
                for order in self.owned_orders
                if order.coin in {yes_coin, no_coin} and order.side == "B"
            ),
            Decimal(0),
        )
        own_no_hold = sum(
            (
                order.size
                for order in self.owned_orders
                if order.coin == no_coin and order.side == "A"
            ),
            Decimal(0),
        )
        split = self.simulated_startup_split
        return InventoryState(
            quote=TokenBalance(
                total=self.inventory.quote.total - split,
                hold=max(Decimal(0), self.inventory.quote.hold - own_quote_hold),
            ),
            yes=TokenBalance(
                total=self.inventory.yes.total + split,
                hold=max(Decimal(0), self.inventory.yes.hold - own_yes_hold),
            ),
            no=TokenBalance(
                total=self.inventory.no.total + split,
                hold=max(Decimal(0), self.inventory.no.hold - own_no_hold),
            ),
        )

    def _emit_reconciliation_intents(
        self,
        legs: tuple[QuoteLeg, ...],
        *,
        reservation_price: Decimal | None = None,
        cross_guard: dict[str, tuple[Decimal, Decimal]] | None = None,
    ) -> None:
        """Maintain price-sorted quote lists with asymmetric hysteresis.

        A resting order has no permanent rung number.  When fair value moves away,
        the order stays put and naturally becomes a back level.  It is canceled
        for price only when fair value moves toward it far enough to cross the
        cancel threshold.
        """

        assert self.contract is not None
        candidates: dict[str, QuoteLeg] = {}
        side_legs: dict[str, list[QuoteLeg]] = {"bid": [], "ask": []}
        capacities = {"bid": 0, "ask": 0}
        for leg in legs:
            side_legs[leg.effective_side].append(leg)
            capacities[leg.effective_side] += 1
            current = candidates.get(leg.effective_side)
            if current is None or leg.level < current.level:
                candidates[leg.effective_side] = leg

        now_ms = _now_ms()
        active = {"bid": [], "ask": []}
        awaiting_cancel = {"bid": False, "ask": False}
        cancel_reasons: dict[int, str] = {}
        managed = self._managed_orders()

        for order in managed:
            effective = self._effective_open_order(order)
            cloid = order.cloid or ""
            pending_since = self.pending_market_cancels.get(cloid)
            if (
                pending_since is not None
                and now_ms - pending_since <= ORDER_ACK_TIMEOUT_MS
            ):
                if effective is not None:
                    awaiting_cancel[effective.effective_side] = True
                continue
            if pending_since is not None:
                self.pending_market_cancels.pop(cloid, None)

            if effective is None:
                cancel_reasons[order.oid] = "owned order has invalid HIP-4 routing"
                continue
            side = effective.effective_side
            if capacities[side] == 0 or reservation_price is None:
                cancel_reasons[order.oid] = f"effective {side} quoting is disabled"
                continue
            if self._crosses_cancel_threshold(effective, reservation_price):
                cancel_reasons[order.oid] = (
                    f"effective {side} crossed asymmetric cancel threshold"
                )
                continue
            active[side].append(effective)

        rung_spacing = (
            Decimal(str(self.trader["place_thresh"]))
            * Decimal(str(self.trader["rung_thresh_mult"]))
        )
        for side in ("bid", "ask"):
            reverse = side == "bid"
            ordered = sorted(
                active[side], key=lambda item: item.effective_price, reverse=reverse
            )

            # Keep the inside order, then retain only properly spaced back levels.
            spaced: list[EffectiveOpenOrder] = []
            for effective in ordered:
                if not spaced:
                    spaced.append(effective)
                    continue
                previous = spaced[-1].effective_price
                spacing = (
                    previous - effective.effective_price
                    if side == "bid"
                    else effective.effective_price - previous
                )
                if spacing < rung_spacing:
                    cancel_reasons[effective.order.oid] = (
                        f"effective {side} back level violates rung spacing"
                    )
                else:
                    spaced.append(effective)

            capacity = capacities[side]
            while len(spaced) > capacity:
                removed = spaced.pop()
                cancel_reasons[removed.order.oid] = (
                    f"effective {side} exceeds allowed quote count"
                )

            candidate = candidates.get(side)
            needs_front = candidate is not None and (
                not spaced
                or (
                    candidate.effective_price - spaced[0].effective_price
                    if side == "bid"
                    else spaced[0].effective_price - candidate.effective_price
                )
                >= rung_spacing
            )
            if needs_front and len(spaced) >= capacity and spaced:
                removed = spaced.pop()
                cancel_reasons[removed.order.oid] = (
                    f"effective {side} farthest back level makes room for new front"
                )
            active[side] = spaced

        canceled_sides: set[str] = set()
        cancellations: list[tuple[OpenOrder, str]] = []
        for order in managed:
            reason = cancel_reasons.get(order.oid)
            if reason is None:
                continue
            effective = self._effective_open_order(order)
            if effective is not None:
                canceled_sides.add(effective.effective_side)
            cancellations.append((order, reason))
        self._emit_cancel_batch(tuple(cancellations))

        used_cloids = {
            order.cloid for order in self._managed_orders() if order.cloid is not None
        }
        place_back_levels = bool(self.trader.get("place_back_levels", True))
        submissions: list[tuple[QuoteLeg, str, str, ActionIntent]] = []
        skipped_cross: list[QuoteLeg] = []
        for side in ("bid", "ask"):
            if not side_legs[side] or len(active[side]) >= capacities[side]:
                continue
            if awaiting_cancel[side] or (self.is_live and side in canceled_sides):
                continue
            occupied_prices = [item.effective_price for item in active[side]]
            placements: list[QuoteLeg] = []
            for leg in sorted(side_legs[side], key=lambda item: item.level):
                if len(occupied_prices) >= capacities[side]:
                    break
                if occupied_prices and not all(
                    abs(leg.effective_price - price) >= rung_spacing
                    for price in occupied_prices
                ):
                    continue
                # Post-only orders are rejected if they would immediately match.
                # Skip any leg whose price would cross the resting liquidity on the
                # native book it routes to, so we never submit a doomed order.
                if cross_guard is not None and _leg_would_cross(leg, cross_guard):
                    skipped_cross.append(leg)
                    continue
                placements.append(leg)
                occupied_prices.append(leg.effective_price)
                if not place_back_levels:
                    break

            for leg in placements:
                side_index = (
                    self.contract.canonical_yes_side_index
                    if leg.token_side == "yes"
                    else self.contract.canonical_no_side_index
                )
                coin = outcome_coin(self.contract.outcome_id, side_index)
                cloid = self._available_cloid(coin, side, used_cloids)
                used_cloids.add(cloid)
                submissions.append(
                    (
                        leg,
                        coin,
                        cloid,
                        ActionIntent(
                            kind=ActionKind.PLACE,
                            reason=(
                                f"new effective {side} "
                                f"{'front' if leg.level == 0 else 'back level'}"
                            ),
                            payload={
                                "slot": self._cloid_slot(cloid, coin, side),
                                "coin": coin,
                                "asset_id": outcome_asset_id(
                                    self.contract.outcome_id, side_index
                                ),
                                "is_buy": leg.is_buy,
                                "limit_px": leg.native_price,
                                "sz": leg.size,
                                "tif": "Alo",
                                "cloid": cloid,
                            },
                        ),
                    )
                )

        if skipped_cross:
            self.recorder.record(
                "quote_skipped_would_cross",
                {
                    "count": len(skipped_cross),
                    "legs": [
                        {
                            "effective_side": leg.effective_side,
                            "effective_price": leg.effective_price,
                            "token_side": leg.token_side,
                            "level": leg.level,
                        }
                        for leg in skipped_cross
                    ],
                },
            )
        if not submissions:
            return
        results = self.sink.emit_batch(
            tuple(submission[3] for submission in submissions)
        )
        post_only_rejects: list[str] = []
        other_rejects: list[str] = []
        reconcile_after_batch = False
        for (leg, coin, cloid, _), result in zip(
            submissions, results, strict=True
        ):
            if not result.accepted:
                # A rejected place put nothing on the book, so no exposure was
                # taken and the leg is simply retried on a later cycle. Post-only
                # "would immediately match" is a benign race; anything else (tick
                # size, min notional, ...) is logged separately so it stays
                # visible. Neither aborts the bot.
                if _is_post_only_cross(result.status):
                    post_only_rejects.append(f"{cloid}: {result.status}")
                else:
                    other_rejects.append(f"{cloid}: {result.status}")
                continue
            if not self.is_live:
                simulated = OpenOrder(
                    coin=coin,
                    oid=self.next_simulated_oid,
                    side="B" if leg.is_buy else "A",
                    price=leg.native_price,
                    size=leg.size,
                    cloid=cloid,
                )
                self.next_simulated_oid -= 1
                self.simulated_orders[cloid] = simulated
                self._remember_order_identity(simulated.oid, cloid)
                self.recorder.record(
                    "simulated_order_update",
                    {
                        "source": "dry_run",
                        "simulated": True,
                        "cloid": cloid,
                        "oid": simulated.oid,
                        "coin": coin,
                        "side": simulated.side,
                        "price": leg.native_price,
                        "remaining_size": leg.size,
                        "status": "open",
                    },
                )
            elif result.status == "resting" and result.oid is not None:
                resting = OpenOrder(
                    coin=coin,
                    oid=result.oid,
                    side="B" if leg.is_buy else "A",
                    price=leg.native_price,
                    size=leg.size,
                    cloid=cloid,
                )
                by_cloid = {
                    item.cloid: item for item in self.owned_orders if item.cloid
                }
                by_cloid[cloid] = resting
                self.owned_orders = tuple(by_cloid.values())
                self._remember_order_identity(resting.oid, cloid)
                self.recorder.record(
                    "market_order_update",
                    {
                        "source": "submission_response",
                        "cloid": cloid,
                        "oid": resting.oid,
                        "coin": coin,
                        "side": resting.side,
                        "price": leg.native_price,
                        "remaining_size": leg.size,
                        "status": "open",
                    },
                )
            else:
                reconcile_after_batch = True
        if post_only_rejects:
            self.recorder.record(
                "place_rejected_post_only",
                {"count": len(post_only_rejects), "orders": post_only_rejects},
            )
        if other_rejects:
            self.recorder.record(
                "place_rejected",
                {"count": len(other_rejects), "orders": other_rejects},
            )
        if reconcile_after_batch:
            self._reconcile(force=True)
        # Place rejections are never fatal: nothing was placed, so there is no
        # exposure to unwind. The leg is retried on a later cycle.

    def _managed_orders(self) -> tuple[OpenOrder, ...]:
        values = dict(self.simulated_orders)
        for order in self.owned_orders:
            if order.cloid:
                values[order.cloid] = order
        return tuple(values.values())

    def _effective_open_order(self, order: OpenOrder) -> EffectiveOpenOrder | None:
        assert self.contract is not None
        yes_coin = outcome_coin(
            self.contract.outcome_id, self.contract.canonical_yes_side_index
        )
        no_coin = outcome_coin(
            self.contract.outcome_id, self.contract.canonical_no_side_index
        )
        if order.coin == yes_coin:
            return EffectiveOpenOrder(
                order=order,
                effective_side="bid" if order.side == "B" else "ask",
                effective_price=order.price,
                token_side="yes",
            )
        if order.coin == no_coin:
            return EffectiveOpenOrder(
                order=order,
                effective_side="bid" if order.side == "A" else "ask",
                effective_price=ONE - order.price,
                token_side="no",
            )
        return None

    def _crosses_cancel_threshold(
        self, order: EffectiveOpenOrder, reservation_price: Decimal
    ) -> bool:
        cancel_threshold = Decimal(str(self.trader["cancel_thresh"]))
        if order.effective_side == "bid":
            return reservation_price - cancel_threshold < order.effective_price
        return reservation_price + cancel_threshold > order.effective_price

    def _emit_cancel_batch(
        self, cancellations: tuple[tuple[OpenOrder, str], ...]
    ) -> None:
        if not cancellations:
            return
        intents = tuple(
            ActionIntent(
                kind=ActionKind.CANCEL,
                reason=reason,
                payload={"coin": order.coin, "oid": order.oid, "cloid": order.cloid},
            )
            for order, reason in cancellations
        )
        results = self.sink.emit_batch(intents)
        rejected: list[str] = []
        accepted_ms = _now_ms()
        for (order, _), result in zip(cancellations, results, strict=True):
            if not result.accepted:
                rejected.append(f"{order.cloid}: {result.status}")
                continue
            if order.cloid in self.simulated_orders:
                self.simulated_orders.pop(order.cloid, None)
                self.recorder.record(
                    "simulated_order_update",
                    {
                        "source": "dry_run",
                        "simulated": True,
                        "cloid": order.cloid,
                        "oid": order.oid,
                        "coin": order.coin,
                        "status": "canceled",
                    },
                )
            elif order.cloid:
                self.pending_market_cancels[order.cloid] = accepted_ms
        if rejected:
            # Non-fatal: a rejected cancel simply was not marked pending, so it is
            # retried on the next cycle and reconciled against REST. Do not abort;
            # aborting would not cancel the order either, and shutdown retries all
            # cancels anyway.
            self.recorder.record(
                "cancel_rejected",
                {"count": len(rejected), "orders": rejected},
            )

    def _available_cloid(
        self, coin: str, effective_side: str, used_cloids: set[str]
    ) -> str:
        for slot in range(int(self.trader["max_back_levels"]) + 1):
            cloid = self._cloid(coin, effective_side, slot)
            if cloid not in used_cloids:
                return cloid
        raise RunnerError(f"no free client-order-id slot for effective {effective_side}")

    def _cloid_slot(self, cloid: str, coin: str, effective_side: str) -> int:
        for slot in range(int(self.trader["max_back_levels"]) + 1):
            if self._cloid(coin, effective_side, slot) == cloid:
                return slot
        raise RunnerError(f"client-order-id is not a managed {effective_side} slot")

    def _emit_startup_split_intent(self) -> None:
        target = Decimal(str(self.risk["startup_complete_sets"]))
        amount = max(Decimal(0), target - self.inventory.complete_sets)
        if amount == 0:
            self.recorder.record(
                "startup_split_not_needed",
                {
                    "target_complete_sets": target,
                    "existing_complete_sets": self.inventory.complete_sets,
                },
            )
            return
        reasons = assess_split(
            self.inventory,
            amount,
            min_free_quote=Decimal(str(self.risk["min_free_quote"])),
        )
        if reasons:
            self.recorder.record(
                "startup_split_blocked",
                {"amount": amount, "reasons": list(reasons)},
            )
            if self.is_live:
                raise RunnerError(
                    "startup split blocked: " + "; ".join(reasons)
                )
            return
        assert self.contract is not None
        result = self.sink.emit(
            ActionIntent(
                kind=ActionKind.SPLIT,
                reason="startup inventory capitalization",
                payload={"outcome": self.contract.outcome_id, "amount": amount},
            )
        )
        if not result.accepted:
            if self.is_live:
                raise RunnerError(f"startup split rejected: {result.status}")
            return
        if not self.is_live:
            self.simulated_startup_split = amount
            return

        deadline = _now_ms() + SPLIT_CONFIRM_TIMEOUT_MS
        while _now_ms() < deadline:
            self._reconcile(force=True)
            if self.inventory.complete_sets >= target:
                self.recorder.record(
                    "startup_split_confirmed",
                    {
                        "target_complete_sets": target,
                        "complete_sets": self.inventory.complete_sets,
                    },
                )
                return
            time.sleep(0.25)
        raise RunnerError(
            "startup split was accepted but balances did not confirm before timeout"
        )

    def _cloid(self, coin: str, effective_side: str, level: int) -> str:
        assert self.contract is not None
        prefix_hex = CLIENT_ORDER_ID_PREFIX[2:].lower()
        digest = hashlib.sha256(
            (
                f"{self.instance_started_ms}:{self.contract.mapping_id}:"
                f"{coin}:{effective_side}:{level}"
            ).encode("utf-8")
        ).hexdigest()
        digest_length = 32 - len(prefix_hex) - len(self.cloid_instance_suffix)
        return (
            "0x"
            + prefix_hex
            + digest[:digest_length]
            + self.cloid_instance_suffix
        )

    def _derive_market_cloids(self) -> frozenset[str]:
        """Return every deterministic order ID this process instance can emit."""

        assert self.contract is not None
        coins = (
            outcome_coin(
                self.contract.outcome_id,
                self.contract.canonical_yes_side_index,
            ),
            outcome_coin(
                self.contract.outcome_id,
                self.contract.canonical_no_side_index,
            ),
        )
        return frozenset(
            self._cloid(coin, effective_side, level)
            for coin in coins
            for effective_side in ("bid", "ask")
            for level in range(int(self.trader["max_back_levels"]) + 1)
        )


# Compatibility for callers written while the runtime was dry-run-only.
DryRunBot = MarketMakerBot


def _quote_parameters(trader: Mapping[str, Any]) -> QuoteParameters:
    return QuoteParameters(
        place_threshold=Decimal(str(trader["place_thresh"])),
        order_size=Decimal(str(trader["order_size"])),
        max_back_levels=int(trader["max_back_levels"]),
        rung_threshold_multiplier=Decimal(str(trader["rung_thresh_mult"])),
    )


def _risk_limits(risk: Mapping[str, Any]) -> RiskLimits:
    return RiskLimits(
        max_position=Decimal(str(risk["max_position"])),
    )


def _leg_would_cross(
    leg: QuoteLeg, cross_guard: Mapping[str, tuple[Decimal, Decimal]]
) -> bool:
    """True if this post-only leg would immediately match resting liquidity.

    cross_guard maps each native token side to the canonical (best_bid, best_ask)
    of that token's own book. A bid crosses when its price reaches the best ask;
    an ask crosses when its price reaches the best bid. Routing an effective quote
    through the Yes or the No token yields the same canonical condition, so only
    the routed token's book matters. A missing side defaults to 0/1 (nothing to
    cross).
    """

    best_bid, best_ask = cross_guard.get(leg.token_side, (Decimal(0), Decimal(1)))
    if leg.effective_side == "bid":
        return leg.effective_price >= best_ask
    return leg.effective_price <= best_bid


def _is_post_only_cross(status: Any) -> bool:
    """Recognize Hyperliquid's post-only-would-immediately-match rejection."""

    text = str(status).lower()
    return "post only" in text and "immediately match" in text


def _book_payload(book: CanonicalBook) -> dict[str, Any]:
    return {
        "source": book.source,
        "market_id": book.market_id,
        "bid": book.bid,
        "ask": book.ask,
        "mid": book.midpoint,
        "bid_size": book.bid_size,
        "ask_size": book.ask_size,
        "source_ts_ms": book.source_ts_ms,
        "received_ts_ms": book.received_ts_ms,
    }


def _inventory_payload(inventory: InventoryState) -> dict[str, Any]:
    return {
        "quote_total": inventory.quote.total,
        "quote_available": inventory.quote.available,
        "yes_total": inventory.yes.total,
        "yes_available": inventory.yes.available,
        "no_total": inventory.no.total,
        "no_available": inventory.no.available,
        "directional_shares": inventory.directional_shares,
        "complete_sets": inventory.complete_sets,
    }


def _basis_log_value(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(BASIS_LOG_QUANTUM)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _required_event_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RunnerError(f"{field} must be a non-empty string")
    return value


def _required_event_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunnerError(f"{field} must be a nonnegative integer")
    return value


def _required_event_side(value: Any) -> str:
    if value not in {"A", "B"}:
        raise RunnerError("event side must be A or B")
    return value
