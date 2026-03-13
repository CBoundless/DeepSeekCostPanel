#!/usr/bin/env python3
"""Auto trader (OKX demo trading) based on analyzer BUY/SELL/HOLD output.

This is a minimal, safety-first implementation:
- Spot trading only (tdMode=cash) by default.
- Market orders.
- Uses confidence threshold to reduce churn.
- Maintains local position state + queries balances when needed.
- Supports a default mainstream-symbol pool and ranks BUY signals by confidence.

Env vars:
- OKX_SYMBOLS: comma/space separated instIds (e.g. "BTC-USDT,ETH-USDT")
  - if empty, defaults to 10 mainstream pairs.
- OKX_TD_MODE: default "cash"
- OKX_BAR: default "1H" (OKX bar)
- OKX_LIMIT: default 200
- OKX_TRADE_QUOTE: default 15 (USDT) - 作为 AI 未给金额时的基础回退金额
- OKX_SPOT_TGT_CCY: default "quote_ccy" (for market buy sizing)
- OKX_CONF_THRESHOLD: default 65
- OKX_LOOP_SECONDS: default 60
- OKX_ORDER_CHECK_RETRIES: default 5 (下单后最多轮询几次订单状态)
- OKX_ORDER_CHECK_INTERVAL_MS: default 1000 (每次轮询间隔毫秒)
- OKX_MAX_TOTAL_EXPOSURE_RATIO: default 0.70 (组合总仓位上限)
- OKX_MAX_SINGLE_ASSET_WEIGHT: default 0.35 (单币仓位上限)
- OKX_MAX_ORDER_CASH_RATIO: default 0.20 (单次下单最多占可用现金比例)
- OKX_MIN_CASH_RESERVE_RATIO: default 0.10 (最少保留现金比例)
- OKX_SYNC_POSITIONS_ON_START: default 1 (启动时从交易所同步真实持仓)

Dynamic position (持仓管理):
- OKX_STOP_LOSS_PCT: default 0.02 (2%)
- OKX_TRAILING_STOP_PCT: default 0.01 (1%)
- OKX_TRAILING_ACTIVATE_PCT: default 0.005 (0.5%)
- OKX_ESTIMATED_ROUND_TRIP_COST_PCT: default 0.0 (预估双边手续费+滑点成本，用于净收益判断)
- OKX_ENTRY_COST_BUFFER_PCT: default 0.0 (买入前额外预留的单边成本缓冲)
- OKX_MIN_NET_PROFIT_PCT: default 0.0 (开仓所需的最小预估净收益)
- OKX_EXIT_ON_AI_SELL: default 1 (AI 给 SELL 且达到阈值就平仓)
- OKX_DYNAMIC_POSITION_ENABLED: default 1 (启用非杠杆动态仓位)
- OKX_MARKET_QUALITY_THRESHOLD: default 0.58 (低于该市场质量不新开仓)
- OKX_DYNAMIC_MIN_FACTOR: default 0.7 (较弱可做信号对应最小仓位系数)
- OKX_DYNAMIC_MAX_FACTOR: default 1.5 (高质量信号对应最大仓位系数)
- OKX_DECISION_HISTORY_LIMIT: default 300 (内存中保留的最近决策记录数)

Important:
- This is demo trading only by default (x-simulated-trading: 1).
- Do NOT grant withdrawal permission to your API key.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from okx_rest_client import OKXClient, okx_extract_first_data


DEFAULT_MAINSTREAM_INST_IDS = [
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "DOGE-USDT",
    "ADA-USDT",
    "AVAX-USDT",
    "LINK-USDT",
    "LTC-USDT",
    "BCH-USDT",
]

AUTOTRADE_RELEASE_TAG = "risk-cost-controls-20260313"


LogFn = Callable[[str], None]


@dataclass
class PositionState:
    holding: bool = False
    entry_price: Optional[float] = None
    peak_price: Optional[float] = None
    last_price: Optional[float] = None
    base_size: float = 0.0
    quote_size: float = 0.0
    entry_confidence: int = 0
    entry_market_quality: float = 0.0
    last_market_quality: float = 0.0


@dataclass
class DecisionRecord:
    ts: float
    inst_id: str
    action: str
    reason: str
    confidence: int = 0
    signal_quality: float = 0.0
    market_quality: float = 0.0
    position_factor: float = 0.0
    planned_quote: float = 0.0


@dataclass
class TradeConfig:
    inst_ids: List[str]
    bar: str
    limit: int = 200
    td_mode: str = "cash"  # spot
    trade_quote: float = 15.0
    spot_tgt_ccy: str = "quote_ccy"
    conf_threshold: int = 65
    loop_seconds: int = 60
    order_check_retries: int = 5
    order_check_interval_ms: int = 1000

    # dynamic position management / AI sizing fallback
    stop_loss_pct: float = 0.02
    trailing_stop_pct: float = 0.01
    trailing_activate_pct: float = 0.005
    estimated_round_trip_cost_pct: float = 0.0
    entry_cost_buffer_pct: float = 0.0
    min_net_profit_pct: float = 0.0
    exit_on_ai_sell: bool = True
    dynamic_position_enabled: bool = True
    market_quality_threshold: float = 0.58
    dynamic_min_factor: float = 0.7
    dynamic_max_factor: float = 1.5
    max_total_exposure_ratio: float = 0.70
    max_single_asset_weight: float = 0.35
    max_order_cash_ratio: float = 0.20
    min_cash_reserve_ratio: float = 0.10
    sync_positions_on_start: bool = True
    decision_history_limit: int = 300


def default_okx_inst_ids() -> List[str]:
    return list(DEFAULT_MAINSTREAM_INST_IDS)


def default_okx_symbols_env_value() -> str:
    return ",".join(default_okx_inst_ids())


def parse_inst_ids(s: str) -> List[str]:
    """解析 instId 列表。

    支持这些输入：
    - OKX instId：`BTC-USDT` / `ETH-USDT`
    - 常见简写：`BTCUSDT` -> `BTC-USDT`
    - 进一步简写：`btc`/`eth`/`sol` -> `BTC-USDT`/`ETH-USDT`/`SOL-USDT`

    说明：默认 quote 采用 `USDT`，如需其它 quote（例如 `BTC-USDC`），请显式输入带 `-` 的完整 instId。
    """

    raw = (s or "").strip()
    if not raw:
        return []

    out: List[str] = []
    for part in raw.replace("\n", " ").replace("\t", " ").replace(",", " ").split():
        p = part.strip()
        if not p:
            continue

        up = p.upper().replace("/", "-")

        # 1) normalize common symbol format like BTCUSDT/BTCUSDC -> BTC-USDT/BTC-USDC
        if "-" not in up and len(up) > 4:
            if up.endswith("USDT"):
                up = up[:-4] + "-USDT"
            elif up.endswith("USDC"):
                up = up[:-4] + "-USDC"
            elif up.endswith("USD"):
                up = up[:-3] + "-USD"

        # 2) allow base-only shorthand like BTC/ETH -> BTC-USDT
        if "-" not in up and re.fullmatch(r"[A-Z0-9]{2,20}", up):
            # Avoid accidentally converting already-structured strings; at this point it's a bare token.
            up = up + "-USDT"

        out.append(up)

    # de-dup preserve order
    dedup: List[str] = []
    seen = set()
    for x in out:
        if x not in seen:
            dedup.append(x)
            seen.add(x)
    return dedup


class AutoTrader:
    def __init__(self, *, analyzer: Any, okx: OKXClient, cfg: TradeConfig, log: Optional[LogFn] = None):
        self.analyzer = analyzer
        self.okx = okx
        self.cfg = cfg
        self.log = log or (lambda _msg: None)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # local position state
        self._pos: Dict[str, PositionState] = {inst: PositionState(holding=False) for inst in cfg.inst_ids}
        self._debug_conf50_printed: set[str] = set()
        self._decision_history: List[DecisionRecord] = []

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="OKXAutoTrader", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _holding_count(self) -> int:
        return sum(1 for pos in self._pos.values() if pos.holding)

    def _get_last_price(self, inst_id: str) -> Optional[float]:
        """获取最新价（best-effort）。"""
        try:
            payload = self.okx.get_ticker(inst_id)
            if not isinstance(payload, dict) or str(payload.get("code")) != "0":
                return None
            data = payload.get("data")
            if not isinstance(data, list) or not data or not isinstance(data[0], dict):
                return None
            d0 = data[0]
            # 常见字段：last / lastPx
            v = d0.get("last")
            if v is None:
                v = d0.get("lastPx")
            return float(v) if v is not None else None
        except Exception:
            return None

    @staticmethod
    def _calc_pnl_pct(entry: Optional[float], last: Optional[float]) -> Optional[float]:
        if entry is None or last is None or entry <= 0:
            return None
        return (float(last) - float(entry)) / float(entry)

    def _estimated_round_trip_cost_pct(self) -> float:
        return max(0.0, float(self.cfg.estimated_round_trip_cost_pct or 0.0))

    def _estimated_entry_cost_buffer_pct(self) -> float:
        return max(0.0, float(self.cfg.entry_cost_buffer_pct or 0.0))

    def _calc_net_pnl_pct(self, entry: Optional[float], last: Optional[float]) -> Optional[float]:
        gross = self._calc_pnl_pct(entry, last)
        if gross is None:
            return None
        return float(gross) - self._estimated_round_trip_cost_pct()

    def _calc_target_net_profit_pct(self, entry: Optional[float], target: Optional[float]) -> Optional[float]:
        gross = self._calc_pnl_pct(entry, target)
        if gross is None:
            return None
        return float(gross) - self._estimated_round_trip_cost_pct()

    @staticmethod
    def _calc_drawdown_pct(peak: Optional[float], last: Optional[float]) -> Optional[float]:
        if peak is None or last is None or peak <= 0:
            return None
        return (float(last) - float(peak)) / float(peak)

    def _log_position(self, inst: str, pos: PositionState, *, note: str = ""):
        gross_pnl = self._calc_pnl_pct(pos.entry_price, pos.last_price)
        net_pnl = self._calc_net_pnl_pct(pos.entry_price, pos.last_price)
        dd = self._calc_drawdown_pct(pos.peak_price, pos.last_price)
        parts = [f"[{inst}] holding={pos.holding}"]
        if pos.entry_price is not None:
            parts.append(f"entry={pos.entry_price:.4f}")
        if pos.last_price is not None:
            parts.append(f"last={pos.last_price:.4f}")
        if pos.base_size > 0:
            parts.append(f"base_sz={pos.base_size:.8f}")
        if pos.quote_size > 0:
            parts.append(f"quote={pos.quote_size:.4f}")
        if pos.last_market_quality > 0:
            parts.append(f"market_q={pos.last_market_quality:.2f}")
        if gross_pnl is not None:
            parts.append(f"gross_pnl={gross_pnl * 100:.2f}%")
        if net_pnl is not None:
            parts.append(f"net_pnl={net_pnl * 100:.2f}%")
        if dd is not None and pos.peak_price is not None:
            parts.append(f"dd_from_peak={dd * 100:.2f}%")
        if note:
            parts.append(f"note={note}")
        self.log(" ".join(parts))

    def _reset_position(self, pos: PositionState):
        pos.holding = False
        pos.entry_price = None
        pos.peak_price = None
        pos.base_size = 0.0
        pos.quote_size = 0.0
        pos.entry_confidence = 0
        pos.entry_market_quality = 0.0
        pos.last_market_quality = 0.0

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
        return max(lower, min(upper, float(value)))

    def _append_decision(
        self,
        *,
        inst_id: str,
        action: str,
        reason: str,
        confidence: int = 0,
        signal_quality: float = 0.0,
        market_quality: float = 0.0,
        position_factor: float = 0.0,
        planned_quote: float = 0.0,
    ) -> None:
        self._decision_history.append(
            DecisionRecord(
                ts=time.time(),
                inst_id=inst_id,
                action=str(action).upper(),
                reason=reason,
                confidence=int(confidence),
                signal_quality=float(signal_quality),
                market_quality=float(market_quality),
                position_factor=float(position_factor),
                planned_quote=float(planned_quote),
            )
        )
        limit = max(10, int(self.cfg.decision_history_limit))
        if len(self._decision_history) > limit:
            self._decision_history = self._decision_history[-limit:]

    def get_decision_history(self, limit: int = 50) -> List[DecisionRecord]:
        lim = max(1, int(limit))
        return list(self._decision_history[-lim:])

    def _calc_trend_strength(self, indicators: Dict[str, Any]) -> float:
        ema_9 = self._to_float(indicators.get("ema_9")) or 0.0
        ema_20 = self._to_float(indicators.get("ema_20")) or 0.0
        ema_50 = self._to_float(indicators.get("ema_50")) or 0.0
        last_close = self._to_float(indicators.get("last_close")) or 0.0
        macd_hist = self._to_float(indicators.get("macd_hist")) or 0.0
        close_change = self._to_float(indicators.get("close_change_pct_20")) or 0.0
        ema_spread = self._to_float(indicators.get("ema_spread_pct")) or 0.0

        score = 0.5
        if ema_9 > ema_20 > ema_50:
            score += 0.2
        elif ema_9 < ema_20 < ema_50:
            score -= 0.2

        if last_close > 0 and ema_20 > 0 and last_close >= ema_20:
            score += 0.1
        else:
            score -= 0.05

        if macd_hist > 0:
            score += 0.1
        elif macd_hist < 0:
            score -= 0.05

        score += max(-0.15, min(0.15, close_change * 1.5))
        score += max(-0.1, min(0.1, ema_spread * 8.0))
        return self._clamp(score)

    def _calc_market_quality(self, result: Dict[str, Any], confidence: int) -> Tuple[float, float, float, float]:
        indicators = (result or {}).get("indicators") or {}
        signal_quality = self._clamp(self._to_float((result or {}).get("signal_quality")) or 0.5)
        trend_strength = self._calc_trend_strength(indicators)
        volatility_20 = abs(self._to_float(indicators.get("volatility_20")) or 0.0)
        volatility_score = self._clamp(1.0 - min(volatility_20 / 0.05, 1.0))
        confidence_score = self._clamp((int(confidence) if confidence is not None else 0) / 100.0)
        market_quality = self._clamp(
            confidence_score * 0.40 + signal_quality * 0.25 + trend_strength * 0.25 + volatility_score * 0.10
        )
        return market_quality, signal_quality, trend_strength, volatility_score

    def _calc_position_factor(self, market_quality: float, confidence: int) -> float:
        if not bool(self.cfg.dynamic_position_enabled):
            return 1.0

        min_factor = max(0.1, float(self.cfg.dynamic_min_factor))
        max_factor = max(min_factor, float(self.cfg.dynamic_max_factor))

        if market_quality >= 0.88 and confidence >= int(self.cfg.conf_threshold) + 18:
            factor = 1.5
        elif market_quality >= 0.78:
            factor = 1.2
        elif market_quality >= 0.68:
            factor = 1.0
        else:
            factor = min_factor

        return max(min_factor, min(max_factor, float(factor)))

    @staticmethod
    def _format_order_summary(order: Optional[Dict[str, Any]]) -> str:
        if not order:
            return "order=unavailable"
        parts: List[str] = []
        for key in ("ordId", "clOrdId", "state", "side", "ordType"):
            value = order.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        for key in ("sz", "accFillSz", "fillSz", "avgPx", "fillPx", "fee"):
            value = order.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        return " ".join(parts) if parts else str(order)

    @staticmethod
    def _extract_submit_error(first: Optional[Dict[str, Any]]) -> str:
        if not isinstance(first, dict):
            return ""
        s_code = str(first.get("sCode") or "0")
        if s_code and s_code != "0":
            return f"OKX order rejected sCode={s_code} sMsg={first.get('sMsg')}"
        return ""

    @staticmethod
    def _build_cl_ord_id(action: str, inst_id: str) -> str:
        safe_action = re.sub(r"[^A-Za-z0-9]", "", str(action).upper())[:1] or "X"
        safe_inst = re.sub(r"[^A-Za-z0-9]", "", str(inst_id).upper())[:12] or "PAIR"
        nonce = str(time.time_ns())[-13:]
        return f"DS{safe_action}{nonce}{safe_inst}"[:32]

    def _get_balance_snapshot(self, inst_id: str) -> Optional[Dict[str, Dict[str, Optional[float]]]]:
        if "-" in inst_id:
            base_ccy, quote_ccy = inst_id.split("-", 1)
        else:
            base_ccy, quote_ccy = inst_id, "USDT"
        ccy = f"{base_ccy.upper()},{quote_ccy.upper()}"
        payload = self.okx.get_balance(ccy=ccy)
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            self.log(f"[{inst_id}] ⚠️ 余额查询失败 payload={payload}")
            return None

        details: List[Dict[str, Any]] = []
        try:
            data = payload.get("data") or []
            if data and isinstance(data, list):
                details = (data[0] or {}).get("details") or []
        except Exception:
            details = []

        snapshot: Dict[str, Dict[str, Optional[float]]] = {}
        for item_ccy in (base_ccy.upper(), quote_ccy.upper()):
            hit = next((d for d in details if str(d.get("ccy")).upper() == item_ccy), None) or {}
            snapshot[item_ccy] = {
                "availBal": self._to_float(hit.get("availBal")),
                "cashBal": self._to_float(hit.get("cashBal")),
                "eq": self._to_float(hit.get("eq")),
                "frozenBal": self._to_float(hit.get("frozenBal")),
            }
        return snapshot

    @staticmethod
    def _format_balance_snapshot(snapshot: Optional[Dict[str, Dict[str, Optional[float]]]]) -> str:
        if not snapshot:
            return "snapshot=unavailable"
        parts: List[str] = []
        for ccy, values in snapshot.items():
            parts.append(
                f"{ccy}(avail={values.get('availBal') if values.get('availBal') is not None else 'n/a'}, "
                f"cash={values.get('cashBal') if values.get('cashBal') is not None else 'n/a'}, "
                f"eq={values.get('eq') if values.get('eq') is not None else 'n/a'})"
            )
        return "; ".join(parts)

    def _get_account_balance_payload(self) -> Optional[Dict[str, Any]]:
        payload = self.okx.get_balance()
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            self.log(f"⚠️ 账户余额查询失败 payload={payload}")
            return None
        return payload

    def _get_account_balance_details(self, payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        data_payload = payload if isinstance(payload, dict) else self._get_account_balance_payload()
        if not isinstance(data_payload, dict):
            return []
        try:
            data = data_payload.get("data") or []
            if data and isinstance(data, list):
                details = (data[0] or {}).get("details") or []
                return [x for x in details if isinstance(x, dict)]
        except Exception:
            pass
        return []

    def _build_portfolio_snapshot(self) -> Optional[Dict[str, Any]]:
        payload = self._get_account_balance_payload()
        details = self._get_account_balance_details(payload)
        if not details:
            return None

        detail_map: Dict[str, Dict[str, Any]] = {}
        for item in details:
            ccy = str(item.get("ccy") or "").upper()
            if ccy:
                detail_map[ccy] = item

        usdt_detail = detail_map.get("USDT") or {}
        available_usdt = max(
            0.0,
            float(
                self._to_float(usdt_detail.get("availBal"))
                or self._to_float(usdt_detail.get("cashBal"))
                or self._to_float(usdt_detail.get("eq"))
                or 0.0
            ),
        )

        total_equity_usdt = 0.0
        data0 = ((payload or {}).get("data") or [None])[0] or {}
        for key in ("totalEq", "adjEq", "isoEq"):
            try:
                total_equity_usdt = max(total_equity_usdt, float(self._to_float(data0.get(key)) or 0.0))
            except Exception:
                continue

        if total_equity_usdt <= 0:
            total_equity_usdt = sum(max(0.0, float(self._to_float(item.get("eqUsd")) or 0.0)) for item in details)

        holdings: List[Dict[str, Any]] = []
        holdings_by_inst: Dict[str, Dict[str, Any]] = {}
        tracked_value_usdt = 0.0

        for inst_id in self.cfg.inst_ids:
            base_ccy = inst_id.split("-", 1)[0].upper() if "-" in inst_id else inst_id.upper()
            detail = detail_map.get(base_ccy) or {}
            base_size = max(
                0.0,
                float(
                    self._to_float(detail.get("availBal"))
                    or self._to_float(detail.get("cashBal"))
                    or self._to_float(detail.get("eq"))
                    or 0.0
                ),
            )
            if base_size <= 0:
                continue

            pos = self._pos.get(inst_id) or PositionState()
            price = float(pos.last_price or 0.0)
            if price <= 0:
                price = float(self._get_last_price(inst_id) or 0.0)
            market_value_usdt = float(self._to_float(detail.get("eqUsd")) or 0.0)
            if market_value_usdt <= 0 and price > 0:
                market_value_usdt = float(base_size) * float(price)
            tracked_value_usdt += max(0.0, market_value_usdt)
            holding = {
                "inst_id": inst_id,
                "base_ccy": base_ccy,
                "base_size": float(base_size),
                "price": float(price),
                "market_value_usdt": max(0.0, float(market_value_usdt)),
                "weight": 0.0,
            }
            holdings.append(holding)
            holdings_by_inst[inst_id] = holding

        if total_equity_usdt <= 0:
            total_equity_usdt = max(available_usdt + tracked_value_usdt, available_usdt)

        invested_usdt = max(tracked_value_usdt, max(0.0, total_equity_usdt - available_usdt))
        exposure_ratio = (invested_usdt / total_equity_usdt) if total_equity_usdt > 0 else 0.0
        cash_reserve_ratio = (available_usdt / total_equity_usdt) if total_equity_usdt > 0 else 0.0

        if total_equity_usdt > 0:
            for holding in holdings:
                holding["weight"] = max(0.0, float(holding.get("market_value_usdt") or 0.0) / total_equity_usdt)

        holdings.sort(key=lambda item: float(item.get("market_value_usdt") or 0.0), reverse=True)
        return {
            "total_equity_usdt": float(total_equity_usdt),
            "available_usdt": float(available_usdt),
            "invested_usdt": float(invested_usdt),
            "exposure_ratio": float(exposure_ratio),
            "cash_reserve_ratio": float(cash_reserve_ratio),
            "max_total_exposure_ratio": float(self.cfg.max_total_exposure_ratio),
            "max_single_asset_weight": float(self.cfg.max_single_asset_weight),
            "max_order_cash_ratio": float(self.cfg.max_order_cash_ratio),
            "min_cash_reserve_ratio": float(self.cfg.min_cash_reserve_ratio),
            "holdings": holdings,
            "holdings_by_inst": holdings_by_inst,
        }

    def _sync_positions_from_portfolio(self, snapshot: Optional[Dict[str, Any]]) -> int:
        if not isinstance(snapshot, dict):
            return 0
        holdings_by_inst = snapshot.get("holdings_by_inst") or {}
        changed = 0
        for inst in self.cfg.inst_ids:
            pos = self._pos.get(inst) or PositionState()
            self._pos[inst] = pos
            holding = holdings_by_inst.get(inst) if isinstance(holdings_by_inst, dict) else None
            if isinstance(holding, dict) and float(holding.get("base_size") or 0.0) > 0:
                was_holding = bool(pos.holding)
                pos.holding = True
                pos.base_size = max(0.0, float(holding.get("base_size") or 0.0))
                pos.quote_size = max(0.0, float(holding.get("market_value_usdt") or 0.0))
                px = self._to_float(holding.get("price"))
                if px is not None and px > 0:
                    pos.last_price = float(px)
                    if pos.entry_price is None:
                        pos.entry_price = float(px)
                    if pos.peak_price is None:
                        pos.peak_price = float(px)
                    else:
                        pos.peak_price = max(float(pos.peak_price), float(px))
                if not was_holding:
                    changed += 1
            else:
                if pos.holding or pos.base_size > 0 or pos.quote_size > 0:
                    self._reset_position(pos)
                    changed += 1
        return changed

    def _resolve_requested_buy_quote(self, result: Dict[str, Any], market_quality: float, confidence: int) -> Tuple[float, float, str]:
        fallback_factor = self._calc_position_factor(market_quality, confidence)
        fallback_quote = max(0.0, float(self.cfg.trade_quote) * fallback_factor)
        raw_buy_amount = (result or {}).get("buy_amount_usdt")
        if raw_buy_amount is None:
            return fallback_quote, fallback_factor, "fallback_dynamic_quote"
        buy_amount = self._to_float(raw_buy_amount)
        if buy_amount is None:
            return 0.0, fallback_factor, "invalid_ai_buy_amount"
        buy_amount = max(0.0, float(buy_amount))
        ai_factor = (buy_amount / float(self.cfg.trade_quote)) if float(self.cfg.trade_quote) > 0 else fallback_factor
        return buy_amount, ai_factor, "ai_buy_amount"

    def _assess_buy_profitability(
        self,
        result: Dict[str, Any],
        current_price: Optional[float],
    ) -> Tuple[bool, str, Dict[str, float]]:
        min_net_profit_pct = max(0.0, float(self.cfg.min_net_profit_pct or 0.0))
        target_price = self._to_float((result or {}).get("target_price"))
        meta: Dict[str, float] = {
            "min_net_profit_pct": min_net_profit_pct,
            "estimated_round_trip_cost_pct": self._estimated_round_trip_cost_pct(),
            "current_price": float(current_price or 0.0),
            "target_price": float(target_price or 0.0),
        }
        if min_net_profit_pct <= 0:
            return True, "min_net_profit_disabled", meta
        if current_price is None or current_price <= 0:
            return False, "missing_last_price", meta
        if target_price is None or target_price <= 0:
            return False, "missing_target_price", meta

        gross_profit_pct = self._calc_pnl_pct(current_price, target_price)
        net_profit_pct = self._calc_target_net_profit_pct(current_price, target_price)
        meta["gross_profit_pct"] = float(gross_profit_pct or 0.0)
        meta["net_profit_pct"] = float(net_profit_pct or 0.0)

        if gross_profit_pct is None:
            return False, "invalid_target_price", meta
        if float(gross_profit_pct) <= 0:
            return False, "target_price_below_market", meta
        if net_profit_pct is None:
            return False, "invalid_target_net_profit", meta
        if float(net_profit_pct) < min_net_profit_pct:
            return False, "net_profit_below_threshold", meta
        return True, "ok", meta

    def _resolve_sell_request(self, result: Dict[str, Any]) -> Tuple[Optional[float], float, bool]:
        raw_sell_ratio = (result or {}).get("sell_ratio")
        raw_sell_amount = (result or {}).get("sell_amount_usdt")
        has_explicit_sell = raw_sell_ratio is not None or raw_sell_amount is not None
        sell_ratio = None
        if raw_sell_ratio is not None:
            ratio = self._to_float(raw_sell_ratio)
            if ratio is not None:
                sell_ratio = self._clamp(float(ratio), 0.0, 1.0)
        sell_amount_usdt = 0.0
        if raw_sell_amount is not None:
            sell_amount_usdt = max(0.0, float(self._to_float(raw_sell_amount) or 0.0))
        return sell_ratio, sell_amount_usdt, has_explicit_sell

    def _calc_buy_quote_after_risk(
        self,
        inst_id: str,
        requested_quote: float,
        snapshot: Optional[Dict[str, Any]],
    ) -> Tuple[float, str, Dict[str, float]]:
        req = max(0.0, float(requested_quote))
        entry_cost_buffer_pct = self._estimated_entry_cost_buffer_pct()
        buffered_req = max(0.0, req * max(0.0, 1.0 - entry_cost_buffer_pct))
        base_meta = {
            "requested_quote": req,
            "buffered_requested_quote": buffered_req,
            "entry_cost_buffer_pct": entry_cost_buffer_pct,
        }
        if req <= 0:
            return 0.0, "non_positive_request", base_meta
        if buffered_req <= 0:
            return 0.0, "entry_cost_buffer_exhausted", base_meta
        if not isinstance(snapshot, dict):
            meta = dict(base_meta)
            meta["final_quote"] = buffered_req
            return buffered_req, "no_portfolio_snapshot", meta

        available_usdt = max(0.0, float(snapshot.get("available_usdt") or 0.0))
        total_equity_usdt = max(0.0, float(snapshot.get("total_equity_usdt") or 0.0))
        invested_usdt = max(0.0, float(snapshot.get("invested_usdt") or 0.0))
        holdings_by_inst = snapshot.get("holdings_by_inst") or {}
        current_holding = holdings_by_inst.get(inst_id) if isinstance(holdings_by_inst, dict) else None
        current_inst_value = max(0.0, float((current_holding or {}).get("market_value_usdt") or 0.0))

        caps: Dict[str, float] = {
            "available_balance": max(0.0, available_usdt * 0.98),
        }
        caps["max_order_cash_ratio"] = max(0.0, available_usdt * float(self.cfg.max_order_cash_ratio))
        if total_equity_usdt > 0:
            caps["cash_reserve"] = max(0.0, available_usdt - total_equity_usdt * float(self.cfg.min_cash_reserve_ratio))
            caps["portfolio_room"] = max(
                0.0,
                total_equity_usdt * float(self.cfg.max_total_exposure_ratio) - invested_usdt,
            )
            caps["single_asset_room"] = max(
                0.0,
                total_equity_usdt * float(self.cfg.max_single_asset_weight) - current_inst_value,
            )

        final_quote = min([buffered_req] + list(caps.values())) if caps else buffered_req
        final_quote = max(0.0, float(final_quote))
        meta = {
            **base_meta,
            "final_quote": final_quote,
            "available_usdt": available_usdt,
            "total_equity_usdt": total_equity_usdt,
            "invested_usdt": invested_usdt,
            "current_inst_value": current_inst_value,
        }
        meta.update({k: float(v) for k, v in caps.items()})
        reason = "ok" if final_quote > 0 else "risk_blocked"
        return final_quote, reason, meta

    def _verify_order_state(
        self,
        inst_id: str,
        *,
        ord_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        if not ord_id and not cl_ord_id:
            return None, "缺少 ordId/clOrdId"

        attempts = max(1, int(self.cfg.order_check_retries))
        interval_seconds = max(0, int(self.cfg.order_check_interval_ms)) / 1000.0
        last_order: Optional[Dict[str, Any]] = None
        last_err = ""

        for attempt in range(1, attempts + 1):
            payload = self.okx.get_order(inst_id=inst_id, ord_id=ord_id, cl_ord_id=cl_ord_id)
            order, err = okx_extract_first_data(payload)
            if err:
                last_err = err
            elif order:
                last_order = order
                state = str(order.get("state") or "").lower()
                if state in ("filled", "canceled", "cancelled", "mmp_canceled"):
                    return order, ""
            if attempt < attempts and interval_seconds > 0:
                time.sleep(interval_seconds)

        return last_order, last_err

    def _should_force_exit_by_risk(self, inst: str, pos: PositionState) -> bool:
        """风控触发：止损 or 移动止盈。

        - 止损：net_pnl <= -stop_loss_pct
        - 移动止盈：当 net_pnl >= trailing_activate_pct 后，如果从峰值回撤 <= -trailing_stop_pct 则平仓
        """
        if not pos.holding:
            return False

        net_pnl = self._calc_net_pnl_pct(pos.entry_price, pos.last_price)
        if net_pnl is None:
            return False

        # hard stop loss
        if float(net_pnl) <= -abs(float(self.cfg.stop_loss_pct)):
            self._log_position(inst, pos, note=f"触发止损(净收益 {self.cfg.stop_loss_pct * 100:.2f}%)")
            return True

        # trailing stop only after some profit
        if float(net_pnl) >= float(self.cfg.trailing_activate_pct):
            dd = self._calc_drawdown_pct(pos.peak_price, pos.last_price)
            if dd is not None and float(dd) <= -abs(float(self.cfg.trailing_stop_pct)):
                self._log_position(inst, pos, note=f"触发移动止盈(净收益达标后回撤 {self.cfg.trailing_stop_pct * 100:.2f}%)")
                return True

        return False

    # ---------- internal ----------
    def _run_loop(self):
        mode_label = "模拟盘" if getattr(self.okx, "simulated_trading", False) else "实盘"
        self.log(f"🟡 自动交易启动（OKX {mode_label}） AUTOTRADE_RELEASE_TAG={AUTOTRADE_RELEASE_TAG}")
        self.log(
            f"instIds={self.cfg.inst_ids} bar={self.cfg.bar} loop={self.cfg.loop_seconds}s conf>={self.cfg.conf_threshold} "
            f"base_trade_quote={self.cfg.trade_quote} dynamic_position_enabled={self.cfg.dynamic_position_enabled} "
            f"market_quality_threshold={self.cfg.market_quality_threshold} dynamic_factor=[{self.cfg.dynamic_min_factor},{self.cfg.dynamic_max_factor}] "
            f"risk_limits(total_exposure={self.cfg.max_total_exposure_ratio}, single_asset={self.cfg.max_single_asset_weight}, "
            f"order_cash={self.cfg.max_order_cash_ratio}, cash_reserve={self.cfg.min_cash_reserve_ratio}) "
            f"cost_controls(round_trip_cost={self.cfg.estimated_round_trip_cost_pct}, entry_buffer={self.cfg.entry_cost_buffer_pct}, "
            f"min_net_profit={self.cfg.min_net_profit_pct}) order_check_retries={self.cfg.order_check_retries} "
            f"order_check_interval_ms={self.cfg.order_check_interval_ms} stop_loss={self.cfg.stop_loss_pct} "
            f"trailing_stop={self.cfg.trailing_stop_pct} trailing_activate={self.cfg.trailing_activate_pct} "
            f"exit_on_ai_sell={self.cfg.exit_on_ai_sell}"
        )

        # Ensure OKX auth exists
        try:
            self.okx.require_auth()
        except Exception as e:
            self.log(f"❌ 无法启动：{e}")
            return

        if bool(self.cfg.sync_positions_on_start):
            snapshot = self._build_portfolio_snapshot()
            changed = self._sync_positions_from_portfolio(snapshot)
            if isinstance(snapshot, dict):
                self.log(
                    f"🔁 启动同步持仓完成 changed={changed} total_equity={float(snapshot.get('total_equity_usdt') or 0.0):.4f} "
                    f"available_usdt={float(snapshot.get('available_usdt') or 0.0):.4f} holdings={len(snapshot.get('holdings') or [])}"
                )
            else:
                self.log("⚠️ 启动时未获取到组合快照，将在后续轮次继续尝试同步")

        while not self._stop.is_set():
            try:
                self._tick_once()
            except Exception as e:
                self.log(f"❌ tick 异常：{type(e).__name__}: {e}")

            # sleep with early stop
            for _ in range(max(1, int(self.cfg.loop_seconds))):
                if self._stop.is_set():
                    break
                time.sleep(1)

        self.log("🟠 自动交易已停止")

    def _tick_once(self):
        def _fmt_meta(rr: dict) -> str:
            src = rr.get("source")
            tks = rr.get("tokens")
            cst = rr.get("cost")
            parts = []
            if src:
                parts.append(f"source={src}")
            if tks is not None:
                parts.append(f"tokens={tks}")
            if cst is not None:
                parts.append(f"cost=${cst}")
            return (" " + " ".join(parts)) if parts else ""

        # 先统一刷新价格，保证组合快照、止损和日志使用同一轮价格
        for inst in self.cfg.inst_ids:
            pos = self._pos.get(inst) or PositionState()
            self._pos[inst] = pos
            px = self._get_last_price(inst)
            if px is not None:
                pos.last_price = px
                if pos.holding:
                    if pos.peak_price is None:
                        pos.peak_price = px
                    else:
                        pos.peak_price = max(float(pos.peak_price), float(px))

        portfolio_snapshot = self._build_portfolio_snapshot()
        if isinstance(portfolio_snapshot, dict):
            self._sync_positions_from_portfolio(portfolio_snapshot)
            self.log(
                f"💼 组合快照 total_equity={float(portfolio_snapshot.get('total_equity_usdt') or 0.0):.4f} "
                f"available_usdt={float(portfolio_snapshot.get('available_usdt') or 0.0):.4f} "
                f"invested_usdt={float(portfolio_snapshot.get('invested_usdt') or 0.0):.4f} "
                f"exposure={float(portfolio_snapshot.get('exposure_ratio') or 0.0):.2%} holdings={len(portfolio_snapshot.get('holdings') or [])}"
            )
        else:
            self.log("⚠️ 本轮未获取到组合快照，AI 将只基于行情做判断，程序仍会在下单前做余额限制")

        # Use batch analysis to reduce DeepSeek calls
        batch = self.analyzer.analyze_markets_from_okx(
            inst_ids=self.cfg.inst_ids,
            okx_client=self.okx,
            bar=self.cfg.bar,
            limit=int(self.cfg.limit),
            force_analysis=False,
            portfolio_context=portfolio_snapshot,
        )
        results = (batch or {}).get("results") or {}

        # 批量概要：方便判断“这一轮到底有没有调 DeepSeek”
        try:
            bsrc = (batch or {}).get("source")
            binfo = (batch or {}).get("batch") or {}
            budget = (batch or {}).get("budget") or {}
            if bsrc in ("batch_api", "batch_api_chunked", "partial_api_error", "api_error"):
                tks = binfo.get("tokens")
                cst = binfo.get("cost")
                rem = budget.get("remaining")
                parsed_count = binfo.get("parsed_count")
                parse_miss_count = binfo.get("parse_miss_count")
                fallback_used = binfo.get("single_fallback_used")
                batch_count = binfo.get("batch_count")
                batch_sizes = binfo.get("batch_sizes")
                failed_batch_count = binfo.get("failed_batch_count")
                self.log(
                    f"📊 本轮批量分析 source={bsrc} batches={batch_count} batch_sizes={batch_sizes} failed_batches={failed_batch_count} "
                    f"tokens={tks} cost=${cst} budget_remaining={rem} parsed={parsed_count} "
                    f"parse_miss={parse_miss_count} fallback_single={fallback_used}"
                )
        except Exception:
            pass

        buy_candidates: List[Tuple[str, float, int, float, float, float, dict, str]] = []
        frequency_limit_skips: List[str] = []

        for inst in self.cfg.inst_ids:
            pos = self._pos.get(inst) or PositionState()
            self._pos[inst] = pos

            r = results.get(inst) or {}
            rec = str(r.get("recommendation") or "HOLD").upper()
            conf = int(r.get("confidence") or 0)
            market_quality, signal_quality, trend_strength, volatility_score = self._calc_market_quality(r, conf)
            pos.last_market_quality = market_quality if (pos.holding or rec == "BUY") else pos.last_market_quality

            # 调试：如果 conf 总是 50，先把模型原文打一条（每个 inst 只打一次，避免刷屏）
            if conf == 50 and r.get("source") == "api" and inst not in self._debug_conf50_printed:
                self._debug_conf50_printed.add(inst)
                raw = r.get("raw_analysis")
                if raw:
                    try:
                        s = str(raw).replace("\n", " ")
                        s = (s[:500] + "…(truncated)") if len(s) > 500 else s
                        self.log(f"[{inst}] conf=50 debug raw_analysis={s}")
                    except Exception:
                        pass

            # 允许在持仓时，即使分析失败也做风控检查（例如止损/移动止盈）
            if r.get("status") != "success":
                reason = r.get("reason") or r.get("message") or ""
                if pos.holding and self._should_force_exit_by_risk(inst, pos):
                    ok, _close_info = self._close_long_spot(inst)
                    if ok:
                        portfolio_snapshot = self._build_portfolio_snapshot() or portfolio_snapshot
                        self._sync_positions_from_portfolio(portfolio_snapshot)
                        self._append_decision(
                            inst_id=inst,
                            action="SELL",
                            reason="risk_exit_after_analysis_skip",
                            confidence=conf,
                            signal_quality=signal_quality,
                            market_quality=market_quality,
                        )
                else:
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason=reason or "analysis_not_ready",
                        confidence=conf,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    if str(r.get("source") or "") == "frequency_limit":
                        frequency_limit_skips.append(inst)
                    else:
                        self.log(f"[{inst}] skip status={r.get('status')} {reason}{_fmt_meta(r)}")
                        raw = r.get("raw_analysis")
                        if raw:
                            try:
                                s = str(raw)
                                s = (s[:800] + "…(truncated)") if len(s) > 800 else s
                                self.log(f"[{inst}] raw_analysis={s}")
                            except Exception:
                                pass
                continue

            # 先做风控：止损/移动止盈（不依赖 AI）
            if pos.holding and self._should_force_exit_by_risk(inst, pos):
                ok, _close_info = self._close_long_spot(inst)
                if ok:
                    portfolio_snapshot = self._build_portfolio_snapshot() or portfolio_snapshot
                    self._sync_positions_from_portfolio(portfolio_snapshot)
                    self._append_decision(
                        inst_id=inst,
                        action="SELL",
                        reason="risk_exit",
                        confidence=conf,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                continue

            # AI 置信度不足：不加仓；持仓则继续持有
            if conf < int(self.cfg.conf_threshold):
                self._append_decision(
                    inst_id=inst,
                    action="HOLD",
                    reason=f"confidence_below_threshold:{conf}",
                    confidence=conf,
                    signal_quality=signal_quality,
                    market_quality=market_quality,
                )
                if pos.holding:
                    self._log_position(inst, pos, note=f"AI conf {conf} < {self.cfg.conf_threshold}，继续持有 market_q={market_quality:.2f}{_fmt_meta(r)}")
                else:
                    self.log(f"[{inst}] HOLD (conf {conf} < {self.cfg.conf_threshold} market_q={market_quality:.2f}){_fmt_meta(r)}")
                continue

            if rec == "SELL" and pos.holding and bool(self.cfg.exit_on_ai_sell):
                sell_ratio, sell_amount_usdt, has_explicit_sell = self._resolve_sell_request(r)
                if has_explicit_sell and sell_ratio is None and sell_amount_usdt <= 0:
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason="invalid_ai_sell_amount",
                        confidence=conf,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    self._log_position(inst, pos, note=f"SELL(conf={conf}) 但 AI 卖出数量无效{_fmt_meta(r)}")
                    continue

                sell_ratio_to_use = sell_ratio
                if sell_ratio_to_use is None and sell_amount_usdt <= 0:
                    sell_ratio_to_use = 1.0

                ok, _close_info = self._close_long_spot(
                    inst,
                    sell_ratio=sell_ratio_to_use,
                    sell_quote_usdt=sell_amount_usdt,
                )
                if ok:
                    portfolio_snapshot = self._build_portfolio_snapshot() or portfolio_snapshot
                    self._sync_positions_from_portfolio(portfolio_snapshot)
                    pos = self._pos.get(inst) or PositionState()
                    action = "SELL" if not pos.holding else "SELL_PARTIAL"
                    reason = "ai_sell_signal"
                    if sell_ratio_to_use is not None:
                        reason += f":ratio={sell_ratio_to_use:.4f}"
                    elif sell_amount_usdt > 0:
                        reason += f":quote={sell_amount_usdt:.4f}"
                    self._append_decision(
                        inst_id=inst,
                        action=action,
                        reason=reason,
                        confidence=conf,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    if pos.holding:
                        self._log_position(inst, pos, note=f"部分 SELL 后继续持有 market_q={market_quality:.2f}{_fmt_meta(r)}")
                else:
                    self._log_position(inst, pos, note=f"SELL(conf={conf}) 信号但平仓失败 market_q={market_quality:.2f}{_fmt_meta(r)}")
                continue

            if rec == "BUY":
                if market_quality < float(self.cfg.market_quality_threshold):
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason=f"market_quality_below_threshold:{market_quality:.2f}",
                        confidence=conf,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    self.log(
                        f"[{inst}] BUY(conf={conf}) 但 market_q={market_quality:.2f} < {self.cfg.market_quality_threshold:.2f} "
                        f"trend={trend_strength:.2f} vol_score={volatility_score:.2f}{_fmt_meta(r)}"
                    )
                    continue

                profitable, profit_reason, profit_meta = self._assess_buy_profitability(r, pos.last_price)
                if not profitable:
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason=profit_reason,
                        confidence=conf,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    self.log(
                        f"[{inst}] BUY(conf={conf}) 但预估净收益不足 reason={profit_reason} meta={profit_meta}{_fmt_meta(r)}"
                    )
                    continue

                requested_quote, position_factor, quote_source = self._resolve_requested_buy_quote(r, market_quality, conf)
                if requested_quote <= 0:
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason=quote_source,
                        confidence=conf,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    self.log(f"[{inst}] BUY(conf={conf}) 但买入金额无效 source={quote_source}{_fmt_meta(r)}")
                    continue

                buy_candidates.append((inst, market_quality, conf, requested_quote, position_factor, signal_quality, r, quote_source))
                self._append_decision(
                    inst_id=inst,
                    action="BUY_CANDIDATE",
                    reason=f"{quote_source},{profit_reason}",
                    confidence=conf,
                    signal_quality=signal_quality,
                    market_quality=market_quality,
                    position_factor=position_factor,
                    planned_quote=requested_quote,
                )
                continue

            self._append_decision(
                inst_id=inst,
                action=rec,
                reason="hold_or_watch",
                confidence=conf,
                signal_quality=signal_quality,
                market_quality=market_quality,
            )
            if pos.holding:
                self._log_position(inst, pos, note=f"{rec} conf={conf} market_q={market_quality:.2f}{_fmt_meta(r)}")
            else:
                self.log(f"[{inst}] {rec} (conf={conf} market_q={market_quality:.2f} holding={pos.holding}){_fmt_meta(r)}")

        if frequency_limit_skips:
            preview = ",".join(frequency_limit_skips[:3])
            more = "" if len(frequency_limit_skips) <= 3 else f" 等{len(frequency_limit_skips)}个"
            self.log(f"⏸ 本轮因调用频率限制跳过：{preview}{more}")

        if not buy_candidates:
            return

        ranked_candidates = sorted(
            enumerate(buy_candidates),
            key=lambda item: (-float(item[1][1]), -int(item[1][2]), item[0]),
        )
        summary = ", ".join(
            f"{inst}:mq={market_quality:.2f}/conf={conf}/ai_quote={requested_quote:.2f}/src={quote_source}"
            for _idx, (inst, market_quality, conf, requested_quote, _factor, _signal_quality, _r, quote_source) in ranked_candidates
        )
        self.log(f"🧠 BUY 候选排序（按市场质量/置信度）：{summary}")

        for rank, (_idx, (inst, market_quality, conf, requested_quote, position_factor, signal_quality, r, quote_source)) in enumerate(ranked_candidates, start=1):
            current_snapshot = self._build_portfolio_snapshot() or portfolio_snapshot
            if isinstance(current_snapshot, dict):
                portfolio_snapshot = current_snapshot
                self._sync_positions_from_portfolio(portfolio_snapshot)

            final_quote, clamp_reason, clamp_meta = self._calc_buy_quote_after_risk(inst, requested_quote, portfolio_snapshot)
            if final_quote <= 0:
                self._append_decision(
                    inst_id=inst,
                    action="BUY_BLOCKED",
                    reason=clamp_reason,
                    confidence=conf,
                    signal_quality=signal_quality,
                    market_quality=market_quality,
                    position_factor=position_factor,
                    planned_quote=requested_quote,
                )
                self.log(
                    f"[{inst}] BUY(conf={conf}) 被风控拦截 ai_quote={requested_quote:.4f} clamp_reason={clamp_reason} "
                    f"caps={clamp_meta}{_fmt_meta(r)}"
                )
                continue

            pos = self._pos.get(inst) or PositionState()
            self._pos[inst] = pos
            was_holding = bool(pos.holding)
            old_entry_price = pos.entry_price
            old_base_size = max(0.0, float(pos.base_size or 0.0))
            ok, open_info = self._open_long_spot(
                inst,
                quote=final_quote,
                requested_quote=requested_quote,
                market_quality=market_quality,
                signal_quality=signal_quality,
                confidence=conf,
                position_factor=position_factor,
            )
            if ok:
                portfolio_snapshot = self._build_portfolio_snapshot() or portfolio_snapshot
                self._sync_positions_from_portfolio(portfolio_snapshot)
                pos = self._pos.get(inst) or pos
                self._pos[inst] = pos
                pos.holding = True
                pos.entry_confidence = conf
                pos.entry_market_quality = market_quality
                pos.last_market_quality = market_quality
                if pos.quote_size <= 0:
                    pos.quote_size = self._to_float((open_info or {}).get("requestQuote")) or final_quote
                filled_base = self._to_float((open_info or {}).get("accFillSz")) or self._to_float((open_info or {}).get("fillSz"))
                avg_px = self._to_float((open_info or {}).get("avgPx"))
                fill_px = self._to_float((open_info or {}).get("fillPx"))
                entry_px = avg_px or fill_px or pos.last_price
                if entry_px is not None:
                    if was_holding and old_entry_price is not None and old_base_size > 0 and filled_base is not None and filled_base > 0:
                        total_base = old_base_size + float(filled_base)
                        if total_base > 0:
                            pos.entry_price = ((float(old_entry_price) * old_base_size) + (float(entry_px) * float(filled_base))) / total_base
                    elif pos.entry_price is None or not was_holding:
                        pos.entry_price = float(entry_px)
                    if pos.peak_price is None:
                        pos.peak_price = float(entry_px)
                action = "BUY_ADD" if was_holding else "BUY"
                self._append_decision(
                    inst_id=inst,
                    action=action,
                    reason=f"rank={rank},{quote_source},{clamp_reason}",
                    confidence=conf,
                    signal_quality=signal_quality,
                    market_quality=market_quality,
                    position_factor=position_factor,
                    planned_quote=final_quote,
                )
                self._log_position(
                    inst,
                    pos,
                    note=(
                        f"AI建议开仓 rank={rank} conf={conf} market_q={market_quality:.2f} signal_q={signal_quality:.2f} "
                        f"ai_quote={requested_quote:.2f} final_quote={final_quote:.2f} source={quote_source} clamp={clamp_reason}{_fmt_meta(r)}"
                    ),
                )
            else:
                self._append_decision(
                    inst_id=inst,
                    action="BUY_FAILED",
                    reason=f"rank={rank},{quote_source},{clamp_reason}",
                    confidence=conf,
                    signal_quality=signal_quality,
                    market_quality=market_quality,
                    position_factor=position_factor,
                    planned_quote=final_quote,
                )

    def _open_long_spot(
        self,
        inst_id: str,
        *,
        quote: float,
        requested_quote: Optional[float] = None,
        market_quality: float = 0.0,
        signal_quality: float = 0.0,
        confidence: int = 0,
        position_factor: float = 1.0,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        # Market buy using quote amount (USDT) by default
        quote = float(quote)
        if quote <= 0:
            self.log(f"[{inst_id}] ❌ trade_quote<=0，跳过")
            return False, None

        before_snapshot = self._get_balance_snapshot(inst_id)
        self.log(f"[{inst_id}] 💰 BUY 前余额：{self._format_balance_snapshot(before_snapshot)}")

        effective_quote = quote
        quote_ccy = inst_id.split("-", 1)[1].upper() if "-" in inst_id else "USDT"
        try:
            quote_avail = self._to_float(((before_snapshot or {}).get(quote_ccy) or {}).get("availBal"))
            if quote_avail is not None and quote_avail > 0:
                capped = max(0.0, min(float(quote), float(quote_avail) * 0.98))
                if capped <= 0:
                    self.log(f"[{inst_id}] ❌ {quote_ccy} 可用余额不足，跳过 BUY")
                    return False, None
                if abs(capped - quote) > 1e-9:
                    self.log(f"[{inst_id}] ⚠️ BUY 金额从 {quote:.4f} 调整为 {capped:.4f}（按可用余额限制）")
                effective_quote = capped
        except Exception:
            pass

        clid = self._build_cl_ord_id("B", inst_id)
        ai_quote_text = "n/a" if requested_quote is None else f"{float(requested_quote):.4f}"
        self.log(
            f"[{inst_id}] 🚀 提交 BUY 市价单 ai_quote={ai_quote_text} final_quote={effective_quote:.4f} conf={confidence} "
            f"market_q={market_quality:.2f} signal_q={signal_quality:.2f} factor={position_factor:.2f} clOrdId={clid}"
        )
        payload = self.okx.place_order(
            inst_id=inst_id,
            td_mode=self.cfg.td_mode,
            side="buy",
            ord_type="market",
            sz=str(effective_quote),
            tgt_ccy=self.cfg.spot_tgt_ccy,
            cl_ord_id=clid,
        )
        first, err = okx_extract_first_data(payload)
        if err:
            self.log(f"[{inst_id}] ❌ BUY failed: {err} payload={payload}")
            return False, None

        submit_err = self._extract_submit_error(first)
        if submit_err:
            self.log(f"[{inst_id}] ❌ BUY rejected: {submit_err} payload={payload}")
            return False, first

        ord_id = (first or {}).get("ordId") if isinstance(first, dict) else None
        self.log(f"[{inst_id}] 🟢 BUY 已受理 ordId={ord_id} clOrdId={clid}")

        order_info, verify_err = self._verify_order_state(inst_id, ord_id=str(ord_id) if ord_id else None, cl_ord_id=clid)
        if order_info:
            state = str(order_info.get("state") or "unknown").lower()
            summary = self._format_order_summary(order_info)
            if state == "filled":
                self.log(f"[{inst_id}] ✅ BUY 已确认成交 {summary}")
            elif state in ("canceled", "cancelled", "mmp_canceled"):
                self.log(f"[{inst_id}] ⚠️ BUY 已受理但最终未成交 state={state} {summary}")
            else:
                self.log(f"[{inst_id}] ⏳ BUY 已受理，暂未确认最终状态 {summary}")
        elif verify_err:
            self.log(f"[{inst_id}] ⚠️ BUY 已受理，但订单查询失败：{verify_err}")

        after_snapshot = self._get_balance_snapshot(inst_id)
        self.log(f"[{inst_id}] 💰 BUY 后余额：{self._format_balance_snapshot(after_snapshot)}")
        out = dict(order_info or first or {})
        out.setdefault("requestQuote", effective_quote)
        return True, out

    def _close_long_spot(
        self,
        inst_id: str,
        *,
        sell_ratio: Optional[float] = None,
        sell_quote_usdt: float = 0.0,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        # Sell tracked base asset amount first; fallback to available balance if local position size is unknown.
        base_ccy = inst_id.split("-")[0].upper() if "-" in inst_id else inst_id.upper()
        before_snapshot = self._get_balance_snapshot(inst_id)
        self.log(f"[{inst_id}] 💰 SELL 前余额：{self._format_balance_snapshot(before_snapshot)}")
        bal = self.okx.get_balance(ccy=base_ccy)

        avail_base = None
        try:
            if str(bal.get("code")) == "0":
                data = bal.get("data") or []
                if data and isinstance(data, list):
                    details = (data[0] or {}).get("details") or []
                    for d in details:
                        if str(d.get("ccy")).upper() == base_ccy:
                            avail = d.get("availBal") or d.get("availEq") or d.get("cashBal")
                            avail_base = self._to_float(avail)
                            break
        except Exception:
            avail_base = None

        tracked_base = 0.0
        tracked_last_price = 0.0
        try:
            pos = self._pos.get(inst_id) or PositionState()
            tracked_base = max(0.0, float(pos.base_size))
            tracked_last_price = max(0.0, float(pos.last_price or 0.0))
        except Exception:
            tracked_base = 0.0
            tracked_last_price = 0.0

        sellable_base = avail_base if avail_base is not None else 0.0
        if tracked_base > 0 and avail_base is not None and avail_base > 0:
            sellable_base = min(float(avail_base), float(tracked_base))
            self.log(
                f"[{inst_id}] 🧾 优先按机器人记录仓位卖出 tracked={tracked_base:.8f} avail={avail_base:.8f} sellable={sellable_base:.8f}"
            )
        elif tracked_base > 0:
            sellable_base = tracked_base
            self.log(f"[{inst_id}] 🧾 未取到可用余额，按机器人记录仓位卖出 tracked={tracked_base:.8f}")

        if sellable_base <= 0:
            self.log(f"[{inst_id}] ⚠️ 未找到可卖数量（base={base_ccy} tracked={tracked_base} avail={avail_base}），跳过平仓 payload={bal}")
            return False, None

        sell_size_float = sellable_base
        sell_reason = "full_exit"
        if sell_ratio is not None:
            ratio = self._clamp(float(sell_ratio), 0.0, 1.0)
            sell_size_float = sellable_base * ratio
            sell_reason = f"ratio={ratio:.4f}"
        elif sell_quote_usdt > 0:
            last_price = tracked_last_price or float(self._get_last_price(inst_id) or 0.0)
            if last_price > 0:
                sell_size_float = min(sellable_base, float(sell_quote_usdt) / float(last_price))
                sell_reason = f"quote={float(sell_quote_usdt):.4f}"
            else:
                self.log(f"[{inst_id}] ⚠️ 无法根据 sell_amount_usdt 反推数量，回退为整仓卖出")

        if sell_size_float <= 0:
            self.log(f"[{inst_id}] ⚠️ AI 卖出数量计算结果<=0，跳过 SELL reason={sell_reason}")
            return False, None

        sell_sz = f"{sell_size_float:.12f}".rstrip("0").rstrip(".")
        clid = self._build_cl_ord_id("S", inst_id)
        self.log(f"[{inst_id}] 🚀 提交 SELL 市价单 sz={sell_sz} {base_ccy} reason={sell_reason} clOrdId={clid}")
        payload = self.okx.place_order(
            inst_id=inst_id,
            td_mode=self.cfg.td_mode,
            side="sell",
            ord_type="market",
            sz=str(sell_sz),
            cl_ord_id=clid,
        )
        first, err = okx_extract_first_data(payload)
        if err:
            self.log(f"[{inst_id}] ❌ SELL failed: {err} payload={payload}")
            return False, None

        submit_err = self._extract_submit_error(first)
        if submit_err:
            self.log(f"[{inst_id}] ❌ SELL rejected: {submit_err} payload={payload}")
            return False, first

        ord_id = (first or {}).get("ordId") if isinstance(first, dict) else None
        self.log(f"[{inst_id}] 🟢 SELL 已受理 ordId={ord_id} clOrdId={clid}")

        order_info, verify_err = self._verify_order_state(inst_id, ord_id=str(ord_id) if ord_id else None, cl_ord_id=clid)
        if order_info:
            state = str(order_info.get("state") or "unknown").lower()
            summary = self._format_order_summary(order_info)
            if state == "filled":
                self.log(f"[{inst_id}] ✅ SELL 已确认成交 {summary}")
            elif state in ("canceled", "cancelled", "mmp_canceled"):
                self.log(f"[{inst_id}] ⚠️ SELL 已受理但最终未成交 state={state} {summary}")
            else:
                self.log(f"[{inst_id}] ⏳ SELL 已受理，暂未确认最终状态 {summary}")
        elif verify_err:
            self.log(f"[{inst_id}] ⚠️ SELL 已受理，但订单查询失败：{verify_err}")

        after_snapshot = self._get_balance_snapshot(inst_id)
        self.log(f"[{inst_id}] 💰 SELL 后余额：{self._format_balance_snapshot(after_snapshot)}")
        out = dict(order_info or first or {})
        out.setdefault("requestedSellBase", sell_size_float)
        out.setdefault("sellReason", sell_reason)
        return True, out


def load_trade_config_from_env() -> TradeConfig:
    inst_ids = parse_inst_ids(os.environ.get("OKX_SYMBOLS") or default_okx_symbols_env_value())
    bar = (os.environ.get("OKX_BAR") or "1H").strip()
    limit = int(float(os.environ.get("OKX_LIMIT") or 200))
    td_mode = (os.environ.get("OKX_TD_MODE") or "cash").strip()
    trade_quote = float(os.environ.get("OKX_TRADE_QUOTE") or 15)
    spot_tgt_ccy = (os.environ.get("OKX_SPOT_TGT_CCY") or "quote_ccy").strip()
    conf_threshold = int(float(os.environ.get("OKX_CONF_THRESHOLD") or 65))
    loop_seconds = int(float(os.environ.get("OKX_LOOP_SECONDS") or 60))
    order_check_retries = max(1, int(float(os.environ.get("OKX_ORDER_CHECK_RETRIES") or 5)))
    order_check_interval_ms = max(0, int(float(os.environ.get("OKX_ORDER_CHECK_INTERVAL_MS") or 1000)))

    stop_loss_pct = float(os.environ.get("OKX_STOP_LOSS_PCT") or 0.02)
    trailing_stop_pct = float(os.environ.get("OKX_TRAILING_STOP_PCT") or 0.01)
    trailing_activate_pct = float(os.environ.get("OKX_TRAILING_ACTIVATE_PCT") or 0.005)
    entry_cost_buffer_pct = max(0.0, min(1.0, float(os.environ.get("OKX_ENTRY_COST_BUFFER_PCT") or 0.0)))
    estimated_round_trip_cost_pct = max(
        entry_cost_buffer_pct,
        min(1.0, float(os.environ.get("OKX_ESTIMATED_ROUND_TRIP_COST_PCT") or 0.0)),
    )
    min_net_profit_pct = max(0.0, min(1.0, float(os.environ.get("OKX_MIN_NET_PROFIT_PCT") or 0.0)))
    exit_on_ai_sell = (os.environ.get("OKX_EXIT_ON_AI_SELL") or "1").strip() not in ("0", "false", "False")
    dynamic_position_enabled = (os.environ.get("OKX_DYNAMIC_POSITION_ENABLED") or "1").strip() not in ("0", "false", "False")
    market_quality_threshold = max(0.0, min(1.0, float(os.environ.get("OKX_MARKET_QUALITY_THRESHOLD") or 0.58)))
    dynamic_min_factor = max(0.1, float(os.environ.get("OKX_DYNAMIC_MIN_FACTOR") or 0.7))
    dynamic_max_factor = max(dynamic_min_factor, float(os.environ.get("OKX_DYNAMIC_MAX_FACTOR") or 1.5))
    max_total_exposure_ratio = max(0.0, min(1.0, float(os.environ.get("OKX_MAX_TOTAL_EXPOSURE_RATIO") or 0.70)))
    max_single_asset_weight = max(0.0, min(1.0, float(os.environ.get("OKX_MAX_SINGLE_ASSET_WEIGHT") or 0.35)))
    max_order_cash_ratio = max(0.0, min(1.0, float(os.environ.get("OKX_MAX_ORDER_CASH_RATIO") or 0.20)))
    min_cash_reserve_ratio = max(0.0, min(1.0, float(os.environ.get("OKX_MIN_CASH_RESERVE_RATIO") or 0.10)))
    sync_positions_on_start = (os.environ.get("OKX_SYNC_POSITIONS_ON_START") or "1").strip() not in ("0", "false", "False")
    decision_history_limit = max(10, int(float(os.environ.get("OKX_DECISION_HISTORY_LIMIT") or 300)))

    return TradeConfig(
        inst_ids=inst_ids,
        bar=bar,
        limit=limit,
        td_mode=td_mode,
        trade_quote=trade_quote,
        spot_tgt_ccy=spot_tgt_ccy,
        conf_threshold=conf_threshold,
        loop_seconds=loop_seconds,
        order_check_retries=order_check_retries,
        order_check_interval_ms=order_check_interval_ms,
        stop_loss_pct=stop_loss_pct,
        trailing_stop_pct=trailing_stop_pct,
        trailing_activate_pct=trailing_activate_pct,
        estimated_round_trip_cost_pct=estimated_round_trip_cost_pct,
        entry_cost_buffer_pct=entry_cost_buffer_pct,
        min_net_profit_pct=min_net_profit_pct,
        exit_on_ai_sell=bool(exit_on_ai_sell),
        dynamic_position_enabled=bool(dynamic_position_enabled),
        market_quality_threshold=market_quality_threshold,
        dynamic_min_factor=dynamic_min_factor,
        dynamic_max_factor=dynamic_max_factor,
        max_total_exposure_ratio=max_total_exposure_ratio,
        max_single_asset_weight=max_single_asset_weight,
        max_order_cash_ratio=max_order_cash_ratio,
        min_cash_reserve_ratio=min_cash_reserve_ratio,
        sync_positions_on_start=bool(sync_positions_on_start),
        decision_history_limit=decision_history_limit,
    )
