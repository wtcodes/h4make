"""Interactive, read-only market-side discovery and manual approval."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from hip4maker.clients import (
    KALSHI_REST_URL,
    POLYMARKET_CLOB_URL,
    POLYMARKET_GAMMA_URL,
    HyperliquidInfoClient,
    KalshiReadOnlyClient,
    PolymarketReadOnlyClient,
    ReadOnlyTransport,
)
from hip4maker.config import load_config, validate_config
from hip4maker.hip4 import outcome_coin
from hip4maker.metadata import (
    Hip4Discovery,
    KalshiDiscovery,
    MetadataError,
    PolymarketDiscovery,
    discover_hip4,
    discover_kalshi,
    discover_polymarket,
)
from hip4maker.transport import ReadOnlyHttpTransport


class MappingReviewError(RuntimeError):
    pass


HYPERLIQUID_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
HYPERLIQUID_MAINNET_URL = "https://api.hyperliquid.xyz"


def review_mapping(
    path: str | Path,
    *,
    mode: str = "testnet",
    transport: ReadOnlyTransport | None = None,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
) -> bool:
    """Discover, display, confirm, and atomically store one mapping.

    No file modification occurs unless all confirmations pass and the user
    explicitly approves the final write.
    """

    config_path = Path(path)
    config = load_config(config_path)
    report = validate_config(config, require_ready=False)
    if not report.structurally_valid:
        details = "; ".join(f"{item.path}: {item.message}" for item in report.errors)
        raise MappingReviewError(f"configuration is structurally invalid: {details}")
    mapping = config["market"]
    hip4 = mapping["hip4"]
    outcome_id = _required_integer(hip4.get("outcome_id"), "hip4.outcome_id")

    try:
        hyperliquid_url = {
            "testnet": HYPERLIQUID_TESTNET_URL,
            "mainnet": HYPERLIQUID_MAINNET_URL,
        }[mode]
    except KeyError as exc:
        raise MappingReviewError("review mode must be 'testnet' or 'mainnet'") from exc

    selected_transport = transport or ReadOnlyHttpTransport()
    hl = HyperliquidInfoClient.create(hyperliquid_url, selected_transport)
    response = hl.outcome_meta()
    if not isinstance(response.payload, dict):
        raise MappingReviewError("Hyperliquid outcomeMeta response must be an object")
    discovered_hip4 = discover_hip4(response.payload, outcome_id=outcome_id)

    references = mapping["references"]
    kalshi: KalshiDiscovery | None = None
    polymarket: PolymarketDiscovery | None = None
    if "kalshi" in references and references["kalshi"]["enabled"]:
        ticker = _required_string(
            references["kalshi"].get("market_ticker"), "kalshi.market_ticker"
        )
        client = KalshiReadOnlyClient.create(KALSHI_REST_URL, selected_transport)
        kalshi = discover_kalshi(client.market(ticker).payload, market_ticker=ticker)
    if "polymarket" in references and references["polymarket"]["enabled"]:
        slug = _required_string(
            references["polymarket"].get("market_slug"), "polymarket.market_slug"
        )
        client = PolymarketReadOnlyClient.create(
            POLYMARKET_GAMMA_URL, POLYMARKET_CLOB_URL, selected_transport
        )
        polymarket = discover_polymarket(
            client.market_by_slug(slug).payload, market_slug=slug
        )

    canonical_yes = mapping.get("canonical_yes") or discovered_hip4.side_participants[0]
    canonical_no = mapping.get("canonical_no") or discovered_hip4.side_participants[1]
    hip4_side = _infer_hip4_side(
        discovered_hip4, canonical_yes, canonical_no, input_fn, output
    )
    kalshi_side = (
        _infer_kalshi_side(kalshi, canonical_yes, canonical_no, input_fn, output)
        if kalshi is not None
        else None
    )
    polymarket_outcome = (
        _infer_polymarket_outcome(
            polymarket, canonical_yes, canonical_no, input_fn, output
        )
        if polymarket is not None
        else None
    )

    _print_proposal(
        output,
        mode,
        discovered_hip4,
        hip4_side,
        kalshi,
        kalshi_side,
        polymarket,
        polymarket_outcome,
    )
    confirmations = [
        (
            "Is this strictly a two-outcome event with no possible tie or draw? [y/N] ",
            "two-outcome scope was not confirmed",
        ),
        (
            f"Does HIP-4 side {hip4_side} mean {canonical_yes!r} wins? [y/N] ",
            "HIP-4 side mapping was not confirmed",
        ),
    ]
    if kalshi is not None:
        confirmations.append(
            (
                f"Does Kalshi {kalshi_side.upper()} mean {canonical_yes!r} wins? [y/N] ",
                "Kalshi side mapping was not confirmed",
            )
        )
    if polymarket is not None:
        confirmations.append(
            (
                f"Does Polymarket outcome {polymarket_outcome!r} mean "
                f"{canonical_yes!r} wins? [y/N] ",
                "Polymarket side mapping was not confirmed",
            )
        )
    for prompt, reason in confirmations:
        if not _yes(input_fn(prompt)):
            _write(output, f"ABORTED: {reason}. Nothing was written.\n")
            return False
    typed = input_fn(f"Type {canonical_yes!r} to confirm canonical YES: ").strip()
    if typed != canonical_yes:
        _write(output, "ABORTED: canonical YES text did not match. Nothing was written.\n")
        return False
    if not _yes(input_fn(f"Write the verified mapping to {config_path}? [y/N] ")):
        _write(output, "ABORTED: write not approved. Nothing was written.\n")
        return False

    mapping["canonical_yes"] = canonical_yes
    mapping["canonical_no"] = canonical_no
    hip4["canonical_yes_side"] = hip4_side
    hip4["quote_token"] = discovered_hip4.quote_token
    if kalshi_side is not None:
        references["kalshi"]["canonical_yes_side"] = kalshi_side
    if polymarket_outcome is not None:
        references["polymarket"]["canonical_yes_outcome"] = polymarket_outcome
    mapping["reviewed"] = True
    _atomic_write_json(config_path, config)
    _write(output, f"VERIFIED: mapping written to {config_path}.\n")
    return True


def _infer_hip4_side(
    discovery: Hip4Discovery,
    canonical_yes: str,
    canonical_no: str,
    input_fn: Callable[[str], str],
    output: TextIO,
) -> int:
    matches = [
        index
        for index, participant in enumerate(discovery.side_participants)
        if _label_matches(participant, canonical_yes)
        and _label_matches(discovery.side_participants[1 - index], canonical_no)
    ]
    if len(matches) == 1:
        return matches[0]
    _write(
        output,
        "HIP-4 canonical side could not be inferred from its participant labels.\n",
    )
    selected = _choose("HIP-4 side", ("0", "1"), input_fn)
    return int(selected)


def _infer_kalshi_side(
    discovery: KalshiDiscovery,
    canonical_yes: str,
    canonical_no: str,
    input_fn: Callable[[str], str],
    output: TextIO,
) -> str:
    yes_matches = _label_matches(discovery.yes_label, canonical_yes) and _label_matches(
        discovery.no_label, canonical_no
    )
    no_matches = _label_matches(discovery.no_label, canonical_yes) and _label_matches(
        discovery.yes_label, canonical_no
    )
    if yes_matches != no_matches:
        return "yes" if yes_matches else "no"
    _write(
        output,
        "Kalshi canonical side was ambiguous from its displayed labels.\n",
    )
    return _choose("Kalshi", ("yes", "no"), input_fn).lower()


def _infer_polymarket_outcome(
    discovery: PolymarketDiscovery,
    canonical_yes: str,
    canonical_no: str,
    input_fn: Callable[[str], str],
    output: TextIO,
) -> str:
    matches = [
        outcome for outcome in discovery.outcomes if _label_matches(outcome, canonical_yes)
    ]
    if len(matches) == 1:
        return matches[0]
    normalized_outcomes = {_normalized(outcome): outcome for outcome in discovery.outcomes}
    question = _normalized(discovery.question)
    yes_in_question = _normalized(canonical_yes) in question
    no_in_question = _normalized(canonical_no) in question
    if set(normalized_outcomes) == {"yes", "no"} and yes_in_question != no_in_question:
        return normalized_outcomes["yes" if yes_in_question else "no"]
    _write(
        output,
        "Polymarket canonical outcome was ambiguous from its outcomes and question.\n",
    )
    return _choose("Polymarket", discovery.outcomes, input_fn)


def _print_proposal(
    output: TextIO,
    mode: str,
    hip4: Hip4Discovery,
    hip4_side: int,
    kalshi: KalshiDiscovery | None,
    kalshi_side: str | None,
    polymarket: PolymarketDiscovery | None,
    polymarket_outcome: str | None,
) -> None:
    lines = [
        "",
        "DISCOVERED MARKET MAPPING",
        "=========================",
        "",
        "HIP-4",
        f"  Network: {mode}",
        f"  Standalone outcome: {hip4.outcome_name} (outcome {hip4.outcome_id})",
        f"  Side 0: {hip4.side_labels[0]!r} = {hip4.side_participants[0]!r}",
        f"  Side 1: {hip4.side_labels[1]!r} = {hip4.side_participants[1]!r}",
        f"  Side 0 coin: {outcome_coin(hip4.outcome_id, 0)}",
        f"  Side 1 coin: {outcome_coin(hip4.outcome_id, 1)}",
        f"  Proposed canonical YES side: {hip4_side}",
        f"  Quote token: {hip4.quote_token}",
        "  Outcome terms:",
        _indented(hip4.terms),
    ]
    if kalshi is not None:
        lines.extend(
            [
                "",
                "KALSHI",
                f"  Ticker: {kalshi.market_ticker}",
                f"  Title: {kalshi.title}",
                f"  YES label: {kalshi.yes_label}",
                f"  NO label:  {kalshi.no_label}",
                f"  Proposed canonical YES side: {kalshi_side}",
                "  Rules:",
                _indented(kalshi.rules),
            ]
        )
    if polymarket is not None:
        lines.extend(
            [
                "",
                "POLYMARKET",
                f"  Slug: {polymarket.market_slug}",
                f"  Question: {polymarket.question}",
                f"  Outcome 0: {polymarket.outcomes[0]!r}",
                f"  Token 0:   {polymarket.token_ids[0]}",
                f"  Outcome 1: {polymarket.outcomes[1]!r}",
                f"  Token 1:   {polymarket.token_ids[1]}",
                f"  Proposed canonical YES outcome: {polymarket_outcome!r}",
                "  Description:",
                _indented(polymarket.description),
            ]
        )
    lines.extend(
        [
            "",
            "NORMALIZED PROPOSITION",
            f"  Canonical YES: {hip4.side_participants[hip4_side]}",
            f"  Canonical NO:  {hip4.side_participants[1 - hip4_side]}",
            "",
        ]
    )
    _write(output, "\n".join(lines))


def _choose(
    venue: str, options: tuple[str, str], input_fn: Callable[[str], str]
) -> str:
    prompt = f"Which {venue} value represents canonical YES, {options[0]!r} or {options[1]!r}? "
    answer = input_fn(prompt).strip()
    exact = [option for option in options if answer.casefold() == option.casefold()]
    if len(exact) != 1:
        raise MappingReviewError(f"invalid {venue} side selection; review aborted")
    return exact[0]


def _label_matches(actual: str, expected: str) -> bool:
    left = _normalized(actual)
    right = _normalized(expected)
    return left == right or (len(right) >= 4 and right in left)


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _indented(value: str) -> str:
    text = value.strip() or "(not supplied by venue)"
    return "\n".join(f"    {line}" for line in text.splitlines())


def _yes(value: str) -> bool:
    return value.strip().casefold() in {"y", "yes"}


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise MappingReviewError(f"{field} must be filled before mapping review")
    return value


def _required_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MappingReviewError(f"{field} must be filled before mapping review")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _write(output: TextIO, value: str) -> None:
    output.write(value)
    output.flush()
