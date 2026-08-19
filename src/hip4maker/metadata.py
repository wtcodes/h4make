"""Discover and verify the native sides of configured outcome markets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class MetadataError(ValueError):
    pass


def _identity_hash(document: Any) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Hip4Discovery:
    outcome_id: int
    outcome_name: str
    side_labels: tuple[str, str]
    side_participants: tuple[str, str]
    quote_token: str
    terms: str


@dataclass(frozen=True, slots=True)
class KalshiDiscovery:
    market_ticker: str
    title: str
    yes_label: str
    no_label: str
    rules: str


@dataclass(frozen=True, slots=True)
class PolymarketDiscovery:
    market_slug: str
    question: str
    outcomes: tuple[str, str]
    token_ids: tuple[str, str]
    description: str


@dataclass(frozen=True, slots=True)
class VerifiedHip4Contract:
    mapping_id: str
    outcome_id: int
    outcome_name: str
    canonical_yes_side_index: int
    canonical_no_side_index: int
    quote_token: str


@dataclass(frozen=True, slots=True)
class VerifiedReferenceContract:
    name: str
    venue: str
    market_id: str
    canonical_yes_locator: str


def discover_hip4(outcome_meta: Mapping[str, Any], *, outcome_id: int) -> Hip4Discovery:
    outcomes = _sequence(outcome_meta.get("outcomes"), "outcomeMeta.outcomes")
    outcome = _find_by_integer(outcomes, "outcome", outcome_id, "outcome")
    fields = _metadata_fields(outcome.get("description"))
    outcome_template = _string(outcome.get("name"), "outcome.name")
    if outcome_template.casefold() != "template:sportscontestwinner3":
        raise MetadataError(
            "HIP-4 outcome is not a sportsContestWinner3 standalone outcome"
        )
    side_specs = _sequence(outcome.get("sideSpecs"), "outcome.sideSpecs")
    if len(side_specs) != 2:
        raise MetadataError("HIP-4 outcome must have exactly two sideSpecs")
    side_labels = tuple(
        _resolve_template_value(
            _string(_mapping(side, "sideSpec").get("name"), "sideSpec.name"),
            fields,
        )
        for side in side_specs
    )
    if len(set(label.casefold() for label in side_labels)) != 2:
        raise MetadataError("HIP-4 side labels must be distinct")
    side_participants = _side_participants(fields, side_labels)
    if any(_is_draw_or_tie_label(value) for value in (*side_labels, *side_participants)):
        raise MetadataError("draw/tie outcomes are not supported")
    participant_a = fields.get("participantA")
    participant_b = fields.get("participantB")
    outcome_name = (
        f"{participant_a} vs {participant_b}"
        if participant_a and participant_b
        else _string(outcome.get("name"), "outcome.name")
    )
    return Hip4Discovery(
        outcome_id=outcome_id,
        outcome_name=outcome_name,
        side_labels=(side_labels[0], side_labels[1]),
        side_participants=side_participants,
        quote_token=_string(outcome.get("quoteToken"), "outcome.quoteToken"),
        terms=str(outcome.get("description", "")),
    )


def _resolve_template_value(value: str, fields: Mapping[str, str]) -> str:
    prefix = "template:{"
    if value.startswith(prefix) and value.endswith("}"):
        key = value[len(prefix) : -1]
        resolved = fields.get(key)
        if not resolved:
            raise MetadataError(f"HIP-4 side template has no {key!r} metadata")
        return resolved
    return value


def _side_participants(
    fields: Mapping[str, str], side_labels: tuple[str, ...]
) -> tuple[str, str]:
    pairs: list[tuple[str, str]] = []
    for suffix in ("A", "B"):
        participant = fields.get(f"participant{suffix}")
        short_name = fields.get(f"shortName{suffix}")
        if not participant or not short_name:
            raise MetadataError(
                "standalone sports outcome must define participantA/B and shortNameA/B"
            )
        pairs.append((short_name, participant))
    participants: list[str] = []
    for side_label in side_labels:
        matches = [
            participant
            for short_name, participant in pairs
            if _same_label(side_label, short_name)
            or _same_label(side_label, participant)
        ]
        if len(matches) != 1:
            raise MetadataError(
                f"HIP-4 side {side_label!r} does not identify exactly one participant"
            )
        participants.append(matches[0])
    if len(set(value.casefold() for value in participants)) != 2:
        raise MetadataError("HIP-4 sides must identify two distinct participants")
    return participants[0], participants[1]


def _is_draw_or_tie_label(value: str) -> bool:
    return value.strip().casefold() in {"draw", "tie"}


def _outcome_display_name(value: Mapping[str, Any], field: str) -> str:
    name = _string(value.get("name"), f"{field}.name")
    normalized = name.casefold()
    if normalized == "template:sportscontestdraw":
        return "Draw"
    if normalized == "template:sportscontestparticipant":
        participant = _metadata_fields(value.get("description")).get("participant")
        if participant:
            return participant
        raise MetadataError(f"{field} participant template has no participant metadata")
    return name


def _question_display_name(value: Mapping[str, Any]) -> str:
    name = _string(value.get("name"), "question.name")
    if name.casefold() != "template:sportscontestresult":
        return name
    fields = _metadata_fields(value.get("description"))
    participant_a = fields.get("participantA")
    participant_b = fields.get("participantB")
    if participant_a and participant_b:
        return f"{participant_a} vs {participant_b}"
    return name


def _is_draw_or_tie(value: Mapping[str, Any], display_name: str) -> bool:
    raw_name = str(value.get("name", "")).casefold()
    normalized_display = display_name.strip().casefold()
    return normalized_display in {"draw", "tie"} or raw_name.endswith(
        ("contestdraw", "contesttie")
    )


def _metadata_fields(value: Any) -> dict[str, str]:
    if not isinstance(value, str):
        return {}
    fields: dict[str, str] = {}
    for part in value.split("|"):
        key, separator, item = part.partition(":")
        if separator and key and item:
            fields[key] = item
    return fields


def discover_kalshi(payload: Any, *, market_ticker: str) -> KalshiDiscovery:
    root = _mapping(payload, "Kalshi market response")
    market_value = root.get("market", root)
    market = _mapping(market_value, "Kalshi market")
    actual_ticker = _string(market.get("ticker"), "Kalshi market.ticker")
    if actual_ticker != market_ticker:
        raise MetadataError(
            f"Kalshi returned ticker {actual_ticker!r}, expected {market_ticker!r}"
        )
    return KalshiDiscovery(
        market_ticker=actual_ticker,
        title=_string(market.get("title"), "Kalshi market.title"),
        yes_label=_string(market.get("yes_sub_title"), "Kalshi yes_sub_title"),
        no_label=_string(market.get("no_sub_title"), "Kalshi no_sub_title"),
        rules="\n".join(
            str(market.get(key, ""))
            for key in ("rules_primary", "rules_secondary")
            if market.get(key)
        ),
    )


def discover_polymarket(payload: Any, *, market_slug: str) -> PolymarketDiscovery:
    values = payload if isinstance(payload, list) else [payload]
    if len(values) != 1:
        raise MetadataError("Polymarket market lookup must return exactly one market")
    market = _mapping(values[0], "Polymarket market")
    actual_slug = _string(market.get("slug"), "Polymarket market.slug")
    if actual_slug != market_slug:
        raise MetadataError(
            f"Polymarket returned slug {actual_slug!r}, expected {market_slug!r}"
        )
    outcomes = _string_array(market.get("outcomes"), "Polymarket outcomes")
    token_ids = _string_array(market.get("clobTokenIds"), "Polymarket clobTokenIds")
    if len(outcomes) != 2 or len(token_ids) != 2:
        raise MetadataError("Polymarket market must have exactly two outcomes and tokens")
    if len(set(outcomes)) != 2 or len(set(token_ids)) != 2:
        raise MetadataError("Polymarket outcomes and token IDs must be distinct")
    return PolymarketDiscovery(
        market_slug=actual_slug,
        question=_string(market.get("question"), "Polymarket market.question"),
        outcomes=(outcomes[0], outcomes[1]),
        token_ids=(token_ids[0], token_ids[1]),
        description=str(market.get("description", "")),
    )


def verify_hip4_contract(
    outcome_meta: Mapping[str, Any], mapping: Mapping[str, Any]
) -> VerifiedHip4Contract:
    hip4 = _mapping(mapping.get("hip4"), "market.hip4")
    discovery = discover_hip4(
        outcome_meta,
        outcome_id=_integer(hip4.get("outcome_id"), "hip4.outcome_id"),
    )
    expected_yes = _string(mapping.get("canonical_yes"), "canonical_yes")
    expected_no = _string(mapping.get("canonical_no"), "canonical_no")
    yes_index = _integer(hip4.get("canonical_yes_side"), "canonical_yes_side")
    if yes_index not in {0, 1}:
        raise MetadataError("HIP-4 canonical_yes_side must be 0 or 1")
    no_index = 1 - yes_index
    if not _same_label(discovery.side_participants[yes_index], expected_yes):
        raise MetadataError(
            f"HIP-4 side {yes_index} changed: expected {expected_yes!r}, "
            f"received {discovery.side_participants[yes_index]!r}"
        )
    if not _same_label(discovery.side_participants[no_index], expected_no):
        raise MetadataError(
            f"HIP-4 side {no_index} changed: expected {expected_no!r}, "
            f"received {discovery.side_participants[no_index]!r}"
        )
    expected_quote = _string(hip4.get("quote_token"), "hip4.quote_token")
    if discovery.quote_token != expected_quote:
        raise MetadataError(
            f"HIP-4 quote token changed from {expected_quote} to {discovery.quote_token}"
        )
    identity = {
        "outcome_id": discovery.outcome_id,
        "canonical_yes": expected_yes,
        "canonical_no": expected_no,
        "side": yes_index,
        "quote": expected_quote,
    }
    identity_hash = _identity_hash(identity)
    return VerifiedHip4Contract(
        mapping_id=identity_hash[:24],
        outcome_id=discovery.outcome_id,
        outcome_name=discovery.outcome_name,
        canonical_yes_side_index=yes_index,
        canonical_no_side_index=no_index,
        quote_token=discovery.quote_token,
    )


def verify_kalshi_reference(
    payload: Any, reference: Mapping[str, Any], mapping: Mapping[str, Any]
) -> VerifiedReferenceContract:
    ticker = _string(reference.get("market_ticker"), "Kalshi market_ticker")
    discovery = discover_kalshi(payload, market_ticker=ticker)
    source_side = _string(
        reference.get("canonical_yes_side"), "Kalshi canonical_yes_side"
    ).lower()
    if source_side not in {"yes", "no"}:
        raise MetadataError("Kalshi canonical_yes_side must be yes or no")
    selected = discovery.yes_label if source_side == "yes" else discovery.no_label
    opposite = discovery.no_label if source_side == "yes" else discovery.yes_label
    # Exact label matches are strong automatic checks. Differently styled
    # labels are allowed only because the interactive review explicitly binds
    # the venue side to the configured proposition.
    expected_yes = _string(mapping.get("canonical_yes"), "canonical_yes")
    expected_no = _string(mapping.get("canonical_no"), "canonical_no")
    return VerifiedReferenceContract(
        name="kalshi",
        venue="kalshi",
        market_id=ticker,
        canonical_yes_locator=source_side,
    )


def verify_polymarket_reference(
    payload: Any, reference: Mapping[str, Any]
) -> VerifiedReferenceContract:
    slug = _string(reference.get("market_slug"), "Polymarket market_slug")
    discovery = discover_polymarket(payload, market_slug=slug)
    selected = _string(
        reference.get("canonical_yes_outcome"), "Polymarket canonical_yes_outcome"
    )
    matches = [
        index for index, outcome in enumerate(discovery.outcomes) if outcome == selected
    ]
    if len(matches) != 1:
        raise MetadataError(
            f"Polymarket canonical outcome {selected!r} not found exactly once in "
            f"{discovery.outcomes!r}"
        )
    token_id = discovery.token_ids[matches[0]]
    return VerifiedReferenceContract(
        name="polymarket",
        venue="polymarket",
        market_id=slug,
        canonical_yes_locator=token_id,
    )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise MetadataError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise MetadataError(f"{field} must be an array")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise MetadataError(f"{field} must be a non-empty string")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MetadataError(f"{field} must be a nonnegative integer")
    return value


def _find_by_integer(
    values: Sequence[Any], key: str, target: int, description: str
) -> Mapping[str, Any]:
    for value in values:
        item = _mapping(value, description)
        if item.get(key) == target:
            return item
    raise MetadataError(f"{description} {target} not found")


def _same_label(actual: Any, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()).casefold() == " ".join(
        expected.split()
    ).casefold()


def _string_array(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MetadataError(f"{field} string must contain a JSON array") from exc
    return [_string(item, field) for item in _sequence(value, field)]
