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
- OKX_TRADE_QUOTE: default 10 (USDT) - market buy uses quote amount when tgtCcy=quote_ccy
- OKX_SPOT_TGT_CCY: default "quote_ccy" (for market buy sizing)
- OKX_CONF_THRESHOLD: default 65
- OKX_LOOP_SECONDS: default 60
- OKX_MAX_POSITIONS: default 1 (最多同时持有几个币)

Dynamic position (持仓管理):
- OKX_STOP_LOSS_PCT: default 0.02 (2%)
- OKX_TRAILING_STOP_PCT: default 0.01 (1%)
- OKX_TRAILING_ACTIVATE_PCT: default 0.005 (0.5%)
- OKX_EXIT_ON_AI_SELL: default 1 (AI 给 SELL 且达到阈值就平仓)

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


LogFn = Callable[[str], None]


@dataclass
class PositionState:
    holding: bool = False
    entry_price: Optional[float] = None
    peak_price: Optional[float] = None
    last_price: Optional[float] = None


@dataclass
class TradeConfig:
    inst_ids: List[str]
    bar: str
    limit: int = 200
    td_mode: str = "cash"  # spot
    trade_quote: float = 10.0
    spot_tgt_ccy: str = "quote_ccy"
    conf_threshold: int = 65
    loop_seconds: int = 60
    max_positions: int = 1

    # dynamic position management
    stop_loss_pct: float = 0.02
    trailing_stop_pct: float = 0.01
    trailing_activate_pct: float = 0.005
    exit_on_ai_sell: bool = True


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

    @staticmethod
    def _calc_drawdown_pct(peak: Optional[float], last: Optional[float]) -> Optional[float]:
        if peak is None or last is None or peak <= 0:
            return None
        return (float(last) - float(peak)) / float(peak)

    def _log_position(self, inst: str, pos: PositionState, *, note: str = ""):
        pnl = self._calc_pnl_pct(pos.entry_price, pos.last_price)
        dd = self._calc_drawdown_pct(pos.peak_price, pos.last_price)
        parts = [f"[{inst}] holding={pos.holding}"]
        if pos.entry_price is not None:
            parts.append(f"entry={pos.entry_price:.4f}")
        if pos.last_price is not None:
            parts.append(f"last={pos.last_price:.4f}")
        if pnl is not None:
            parts.append(f"pnl={pnl * 100:.2f}%")
        if dd is not None and pos.peak_price is not None:
            parts.append(f"dd_from_peak={dd * 100:.2f}%")
        if note:
            parts.append(f"note={note}")
        self.log(" ".join(parts))

    def _reset_position(self, pos: PositionState):
        pos.holding = False
        pos.entry_price = None
        pos.peak_price = None

    def _should_force_exit_by_risk(self, inst: str, pos: PositionState) -> bool:
        """风控触发：止损 or 移动止盈。

        - 止损：pnl <= -stop_loss_pct
        - 移动止盈：当 pnl >= trailing_activate_pct 后，如果从峰值回撤 <= -trailing_stop_pct 则平仓
        """
        if not pos.holding:
            return False

        pnl = self._calc_pnl_pct(pos.entry_price, pos.last_price)
        if pnl is None:
            return False

        # hard stop loss
        if float(pnl) <= -abs(float(self.cfg.stop_loss_pct)):
            self._log_position(inst, pos, note=f"触发止损({self.cfg.stop_loss_pct * 100:.2f}%)")
            return True

        # trailing stop only after some profit
        if float(pnl) >= float(self.cfg.trailing_activate_pct):
            dd = self._calc_drawdown_pct(pos.peak_price, pos.last_price)
            if dd is not None and float(dd) <= -abs(float(self.cfg.trailing_stop_pct)):
                self._log_position(inst, pos, note=f"触发移动止盈(回撤 {self.cfg.trailing_stop_pct * 100:.2f}%)")
                return True

        return False

    # ---------- internal ----------
    def _run_loop(self):
        mode_label = "模拟盘" if getattr(self.okx, "simulated_trading", False) else "实盘"
        self.log(f"🟡 自动交易启动（OKX {mode_label}）")
        self.log(
            f"instIds={self.cfg.inst_ids} bar={self.cfg.bar} loop={self.cfg.loop_seconds}s conf>={self.cfg.conf_threshold} "
            f"trade_quote={self.cfg.trade_quote} max_positions={self.cfg.max_positions} "
            f"stop_loss={self.cfg.stop_loss_pct} trailing_stop={self.cfg.trailing_stop_pct} "
            f"trailing_activate={self.cfg.trailing_activate_pct} exit_on_ai_sell={self.cfg.exit_on_ai_sell}"
        )

        # Ensure OKX auth exists
        try:
            self.okx.require_auth()
        except Exception as e:
            self.log(f"❌ 无法启动：{e}")
            return

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
        # Use batch analysis to reduce DeepSeek calls
        batch = self.analyzer.analyze_markets_from_okx(
            inst_ids=self.cfg.inst_ids,
            okx_client=self.okx,
            bar=self.cfg.bar,
            limit=int(self.cfg.limit),
            force_analysis=False,
        )
        results = (batch or {}).get("results") or {}

        # 批量概要：方便判断“这一轮到底有没有调 DeepSeek”
        try:
            bsrc = (batch or {}).get("source")
            binfo = (batch or {}).get("batch") or {}
            budget = (batch or {}).get("budget") or {}
            if bsrc in ("batch_api", "api_error"):
                tks = binfo.get("tokens")
                cst = binfo.get("cost")
                rem = budget.get("remaining")
                parsed_count = binfo.get("parsed_count")
                parse_miss_count = binfo.get("parse_miss_count")
                fallback_used = binfo.get("single_fallback_used")
                self.log(
                    f"📊 本轮批量分析 source={bsrc} tokens={tks} cost=${cst} budget_remaining={rem} "
                    f"parsed={parsed_count} parse_miss={parse_miss_count} fallback_single={fallback_used}"
                )
        except Exception:
            pass

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

        # 先统一刷新价格，保证后面的止损/持仓日志使用同一轮价格
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

        buy_candidates: List[Tuple[str, int, dict]] = []

        for inst in self.cfg.inst_ids:
            pos = self._pos.get(inst) or PositionState()
            self._pos[inst] = pos

            r = results.get(inst) or {}
            rec = str(r.get("recommendation") or "HOLD").upper()
            conf = int(r.get("confidence") or 0)

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
                    ok = self._close_long_spot(inst)
                    if ok:
                        self._reset_position(pos)
                else:
                    self.log(f"[{inst}] skip status={r.get('status')} {reason}{_fmt_meta(r)}")

                    # 解析失败时，把 raw_analysis 也打出来（截断），方便定位 key/格式问题
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
                ok = self._close_long_spot(inst)
                if ok:
                    self._reset_position(pos)
                continue

            # AI 置信度不足：不新开仓；持仓则继续持有
            if conf < int(self.cfg.conf_threshold):
                if pos.holding:
                    self._log_position(inst, pos, note=f"AI conf {conf} < {self.cfg.conf_threshold}，继续持有{_fmt_meta(r)}")
                else:
                    self.log(f"[{inst}] HOLD (conf {conf} < {self.cfg.conf_threshold}){_fmt_meta(r)}")
                continue

            if rec == "SELL" and pos.holding and bool(self.cfg.exit_on_ai_sell):
                ok = self._close_long_spot(inst)
                if ok:
                    self._reset_position(pos)
                else:
                    self._log_position(inst, pos, note=f"SELL(conf={conf}) 信号但平仓失败{_fmt_meta(r)}")
                continue

            if rec == "BUY" and not pos.holding:
                buy_candidates.append((inst, conf, r))
                continue

            # 默认：继续持有或观望
            if pos.holding:
                self._log_position(inst, pos, note=f"{rec} conf={conf}{_fmt_meta(r)}")
            else:
                self.log(f"[{inst}] {rec} (conf={conf} holding={pos.holding}){_fmt_meta(r)}")

        if not buy_candidates:
            return

        ranked_candidates = sorted(
            enumerate(buy_candidates),
            key=lambda item: (-int(item[1][1]), item[0]),
        )
        summary = ", ".join(f"{inst}:{conf}" for _idx, (inst, conf, _r) in ranked_candidates)
        self.log(f"🧠 BUY 候选排序（按置信度）：{summary}")

        available_slots = max(0, int(self.cfg.max_positions) - self._holding_count())
        if available_slots <= 0:
            for _idx, (inst, conf, r) in ranked_candidates:
                self.log(f"[{inst}] BUY(conf={conf}) 但已达到最大持仓数 {self.cfg.max_positions}{_fmt_meta(r)}")
            return

        opened = 0
        for rank, (_idx, (inst, conf, r)) in enumerate(ranked_candidates, start=1):
            if opened >= available_slots:
                self.log(f"[{inst}] BUY(conf={conf}) 但本轮优先级不足，较高置信度标的已占用仓位{_fmt_meta(r)}")
                continue

            pos = self._pos.get(inst) or PositionState()
            self._pos[inst] = pos
            ok = self._open_long_spot(inst)
            if ok:
                opened += 1
                pos.holding = True
                if pos.last_price is not None:
                    pos.entry_price = float(pos.last_price)
                    pos.peak_price = float(pos.last_price)
                self._log_position(inst, pos, note=f"按置信度排名第 {rank} 开仓(conf={conf}){_fmt_meta(r)}")

    def _open_long_spot(self, inst_id: str) -> bool:
        # Market buy using quote amount (USDT) by default
        quote = float(self.cfg.trade_quote)
        if quote <= 0:
            self.log(f"[{inst_id}] ❌ trade_quote<=0，跳过")
            return False

        clid = f"ds-{int(time.time())}-{inst_id.replace('-', '')[:10]}"[-32:]
        payload = self.okx.place_order(
            inst_id=inst_id,
            td_mode=self.cfg.td_mode,
            side="buy",
            ord_type="market",
            sz=str(quote),
            tgt_ccy=self.cfg.spot_tgt_ccy,
            cl_ord_id=clid,
        )
        first, err = okx_extract_first_data(payload)
        if err:
            self.log(f"[{inst_id}] ❌ BUY failed: {err} payload={payload}")
            return False

        self.log(f"[{inst_id}] ✅ BUY market (quote={quote}) ordId={first.get('ordId') if first else ''}")
        return True

    def _close_long_spot(self, inst_id: str) -> bool:
        # Sell available base asset amount from balance
        base_ccy = inst_id.split("-")[0].upper() if "-" in inst_id else inst_id.upper()
        bal = self.okx.get_balance(ccy=base_ccy)

        # OKX balance response nesting differs across accounts; do best-effort extraction
        sell_sz = None
        try:
            if str(bal.get("code")) == "0":
                data = bal.get("data") or []
                if data and isinstance(data, list):
                    details = (data[0] or {}).get("details") or []
                    for d in details:
                        if str(d.get("ccy")).upper() == base_ccy:
                            avail = d.get("availBal") or d.get("availEq") or d.get("cashBal")
                            if avail is not None:
                                sell_sz = str(avail)
                                break
        except Exception:
            sell_sz = None

        if not sell_sz or sell_sz in ("0", "0.0", "0.00"):
            self.log(f"[{inst_id}] ⚠️ 未找到可卖数量（base={base_ccy}），跳过平仓 payload={bal}")
            return False

        clid = f"ds-{int(time.time())}-{base_ccy}"[-32:]
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
            return False

        self.log(f"[{inst_id}] ✅ SELL market (sz={sell_sz} {base_ccy}) ordId={first.get('ordId') if first else ''}")
        return True


