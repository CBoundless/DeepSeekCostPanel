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
from decimal import Decimal, InvalidOperation, ROUND_DOWN
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
    side: str = "flat"
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
class OrderRecord:
    ts: float
    inst_id: str
    side: str
    purpose: str = "trade"
    ord_id: Optional[str] = None
    cl_ord_id: Optional[str] = None
    state: str = "unknown"
    ord_type: str = "market"
    requested_quote: Optional[float] = None
    requested_size: Optional[float] = None
    filled_size: Optional[float] = None
    avg_px: Optional[float] = None
    fill_px: Optional[float] = None
    fee: Optional[float] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass
class TradeConfig:
    inst_ids: List[str]
    bar: str
    limit: int = 200
    exchange: str = "okx"
    market_type: str = "spot"
    td_mode: str = "cash"  # spot
    leverage: float = 1.0
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


def normalize_inst_ids_for_market(inst_ids: List[str], market_type: str) -> List[str]:
    market = str(market_type or "spot").strip().lower()
    normalized: List[str] = []
    seen = set()
    for raw in inst_ids or []:
        inst = str(raw or "").strip().upper().replace("/", "-")
        if not inst:
            continue
        if market == "swap" and not inst.endswith("-SWAP"):
            inst = f"{inst}-SWAP"
        elif market != "swap" and inst.endswith("-SWAP"):
            inst = inst[:-5]
        if inst not in seen:
            normalized.append(inst)
            seen.add(inst)
    return normalized


