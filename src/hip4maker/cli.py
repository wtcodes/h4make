"""Command-line interface for hip4maker."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import signal
import sys
from pathlib import Path
from typing import Sequence

from hip4maker.config import ConfigLoadError, load_config, validate_config
from hip4maker.hip4 import outcome_asset_id, outcome_coin, outcome_encoding, outcome_token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hip4maker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a JSON configuration")
    validate.add_argument("config", type=Path)
    validate.add_argument(
        "--require-ready",
        action="store_true",
        help="treat unset mappings and disabled trading as errors",
    )
    validate.add_argument("--json", action="store_true", dest="json_output")

    ids = subparsers.add_parser("ids", help="derive HIP-4 identifiers")
    ids.add_argument("outcome_id", type=int)
    ids.add_argument("side", type=int, choices=(0, 1))

    run = subparsers.add_parser("run", help="run the HIP-4 market maker")
    run.add_argument("config", type=Path)
    run.add_argument(
        "--mode",
        choices=("dry-run", "testnet", "mainnet"),
        default="dry-run",
        help="execution mode (default: dry-run; dry-run reads testnet data)",
    )
    run.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="number of polling cycles; 0 runs until interrupted",
    )
    run.add_argument(
        "--output",
        type=Path,
        help="output prefix for PREFIX_orders.jsonl and PREFIX_basis.jsonl",
    )
    run.add_argument(
        "--kalshi-credentials",
        type=Path,
        help="credentials JSON enabling the authenticated Kalshi order-book WebSocket",
    )

    review = subparsers.add_parser(
        "review-mapping",
        help="infer venue sides, display them, and require manual verification",
    )
    review.add_argument("config", type=Path)
    review.add_argument(
        "--mode",
        choices=("testnet", "mainnet"),
        default="testnet",
        help="Hyperliquid network whose HIP-4 metadata is reviewed (default: testnet)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return _validate(args.config, args.require_ready, args.json_output)
    if args.command == "ids":
        return _ids(args.outcome_id, args.side)
    if args.command == "run":
        return _run(
            args.config,
            args.cycles,
            args.output,
            args.mode,
            args.kalshi_credentials,
        )
    if args.command == "review-mapping":
        return _review_mapping(args.config, args.mode)
    raise AssertionError(f"unhandled command {args.command}")


def _validate(path: Path, require_ready: bool, json_output: bool) -> int:
    try:
        config = load_config(path)
    except ConfigLoadError as exc:
        if json_output:
            print(json.dumps({"valid": False, "ready": False, "error": str(exc)}))
        else:
            print(f"ERROR {exc}", file=sys.stderr)
        return 2

    report = validate_config(config, require_ready=require_ready)
    if json_output:
        print(
            json.dumps(
                {
                    "valid": report.structurally_valid,
                    "ready": report.trade_ready,
                    "issues": [
                        {
                            "severity": issue.severity,
                            "path": issue.path,
                            "message": issue.message,
                        }
                        for issue in report.issues
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for issue in report.issues:
            print(f"{issue.severity.upper():9} {issue.path}: {issue.message}")
        if report.structurally_valid:
            status = "trade-ready" if report.trade_ready else "valid template; not trade-ready"
            print(f"OK {path}: {status}")
        else:
            print(f"INVALID {path}", file=sys.stderr)

    return 0 if report.structurally_valid else 1


def _ids(outcome_id: int, side: int) -> int:
    payload = {
        "outcome_id": outcome_id,
        "side": side,
        "encoding": outcome_encoding(outcome_id, side),
        "coin": outcome_coin(outcome_id, side),
        "token": outcome_token(outcome_id, side),
        "asset_id": outcome_asset_id(outcome_id, side),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _run(
    path: Path,
    cycles: int,
    output: Path | None,
    mode: str,
    kalshi_credentials: Path | None,
) -> int:
    from hip4maker.actions import ActionSubmissionError
    from hip4maker.metadata import MetadataError
    from hip4maker.recording import JsonlRecorder
    from hip4maker.runner import MarketMakerBot, RunnerError
    from hip4maker.transport import TransportError

    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def stop_on_sigterm(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        try:
            config = load_config(path)
            orders_path, basis_path = _output_paths(output)
            with ExitStack() as stack:
                recorder = stack.enter_context(JsonlRecorder(orders_path))
                basis_recorder = stack.enter_context(JsonlRecorder(basis_path))
                bot = MarketMakerBot(
                    config,
                    recorder,
                    basis_recorder=basis_recorder,
                    mode=mode,
                    kalshi_credentials=kalshi_credentials,
                )
                try:
                    bot.initialize()
                    bot.run(cycles=cycles)
                finally:
                    bot.close()
        except (
            ActionSubmissionError,
            ConfigLoadError,
            RunnerError,
            MetadataError,
            TransportError,
            OSError,
        ) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


def _output_paths(output: Path | None) -> tuple[Path | None, Path]:
    if output is None:
        return None, Path("basis.jsonl")
    if output.suffix == ".jsonl":
        raise ConfigLoadError(
            "--output is a prefix, not a filename; use --output without .jsonl"
        )
    return (
        output.with_name(f"{output.name}_orders.jsonl"),
        output.with_name(f"{output.name}_basis.jsonl"),
    )


def _review_mapping(path: Path, mode: str) -> int:
    from hip4maker.metadata import MetadataError
    from hip4maker.review import MappingReviewError, review_mapping
    from hip4maker.transport import TransportError

    try:
        approved = review_mapping(path, mode=mode)
    except (ConfigLoadError, MappingReviewError, MetadataError, TransportError, OSError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    return 0 if approved else 1
