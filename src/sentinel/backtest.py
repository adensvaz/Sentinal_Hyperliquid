"""Walk-forward backtest of the market-neutral price-signal core.

HONEST SCOPE — read this:
  * Tests momentum + volume (the price-derived signal) on KoinBay 4h klines.
  * EXCLUDES funding (no historical endpoint) and the whale overlay (backtesting the basket would be
    lookahead/survivorship-biased — the wallets were chosen *because* they recently won).
  * History is capped at KoinBay's 300-bar kline limit => ~50 days at 4h. Small sample; treat as
    indicative, not proof.
So this answers exactly one question: does the price core clear realistic fees + slippage?

Uses the REAL signal (MarketProxySignal) and weighting (capped_weights); sizing is in notional
(skips integer-contract rounding — second-order for an edge-vs-cost test).
"""
from __future__ import annotations

import logging
import math

from .config import Config
from .exchange.client import KoinbayClient
from .exchange.contracts import ContractRegistry
from .exchange.futures import KoinbayFutures
from .engine.marketdata import build_universe
from .risk.limits import crash_guard, dispersion_scale, risk_scale
from .signal.base import SymbolData
from .signal.market_proxy import MarketProxySignal
from .signal.technicals import passes_veto, ta_consensus
from .strategy.portfolio import beta_scales, book_beta, compute_betas, demean_by_beta, dispersion
from .strategy.sizing import capped_weights, net_scales, sleeve_notionals, vol_adjust
from .util.mathx import parse_duration_to_hours, realized_vol

log = logging.getLogger("sentinel")


def load_history(cfg: Config, interval: str = "4h", limit: int = 300) -> dict:
    client = KoinbayClient(cfg.exchange.futures_host, timeout_s=cfg.exchange.timeout_s)
    fx = KoinbayFutures(client)
    try:
        registry = ContractRegistry.from_futures(fx)
        universe = build_universe(fx, registry, cfg.universe)
        series: dict[str, dict] = {}
        for coin in universe:
            try:
                rows = fx.klines(coin, interval, limit)
            except Exception:
                continue
            series[coin] = {int(r["idx"]): (float(r["close"]), float(r.get("vol", 0) or 0)) for r in rows}
        # Align to a reference timeline (the most-complete coin), forward-filling small gaps and
        # dropping coins that don't cover >=90% of it (e.g. commodity perps on a different grid).
        common: list[int] = []
        closes: dict[str, list] = {}
        vols: dict[str, list] = {}
        good: list[str] = []
        if series:
            ref = max(series, key=lambda c: len(series[c]))
            common = sorted(series[ref])
            for c in series:
                ac, av, have, lc, lv = [], [], 0, None, None
                for t in common:
                    if t in series[c]:
                        lc, lv = series[c][t]
                        have += 1
                    ac.append(lc)
                    av.append(lv)
                if have / len(common) >= 0.9 and ac[0] is not None:
                    closes[c] = ac
                    vols[c] = av
                    good.append(c)
    finally:
        client.close()
    return {"universe": good, "T": common, "closes": closes, "vols": vols,
            "interval_h": parse_duration_to_hours(interval)}


def _signal(cfg: Config, interval_h: float) -> MarketProxySignal:
    w = cfg.signal.weights
    return MarketProxySignal(
        horizons_hours=[parse_duration_to_hours(h) for h in cfg.signal.momentum_horizons],
        weights={"momentum": w.momentum, "funding": w.funding, "funding_vel": w.funding_vel,
                 "volume": w.volume, "whale": w.whale, "reversal": w.reversal},
        funding_sign=cfg.signal.funding_sign,
        reversal_hours=cfg.signal.reversal_hours,
    )


def _max_drawdown(curve: list[float]) -> float:
    peak = curve[0] if curve else 0.0
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd * 100.0


