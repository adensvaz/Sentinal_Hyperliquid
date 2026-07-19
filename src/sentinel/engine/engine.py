"""Engine: wires the components together and runs one rebalance cycle.

Pipeline per cycle:
  universe -> market data -> funding filter -> scores -> equity -> drawdown guard
  -> dollar-neutral target book -> leverage safety scale -> reconcile vs live positions
  -> (live: per-contract setup) -> execute -> persist equity/trades/state.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import Config
from ..exchange.client import KoinbayClient
from ..exchange.contracts import ContractRegistry
from ..exchange.futures import KoinbayFutures
from ..exchange.factory import make_futures
from ..execution.live_broker import LiveBroker
from ..execution.paper_broker import PaperBroker, PaperPosition
from ..execution.reconciler import reconcile
from ..risk.limits import (crash_guard, dispersion_scale, drawdown_pct, filter_by_funding,
                           leverage_scale, net_exposure_ratio, position_loss_breached, risk_scale)
from ..signal.market_proxy import MarketProxySignal
from ..signal.technicals import ta_consensus
from ..signal.whale_tracker import WhaleTracker, map_bias_to_symbols
from ..state.store import Store
from ..strategy.funding_carry import FundingCarryStrategy
from ..strategy.trend import TrendStrategy
from ..strategy.momentum_regime import MomentumRegimeStrategy
from ..strategy.portfolio import beta_scales, book_beta, compute_betas, demean_by_beta, dispersion
from ..strategy.sentiment_edge import SentimentEdgeStrategy
from ..strategy.sizing import adv_caps, net_scales
from ..util.concurrency import pmap
from ..util.mathx import parse_duration_to_hours, realized_vol
from .marketdata import build_universe, load_symbol_data, mark_price, pick_base_interval

log = logging.getLogger("sentinel")


@dataclass
class RebalanceReport:
    mode: str
    equity: float
    gross: float
    net: float
    drawdown_pct: float
    paused: bool
    universe_size: int
    n_long: int
    n_short: int
    n_orders: int
    positions: list[dict] = field(default_factory=list)
    fills: list = field(default_factory=list)
    top_scores: list = field(default_factory=list)


class Engine:
    def __init__(self, cfg: Config, live: bool = False):
        self.cfg = cfg
        self.live = live
        self.mode = "live" if live else "paper"
        self.fx = make_futures(cfg)                      # KoinbayFutures | HyperliquidFutures per cfg.exchange.venue
        self.client = getattr(self.fx, "c", None)        # underlying KoinBay client (None on Hyperliquid)
        self._funding_interval_h = getattr(self.fx, "funding_interval_hours", 8.0)  # HL=1h, KoinBay=8h
        self.btc_sym = "BTC" if cfg.exchange.venue == "hyperliquid" else "E-BTC-USDT"  # venue-aware BTC reference
        self.strategy = SentimentEdgeStrategy(cfg.portfolio)
        self.champion = MomentumRegimeStrategy(cfg)   # directional momentum+regime (used when cfg.strategy=='champion')
        self.carry = FundingCarryStrategy(cfg)        # funding-carry+momentum, market-neutral (cfg.strategy=='carry')
        self.trend = TrendStrategy(cfg)               # dollar-neutral cross-sectional trend/CTA (cfg.strategy=='trend')
        self.store = Store(cfg.state.db_path, cfg.state.equity_csv)
        self.registry: Optional[ContractRegistry] = None
        self.signal: Optional[MarketProxySignal] = None
        self.base_interval = "4h"   # refined by a live probe in _ensure_market()
        self.history_bars = 120
        self.coin_to_symbol: dict[str, str] = {}
        self.whales = self._build_whale_tracker()
        self.broker = self._build_broker()

    # -- construction helpers -------------------------------------------------
    def _build_whale_tracker(self) -> Optional[WhaleTracker]:
        cfg = self.cfg
        if not cfg.whales.enabled:
            return None
        addresses = list(cfg.whales.addresses)
        path = cfg.whales.source_file
        if path and os.path.exists(path):   # prefer the refreshed basket
            try:
                data = json.loads(open(path).read())
                if data.get("addresses"):
                    addresses = data["addresses"]
            except Exception as e:
                log.warning("could not read %s: %s", path, e)
        if not addresses:
            return None
        log.info("tracking %d whale wallets", len(addresses))
        return WhaleTracker(addresses, cfg.whales.info_url, cfg.whales.weighting)

    def _maybe_refresh_whales(self) -> None:
        """Weekly re-screen: if the basket file is missing or older than refresh_days, rebuild it."""
        cfg = self.cfg
        if not cfg.whales.enabled or self.registry is None:
            return
        path = cfg.whales.source_file
        if path and os.path.exists(path):
            try:
                age = time.time() - json.loads(open(path).read()).get("selected_ts", 0)
                if age < cfg.whales.refresh_days * 86_400:
                    return
            except Exception:
                pass
        from ..signal.whale_select import select_top_whales, write_basket
        kb_coins = {s.multiplier_coin.upper() for s in self.registry.active_usdt()}
        log.info("whale basket missing/stale -> weekly re-screen (~1-2 min)...")
        try:
            picks = select_top_whales(kb_coins, info_url=cfg.whales.info_url,
                                      max_inactive_days=cfg.whales.max_inactive_days, log=log)
            if picks:
                write_basket(path, picks)
                self.whales = self._build_whale_tracker()
                log.info("whale basket refreshed: %d wallets", len(picks))
        except Exception as e:
            log.warning("whale refresh failed: %s", e)

    def _build_broker(self):
        if self.live:
            return LiveBroker(self.fx, capital_override=self.cfg.capital_usdt,
                              stop_loss_pct=self.cfg.risk.stop_loss_pct)
        capital = self.cfg.capital_usdt or 10_000.0
        broker = PaperBroker(starting_capital=capital)
        acct = self.store.load_account(self.mode)
        if acct:
            broker.starting_capital = acct.get("starting_capital") or capital
            broker.realized_pnl = acct.get("realized_pnl") or 0.0
            broker.fees_paid = acct.get("fees_paid") or 0.0
            broker.funding_pnl = acct.get("funding_pnl") or 0.0
            broker.last_funding_ts = acct.get("last_funding_ts") or 0.0
        for sym, p in self.store.load_positions(self.mode).items():
            broker._positions[sym] = PaperPosition(
                contracts=int(p["contracts"]), avg_price=float(p["avg_price"]),
                multiplier=float(p.get("multiplier", 1.0)), opened_ts=float(p.get("opened_ts", 0.0)))
        return broker

    def _ensure_market(self) -> None:
        if self.registry is not None:
            return
        self.registry = ContractRegistry.from_futures(self.fx)
        active = self.registry.active_usdt()
        self.coin_to_symbol = {s.multiplier_coin.upper(): s.name for s in active}
        ref = self.btc_sym if self.registry.has(self.btc_sym) else (active[0].name if active else self.btc_sym)
        horizons = [parse_duration_to_hours(h) for h in self.cfg.signal.momentum_horizons]
        if self.cfg.strategy == "champion":
            # the champion's signals are DAILY (50/100-day MAs, 30-day momentum) — force daily bars
            # with enough history to compute the regime/trend MAs.
            ch = self.cfg.champion
            self.base_interval = "1day"
            self.history_bars = min(300, max(ch.regime_ma, ch.trend_ma, ch.lookback, ch.breadth_ma) + 30)
        elif self.cfg.strategy in ("carry", "trend"):
            # carry / trend rank on DAILY bars — force daily with enough history.
            self.base_interval = "1day"
            if self.cfg.strategy == "carry":
                self.history_bars = min(300, self.cfg.carry.lookback + 30)
            else:
                self.history_bars = min(300, self.cfg.trend.ma_period + 35)
        else:
            self.base_interval = pick_base_interval(self.fx, ref)
            itv_h = parse_duration_to_hours(self.base_interval)
            self.history_bars = max(30, min(300, int(max(horizons) / itv_h) + 5))
        w = self.cfg.signal.weights
        self.signal = MarketProxySignal(
            horizons_hours=horizons,
            weights={"momentum": w.momentum, "funding": w.funding, "funding_vel": w.funding_vel,
                     "volume": w.volume, "whale": w.whale, "reversal": w.reversal},
            funding_sign=self.cfg.signal.funding_sign,
            base_interval=self.base_interval,
            history_bars=self.history_bars,
            reversal_hours=self.cfg.signal.reversal_hours,
        )
        log.info("market ready: %d active USDT perps; base interval=%s; history=%d bars",
                 len(active), self.base_interval, self.history_bars)

    # -- core cycle -----------------------------------------------------------
    def run_once(self, flatten: bool = False) -> RebalanceReport:
        cfg = self.cfg
        if self.live:
            self.broker.preflight()
        self._ensure_market()
        assert self.registry is not None and self.signal is not None
        self._maybe_refresh_whales()

        universe = build_universe(self.fx, self.registry, cfg.universe)
        data, prices, funding = load_symbol_data(self.fx, self.registry, universe,
                                                 self.base_interval, self.history_bars)
        allowed = set(filter_by_funding(funding, cfg.risk.funding_abs_max))
        data = {s: d for s, d in data.items() if s in allowed}
        prices = {s: p for s, p in prices.items() if s in allowed}

        whale_bias: dict[str, float] = {}
        if self.whales:
            try:
                snaps = self.whales.fetch()
                coin_bias = self.whales.blended_bias(snaps)
                whale_bias = map_bias_to_symbols(coin_bias, self.coin_to_symbol)
                self._persist_whales(snaps, coin_bias)
                log.info("whales: %d/%d wallets, consensus on %d coins", len(snaps),
                         len(self.cfg.whales.addresses), len(coin_bias))
            except Exception as e:
                log.warning("whale tracker failed: %s", e)

        # BTC-betas (cross-section) computed once and reused for whale-demean + book beta-hedge
        betas_cs = compute_betas({s: d.closes for s, d in data.items()},
                                 self.btc_sym, cfg.portfolio.beta_lookback) if data else {}
        if cfg.portfolio.beta_neutralize and whale_bias and betas_cs:
            # kill the whale overlay's directional BTC tilt at SOURCE (orthogonalize vs beta)
            whale_bias = demean_by_beta(whale_bias, betas_cs)

        scores = self.signal.scores(data, whale_bias=whale_bias)
        # beta-aware selection: rank by RESIDUAL score (strip the part explained by BTC-beta) so the book
        # is born market-neutral — residual momentum instead of raw, which carries a hidden market bet
        if cfg.portfolio.beta_neutral_scores and betas_cs:
            scores = demean_by_beta(scores, betas_cs)
        # snapshot funding so funding-level / funding-velocity become backtestable from real history later
        self.store.record_funding(self.mode, funding, {s: d.next_funding for s, d in data.items()})
        top_scores = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

        if not self.live:
            self.broker.set_marks(prices)
        raw_equity = self.broker.equity() or (cfg.capital_usdt or 0.0)
        # Portfolio-level allocation weight: risk-parity allocator sets capital_weight
        # (0-1) so each book sizes to its share of the total portfolio capital.
        equity = raw_equity * cfg.capital_weight

        peak = max(self.store.peak_equity(self.mode, equity), equity)
        dd = drawdown_pct(peak, equity)
        breached = dd > cfg.risk.max_drawdown_pct

        paused = self.is_paused()
        book = None
        if flatten or breached or paused:
            if breached:
                log.warning("drawdown %.2f%% exceeds limit %.2f%% -> flattening and pausing %.0fh",
                            dd, cfg.risk.max_drawdown_pct, cfg.risk.pause_hours)
                self.store.set_paused_until(self.mode, time.time() + cfg.risk.pause_hours * 3600)
            elif paused:
                log.info("paused (drawdown cooldown) -> staying flat this cycle")
            target: dict[str, int] = {}
        else:
            gscale = 1.0
            if cfg.risk.dynamic_risk:
                rets = self.store.daily_returns(self.mode, cfg.risk.vol_lookback)
                ddf = drawdown_pct(peak, equity) / 100.0
                gscale = risk_scale(rets, ddf, cfg.risk.vol_target_daily, cfg.risk.min_risk_scale,
                                    cfg.risk.dd_throttle_start, cfg.risk.dd_throttle_full)
                if gscale < 0.999:
                    log.info("dynamic de-risk: gross x%.2f (drawdown=%.1f%%)", gscale, ddf * 100)
            if cfg.risk.crash_guard:
                itv_h = parse_duration_to_hours(self.base_interval)
                ref_c = (data.get(self.btc_sym) or next(iter(data.values()), None))
                cg = crash_guard(ref_c.closes if ref_c else [],
                                 int(cfg.risk.crash_dd_hours / itv_h), int(cfg.risk.crash_bounce_hours / itv_h),
                                 cfg.risk.crash_dd_trigger, cfg.risk.crash_bounce_trigger,
                                 cfg.risk.crash_scale_floor)
                if cg < 0.999:
                    log.warning("MOMENTUM-CRASH guard: gross x%.2f (bear + violent bounce regime)", cg)
                    gscale *= cg
            if cfg.risk.dispersion_gate:
                disp_now = dispersion({s: d.closes for s, d in data.items()})
                hist = self.store.get_meta("disp_hist", []) or []
                ref = sorted(hist)[len(hist) // 2] if hist else 0.0   # trailing median
                dg = dispersion_scale(disp_now, ref, cfg.risk.dispersion_min_scale)
                if dg < 0.999:
                    log.info("dispersion gate: gross x%.2f (low edge: disp %.4f vs ref %.4f)", dg, disp_now, ref)
                    gscale *= dg
                self.store.set_meta("disp_hist", (hist + [round(disp_now, 6)])[-cfg.risk.dispersion_ref_window:])
            if cfg.risk.daily_loss_limit_pct > 0:
                gscale *= float(self.store.get_meta("daily_loss_scale", 1.0) or 1.0)
            self.store.set_meta("crash_scale", round(gscale, 3))
            if cfg.strategy == "champion":
                # DIRECTIONAL champion: long-only top-K momentum, BTC-regime gated. Beta is intentional —
                # NO beta-neutralize / net-clamp / dollar-neutrality (that's the whole point of this book).
                book = self.champion.build_book({s: d.closes for s, d in data.items()}, prices,
                                                self.registry, equity, gross_scale=gscale)
            elif cfg.strategy == "carry":
                # CARRY: funding-carry + momentum, dollar-neutral long/short by construction. Rank on the
                # TRAILING-AVERAGE funding (persistent, cleaner than a single rate); funding is kept
                # unfiltered via risk.funding_abs_max so high-funding shorts are retained. No extra
                # beta-hedge — the long/short legs are already balanced.
                favg = self.store.recent_funding_avg(self.mode, cfg.carry.funding_avg_cycles)
                fsig = {s: favg.get(s, funding.get(s)) for s in funding}   # trailing avg, fallback to current
                book = self.carry.build_book({s: d.closes for s, d in data.items()}, fsig, prices,
                                             self.registry, equity, gross_scale=gscale)
            elif cfg.strategy == "trend":
                # TREND (CTA): dollar-neutral cross-sectional trend — long the strongest uptrends, short
                # the strongest downtrends. Dollar-neutral by construction (equal-weight sleeves); the
                # vol-target / drawdown-throttle overlay (gross_scale) tames the raw drawdown.
                book = self.trend.build_book({s: d.closes for s, d in data.items()}, prices,
                                             self.registry, equity, gross_scale=gscale)
            else:
                ta_scores = ({s: ta_consensus(d.closes) for s, d in data.items()}
                             if cfg.signal.ta_veto > 0 else None)
                adv_quote = self._adv_quote(data) if cfg.portfolio.adv_cap_frac > 0 else {}
                vol_by_symbol = ({s: realized_vol(d.closes) for s, d in data.items()}
                                 if cfg.portfolio.risk_parity else None)
                book = self.strategy.build_book(scores, prices, self.registry, equity, gross_scale=gscale,
                                                ta=ta_scores, ta_veto=cfg.signal.ta_veto,
                                                vol_by_symbol=vol_by_symbol)
                # ---- portfolio robustness, in order: beta-hedge -> ADV caps -> dollar-neutral clamp ----
                closes_by = {s: d.closes for s, d in data.items()}
                betas = betas_cs if book.positions else {}   # computed once above (also used for whale-demean)
                if cfg.portfolio.beta_neutralize and betas:
                    sn = {s: p.target_notional for s, p in book.positions.items()}
                    b0 = book_beta(sn, betas, equity)
                    # bound the sleeve shrink by max_net_exposure so the hedge never breaches the rail
                    heavier = max(sum(n for n in sn.values() if n > 0), sum(-n for n in sn.values() if n < 0))
                    net_cap = (cfg.risk.max_net_exposure * equity / heavier) if heavier > 0 else 0.0
                    self._apply_scales(book, beta_scales(sn, betas, min(cfg.portfolio.beta_max_imbalance, net_cap)))
                    b1 = book_beta({s: p.target_notional for s, p in book.positions.items()}, betas, equity)
                    self.store.set_meta("book_beta", round(b1, 4))
                    if abs(b0) >= 0.05:
                        log.info("beta-neutralize: book BTC-beta %+.2f -> %+.2f", b0, b1)
                if cfg.portfolio.adv_cap_frac > 0 and book.positions:
                    acaps = adv_caps({s: p.target_notional for s, p in book.positions.items()},
                                     adv_quote, cfg.portfolio.adv_cap_frac)
                    hit = [s for s, f in acaps.items() if f < 0.999]
                    self._apply_scales(book, acaps)
                    if hit:
                        log.info("ADV cap: shrank %d name(s) to <=%.0f%% of ADV: %s",
                                 len(hit), cfg.portfolio.adv_cap_frac * 100, ", ".join(hit))
                self.store.set_meta("dispersion", round(dispersion(closes_by), 5))
                # HARD dollar-neutral clamp, after the hedge + ADV caps + integer rounding
                self._apply_scales(book, net_scales({s: p.target_notional for s, p in book.positions.items()},
                                                    equity, cfg.risk.max_net_exposure))
            target = book.signed_contracts()
            scale = leverage_scale(book.realized_gross, equity, cfg.risk.max_gross_leverage)
            if scale < 0.999:
                target = {s: int(c * scale) for s, c in target.items()}
            nr = net_exposure_ratio(book.realized_net, equity)
            self.store.set_meta("net_ratio", round(nr, 4))
            if nr > cfg.risk.max_net_exposure + 0.01:   # residual past the clamp = pure rounding
                log.warning("net exposure %.1f%% above rail %.1f%% after clamp (integer rounding)",
                            nr * 100, cfg.risk.max_net_exposure * 100)

        current = self.broker.positions()
        self._accrue_funding(current)   # book funding earned/paid since last cycle
        for sym in current:
            if sym not in prices and self.registry.has(sym):
                mp = mark_price(self.fx, sym)
                if mp > 0:
                    prices[sym] = mp

        orders = reconcile(target, current, prices, self.registry, cfg.execution.min_trade_notional,
                           cfg.execution.min_rebalance_frac)
        if orders and self.live:
            self.broker.prepare(sorted({o.symbol for o in orders}), self.registry,
                                cfg.portfolio, cfg.execution)
        fills = self.broker.execute(orders, self.registry, cfg.execution)

        if not self.live:
            self.broker.set_marks(prices)
            self._persist_paper()

        new_equity = self.broker.equity() or equity
        peak = self.store.update_peak_equity(self.mode, new_equity)
        gross = book.realized_gross if book else 0.0
        net = book.realized_net if book else 0.0
        self.store.record_equity(self.mode, new_equity, gross, net, drawdown_pct(peak, new_equity))
        self.store.record_fills(self.mode, fills)
        self.store.save_scores(self.mode, scores)
        self._record_closed()

        positions = []
        if book:
            for p in book.positions.values():
                positions.append({"symbol": p.symbol, "side": p.side, "contracts": p.target_contracts,
                                  "notional": p.target_notional, "score": round(p.score, 3)})
        n_long = sum(1 for c in target.values() if c > 0)
        n_short = sum(1 for c in target.values() if c < 0)
        return RebalanceReport(
            mode=self.mode, equity=new_equity, gross=gross, net=net,
            drawdown_pct=drawdown_pct(peak, new_equity), paused=breached,
            universe_size=len(universe), n_long=n_long, n_short=n_short, n_orders=len(orders),
            positions=positions, fills=fills, top_scores=top_scores[:10],
        )

    def flatten(self) -> RebalanceReport:
        return self.run_once(flatten=True)

    def _persist_paper(self) -> None:
        if self.live:
            return
        b = self.broker
        self.store.save_account(self.mode, starting_capital=b.starting_capital, realized_pnl=b.realized_pnl,
                                fees_paid=b.fees_paid, funding_pnl=b.funding_pnl, last_funding_ts=b.last_funding_ts)
        self.store.save_positions(self.mode, {
            s: {"contracts": p.contracts, "avg_price": p.avg_price, "multiplier": p.multiplier,
                "opened_ts": p.opened_ts} for s, p in b._positions.items()})

    def _apply_scales(self, book, scales: dict) -> None:
        """Apply per-symbol scale factors (<=1) to a target book in place, re-rounding to contracts.
        Shared by the beta-hedge, ADV-cap, and net-clamp robustness stages."""
        for s, p in book.positions.items():
            f = scales.get(s, 1.0)
            if f < 0.999:
                spec = self.registry.get(s)
                p.target_contracts = int(p.target_contracts * f)
                rn = spec.contracts_to_notional(p.target_contracts, p.price)
                p.target_notional = rn if p.target_contracts > 0 else -rn

    def _adv_quote(self, data: dict) -> dict:
        """Estimate each name's average daily quote volume (USDT) from recent kline volume.
        per-bar quote = contracts * multiplier * price; scaled to a day by the bar count."""
        per_day = max(1.0, 24.0 / parse_duration_to_hours(self.base_interval))
        out = {}
        for s, d in data.items():
            vv = [v for v in d.volumes[-30:] if v and v > 0]
            if vv and self.registry.has(s):
                avg_bar = self.registry.get(s).contracts_to_notional(sum(vv) / len(vv), d.last_price)
                out[s] = avg_bar * per_day
        return out

    # -- safety rails (run frequently by the loop between rebalances) ---------
    def is_paused(self) -> bool:
        return time.time() < self.store.paused_until(self.mode)

    def _marks_for(self, symbols) -> dict:
        out = {}
        for s in symbols:
            if self.registry and self.registry.has(s):
                mp = mark_price(self.fx, s)
                if mp > 0:
                    out[s] = mp
        return out

    def _index_data(self, symbols):
        """One index call per symbol -> (marks, funding_rates). Fetched concurrently."""
        def _one(s):
            if not (self.registry and self.registry.has(s)):
                return None
            idx = self.fx.index(s)
            p = float(idx.get("tagPrice") or 0)
            return (s, p, float(idx.get("currentFundRate") or 0)) if p > 0 else None

        marks, funding = {}, {}
        for r in pmap(_one, symbols, workers=12):
            if r:
                marks[r[0]], funding[r[0]] = r[1], r[2]
        return marks, funding

    def _accrue_funding(self, positions) -> None:
        if self.live:
            return  # live funding is settled by the exchange itself
        marks, funding = self._index_data(positions) if positions else ({}, {})
        self.broker.accrue_funding(funding, marks, interval_hours=self._funding_interval_h)

    def _live_equity(self, marks: dict) -> float:
        if not self.live:
            self.broker.set_marks(marks)
            return self.broker.equity()
        return self.broker.equity() or (self.cfg.capital_usdt or 0.0)

    def _exposure(self, positions: dict, marks: dict):
        gross = net = 0.0
        for s, c in positions.items():
            if not self.registry.has(s) or s not in marks:
                continue
            ntl = abs(c) * marks[s] * self.registry.get(s).multiplier
            gross += ntl
            net += ntl if c > 0 else -ntl
        return gross, net

    def close_all(self, reason: str = "manual") -> list:
        self._ensure_market()
        cur = self.broker.positions()
        marks = self._marks_for(cur)
        orders = reconcile({}, cur, marks, self.registry, 0.0)
        if self.live and orders:
            self.broker.prepare(sorted({o.symbol for o in orders}), self.registry,
                                self.cfg.portfolio, self.cfg.execution)
        fills = self.broker.execute(orders, self.registry, self.cfg.execution)
        if not self.live:
            self.broker.set_marks(marks)
            self._persist_paper()
        eq = self.broker.equity() or 0.0
        peak = self.store.update_peak_equity(self.mode, eq)
        self.store.record_equity(self.mode, eq, 0.0, 0.0, drawdown_pct(peak, eq))
        self.store.record_fills(self.mode, fills)
        self.store.save_scores(self.mode, {})
        self._record_closed()
        log.warning("FLATTEN ALL (%s): closed %d positions, equity=%.2f", reason, len(cur), eq)
        return fills

    def close_symbol(self, sym: str, marks: dict) -> None:
        cur = self.broker.positions()
        if sym not in cur:
            return
        orders = reconcile({}, {sym: cur[sym]}, marks, self.registry, 0.0)
        fills = self.broker.execute(orders, self.registry, self.cfg.execution)
        if not self.live:
            self.broker.set_marks(marks)
            self._persist_paper()
        self.store.record_fills(self.mode, fills)
        self._record_closed()
        log.warning("catastrophe stop: closed %s", sym)

    def risk_check(self) -> dict:
        """Lightweight between-rebalance safety check: drawdown kill-switch + per-position stop.
        Records an equity tick so the curve/dashboard stay live."""
        self._ensure_market()
        cur = self.broker.positions()
        marks, funding = self._index_data(cur) if cur else ({}, {})
        if not self.live:
            self.broker.accrue_funding(funding, marks, interval_hours=self._funding_interval_h)
        eq = self._live_equity(marks)
        peak = self.store.update_peak_equity(self.mode, eq)
        dd = drawdown_pct(peak, eq)
        gross, net = self._exposure(cur, marks)
        # check the kill-switch every monitor tick, but only PERSIST an equity point every ~15 min so
        # the curve stays daily-clean (the dashboard adds a live "now" point regardless of this)
        now = time.time()
        if now - getattr(self, "_last_eq_persist", 0.0) >= 900:
            self.store.record_equity(self.mode, eq, gross, net, dd)
            self._last_eq_persist = now
        # daily-loss circuit-breaker: at each day rollover, score the day that just ended; a big down-day
        # shrinks the NEXT day's gross (read in run_once). Matches the backtest's next-bar cut.
        if self.cfg.risk.daily_loss_limit_pct > 0:
            today = time.strftime("%Y-%m-%d", time.localtime(now))
            if self.store.get_meta("day_start_date") != today:
                ds_eq = self.store.get_meta("day_start_eq")
                if ds_eq and float(ds_eq) > 0:
                    day_ret = eq / float(ds_eq) - 1.0
                    self.store.set_meta("daily_loss_scale",
                                        self.cfg.risk.daily_loss_scale
                                        if day_ret <= -self.cfg.risk.daily_loss_limit_pct / 100.0 else 1.0)
                self.store.set_meta("day_start_date", today)
                self.store.set_meta("day_start_eq", round(eq, 4))
        if dd > self.cfg.risk.max_drawdown_pct:
            self.close_all("drawdown kill-switch %.2f%% > %.2f%%" % (dd, self.cfg.risk.max_drawdown_pct))
            self.store.set_paused_until(self.mode, time.time() + self.cfg.risk.pause_hours * 3600)
            return {"action": "flatten_drawdown", "drawdown_pct": round(dd, 2)}
        paper_pos = getattr(self.broker, "_positions", {})
        for sym, contracts in list(cur.items()):
            pp = paper_pos.get(sym)
            if pp is None or sym not in marks:
                continue
            pnl = (marks[sym] - pp.avg_price) * contracts * pp.multiplier
            if position_loss_breached(pnl, eq, self.cfg.risk.max_position_loss_pct):
                self.close_symbol(sym, marks)
                return {"action": "close_position", "symbol": sym, "loss": round(pnl, 2)}
        return {"action": "none", "equity": round(eq, 2), "drawdown_pct": round(dd, 2)}

    def _record_closed(self) -> None:
        """Drain the broker's closed round-trips into the store (the Trades ledger)."""
        ct = getattr(self.broker, "closed_trades", None)
        if ct:
            self.store.record_closed_trades(self.mode, ct)
            ct.clear()

    # -- snapshots for the dashboard -----------------------------------------
    def _persist_whales(self, snaps, coin_bias: dict) -> None:
        wallets = [{"address": s.address, "equity": s.equity, "gross": s.gross, "net": s.net}
                   for s in snaps]
        consensus = [{"coin": c, "bias": round(v, 4)}
                     for c, v in sorted(coin_bias.items(), key=lambda x: -abs(x[1]))
                     if abs(v) >= 0.005][:20]
        self.store.save_whales(consensus, wallets)

    # -- read-only diagnostics ------------------------------------------------
    def selftest(self) -> None:
        log.info("== Sentinel Edge selftest (read-only) ==")
        log.info("futures ping: %s | serverTime=%s", self.fx.ping(), self.fx.server_time())
        self._ensure_market()
        assert self.registry is not None and self.signal is not None
        universe = build_universe(self.fx, self.registry, self.cfg.universe)
        log.info("universe (%d): %s", len(universe), ", ".join(universe[:12]) + (" ..." if len(universe) > 12 else ""))
        data, prices, funding = load_symbol_data(self.fx, self.registry, universe,
                                                 self.base_interval, self.history_bars)
        scores = self.signal.scores(data)
        equity = self.cfg.capital_usdt or 10_000.0
        book = self.strategy.build_book(scores, prices, self.registry, equity)
        top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        log.info("scored %d symbols. Top: %s", len(scores),
                 ", ".join(f"{s}={v:+.2f}" for s, v in top[:5]))
        log.info("bottom: %s", ", ".join(f"{s}={v:+.2f}" for s, v in top[-5:]))
        log.info("proposed book @ equity=%.0f gross=%.0f net=%.2f | %d long / %d short",
                 equity, book.realized_gross, book.realized_net,
                 sum(1 for p in book.positions.values() if p.target_contracts > 0),
                 sum(1 for p in book.positions.values() if p.target_contracts < 0))
        orders = reconcile(book.signed_contracts(), {}, prices, self.registry,
                           self.cfg.execution.min_trade_notional)
        log.info("orders it WOULD place (%d):", len(orders))
        for o in orders:
            log.info("  %-14s %-4s %-5s vol=%-8d @~%.6g  ($%.0f)", o.symbol, o.side, o.open,
                     o.volume, o.ref_price, o.notional)
        if self.cfg.api_key and self.cfg.api_secret:
            try:
                acct = self.fx.account()
                coins = [a.get("marginCoin") for a in (acct.get("account") or [])]
                log.info("signed account OK (signing works). margin coins: %s", coins)
            except Exception as e:
                log.error("signed account call FAILED: %s", e)
        else:
            log.info("no API keys set -> skipped signed account check (public data only).")

    def close(self) -> None:
        if self.whales:
            self.whales.close()
        self.store.close()
        if self.client is not None:
            self.client.close()
        elif hasattr(self.fx, "close"):
            self.fx.close()
