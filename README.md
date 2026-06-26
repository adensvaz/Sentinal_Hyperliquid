# Sentinel Edge — KoinBay Copy-Trading Bot

> Market-neutral long/short crypto futures strategy, modeled on Gamma's **Sentiment Edge** vault.  
> Runs on **KoinBay futures**. Goal: become a **copy-trading lead** (signal provider).

---

## Architecture

```mermaid
flowchart TD
    classDef data fill:#1e3a5f,stroke:#4a9eff,color:#e8f4ff,font-weight:bold
    classDef signal fill:#1a3a2a,stroke:#27d796,color:#e8fff4,font-weight:bold
    classDef portfolio fill:#2a1f3d,stroke:#a78bfa,color:#f0e8ff,font-weight:bold
    classDef risk fill:#3d1a1a,stroke:#ff5d6c,color:#ffe8e8,font-weight:bold
    classDef exec fill:#1f2d3d,stroke:#60a5fa,color:#e8f0ff,font-weight:bold
    classDef state fill:#1a2a1a,stroke:#86efac,color:#e8ffe8,font-weight:bold
    classDef good fill:#14532d,stroke:#22c55e,color:#dcfce7,font-weight:bold
    classDef warn fill:#7c2d12,stroke:#f97316,color:#ffedd5,font-weight:bold

    KB[("🏦 KoinBay Futures\nLive Market Data")]:::data
    HL[("🔗 Hyperliquid\nLeaderboard")]:::data

    KB --> MOM["📈 Momentum\n4h · 1d · 7d horizons\nweight 0.4"]:::signal
    KB --> FUND["💸 Funding / Crowding\nfade crowded longs\nweight 0.2"]:::signal
    KB --> VOL["📊 Volume Surge\nrecent vs trailing avg\nweight 0.1"]:::signal
    HL --> WHALE["🐋 Whale Overlay\n10 wallets · win-rate gated\ndaily refresh · weight 0.3\nbeta-demeaned at source"]:::signal

    MOM & FUND & VOL & WHALE --> RESID["⚡ Residual Momentum\nStrip BTC-beta from scores BEFORE ranking\nbook born market-neutral at selection stage\nSharpe 4.77 → 5.01 in backtest"]:::signal

    RESID --> RANK["🎯 Cross-sectional Rank\n24 liquid USDT perps"]:::portfolio

    RANK --> LONGB["🟢 LONG\nTop 5 names"]:::good
    RANK --> SHORTB["🔴 SHORT\nBottom 5 names"]:::warn

    LONGB & SHORTB --> SIZE["⚖️ Conviction Weighting\nscore^1.5 · max 25% per name"]:::portfolio
    SIZE --> BETAH["🧲 BTC-Beta Neutralization\nβ 0.135 → 0.072 · net rail ≤5%"]:::portfolio
    BETAH --> ADVC["💧 ADV Liquidity Cap\nmax 5% of daily volume\ndollar-neutral hard clamp"]:::portfolio

    ADVC --> DRISK["📉 Dynamic De-risk\nvol-target + DD throttle\n25–100% gross scale"]:::risk
    ADVC --> CRASH["💥 Crash Guard\nbear + violent bounce regime\ngross cut to 50% floor"]:::risk
    ADVC --> DAILY["⚡ Daily Circuit Breaker\ndown ≥3% → next day at 50%\nSharpe 4.19 → 4.32"]:::risk
    ADVC --> KILL["🚨 Kill-switch\nDD >15% → flatten all\n12h cooling pause"]:::risk
    ADVC --> PSTOP["🛑 Per-position Stop\nloss >6% equity\nclose that name only"]:::risk

    DRISK & CRASH & DAILY & KILL & PSTOP --> RECON["🔄 Reconcile\ntarget vs live → minimal delta orders\nmaker POST_ONLY · anti-churn band"]:::exec

    RECON --> PAPER["📄 PAPER MODE\ndefault · zero risk\nreal signals · simulated fills\nbuilds real track record"]:::good
    RECON --> LIVE["🔴 LIVE MODE\nmode: live + --live flag\nreal KoinBay futures orders\npreflight auth check"]:::warn

    PAPER & LIVE --> DB[("💾 SQLite WAL Store\nequity curve · trades\nfills · scores · funding")]:::state
    DB --> DASH["🖥️ Dashboard :8787\nPnL · Positions · Smart Money\nequity chart · trade history"]:::state
    DB --> COPY["📡 Copy-trading Lead\nfollowable book ≤10×\nKoinBay signal provider"]:::good
```

