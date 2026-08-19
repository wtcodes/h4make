"""Strict loading and validation for one HIP-4 market maker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from hip4maker.basis import BasisParameters


class ConfigLoadError(ValueError):
    """Raised when a JSON configuration is ambiguous or malformed."""


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    severity: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def readiness(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "readiness")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def structurally_valid(self) -> bool:
        return not self.errors

    @property
    def trade_ready(self) -> bool:
        return not self.errors and not self.readiness


class _DuplicateKeyDetector(dict[str, Any]):
    def __init__(self, pairs: Iterable[tuple[str, Any]]) -> None:
        super().__init__()
        duplicates: list[str] = []
        for key, value in pairs:
            if key in self:
                duplicates.append(key)
            self[key] = value
        if duplicates:
            raise ConfigLoadError(
                "duplicate JSON key(s): " + ", ".join(sorted(set(duplicates)))
            )


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigLoadError(f"cannot read {config_path}: {exc}") from exc
    try:
        value = json.loads(text, object_pairs_hook=_DuplicateKeyDetector)
    except ConfigLoadError:
        raise
    except json.JSONDecodeError as exc:
        raise ConfigLoadError(
            f"invalid JSON in {config_path} at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise ConfigLoadError("configuration root must be an object")
    return dict(value)


ROOT_FIELDS = {"market", "risk", "trader"}
MARKET_FIELDS = {
    "reviewed",
    "scheduled_start_utc",
    "canonical_yes",
    "canonical_no",
    "hip4",
    "references",
}
HIP4_FIELDS = {"outcome_id", "canonical_yes_side", "quote_token"}
KALSHI_FIELDS = {"enabled", "weight", "market_ticker", "canonical_yes_side"}
POLYMARKET_FIELDS = {"enabled", "weight", "market_slug", "canonical_yes_outcome"}
RISK_FIELDS = {"startup_complete_sets", "min_free_quote", "max_position"}
TRADER_FIELDS = {
    "basis_sample_interval_s",
    "basis_ema_tdc_s",
    "basis_apply_fraction",
    "place_thresh",
    "cancel_thresh",
    "order_size",
    "max_back_levels",
    "place_back_levels",
    "rung_thresh_mult",
    "poll_interval_ms",
}


class _Validator:
    def __init__(self, require_ready: bool) -> None:
        self.require_ready = require_ready
        self.issues: list[ValidationIssue] = []

    def error(self, path: str, message: str) -> None:
        self.issues.append(ValidationIssue("error", path, message))

    def not_ready(self, path: str, message: str) -> None:
        severity = "error" if self.require_ready else "readiness"
        self.issues.append(ValidationIssue(severity, path, message))

    def object(
        self,
        value: Any,
        path: str,
        fields: set[str],
        *,
        require_all: bool = True,
    ) -> Mapping[str, Any] | None:
        if not isinstance(value, dict):
            self.error(path, "must be an object")
            return None
        for key in sorted(set(value) - fields):
            self.error(f"{path}.{key}", "unknown field")
        if require_all:
            for key in sorted(fields - set(value)):
                self.error(f"{path}.{key}", "required field is missing")
        return value

    def string(self, value: Any, path: str, *, ready: bool = False) -> str | None:
        if value is None and ready:
            self.not_ready(path, "must be populated by mapping review")
            return None
        if not isinstance(value, str) or not value:
            self.error(path, "must be a non-empty string")
            return None
        return value

    def boolean(self, value: Any, path: str) -> bool | None:
        if not isinstance(value, bool):
            self.error(path, "must be a boolean")
            return None
        return value

    def number(
        self,
        value: Any,
        path: str,
        *,
        positive: bool = False,
        nonnegative: bool = False,
    ) -> Decimal | None:
        if isinstance(value, bool):
            self.error(path, "must be numeric")
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            self.error(path, "must be numeric")
            return None
        if not result.is_finite():
            self.error(path, "must be finite")
        elif positive and result <= 0:
            self.error(path, "must be positive")
        elif nonnegative and result < 0:
            self.error(path, "must be nonnegative")
        return result

    def integer(
        self,
        value: Any,
        path: str,
        *,
        positive: bool = False,
        nonnegative: bool = False,
        ready: bool = False,
    ) -> int | None:
        if value is None and ready:
            self.not_ready(path, "must be populated before mapping review")
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self.error(path, "must be an integer")
            return None
        if positive and value <= 0:
            self.error(path, "must be positive")
        if nonnegative and value < 0:
            self.error(path, "must be nonnegative")
        return value


def validate_config(config: Mapping[str, Any], *, require_ready: bool = False) -> ValidationReport:
    v = _Validator(require_ready)
    root = v.object(config, "$", ROOT_FIELDS)
    if root is None:
        return ValidationReport(tuple(v.issues))
    _validate_market(v, root.get("market"), "$.market")
    risk = _validate_risk(v, root.get("risk"), "$.risk")
    trader = _validate_trader(v, root.get("trader"), "$.trader")
    if risk is not None and trader is not None:
        _validate_inventory_envelope(v, risk, trader)
    return ValidationReport(tuple(v.issues))


def _validate_market(v: _Validator, value: Any, path: str) -> None:
    market = v.object(value, path, MARKET_FIELDS)
    if market is None:
        return
    reviewed = v.boolean(market.get("reviewed"), f"{path}.reviewed")
    if reviewed is not True:
        v.not_ready(f"{path}.reviewed", "mapping has not been manually verified")
    if market.get("scheduled_start_utc") is None:
        v.not_ready(f"{path}.scheduled_start_utc", "must be set before running")
    else:
        v.string(market.get("scheduled_start_utc"), f"{path}.scheduled_start_utc")
    for field in ("canonical_yes", "canonical_no"):
        v.string(market.get(field), f"{path}.{field}", ready=True)
    if market.get("canonical_yes") == market.get("canonical_no") and market.get(
        "canonical_yes"
    ) is not None:
        v.error(f"{path}.canonical_no", "must differ from canonical_yes")

    hip4 = v.object(market.get("hip4"), f"{path}.hip4", HIP4_FIELDS)
    if hip4 is not None:
        v.integer(
            hip4.get("outcome_id"), f"{path}.hip4.outcome_id", nonnegative=True, ready=True
        )
        side = hip4.get("canonical_yes_side")
        if side is None:
            v.not_ready(
                f"{path}.hip4.canonical_yes_side",
                "must be populated by mapping review",
            )
        elif side not in {0, 1} or isinstance(side, bool):
            v.error(f"{path}.hip4.canonical_yes_side", "must be 0 or 1")
        v.string(hip4.get("quote_token"), f"{path}.hip4.quote_token", ready=True)

    references = v.object(
        market.get("references"),
        f"{path}.references",
        {"kalshi", "polymarket"},
        require_all=False,
    )
    if references is None:
        return
    if not references:
        v.error(f"{path}.references", "must contain kalshi and/or polymarket")
    enabled_count = 0
    if "kalshi" in references:
        ref = v.object(references["kalshi"], f"{path}.references.kalshi", KALSHI_FIELDS)
        if ref is not None:
            enabled = v.boolean(
                ref.get("enabled"), f"{path}.references.kalshi.enabled"
            )
            if enabled:
                enabled_count += 1
                v.number(ref.get("weight"), f"{path}.references.kalshi.weight", positive=True)
                v.string(
                    ref.get("market_ticker"),
                    f"{path}.references.kalshi.market_ticker",
                    ready=True,
                )
                side = ref.get("canonical_yes_side")
                if side is None:
                    v.not_ready(
                        f"{path}.references.kalshi.canonical_yes_side",
                        "must be populated by mapping review",
                    )
                elif side not in {"yes", "no"}:
                    v.error(
                        f"{path}.references.kalshi.canonical_yes_side",
                        "must be yes or no",
                    )
    if "polymarket" in references:
        ref = v.object(
            references["polymarket"],
            f"{path}.references.polymarket",
            POLYMARKET_FIELDS,
        )
        if ref is not None:
            enabled = v.boolean(
                ref.get("enabled"), f"{path}.references.polymarket.enabled"
            )
            if enabled:
                enabled_count += 1
                v.number(
                    ref.get("weight"),
                    f"{path}.references.polymarket.weight",
                    positive=True,
                )
                v.string(
                    ref.get("market_slug"),
                    f"{path}.references.polymarket.market_slug",
                    ready=True,
                )
                v.string(
                    ref.get("canonical_yes_outcome"),
                    f"{path}.references.polymarket.canonical_yes_outcome",
                    ready=True,
                )
    if references and enabled_count == 0:
        v.error(f"{path}.references", "at least one reference must be enabled")


def _validate_risk(
    v: _Validator, value: Any, path: str
) -> Mapping[str, Any] | None:
    risk = v.object(value, path, RISK_FIELDS)
    if risk is None:
        return None
    v.number(risk.get("startup_complete_sets"), f"{path}.startup_complete_sets", nonnegative=True)
    v.number(risk.get("min_free_quote"), f"{path}.min_free_quote", nonnegative=True)
    v.number(risk.get("max_position"), f"{path}.max_position", positive=True)
    return risk


def _validate_trader(
    v: _Validator, value: Any, path: str
) -> Mapping[str, Any] | None:
    trader = v.object(value, path, TRADER_FIELDS, require_all=False)
    if trader is None:
        return None
    for field in sorted(TRADER_FIELDS - {"place_back_levels"} - set(trader)):
        v.error(f"{path}.{field}", "required field is missing")
    for field in (
        "basis_sample_interval_s",
        "basis_ema_tdc_s",
        "place_thresh",
        "cancel_thresh",
        "order_size",
        "rung_thresh_mult",
    ):
        v.number(trader.get(field), f"{path}.{field}", positive=True)
    apply_fraction = v.number(
        trader.get("basis_apply_fraction"), f"{path}.basis_apply_fraction", nonnegative=True
    )
    if apply_fraction is not None and apply_fraction > 1:
        v.error(f"{path}.basis_apply_fraction", "must be within [0, 1]")
    v.integer(trader.get("max_back_levels"), f"{path}.max_back_levels", nonnegative=True)
    if "place_back_levels" in trader:
        v.boolean(trader["place_back_levels"], f"{path}.place_back_levels")
    v.integer(trader.get("poll_interval_ms"), f"{path}.poll_interval_ms", positive=True)
    return trader


def _validate_inventory_envelope(
    v: _Validator, risk: Mapping[str, Any], trader: Mapping[str, Any]
) -> None:
    startup = v.number(risk.get("startup_complete_sets"), "$.risk.startup_complete_sets")
    max_position = v.number(risk.get("max_position"), "$.risk.max_position")
    order_size = v.number(trader.get("order_size"), "$.trader.order_size")
    levels = trader.get("max_back_levels")
    if None in (startup, max_position, order_size) or not isinstance(levels, int):
        return
    ladder_size = order_size * (levels + 1)
    if ladder_size > startup:
        v.error(
            "$.trader.max_back_levels",
            "full ladder requires more shares than startup_complete_sets",
        )
    if ladder_size > max_position:
        v.error(
            "$.trader.max_back_levels",
            "full asymmetric ladder fill would exceed max_position",
        )


def basis_parameters_from_config(config: Mapping[str, Any]) -> BasisParameters:
    trader = config["trader"]
    return BasisParameters(
        sample_interval_ms=int(Decimal(str(trader["basis_sample_interval_s"])) * 1000),
        ema_time_constant_ms=int(Decimal(str(trader["basis_ema_tdc_s"])) * 1000),
        apply_fraction=Decimal(str(trader["basis_apply_fraction"])),
        max_timestamp_skew_ms=2_000,
        local_stale_after_ms=5_000,
        reference_stale_after_ms=5_000,
    )
