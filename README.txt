HIP4MAKER
=========

hip4maker is a Python market maker for one two-sided HIP-4 sports market.
It supports dry-run, Hyperliquid testnet, and Hyperliquid mainnet.

The first version is for straight win/lose outcomes only. It does not support
questions, draws, or markets with more than two possible results.


INSTALL
-------

Python 3.11 or newer is required.

  python3 -m venv .venv
  . .venv/bin/activate
  python -m pip install -e .

Dependencies are declared in pyproject.toml. A requirements.txt file is not
needed.


CONFIGURE A MARKET
------------------

Start from:

  config/hip4_rel_wide_mm.json

The configuration has three sections:

  market   HIP-4 outcome and reference-market mapping
  risk     startup inventory and maximum position
  trader   basis, spread, order size, and ladder settings

At minimum, fill in:

  market.scheduled_start_utc
  market.hip4.outcome_id
  market.references.kalshi.market_ticker

Set enabled to false for any unused reference. API URLs and asset identifiers
are discovered automatically.


VERIFY THE SIDE MAPPING
-----------------------

Never set the YES/NO mapping by inspection alone. Let the review command fetch
both venue sides, then manually confirm every proposed mapping.

For testnet:

  hip4maker review-mapping my_market.json --mode testnet

For mainnet:

  hip4maker review-mapping my_market.json --mode mainnet

The command updates the configuration only after all confirmations succeed.
Validate the resulting file with:

  hip4maker validate --require-ready my_market.json

A configuration reviewed against testnet must not be reused on mainnet.


CREDENTIALS
-----------

Hyperliquid live modes use a non-delegated wallet private key. Keep the JSON
file outside this repository:

  {
    "secret_key": "0x..."
  }

Point the bot to it with HL_CREDENTIALS_FILE. The account address is derived
from the key; separate master, vault, and delegated addresses are not
supported.

Kalshi public REST needs no credentials. To use the lower-latency Kalshi
WebSocket, create a second external JSON file:

  {
    "key_id": "your-kalshi-api-key-id",
    "private_key_path": "/path/to/kalshi-private-key.pem"
  }

api_key_id may be used instead of key_id. The private key path may be absolute
or relative to the credentials JSON file.


RUN
---

Dry-run against testnet data:

  HL_CREDENTIALS_FILE=/path/to/hyperliquid.json \
    ./run.sh my_market.json --mode dry-run --output run

Dry-run with the Kalshi WebSocket:

  HL_CREDENTIALS_FILE=/path/to/hyperliquid.json \
    ./run.sh my_market.json --mode dry-run \
      --kalshi-credentials /path/to/kalshi.json --output run

Submit orders on testnet:

  HL_CREDENTIALS_FILE=/path/to/hyperliquid.json \
    ./run.sh my_market.json --mode testnet \
      --kalshi-credentials /path/to/kalshi.json --output run

Use --mode mainnet only with a mainnet-reviewed configuration.

The process runs until interrupted. Use --cycles N for a finite dry run.
Set PYTHON_BIN if the desired Python is not currently active:

  PYTHON_BIN=/path/to/venv/bin/python ./run.sh my_market.json --mode dry-run

Dry-run without HL_CREDENTIALS_FILE can read market data, but it sees no
account inventory and therefore normally cannot construct funded quotes.


OUTPUT
------

--output takes a prefix, not a filename:

  --output run

This writes:

  run_orders.jsonl   orders, acknowledgements, fills, and stream events
  run_basis.jsonl    compact basis and inventory state every 30 seconds

Existing files are appended to. Use a new prefix when starting a distinct run.


TRADING BEHAVIOR
----------------

Kalshi is the reference by default. Public REST is used unless WebSocket
credentials are supplied; the bot falls back to REST while that socket is
unavailable. Empty binary-book sides are bounded by bid 0 or ask 1 with zero
displayed size.

All prices are normalized to the manually verified canonical YES side:

  basis      = Hyperliquid midpoint - reference midpoint
  fair value = reference midpoint + basis_apply_fraction * basis estimate

The Hyperliquid midpoint uses the full published book, including this bot's
own resting quotes.

At startup, the bot performs at most one split to reach
risk.startup_complete_sets. It does not split again as inventory changes.
Quotes are limited by free quote tokens, side-token inventory, and
risk.max_position.

The front quotes are place_thresh from the inventory-adjusted fair value.
Back-level spacing is:

  place_thresh * rung_thresh_mult

place_back_levels defaults to true, which proactively places every funded
rung. Price cancellation is asymmetric: an order is canceled when fair value
moves toward or through its cancel threshold, but is left resting when fair
value moves away from it.

Hyperliquid requires at least 10 quote tokens of native order notional. When a
YES-token order would be too small, the bot routes the same exposure through
the complementary NO token when possible.

Fills and order acknowledgements are consumed from Hyperliquid WebSockets and
reconciled with REST account state. Placements and cancellations are batched.


SHUTDOWN AND OPERATING LIMITS
-----------------------------

Ctrl-C, SIGTERM, and normal completion trigger one batch cancellation for this
process's open orders, followed by REST confirmation.

There is no dead-man switch. SIGKILL, machine failure, or network failure can
leave orders resting. Because CLOIDs are unique to a process instance, a new
process will not adopt orders left by an earlier crashed process. Inspect and
cancel those orders directly on Hyperliquid before restarting.

Run only one instance per account and market when relying on max_position.
Separate instances do not combine their outstanding-order exposure into one
risk calculation.

scheduled_start_utc identifies the event; it is not a start or stop gate. The
bot quotes during the game and does not automatically stop at resolution.

Treat this project as beta software. Prove each configuration in dry-run and
testnet before considering mainnet.
