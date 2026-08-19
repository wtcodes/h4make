"""Sampled local-minus-reference basis estimation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from hip4maker.books import CanonicalBook, ONE, ZERO, decimal_value


class SampleStatus(StrEnum):
    ACCEPTED = "accepted"
    TOO_SOON = "too_soon"
    LOCAL_STALE = "local_stale"
    REFERENCE_STALE = "reference_stale"
    TIMESTAMP_SKEW = "timestamp_skew"
    MAPPING_MISMATCH = "mapping_mismatch"


@dataclass(frozen=True, slots=True)
class BasisParameters:
    sample_interval_ms: int
    ema_time_constant_ms: int
    apply_fraction: Decimal
    max_timestamp_skew_ms: int
    local_stale_after_ms: int
    reference_stale_after_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "apply_fraction", decimal_value(self.apply_fraction, "apply_fraction")
        )
        if self.sample_interval_ms <= 0:
            raise ValueError("sample_interval_ms must be positive")
        if self.ema_time_constant_ms <= 0:
            raise ValueError("ema_time_constant_ms must be positive")
        if not ZERO <= self.apply_fraction <= ONE:
            raise ValueError("apply_fraction must be within [0, 1]")
        for name in (
            "max_timestamp_skew_ms",
            "local_stale_after_ms",
            "reference_stale_after_ms",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")


@dataclass(frozen=True, slots=True)
class SampleResult:
    status: SampleStatus
    raw_basis: Decimal | None = None
    accepted_basis: Decimal | None = None
    estimate: Decimal | None = None
    sample_count: int = 0


class BasisEstimator:
    """Time-aware EMA of sampled `local midpoint - reference midpoint`."""

    def __init__(self, parameters: BasisParameters) -> None:
        self.parameters = parameters
        self.estimate: Decimal | None = None
        self.sample_count = 0
        self.last_sample_ms: int | None = None

    @property
    def ready(self) -> bool:
        return self.estimate is not None

    def reset(self) -> None:
        self.estimate = None
        self.sample_count = 0
        self.last_sample_ms = None

    def sample(
        self,
        local: CanonicalBook,
        reference: CanonicalBook,
        *,
        now_ms: int,
    ) -> SampleResult:
        if local.mapping_id != reference.mapping_id:
            return self._result(SampleStatus.MAPPING_MISMATCH)
        if local.is_stale(now_ms, self.parameters.local_stale_after_ms):
            return self._result(SampleStatus.LOCAL_STALE)
        if reference.is_stale(now_ms, self.parameters.reference_stale_after_ms):
            return self._result(SampleStatus.REFERENCE_STALE)
        if (
            abs(local.received_ts_ms - reference.received_ts_ms)
            > self.parameters.max_timestamp_skew_ms
        ):
            return self._result(SampleStatus.TIMESTAMP_SKEW)
        if (
            self.last_sample_ms is not None
            and now_ms - self.last_sample_ms < self.parameters.sample_interval_ms
        ):
            return self._result(SampleStatus.TOO_SOON)

        raw_basis = local.midpoint - reference.midpoint
        accepted = raw_basis

        if self.estimate is None:
            self.estimate = accepted
        else:
            assert self.last_sample_ms is not None
            dt_ms = max(0, now_ms - self.last_sample_ms)
            alpha = Decimal(str(1.0 - math.exp(-dt_ms / self.parameters.ema_time_constant_ms)))
            self.estimate = alpha * accepted + (ONE - alpha) * self.estimate

        self.sample_count += 1
        self.last_sample_ms = now_ms
        return self._result(
            SampleStatus.ACCEPTED,
            raw_basis=raw_basis,
            accepted_basis=accepted,
        )

    def fair_value(self, reference: CanonicalBook) -> Decimal:
        if not self.ready or self.estimate is None:
            raise RuntimeError("basis estimator is not initialized")
        translated = reference.midpoint + self.parameters.apply_fraction * self.estimate
        return self._clip(translated, ZERO, ONE)

    def _result(
        self,
        status: SampleStatus,
        *,
        raw_basis: Decimal | None = None,
        accepted_basis: Decimal | None = None,
    ) -> SampleResult:
        return SampleResult(
            status=status,
            raw_basis=raw_basis,
            accepted_basis=accepted_basis,
            estimate=self.estimate,
            sample_count=self.sample_count,
        )

    @staticmethod
    def _clip(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
        return max(lower, min(value, upper))
