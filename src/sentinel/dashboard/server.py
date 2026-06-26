"""Tiny local dashboard (stdlib HTTP server + Chart.js, no extra deps).

Reads the same SQLite the loop writes to and serves:
  GET /            -> the HTML dashboard
  GET /api/state   -> JSON snapshot (equity curve, PnL, positions, smart-money whales)
Run via:  ./sentinel dashboard   (or  python run.py dashboard --port 8787)
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..config import Config
from ..state.store import Store

log = logging.getLogger("sentinel")
_CFG: Config = None  # set by serve()


def _loop_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", "run.py loop"], capture_output=True, text=True)
        return bool(out.stdout.strip())
    except Exception:
        return False


_marks = {"ts": 0.0, "px": {}}
_marks_lock = threading.Lock()
_fx_client = None


def _get_marks(cfg, symbols, ttl: float = 12.0) -> dict:
    """Current tagPrice per symbol from public /fapi/v1/index, cached server-side (TTL) so many
    5s browser polls share one fetch and we don't hammer the exchange."""
    global _fx_client
    if not symbols:
        return {}
    now = time.time()
    with _marks_lock:
        if now - _marks["ts"] < ttl and all(s in _marks["px"] for s in symbols):
            return dict(_marks["px"])
    from ..exchange.client import KoinbayClient
    from ..exchange.futures import KoinbayFutures
    if _fx_client is None:
        _fx_client = KoinbayFutures(KoinbayClient(cfg.exchange.futures_host, timeout_s=8))
    px = {}
    for s in symbols:
        try:
            p = float(_fx_client.index(s).get("tagPrice") or 0)
            if p > 0:
                px[s] = p
        except Exception:
            pass
    with _marks_lock:
        _marks["px"].update(px)
        _marks["ts"] = now
        return dict(_marks["px"])


_whales_cache = {"ts": 0.0, "data": None}
_whales_lock = threading.Lock()
_whale_tracker = None


def _get_whales(cfg, ttl: float = 60.0) -> dict:
    """Live blended whale bias for the dashboard, recomputed from current on-chain positions
    (cached ~60s). Falls back to the cached value on error."""
    global _whale_tracker
    if not cfg.whales.enabled:
        return {}
    now = time.time()
    with _whales_lock:
        if _whales_cache["data"] is not None and now - _whales_cache["ts"] < ttl:
            return _whales_cache["data"]
    import json
    import os
    addrs = list(cfg.whales.addresses)
    path = cfg.whales.source_file
    if path and os.path.exists(path):
        try:
            a = json.loads(open(path).read()).get("addresses")
            if a:
                addrs = a
        except Exception:
            pass
    if not addrs:
        return {}
    from ..signal.whale_tracker import WhaleTracker
    if _whale_tracker is None or set(_whale_tracker.addresses) != set(addrs):
        if _whale_tracker:
            _whale_tracker.close()
        _whale_tracker = WhaleTracker(addrs, cfg.whales.info_url, cfg.whales.weighting)
    try:
        snaps = _whale_tracker.fetch()
        bias = _whale_tracker.blended_bias(snaps)
    except Exception:
        return _whales_cache["data"] or {}
    consensus = [{"coin": c, "bias": round(v, 4)}
                 for c, v in sorted(bias.items(), key=lambda x: -abs(x[1])) if abs(v) >= 0.005][:14]
    wallets = [{"address": s.address, "equity": s.equity, "gross": s.gross, "net": s.net} for s in snaps]
    data = {"n_wallets": len(snaps), "consensus": consensus, "wallets": wallets, "ts": int(now)}
    with _whales_lock:
        _whales_cache["data"] = data
        _whales_cache["ts"] = now
    return data


def gather_state(cfg: Config) -> dict:
    store = Store(cfg.state.db_path, cfg.state.equity_csv)
    try:
        mode = cfg.mode
        series = store.equity_series(mode)
        acct = store.load_account(mode) or {}
        starting = float(acct.get("starting_capital") or cfg.capital_usdt or 10_000.0)
        realized = float(acct.get("realized_pnl") or 0.0)
        fees = float(acct.get("fees_paid") or 0.0)
        funding = float(acct.get("funding_pnl") or 0.0)
        whales = _get_whales(cfg) or store.latest_whales() or {}  # live, fallback to last cycle
        scores = store.scores_map(mode)
        lev = cfg.portfolio.per_name_leverage

        # ---- LIVE mark-to-market from the normalized positions table ----
        positions = store.load_positions(mode)
        marks = _get_marks(cfg, list(positions))
        book = []
        gross = net = live_unreal = 0.0
        for sym, p in positions.items():
            c = int(p["contracts"])
            if c == 0:
                continue
            entry = float(p["avg_price"])
            mult = float(p.get("multiplier", 1.0))
            mark = marks.get(sym) or entry
            notional = abs(c) * mark * mult
            upnl = (mark - entry) * c * mult
            gross += notional
            net += notional if c > 0 else -notional
            live_unreal += upnl
            book.append({"symbol": sym, "side": "LONG" if c > 0 else "SHORT", "contracts": c,
                         "entry": round(entry, 6), "mark": round(mark, 6),
                         "notional": round(notional, 2), "upnl": round(upnl, 2),
                         "score": round(scores[sym], 3) if sym in scores else None, "leverage": lev,
                         "entry_notional": round(abs(c) * entry * mult, 2),
                         "base_size": round(abs(c) * mult, 8)})
        book.sort(key=lambda x: -x["notional"])
        if book:
            eq = starting + realized + funding + live_unreal
        else:
            last_eq = store.last_equity(mode)
            eq = float(last_eq) if last_eq is not None else starting

        peak = max(float(store.peak_equity(mode, eq)), eq)
        pnl = eq - starting
        dd = max(0.0, (peak - eq) / peak * 100.0) if peak else 0.0
        # transient "now" point so the chart ticks live (not persisted to the DB)
        live_series = list(series) + [{"ts": int(time.time()), "equity": round(eq, 4),
                                       "gross": round(gross, 2), "net": round(net, 2),
                                       "drawdown_pct": round(dd, 4)}]
        return {
            "mode": cfg.mode,
            "running": _loop_running(),
            "rebalance_minutes": cfg.schedule.rebalance_minutes,
            "starting_capital": starting,
            "equity": eq,
            "pnl": pnl,
            "pnl_pct": (pnl / starting * 100.0) if starting else 0.0,
            "realized": realized,
            "unrealized": eq - starting - realized - funding,
            "funding": funding,
            "fees": fees,
            "peak": peak,
            "drawdown_pct": dd,
            "gross": gross,
            "net": net,
            "leverage": (gross / eq if eq else 0.0),
            "max_gross_leverage": cfg.risk.max_gross_leverage,
            "long_notional": (gross + net) / 2.0,
            "short_notional": (gross - net) / 2.0,
            "n_positions": len(book),
            "book_beta": store.get_meta("book_beta"),
            "dispersion": store.get_meta("dispersion"),
            "book": book,
            "whales": whales,
            "trade_stats": store.trade_stats(mode),   # tables are paginated via /api/trades & /api/fills
            "equity_series": live_series,
            "cycles": len(series),
        }
    finally:
        store.close()