---

## Backtest Results (Jan → Jun 2026, real KoinBay daily data)

| Metric | Value |
|---|---|
| Net return | **+48.4%** |
| Sharpe ratio | **~5.0** |
| Max drawdown | ~12.6% |
| Worst month | ~−2.5% |
| Book BTC-beta | **0.07** (near-zero) |
| Trades | 503 closed round-trips |

> ⚠️ Backtest caveats: survivorship bias, assumes maker fills, no historical whale/funding data.
> Relative improvements between configs are robust; absolute returns are optimistic.

---

## Signal Stack

```
Raw KoinBay data
      │
      ├─ Momentum (40%)   risk-adjusted cross-sectional, horizons: 1d / 3d / 7d / 14d
      ├─ Funding  (20%)   z(-currentFundRate) — fade crowded longs (contrarian)
      ├─ Volume   (10%)   recent surge vs trailing baseline
      └─ Whales   (30%)   blended net positioning from 10 top Hyperliquid wallets
                           (win-rate gated, daily refresh, beta-demeaned at source)
      │
      ▼
  Beta-orthogonalization → residual scores (strip BTC-beta before ranking)
      │
      ▼
  Cross-sectional z-score → continuous rank → long top-5 / short bottom-5
```

---

## Safety Model

| Layer | What it does |
|---|---|
| Paper mode (default) | Real signals, simulated fills — zero funds at risk |
| Net-rail clamp | `\|net\|/equity` hard-clamped to ≤5% every cycle |
| Beta-hedge | Post-construction BTC-beta neutralization |
| Dynamic de-risk | Vol-targeting + drawdown throttle cuts gross 25–100% |
| Crash guard | Bear + violent-bounce regime → additional gross cut |
| Daily circuit-breaker | Day down ≥3% → next day runs at 50% gross |
| Kill-switch | Drawdown >15% → flatten all + 12h pause |
| Per-position stop | Single name loss >6% equity → close it |

---

## Quick Start

```bash
# 1. Install
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env      # add KOINBAY_API_KEY + KOINBAY_API_SECRET

# 3. Run (paper mode — safe)
python run.py selftest    # verify connection + print proposed book
python run.py loop        # start the daily rebalance loop
python run.py dashboard   # open dashboard at http://127.0.0.1:8787

# 4. Go live (only after reviewing paper track record)
# set mode: live in config.yaml, then:
python run.py once --live
```

---

## Project Structure

```
GAMMA_SENTIMENTEDGE/
├── config.yaml                    # all strategy knobs (secrets in .env only)
├── run.py                         # CLI entrypoint
├── sentinel                       # shell helper (start/stop/status)
├── src/sentinel/
│   ├── config.py                  # pydantic config + .env overlay
│   ├── exchange/                  # KoinBay client, signing, contract specs
│   ├── signal/
│   │   ├── market_proxy.py        # momentum + funding + volume + whale scorer
│   │   ├── whale_tracker.py       # Hyperliquid top-wallet consensus
│   │   └── whale_select.py        # win-rate-gated wallet screening
│   ├── strategy/
│   │   ├── sentiment_edge.py      # scores → dollar-neutral target book
│   │   ├── sizing.py              # weights, caps, vol-adjust, net-clamp
│   │   └── portfolio.py           # beta math: compute_betas, demean_by_beta, dispersion
│   ├── risk/limits.py             # all risk functions (pure, testable)
│   ├── execution/                 # paper + live broker, reconciler
│   ├── engine/engine.py           # full rebalance pipeline
│   ├── state/store.py             # SQLite WAL store (equity, trades, fills, funding)
│   └── dashboard/server.py        # local web dashboard
├── scripts/
│   ├── backfill.py                # seed dashboard with backtest history
│   └── bt_compare.py             # A/B harness (used to validate every feature)
├── tests/                         # 115 tests
└── docs/KOINBAY_API.md            # live-verified KoinBay API reference
```

---

## Disclaimer

Trading futures is risky. This is software, not financial advice. You are responsible for your keys,
capital, and live orders. Always start in paper mode and review the track record before going live.
