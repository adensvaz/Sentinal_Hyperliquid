"""KoinBay REST client: X-CH-* HMAC signing + request plumbing + the 3 error envelopes.

Signing (verified 2026-06-03, see docs/KOINBAY_API.md):
    prehash = timestamp + METHOD + requestPath [+ "?" + queryString] [+ body]
    X-CH-SIGN = lowercase hex HMAC-SHA256(secret, prehash)
    X-CH-TS   = epoch milliseconds
The same querystring/body bytes that are signed MUST be the ones sent on the wire, so this module builds
them once and reuses them.
"""
from __future__ import annotations

import hmac
import json
import time
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode

import httpx


# ---- pure helpers (unit-tested directly) -----------------------------------

def build_prehash(timestamp: str, method: str, path: str, query: str = "", body: str = "") -> str:
    """Construct the exact string that gets HMAC'd. `query` is WITHOUT the leading '?'."""
    s = f"{timestamp}{method.upper()}{path}"
    if query:
        s += f"?{query}"
    if body:
        s += body
    return s


def sign(secret: str, prehash: str) -> str:
    """Lowercase hex HMAC-SHA256."""
    return hmac.new(secret.encode(), prehash.encode(), sha256).hexdigest()


def encode_query(params: dict[str, Any] | None) -> str:
    """Deterministic querystring (no leading '?'); None values dropped."""
    if not params:
        return ""
    clean = {k: v for k, v in params.items() if v is not None}
    return urlencode(clean)


def encode_body(body: dict[str, Any] | None) -> str:
    """Compact JSON body string; None values dropped. Empty body -> ''."""
    if not body:
        return ""
    clean = {k: v for k, v in body.items() if v is not None}
    return json.dumps(clean, separators=(",", ":"))


# ---- errors -----------------------------------------------------------------

class KoinbayAPIError(Exception):
    def __init__(self, code: Any, msg: str | None, raw: Any = None, kind: str = "business"):
        self.code = code
        self.msg = msg
        self.raw = raw
        self.kind = kind  # business | infra | http
        super().__init__(f"[{kind}] code={code} msg={msg}")


_SPRING_KEYS = {"timestamp", "status", "error", "path"}


def _interpret(status_code: int, body: Any) -> Any:
    """Raise on any of the 3 error envelopes; otherwise return the parsed body."""
    if isinstance(body, dict):
        # infra / 404 (Spring-style)
        if _SPRING_KEYS.issubset(body.keys()):
            raise KoinbayAPIError(body.get("status"), body.get("message") or body.get("error"),
                                  raw=body, kind="infra")
        # business error: spot {code:int,msg}; futures {code:"str",succ:false,msg}.
        # success envelopes use code 0 / "0"; absence of "code" is also success.
        if "code" in body and str(body["code"]) not in ("0",):
            raise KoinbayAPIError(body["code"], body.get("msg"), raw=body, kind="business")
    if status_code >= 400:
        raise KoinbayAPIError(status_code, str(body), raw=body, kind="http")
    return body


# ---- client -----------------------------------------------------------------

class KoinbayClient:
    """One client per host (spot or futures). Signing is identical across both."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        timeout_s: float = 15.0,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.max_retries = max_retries
        self._http = httpx.Client(timeout=timeout_s)

    # -- low level
    @staticmethod
    def _ts() -> str:
        return str(int(time.time() * 1000))

    def _signed_headers(self, ts: str, method: str, path: str, query: str, body: str) -> dict[str, str]:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("signed request requires api_key/api_secret")
        prehash = build_prehash(ts, method, path, query, body)
        return {
            "X-CH-APIKEY": self.api_key,
            "X-CH-TS": ts,
            "X-CH-SIGN": sign(self.api_secret, prehash),
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        query = encode_query(params)
        url = self.base_url + path + (f"?{query}" if query else "")
        headers = {"Content-Type": "application/json"}
        if signed:
            headers = self._signed_headers(self._ts(), "GET", path, query, "")
        return self._request("GET", url, headers=headers)

    def post(self, path: str, body: dict[str, Any] | None = None, signed: bool = False) -> Any:
        body_str = encode_body(body)
        url = self.base_url + path
        headers = {"Content-Type": "application/json"}
        if signed:
            headers = self._signed_headers(self._ts(), "POST", path, "", body_str)
        return self._request("POST", url, headers=headers, content=body_str)

    def _request(self, method: str, url: str, headers: dict[str, str], content: str | None = None) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._http.request(method, url, headers=headers, content=content)
                body = self._parse(resp)
                # only auto-retry idempotent GETs on transient rate-limit/server errors
                if resp.status_code in (429, 418, 500, 502, 503, 504) and method == "GET":
                    last_exc = KoinbayAPIError(resp.status_code, "transient", kind="http")
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                return _interpret(resp.status_code, body)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                if method != "GET":
                    raise
                time.sleep(0.5 * (2 ** attempt))
        if last_exc:
            raise last_exc
        raise RuntimeError("request failed without an exception")  # pragma: no cover

    @staticmethod
    def _parse(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return {"_raw_text": resp.text, "status": resp.status_code}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "KoinbayClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