def simulate(hist: dict, cfg: Config, rebalance_every: int = 1,
             taker_fee: float = 0.00075, slippage_bps: float = 8.0, funding: dict = None) -> dict:
    universe, T, closes, vols = hist["universe"], hist["T"], hist["closes"], hist["vols"]
    fund = funding or {}          # optional per-symbol daily funding series aligned to T (signal + carry)
    funding_total = 0.0
    interval_h = hist["interval_h"]
    sig = _signal(cfg, interval_h)
    p = cfg.portfolio
    cost_rate = taker_fee + slippage_bps / 10_000.0
    band = cfg.execution.min_rebalance_frac

    horizons = [parse_duration_to_hours(h) for h in cfg.signal.momentum_horizons]
    warmup = max(10, int(max(horizons) / interval_h) + 1)
    n = len(T)
    if n < warmup + 10:
        return {"error": f"insufficient history: {n} bars, need >{warmup + 10}"}

    equity = float(cfg.capital_usdt or 10_000.0)
    gross_equity = equity   # hypothetical, no costs charged
    peak = equity
    cur = {c: 0.0 for c in universe}
    eq_curve, gross_curve, ts_curve, rets = [equity], [gross_equity], [T[warmup]], []
    fees_total = turnover_total = 0.0
    rebalances = 0
    pos: dict = {}          # coin -> {ntl, entry, open_ts, accum} for round-trip reconstruction
    bt_trades: list = []    # closed round-trips
    bt_fills: list = []     # individual fills (capped on return)
    beta_abs_sum = beta_abs_sum_post = 0.0  # observability: mean |book BTC-beta| pre/post neutralization
    beta_count = beta_count_post = 0
    disp_hist: list = []        # rolling cross-sectional dispersion (for the no-edge-day gate)
    daily_loss_scale_next = 1.0  # carries a daily-loss circuit-breaker cut into the next bar
    scaled: set = set()         # names already partial-scaled-out (held trimmed until they close)

    i = warmup
    while i < n - 1:
        nxt = min(i + rebalance_every, n - 1)
        # ---- score using ONLY data up to bar i (no lookahead) ----
        data = {c: SymbolData(c, T[: i + 1], closes[c][: i + 1], vols[c][: i + 1],
                              funding=(fund[c][i] if c in fund and i < len(fund[c]) else 0.0),
                              last_price=closes[c][i]) for c in universe}
        scores = sig.scores(data)              # funding & whale components are 0 here (see module docstring)
        # beta-aware selection: rank by RESIDUAL (beta-adjusted) score so the book is born neutral
        if getattr(p, "beta_neutral_scores", False):
            closes_by = {c: closes[c][: i + 1] for c in universe}
            ref = "E-BTC-USDT" if "E-BTC-USDT" in closes_by else next(iter(closes_by))
            sbetas = compute_betas(closes_by, ref, p.beta_lookback)
            if sbetas:
                scores = demean_by_beta(scores, sbetas)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        nL, nS = p.longs, p.shorts
        if len(ranked) < nL + nS:
            half = len(ranked) // 2
            nL, nS = min(nL, half), min(nS, half)
        if nL >= 1 and nS >= 1:
            thr = cfg.signal.ta_veto
            ta = {c: ta_consensus(closes[c][: i + 1]) for c in universe} if thr > 0 else {}
            used = set()

            def _pick(seq, n, side):
                o = []
                for s, sc in seq:
                    if s in used or (thr > 0 and not passes_veto(ta.get(s, 0.0), side, thr)):
                        continue
                    o.append((s, sc)); used.add(s)
                    if len(o) >= n:
                        break
                return o
            longs = _pick(ranked, nL, "LONG")
            shorts = _pick(list(reversed(ranked)), nS, "SHORT")
            if p.weighting == "equal":
                wl, ws = [1.0] * len(longs), [1.0] * len(shorts)
            else:
                k = p.conviction
                wl = [max(s, 0.0) ** k for _, s in longs]
                ws = [max(-s, 0.0) ** k for _, s in shorts]
            if getattr(p, "risk_parity", False):
                wl = vol_adjust(wl, [realized_vol(closes[s][: i + 1]) for s, _ in longs])
                ws = vol_adjust(ws, [realized_vol(closes[s][: i + 1]) for s, _ in shorts])
            wl, ws = capped_weights(wl, p.max_name_weight), capped_weights(ws, p.max_name_weight)
            gscale = 1.0
            if getattr(cfg.risk, "dynamic_risk", False):
                ddf = (peak - equity) / peak if peak > 0 else 0.0
                gscale = risk_scale(rets[-cfg.risk.vol_lookback:], ddf, cfg.risk.vol_target_daily,
                                    cfg.risk.min_risk_scale, cfg.risk.dd_throttle_start, cfg.risk.dd_throttle_full)
            if getattr(cfg.risk, "crash_guard", False) and "E-BTC-USDT" in closes:
                gscale *= crash_guard(closes["E-BTC-USDT"][: i + 1],
                                      int(cfg.risk.crash_dd_hours / interval_h),
                                      int(cfg.risk.crash_bounce_hours / interval_h),
                                      cfg.risk.crash_dd_trigger, cfg.risk.crash_bounce_trigger,
                                      cfg.risk.crash_scale_floor)
            if getattr(cfg.risk, "dispersion_gate", False):
                disp = dispersion({c: closes[c][: i + 1] for c in universe})
                disp_hist.append(disp)
                if len(disp_hist) > cfg.risk.dispersion_ref_window:
                    disp_hist.pop(0)
                ref_abs = getattr(cfg.risk, "dispersion_ref_abs", 0.0)
                if ref_abs > 0:   # gate on SMOOTHED dispersion vs an absolute healthy level (regime, not noise)
                    smoothed = sum(disp_hist) / len(disp_hist)
                    gscale *= dispersion_scale(smoothed, ref_abs, cfg.risk.dispersion_min_scale)
                else:             # legacy relative gate (trailing median)
                    ref = sorted(disp_hist)[len(disp_hist) // 2] if disp_hist else 0.0
                    gscale *= dispersion_scale(disp, ref, cfg.risk.dispersion_min_scale)
            gscale *= daily_loss_scale_next   # daily-loss circuit-breaker carry (1.0 unless tripped)
            sleeve = p.target_gross_leverage * gscale * equity / 2.0
            target = {c: 0.0 for c in universe}
            for (c, _), ntl in zip(longs, sleeve_notionals(wl, sleeve, +1)):
                target[c] = ntl
            for (c, _), ntl in zip(shorts, sleeve_notionals(ws, sleeve, -1)):
                target[c] = ntl
        else:
            target = {c: 0.0 for c in universe}

        # ---- robustness: neutralize the book's BTC-beta (dollar-neutral != market-neutral) ----
        betas = {}
        if any(target.values()):
            closes_by = {c: closes[c][: i + 1] for c in universe}
            ref = "E-BTC-USDT" if "E-BTC-USDT" in closes_by else next(iter(closes_by))
            betas = compute_betas(closes_by, ref, p.beta_lookback)
            if betas and equity > 0:
                beta_abs_sum += abs(book_beta(target, betas, equity)); beta_count += 1
            if getattr(p, "beta_neutralize", False) and betas:
                # cap the sleeve shrink so introduced |net|/equity stays within max_net_exposure
                heavier = max(sum(n for n in target.values() if n > 0), sum(-n for n in target.values() if n < 0))
                net_cap = (cfg.risk.max_net_exposure * equity / heavier) if heavier > 0 else 0.0
                sc = beta_scales(target, betas, min(p.beta_max_imbalance, net_cap))
                target = {c: target[c] * sc.get(c, 1.0) for c in target}
                if equity > 0:
                    beta_abs_sum_post += abs(book_beta(target, betas, equity)); beta_count_post += 1

        # NOTE: ADV/liquidity caps are deliberately NOT applied here. They require the per-contract
        # multiplier to convert volume->quote, which this notional-space backtest doesn't carry, so any
        # proxy is wrong-units and binds spuriously. ADV caps live in the engine (live) only — correctly
        # computed via the multiplier, inert at current capital, activating at scale.

        # ---- enforce the dollar-neutral rail (hard clamp, after beta-hedge) ----
        if any(target.values()) and equity > 0:
            ns = net_scales(target, equity, cfg.risk.max_net_exposure)
            target = {c: target[c] * ns.get(c, 1.0) for c in target}

        # ---- take-profit: close a held name once its gain since entry exceeds tp_pct (locks the winner) ----
        tp = getattr(cfg.execution, "take_profit_pct", 0.0)
        if tp > 0:
            for c in universe:
                cu = cur[c]
                if cu != 0 and c in pos and pos[c].get("entry", 0) > 0:
                    gain = (closes[c][i] / pos[c]["entry"] - 1.0) * (1 if cu > 0 else -1)
                    if gain >= tp / 100.0:
                        target[c] = 0.0

        # ---- partial scale-out: once a held name is up scale_out_pct, permanently trim it by frac (keep the rest) ----
        so_pct = getattr(cfg.execution, "scale_out_pct", 0.0)
        so_frac = getattr(cfg.execution, "scale_out_frac", 0.0)
        if so_pct > 0 and so_frac > 0:
            for c in universe:
                if c in scaled and cur[c] == 0:
                    scaled.discard(c)                    # rotated out -> reset for next entry
                if cur[c] != 0 and c in pos and pos[c].get("entry", 0) > 0:
                    g = (closes[c][i] / pos[c]["entry"] - 1.0) * (1 if cur[c] > 0 else -1)
                    if g >= so_pct / 100.0:
                        scaled.add(c)
                if c in scaled:
                    target[c] = target[c] * (1.0 - so_frac)   # hold the trimmed weight until it closes

        # ---- apply anti-churn band, compute turnover + cost ----
        turnover = 0.0
        new = {}
        asym = getattr(cfg.execution, "asym_rebalance", False)
        for c in universe:
            cu, tg = cur[c], target[c]
            if cu != 0 and (cu > 0) == (tg > 0):
                eff_band = band
                if asym and c in pos and pos[c].get("entry"):
                    winning = (closes[c][i] > pos[c]["entry"]) == (cu > 0)
                    trimming = abs(tg) < abs(cu)
                    if (winning and trimming) or (not winning and not trimming):  # trim-winner / add-loser
                        eff_band = band * cfg.execution.asym_band_mult
                nw = cu if abs(tg - cu) < eff_band * abs(cu) else tg
            else:
                nw = tg
            turnover += abs(nw - cu)
            new[c] = nw
            # ---- reconstruct fills + round-trip trades (does not affect equity math) ----
            px, ts = closes[c][i], T[i]
            if nw != cu and px > 0:
                if cu == 0:                               # open
                    bt_fills.append((ts, "BUY" if nw > 0 else "SELL", "OPEN", abs(nw), px, c))
                    pos[c] = {"ntl": nw, "entry": px, "open_ts": ts, "accum": 0.0}
                elif nw == 0 or (cu > 0) != (nw > 0):     # full close or flip
                    bt_fills.append((ts, "SELL" if cu > 0 else "BUY", "CLOSE", abs(cu), px, c))
                    pp = pos.get(c)
                    if pp:
                        bt_trades.append({"symbol": c, "side": "LONG" if cu > 0 else "SHORT",
                                          "entry": pp["entry"], "exit": px, "pnl": pp["accum"],
                                          "opened_ts": pp["open_ts"], "closed_ts": ts, "ntl": abs(cu)})
                    if nw == 0:
                        pos.pop(c, None)
                    else:                                  # flip -> open the new side
                        bt_fills.append((ts, "BUY" if nw > 0 else "SELL", "OPEN", abs(nw), px, c))
                        pos[c] = {"ntl": nw, "entry": px, "open_ts": ts, "accum": 0.0}
                else:                                      # same-side add / reduce
                    bt_fills.append((ts, "BUY" if nw > cu else "SELL",
                                     "OPEN" if abs(nw) > abs(cu) else "CLOSE", abs(nw - cu), px, c))
                    if c in pos:
                        pos[c]["ntl"] = nw
                    else:
                        pos[c] = {"ntl": nw, "entry": px, "open_ts": ts, "accum": 0.0}
        cur = new
        cost = turnover * cost_rate
        fees_total += cost
        turnover_total += turnover
        rebalances += 1

        # ---- realize PnL over the holding period [i, nxt] ----
        pnl = 0.0
        fpnl = 0.0
        for c, s in cur.items():
            if s != 0 and closes[c][i] > 0:
                d = s * (closes[c][nxt] / closes[c][i] - 1.0)
                pnl += d
                if c in pos:
                    pos[c]["accum"] += d   # attribute period PnL to the open round-trip
            # funding carry: long pays positive funding, short collects it (summed over the hold)
            if s != 0 and c in fund:
                for j in range(i, nxt):
                    if j < len(fund[c]):
                        fpnl += -fund[c][j] * s
        pnl += fpnl
        funding_total += fpnl

        equity_before = equity
        equity += pnl - cost
        peak = max(peak, equity)
        gross_equity += pnl            # no cost
        rets.append((pnl - cost) / equity_before if equity_before else 0.0)
        # daily-loss circuit-breaker: a big down-bar shrinks the NEXT bar's gross (one daily bar = one day)
        if getattr(cfg.risk, "daily_loss_limit_pct", 0.0) > 0:
            daily_loss_scale_next = (cfg.risk.daily_loss_scale
                                     if rets[-1] <= -cfg.risk.daily_loss_limit_pct / 100.0 else 1.0)
        eq_curve.append(equity)
        gross_curve.append(gross_equity)
        ts_curve.append(T[nxt])
        i = nxt

    # ---- metrics ----
    days = (T[-1] - T[warmup]) / 86_400_000 if n else 0
    periods_per_year = (365 * 24) / (interval_h * rebalance_every)
    mean_r = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean_r) ** 2 for r in rets) / len(rets) if rets else 0.0
    sd = math.sqrt(var)
    sharpe = (mean_r / sd) * math.sqrt(periods_per_year) if sd > 0 else 0.0
    net_ret = equity / eq_curve[0] - 1.0
    gross_ret = gross_equity / gross_curve[0] - 1.0
    ann = (equity / eq_curve[0]) ** (365.0 / days) - 1.0 if days > 1 else net_ret
    hit = 100.0 * sum(1 for r in rets if r > 0) / len(rets) if rets else 0.0
    return {
        "rebalance_every": rebalance_every, "cadence_h": interval_h * rebalance_every,
        "days": days, "rebalances": rebalances, "n_coins": len(universe),
        "net_return_pct": net_ret * 100, "gross_return_pct": gross_ret * 100,
        "fee_drag_pct": (gross_ret - net_ret) * 100,
        "ann_return_pct": ann * 100, "sharpe": sharpe, "max_dd_pct": _max_drawdown(eq_curve),
        "hit_rate_pct": hit, "fees_total": fees_total, "funding_total": funding_total,
        "avg_turnover_x": (turnover_total / rebalances / eq_curve[0]) if rebalances else 0.0,
        "final_equity": equity, "start_equity": eq_curve[0],
        "book_beta_pre": (beta_abs_sum / beta_count) if beta_count else 0.0,
        "book_beta_post": (beta_abs_sum_post / beta_count_post) if beta_count_post else None,
        "curve": list(zip(ts_curve, eq_curve, gross_curve)),
        "bt_trades": bt_trades, "bt_fills": bt_fills[-5000:],
    }


