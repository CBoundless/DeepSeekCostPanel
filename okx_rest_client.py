#!/usr/bin/env python3
"""OKX REST API v5 client (minimal).

Goals:
- Support OKX demo trading (simulated) via request header.
- Provide signed requests for private endpoints.
- Keep dependencies minimal (requests only).

Env vars (recommended):
- OKX_API_KEY
- OKX_API_SECRET
- OKX_PASSPHRASE
- OKX_BASE_URL (default: https://www.okx.com)
- OKX_BASE_URLS (optional): comma-separated base urls fallback, e.g. "https://www.okx.com,https://okx.com"
- OKX_SIMULATED_TRADING (default: 1)

Network tuning (optional):
- OKX_TIMEOUT (seconds, default: 15)
- OKX_RETRIES (default: 1)  # best-effort retries for transient errors
- OKX_TRUST_ENV (default: 1)  # 0 to ignore system proxy env vars
- OKX_HTTP_PROXY / OKX_HTTPS_PROXY / OKX_ALL_PROXY  # overrides HTTP_PROXY/HTTPS_PROXY/ALL_PROXY for OKX only
- OKX_CA_BUNDLE  # path to CA bundle file (maps to requests.verify)

Notes:
- This module intentionally does NOT store keys on disk.
- For safety, default simulated trading header is enabled.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests
from urllib.parse import urlencode


@dataclass
class OKXAuth:
    api_key: str
    api_secret: str
    passphrase: str


def _env_bool(name: str, default: bool) -> bool:
    v = (os.environ.get(name) or "").strip()
    if not v:
        return bool(default)
    return v not in ("0", "false", "False", "no", "NO")


def _env_int(name: str, default: int) -> int:
    v = (os.environ.get(name) or "").strip()
    if not v:
        return int(default)
    try:
        return int(float(v))
    except Exception:
        return int(default)


class OKXClient:
    def __init__(
        self,
        auth: Optional[OKXAuth] = None,
        base_url: Optional[str] = None,
        base_urls: Optional[list[str]] = None,
        simulated_trading: Optional[bool] = None,
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
        proxies: Optional[Dict[str, str]] = None,
        verify: Optional[object] = None,
        trust_env: Optional[bool] = None,
    ):
        if auth is None:
            k = (os.environ.get("OKX_API_KEY") or "").strip()
            s = (os.environ.get("OKX_API_SECRET") or "").strip()
            p = (os.environ.get("OKX_PASSPHRASE") or "").strip()
            auth = OKXAuth(k, s, p) if (k and s and p) else None

        self.auth = auth

        # Base URLs fallback
        env_many = (os.environ.get("OKX_BASE_URLS") or "").strip()
        env_one = (os.environ.get("OKX_BASE_URL") or "").strip()

        urls: list[str] = []
        if base_urls:
            urls.extend([str(u).strip() for u in base_urls if str(u).strip()])
        if base_url:
            urls.append(str(base_url).strip())
        if env_many:
            urls.extend([u.strip() for u in env_many.split(",") if u.strip()])
        if env_one:
            urls.append(env_one)

        # sensible defaults (some networks may block one but allow another)
        urls.extend(
            [
                "https://www.okx.com",
                "https://okx.com",
                "https://www.okex.com",
            ]
        )

        # normalize + de-dup preserve order
        dedup: list[str] = []
        seen = set()
        for u in urls:
            nu = str(u).rstrip("/")
            if not nu:
                continue
            if nu not in seen:
                dedup.append(nu)
                seen.add(nu)

        self.base_urls = dedup
        self.base_url = self.base_urls[0] if self.base_urls else "https://www.okx.com"

        if simulated_trading is None:
            simulated_trading = _env_bool("OKX_SIMULATED_TRADING", True)
        self.simulated_trading = bool(simulated_trading)

        if timeout is None:
            timeout = _env_int("OKX_TIMEOUT", 15)
        self.timeout = int(timeout)

        if retries is None:
            retries = _env_int("OKX_RETRIES", 1)
        self.retries = max(0, int(retries))

        if trust_env is None:
            trust_env = _env_bool("OKX_TRUST_ENV", True)

        # per-client session: allows trust_env control
        self.session = requests.Session()
        self.session.trust_env = bool(trust_env)

        # proxies: prefer OKX_*_PROXY; fallback to standard env vars
        if proxies is None:
            p: Dict[str, str] = {}
            hp = (os.environ.get("OKX_HTTP_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()
            sp = (os.environ.get("OKX_HTTPS_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
            ap = (os.environ.get("OKX_ALL_PROXY") or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or "").strip()

            if hp:
                p["http"] = hp
            if sp:
                p["https"] = sp

            # if only ALL_PROXY is set, apply to both schemes
            if ap and not p:
                p = {"http": ap, "https": ap}

            proxies = p or None

        self.proxies = proxies

        # verify: allow passing CA bundle path, or read from env
        if verify is None:
            ca = (
                (os.environ.get("OKX_CA_BUNDLE") or "").strip()
                or (os.environ.get("REQUESTS_CA_BUNDLE") or "").strip()
                or (os.environ.get("SSL_CERT_FILE") or "").strip()
            )
            verify = ca if ca else True

        self.verify = verify

        # best-effort retries adapter (works for transient issues; won't fix blocked TLS)
        if self.retries > 0:
            try:
                from requests.adapters import HTTPAdapter
                from urllib3.util.retry import Retry

                retry_kwargs = {
                    "total": int(self.retries),
                    "connect": int(self.retries),
                    "read": int(self.retries),
                    "status": int(self.retries),
                    "backoff_factor": 0.2,
                    "status_forcelist": (429, 500, 502, 503, 504),
                    "raise_on_status": False,
                }

                try:
                    retry = Retry(**retry_kwargs, allowed_methods=None)
                except TypeError:
                    # older urllib3
                    retry = Retry(**retry_kwargs, method_whitelist=None)  # type: ignore[arg-type]

                adapter = HTTPAdapter(max_retries=retry)
                self.session.mount("https://", adapter)
                self.session.mount("http://", adapter)
            except Exception:
                pass

    # ---------- helpers ----------
    @staticmethod
    def _iso_timestamp() -> str:
        # OKX typically accepts RFC3339 with milliseconds, UTC 'Z'
        t = time.time()
        ms = int((t - int(t)) * 1000)
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{ms:03d}Z"

    @staticmethod
    def _sign(timestamp: str, method: str, request_path: str, body: str, secret: str) -> str:
        prehash = f"{timestamp}{method.upper()}{request_path}{body}"
        mac = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(mac).decode("utf-8")

    def _headers(self, method: str, request_path: str, body: str) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "DeepSeekCostPanel/1.0",
        }

        # Demo trading header
        if self.simulated_trading:
            headers["x-simulated-trading"] = "1"

        if self.auth is None:
            return headers

        ts = self._iso_timestamp()
        sign = self._sign(ts, method, request_path, body, self.auth.api_secret)
        headers.update(
            {
                "OK-ACCESS-KEY": self.auth.api_key,
                "OK-ACCESS-SIGN": sign,
                "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": self.auth.passphrase,
            }
        )
        return headers

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request_path = path if path.startswith("/") else f"/{path}"

        body = ""
        raw_body = None
        if data is not None:
            # OKX 签名要求 body 与实际发送内容完全一致，不能让 requests 再次序列化。
            body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
            raw_body = body.encode("utf-8")

        signed_path = request_path
        if params:
            query = urlencode([(str(k), "" if v is None else str(v)) for k, v in params.items()])
            if query:
                signed_path = f"{request_path}?{query}"

        headers = self._headers(method, signed_path, body)

        errs: list[str] = []
        for base in (self.base_urls or [self.base_url]):
            url = f"{base}{request_path}"
            try:
                r = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    data=raw_body,
                    headers=headers,
                    timeout=self.timeout,
                    proxies=self.proxies,
                    verify=self.verify,
                )

                try:
                    payload = r.json()
                except Exception:
                    payload = {"code": str(r.status_code), "msg": r.text}

                # Remember the working base URL
                if r.status_code == 200:
                    self.base_url = base

                if r.status_code != 200:
                    payload.setdefault("code", str(r.status_code))
                    payload.setdefault("msg", "HTTP error")

                return payload
            except requests.exceptions.RequestException as e:
                errs.append(f"{base} -> {type(e).__name__}: {e}")
                continue

        # All bases failed
        env_flags = {
            "OKX_HTTP_PROXY": bool((os.environ.get("OKX_HTTP_PROXY") or "").strip()),
            "OKX_HTTPS_PROXY": bool((os.environ.get("OKX_HTTPS_PROXY") or "").strip()),
            "OKX_ALL_PROXY": bool((os.environ.get("OKX_ALL_PROXY") or "").strip()),
            "HTTP_PROXY": bool((os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()),
            "HTTPS_PROXY": bool((os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()),
            "ALL_PROXY": bool((os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or "").strip()),
            "OKX_CA_BUNDLE": bool((os.environ.get("OKX_CA_BUNDLE") or "").strip()),
            "REQUESTS_CA_BUNDLE": bool((os.environ.get("REQUESTS_CA_BUNDLE") or "").strip()),
            "SSL_CERT_FILE": bool((os.environ.get("SSL_CERT_FILE") or "").strip()),
        }

        details = "\n".join((errs[-5:] if errs else ["(no error details)"]))

        hint = (
            "无法连接 OKX（网络/代理/DNS/地区限制导致）。你可以尝试：\n"
            "1) 换网络（手机热点）或开启可访问 OKX 的 VPN/代理\n"
            "2) 设置环境变量 OKX_BASE_URL 或 OKX_BASE_URLS 切换域名\n"
            '   例如：export OKX_BASE_URLS="https://okx.com,https://www.okx.com"\n'
            "3) 如走代理：设置 OKX_HTTPS_PROXY/OKX_HTTP_PROXY（或 HTTPS_PROXY/HTTP_PROXY）\n"
            "4) 若公司网关做 HTTPS 证书替换：需要配置 CA（OKX_CA_BUNDLE 或 REQUESTS_CA_BUNDLE）\n"
        )

        return {
            "code": "NETWORK_ERROR",
            "msg": f"{details}\n\n当前环境变量(是否设置)：{env_flags}\n\n{hint}",
            "data": [],
        }

    # ---------- public endpoints ----------
    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> Dict[str, Any]:
        # GET /api/v5/market/candles?instId=BTC-USDT&bar=1H&limit=100
        return self._request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": int(limit)},
        )

    def get_ticker(self, inst_id: str) -> Dict[str, Any]:
        # GET /api/v5/market/ticker?instId=BTC-USDT
        return self._request("GET", "/api/v5/market/ticker", params={"instId": inst_id})

    def get_instruments(self, inst_type: str = "SPOT", inst_id: Optional[str] = None) -> Dict[str, Any]:
        # GET /api/v5/public/instruments?instType=SPOT&instId=BTC-USDT
        params: Dict[str, Any] = {"instType": str(inst_type or "SPOT").upper()}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v5/public/instruments", params=params)

    # ---------- private endpoints ----------
    def require_auth(self):
        if self.auth is None or not (self.auth.api_key and self.auth.api_secret and self.auth.passphrase):
            raise RuntimeError("缺少 OKX API 凭证：请设置环境变量 OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE")

    def get_balance(self, ccy: Optional[str] = None) -> Dict[str, Any]:
        # GET /api/v5/account/balance?ccy=USDT,BTC
        self.require_auth()
        params = {"ccy": ccy} if ccy else None
        return self._request("GET", "/api/v5/account/balance", params=params)

    def get_order(
        self,
        *,
        inst_id: str,
        ord_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        # GET /api/v5/trade/order?instId=BTC-USDT&ordId=123456
        self.require_auth()
        params: Dict[str, Any] = {"instId": inst_id}
        if ord_id:
            params["ordId"] = ord_id
        if cl_ord_id:
            params["clOrdId"] = cl_ord_id
        return self._request("GET", "/api/v5/trade/order", params=params)

    def get_positions(self, *, inst_type: Optional[str] = None, inst_id: Optional[str] = None) -> Dict[str, Any]:
        # GET /api/v5/account/positions?instType=SWAP&instId=BTC-USDT-SWAP
        self.require_auth()
        params: Dict[str, Any] = {}
        if inst_type:
            params["instType"] = str(inst_type).upper()
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v5/account/positions", params=params or None)

    def place_order(
        self,
        *,
        inst_id: str,
        td_mode: str,
        side: str,
        ord_type: str,
        sz: str,
        tgt_ccy: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
        pos_side: Optional[str] = None,
        reduce_only: Optional[bool] = None,
    ) -> Dict[str, Any]:
        # POST /api/v5/trade/order
        self.require_auth()
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": str(sz),
        }
        if tgt_ccy:
            body["tgtCcy"] = tgt_ccy
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        if pos_side:
            body["posSide"] = pos_side
        if reduce_only is not None:
            body["reduceOnly"] = bool(reduce_only)
        return self._request("POST", "/api/v5/trade/order", data=body)

    def cancel_order(self, *, inst_id: str, ord_id: Optional[str] = None, cl_ord_id: Optional[str] = None) -> Dict[str, Any]:
        # POST /api/v5/trade/cancel-order
        self.require_auth()
        body: Dict[str, Any] = {"instId": inst_id}
        if ord_id:
            body["ordId"] = ord_id
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        return self._request("POST", "/api/v5/trade/cancel-order", data=body)


def okx_extract_first_data(payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Helper to extract OKX standard response: {code,msg,data:[...]}

    Returns:
        (first_data_or_none, error_message)
    """
    if not isinstance(payload, dict):
        return None, "响应不是 dict"

    code = str(payload.get("code") or "")
    if code and code != "0":
        return None, f"OKX error code={code} msg={payload.get('msg')}"

    data = payload.get("data")
    if isinstance(data, list) and data:
        x = data[0]
        return (x if isinstance(x, dict) else None), ""

    return None, "OKX 返回 data 为空"
