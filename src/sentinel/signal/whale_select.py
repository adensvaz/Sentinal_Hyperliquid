"""Screen the Hyperliquid leaderboard and select a weekly basket of high-quality whales.

Methodology (see README): hard gates for skill / usability / FRESHNESS, then a composite percentile rank
that balances money, risk-adjusted skill, consistency, recent profitability, and recency of activity.

Hard gates (all must pass):
  - active within `max_inactive_days` (last on-chain fill)         <- recency
  - 30d PnL > 0, equity >= $100k, >= `min_closes` closing trades
  - win rate >= `min_win_rate` (auto-relaxed if too few qualify)
  - >= `min_overlap` of book in KoinBay-listed coins, net-directional >= `min_dir`

Composite rank (among survivors):
  q = 0.35*pct(30d PnL) + 0.20*pct(30d ROI) + 0.20*win_rate + 0.10*pct(7d PnL) + 0.15*recency
"""
from __future__ import annotations

import json
import os
import time

import httpx

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def _fnum(x, d: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _window(row: dict, name: str) -> dict:
    for k, v in row.get("windowPerformances", []):
        if k == name:
            return v
    return {}


def fetch_candidates(http: httpx.Client, pool: int) -> list[dict]:
    rows = http.get(LEADERBOARD_URL).json().get("leaderboardRows", [])
    cand = []
    for r in rows:
        m, wk = _window(r, "month"), _window(r, "week")
        cand.append({"address": r.get("ethAddress"), "equity": _fnum(r.get("accountValue")),
                     "month_pnl": _fnum(m.get("pnl")), "month_roi": _fnum(m.get("roi")),
                     "week_pnl": _fnum(wk.get("pnl")), "week_roi": _fnum(wk.get("roi"))})
    cand.sort(key=lambda x: -x["month_pnl"])
    return cand[:pool]


def wallet_stats(http: httpx.Client, addr: str, info_url: str, kb_coins: set) -> dict:
    fills = http.post(info_url, json={"type": "userFills", "user": addr}).json()
    if not isinstance(fills, list):
        fills = []
    closes = [f for f in fills if _fnum(f.get("closedPnl")) != 0]
    wins = sum(1 for f in closes if _fnum(f.get("closedPnl")) > 0)
    win_rate = (wins / len(closes)) if closes else 0.0
    last_fill_ms = max((int(f.get("time", 0) or 0) for f in fills), default=0)

    st = http.post(info_url, json={"type": "clearinghouseState", "user": addr}).json()
    eq = _fnum(st.get("marginSummary", {}).get("accountValue"))
    gross = net = overlap = 0.0
    for p in st.get("assetPositions", []):
        pos = p.get("position", {})
        szi = _fnum(pos.get("szi"))
        pv = _fnum(pos.get("positionValue"))
        if pv == 0:
            continue
        gross += pv
        net += pv if szi > 0 else -pv
        if str(pos.get("coin", "")).split(":")[-1].upper() in kb_coins:
            overlap += pv
    return {"win_rate": win_rate, "n_closes": len(closes), "equity": eq, "gross": gross,
            "last_fill_ms": last_fill_ms,
            "overlap": (overlap / gross if gross else 0.0), "dir": (abs(net) / gross if gross else 0.0)}


def _pctile_fn(values: list[float]):
    s = sorted(values)
    n = len(s)
    if n <= 1:
        return lambda x: 1.0
    return lambda x: (sum(1 for v in s if v <= x) - 1) / (n - 1)


def rank_candidates(rows: list[dict], top: int = 10, min_win_rate: float = 0.6,
                    max_inactive_days: float = 5.0, win_rate_floor: float = 0.5) -> list[dict]:
    """Pure ranking: gate by win rate (PROGRESSIVELY relaxed down to win_rate_floor if too few
    qualify), score by the composite, return top N.
    Each row needs: month_pnl, month_roi, week_pnl, win_rate, days_since."""
    if not rows:
        return []
    thr = min_win_rate
    gated = [r for r in rows if r["win_rate"] >= thr]
    while len(gated) < top and round(thr - 0.05, 2) >= win_rate_floor:
        thr = round(thr - 0.05, 2)
        gated = [r for r in rows if r["win_rate"] >= thr]
    if len(gated) < top:   # still short even at the floor -> take everything that passed the hard gates
        gated = list(rows)
    p_pnl = _pctile_fn([r["month_pnl"] for r in gated])
    p_roi = _pctile_fn([r["month_roi"] for r in gated])
    p_wk = _pctile_fn([r["week_pnl"] for r in gated])
    for r in gated:
        rec = max(0.0, 1.0 - r["days_since"] / max_inactive_days) if max_inactive_days else 0.0
        r["q"] = (0.35 * p_pnl(r["month_pnl"]) + 0.20 * p_roi(r["month_roi"])
                  + 0.20 * r["win_rate"] + 0.10 * p_wk(r["week_pnl"]) + 0.15 * rec)
    gated.sort(key=lambda r: -r["q"])
    return gated[:top]


def select_top_whales(
    kb_coins: set,
    info_url: str = "https://api.hyperliquid.xyz/info",
    pool: int = 200,
    top: int = 10,
    min_win_rate: float = 0.6,
    min_overlap: float = 0.15,
    min_dir: float = 0.2,
    min_equity: float = 100_000,
    min_closes: int = 20,
    max_inactive_days: float = 5.0,
    now_ms: int = None,
    timeout: float = 30.0,
    log=None,
) -> list[dict]:
    http = httpx.Client(timeout=timeout)
    try:
        now_ms = now_ms or int(time.time() * 1000)
        rows = []
        for c in fetch_candidates(http, pool):
            if c["equity"] < min_equity or c["month_pnl"] <= 0:
                continue
            try:
                s = wallet_stats(http, c["address"], info_url, kb_coins)
            except Exception:
                continue
            if s["gross"] <= 0 or s["overlap"] < min_overlap or s["dir"] < min_dir or s["n_closes"] < min_closes:
                continue
            days = max(0.0, (now_ms - s["last_fill_ms"]) / 86_400_000) if s["last_fill_ms"] else 1e9
            if days > max_inactive_days:   # recency gate: skip wallets that have gone quiet
                continue
            rows.append({**c, **s, "days_since": round(days, 2)})
            if log:
                log.info("  ok %s  win=%.0f%%  30dPnL=$%.0f  roi=%.0f%%  active=%.1fd  overlap=%.0f%%",
                         c["address"][:10], s["win_rate"] * 100, c["month_pnl"],
                         c["month_roi"] * 100, days, s["overlap"] * 100)
        return rank_candidates(rows, top, min_win_rate, max_inactive_days)
    finally:
        http.close()


def write_basket(path: str, picks: list[dict]) -> dict:
    out = {
        "selected_ts": int(time.time()),
        "method": ("active<=5d; win-rate gated; rank = 35% 30dPnL + 20% ROI + 20% winrate "
                   "+ 10% 7dPnL + 15% recency"),
        "addresses": [p["address"] for p in picks],
        "detail": [{k: (round(p[k], 4) if isinstance(p.get(k), float) else p.get(k))
                    for k in ("address", "win_rate", "month_pnl", "month_roi", "week_pnl",
                              "days_since", "overlap", "equity", "q")}
                   for p in picks],
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(out, indent=2))
    return out
