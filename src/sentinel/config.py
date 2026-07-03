"""Typed configuration: loads config.yaml + secrets from .env, validates with pydantic."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator


class ExchangeCfg(BaseModel):
    venue: Literal["koinbay", "hyperliquid"] = "koinbay"   # which exchange to read/trade
    futures_host: str = "https://futuresopenapi.koinbay.com"
    spot_host: str = "https://openapi.koinbay.com"
    hyperliquid_info_url: str = "https://api.hyperliquid.xyz/info"   # HL public data (Phase 1, no keys)
    recv_window_ms: int = 5000
    timeout_s: float = 15.0


class UniverseCfg(BaseModel):
    top_n: int = 24
    allowlist: list[str] = Field(default_factory=list)
    denylist: list[str] = Field(default_factory=list)
    min_quote_volume_usdt: float = 500_000


class SignalWeights(BaseModel):
    momentum: float = 0.5
    funding: float = 0.3
    funding_vel: float = 0.0  # funding velocity (next - current): positioning acceleration (live-only)
    volume: float = 0.2
    whale: float = 0.0      # blended smart-money positioning (0 = off)
    reversal: float = 0.0   # short-horizon mean-reversion: fade recent movers (anti-correlated to momentum)


class SignalCfg(BaseModel):
    momentum_horizons: list[str] = Field(default_factory=lambda: ["4h", "24h", "7d"])
    weights: SignalWeights = Field(default_factory=SignalWeights)
    funding_sign: int = -1  # -1 contrarian (fade crowded funding), +1 trend-follow
    reversal_hours: float = 24.0  # horizon to fade for the reversal factor
    ta_veto: float = 0.0    # TA-confirmation veto (0=off; e.g. 0.3 blocks longs with TA<-0.3 / shorts with TA>+0.3)


class WhalesCfg(BaseModel):
    """Track a basket of top Hyperliquid traders and blend their net positioning into the score.
    The live basket is read from `source_file` (written by `refresh-whales`); `addresses` is the
    fallback seed if that file is absent."""
    enabled: bool = False
    addresses: list[str] = Field(default_factory=list)
    source_file: str = "data/whales.json"
    weighting: Literal["equal", "equity"] = "equal"
    info_url: str = "https://api.hyperliquid.xyz/info"
    refresh_days: int = 7          # auto re-screen the basket when it's older than this
    max_inactive_days: int = 5     # drop wallets with no on-chain fill in this many days (recency)


class PortfolioCfg(BaseModel):
    longs: int = 5
    shorts: int = 5
    weighting: Literal["score", "equal"] = "score"
    conviction: float = 1.0   # weight ~ |score|^conviction; >1 concentrates into the strongest signals
    risk_parity: bool = False # size by inverse volatility (equal-risk): wild coins get smaller allocations
    beta_neutral_scores: bool = False  # rank by RESIDUAL (beta-adjusted) score so the book is born
                                       # market-neutral at selection — residual momentum, not raw
    target_gross_leverage: float = 3.0
    per_name_leverage: int = 3
    max_name_weight: float = 0.25
    beta_neutralize: bool = True       # neutralize the book's BTC-beta (dollar-neutral != market-neutral)
    beta_max_imbalance: float = 0.15   # cap the dollar imbalance introduced when neutralizing beta
    beta_lookback: int = 30            # bars of returns used to estimate each coin's BTC-beta
    adv_cap_frac: float = 0.0          # cap each name's notional at this fraction of its ADV (0 = off);
                                       # capacity guardrail for copy-trading — only binds at scale


class ExecutionCfg(BaseModel):
    order_type: Literal["limit", "market"] = "limit"
    maker: bool = False          # rest POST_ONLY at the touch (maker fee ~1/3 of taker; fills not guaranteed)
    slippage_bps: float = 8.0
    time_in_force: Literal["IOC", "FOK", "POST_ONLY"] = "IOC"
    min_trade_notional: float = 25.0
    min_rebalance_frac: float = 0.10   # skip same-side adjustments smaller than this fraction of the
                                       # current position (anti-churn / fee guard)
    asym_rebalance: bool = False       # let winners run / don't feed losers: widen the no-trade band for
                                       # trimming a winning position or adding to a losing one
    asym_band_mult: float = 3.0        # band multiplier applied in those two cases
    take_profit_pct: float = 0.0       # close a held name once its gain since entry exceeds this % (0 = off)
    scale_out_pct: float = 0.0         # once a held name is up this %, permanently trim it (partial profit)
    scale_out_frac: float = 0.0        # ... by this fraction, keeping the rest running (0 = off)
    margin_mode: Literal["isolated", "cross"] = "isolated"
    position_mode: Literal["net", "two_way"] = "net"

    @property
    def margin_model_code(self) -> int:
        return 2 if self.margin_mode == "isolated" else 1

    @property
    def position_model_code(self) -> int:
        return 1 if self.position_mode == "net" else 2


class RiskCfg(BaseModel):
    max_gross_leverage: float = 10.0
    max_net_exposure: float = 0.05
    max_drawdown_pct: float = 15.0
    funding_abs_max: float = 0.003
    monitor_seconds: int = 60          # how often the safety rail checks risk between rebalances
    max_position_loss_pct: float = 6.0  # close a single position if its loss exceeds this % of equity
    pause_hours: float = 12.0          # after a drawdown kill-switch, stay flat this long
    stop_loss_pct: float = 0.0         # LIVE only: rest a protective stop this % from entry (0 = off)
    # dynamic de-risking (smooths drawdowns / negative months by cutting gross in bad regimes)
    dynamic_risk: bool = True
    vol_target_daily: float = 0.015    # de-risk when recent daily book-return vol exceeds this
    vol_lookback: int = 21             # days of returns for the vol estimate
    dd_throttle_start: float = 0.04    # start cutting gross at this drawdown fraction
    dd_throttle_full: float = 0.12     # gross fully throttled (to min) at this drawdown fraction
    min_risk_scale: float = 0.25       # never cut gross below this fraction of base
    # momentum-crash guard (cuts gross in the bear-then-violent-bounce regime that runs over momentum)
    crash_guard: bool = True
    crash_dd_hours: float = 720.0      # bear context: drawdown measured over this window (~30d)
    crash_bounce_hours: float = 48.0   # snapback measured over this short window (~2d)
    crash_dd_trigger: float = 0.15     # require the market this far below its recent high
    crash_bounce_trigger: float = 0.05 # AND a snapback of at least this much over the short window
    crash_scale_floor: float = 0.5     # cut gross no lower than this fraction in a full-blown crash regime
    # dispersion gate (shrink the book on no-edge days when coins move together)
    dispersion_gate: bool = False
    dispersion_ref_window: int = 20    # trailing window (rebalances) for the dispersion reference
    dispersion_ref_abs: float = 0.0    # if >0, use this ABSOLUTE healthy-dispersion level as the reference
                                       # (catches sustained low-dispersion regimes a trailing median misses)
    dispersion_min_scale: float = 0.5  # floor when dispersion collapses
    # daily loss circuit-breaker (cap the worst single days)
    daily_loss_limit_pct: float = 0.0  # if a day loses >= this %, shrink next day's gross (0 = off)
    daily_loss_scale: float = 0.5      # gross multiplier applied the day after a daily-loss breach


class ChampionCfg(BaseModel):
    """Directional momentum + regime strategy (the backtest champion). Long-only, keeps market beta."""
    top_k: int = 5                 # hold the top-K strongest names
    lookback: int = 30             # momentum lookback (days)
    trend_ma: int = 50             # a name must be above this MA to qualify (absolute uptrend)
    regime_ma: int = 100           # only invest when BTC is above this MA (risk-on brake)
    dual_confirm: bool = True      # require both relative strength AND absolute uptrend
    target_gross: float = 1.0      # gross leverage when fully risk-on (vol-target may scale below)
    # --- improvements validated on 5.5yr walk-forward (scripts/champion_research.py) ---
    breadth_scale: bool = False    # scale gross by market breadth (fraction of universe above breadth_ma):
                                   # a parallel market-health check that de-risks thin/narrow rallies.
                                   # Backtest: maxDD 35%->24% with equal-or-better Sharpe & robustness.
    breadth_ma: int = 100          # MA each coin is measured against for the breadth count
    breadth_lo: float = 0.30       # gross scales to 0 at/below this breadth fraction
    breadth_hi: float = 0.80       # gross scales to full at/above this fraction (linear in between)
    mom_weighted: bool = False     # size by momentum strength instead of equal-weight (lean into leaders)
    max_name_weight: float = 0.40  # cap any single name at this fraction of gross (copy-trade-safe)


class CarryCfg(BaseModel):
    """Funding-carry + momentum combo (market-neutral). Long hi-momentum/lo-funding, short lo-momentum/
    hi-funding, dollar-neutral. Harvests the structural perp-funding premium; orthogonal to price."""
    top_k: int = 5                 # names per sleeve (long K / short K)
    lookback: int = 21             # momentum lookback (days)
    target_gross: float = 2.0      # total gross (~1x per side); vol-target/DD-throttle may scale below
    funding_avg_cycles: int = 5    # rank on the trailing average of this many funding snapshots
                                   # (funding is persistent — the average is a cleaner signal than a
                                   #  single instantaneous rate; backtest Sharpe ~1.6 -> ~2.0 with it)


class ScheduleCfg(BaseModel):
    rebalance_minutes: int = 240
    rebalance_hour_utc: Optional[int] = None  # if 0-23, rebalance daily at this fixed UTC hour
                                              # (predictable + restart-proof); else use the interval


class StateCfg(BaseModel):
    db_path: str = "data/sentinel.db"
    equity_csv: str = "data/equity.csv"
    log_path: str = "data/sentinel.log"


class Config(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    # neutral = market-neutral momentum book; champion = directional momentum+regime; carry = funding-carry+momentum
    strategy: Literal["neutral", "champion", "carry"] = "neutral"
    exchange: ExchangeCfg = Field(default_factory=ExchangeCfg)
    capital_usdt: Optional[float] = 10_000.0
    universe: UniverseCfg = Field(default_factory=UniverseCfg)
    signal: SignalCfg = Field(default_factory=SignalCfg)
    champion: ChampionCfg = Field(default_factory=ChampionCfg)
    carry: CarryCfg = Field(default_factory=CarryCfg)
    whales: WhalesCfg = Field(default_factory=WhalesCfg)
    portfolio: PortfolioCfg = Field(default_factory=PortfolioCfg)
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    state: StateCfg = Field(default_factory=StateCfg)

    # secrets (from .env, never config.yaml)
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    # Hyperliquid execution (Phase 2) — use an AGENT/API wallet (trade-only, cannot withdraw).
    # Read-only data + PAPER need neither of these.
    hl_account_address: Optional[str] = None   # your main account address (public)
    hl_agent_key: Optional[str] = None         # agent wallet private key — .env ONLY, never commit

    @model_validator(mode="after")
    def _check(self) -> "Config":
        if self.portfolio.target_gross_leverage > self.risk.max_gross_leverage:
            raise ValueError(
                f"portfolio.target_gross_leverage ({self.portfolio.target_gross_leverage}) "
                f"exceeds risk.max_gross_leverage ({self.risk.max_gross_leverage})"
            )
        if self.portfolio.per_name_leverage > self.risk.max_gross_leverage:
            raise ValueError("portfolio.per_name_leverage exceeds risk.max_gross_leverage")
        if self.portfolio.longs < 1 or self.portfolio.shorts < 1:
            raise ValueError("portfolio.longs and portfolio.shorts must each be >= 1")
        w = self.signal.weights
        if (w.momentum + w.funding + w.funding_vel + w.volume + w.whale + w.reversal) <= 0:
            raise ValueError("signal.weights must sum to a positive number")
        return self

    def require_keys(self) -> None:
        if not self.api_key or not self.api_secret:
            raise RuntimeError(
                "KOINBAY_API_KEY / KOINBAY_API_SECRET not set. Copy .env.example to .env and fill them in."
            )


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load config.yaml, overlay secrets from the environment (.env), and validate."""
    load_dotenv()
    p = Path(path)
    data: dict = {}
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
    cfg = Config(**data)
    cfg.api_key = os.getenv("KOINBAY_API_KEY") or None
    cfg.api_secret = os.getenv("KOINBAY_API_SECRET") or None
    cfg.hl_account_address = os.getenv("HL_ACCOUNT_ADDRESS") or None
    cfg.hl_agent_key = os.getenv("HL_AGENT_KEY") or None
    return cfg