def _page_payload(path: str) -> bytes:
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(path)
    q = parse_qs(parsed.query)

    def _int(name, default):
        try:
            return int(q.get(name, [default])[0])
        except (ValueError, TypeError):
            return default
    offset = max(0, _int("offset", 0))
    limit = min(100, max(1, _int("limit", 25)))
    store = Store(_CFG.state.db_path, _CFG.state.equity_csv)
    try:
        if parsed.path.endswith("/fills"):
            rows, total = store.fills_page(_CFG.mode, offset, limit)
        else:
            rows, total = store.closed_trades_page(_CFG.mode, offset, limit)
        return json.dumps({"rows": rows, "total": total, "offset": offset, "limit": limit}).encode()
    except Exception as e:  # pragma: no cover
        return json.dumps({"rows": [], "total": 0, "error": str(e)}).encode()
    finally:
        store.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence default stderr logging
        return

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            try:
                payload = json.dumps(gather_state(_CFG)).encode()
            except Exception as e:  # pragma: no cover
                payload = json.dumps({"error": str(e)}).encode()
            self._send(200, payload, "application/json")
        elif self.path.startswith("/api/trades") or self.path.startswith("/api/fills"):
            self._send(200, _page_payload(self.path), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, HTML.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")


def serve(cfg: Config, port: int = 8787, open_browser: bool = True) -> None:
    global _CFG
    _CFG = cfg
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    url = f"http://127.0.0.1:{port}"
    log.info("dashboard live at %s  (Ctrl-C to stop)", url)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("dashboard stopped")
    finally:
        httpd.server_close()


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Sentinel Edge</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#070a0f; --bg2:#0a0e15; --surface:#0f141d; --surface2:#131a25; --line:#1c2433;
    --txt:#eef2f8; --mut:#7d8799; --dim:#5a6473;
    --grn:#27d796; --grn-d:#0e3a2c; --red:#ff5d6c; --red-d:#3a1620;
    --accent:#6b8cff; --accent2:#9a6bff; --gold:#f5c451;
    --r:16px;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{background:
      radial-gradient(900px 420px at 78% -8%, rgba(107,140,255,.10), transparent 60%),
      radial-gradient(700px 380px at 10% -6%, rgba(154,107,255,.08), transparent 55%),
      var(--bg);
    color:var(--txt); font:14px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased; min-height:100vh}
  .num{font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1}
  .grn{color:var(--grn)} .red{color:var(--red)} .mut{color:var(--mut)}
  a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}

  header{display:flex;align-items:center;gap:14px;padding:16px 26px;border-bottom:1px solid var(--line);
    position:sticky;top:0;z-index:9;background:linear-gradient(180deg,rgba(8,11,17,.92),rgba(8,11,17,.72));backdrop-filter:blur(10px)}
  .brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:16px;letter-spacing:.2px}
  .brand .mark{width:26px;height:26px;border-radius:8px;display:grid;place-items:center;font-size:14px;
    background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 4px 16px rgba(107,140,255,.4)}
  .pill{font-size:11px;font-weight:700;padding:4px 10px;border-radius:999px;letter-spacing:.6px;text-transform:uppercase}
  .pill.paper{background:rgba(107,140,255,.14);color:#a9c0ff;border:1px solid rgba(107,140,255,.28)}
  .pill.live{background:var(--red-d);color:var(--red);border:1px solid rgba(255,93,108,.35)}
  .status{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--mut);
    background:var(--surface);border:1px solid var(--line);padding:5px 11px;border-radius:999px}
  .dot{width:8px;height:8px;border-radius:50%}
  .dot.on{background:var(--grn);box-shadow:0 0 0 0 rgba(39,215,150,.6);animation:pulse 1.8s infinite}
  .dot.off{background:#4b5563}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(39,215,150,.5)}70%{box-shadow:0 0 0 7px rgba(39,215,150,0)}100%{box-shadow:0 0 0 0 rgba(39,215,150,0)}}
  .meta{margin-left:auto;font-size:12px;color:var(--dim)}

  main{padding:22px 26px;max-width:1280px;margin:0 auto}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(232px,1fr));gap:14px;margin-bottom:18px}
  .card{position:relative;background:linear-gradient(180deg,var(--surface2),var(--surface));border:1px solid var(--line);
    border-radius:var(--r);padding:16px 18px;overflow:hidden;transition:transform .15s ease,border-color .15s ease}
  .card:hover{transform:translateY(-2px);border-color:#2a3445}
  .card::before{content:"";position:absolute;left:0;right:0;top:0;height:2px;background:linear-gradient(90deg,var(--accent),var(--accent2));opacity:.0;transition:opacity .15s}
  .card:hover::before{opacity:.7}
  .card .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.9px;color:var(--mut);margin-bottom:9px}
  .card .v{font-size:25px;font-weight:760;letter-spacing:-.3px}
  .card .s{font-size:12px;color:var(--mut);margin-top:6px}
  .bar{height:7px;border-radius:5px;background:#0a0f17;overflow:hidden;margin-top:11px;display:flex;border:1px solid #161d29}
  .bar>span{height:100%;display:block;transition:width .4s ease}

  .panel{background:linear-gradient(180deg,var(--surface2),var(--surface));border:1px solid var(--line);
    border-radius:var(--r);padding:18px 20px;margin-bottom:18px}
  .panel h2{font-size:11px;text-transform:uppercase;letter-spacing:.9px;color:var(--mut);margin:0 0 14px;font-weight:700}
  .ph{display:flex;align-items:flex-end;gap:14px;margin-bottom:12px}
  .ph .lab{font-size:10.5px;text-transform:uppercase;letter-spacing:.9px;color:var(--mut)}
  .ph .big{font-size:30px;font-weight:780;letter-spacing:-.5px;margin-top:2px}
  .seg{margin-left:auto;display:inline-flex;background:#0a0f17;border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px}
  .seg .t{font-size:11.5px;font-weight:600;color:var(--mut);border:0;background:transparent;border-radius:7px;padding:5px 11px;cursor:pointer;transition:.12s}
  .seg .t:hover{color:var(--txt)} .seg .t.on{color:#fff;background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 2px 10px rgba(107,140,255,.35)}
  .segs{display:flex;gap:10px;margin-bottom:6px;flex-wrap:wrap}

  .cols{display:grid;grid-template-columns:1.3fr 1fr;gap:18px}
  @media(max-width:920px){.cols{grid-template-columns:1fr}}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:11px 14px;white-space:nowrap;vertical-align:middle}
  thead tr{position:sticky;top:0;z-index:2}
  thead th{
    color:var(--dim);font-weight:700;font-size:10px;text-transform:uppercase;
    letter-spacing:.9px;background:rgba(8,11,17,.92);backdrop-filter:blur(10px);
    border-bottom:1px solid var(--line);border-top:0;
  }
  thead th.r{text-align:right}
  tbody tr{border-bottom:1px solid rgba(28,36,51,.45);transition:background .1s}
  tbody tr:hover{background:rgba(107,140,255,.07);cursor:default}
  tbody tr:last-child{border-bottom:0}
  td.r{text-align:right}
  td.num{font-variant-numeric:tabular-nums;font-family:'SF Mono','Fira Code','Courier New',monospace;font-size:12.5px}
  td.mut{color:var(--mut)}
  td.asset{font-weight:700;font-size:13.5px;letter-spacing:.4px}
  .chip{display:inline-block;font-size:10px;font-weight:800;padding:3px 9px;border-radius:5px;letter-spacing:.6px;text-transform:uppercase}
  .chip.LONG{background:rgba(39,215,150,.15);color:var(--grn);border:1px solid rgba(39,215,150,.25)}
  .chip.SHORT{background:rgba(255,93,108,.13);color:var(--red);border:1px solid rgba(255,93,108,.22)}
  .sc{font-size:11px;font-weight:700;padding:3px 8px;border-radius:6px;background:#0c121b;border:1px solid var(--line);font-variant-numeric:tabular-nums}

  .wrow{display:grid;grid-template-columns:58px 1fr 64px;align-items:center;gap:10px;padding:6px 0}
  .wrow .cn{font-weight:600;font-size:12.5px}
  .div{position:relative;height:16px;background:#0a0f17;border:1px solid #161d29;border-radius:6px;overflow:hidden}
  .div .c{position:absolute;left:50%;top:1px;bottom:1px;width:1px;background:#2a3445}
  .div .f{position:absolute;top:2px;bottom:2px;border-radius:4px}
  .div .f.l{background:linear-gradient(90deg,rgba(39,215,150,.5),var(--grn))}
  .div .f.s{background:linear-gradient(90deg,var(--red),rgba(255,93,108,.5))}
  .wpct{text-align:right;font-size:12px;font-weight:700}
  .wallets{margin-top:14px;border-top:1px solid var(--line);padding-top:12px;font-size:12px}
  .wallets .w{display:flex;justify-content:space-between;align-items:center;padding:4px 0;color:var(--mut)}
  #chart{max-height:340px}
  .empty{color:var(--dim);padding:14px 2px;font-size:13px}

  .share{background:#0c121b;border:1px solid var(--line);color:var(--mut);border-radius:7px;
    padding:3px 9px;cursor:pointer;font-size:13px;line-height:1;transition:.12s}
  .share:hover{color:#fff;border-color:var(--accent);box-shadow:0 0 0 2px rgba(107,140,255,.15)}
  .modal{position:fixed;inset:0;background:rgba(4,6,10,.78);backdrop-filter:blur(7px);
    display:none;align-items:center;justify-content:center;z-index:50;padding:22px}
  .modal-box{background:var(--surface);border:1px solid var(--line);border-radius:18px;max-width:780px;
    width:100%;overflow:hidden;box-shadow:0 30px 80px rgba(0,0,0,.6)}
  .modal-head{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;
    border-bottom:1px solid var(--line);font-size:14px;font-weight:600}
  .modal-head .x{background:transparent;border:0;color:var(--mut);font-size:17px;cursor:pointer}
  .modal-head .x:hover{color:#fff}
  #cardCanvas{display:block;width:100%;height:auto;background:#000}
  .modal-foot{display:flex;align-items:center;gap:12px;padding:14px 18px;flex-wrap:wrap}
  .swatches{display:flex;gap:8px;margin-right:auto}
  .sw{width:32px;height:32px;border-radius:9px;border:2px solid transparent;cursor:pointer;padding:0}
  .sw.on{border-color:#fff;box-shadow:0 0 0 2px rgba(255,255,255,.15)}
  .btn{background:#1a212b;border:1px solid var(--line);color:var(--txt);border-radius:10px;
    padding:9px 18px;font-weight:600;cursor:pointer;font-size:13px}
  .btn:hover{filter:brightness(1.12)}
  .btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:0;color:#fff}
  .tstrip{display:flex;flex-wrap:wrap;gap:22px;margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid var(--line)}
  .tstrip .it{display:flex;flex-direction:column;gap:3px}
  .tstrip .it b{font-size:17px;font-weight:750;font-variant-numeric:tabular-nums}
  .tstrip .it span{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--mut)}
  .tabwrap{padding-top:6px}
  .tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin:-6px -6px 16px;padding:0 6px;flex-wrap:wrap}
  .tab{background:transparent;border:0;border-bottom:2px solid transparent;color:var(--mut);font-size:13px;
    font-weight:650;padding:11px 15px;cursor:pointer;display:flex;align-items:center;gap:8px;transition:.12s;margin-bottom:-1px}
  .tab:hover{color:var(--txt)}
  .tab.on{color:var(--txt);border-bottom-color:var(--accent)}
  .tab b{font-size:10.5px;font-weight:700;background:#0c121b;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--mut)}
  .tab.on b{color:#fff;background:linear-gradient(135deg,var(--accent),var(--accent2));border:0}
  .pane{display:none} .pane.on{display:block;animation:fade .18s ease}
  @keyframes fade{from{opacity:0}to{opacity:1}}
  .pager{display:flex;align-items:center;gap:10px;margin-top:14px;padding-top:13px;border-top:1px solid var(--line);flex-wrap:wrap}
  .pager .info{font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
  .pager .ctrls{margin-left:auto;display:flex;gap:4px;align-items:center}
  .pgb{background:#0c121b;border:1px solid var(--line);color:var(--mut);min-width:32px;height:32px;
    padding:0 10px;border-radius:9px;font-size:12.5px;font-weight:650;cursor:pointer;transition:.12s;font-variant-numeric:tabular-nums}
  .pgb:hover:not(:disabled){color:#fff;border-color:#34506f;box-shadow:0 0 0 2px rgba(107,140,255,.12)}
  .pgb.on{color:#fff;background:linear-gradient(135deg,var(--accent),var(--accent2));border:0;box-shadow:0 2px 10px rgba(107,140,255,.35)}
  .pgb:disabled{opacity:.32;cursor:default}
  .pager .dots{color:var(--dim);padding:0 3px;font-weight:700}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="mark">⚡</span> Sentinel&nbsp;Edge</div>
  <span id="mode" class="pill paper">paper</span>
  <span class="status"><span id="dot" class="dot off"></span><span id="run">checking…</span></span>
  <span class="meta">updated <b id="upd">—</b> · <span id="cycles">0</span> cycles · rebal <span id="rebal">—</span></span>
</header>
<main>
  <div class="cards" id="cards"></div>

  <div class="panel">
    <div class="ph">
      <div><div class="lab">Performance</div><div class="big num" id="chartval">—</div></div>
      <div class="seg" id="metricTog"><button class="t on" data-m="pnl">PnL</button><button class="t" data-m="value">Value</button></div>
    </div>
    <div class="segs"><div class="seg" id="rangeTog">
      <button class="t" data-r="24H">24H</button><button class="t" data-r="7D">7D</button>
      <button class="t" data-r="30D">30D</button><button class="t on" data-r="ALL">ALL</button>
    </div></div>
    <canvas id="chart"></canvas>
  </div>

  <div class="panel tabwrap">
    <div class="tabs" id="tabs">
      <button class="tab on" data-t="positions">Positions <b id="np">0</b></button>
      <button class="tab" data-t="trades">Trades <b id="tcount">0</b></button>
      <button class="tab" data-t="fills">Fills</button>
      <button class="tab" data-t="smart">Smart Money <b id="nw">0</b></button>
    </div>

    <div class="pane on" id="pane-positions">
      <table><colgroup><col style="width:90px"><col style="width:80px"><col style="width:100px"><col style="width:120px"><col style="width:110px"><col style="width:120px"><col style="width:100px"><col style="width:90px"><col style="width:36px"></colgroup>
      <thead><tr><th>Asset</th><th>Side</th><th class="r">Size</th><th class="r">Value</th>
      <th class="r">Entry</th><th class="r">Mark</th><th class="r">uPnL</th><th class="r">Score</th><th></th></tr></thead>
      <tbody id="pos"></tbody></table>
    </div>

    <div class="pane" id="pane-trades">
      <div class="tstrip" id="tstrip"></div>
      <table><thead><tr><th>Asset</th><th>Side</th><th class="r">Entry</th><th class="r">Exit</th>
      <th class="r">Net PnL</th><th class="r">Duration</th><th class="r">Closed</th></tr></thead>
      <tbody id="ctrd"></tbody></table>

      <div class="pager" id="pgTrades"></div>
    </div>

    <div class="pane" id="pane-fills">
      <table><thead><tr><th>Asset</th><th>Side</th><th>Action</th><th class="r">Vol</th><th class="r">Price</th><th class="r">Status</th></tr></thead>
      <tbody id="fillrows"></tbody></table>
      <div class="pager" id="pgFills"></div>
    </div>

    <div class="pane" id="pane-smart">
      <div id="consensus"></div>
      <div class="wallets" id="wallets"></div>
    </div>
  </div>
</main>

<div id="cardModal" class="modal">
  <div class="modal-box">
    <div class="modal-head"><span>Share trade card</span><button class="x" onclick="closeCard()">✕</button></div>
    <canvas id="cardCanvas"></canvas>
    <div class="modal-foot">
      <div class="swatches" id="swatches"></div>
      <button class="btn" onclick="copyCard()">Copy</button>
      <button class="btn primary" onclick="downloadCard()">Download PNG</button>
    </div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
const money=n=>'$'+(+n||0).toLocaleString(undefined,{maximumFractionDigits:2});
const sgn=n=>(n>=0?'+':'−')+'$'+Math.abs(+n||0).toLocaleString(undefined,{maximumFractionDigits:2});
const sig6=n=>n==null?'—':(+n).toLocaleString(undefined,{maximumSignificantDigits:6});
const short=s=>(s||'').replace(/^E-|-USDT$/g,'');
const dur=s=>{s=+s||0;const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d}d ${h}h`:(h?`${h}h ${m}m`:`${m}m`);};
let DATA=null, chart=null, metric='pnl', range='ALL';
const RANGES={'24H':864e5,'7D':6048e5,'30D':2592e6,'ALL':Infinity};

function card(k,v,cls,s,bar){return `<div class="card"><div class="k">${k}</div><div class="v num ${cls||''}">${v}</div>${bar||''}${s?`<div class="s">${s}</div>`:''}</div>`}

function render(d){
  if(d.error){$('run').textContent='error: '+d.error;return;}
  DATA=d;
  $('mode').textContent=d.mode; $('mode').className='pill '+d.mode;
  $('dot').className='dot '+(d.running?'on':'off');
  $('run').textContent=d.running?'loop running':'loop stopped'; $('run').className=d.running?'grn':'mut';
  $('upd').textContent=new Date().toLocaleTimeString(); $('cycles').textContent=d.cycles; $('rebal').textContent=d.rebalance_minutes+'m';

  const pc=d.pnl>=0?'grn':'red';
  const lev=d.leverage||0, levPct=Math.min(100,lev/(d.max_gross_leverage||10)*100);
  const ln=d.long_notional||0, sn=d.short_notional||0, tot=ln+sn||1, lp=ln/tot*100, sp=sn/tot*100;
  const imb=(ln-sn)/tot;   // dollar-neutral book -> ~0; beta-hedge may run a small intentional imbalance,
  const bias=Math.abs(imb)<0.06?'Neutral ·':(imb>0?'Bullish ↑':'Bearish ↓');  // so neutral band sits above the 5% net rail
  const bc=Math.abs(imb)<0.06?'mut':(imb>0?'grn':'red');
  $('cards').innerHTML=[
    card('All-time PnL',sgn(d.pnl),pc,`<span class="${pc}">${d.pnl_pct>=0?'+':''}${d.pnl_pct.toFixed(2)}%</span> · equity ${money(d.equity)}`),
    card('Leverage',lev.toFixed(2)+'×','',`${money(d.gross)} notional · ${money(d.equity)} equity`,
      `<div class="bar"><span style="width:${levPct}%;background:linear-gradient(90deg,var(--accent),var(--accent2))"></span></div>`),
    card('Direction bias',`<span class="${bc}">${bias}</span>`,'',
      `${lp.toFixed(0)}% long · ${sp.toFixed(0)}% short`+(d.book_beta!=null?` · BTC-β ${(d.book_beta>=0?'+':'')}${d.book_beta.toFixed(2)}`:''),
      `<div class="bar"><span style="width:${lp}%;background:linear-gradient(90deg,rgba(39,215,150,.6),var(--grn))"></span><span style="width:${sp}%;background:linear-gradient(90deg,var(--red),rgba(255,93,108,.6))"></span></div>`),
    card('Drawdown',d.drawdown_pct.toFixed(2)+'%',d.drawdown_pct>0?'red':'',`peak ${money(d.peak)} · fees ${money(d.fees)} · funding ${sgn(d.funding||0)}`),
  ].join('');

  const b=d.book||[];
  $('np').textContent=b.length;
  $('pos').innerHTML=b.map((p,i)=>{
    const up=p.upnl, uc=up==null?'mut':(up>=0?'grn':'red');
    const scc=p.score>=0?'grn':'red';
    return `<tr><td class="asset">${short(p.symbol)}</td>
      <td><span class="chip ${p.side}">${p.side}</span></td>
      <td class="r num mut">${(+p.contracts).toLocaleString()}</td>
      <td class="r num">${money(p.notional)}</td>
      <td class="r num mut" style="font-size:11.5px">${sig6(p.entry)}</td>
      <td class="r num" style="font-size:11.5px">${sig6(p.mark)}</td>
      <td class="r num ${uc}" style="font-weight:700">${up!=null?sgn(up):'—'}</td>
      <td class="r"><span class="sc ${p.score!=null?scc:''}">${p.score!=null?(p.score>=0?'+':'')+(+p.score).toFixed(2):'—'}</span></td>
      <td class="r"><button class="share" title="Share" onclick="openCard(${i})">↗</button></td></tr>`;
  }).join('')||'<tr><td colspan="9" class="empty">no open positions</td></tr>';

  const w=d.whales||{}, cons=w.consensus||[];
  $('nw').textContent=w.n_wallets||0;
  $('consensus').innerHTML=cons.slice(0,12).map(c=>{
    const v=Math.max(-1,Math.min(1,c.bias)), mag=Math.abs(v)*50;
    const seg=v>=0?`<span class="f l" style="left:50%;width:${mag}%"></span>`:`<span class="f s" style="right:50%;width:${mag}%"></span>`;
    return `<div class="wrow"><span class="cn">${c.coin}</span>
      <span class="div"><span class="c"></span>${seg}</span>
      <span class="wpct ${v>=0?'grn':'red'}">${v>=0?'L':'S'} ${(Math.abs(v)*100).toFixed(0)}%</span></div>`;
  }).join('')||'<div class="empty">no whale data yet (populates next cycle)</div>';
  $('wallets').innerHTML=(w.wallets||[]).map(x=>{const net=x.net||0;
    return `<div class="w"><a href="https://hyperdash.com/address/${x.address}" target="_blank">${x.address.slice(0,6)}…${x.address.slice(-4)}</a>
      <span>${money(x.equity)} · <span class="${net>=0?'grn':'red'}">${net>=0?'net long':'net short'}</span></span></div>`;
  }).join('');

  const ts=d.trade_stats||{};
  $('tcount').textContent=ts.n||0;
  const pf=ts.profit_factor||0;
  $('tstrip').innerHTML=[
    ['Win rate',(ts.win_rate||0).toFixed(0)+'%',(ts.win_rate||0)>=50?'grn':'red'],
    ['W / L',`${ts.wins||0} / ${ts.losses||0}`,''],
    ['Avg win',sgn(ts.avg_win||0),'grn'],
    ['Avg loss',sgn(ts.avg_loss||0),'red'],
    ['Profit factor',pf?pf.toFixed(2):'—',pf>=1?'grn':'red'],
    ['Best',sgn(ts.max_win||0),'grn'],
    ['Worst',sgn(ts.max_loss||0),'red'],
    ['Total',sgn(ts.total_pnl||0),(ts.total_pnl||0)>=0?'grn':'red'],
  ].map(([k,v,c])=>`<div class="it"><b class="${c}">${v}</b><span>${k}</span></div>`).join('');
  const _at=document.querySelector('#tabs .tab.on'), _atid=_at?_at.dataset.t:'';
  if(_atid==='trades') pagerTrades.tick(); else if(_atid==='fills') pagerFills.tick();

  drawChart();
}

function drawChart(){
  if(!DATA) return;
  const now=Date.now(), span=RANGES[range];
  let s=(DATA.equity_series||[]).filter(r=>span===Infinity||r.ts*1000>=now-span);
  // long ranges track DAILY: collapse intraday ticks to one point/day (the day's closing equity)
  const daily=(range==='30D'||range==='ALL');
  if(daily){
    const byDay=new Map();
    for(const r of s){const d=new Date(r.ts*1000); byDay.set(d.getFullYear()+'-'+d.getMonth()+'-'+d.getDate(), r);}
    s=[...byDay.values()].sort((a,b)=>a.ts-b.ts);
  }
  const fmt=daily?{month:'short',day:'numeric'}:{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'};
  const labels=s.map(r=>new Date(r.ts*1000).toLocaleString([], fmt));
  const vals=s.map(r=>metric==='value'?r.equity:(r.equity-DATA.starting_capital));
  const last=vals.length?vals[vals.length-1]:0;
  $('chartval').innerHTML=metric==='value'?money(last):`<span class="${last>=0?'grn':'red'}">${sgn(last)}</span>`;
  const up=metric==='value'?true:last>=0;
  const line=up?'#27d796':'#ff5d6c';
  const ctx=$('chart').getContext('2d');
  const grad=ctx.createLinearGradient(0,0,0,340);
  grad.addColorStop(0, up?'rgba(39,215,150,.28)':'rgba(255,93,108,.28)');
  grad.addColorStop(1,'rgba(0,0,0,0)');
  const ds={data:vals,borderColor:line,backgroundColor:grad,fill:true,tension:.32,pointRadius:0,borderWidth:2.4};
  if(!chart){
    chart=new Chart(ctx,{type:'line',data:{labels,datasets:[ds]},
      options:{responsive:true,animation:{duration:300},plugins:{legend:{display:false},
        tooltip:{backgroundColor:'#0f141d',borderColor:'#1c2433',borderWidth:1,titleColor:'#7d8799',padding:10,
          callbacks:{label:c=>(metric==='value'?money(c.parsed.y):sgn(c.parsed.y))}}},
        scales:{x:{ticks:{color:'#5a6473',maxTicksLimit:8,font:{size:11}},grid:{color:'rgba(28,36,51,.5)'}},
                y:{ticks:{color:'#5a6473',font:{size:11}},grid:{color:'rgba(28,36,51,.5)'}}}}});
  }else{chart.data.labels=labels;chart.data.datasets[0]=ds;chart.update('none');}
}

document.querySelectorAll('#metricTog .t').forEach(t=>t.onclick=()=>{
  metric=t.dataset.m;document.querySelectorAll('#metricTog .t').forEach(x=>x.classList.toggle('on',x===t));drawChart();});
document.querySelectorAll('#rangeTog .t').forEach(t=>t.onclick=()=>{
  range=t.dataset.r;document.querySelectorAll('#rangeTog .t').forEach(x=>x.classList.toggle('on',x===t));drawChart();});
// ---- server-side paginated tables (Trades / Fills) ----
function pageList(page,pages){
  if(pages<=7) return Array.from({length:pages},(_,i)=>i+1);
  const out=[1];
  if(page>4) out.push('…');
  for(let i=Math.max(2,page-1);i<=Math.min(pages-1,page+1);i++) out.push(i);
  if(page<pages-3) out.push('…');
  out.push(pages); return out;
}
function tradeRow(t){const pc=t.pnl>=0?'grn':'red';
  return `<tr><td class="asset">${short(t.symbol)}</td><td><span class="chip ${t.side}">${t.side}</span></td>
  <td class="r num mut">${sig6(t.entry)}</td><td class="r num">${sig6(t.exit)}</td>
  <td class="r num ${pc}">${sgn(t.pnl)}</td><td class="r num mut">${dur(t.duration_s)}</td>
  <td class="r mut">${new Date(t.ts*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}</td></tr>`;}
function fillRow(t){const c=t.side==='BUY'?'grn':'red';
  return `<tr><td class="asset">${short(t.symbol)}</td><td class="${c}">${t.side}</td><td class="mut">${t.open_close}</td>
  <td class="r num">${(+t.volume).toLocaleString()}</td><td class="r num">${t.price}</td><td class="r mut">${t.status}</td></tr>`;}
function makePager(endpoint,tbodyId,pagerId,renderRow,gname,cols){
  let offset=0,total=0,loaded=false; const size=25;
  async function load(){
    let r; try{ r=await (await fetch(endpoint+`?offset=${offset}&limit=${size}`)).json(); }catch(e){ return; }
    total=r.total||0; loaded=true;
    $(tbodyId).innerHTML=(r.rows&&r.rows.length)?r.rows.map(renderRow).join(''):`<tr><td colspan="${cols}" class="empty">nothing yet</td></tr>`;
    draw();
  }
  function draw(){
    const pages=Math.max(1,Math.ceil(total/size)),page=Math.floor(offset/size)+1;
    const from=total?offset+1:0,to=Math.min(offset+size,total);
    const b=(lbl,p,on,dis)=>`<button class="pgb ${on?'on':''}" ${dis?'disabled':''} onclick="${gname}.go(${p})">${lbl}</button>`;
    let h=`<span class="info">${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}</span><div class="ctrls">`;
    h+=b('«',1,false,page<=1)+b('‹',Math.max(1,page-1),false,page<=1);
    for(const n of pageList(page,pages)) h+= n==='…'?'<span class="dots">…</span>':b(n,n,n===page,false);
    h+=b('›',page+1,false,page>=pages)+b('»',pages,false,page>=pages);
    $(pagerId).innerHTML=h+'</div>';
  }
  return { go(p){const pages=Math.max(1,Math.ceil(total/size));offset=Math.max(0,Math.min(pages-1,p-1))*size;load();},
           ensure(){if(!loaded)load();}, tick(){if(offset===0)load();} };
}
window.pagerTrades=makePager('/api/trades','ctrd','pgTrades',tradeRow,'pagerTrades',7);
window.pagerFills =makePager('/api/fills','fillrows','pgFills',fillRow,'pagerFills',6);

document.querySelectorAll('#tabs .tab').forEach(t=>t.onclick=()=>{
  const id=t.dataset.t;
  document.querySelectorAll('#tabs .tab').forEach(x=>x.classList.toggle('on',x===t));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='pane-'+id));
  if(id==='trades') pagerTrades.ensure(); else if(id==='fills') pagerFills.ensure();});

// ---- shareable trade card (canvas, procedural cosmic backgrounds) ----
let cardIdx=0, cardVar=0;
function stars(c,W,H,n){for(let i=0;i<n;i++){c.globalAlpha=Math.random()*.8+.2;c.fillStyle='#fff';
  c.beginPath();c.arc(Math.random()*W,Math.random()*H,Math.random()*1.5,0,7);c.fill();}c.globalAlpha=1;}
const VARS=[
 {n:'Nebula',sw:'radial-gradient(circle at 68% 32%,#7b3fb0,#0b0b16)',d:(c,W,H)=>{let g=c.createRadialGradient(W*.68,H*.34,30,W*.68,H*.34,W*.85);
   g.addColorStop(0,'#8a4ec4');g.addColorStop(.42,'#2a1640');g.addColorStop(1,'#07060d');c.fillStyle=g;c.fillRect(0,0,W,H);stars(c,W,H,240);}},
 {n:'Aurora',sw:'linear-gradient(135deg,#0a4a36,#04101a)',d:(c,W,H)=>{let g=c.createLinearGradient(0,0,W,H);
   g.addColorStop(0,'#062b22');g.addColorStop(.5,'#0a4a36');g.addColorStop(1,'#04101a');c.fillStyle=g;c.fillRect(0,0,W,H);
   for(let k=0;k<3;k++){let gg=c.createLinearGradient(0,0,W,H);gg.addColorStop(0,'rgba(39,215,150,0)');
     gg.addColorStop(.5,'rgba(39,215,150,.22)');gg.addColorStop(1,'rgba(39,215,150,0)');c.fillStyle=gg;c.fillRect(0,H*.12*k,W,H*.45);}stars(c,W,H,150);}},
 {n:'Deep space',sw:'radial-gradient(circle at 42% 50%,#15355f,#04050b)',d:(c,W,H)=>{let g=c.createRadialGradient(W*.42,H*.5,20,W*.42,H*.5,W);
   g.addColorStop(0,'#15355f');g.addColorStop(.5,'#0a1530');g.addColorStop(1,'#04050b');c.fillStyle=g;c.fillRect(0,0,W,H);stars(c,W,H,320);}},
 {n:'Ember',sw:'radial-gradient(circle at 70% 60%,#b3461f,#080404)',d:(c,W,H)=>{let g=c.createRadialGradient(W*.7,H*.6,20,W*.7,H*.6,W*.95);
   g.addColorStop(0,'#c8551f');g.addColorStop(.4,'#3a1208');g.addColorStop(1,'#080404');c.fillStyle=g;c.fillRect(0,0,W,H);stars(c,W,H,110);}},
 {n:'Mono',sw:'linear-gradient(135deg,#161d29,#070a0f)',d:(c,W,H)=>{let g=c.createLinearGradient(0,0,W,H);
   g.addColorStop(0,'#161d29');g.addColorStop(1,'#070a0f');c.fillStyle=g;c.fillRect(0,0,W,H);
   c.strokeStyle='rgba(255,255,255,.045)';for(let x=0;x<W;x+=44){c.beginPath();c.moveTo(x,0);c.lineTo(x,H);c.stroke();}
   for(let y=0;y<H;y+=44){c.beginPath();c.moveTo(0,y);c.lineTo(W,y);c.stroke();}}},
];
function roiPct(p){if(p.entry_notional&&p.leverage){const m=p.entry_notional/p.leverage;if(m>0)return p.upnl/m*100;}return 0;}
function sizeStr(p){const u=p.base_size!=null?p.base_size:Math.abs(p.contracts);
  return u.toLocaleString(undefined,{maximumFractionDigits:4})+' '+short(p.symbol);}
function drawCard(){
  const p=DATA.book[cardIdx]; if(!p)return;
  const cv=$('cardCanvas'),W=1200,H=675,c=cv.getContext('2d'); cv.width=W; cv.height=H;
  VARS[cardVar].d(c,W,H);
  let ov=c.createLinearGradient(0,0,W,0); ov.addColorStop(0,'rgba(0,0,0,.6)'); ov.addColorStop(.55,'rgba(0,0,0,.15)'); ov.addColorStop(1,'rgba(0,0,0,.4)');
  c.fillStyle=ov; c.fillRect(0,0,W,H);
  c.fillStyle='rgba(0,0,0,.34)'; c.fillRect(0,H-176,W,176);
  c.textBaseline='alphabetic'; c.textAlign='left';
  c.fillStyle='#fff'; c.font='800 27px ui-sans-serif,system-ui,Arial'; c.fillText('⚡ SENTINEL EDGE',58,74);
  c.fillStyle='rgba(255,255,255,.62)'; c.font='600 17px ui-sans-serif,system-ui,Arial'; c.textAlign='right';
  c.fillText(new Date().toLocaleString([], {month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'})+' · '+(DATA.mode||'paper').toUpperCase(),W-58,72);
  c.textAlign='left';
  const col=p.side==='LONG'?'#27d796':'#ff5d6c';
  c.fillStyle='#fff'; c.font='800 58px ui-sans-serif,system-ui,Arial'; const asset=short(p.symbol); c.fillText(asset,58,216);
  const aw=c.measureText(asset).width; c.fillStyle=col; c.font='800 30px ui-sans-serif,system-ui,Arial';
  c.fillText(`${p.side} ${p.leverage||1}X`,58+aw+24,216);
  const pnl=p.upnl||0, roi=roiPct(p), pc=pnl>=0?'#27d796':'#ff5d6c';
  c.fillStyle=pc; c.font='800 80px ui-sans-serif,system-ui,Arial';
  const pnlS=(pnl>=0?'+$':'−$')+Math.abs(pnl).toLocaleString(undefined,{maximumFractionDigits:2});
  c.fillText(pnlS,58,322);
  const bigW=c.measureText(pnlS).width;          // measured at 80px (was the bug: measured at small font)
  c.font='800 36px ui-sans-serif,system-ui,Arial'; c.fillStyle=pc;
  c.fillText(`(${roi>=0?'+':''}${roi.toFixed(2)}%)`,58+bigW+20,322);
  const rows=[['SIZE',sizeStr(p)],['ENTRY','$'+sig6(p.entry)],['MARK','$'+sig6(p.mark)]];
  c.font='600 22px ui-sans-serif,system-ui,Arial'; let y=H-120;
  rows.forEach(([k,v])=>{c.fillStyle='rgba(255,255,255,.5)';c.textAlign='left';c.fillText(k,58,y);
    c.fillStyle='#fff';c.textAlign='right';c.fillText(v,W-58,y);
    c.strokeStyle='rgba(255,255,255,.13)';c.beginPath();c.moveTo(58,y+15);c.lineTo(W-58,y+15);c.stroke();y+=44;});
  c.textAlign='left';
}
function renderSwatches(){$('swatches').innerHTML=VARS.map((v,i)=>`<button class="sw ${i===cardVar?'on':''}" style="background:${v.sw}" title="${v.n}" onclick="pickVar(${i})"></button>`).join('');}
function openCard(i){cardIdx=i;cardVar=0;$('cardModal').style.display='flex';renderSwatches();drawCard();}
function closeCard(){$('cardModal').style.display='none';}
function pickVar(i){cardVar=i;renderSwatches();drawCard();}
function downloadCard(){const p=DATA.book[cardIdx];$('cardCanvas').toBlob(b=>{const a=document.createElement('a');
  a.href=URL.createObjectURL(b);a.download=`sentinel-${short(p.symbol)}-${p.side}.png`;a.click();});}
function copyCard(){try{$('cardCanvas').toBlob(async b=>{try{await navigator.clipboard.write([new ClipboardItem({'image/png':b})]);}catch(e){downloadCard();}});}catch(e){downloadCard();}}
$('cardModal').addEventListener('click',e=>{if(e.target.id==='cardModal')closeCard();});

async function tick(){try{render(await (await fetch('/api/state')).json());}catch(e){$('run').textContent='offline';}}
tick();setInterval(tick,5000);
</script>
</body>
</html>"""
