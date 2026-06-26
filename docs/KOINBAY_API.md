# KoinBay Exchange — REST & WebSocket API Reference

> **Live-verified 2026-06-03 · 38/38 REST endpoints confirmed.**
> This is the authoritative reference for this project. It supersedes the legacy whitelabel GitBook
> (`exchangedocsv2.gitbook.io/...`), which has placeholder hosts, wrong signing rules (base64 example is
> wrong), and routes that 404. When in doubt, this file wins.

---

## 1. Overview

### Live hosts

| Surface          | Base URL                              |
|------------------|---------------------------------------|
| Spot REST        | `https://openapi.koinbay.com`         |
| Futures REST     | `https://futuresopenapi.koinbay.com`  |
| Spot WebSocket   | `wss://ws.koinbay.com/kline-api/ws`   |
| Futures WebSocket| `wss://futuresws.koinbay.com/kline-api/ws` |

### Authentication & signing

Trade/account endpoints require three headers:

| Header         | Value                                            |
|----------------|--------------------------------------------------|
| `X-CH-APIKEY`  | API key                                          |
| `X-CH-TS`      | Request timestamp, Unix epoch **milliseconds**   |
| `X-CH-SIGN`    | HMAC-SHA256 signature, **lowercase hex**         |

**Pre-hash string:**

```
timestamp + METHOD + requestPath [+ "?" + queryString] [+ body]
```

- `METHOD` uppercase (`GET` / `POST`).
- **GET with query params: append `?` + the exact query string.** (Most common integration bug — the
  legacy prose omitted it.)
- **POST: append the exact JSON body string you send.**
- HMAC key = API secret. Output = **hex** (legacy base64 example is wrong).
- `recvWindow` (default 5000 ms) bounds clock skew. `Content-Type` is always `application/json`.

### Three error envelopes (clients must handle all three)

| Source                | Shape |
|-----------------------|-------|
| Spot business error   | `{"code": <int>, "msg": <str>}` |
| Futures business error| `{"code": "<str>", "succ": false, "msgData": null, "msg": <str>}` (string code) |
| Infrastructure / 404  | `{"timestamp","status","error","message","path"}` (Spring-style) |

### Order-ID precision warning

`orderId` values **exceed 2^53** → 64-bit-float JSON parsers corrupt them. **Treat all order IDs as
strings.** Spot returns `orderId` (number) AND `orderIdString` (string). Futures `POST /order` and
`POST /cancel` return `orderId` as a **string**.

---

## 2. Spot REST API — base `https://openapi.koinbay.com` (16 endpoints)

| Endpoint | Method | Path | Auth | Notes |
|----------|--------|------|------|-------|
| Ping | GET | `/sapi/v1/ping` | public | `{}` |
| Server time | GET | `/sapi/v1/time` | public | `{serverTime, timezone}` |
| Symbols | GET | `/sapi/v1/symbols` | public | use `baseAssetName` for display, UPPERCASE `symbol` for trading; `marketBuyMin/marketSellMin` NOT returned |
| Depth | GET | `/sapi/v1/depth` | public | `symbol` (req), `limit` (req, ≤100). `{asks, bids, time}` |
| 24h ticker | GET | `/sapi/v1/ticker` | public | `symbol` (req). buy/sell numbers; high/low/last/vol/rose/open strings |
| Recent trades | GET | `/sapi/v1/trades` | public | `symbol`,`limit` (req). Returns **`{"list":[...]}`**, not bare array. symbol UPPERCASE |
| Klines | GET | `/sapi/v1/klines` | public | intervals `1min,5min,15min,30min,60min,1day,1week,1month` (**`1h` returns `[]` on spot — use `60min`**) |
| Create order | POST | `/sapi/v1/order` | signed | body `{symbol, volume, side, type, price?}`. Returns `orderId` (number) + `orderIdString`; `status` int `0` on create |
| Test order | POST | `/sapi/v1/order/test` | signed | validates, does NOT hit matching engine. Returns `{}` |
| Batch orders | POST | `/sapi/v1/batchOrders` | signed | body `{symbol, orders[≤10]}`. Returns `idsString` + `ids` |
| Query order | GET | `/sapi/v1/order` | signed | `orderId`,`symbol`. `status` is a STRING here (NEW/PENDING_CANCEL/...) |
| Cancel order | POST | `/sapi/v1/cancel` | signed | `orderId`,`symbol`. Async → `status: PENDING_CANCEL` |
| Batch cancel | POST | `/sapi/v1/batchCancel` | signed | `{symbol, orderIds:[int]}` → `{success, failed}` |
| Open orders | GET | `/sapi/v1/openOrders` | signed | `symbol`,`limit`. Returns **`{"list":[...]}`** |
| My trades | GET | `/sapi/v1/myTrades` | signed | `symbol`,`limit`. Returns `{"list":[...]}`; uses bidId/askId/feeCoin |
| Account | GET | `/sapi/v1/account` | signed | `{"balances":[{uid,asset,free,locked}]}` — ALL coins incl. zero |

---

## 3. Futures REST API — base `https://futuresopenapi.koinbay.com` (22 endpoints)

