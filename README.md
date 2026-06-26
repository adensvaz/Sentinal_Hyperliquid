# Sentinel Edge

A market-neutral, long/short crypto strategy modeled on Gamma's **Sentiment Edge** Hyperliquid vault,
executing on **KoinBay futures**.

It scores every liquid USDT-margined perpetual with a single **continuous score**, ranks the universe,
goes **long the top-scored / short the bottom-scored** names, and sizes them so long notional ≈ short
notional (**net exposure ≈ 0**). Each rebalance it diffs the live book and trades only the delta.

> **The goal:** run a clean, followable, ≤10× market-neutral book so the KoinBay account can be enabled
> as a **copy-trading lead** (signal provider) that other KoinBay users mirror.

## How it mirrors Sentiment Edge

| Sentiment Edge (Gamma) | Sentinel Edge (this repo) |
|---|---|
| Continuous sentiment score per asset | Continuous **market-derived proxy** score (momentum + funding/crowding + volume) |
| Cross-sectional rank, long top / short bottom | Same |
| Dollar-neutral (net ≈ 0) | Same — long sleeve notional == short sleeve notional |
| ~1× leverage, isolated margin, periodic rebalance | Configurable gross leverage (≤10×), isolated margin, scheduled rebalance |
| Main risk: funding | Funding-aware filter + funding used as a signal |

The score is a **momentum/flow/crowding proxy**, not true social sentiment — but the signal layer is
pluggable (`SignalProvider`), so a real sentiment feed can be dropped in without touching the rest.

## Safety model

- **`mode: paper` is the default.** Paper mode uses the *real* data, score, sizing, and order logic but
  simulates fills against live bid/ask — it builds a real track-record-shaped equity curve with **zero
  funds at risk.**
- **Live trading needs two opt-ins:** `mode: live` in `config.yaml` **and** the `--live` CLI flag. Live
  runs an auth preflight (`GET /fapi/v1/account`) and refuses to trade if it fails.
- A **max-drawdown kill-switch** flattens and pauses if equity falls past `risk.max_drawdown_pct`.

This tool never flips itself to live and never trades without your explicit `--live`. Review the paper
results first.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then add your KoinBay API key/secret
```

Create keys in KoinBay (Profile → API Management → Create API) with **Futures trade** permission.
Edit `config.yaml` to taste. Secrets stay in `.env`.

## Usage

```bash
python run.py selftest          # read-only: hit live KoinBay, build universe, print the book + orders it WOULD send
python run.py once              # one rebalance cycle (paper by default)
python run.py loop              # rebalance every config.schedule.rebalance_minutes
python run.py status            # current book, net/gross exposure, PnL, drawdown
python run.py dashboard         # local web UI (PnL, equity curve, positions) at http://127.0.0.1:8787
python run.py refresh-whales    # re-screen the HL leaderboard (win-rate gated) -> data/whales.json
python run.py flatten           # close everything

# live (after reviewing paper results and setting mode: live in config.yaml):
python run.py once --live
```

`selftest` works without API keys for the public-data parts; with keys it also does a signed
`GET /fapi/v1/account` to prove your signing works against the live server.

## Architecture

```
src/sentinel/
  config.py            # typed config (pydantic) + .env secrets
  exchange/            # KoinBay client (X-CH-* signing), typed futures endpoints, contract-spec math
  signal/              # SignalProvider interface + market-derived proxy scorer
  strategy/            # scores -> dollar-neutral target book; sizing/neutralization/caps
  risk/                # leverage cap, net tolerance, drawdown kill-switch, funding filter
  execution/           # Broker (paper + live), reconciler (target vs live -> minimal orders)
  engine/              # one rebalance cycle + the scheduler loop
  state/               # sqlite store + equity curve
  util/                # math + logging
docs/KOINBAY_API.md    # live-verified API reference (source of truth)
```

See `docs/KOINBAY_API.md` for the verified endpoint/signing details this is built against.

## Disclaimer

Trading futures is risky and can lose money, including via funding and liquidation. This is software,
not financial advice. You are responsible for any keys, capital, and live orders. Start in paper mode.
