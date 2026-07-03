# Running Sentinel Edge on Hyperliquid

Hyperliquid has **real, market-clearing funding** (measured cross-sectional dispersion ≈ **52× KoinBay's**,
~4.5× the Binance data the backtests used) and **cheaper fees** (maker ~1.5 bps / taker ~4.5 bps vs
KoinBay's 2.5 / 7.5). The funding-carry edge that is *dead* on KoinBay is alive here. Our whale/"smart-money"
overlay already sources Hyperliquid, so on HL it becomes a native, tradeable signal.

The port is a **drop-in adapter**, not a rewrite: `HyperliquidFutures` implements the same read surface as
`KoinbayFutures`, selected by `exchange.venue` via `exchange/factory.py`. The engine, strategies, market-data
and dashboard are venue-agnostic.

---

## Phase 1 — data + PAPER (enabled now, no keys, no risk)

Everything read-only works today against Hyperliquid's public `info` API.

```bash
# paper-trade carry on Hyperliquid data
python run.py loop --config config.hyperliquid.yaml

# dashboard for it (venue-aware; shows HL marks/funding)
python run.py dashboard --config config.hyperliquid.yaml --port 8790
```

To point any existing config at Hyperliquid, just add to its `exchange:` block:
```yaml
exchange:
  venue: hyperliquid
```

Notes:
- HL sizes are coin units with per-coin `szDecimals`; the adapter maps `szDecimals → multiplier = 10^-szDecimals`
  so the existing integer-"contract" sizing math is exact (1 contract = one size tick).
- HL funding is **hourly** (KoinBay was 8h). Carry *ranks* on it identically; interval-aware funding **accrual**
  is handled in the Phase-2 execution layer.

---

## Phase 2 — live execution (secure, testnet-first, double-opt-in)

Live orders on Hyperliquid are signed with an **EIP-712 wallet signature**. Do **not** use your main wallet key.

1. **Create an Agent (API) wallet** in the Hyperliquid UI → *More → API*. It can **trade but cannot withdraw**.
   This is the designed-for-bots credential and strictly limits blast radius.
2. **Put it in `.env`** (git-ignored, never committed, never shared):
   ```
   HL_ACCOUNT_ADDRESS=0xYourMainAccountAddress
   HL_AGENT_KEY=0xAgentWalletPrivateKey
   ```
3. **Install the official SDK**: `pip install hyperliquid-python-sdk` (handles the msgpack + EIP-712 signing).
4. **Validate on TESTNET first** (`https://api.hyperliquid-testnet.xyz`) — free test USDC, zero real risk.
5. **Go live only when you choose**: `mode: live` in the config **and** the `--live` CLI flag (the existing
   double opt-in). The execution methods in `HyperliquidFutures` stay `NotImplementedError` until this layer
   is built and wired, so paper can never accidentally place a real order.

### Security posture
- Read-only data and paper need **no key**.
- Live uses an **agent wallet** (trade-only, no withdrawal) — a compromised bot cannot drain funds.
- Keys live in `.env` only. The agent's account address is public; the agent private key never leaves your machine.
