"""Dry-run and signed Hyperliquid action sinks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from hip4maker.constraints import (
    MAX_PRICE,
    MIN_ORDER_NOTIONAL,
    MIN_PRICE,
    valid_order_notional,
    valid_order_price,
)


CREDENTIALS_FILE_ENV = "HL_CREDENTIALS_FILE"


class ActionKind(StrEnum):
    PLACE = "place"
    CANCEL = "cancel"
    SPLIT = "split"
    MERGE = "merge"


@dataclass(frozen=True, slots=True)
class ActionIntent:
    kind: ActionKind
    reason: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ActionResult:
    accepted: bool
    status: str
    oid: int | None = None
    response: Any = None


class ActionSubmissionError(RuntimeError):
    pass


class IntentRecorder(Protocol):
    def record(self, event: str, payload: dict[str, Any]) -> None: ...


class ActionSink(Protocol):
    def emit(self, intent: ActionIntent) -> ActionResult: ...

    def emit_batch(
        self, intents: tuple[ActionIntent, ...]
    ) -> tuple[ActionResult, ...]: ...


class DryRunActionSink:
    """Records what the bot would do and has no submission capability."""

    def __init__(self, recorder: IntentRecorder) -> None:
        self.recorder = recorder

    def emit(self, intent: ActionIntent) -> ActionResult:
        _validate_intent(intent)
        self.recorder.record(
            "action_intent",
            {
                "kind": intent.kind.value,
                "reason": intent.reason,
                "payload": _json_safe(intent.payload),
                "submitted": False,
            },
        )
        return ActionResult(accepted=True, status="simulated")

    def emit_batch(
        self, intents: tuple[ActionIntent, ...]
    ) -> tuple[ActionResult, ...]:
        kind = _validate_order_batch(intents)
        self.recorder.record(
            "action_batch_intent",
            {
                "kind": kind.value,
                "count": len(intents),
                "orders": [
                    {"reason": intent.reason, "payload": _json_safe(intent.payload)}
                    for intent in intents
                ],
                "submitted": False,
            },
        )
        return tuple(
            ActionResult(accepted=True, status="simulated") for _ in intents
        )


class HyperliquidActionSink:
    """Submits signed actions through the official Hyperliquid Python SDK."""

    def __init__(
        self,
        recorder: IntentRecorder,
        *,
        base_url: str,
        credentials_file: str | Path,
        coin_assets: dict[str, int],
    ) -> None:
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
        except ImportError as exc:  # pragma: no cover - depends on live installation
            raise ActionSubmissionError(
                "live modes require hyperliquid-python-sdk and eth-account"
            ) from exc

        self.recorder = recorder
        self.base_url = base_url
        try:
            wallet = Account.from_key(_load_secret_key(credentials_file))
            self.exchange = Exchange(
                wallet,
                base_url=base_url,
                account_address=wallet.address,
            )
        except ActionSubmissionError:
            raise
        except Exception as exc:
            raise ActionSubmissionError(
                f"cannot initialize Hyperliquid SDK: {type(exc).__name__}: {exc}"
            ) from exc
        for coin, asset_id in coin_assets.items():
            # Outcome assets are not included in SDK 0.23 spot metadata. Register
            # the verified HIP-4 IDs so its normal order/cancel signing path works.
            self.exchange.info.name_to_coin[coin] = coin
            self.exchange.info.coin_to_asset[coin] = asset_id

    @classmethod
    def from_environment(
        cls,
        recorder: IntentRecorder,
        *,
        base_url: str,
        coin_assets: dict[str, int],
    ) -> "HyperliquidActionSink":
        path = os.environ.get(CREDENTIALS_FILE_ENV)
        if not path:
            raise ActionSubmissionError(
                f"live mode requires {CREDENTIALS_FILE_ENV} to name a credentials JSON file"
            )
        return cls(
            recorder,
            base_url=base_url,
            credentials_file=path,
            coin_assets=coin_assets,
        )

    def emit(self, intent: ActionIntent) -> ActionResult:
        _validate_intent(intent)
        self.recorder.record(
            "action_intent",
            {
                "kind": intent.kind.value,
                "reason": intent.reason,
                "payload": _json_safe(intent.payload),
                "submitted": True,
            },
        )
        try:
            if intent.kind is ActionKind.PLACE:
                response = self._place(intent.payload)
            elif intent.kind is ActionKind.CANCEL:
                response = self._cancel(intent.payload)
            elif intent.kind is ActionKind.SPLIT:
                response = self._split(intent.payload)
            else:
                raise ActionSubmissionError(
                    f"live action {intent.kind.value!r} is not implemented"
                )
        except Exception as exc:
            self.recorder.record(
                "action_response",
                {
                    "kind": intent.kind.value,
                    "accepted": False,
                    "status": "exception",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            if isinstance(exc, ActionSubmissionError):
                raise
            raise ActionSubmissionError(
                f"{intent.kind.value} submission failed: {type(exc).__name__}: {exc}"
            ) from exc

        result = _parse_action_response(intent.kind, response)
        self.recorder.record(
            "action_response",
            {
                "kind": intent.kind.value,
                "accepted": result.accepted,
                "status": result.status,
                "oid": result.oid,
                "response": _json_safe(response),
            },
        )
        return result

    def emit_batch(
        self, intents: tuple[ActionIntent, ...]
    ) -> tuple[ActionResult, ...]:
        kind = _validate_order_batch(intents)
        self.recorder.record(
            "action_batch_intent",
            {
                "kind": kind.value,
                "count": len(intents),
                "orders": [
                    {"reason": intent.reason, "payload": _json_safe(intent.payload)}
                    for intent in intents
                ],
                "submitted": True,
            },
        )
        try:
            payloads = tuple(intent.payload for intent in intents)
            response = (
                self._place_batch(payloads)
                if kind is ActionKind.PLACE
                else self._cancel_batch(payloads)
            )
        except Exception as exc:
            self.recorder.record(
                "action_batch_response",
                {
                    "kind": kind.value,
                    "count": len(intents),
                    "accepted": False,
                    "status": "exception",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            if isinstance(exc, ActionSubmissionError):
                raise
            raise ActionSubmissionError(
                f"{kind.value} batch submission failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        results = _parse_action_responses(kind, response, expected=len(intents))
        self.recorder.record(
            "action_batch_response",
            {
                "kind": kind.value,
                "count": len(intents),
                "accepted": all(result.accepted for result in results),
                "results": [
                    {
                        "cloid": intent.payload.get("cloid"),
                        "accepted": result.accepted,
                        "status": result.status,
                        "oid": result.oid,
                    }
                    for intent, result in zip(intents, results, strict=True)
                ],
                "response": _json_safe(response),
            },
        )
        return results

    def _place(self, payload: dict[str, Any]) -> Any:
        from hyperliquid.utils.types import Cloid

        return self.exchange.order(
            str(payload["coin"]),
            bool(payload["is_buy"]),
            float(payload["sz"]),
            float(payload["limit_px"]),
            {"limit": {"tif": str(payload["tif"])}},
            cloid=Cloid.from_str(str(payload["cloid"])),
        )

    def _place_batch(self, payloads: tuple[dict[str, Any], ...]) -> Any:
        from hyperliquid.utils.types import Cloid

        orders = [
            {
                "coin": str(payload["coin"]),
                "is_buy": bool(payload["is_buy"]),
                "sz": float(payload["sz"]),
                "limit_px": float(payload["limit_px"]),
                "order_type": {"limit": {"tif": str(payload["tif"])}},
                "reduce_only": False,
                "cloid": Cloid.from_str(str(payload["cloid"])),
            }
            for payload in payloads
        ]
        return self.exchange.bulk_orders(orders)

    def _cancel(self, payload: dict[str, Any]) -> Any:
        return self.exchange.cancel(str(payload["coin"]), int(payload["oid"]))

    def _cancel_batch(self, payloads: tuple[dict[str, Any], ...]) -> Any:
        cancels = [
            {"coin": str(payload["coin"]), "oid": int(payload["oid"])}
            for payload in payloads
        ]
        return self.exchange.bulk_cancel(cancels)

    def _split(self, payload: dict[str, Any]) -> Any:
        # SDK 0.23 has no wrapper for HIP-4 userOutcome yet. Its own signing
        # primitive and HTTP submission path are used verbatim.
        from hyperliquid.utils.constants import MAINNET_API_URL
        from hyperliquid.utils.signing import get_timestamp_ms, sign_l1_action

        action = {
            "type": "userOutcome",
            "splitOutcome": {
                "outcome": int(payload["outcome"]),
                "amount": str(payload["amount"]),
            },
        }
        timestamp = get_timestamp_ms()
        signature = sign_l1_action(
            self.exchange.wallet,
            action,
            self.exchange.vault_address,
            timestamp,
            self.exchange.expires_after,
            self.base_url == MAINNET_API_URL,
        )
        return self.exchange._post_action(action, signature, timestamp)


def _load_secret_key(path: str | Path) -> str:
    credential_path = Path(path)
    try:
        payload = json.loads(credential_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActionSubmissionError(
            f"cannot load credentials file {credential_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ActionSubmissionError("credentials JSON must be an object")
    value = payload.get("secret_key")
    if not isinstance(value, str) or not value:
        raise ActionSubmissionError("credentials JSON must contain a secret_key string")
    return value


def implied_account_address_from_environment() -> str | None:
    """Derive the non-delegated Hyperliquid account from its signing key."""

    credentials_file = os.environ.get(CREDENTIALS_FILE_ENV)
    if not credentials_file:
        return None
    try:
        from eth_account import Account
    except ImportError as exc:  # pragma: no cover - installation dependency
        raise ActionSubmissionError(
            "Hyperliquid credentials require eth-account"
        ) from exc
    try:
        return Account.from_key(_load_secret_key(credentials_file)).address
    except ActionSubmissionError:
        raise
    except Exception as exc:
        raise ActionSubmissionError(
            f"cannot derive Hyperliquid account from credentials: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _validate_intent(intent: ActionIntent) -> None:
    if intent.kind is ActionKind.CANCEL:
        coin = intent.payload.get("coin")
        oid = intent.payload.get("oid")
        if not isinstance(coin, str) or not coin:
            raise ActionSubmissionError("cancel action requires a coin")
        if isinstance(oid, bool) or not isinstance(oid, int):
            raise ActionSubmissionError("cancel action requires an integer oid")
        return
    if intent.kind is not ActionKind.PLACE:
        return
    try:
        price = Decimal(str(intent.payload["limit_px"]))
    except (InvalidOperation, KeyError, ValueError) as exc:
        raise ActionSubmissionError("place action requires a decimal limit_px") from exc
    if not valid_order_price(price):
        raise ActionSubmissionError(
            f"refusing place action with price outside "
            f"[{MIN_PRICE}, {MAX_PRICE}]: {price}"
        )
    try:
        size = Decimal(str(intent.payload["sz"]))
    except (InvalidOperation, KeyError, ValueError) as exc:
        raise ActionSubmissionError("place action requires a decimal sz") from exc
    if not valid_order_notional(price, size):
        raise ActionSubmissionError(
            "refusing place action below the minimum notional: "
            f"{price * size} < {MIN_ORDER_NOTIONAL}"
        )


def _validate_order_batch(intents: tuple[ActionIntent, ...]) -> ActionKind:
    if not intents:
        raise ActionSubmissionError("order batch cannot be empty")
    kind = intents[0].kind
    if kind not in {ActionKind.PLACE, ActionKind.CANCEL}:
        raise ActionSubmissionError("a batch may contain only place or cancel actions")
    for intent in intents:
        if intent.kind is not kind:
            raise ActionSubmissionError("an order batch must contain one action kind")
        _validate_intent(intent)
    return kind


def _parse_action_response(kind: ActionKind, response: Any) -> ActionResult:
    return _parse_action_responses(kind, response, expected=1)[0]


def _parse_action_responses(
    kind: ActionKind, response: Any, *, expected: int
) -> tuple[ActionResult, ...]:
    if not isinstance(response, dict) or response.get("status") != "ok":
        return tuple(
            ActionResult(accepted=False, status="rejected", response=response)
            for _ in range(expected)
        )
    if kind not in {ActionKind.PLACE, ActionKind.CANCEL}:
        return tuple(
            ActionResult(accepted=True, status="ok", response=response)
            for _ in range(expected)
        )

    data = response.get("response")
    if not isinstance(data, dict):
        return _malformed_results(response, expected)
    nested = data.get("data")
    statuses = nested.get("statuses") if isinstance(nested, dict) else None
    if not isinstance(statuses, list):
        return _malformed_results(response, expected)
    if len(statuses) != expected:
        if len(statuses) == 1:
            shared = _parse_order_status(kind, statuses[0], response)
            if not shared.accepted:
                return tuple(shared for _ in range(expected))
        return _malformed_results(response, expected)

    return tuple(_parse_order_status(kind, status, response) for status in statuses)


def _parse_order_status(kind: ActionKind, status: Any, response: Any) -> ActionResult:
    if kind is ActionKind.CANCEL:
        accepted = status == "success"
        return ActionResult(
            accepted=accepted,
            status="success" if accepted else _status_name(status),
            response=response,
        )
    if isinstance(status, dict) and isinstance(status.get("resting"), dict):
        oid = status["resting"].get("oid")
        if isinstance(oid, int) and not isinstance(oid, bool):
            return ActionResult(accepted=True, status="resting", oid=oid, response=response)
    if isinstance(status, dict) and isinstance(status.get("filled"), dict):
        oid = status["filled"].get("oid")
        return ActionResult(
            accepted=True,
            status="filled",
            oid=oid if isinstance(oid, int) and not isinstance(oid, bool) else None,
            response=response,
        )
    return ActionResult(accepted=False, status=_status_name(status), response=response)


def _malformed_results(response: Any, expected: int) -> tuple[ActionResult, ...]:
    return tuple(
        ActionResult(accepted=False, status="malformed_response", response=response)
        for _ in range(expected)
    )


def _status_name(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("error"), str):
        return f"error: {value['error']}"
    return str(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