def run_cli(cfg: Config, cadences=(1, 6)) -> None:
    log.info("loading KoinBay 4h history (capped at 300 bars ~ 50 days)...")
    hist = load_history(cfg)
    if len(hist["T"]) < 50:
        log.error("not enough aligned history (%d bars across %d coins)", len(hist["T"]), len(hist["universe"]))
        return
    log.info("history: %d coins, %d aligned 4h bars (~%.0f days)",
             len(hist["universe"]), len(hist["T"]),
             (hist["T"][-1] - hist["T"][0]) / 86_400_000)
    log.warning("scope: momentum+volume core only; funding & whale signals EXCLUDED (see backtest.py docstring)")

    maker = cfg.execution.maker
    fee = 0.00025 if maker else 0.00075
    slip = 2.0 if maker else cfg.execution.slippage_bps

    results = []
    for re in cadences:
        m = simulate(hist, cfg, rebalance_every=re, taker_fee=fee, slippage_bps=slip)
        if "error" in m:
            log.error(m["error"]); continue
        results.append(m)

    print("\n=== BACKTEST (price-signal core; %s fees %.3f%% + %.0fbps slippage; conviction %.1f; dyn-risk %s) ===" %
          ("MAKER" if maker else "taker", fee * 100, slip, cfg.portfolio.conviction, cfg.risk.dynamic_risk))
    hdr = f"{'cadence':>8} {'rebal':>6} {'net%':>8} {'gross%':>8} {'feedrag%':>9} {'ann%':>8} {'Sharpe':>7} {'maxDD%':>7} {'hit%':>6}"
    print(hdr); print("-" * len(hdr))
    for m in results:
        print(f"{m['cadence_h']:>6.0f}h {m['rebalances']:>6} {m['net_return_pct']:>8.2f} "
              f"{m['gross_return_pct']:>8.2f} {m['fee_drag_pct']:>9.2f} {m['ann_return_pct']:>8.1f} "
              f"{m['sharpe']:>7.2f} {m['max_dd_pct']:>7.2f} {m['hit_rate_pct']:>6.0f}")
    # save the primary (first cadence) equity curve
    if results:
        import time as _t
        path = "data/backtest_equity.csv"
        with open(path, "w") as f:
            f.write("ts,iso,equity,gross_equity\n")
            for ts, eq, gr in results[0]["curve"]:
                iso = _t.strftime("%Y-%m-%dT%H:%M", _t.localtime(ts / 1000))
                f.write(f"{ts},{iso},{eq:.2f},{gr:.2f}\n")
        print(f"\nsaved {len(results[0]['curve'])}-point equity curve -> {path}")

    _multi_month_daily(cfg)