> This is the surface the strategy trades on.

### Public market data

| Endpoint | Method | Path | Params | Notes |
|----------|--------|------|--------|-------|
| Ping | GET | `/fapi/v1/ping` | — | `{}` |
| Server time | GET | `/fapi/v1/time` | — | `{serverTime, timezone}` |
| Contracts | GET | `/fapi/v1/contracts` | — | see below |
| Depth | GET | `/fapi/v1/depth` | `contractName` (req), `limit` (≤100) | `{asks, bids, time}` |
| 24h ticker | GET | `/fapi/v1/ticker` | `contractName` (req) | `{high, vol, last, low, buy, sell, rose, time}` (vol in **contracts**) |
| Index / tag price | GET | `/fapi/v1/index` | `contractName` (req) | `{currentFundRate, tagPrice, indexPrice, nextFundRate}` (docs' `markPrice`/`lastFundingRate` are wrong) |
| Klines | GET | `/fapi/v1/klines` | `contractName` (req), `interval` (req), `limit` (≤300) | intervals `1min,5min,15min,30min,1h,1day,1week,1month` (**no `4h`** per this doc — ⚠ but see live note below) |

> ⚠ **Live probe 2026-06-18:** on the live futures host, `interval=1h` returned `[]` while `interval=4h`
> returned data — the *opposite* of the table above. Interval support drifts, so the engine auto-probes
> for a working interval (`marketdata.pick_base_interval`, order `4h,1h,30min,1day`) rather than trusting
> a fixed list.

**`GET /fapi/v1/contracts`** element:
```json
{
  "symbol": "E-BTC-USDT", "contractId": 1, "type": "E", "marginCoin": "USDT",
  "multiplier": 0.0001, "multiplierCoin": "BTC", "pricePrecision": 1,
  "minOrderVolume": 1, "maxMarketVolume": 5000000, "minLever": 0, "maxLever": 150,
  "openMakerFee": 0.00025, "openTakerFee": 0.00075,
  "closeMakerFee": 0.00025, "closeTakerFee": 0.00075, "status": 1
}
```
- `type` = **margin coin** (`E` = USDT-margined, the ones we trade). `contractName` is prefixed (`E-BTC-USDT`).
- **`status==0` is inactive (44/177) — MUST filter to `status==1`.**
- **`volume` is in contracts; notional = `volume * multiplier * price`.** (BTC: 1 contract = 0.0001 BTC.)

### Trade / account (signed)

**Create order — `POST /fapi/v1/order`**
```
body: {
  contractName,            // "E-BTC-USDT"
  volume,                  // number; for MARKET OPEN "unit is value" (notional) — AMBIGUOUS, prefer LIMIT
  price,                   // required for LIMIT
  type,                    // "LIMIT" | "MARKET"
  side,                    // "BUY" | "SELL"
  open,                    // "OPEN" | "CLOSE"
  positionType,            // 1 cross | 2 isolated
  clientOrderId?,          // ≤32 chars
  timeInForce?             // "IOC" | "FOK" | "POST_ONLY"
}
=> { "orderId": "3302507625176983697" }   // STRING
```

**Order info — `GET /fapi/v1/order`** · `contractName`,`orderId`(,`clientOrderId`)
```
=> { side, executedQty, orderId, origQty, avgPrice, tradeFee, realizedAmount,
     type, price, transactTime, action(OPEN/CLOSE), contractName, timeInForce, fills[],
     status }   // status: INIT -> PENDING_CANCEL ...; invalid orderId => {code,msg,data:null}
```

**Cancel — `POST /fapi/v1/cancel`** · `contractName`,`orderId` → `{ "orderId": "<string>" }` (async)

**Open orders — `GET /fapi/v1/openOrders`** · `contractName?` →
`{ "code":"0", "msg":"success", "data": null }` — **empty is `data:null`, NOT `[]`; `code` is a STRING.**

**Order history — `POST /fapi/v1/orderHistorical`** · `contractName?`,`limit?`,`fromId?` → array;
`type`/`status` are **integers** here.

**P&L history — `POST /fapi/v1/profitHistorical`** · `contractName?`,`limit?`,`fromId?` → array.

**My trades — `GET /fapi/v1/myTrades`** · `contractName` (req),`limit?`,`fromId?` → **bare array**;
elements carry both `symbol`(BTC-USDT) and `contractName`(E-BTC-USDT); `fee` is string.

**Positions — `GET /fapi/v1/positions`** · `contractName` (**required**, omitting → `-1121`) →
```json
{ "positions": [ { "symbol":"E-BTC-USDT", "leverage":20, "positionType":1,
  "avgPrice":66969.4, "positionSide":"BUY", "positionVolume":3.0,
  "canCloseVolume":3.0, "openPrice":66969.4, "realizedAmount":0.0 } ] }
```

**Account — `GET /fapi/v1/account`** (balance + ALL positions in one call):
```json
{ "account": [ {
  "marginCoin":"USDT", "accountNormal":999.56, "accountLock":23799.50,
  "achievedAmount":4156.51, "unrealizedAmount":650.64, "partEquity":13917.88,
  "positionVos": [ { "contractName":"E-BTC-USDT", "contractSymbol":"BTC-USDT",
    "positions": [ { "positionType":2, "side":"BUY", "volume":69642.0,
      "openPrice":11840.24, "avgPrice":11840.31, "leverageLevel":24,
      "marginRate":0.2097, "unRealizedAmount":2164.53 } ] } ] } ] }
```
Many top-level fields can be `null`. Margin coins seen: BTC, USDT, EXUSD, FILCOIN.

**Per-contract setup (signed):**

| Action | Method | Path | Body |
|--------|--------|------|------|
| Change leverage | POST | `/fapi/v1/edit_lever` | `{contractName, nowLevel:"<int>"}` |
| Change margin mode | POST | `/fapi/v1/edit_user_margin_model` | `{contractName, marginModel: 1 cross / 2 isolated}` |
| Change position mode | POST | `/fapi/v1/edit_user_position_model` | `{contractName, positionModel: 1 net / 2 two-way}` |

These return the `{code, msg, data}` envelope (msg may be Chinese, e.g. `成功`).

**Conditional / trigger orders:**

| Action | Method | Path | Notes |
|--------|--------|------|-------|
| Create condition order | POST | `/fapi/v1/conditionOrder/` | **trailing slash**. `triggerType` 1 SL / 2 TP / 3 chase-rise(OPEN) / 4 kill-fall. Returns envelope; id at `data.triggerIds[0]`. Use `triggerType=3` to OPEN |
| Trigger order list | POST | `/fapi/v1/trigger_order_list` | READ via POST; `{contractName, page?, limit?}` |
| Cancel trigger order | POST | `/v1/inner/trigger_order_cancel` | **no `/fapi/v1` prefix**; `{contractName, orderId}` where orderId = triggerOrderId |

---

## 4. WebSocket (market streams)

- Spot `wss://ws.koinbay.com/kline-api/ws`, Futures `wss://futuresws.koinbay.com/kline-api/ws`.
- Transport is **binary, gzip-compressed** (except ping/pong text frames). Market streams only — **no
  authenticated user-data stream** despite the docs' `USER_STREAM` type.
- Symbol format: spot = lowercase REST symbol (`btcusdt`); futures = `{type}_{pair}` lowercase
  (`E-BTC-USDT` → `e_btcusdt`).
- Heartbeat: server `{"ping": <ts>}` → client must reply `{"pong": <ts>}`.
- Subscribe: `{"event":"sub|unsub|req","params":{"channel":"...","cb_id":"1"}}`.
- Channels: `market_$symbol_depth_step0` (tick has `asks`/`buys`, not `bids`), `..._trade_ticker`,
  `..._kline_<interval>`, `..._ticker`, and futures-only `mark_price_$symbol` (funding + `nextSettlementTime`).

---

## 5. Error codes & enums

Spot error `{"code": -1121, "msg": "Invalid symbol."}`; futures wraps as
`{"code":"-1121","succ":false,"msgData":null,"msg":"..."}`.

| Code | Meaning |
|------|---------|
| -1002 | UNAUTHORIZED — `X-CH-APIKEY` missing |
| -1003 | TOO_MANY_REQUESTS — rate limited |
| -1021 | INVALID_TIMESTAMP — clock skew too large |
| -1022 | INVALID_SIGNATURE — signature mismatch |
| -1023 | `X-CH-TS` missing · -1024 `X-CH-SIGN` missing |
| -1100 | ILLEGAL_CHARS · -1102 MANDATORY_PARAM_EMPTY · -1103 UNKNOWN_PARAM |
| -1111 | BAD_PRECISION · -1147 PRICE_VOLUME_PRESION_ERROR |
| -1116 | INVALID_ORDER_TYPE (LIMIT/MARKET) · -1117 INVALID_SIDE (BUY/SELL) |
| -1121 | BAD_SYMBOL — invalid/missing symbol or contract |
| -1136 | ORDER_QUANTITY_TOO_SMALL — below min volume |
| -1138 | ORDER_PRICE_WAVE_EXCEED — price outside band |
| -1139 | ORDER_NOT_SUPPORT_MARKET — pair has no market orders |
| -1145 | order status does not allow cancellation |
| -2013 | NO_SUCH_ORDER · -2015 REJECTED_CH_KEY (bad key / IP not whitelisted) |
| -2016 | EXCHANGE_LOCK · -2017 balance_not_enough |
| -2100 | PARAM_ERROR (futures) · -2200 illegal/untrusted IP |
| 10015 | Exceeded the close available (CLOSE/stop with no open position) |

**Enums.** Order status (live): spot create → int `0`, then GET → `NEW` → `PENDING_CANCEL`;
futures → `INIT` → `PENDING_CANCEL`. **Cancellation is async** (returns `PENDING_CANCEL`, not `CANCELED`).
Order types: `LIMIT`, `MARKET`. Sides: `BUY`, `SELL`. Open/close: `OPEN`, `CLOSE`.
Kline intervals: spot `1min,5min,15min,30min,60min,1day,1week,1month` (`1h` → `[]`);
futures `1min,5min,15min,30min,1h,1day,1week,1month` (**no `4h`**).
