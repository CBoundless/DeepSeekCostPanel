#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests


@dataclass
class BinanceAuth:
    api_key: str
    api_secret: str


class BinanceClient:
    def __init__(
        self,
        auth: Optional[BinanceAuth] = None,
        *,
        simulated_trading: bool = True,
        base_url: Optional[str] = None,
        timeout: int = 15,
    ) -> None:
        self.auth = auth
        self.simulated_trading = bool(simulated_trading)
        self.timeout = max(5, int(timeout or 15))
        self.exchange_name = "binance"
        self.base_url = (base_url or ("https://testnet.binance.vision" if self.simulated_trading else "https://api.binance.com")).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "DeepSeekCostPanel/1.0"})

    def require_auth(self) -> None:
        if not self.auth or not self.auth.api_key or not self.auth.api_secret:
            raise RuntimeError("缺少 Binance API Key / Secret，请先完善账号配置")

    def get_ticker(self, inst_id: str) -> Dict[str, Any]:
        symbol = self._to_symbol(inst_id)
        payload = self._request("GET", "/api/v3/ticker/price", params={"symbol": symbol})
        if "code" in payload and payload.get("code") != "0":
            return payload
        return {
            "code": "0",
            "msg": "",
            "data": [{"instId": inst_id, "last": payload.get("price"), "lastPx": payload.get("price"), "symbol": symbol}],
        }

    def get_balance(self, ccy: Optional[str] = None) -> Dict[str, Any]:
        self.require_auth()
        payload = self._signed_request("GET", "/api/v3/account", params={"recvWindow": 5000})
        if payload.get("code") and payload.get("code") != "0":
            return payload

        requested = {item.strip().upper() for item in str(ccy or "").split(",") if item.strip()}
        balances = payload.get("balances") or []
        details = []
        total_eq = 0.0
        for item in balances:
            asset = str(item.get("asset") or "").upper()
            if requested and asset not in requested:
                continue
            free = self._to_float(item.get("free")) or 0.0
            locked = self._to_float(item.get("locked")) or 0.0
            eq = free + locked
            total_eq += eq if asset == "USDT" else 0.0
            details.append(
                {
                    "ccy": asset,
                    "availBal": free,
                    "cashBal": free,
                    "eq": eq,
                    "frozenBal": locked,
                    "eqUsd": eq if asset == "USDT" else 0.0,
                }
            )
        return {
            "code": "0",
            "msg": "",
            "data": [{"totalEq": total_eq, "adjEq": total_eq, "details": details}],
        }

    def get_order(
        self,
        *,
        inst_id: str,
        ord_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.require_auth()
        symbol = self._to_symbol(inst_id)
        params: Dict[str, Any] = {"symbol": symbol, "recvWindow": 5000}
        if ord_id:
            params["orderId"] = ord_id
        if cl_ord_id:
            params["origClientOrderId"] = cl_ord_id
        payload = self._signed_request("GET", "/api/v3/order", params=params)
        if payload.get("code") and payload.get("code") != "0":
            return payload
        data = self._map_order(inst_id, payload)
        return {"code": "0", "msg": "", "data": [data]}

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
    ) -> Dict[str, Any]:
        _ = td_mode
        self.require_auth()
        symbol = self._to_symbol(inst_id)
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": str(side or "BUY").upper(),
            "type": str(ord_type or "MARKET").upper(),
            "newOrderRespType": "FULL",
            "recvWindow": 5000,
        }
        if cl_ord_id:
            params["newClientOrderId"] = cl_ord_id
        is_buy = params["side"] == "BUY"
        if is_buy and str(ord_type or "market").lower() == "market" and (tgt_ccy or "quote_ccy") == "quote_ccy":
            params["quoteOrderQty"] = str(sz)
        else:
            params["quantity"] = str(sz)
        payload = self._signed_request("POST", "/api/v3/order", params=params)
        if payload.get("code") and payload.get("code") != "0":
            return payload
        data = self._map_order(inst_id, payload)
        data.setdefault("sCode", "0")
        data.setdefault("sMsg", "")
        return {"code": "0", "msg": "", "data": [data]}

    def _map_order(self, inst_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        executed_qty = self._to_float(payload.get("executedQty"))
        cummulative_quote_qty = self._to_float(payload.get("cummulativeQuoteQty"))
        avg_px = None
        if executed_qty and executed_qty > 0 and cummulative_quote_qty is not None:
            avg_px = cummulative_quote_qty / executed_qty
        return {
            "ordId": str(payload.get("orderId") or ""),
            "clOrdId": payload.get("clientOrderId") or payload.get("origClientOrderId"),
            "state": self._map_order_status(payload.get("status")),
            "side": str(payload.get("side") or "").lower(),
            "ordType": str(payload.get("type") or "").lower(),
            "sz": payload.get("origQty") or payload.get("quoteOrderQty"),
            "accFillSz": executed_qty,
            "fillSz": executed_qty,
            "avgPx": avg_px,
            "fillPx": avg_px,
            "fee": None,
            "instId": inst_id,
            "raw": payload,
        }

    @staticmethod
    def _map_order_status(status: Any) -> str:
        raw = str(status or "").upper()
        mapping = {
            "NEW": "live",
            "PARTIALLY_FILLED": "partially_filled",
            "FILLED": "filled",
            "CANCELED": "canceled",
            "PENDING_CANCEL": "canceling",
            "REJECTED": "rejected",
            "EXPIRED": "canceled",
        }
        return mapping.get(raw, raw.lower() or "unknown")

    def _signed_request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = dict(params or {})
        params.setdefault("timestamp", int(time.time() * 1000))
        query = urlencode([(key, value) for key, value in params.items() if value is not None])
        signature = hmac.new(self.auth.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        full_query = f"{query}&signature={signature}" if query else f"signature={signature}"
        headers = {"X-MBX-APIKEY": self.auth.api_key}
        return self._request(method, path, params=None, data=None, query=full_query, headers=headers)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        query: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        try:
            response = self.session.request(method.upper(), url, params=params, data=data, timeout=self.timeout, headers=headers)
            payload = response.json()
        except Exception as exc:
            return {"code": "NETWORK_ERROR", "msg": str(exc)}
        if response.status_code != 200:
            code = payload.get("code") if isinstance(payload, dict) else response.status_code
            msg = payload.get("msg") if isinstance(payload, dict) else str(payload)
            return {"code": str(code), "msg": msg or f"HTTP {response.status_code}"}
        return payload if isinstance(payload, dict) else {"code": "0", "data": payload}

    @staticmethod
    def _to_symbol(inst_id: str) -> str:
        raw = str(inst_id or "").strip().upper().replace("/", "-")
        if raw.endswith("-SWAP"):
            raw = raw[:-5]
        return raw.replace("-", "")

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except Exception:
            return None
