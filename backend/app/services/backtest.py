from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from deepseek_analyzer_optimized import OptimizedDeepSeekAnalyzer

from ..core.settings import merge_strategy_config, utc_now
from ..db import SessionLocal
from ..models import BacktestRun, Credential, Strategy
from .control_plane import parse_symbols, strategy_indicator_profile, strategy_trade_profile
from .indicator_dsl import IndicatorDslProfile, IndicatorDslState, indicator_dsl_engine


@dataclass
class PositionState:
    size: float = 0.0
    entry_price: float = 0.0
    entry_notional: float = 0.0
    margin_used: float = 0.0
    entry_fee: float = 0.0
    peak_price: float = 0.0


class BacktestService:
    def run_backtest(
        self,
        *,
        strategy: Strategy,
        credential: Credential,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        bars: int = 240,
        initial_capital_usdt: float = 1000.0,
        engine: Optional[str] = None,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            config = self._load_strategy_config(strategy, credential)
            symbols = parse_symbols(strategy.symbols)
            inst_id = (symbol or (symbols[0] if symbols else "")).strip().upper()
            if not inst_id:
                raise ValueError("策略未配置可回测标的")

            bar = (timeframe or strategy.timeframe or "1H").strip()
            bars = max(80, min(int(bars or 240), 1200))
            initial_capital_usdt = max(50.0, float(initial_capital_usdt or 1000.0))
            trade_profile = strategy_trade_profile(strategy, credential)
            indicator_profile = strategy_indicator_profile(strategy)
            selected_engine = (engine or ("dsl" if indicator_profile.get("dsl_enabled") else "builtin")).strip().lower()

            ohlcv = self._fetch_ohlcv(trade_profile["exchange"], inst_id, bar, bars)
            result = self._simulate(
                ohlcv=ohlcv,
                inst_id=inst_id,
                bar=bar,
                strategy=strategy,
                config=config,
                initial_capital=initial_capital_usdt,
                trade_profile=trade_profile,
                indicator_profile=indicator_profile,
                engine=selected_engine,
            )

            now = utc_now()
            item = BacktestRun(
                strategy_id=strategy.id,
                user_id=strategy.user_id,
                credential_id=strategy.credential_id,
                strategy_name=strategy.name,
                inst_id=inst_id,
                timeframe=bar,
                bar_count=len(ohlcv),
                status="completed",
                summary_json=json.dumps(result["summary"], ensure_ascii=False),
                equity_curve_json=json.dumps(result["equity_curve"], ensure_ascii=False),
                trades_json=json.dumps(result["trades"], ensure_ascii=False),
                created_at=now,
                updated_at=now,
            )
            db.add(item)
            db.commit()
            db.refresh(item)
            return self.serialize_backtest(item)
        finally:
            db.close()

    def list_backtests(self, strategy_ids: List[int], strategy_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if not strategy_ids:
            return []
        db = SessionLocal()
        try:
            query = db.query(BacktestRun).filter(BacktestRun.strategy_id.in_(strategy_ids))
            if strategy_id is not None:
                query = query.filter(BacktestRun.strategy_id == strategy_id)
            items = query.order_by(BacktestRun.id.desc()).limit(40).all()
            return [self.serialize_backtest(item) for item in items]
        finally:
            db.close()

    def get_backtest(self, backtest_id: int, strategy_ids: List[int]) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            item = (
                db.query(BacktestRun)
                .filter(BacktestRun.id == backtest_id, BacktestRun.strategy_id.in_(strategy_ids or [-1]))
                .first()
            )
            if not item:
                raise ValueError("回测记录不存在")
            return self.serialize_backtest(item)
        finally:
            db.close()

    @staticmethod
    def serialize_backtest(item: BacktestRun) -> Dict[str, Any]:
        return {
            "id": item.id,
            "strategy_id": item.strategy_id,
            "credential_id": item.credential_id,
            "strategy_name": item.strategy_name,
            "inst_id": item.inst_id,
            "timeframe": item.timeframe,
            "bar_count": item.bar_count,
            "status": item.status,
            "summary": BacktestService._loads(item.summary_json),
            "equity_curve": BacktestService._loads(item.equity_curve_json),
            "trades": BacktestService._loads(item.trades_json),
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }

    @staticmethod
    def _loads(raw: Optional[str]) -> Any:
        if not raw:
            return []
        try:
            return json.loads(raw)
        except Exception:
            return []

    @staticmethod
    def _load_strategy_config(strategy: Strategy, credential: Credential) -> Dict[str, Any]:
        try:
            raw_config = json.loads(strategy.config_json or "{}")
        except Exception:
            raw_config = {}
        config = merge_strategy_config(strategy.risk_preset, raw_config)
        config.setdefault("exchange", credential.exchange)
        return config

    @staticmethod
    def _fetch_ohlcv(exchange: str, inst_id: str, timeframe: str, bars: int) -> List[List[Any]]:
        if str(exchange).lower() == "binance":
            symbol = inst_id.replace("-SWAP", "").replace("-", "")
            return OptimizedDeepSeekAnalyzer.fetch_ohlcv_binance(symbol, timeframe, limit=bars)
        return OptimizedDeepSeekAnalyzer.fetch_ohlcv_okx(inst_id, timeframe, limit=bars)

    def _simulate(
        self,
        *,
        ohlcv: List[List[Any]],
        inst_id: str,
        bar: str,
        strategy: Strategy,
        config: Dict[str, Any],
        initial_capital: float,
        trade_profile: Dict[str, Any],
        indicator_profile: Dict[str, Any],
        engine: str,
    ) -> Dict[str, Any]:
        closes = [float(item[4]) for item in ohlcv if len(item) >= 5]
        if len(closes) < 60:
            raise ValueError("K线数量不足，至少需要 60 根")

        trade_quote = max(10.0, float(config.get("trade_quote") or 20.0))
        stop_loss_pct = max(0.005, float(config.get("stop_loss_pct") or 0.03))
        trailing_stop_pct = max(0.005, float(config.get("trailing_stop_pct") or 0.02))
        trailing_activate_pct = max(0.005, float(config.get("trailing_activate_pct") or 0.05))
        min_cash_reserve_ratio = max(0.0, min(0.8, float(config.get("min_cash_reserve_ratio") or 0.18)))
        max_order_cash_ratio = max(0.01, min(1.0, float(config.get("max_order_cash_ratio") or 0.18)))
        leverage = max(1.0, float(strategy.leverage or 1.0))
        market_type = trade_profile["market_type"]
        fee_rate = float(config.get("backtest_fee_rate") or (0.001 if market_type == "spot" else 0.0006))
        slippage_bps = float(config.get("backtest_slippage_bps") or (6 if market_type == "swap" else 3))

        cash = float(initial_capital)
        position = PositionState()
        trades: List[Dict[str, Any]] = []
        equity_curve: List[Dict[str, Any]] = []
        max_equity = float(initial_capital)
        max_drawdown = 0.0
        liquidations = 0

        dsl_profile = IndicatorDslProfile(
            indicator_dsl=str(indicator_profile.get("indicator_dsl") or ""),
            entry_rule=str(indicator_profile.get("entry_rule") or ""),
            exit_rule=str(indicator_profile.get("exit_rule") or ""),
        )
        dsl_state = IndicatorDslState()

        for index in range(50, len(ohlcv)):
            window = ohlcv[: index + 1]
            indicators = OptimizedDeepSeekAnalyzer.build_indicators_from_ohlcv(window)
            close_price = float(window[-1][4])
            ts = int(window[-1][0])
            execution_price = close_price * (1.0 + (slippage_bps / 10000.0))
            exit_price = close_price * (1.0 - (slippage_bps / 10000.0))

            unrealized = 0.0
            if position.size > 0 and position.entry_price > 0:
                unrealized = (close_price - position.entry_price) * position.size
            equity = cash + (position.size * close_price if market_type == "spot" else position.margin_used + unrealized)
            max_equity = max(max_equity, equity)
            drawdown = (equity - max_equity) / max_equity if max_equity > 0 else 0.0
            max_drawdown = min(max_drawdown, drawdown)
            equity_curve.append({
                "ts": ts,
                "equity": round(equity, 4),
                "cash": round(cash, 4),
                "position_value": round(position.size * close_price, 4),
                "unrealized_pnl": round(unrealized, 4),
            })

            if position.size > 0 and market_type in {"margin", "swap"} and position.margin_used > 0:
                if unrealized <= -(position.margin_used * 0.92):
                    liquidation_fee = abs(position.size * close_price) * fee_rate
                    cash += max(0.0, position.margin_used + unrealized - liquidation_fee)
                    trades.append({
                        "side": "LIQUIDATE",
                        "ts": ts,
                        "price": round(close_price, 6),
                        "size": round(position.size, 8),
                        "quote": round(position.margin_used, 4),
                        "fee": round(liquidation_fee, 6),
                        "reason": "liquidation",
                        "pnl_usdt": round(unrealized - liquidation_fee - position.entry_fee, 4),
                    })
                    position = PositionState()
                    liquidations += 1
                    continue

            if engine == "dsl" and dsl_profile.indicator_dsl.strip():
                dsl_result = indicator_dsl_engine.evaluate(dsl_profile, window, dsl_state)
                bullish = bool(dsl_result.entry_signal)
                bearish = bool(dsl_result.exit_signal)
                decision_meta = dsl_result.values
                signal_reason = "dsl_rule"
            else:
                ema9 = float(indicators.get("ema_9") or 0.0)
                ema20 = float(indicators.get("ema_20") or 0.0)
                ema50 = float(indicators.get("ema_50") or 0.0)
                macd_hist = float(indicators.get("macd_hist") or 0.0)
                rsi = float(indicators.get("rsi_14") or 50.0)
                change20 = float(indicators.get("close_change_pct_20") or 0.0)
                bullish = ema9 > ema20 > ema50 and macd_hist > 0 and rsi < 68 and change20 > -0.02
                bearish = ema9 < ema20 or macd_hist < 0 or rsi > 74
                decision_meta = {"ema9": ema9, "ema20": ema20, "ema50": ema50, "macd_hist": macd_hist, "rsi": rsi}
                signal_reason = "builtin_trend"

            if position.size > 0 and position.entry_price > 0:
                position.peak_price = max(position.peak_price or position.entry_price, close_price)
                pnl_pct = (close_price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0
                dd_from_peak = (close_price - position.peak_price) / position.peak_price if position.peak_price > 0 else 0.0
                should_exit = bearish or pnl_pct <= -stop_loss_pct or (pnl_pct >= trailing_activate_pct and dd_from_peak <= -trailing_stop_pct)
                if should_exit:
                    if market_type == "spot":
                        proceeds = position.size * exit_price
                        exit_fee = proceeds * fee_rate
                        realized_pnl = proceeds - exit_fee - position.entry_notional - position.entry_fee
                        cash += proceeds - exit_fee
                    else:
                        notional = position.size * exit_price
                        exit_fee = abs(notional) * fee_rate
                        realized_pnl = (exit_price - position.entry_price) * position.size - position.entry_fee - exit_fee
                        cash += position.margin_used + realized_pnl
                        proceeds = notional
                    trades.append({
                        "side": "SELL",
                        "ts": ts,
                        "price": round(exit_price, 6),
                        "size": round(position.size, 8),
                        "quote": round(proceeds, 4),
                        "fee": round(exit_fee, 6),
                        "reason": signal_reason if bearish else ("stop_loss" if pnl_pct <= -stop_loss_pct else "trailing_stop"),
                        "pnl_pct": round(pnl_pct, 6),
                        "pnl_usdt": round(realized_pnl, 4),
                        "meta": decision_meta,
                    })
                    position = PositionState()
                    continue

            if position.size <= 0 and bullish:
                max_cash_to_use = max(0.0, cash * (1.0 - min_cash_reserve_ratio))
                margin_to_use = min(trade_quote, cash * max_order_cash_ratio, max_cash_to_use)
                if margin_to_use > 10 and execution_price > 0:
                    if market_type == "spot":
                        notional = margin_to_use
                        entry_fee = notional * fee_rate
                        if cash < notional + entry_fee:
                            continue
                        size = notional / execution_price
                        cash -= notional + entry_fee
                        position = PositionState(
                            size=size,
                            entry_price=execution_price,
                            entry_notional=notional,
                            margin_used=notional,
                            entry_fee=entry_fee,
                            peak_price=execution_price,
                        )
                    else:
                        notional = margin_to_use * leverage
                        entry_fee = notional * fee_rate
                        if cash < margin_to_use + entry_fee:
                            continue
                        size = notional / execution_price
                        cash -= margin_to_use + entry_fee
                        position = PositionState(
                            size=size,
                            entry_price=execution_price,
                            entry_notional=notional,
                            margin_used=margin_to_use,
                            entry_fee=entry_fee,
                            peak_price=execution_price,
                        )
                    trades.append({
                        "side": "BUY",
                        "ts": ts,
                        "price": round(execution_price, 6),
                        "size": round(position.size, 8),
                        "quote": round(position.entry_notional if market_type == "spot" else position.margin_used, 4),
                        "fee": round(position.entry_fee, 6),
                        "reason": signal_reason,
                        "meta": decision_meta,
                    })

        final_price = float(ohlcv[-1][4])
        final_unrealized = 0.0
        if position.size > 0 and position.entry_price > 0:
            final_unrealized = (final_price - position.entry_price) * position.size
        final_equity = cash + (position.size * final_price if market_type == "spot" else position.margin_used + final_unrealized)
        total_return = (final_equity - initial_capital) / initial_capital if initial_capital > 0 else 0.0
        sell_trades = [item for item in trades if item.get("side") in {"SELL", "LIQUIDATE"}]
        wins = [item for item in sell_trades if float(item.get("pnl_usdt") or 0.0) > 0]
        summary = {
            "strategy_name": strategy.name,
            "inst_id": inst_id,
            "timeframe": bar,
            "bars": len(ohlcv),
            "initial_capital_usdt": round(initial_capital, 4),
            "final_equity_usdt": round(final_equity, 4),
            "return_pct": round(total_return, 6),
            "max_drawdown_pct": round(abs(max_drawdown), 6),
            "trade_count": len(trades),
            "completed_trade_count": len(sell_trades),
            "win_rate": round((len(wins) / len(sell_trades)) if sell_trades else 0.0, 6),
            "holding_position": position.size > 0,
            "exchange": trade_profile["exchange"],
            "market_type": market_type,
            "margin_mode": trade_profile["margin_mode"],
            "leverage": leverage,
            "engine": engine,
            "dsl_enabled": bool(indicator_profile.get("dsl_enabled")),
            "fee_rate": fee_rate,
            "slippage_bps": slippage_bps,
            "liquidations": liquidations,
            "symbol_count": trade_profile["symbol_count"],
        }
        return {"summary": summary, "equity_curve": equity_curve[-300:], "trades": trades[-150:]}


backtest_service = BacktestService()