class AutoTrader:
    def __init__(self, *, analyzer: Any, okx: OKXClient, cfg: TradeConfig, log: Optional[LogFn] = None):
        self.analyzer = analyzer
        self.okx = okx
        self.cfg = cfg
        self.cfg.inst_ids = normalize_inst_ids_for_market(list(self.cfg.inst_ids or []), self.cfg.market_type)
        self.log = log or (lambda _msg: None)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # local position state
        self._pos: Dict[str, PositionState] = {inst: PositionState(holding=False) for inst in self.cfg.inst_ids}
        self._debug_conf50_printed: set[str] = set()
        self._decision_history: List[DecisionRecord] = []
        self._order_history: List[OrderRecord] = []
        self._spot_rule_cache: Dict[str, Dict[str, float]] = {}
        self._swap_rule_cache: Dict[str, Dict[str, Any]] = {}

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

    def _is_spot_market(self) -> bool:
        return str(self.cfg.market_type or "spot").strip().lower() == "spot"

    def _is_swap_market(self) -> bool:
        return str(self.cfg.market_type or "spot").strip().lower() == "swap"

    def _supports_short_side(self) -> bool:
        return str(self.cfg.exchange or "okx").strip().lower() == "okx" and self._is_swap_market()

    @staticmethod
    def _normalize_position_side(side: Optional[str]) -> str:
        key = str(side or "flat").strip().lower()
        if key in {"long", "short"}:
            return key
        return "flat"

    @staticmethod
    def _calc_pnl_pct(entry: Optional[float], last: Optional[float], side: str = "long") -> Optional[float]:
        if entry is None or last is None or entry <= 0:
            return None
        normalized_side = AutoTrader._normalize_position_side(side)
        multiplier = -1.0 if normalized_side == "short" else 1.0
        return ((float(last) - float(entry)) / float(entry)) * multiplier

    def _estimated_round_trip_cost_pct(self) -> float:
        return max(0.0, float(self.cfg.estimated_round_trip_cost_pct or 0.0))

    def _estimated_entry_cost_buffer_pct(self) -> float:
        return max(0.0, float(self.cfg.entry_cost_buffer_pct or 0.0))

    def _calc_net_pnl_pct(self, entry: Optional[float], last: Optional[float], side: str = "long") -> Optional[float]:
        gross = self._calc_pnl_pct(entry, last, side)
        if gross is None:
            return None
        return float(gross) - self._estimated_round_trip_cost_pct()

    def _calc_target_net_profit_pct(self, entry: Optional[float], target: Optional[float], side: str = "long") -> Optional[float]:
        gross = self._calc_pnl_pct(entry, target, side)
        if gross is None:
            return None
        return float(gross) - self._estimated_round_trip_cost_pct()

    @staticmethod
    def _calc_drawdown_pct(best_price: Optional[float], last: Optional[float], side: str = "long") -> Optional[float]:
        if best_price is None or last is None or best_price <= 0:
            return None
        normalized_side = AutoTrader._normalize_position_side(side)
        if normalized_side == "short":
            return (float(best_price) - float(last)) / float(best_price)
        return (float(last) - float(best_price)) / float(best_price)

    @classmethod
    def _update_best_price(cls, pos: PositionState, price: Optional[float]) -> None:
        px = cls._to_float(price)
        if px is None or px <= 0:
            return
        side = cls._normalize_position_side(getattr(pos, "side", "flat"))
        if side == "short":
            if pos.peak_price is None:
                pos.peak_price = float(px)
            else:
                pos.peak_price = min(float(pos.peak_price), float(px))
            return
        if pos.peak_price is None:
            pos.peak_price = float(px)
        else:
            pos.peak_price = max(float(pos.peak_price), float(px))

    def _log_position(self, inst: str, pos: PositionState, *, note: str = ""):
        side = self._normalize_position_side(pos.side)
        gross_pnl = self._calc_pnl_pct(pos.entry_price, pos.last_price, side)
        net_pnl = self._calc_net_pnl_pct(pos.entry_price, pos.last_price, side)
        dd = self._calc_drawdown_pct(pos.peak_price, pos.last_price, side)
        parts = [f"[{inst}] holding={pos.holding}", f"side={side}"]
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
            parts.append(f"dd_from_best={dd * 100:.2f}%")
        if note:
            parts.append(f"note={note}")
        self.log(" ".join(parts))

    def _reset_position(self, pos: PositionState):
        pos.holding = False
        pos.side = "flat"
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

    @staticmethod
    def _floor_to_step(value: float, step: Optional[float]) -> float:
        numeric = max(0.0, float(value or 0.0))
        step_value = AutoTrader._to_float(step)
        if step_value is None or step_value <= 0:
            return numeric
        try:
            base = Decimal(str(numeric))
            quantum = Decimal(str(step_value))
            units = (base / quantum).quantize(Decimal("1"), rounding=ROUND_DOWN)
            return max(0.0, float(units * quantum))
        except (InvalidOperation, ValueError, OverflowError):
            return numeric

    def _get_spot_trade_rules(self, inst_id: str) -> Dict[str, float]:
        cache_key = str(inst_id or "").upper()
        cached = self._spot_rule_cache.get(cache_key)
        if cached is not None:
            return cached
        payload = self.okx.get_instruments(inst_type="SPOT", inst_id=inst_id)
        first, err = okx_extract_first_data(payload)
        if err or not isinstance(first, dict):
            rules: Dict[str, float] = {}
        else:
            rules = {}
            min_sz = self._to_float(first.get("minSz"))
            lot_sz = self._to_float(first.get("lotSz"))
            if min_sz is not None and min_sz > 0:
                rules["min_sz"] = float(min_sz)
            if lot_sz is not None and lot_sz > 0:
                rules["lot_sz"] = float(lot_sz)
        self._spot_rule_cache[cache_key] = rules
        return rules

    def _normalize_spot_base_size(self, inst_id: str, size: float) -> Tuple[float, Optional[str]]:
        normalized = max(0.0, float(size or 0.0))
        rules = self._get_spot_trade_rules(inst_id)
        lot_sz = self._to_float(rules.get("lot_sz"))
        min_sz = self._to_float(rules.get("min_sz"))
        if lot_sz is not None and lot_sz > 0:
            normalized = self._floor_to_step(normalized, lot_sz)
        if normalized <= 0:
            if lot_sz is not None and lot_sz > 0:
                return 0.0, f"按最小步进 {lot_sz:g} 对齐后数量为 0"
            return 0.0, "数量为 0"
        if min_sz is not None and min_sz > 0 and normalized + 1e-12 < min_sz:
            return 0.0, f"数量 {normalized:.12f} 小于最小下单量 {min_sz:.12f}"
        return normalized, None

    def _get_spot_min_buy_quote(self, inst_id: str, last_price: Optional[float] = None) -> Tuple[Optional[float], Optional[float]]:
        rules = self._get_spot_trade_rules(inst_id)
        min_sz = self._to_float(rules.get("min_sz"))
        if min_sz is None or min_sz <= 0:
            return None, None

        resolved_price = self._to_float(last_price)
        if resolved_price is None or resolved_price <= 0:
            pos = self._pos.get(inst_id)
            resolved_price = self._to_float(getattr(pos, "last_price", None)) if pos is not None else None
        if resolved_price is None or resolved_price <= 0:
            resolved_price = self._to_float(self._get_last_price(inst_id))
        if resolved_price is None or resolved_price <= 0:
            return None, None

        return float(min_sz) * float(resolved_price), float(resolved_price)

    def _get_swap_trade_rules(self, inst_id: str) -> Dict[str, Any]:
        cache_key = str(inst_id or "").upper()
        cached = self._swap_rule_cache.get(cache_key)
        if cached is not None:
            return cached
        payload = self.okx.get_instruments(inst_type="SWAP", inst_id=inst_id)
        first, err = okx_extract_first_data(payload)
        if err or not isinstance(first, dict):
            rules: Dict[str, Any] = {}
        else:
            parts = cache_key.split("-")
            quote_ccy = parts[1] if len(parts) >= 2 else "USDT"
            rules = {
                "quote_ccy": quote_ccy,
                "ct_val_ccy": str(first.get("ctValCcy") or "").upper(),
                "settle_ccy": str(first.get("settleCcy") or quote_ccy).upper(),
            }
            for src, dst in (("minSz", "min_sz"), ("lotSz", "lot_sz"), ("ctVal", "ct_val")):
                value = self._to_float(first.get(src))
                if value is not None and value > 0:
                    rules[dst] = float(value)
        self._swap_rule_cache[cache_key] = rules
        return rules

    def _get_swap_quote_per_contract(self, inst_id: str, last_price: Optional[float] = None) -> Tuple[Optional[float], Dict[str, Any]]:
        rules = self._get_swap_trade_rules(inst_id)
        ct_val = self._to_float(rules.get("ct_val"))
        if ct_val is None or ct_val <= 0:
            return None, rules
        quote_ccy = str(rules.get("quote_ccy") or "USDT").upper()
        ct_val_ccy = str(rules.get("ct_val_ccy") or "").upper()
        if ct_val_ccy and ct_val_ccy == quote_ccy:
            return float(ct_val), rules

        resolved_price = self._to_float(last_price)
        if resolved_price is None or resolved_price <= 0:
            pos = self._pos.get(inst_id)
            resolved_price = self._to_float(getattr(pos, "last_price", None)) if pos is not None else None
        if resolved_price is None or resolved_price <= 0:
            resolved_price = self._to_float(self._get_last_price(inst_id))
        if resolved_price is None or resolved_price <= 0:
            return None, rules
        return float(ct_val) * float(resolved_price), rules

    def _normalize_swap_contract_size(self, inst_id: str, size: float) -> Tuple[float, Optional[str]]:
        normalized = max(0.0, float(size or 0.0))
        rules = self._get_swap_trade_rules(inst_id)
        lot_sz = self._to_float(rules.get("lot_sz"))
        min_sz = self._to_float(rules.get("min_sz"))
        if lot_sz is not None and lot_sz > 0:
            normalized = self._floor_to_step(normalized, lot_sz)
        if normalized <= 0:
            if lot_sz is not None and lot_sz > 0:
                return 0.0, f"按合约步进 {lot_sz:g} 对齐后数量为 0"
            return 0.0, "合约数量为 0"
        if min_sz is not None and min_sz > 0 and normalized + 1e-12 < min_sz:
            return 0.0, f"合约数量 {normalized:.12f} 小于最小下单量 {min_sz:.12f}"
        return normalized, None

    def _calc_swap_contracts_for_quote(
        self,
        inst_id: str,
        quote: float,
        last_price: Optional[float] = None,
    ) -> Tuple[float, Optional[str], Dict[str, float]]:
        requested_quote = max(0.0, float(quote or 0.0))
        leverage = max(1.0, float(self.cfg.leverage or 1.0))
        quote_per_contract, rules = self._get_swap_quote_per_contract(inst_id, last_price=last_price)
        meta: Dict[str, float] = {
            "requested_quote": requested_quote,
            "leverage": leverage,
        }
        if quote_per_contract is None or quote_per_contract <= 0:
            return 0.0, "missing_swap_contract_value", meta
        target_notional = requested_quote * leverage
        raw_contracts = target_notional / float(quote_per_contract)
        contracts, normalize_reason = self._normalize_swap_contract_size(inst_id, raw_contracts)
        meta["quote_per_contract"] = float(quote_per_contract)
        meta["target_notional"] = float(target_notional)
        meta["raw_contracts"] = float(raw_contracts)
        meta["contracts"] = float(contracts)
        if contracts <= 0:
            return 0.0, normalize_reason or "swap_contracts_zero", meta
        estimated_margin = (float(contracts) * float(quote_per_contract)) / leverage if leverage > 0 else 0.0
        meta["estimated_margin"] = float(estimated_margin)
        return float(contracts), None, meta

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

    def get_order_history(self, limit: int = 50) -> List[OrderRecord]:
        lim = max(1, int(limit))
        return list(self._order_history[-lim:])

    def _append_order(
        self,
        *,
        inst_id: str,
        side: str,
        purpose: str,
        order: Optional[Dict[str, Any]],
        requested_quote: Optional[float] = None,
        requested_size: Optional[float] = None,
    ) -> None:
        raw = dict(order or {})
        self._order_history.append(
            OrderRecord(
                ts=time.time(),
                inst_id=inst_id,
                side=str(side or "").upper(),
                purpose=str(purpose or "trade"),
                ord_id=(raw.get("ordId") or raw.get("orderId") or None),
                cl_ord_id=(raw.get("clOrdId") or raw.get("clientOrderId") or None),
                state=str(raw.get("state") or "unknown"),
                ord_type=str(raw.get("ordType") or raw.get("type") or "market"),
                requested_quote=self._to_float(requested_quote),
                requested_size=self._to_float(requested_size),
                filled_size=self._to_float(raw.get("accFillSz") or raw.get("fillSz") or raw.get("executedQty")),
                avg_px=self._to_float(raw.get("avgPx") or raw.get("avgPrice")),
                fill_px=self._to_float(raw.get("fillPx") or raw.get("price")),
                fee=self._to_float(raw.get("fee") or raw.get("commission")),
                raw=raw,
            )
        )
        if len(self._order_history) > 300:
            self._order_history = self._order_history[-300:]

    def _analysis_symbols(self) -> List[str]:
        if str(self.cfg.exchange or "okx").lower() != "binance":
            return list(self.cfg.inst_ids)
        out: List[str] = []
        for inst in self.cfg.inst_ids:
            token = str(inst or "").strip().upper().replace("/", "-")
            if token.endswith("-SWAP"):
                token = token[:-5]
            out.append(token.replace("-", ""))
        return out

    def _analysis_results(self, portfolio_snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if str(self.cfg.exchange or "okx").lower() == "binance":
            batch = self.analyzer.analyze_markets_from_binance(
                symbols=self._analysis_symbols(),
                timeframe=self.cfg.bar,
                limit=int(self.cfg.limit),
                force_analysis=False,
            )
            results = (batch or {}).get("results") or {}
            normalized: Dict[str, Any] = {}
            for inst in self.cfg.inst_ids:
                symbol = str(inst or "").strip().upper().replace("/", "-")
                if symbol.endswith("-SWAP"):
                    symbol = symbol[:-5]
                normalized[inst] = results.get(symbol.replace("-", "")) or {}
            batch["results"] = normalized
            return batch
        return self.analyzer.analyze_markets_from_okx(
            inst_ids=self.cfg.inst_ids,
            okx_client=self.okx,
            bar=self.cfg.bar,
            limit=int(self.cfg.limit),
            force_analysis=False,
            portfolio_context=portfolio_snapshot,
        )

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
    def _extract_okx_code(payload: Optional[Dict[str, Any]]) -> str:
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("code") or "").strip()

    @staticmethod
    def _is_retryable_submit_payload(payload: Optional[Dict[str, Any]]) -> bool:
        code = AutoTrader._extract_okx_code(payload)
        return code in {"500", "50001", "50004", "50011", "50040", "50061", "NETWORK_ERROR"}

    def _query_order_once(
        self,
        inst_id: str,
        *,
        ord_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        if not ord_id and not cl_ord_id:
            return None, "缺少 ordId/clOrdId"
        payload = self.okx.get_order(inst_id=inst_id, ord_id=ord_id, cl_ord_id=cl_ord_id)
        return okx_extract_first_data(payload)

    def _submit_order_with_retries(
        self,
        *,
        inst_id: str,
        action_label: str,
        cl_ord_id: str,
        place_kwargs: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
        submit_attempts = 3
        retry_sleep_seconds = 1.0
        last_payload: Optional[Dict[str, Any]] = None
        last_err = ""

        for attempt in range(1, submit_attempts + 1):
            payload = self.okx.place_order(cl_ord_id=cl_ord_id, **place_kwargs)
            last_payload = payload if isinstance(payload, dict) else {"code": "", "msg": str(payload), "data": []}
            first, err = okx_extract_first_data(last_payload)
            if not err:
                return first, last_payload, ""

            code = self._extract_okx_code(last_payload)
            msg = str(last_payload.get("msg") or err or "")
            existing_order, verify_err = self._query_order_once(inst_id, cl_ord_id=cl_ord_id)
            if existing_order:
                self.log(
                    f"[{inst_id}] ℹ️ {action_label} 提交返回临时错误 code={code}，"
                    f"但通过 clOrdId={cl_ord_id} 查询到订单，按已受理处理"
                )
                return existing_order, last_payload, ""

            last_err = f"{err}; verify={verify_err}" if verify_err else err
            if not self._is_retryable_submit_payload(last_payload) or attempt >= submit_attempts:
                break

            sleep_seconds = retry_sleep_seconds * attempt
            self.log(
                f"[{inst_id}] ⚠️ {action_label} 提交遇到临时错误 code={code} msg={msg}，"
                f"{sleep_seconds:.1f}s 后重试({attempt}/{submit_attempts}) clOrdId={cl_ord_id}"
            )
            time.sleep(sleep_seconds)

        existing_order, verify_err = self._verify_order_state(inst_id, cl_ord_id=cl_ord_id)
        if existing_order:
            self.log(f"[{inst_id}] ℹ️ {action_label} 最终返回报错，但通过 clOrdId={cl_ord_id} 追踪到订单，按已受理处理")
            return existing_order, last_payload, ""
        if verify_err:
            last_err = f"{last_err}; verify={verify_err}" if last_err else verify_err
        if not last_err and isinstance(last_payload, dict):
            last_err = f"OKX error code={self._extract_okx_code(last_payload)} msg={last_payload.get('msg')}"
        return None, last_payload, last_err

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

    def _get_swap_positions_payload(self, inst_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        payload = self.okx.get_positions(inst_type="SWAP", inst_id=inst_id)
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            self.log(f"⚠️ 合约持仓查询失败 inst_id={inst_id or '*'} payload={payload}")
            return None
        return payload

    def _extract_swap_position(self, inst_id: str, payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        data_payload = payload if isinstance(payload, dict) else self._get_swap_positions_payload(inst_id)
        if not isinstance(data_payload, dict):
            return None
        try:
            items = data_payload.get("data") or []
        except Exception:
            items = []
        target_inst = str(inst_id or "").upper()
        for item in items:
            if not isinstance(item, dict) or str(item.get("instId") or "").upper() != target_inst:
                continue
            raw_pos = self._to_float(item.get("pos"))
            side = self._normalize_position_side(item.get("posSide"))
            if side == "flat":
                if raw_pos is None or abs(raw_pos) <= 1e-12:
                    continue
                side = "short" if raw_pos < 0 else "long"
            size = abs(float(raw_pos or 0.0))
            if size <= 0:
                size = abs(float(self._to_float(item.get("availPos")) or 0.0))
            if size <= 0:
                continue
            mark_price = self._to_float(item.get("markPx")) or self._to_float(item.get("last"))
            if mark_price is None or mark_price <= 0:
                mark_price = self._to_float(self._get_last_price(inst_id))
            entry_price = self._to_float(item.get("avgPx")) or self._to_float(item.get("fillPx")) or mark_price
            notional_usdt = abs(float(self._to_float(item.get("notionalUsd")) or 0.0))
            if notional_usdt <= 0 and mark_price is not None and mark_price > 0:
                quote_per_contract, _rules = self._get_swap_quote_per_contract(inst_id, last_price=mark_price)
                if quote_per_contract is not None and quote_per_contract > 0:
                    notional_usdt = abs(float(size) * float(quote_per_contract))
            margin_used = abs(
                float(
                    self._to_float(item.get("imr"))
                    or self._to_float(item.get("margin"))
                    or self._to_float(item.get("marginUsed"))
                    or 0.0
                )
            )
            leverage = max(1.0, float(self._to_float(item.get("lever")) or self.cfg.leverage or 1.0))
            if margin_used <= 0 and notional_usdt > 0:
                margin_used = notional_usdt / leverage
            return {
                "inst_id": inst_id,
                "side": side,
                "base_size": float(size),
                "price": float(mark_price or 0.0),
                "entry_price": float(entry_price or 0.0),
                "market_value_usdt": float(max(0.0, margin_used)),
                "notional_usdt": float(max(0.0, notional_usdt)),
                "leverage": float(leverage),
            }
        return None

    def _build_swap_portfolio_snapshot(self) -> Optional[Dict[str, Any]]:
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

        positions_payload = self._get_swap_positions_payload()
        holdings: List[Dict[str, Any]] = []
        holdings_by_inst: Dict[str, Dict[str, Any]] = {}
        tracked_value_usdt = 0.0

        for inst_id in self.cfg.inst_ids:
            holding = self._extract_swap_position(inst_id, payload=positions_payload)
            if not isinstance(holding, dict):
                continue
            tracked_value_usdt += max(0.0, float(holding.get("market_value_usdt") or 0.0))
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
            "market_type": str(self.cfg.market_type or "swap").strip().lower(),
            "supports_short": bool(self._supports_short_side()),
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

    def _build_portfolio_snapshot(self) -> Optional[Dict[str, Any]]:
        if self._is_swap_market():
            return self._build_swap_portfolio_snapshot()

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
            base_size, _ = self._normalize_spot_base_size(inst_id, base_size)
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
                "side": "long",
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
            "market_type": str(self.cfg.market_type or "spot").strip().lower(),
            "supports_short": bool(self._supports_short_side()),
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
                old_side = self._normalize_position_side(pos.side)
                new_side = self._normalize_position_side(holding.get("side") or ("long" if self._is_spot_market() else pos.side))
                pos.holding = True
                pos.side = "long" if new_side == "flat" else new_side
                pos.base_size = max(0.0, float(holding.get("base_size") or 0.0))
                pos.quote_size = max(0.0, float(holding.get("market_value_usdt") or 0.0))
                entry_px = self._to_float(holding.get("entry_price"))
                px = self._to_float(holding.get("price"))
                if entry_px is not None and entry_px > 0:
                    pos.entry_price = float(entry_px)
                if px is not None and px > 0:
                    pos.last_price = float(px)
                    if pos.entry_price is None:
                        pos.entry_price = float(px)
                    if not was_holding or old_side != pos.side or pos.peak_price is None:
                        pos.peak_price = float(px)
                    else:
                        self._update_best_price(pos, px)
                if not was_holding or old_side != pos.side:
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

        gross_profit_pct = self._calc_pnl_pct(current_price, target_price, "long")
        net_profit_pct = self._calc_target_net_profit_pct(current_price, target_price, "long")
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

    def _assess_short_profitability(
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

        gross_profit_pct = self._calc_pnl_pct(current_price, target_price, "short")
        net_profit_pct = self._calc_target_net_profit_pct(current_price, target_price, "short")
        meta["gross_profit_pct"] = float(gross_profit_pct or 0.0)
        meta["net_profit_pct"] = float(net_profit_pct or 0.0)

        if gross_profit_pct is None:
            return False, "invalid_target_price", meta
        if float(gross_profit_pct) <= 0:
            return False, "target_price_above_market", meta
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

    def _resolve_requested_short_quote(self, result: Dict[str, Any], market_quality: float, confidence: int) -> Tuple[float, float, str]:
        fallback_factor = self._calc_position_factor(market_quality, confidence)
        fallback_quote = max(0.0, float(self.cfg.trade_quote) * fallback_factor)
        raw_sell_amount = (result or {}).get("sell_amount_usdt")
        if raw_sell_amount is None:
            return fallback_quote, fallback_factor, "fallback_dynamic_quote"
        short_amount = self._to_float(raw_sell_amount)
        if short_amount is None:
            return 0.0, fallback_factor, "invalid_ai_short_amount"
        short_amount = max(0.0, float(short_amount))
        ai_factor = (short_amount / float(self.cfg.trade_quote)) if float(self.cfg.trade_quote) > 0 else fallback_factor
        return short_amount, ai_factor, "ai_short_amount"

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

        if self._is_spot_market():
            min_buy_quote, min_buy_price = self._get_spot_min_buy_quote(inst_id)
            if min_buy_quote is not None and min_buy_quote > 0:
                meta["exchange_min_buy_quote"] = float(min_buy_quote)
                if min_buy_price is not None and min_buy_price > 0:
                    meta["exchange_min_buy_price"] = float(min_buy_price)
                if final_quote + 1e-12 < min_buy_quote:
                    return 0.0, "below_exchange_min_buy_quote", meta

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
        - 移动止盈：当 net_pnl >= trailing_activate_pct 后，如果相对有利方向最佳价回撤 <= -trailing_stop_pct 则平仓
        """
        if not pos.holding:
            return False

        side = self._normalize_position_side(pos.side)
        net_pnl = self._calc_net_pnl_pct(pos.entry_price, pos.last_price, side)
        if net_pnl is None:
            return False

        if float(net_pnl) <= -abs(float(self.cfg.stop_loss_pct)):
            self._log_position(inst, pos, note=f"触发止损(净收益 {self.cfg.stop_loss_pct * 100:.2f}%)")
            return True

        if float(net_pnl) >= float(self.cfg.trailing_activate_pct):
            dd = self._calc_drawdown_pct(pos.peak_price, pos.last_price, side)
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
        short_candidates: List[Tuple[str, float, int, float, float, float, dict, str]] = []
        frequency_limit_skips: List[str] = []

        def _queue_reopen_candidate(
            inst: str,
            *,
            target_side: str,
            result: Dict[str, Any],
            confidence: int,
            market_quality: float,
            signal_quality: float,
            trend_strength: float,
            volatility_score: float,
        ) -> bool:
            pos_after_close = self._pos.get(inst) or PositionState()
            if target_side == "long":
                if market_quality < float(self.cfg.market_quality_threshold):
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason=f"reverse_long_market_quality_below_threshold:{market_quality:.2f}",
                        confidence=confidence,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    self.log(
                        f"[{inst}] ♻️ 反手开多被拦截 market_q={market_quality:.2f} < {self.cfg.market_quality_threshold:.2f} "
                        f"trend={trend_strength:.2f} vol_score={volatility_score:.2f}{_fmt_meta(result)}"
                    )
                    return False

                profitable, profit_reason, profit_meta = self._assess_buy_profitability(result, pos_after_close.last_price)
                if not profitable:
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason=f"reverse_long_{profit_reason}",
                        confidence=confidence,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    self.log(
                        f"[{inst}] ♻️ 反手开多被拦截 reason={profit_reason} meta={profit_meta}{_fmt_meta(result)}"
                    )
                    return False

                requested_quote, position_factor, quote_source = self._resolve_requested_buy_quote(result, market_quality, confidence)
                if requested_quote <= 0:
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason=f"reverse_long_{quote_source}",
                        confidence=confidence,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    self.log(f"[{inst}] ♻️ 反手开多金额无效 source={quote_source}{_fmt_meta(result)}")
                    return False

                buy_candidates.append((inst, market_quality, confidence, requested_quote, position_factor, signal_quality, result, quote_source))
                self._append_decision(
                    inst_id=inst,
                    action="BUY_REOPEN_CANDIDATE",
                    reason=f"reverse_to_long,{quote_source},{profit_reason}",
                    confidence=confidence,
                    signal_quality=signal_quality,
                    market_quality=market_quality,
                    position_factor=position_factor,
                    planned_quote=requested_quote,
                )
                self.log(
                    f"[{inst}] ♻️ 已加入同轮反手开多候选 conf={confidence} market_q={market_quality:.2f} "
                    f"ai_quote={requested_quote:.2f} source={quote_source}{_fmt_meta(result)}"
                )
                return True

            if target_side == "short":
                if not self._supports_short_side():
                    self.log(f"[{inst}] ♻️ 当前市场不支持同轮反手做空，忽略 reverse_to=short{_fmt_meta(result)}")
                    return False
                if market_quality < float(self.cfg.market_quality_threshold):
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason=f"reverse_short_market_quality_below_threshold:{market_quality:.2f}",
                        confidence=confidence,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    self.log(
                        f"[{inst}] ♻️ 反手开空被拦截 market_q={market_quality:.2f} < {self.cfg.market_quality_threshold:.2f} "
                        f"trend={trend_strength:.2f} vol_score={volatility_score:.2f}{_fmt_meta(result)}"
                    )
                    return False

                profitable, profit_reason, profit_meta = self._assess_short_profitability(result, pos_after_close.last_price)
                if not profitable:
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason=f"reverse_short_{profit_reason}",
                        confidence=confidence,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    self.log(
                        f"[{inst}] ♻️ 反手开空被拦截 reason={profit_reason} meta={profit_meta}{_fmt_meta(result)}"
                    )
                    return False

                requested_quote, position_factor, quote_source = self._resolve_requested_short_quote(result, market_quality, confidence)
                if requested_quote <= 0:
                    self._append_decision(
                        inst_id=inst,
                        action="SKIP",
                        reason=f"reverse_short_{quote_source}",
                        confidence=confidence,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                    )
                    self.log(f"[{inst}] ♻️ 反手开空金额无效 source={quote_source}{_fmt_meta(result)}")
                    return False

                short_candidates.append((inst, market_quality, confidence, requested_quote, position_factor, signal_quality, result, quote_source))
                self._append_decision(
                    inst_id=inst,
                    action="SHORT_REOPEN_CANDIDATE",
                    reason=f"reverse_to_short,{quote_source},{profit_reason}",
                    confidence=confidence,
                    signal_quality=signal_quality,
                    market_quality=market_quality,
                    position_factor=position_factor,
                    planned_quote=requested_quote,
                )
                self.log(
                    f"[{inst}] ♻️ 已加入同轮反手开空候选 conf={confidence} market_q={market_quality:.2f} "
                    f"ai_quote={requested_quote:.2f} source={quote_source}{_fmt_meta(result)}"
                )
                return True

            return False

        for inst in self.cfg.inst_ids:
            pos = self._pos.get(inst) or PositionState()
            self._pos[inst] = pos

            r = results.get(inst) or {}
            rec = str(r.get("recommendation") or "HOLD").upper()
            conf = int(r.get("confidence") or 0)
            market_quality, signal_quality, trend_strength, volatility_score = self._calc_market_quality(r, conf)
            pos.last_market_quality = market_quality if (pos.holding or rec in {"BUY", "SELL"}) else pos.last_market_quality

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
                    exit_side = self._normalize_position_side(pos.side)
                    ok, _close_info = self._close_position(inst)
                    if ok:
                        portfolio_snapshot = self._build_portfolio_snapshot() or portfolio_snapshot
                        self._sync_positions_from_portfolio(portfolio_snapshot)
                        self._append_decision(
                            inst_id=inst,
                            action="BUY_TO_COVER" if exit_side == "short" else "SELL",
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
                exit_side = self._normalize_position_side(pos.side)
                ok, _close_info = self._close_position(inst)
                if ok:
                    portfolio_snapshot = self._build_portfolio_snapshot() or portfolio_snapshot
                    self._sync_positions_from_portfolio(portfolio_snapshot)
                    self._append_decision(
                        inst_id=inst,
                        action="BUY_TO_COVER" if exit_side == "short" else "SELL",
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

            if rec == "SELL":
                current_side = self._normalize_position_side(pos.side)
                reverse_to = self._extract_reverse_to(r)
                wants_reverse_to_short = reverse_to == "short"
                if pos.holding and current_side == "long" and bool(self.cfg.exit_on_ai_sell):
                    sell_ratio, sell_amount_usdt, has_explicit_sell = self._resolve_sell_request(r)
                    if not wants_reverse_to_short and has_explicit_sell and sell_ratio is None and sell_amount_usdt <= 0:
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

                    sell_ratio_to_use = 1.0 if wants_reverse_to_short else sell_ratio
                    sell_amount_usdt_to_close = 0.0 if wants_reverse_to_short else sell_amount_usdt
                    if sell_ratio_to_use is None and sell_amount_usdt_to_close <= 0:
                        sell_ratio_to_use = 1.0

                    ok, _close_info = self._close_position(
                        inst,
                        sell_ratio=sell_ratio_to_use,
                        sell_quote_usdt=sell_amount_usdt_to_close,
                    )
                    if ok:
                        portfolio_snapshot = self._build_portfolio_snapshot() or portfolio_snapshot
                        self._sync_positions_from_portfolio(portfolio_snapshot)
                        pos = self._pos.get(inst) or PositionState()
                        reverse_queued = False
                        if not pos.holding and wants_reverse_to_short:
                            reverse_queued = _queue_reopen_candidate(
                                inst,
                                target_side="short",
                                result=r,
                                confidence=conf,
                                market_quality=market_quality,
                                signal_quality=signal_quality,
                                trend_strength=trend_strength,
                                volatility_score=volatility_score,
                            )
                        action = "SELL" if not pos.holding else "SELL_PARTIAL"
                        reason = "ai_sell_signal"
                        if wants_reverse_to_short:
                            reason = "ai_sell_signal_reverse_to_short"
                        elif sell_ratio_to_use is not None:
                            reason += f":ratio={sell_ratio_to_use:.4f}"
                        elif sell_amount_usdt_to_close > 0:
                            reason += f":quote={sell_amount_usdt_to_close:.4f}"
                        self._append_decision(
                            inst_id=inst,
                            action=action,
                            reason=reason,
                            confidence=conf,
                            signal_quality=signal_quality,
                            market_quality=market_quality,
                        )
                        if reverse_queued:
                            self.log(f"[{inst}] ♻️ SELL 平仓完成，已按模型意图加入同轮反手开空候选{_fmt_meta(r)}")
                        elif pos.holding:
                            self._log_position(inst, pos, note=f"部分 SELL 后继续持有 market_q={market_quality:.2f}{_fmt_meta(r)}")
                    else:
                        self._log_position(inst, pos, note=f"SELL(conf={conf}) 信号但平仓失败 market_q={market_quality:.2f}{_fmt_meta(r)}")
                    continue

                if self._supports_short_side() and (not pos.holding or current_side == "short"):
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
                            f"[{inst}] SHORT(conf={conf}) 但 market_q={market_quality:.2f} < {self.cfg.market_quality_threshold:.2f} "
                            f"trend={trend_strength:.2f} vol_score={volatility_score:.2f}{_fmt_meta(r)}"
                        )
                        continue

                    profitable, profit_reason, profit_meta = self._assess_short_profitability(r, pos.last_price)
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
                            f"[{inst}] SHORT(conf={conf}) 但预估净收益不足 reason={profit_reason} meta={profit_meta}{_fmt_meta(r)}"
                        )
                        continue

                    requested_quote, position_factor, quote_source = self._resolve_requested_short_quote(r, market_quality, conf)
                    if requested_quote <= 0:
                        self._append_decision(
                            inst_id=inst,
                            action="SKIP",
                            reason=quote_source,
                            confidence=conf,
                            signal_quality=signal_quality,
                            market_quality=market_quality,
                        )
                        self.log(f"[{inst}] SHORT(conf={conf}) 但做空金额无效 source={quote_source}{_fmt_meta(r)}")
                        continue

                    short_candidates.append((inst, market_quality, conf, requested_quote, position_factor, signal_quality, r, quote_source))
                    self._append_decision(
                        inst_id=inst,
                        action="SHORT_CANDIDATE",
                        reason=f"{quote_source},{profit_reason}",
                        confidence=conf,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                        position_factor=position_factor,
                        planned_quote=requested_quote,
                    )
                    continue

            if rec == "BUY":
                current_side = self._normalize_position_side(pos.side)
                reverse_to = self._extract_reverse_to(r)
                wants_reverse_to_long = reverse_to == "long"
                if pos.holding and current_side == "short":
                    ok, _close_info = self._close_position(inst)
                    if ok:
                        portfolio_snapshot = self._build_portfolio_snapshot() or portfolio_snapshot
                        self._sync_positions_from_portfolio(portfolio_snapshot)
                        pos = self._pos.get(inst) or PositionState()
                        reverse_queued = False
                        if not pos.holding and wants_reverse_to_long:
                            reverse_queued = _queue_reopen_candidate(
                                inst,
                                target_side="long",
                                result=r,
                                confidence=conf,
                                market_quality=market_quality,
                                signal_quality=signal_quality,
                                trend_strength=trend_strength,
                                volatility_score=volatility_score,
                            )
                        self._append_decision(
                            inst_id=inst,
                            action="BUY_TO_COVER",
                            reason="ai_buy_signal_close_short_reverse_to_long" if wants_reverse_to_long else "ai_buy_signal_close_short",
                            confidence=conf,
                            signal_quality=signal_quality,
                            market_quality=market_quality,
                        )
                        if reverse_queued:
                            self.log(f"[{inst}] ♻️ BUY 平空完成，已按模型意图加入同轮反手开多候选{_fmt_meta(r)}")
                    else:
                        self._log_position(inst, pos, note=f"BUY(conf={conf}) 信号但空头平仓失败 market_q={market_quality:.2f}{_fmt_meta(r)}")
                    continue

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

        if not buy_candidates and not short_candidates:
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
            old_side = self._normalize_position_side(pos.side)
            old_entry_price = pos.entry_price
            old_base_size = max(0.0, float(pos.base_size or 0.0))
            ok, open_info = self._open_long_position(
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
                pos.side = "long"
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
                    if was_holding and old_side == "long" and old_entry_price is not None and old_base_size > 0 and filled_base is not None and filled_base > 0:
                        total_base = old_base_size + float(filled_base)
                        if total_base > 0:
                            pos.entry_price = ((float(old_entry_price) * old_base_size) + (float(entry_px) * float(filled_base))) / total_base
                    elif pos.entry_price is None or not was_holding or old_side != "long":
                        pos.entry_price = float(entry_px)
                    if old_side != "long" or pos.peak_price is None:
                        pos.peak_price = float(entry_px)
                    else:
                        self._update_best_price(pos, entry_px)
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

        if short_candidates:
            ranked_short_candidates = sorted(
                enumerate(short_candidates),
                key=lambda item: (-float(item[1][1]), -int(item[1][2]), item[0]),
            )
            short_summary = ", ".join(
                f"{inst}:mq={market_quality:.2f}/conf={conf}/ai_quote={requested_quote:.2f}/src={quote_source}"
                for _idx, (inst, market_quality, conf, requested_quote, _factor, _signal_quality, _r, quote_source) in ranked_short_candidates
            )
            self.log(f"🧠 SHORT 候选排序（按市场质量/置信度）：{short_summary}")

            for rank, (_idx, (inst, market_quality, conf, requested_quote, position_factor, signal_quality, r, quote_source)) in enumerate(ranked_short_candidates, start=1):
                current_snapshot = self._build_portfolio_snapshot() or portfolio_snapshot
                if isinstance(current_snapshot, dict):
                    portfolio_snapshot = current_snapshot
                    self._sync_positions_from_portfolio(portfolio_snapshot)

                final_quote, clamp_reason, clamp_meta = self._calc_buy_quote_after_risk(inst, requested_quote, portfolio_snapshot)
                if final_quote <= 0:
                    self._append_decision(
                        inst_id=inst,
                        action="SHORT_BLOCKED",
                        reason=clamp_reason,
                        confidence=conf,
                        signal_quality=signal_quality,
                        market_quality=market_quality,
                        position_factor=position_factor,
                        planned_quote=requested_quote,
                    )
                    self.log(
                        f"[{inst}] SHORT(conf={conf}) 被风控拦截 ai_quote={requested_quote:.4f} clamp_reason={clamp_reason} "
                        f"caps={clamp_meta}{_fmt_meta(r)}"
                    )
                    continue

                pos = self._pos.get(inst) or PositionState()
                self._pos[inst] = pos
                was_holding = bool(pos.holding)
                old_side = self._normalize_position_side(pos.side)
                old_entry_price = pos.entry_price
                old_base_size = max(0.0, float(pos.base_size or 0.0))
                ok, open_info = self._open_short_position(
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
                    pos.side = "short"
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
                        if was_holding and old_side == "short" and old_entry_price is not None and old_base_size > 0 and filled_base is not None and filled_base > 0:
                            total_base = old_base_size + float(filled_base)
                            if total_base > 0:
                                pos.entry_price = ((float(old_entry_price) * old_base_size) + (float(entry_px) * float(filled_base))) / total_base
                        elif pos.entry_price is None or not was_holding or old_side != "short":
                            pos.entry_price = float(entry_px)
                        if old_side != "short" or pos.peak_price is None:
                            pos.peak_price = float(entry_px)
                        else:
                            self._update_best_price(pos, entry_px)
                    action = "SHORT_ADD" if was_holding and old_side == "short" else "SHORT"
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
                            f"AI建议开空 rank={rank} conf={conf} market_q={market_quality:.2f} signal_q={signal_quality:.2f} "
                            f"ai_quote={requested_quote:.2f} final_quote={final_quote:.2f} source={quote_source} clamp={clamp_reason}{_fmt_meta(r)}"
                        ),
                    )
                else:
                    self._append_decision(
                        inst_id=inst,
                        action="SHORT_FAILED",
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

        min_buy_quote, min_buy_price = self._get_spot_min_buy_quote(inst_id)
        if min_buy_quote is not None and min_buy_quote > 0 and effective_quote + 1e-12 < min_buy_quote:
            price_text = f"{min_buy_price:.4f}" if min_buy_price is not None and min_buy_price > 0 else "n/a"
            self.log(
                f"[{inst_id}] ⚠️ BUY 金额 {effective_quote:.4f} 小于交易所最小下单额 {min_buy_quote:.4f}（按最新价 {price_text} 估算），跳过 BUY"
            )
            return False, None

        clid = self._build_cl_ord_id("B", inst_id)
        ai_quote_text = "n/a" if requested_quote is None else f"{float(requested_quote):.4f}"
        self.log(
            f"[{inst_id}] 🚀 提交 BUY 市价单 ai_quote={ai_quote_text} final_quote={effective_quote:.4f} conf={confidence} "
            f"market_q={market_quality:.2f} signal_q={signal_quality:.2f} factor={position_factor:.2f} clOrdId={clid}"
        )
        first, payload, err = self._submit_order_with_retries(
            inst_id=inst_id,
            action_label="BUY",
            cl_ord_id=clid,
            place_kwargs={
                "inst_id": inst_id,
                "td_mode": self.cfg.td_mode,
                "side": "buy",
                "ord_type": "market",
                "sz": str(effective_quote),
                "tgt_ccy": self.cfg.spot_tgt_ccy,
            },
        )
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
        self._append_order(
            inst_id=inst_id,
            side="BUY",
            purpose="open_long_spot",
            order=out,
            requested_quote=effective_quote,
        )
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

        pos = self._pos.get(inst_id) or PositionState()
        self._pos[inst_id] = pos
        tracked_base = 0.0
        tracked_last_price = 0.0
        try:
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

        sell_size_float, normalize_reason = self._normalize_spot_base_size(inst_id, sell_size_float)
        if sell_size_float <= 0:
            self.log(f"[{inst_id}] ⚠️ SELL 数量不满足交易所规则，跳过 SELL reason={sell_reason} detail={normalize_reason}")
            return False, None

        sell_sz = f"{sell_size_float:.12f}".rstrip("0").rstrip(".")
        clid = self._build_cl_ord_id("S", inst_id)
        self.log(f"[{inst_id}] 🚀 提交 SELL 市价单 sz={sell_sz} {base_ccy} reason={sell_reason} clOrdId={clid}")
        first, payload, err = self._submit_order_with_retries(
            inst_id=inst_id,
            action_label="SELL",
            cl_ord_id=clid,
            place_kwargs={
                "inst_id": inst_id,
                "td_mode": self.cfg.td_mode,
                "side": "sell",
                "ord_type": "market",
                "sz": str(sell_sz),
            },
        )
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
        self._append_order(
            inst_id=inst_id,
            side="SELL",
            purpose="close_long_spot",
            order=out,
            requested_size=sell_size_float,
        )
        return True, out

    def _open_swap_position(
        self,
        inst_id: str,
        *,
        pos_side: str,
        quote: float,
        requested_quote: Optional[float] = None,
        market_quality: float = 0.0,
        signal_quality: float = 0.0,
        confidence: int = 0,
        position_factor: float = 1.0,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        quote = max(0.0, float(quote or 0.0))
        if quote <= 0:
            self.log(f"[{inst_id}] ❌ swap 开仓金额<=0，跳过")
            return False, None
        side = self._normalize_position_side(pos_side)
        if side not in {"long", "short"}:
            self.log(f"[{inst_id}] ❌ 非法持仓方向 pos_side={pos_side}")
            return False, None

        before_snapshot = self._build_portfolio_snapshot()
        contracts, calc_reason, calc_meta = self._calc_swap_contracts_for_quote(inst_id, quote)
        if contracts <= 0:
            self.log(f"[{inst_id}] ⚠️ 无法换算有效合约张数 side={side} reason={calc_reason} meta={calc_meta}")
            return False, None

        order_side = "buy" if side == "long" else "sell"
        clid = self._build_cl_ord_id("B" if side == "long" else "S", inst_id)
        ai_quote_text = "n/a" if requested_quote is None else f"{float(requested_quote):.4f}"
        self.log(
            f"[{inst_id}] 🚀 提交 {side.upper()} 合约市价单 ai_quote={ai_quote_text} final_quote={quote:.4f} contracts={contracts:.8f} "
            f"lev={float(self.cfg.leverage or 1.0):.2f} conf={confidence} market_q={market_quality:.2f} signal_q={signal_quality:.2f} "
            f"factor={position_factor:.2f} clOrdId={clid}"
        )
        first, payload, err = self._submit_order_with_retries(
            inst_id=inst_id,
            action_label=f"{side.upper()} 开仓",
            cl_ord_id=clid,
            place_kwargs={
                "inst_id": inst_id,
                "td_mode": self.cfg.td_mode,
                "side": order_side,
                "ord_type": "market",
                "sz": f"{contracts:.12f}".rstrip("0").rstrip("."),
                "pos_side": side,
            },
        )
        if err:
            self.log(f"[{inst_id}] ❌ {side.upper()} 开仓失败: {err} payload={payload}")
            return False, None

        submit_err = self._extract_submit_error(first)
        if submit_err:
            self.log(f"[{inst_id}] ❌ {side.upper()} 开仓被拒绝: {submit_err} payload={payload}")
            return False, first

        ord_id = (first or {}).get("ordId") if isinstance(first, dict) else None
        self.log(f"[{inst_id}] 🟢 {side.upper()} 开仓已受理 ordId={ord_id} clOrdId={clid}")
        order_info, verify_err = self._verify_order_state(inst_id, ord_id=str(ord_id) if ord_id else None, cl_ord_id=clid)
        if order_info:
            state = str(order_info.get("state") or "unknown").lower()
            summary = self._format_order_summary(order_info)
            if state == "filled":
                self.log(f"[{inst_id}] ✅ {side.upper()} 开仓已确认成交 {summary}")
            elif state in ("canceled", "cancelled", "mmp_canceled"):
                self.log(f"[{inst_id}] ⚠️ {side.upper()} 开仓已受理但最终未成交 state={state} {summary}")
            else:
                self.log(f"[{inst_id}] ⏳ {side.upper()} 开仓已受理，暂未确认最终状态 {summary}")
        elif verify_err:
            self.log(f"[{inst_id}] ⚠️ {side.upper()} 开仓已受理，但订单查询失败：{verify_err}")

        out = dict(order_info or first or {})
        out.setdefault("requestQuote", quote)
        out.setdefault("requestContracts", contracts)
        out.setdefault("positionSide", side)
        if isinstance(before_snapshot, dict):
            out.setdefault("beforeAvailableUsdt", float(before_snapshot.get("available_usdt") or 0.0))
        self._append_order(
            inst_id=inst_id,
            side=order_side.upper(),
            purpose=f"open_{side}_swap",
            order=out,
            requested_quote=quote,
            requested_size=contracts,
        )
        return True, out

    def _close_swap_position(
        self,
        inst_id: str,
        *,
        pos_side: str,
        close_ratio: Optional[float] = None,
        close_quote_usdt: float = 0.0,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        side = self._normalize_position_side(pos_side)
        if side not in {"long", "short"}:
            return False, None

        live_pos = self._extract_swap_position(inst_id)
        pos = self._pos.get(inst_id) or PositionState()
        self._pos[inst_id] = pos
        tracked_contracts = max(0.0, float(pos.base_size or 0.0)) if self._normalize_position_side(pos.side) == side else 0.0
        live_contracts = max(0.0, float((live_pos or {}).get("base_size") or 0.0)) if isinstance(live_pos, dict) else 0.0
        closable_contracts = min(tracked_contracts, live_contracts) if tracked_contracts > 0 and live_contracts > 0 else max(tracked_contracts, live_contracts)
        if closable_contracts <= 0:
            self.log(f"[{inst_id}] ⚠️ 未找到可平合约 side={side} tracked={tracked_contracts} live={live_contracts}")
            return False, None

        close_contracts = closable_contracts
        close_reason = "full_exit"
        if close_ratio is not None:
            ratio = self._clamp(float(close_ratio), 0.0, 1.0)
            close_contracts = closable_contracts * ratio
            close_reason = f"ratio={ratio:.4f}"
        elif close_quote_usdt > 0:
            contracts_by_quote, calc_reason, _calc_meta = self._calc_swap_contracts_for_quote(inst_id, close_quote_usdt)
            if contracts_by_quote > 0:
                close_contracts = min(closable_contracts, contracts_by_quote)
                close_reason = f"quote={float(close_quote_usdt):.4f}"
            else:
                self.log(f"[{inst_id}] ⚠️ 无法按金额换算平仓张数，回退整仓平仓 reason={calc_reason}")

        close_contracts, normalize_reason = self._normalize_swap_contract_size(inst_id, close_contracts)
        if close_contracts <= 0:
            self.log(f"[{inst_id}] ⚠️ 合约平仓数量无效 side={side} reason={close_reason} detail={normalize_reason}")
            return False, None

        order_side = "sell" if side == "long" else "buy"
        clid = self._build_cl_ord_id("S" if side == "long" else "B", inst_id)
        self.log(f"[{inst_id}] 🚀 提交 {side.upper()} 平仓市价单 contracts={close_contracts:.8f} reason={close_reason} clOrdId={clid}")
        first, payload, err = self._submit_order_with_retries(
            inst_id=inst_id,
            action_label=f"{side.upper()} 平仓",
            cl_ord_id=clid,
            place_kwargs={
                "inst_id": inst_id,
                "td_mode": self.cfg.td_mode,
                "side": order_side,
                "ord_type": "market",
                "sz": f"{close_contracts:.12f}".rstrip("0").rstrip("."),
                "pos_side": side,
                "reduce_only": True,
            },
        )
        if err:
            self.log(f"[{inst_id}] ❌ {side.upper()} 平仓失败: {err} payload={payload}")
            return False, None

        submit_err = self._extract_submit_error(first)
        if submit_err:
            self.log(f"[{inst_id}] ❌ {side.upper()} 平仓被拒绝: {submit_err} payload={payload}")
            return False, first

        ord_id = (first or {}).get("ordId") if isinstance(first, dict) else None
        self.log(f"[{inst_id}] 🟢 {side.upper()} 平仓已受理 ordId={ord_id} clOrdId={clid}")
        order_info, verify_err = self._verify_order_state(inst_id, ord_id=str(ord_id) if ord_id else None, cl_ord_id=clid)
        if order_info:
            state = str(order_info.get("state") or "unknown").lower()
            summary = self._format_order_summary(order_info)
            if state == "filled":
                self.log(f"[{inst_id}] ✅ {side.upper()} 平仓已确认成交 {summary}")
            elif state in ("canceled", "cancelled", "mmp_canceled"):
                self.log(f"[{inst_id}] ⚠️ {side.upper()} 平仓已受理但最终未成交 state={state} {summary}")
            else:
                self.log(f"[{inst_id}] ⏳ {side.upper()} 平仓已受理，暂未确认最终状态 {summary}")
        elif verify_err:
            self.log(f"[{inst_id}] ⚠️ {side.upper()} 平仓已受理，但订单查询失败：{verify_err}")

        out = dict(order_info or first or {})
        out.setdefault("requestContracts", close_contracts)
        out.setdefault("closeReason", close_reason)
        out.setdefault("positionSide", side)
        self._append_order(
            inst_id=inst_id,
            side=order_side.upper(),
            purpose=f"close_{side}_swap",
            order=out,
            requested_quote=close_quote_usdt if close_quote_usdt > 0 else None,
            requested_size=close_contracts,
        )
        return True, out

    def _open_long_position(
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
        if self._is_swap_market():
            return self._open_swap_position(
                inst_id,
                pos_side="long",
                quote=quote,
                requested_quote=requested_quote,
                market_quality=market_quality,
                signal_quality=signal_quality,
                confidence=confidence,
                position_factor=position_factor,
            )
        return self._open_long_spot(
            inst_id,
            quote=quote,
            requested_quote=requested_quote,
            market_quality=market_quality,
            signal_quality=signal_quality,
            confidence=confidence,
            position_factor=position_factor,
        )

    def _open_short_position(
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
        if not self._supports_short_side():
            self.log(f"[{inst_id}] ⚠️ 当前市场类型不支持做空，跳过 SHORT")
            return False, None
        return self._open_swap_position(
            inst_id,
            pos_side="short",
            quote=quote,
            requested_quote=requested_quote,
            market_quality=market_quality,
            signal_quality=signal_quality,
            confidence=confidence,
            position_factor=position_factor,
        )

    def _close_position(
        self,
        inst_id: str,
        *,
        sell_ratio: Optional[float] = None,
        sell_quote_usdt: float = 0.0,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        pos = self._pos.get(inst_id) or PositionState()
        side = self._normalize_position_side(pos.side)
        if side == "short":
            return self._close_swap_position(inst_id, pos_side="short")
        if self._is_swap_market():
            return self._close_swap_position(inst_id, pos_side="long", close_ratio=sell_ratio, close_quote_usdt=sell_quote_usdt)
        return self._close_long_spot(inst_id, sell_ratio=sell_ratio, sell_quote_usdt=sell_quote_usdt)


def load_trade_config_from_env() -> TradeConfig:
    market_type = (os.environ.get("OKX_MARKET_TYPE") or "spot").strip().lower()
    inst_ids = normalize_inst_ids_for_market(
        parse_inst_ids(os.environ.get("OKX_SYMBOLS") or default_okx_symbols_env_value()),
        market_type,
    )
    bar = (os.environ.get("OKX_BAR") or "1H").strip()
    limit = int(float(os.environ.get("OKX_LIMIT") or 200))
    td_mode = (os.environ.get("OKX_TD_MODE") or ("cash" if market_type == "spot" else "cross")).strip()
    leverage = max(1.0, float(os.environ.get("OKX_LEVERAGE") or 1.0))
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
        market_type=market_type,
        td_mode=td_mode,
        leverage=leverage,
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