def load_trade_config_from_env() -> TradeConfig:
    inst_ids = parse_inst_ids(os.environ.get("OKX_SYMBOLS") or default_okx_symbols_env_value())
    bar = (os.environ.get("OKX_BAR") or "1H").strip()
    limit = int(float(os.environ.get("OKX_LIMIT") or 200))
    td_mode = (os.environ.get("OKX_TD_MODE") or "cash").strip()
    trade_quote = float(os.environ.get("OKX_TRADE_QUOTE") or 10)
    spot_tgt_ccy = (os.environ.get("OKX_SPOT_TGT_CCY") or "quote_ccy").strip()
    conf_threshold = int(float(os.environ.get("OKX_CONF_THRESHOLD") or 65))
    loop_seconds = int(float(os.environ.get("OKX_LOOP_SECONDS") or 60))
    max_positions = max(1, int(float(os.environ.get("OKX_MAX_POSITIONS") or 1)))

    stop_loss_pct = float(os.environ.get("OKX_STOP_LOSS_PCT") or 0.02)
    trailing_stop_pct = float(os.environ.get("OKX_TRAILING_STOP_PCT") or 0.01)
    trailing_activate_pct = float(os.environ.get("OKX_TRAILING_ACTIVATE_PCT") or 0.005)
    exit_on_ai_sell = (os.environ.get("OKX_EXIT_ON_AI_SELL") or "1").strip() not in ("0", "false", "False")

    return TradeConfig(
        inst_ids=inst_ids,
        bar=bar,
        limit=limit,
        td_mode=td_mode,
        trade_quote=trade_quote,
        spot_tgt_ccy=spot_tgt_ccy,
        conf_threshold=conf_threshold,
        loop_seconds=loop_seconds,
        max_positions=max_positions,
        stop_loss_pct=stop_loss_pct,
        trailing_stop_pct=trailing_stop_pct,
        trailing_activate_pct=trailing_activate_pct,
        exit_on_ai_sell=bool(exit_on_ai_sell),
    )