def _multi_month_daily(cfg: Config) -> None:
    """Multi-month test on DAILY bars — the only way to exceed ~50 days from KoinBay's 300-bar cap,
    and the better match for the live DAILY rebalance cadence. Uses day-scale momentum horizons."""
    import time as _t
    saved = list(cfg.signal.momentum_horizons)
    cfg.signal.momentum_horizons = ["1day", "3day", "7day", "14day"]
    log.info("loading DAILY history for the multi-month test (up to 300 bars ~ 10 months)...")
    try:
        hist = load_history(cfg, interval="1day", limit=300)
        if len(hist["T"]) < 60:
            log.error("not enough daily history (%d bars)", len(hist["T"]))
            return
        maker = cfg.execution.maker
        fee = 0.00025 if maker else 0.00075
        slip = 2.0 if maker else cfg.execution.slippage_bps
        m = simulate(hist, cfg, rebalance_every=1, taker_fee=fee, slippage_bps=slip)
        if "error" in m:
            log.error(m["error"]); return
        print(f"\n=== MULTI-MONTH BACKTEST (DAILY bars · {'MAKER' if maker else 'taker'} fees · "
              f"conviction {cfg.portfolio.conviction} · dyn-risk {cfg.risk.dynamic_risk}) ===")
        print(f"span ~{m['days']:.0f} days (~{m['days']/30:.1f} months) · {m['n_coins']} coins · {m['rebalances']} rebalances")
        print(f"  NET {m['net_return_pct']:.1f}%   gross {m['gross_return_pct']:.1f}%   fee-drag {m['fee_drag_pct']:.1f}%")
        print(f"  annualized {m['ann_return_pct']:.0f}%   Sharpe {m['sharpe']:.2f}   maxDD {m['max_dd_pct']:.1f}%   hit {m['hit_rate_pct']:.0f}%")
        buckets = {}
        for ts, eq, _g in m["curve"]:
            ym = _t.strftime("%Y-%m", _t.localtime(ts / 1000))
            if ym not in buckets:
                buckets[ym] = [eq, eq]
            buckets[ym][1] = eq
        print("  monthly return:")
        for ym in sorted(buckets):
            f0, f1 = buckets[ym]
            print(f"    {ym}:  {(f1 / f0 - 1) * 100:+6.1f}%")
        with open("data/backtest_6mo_equity.csv", "w") as fh:
            fh.write("ts,iso,equity,gross_equity\n")
            for ts, eq, gr in m["curve"]:
                fh.write(f"{ts},{_t.strftime('%Y-%m-%d', _t.localtime(ts / 1000))},{eq:.2f},{gr:.2f}\n")
        print("  saved -> data/backtest_6mo_equity.csv")
    except Exception as e:
        log.warning("daily backtest failed: %s", e)
    finally:
        cfg.signal.momentum_horizons = saved
