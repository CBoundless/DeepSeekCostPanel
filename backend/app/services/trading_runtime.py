from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from auto_trader import AutoTrader, TradeConfig, normalize_inst_ids_for_market, parse_inst_ids
from binance_rest_client import BinanceAuth, BinanceClient
from deepseek_analyzer_optimized import AnalyzerConfig, OptimizedDeepSeekAnalyzer
from okx_rest_client import OKXAuth, OKXClient

from ..core.crypto import secret_cipher
from ..core.settings import RUN_POLL_INTERVAL_SECONDS, utc_now
from ..db import SessionLocal
from ..models import Credential, RunDecision, RunEvent, RunOrder, RunSnapshot, Strategy, StrategyRun
from .control_plane import (
    apply_policy_to_config,
    count_recent_recovery_attempts,
    create_alert,
    load_merged_strategy_config,
    parse_symbols,
    record_audit,
    record_recovery_action,
    run_metrics,
    strategy_runtime_policy,
)
from .realtime import realtime_hub


ACTIVE_STATUSES = {"starting", "running", "stopping"}


class StrategyPromptAnalyzer(OptimizedDeepSeekAnalyzer):
    def __init__(self, *args: Any, extra_prompt: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.extra_prompt = (extra_prompt or "").strip()

    def _attach_extra_prompt(self, prompt: str) -> str:
        if not self.extra_prompt:
            return prompt
        extra = (
            "\n\n附加策略偏好（优先参考，但不能违反 JSON 输出要求和已有风控规则）：\n"
            f"{self.extra_prompt}\n"
        )
        return f"{prompt}{extra}"

    def _build_batch_prompt(self, timeframe: str, items: list, portfolio_context: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
        prompt = super()._build_batch_prompt(timeframe, items, portfolio_context=portfolio_context)
        return self._attach_extra_prompt(prompt)

    def _build_optimized_prompt(  # type: ignore[override]
        self,
        symbol: str,
        timeframe: str,
        ohlcv_data: list,
        indicators: dict,
        portfolio_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        prompt = super()._build_optimized_prompt(symbol, timeframe, ohlcv_data, indicators, portfolio_context=portfolio_context)
        return self._attach_extra_prompt(prompt)


@dataclass
class RuntimeHandle:
    run_id: int
    strategy_id: int
    owner_user_id: int
    credential_id: int
    trader: AutoTrader
    symbol_set: set[str] = field(default_factory=set)
    policy: Dict[str, Any] = field(default_factory=dict)
    monitor_thread: Optional[threading.Thread] = None
    stop_requested: bool = False
    last_error: str = ""
    last_log: str = ""
    synced_decision_count: int = 0
    synced_order_count: int = 0


class StrategyRuntimeManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handles: Dict[int, RuntimeHandle] = {}
        self._recovery_timers: Dict[int, threading.Timer] = {}
        self._initialized = False

    def initialize(self) -> None:
        pending_recoveries: list[tuple[int, int, int, int, str]] = []
        with self._lock:
            if self._initialized:
                return
            db = SessionLocal()
            try:
                stale_runs = db.query(StrategyRun).filter(StrategyRun.status.in_(tuple(ACTIVE_STATUSES))).all()
                now = utc_now()
                for run in stale_runs:
                    run.status = "error"
                    run.stop_reason = "service_restart"
                    run.stopped_at = now
                    run.updated_at = now
                    strategy = db.query(Strategy).filter(Strategy.id == run.strategy_id).first()
                    credential = db.query(Credential).filter(Credential.id == run.credential_id).first() if strategy else None
                    if not strategy:
                        continue
                    policy = strategy_runtime_policy(strategy, credential)
                    if policy["auto_recover_enabled"] and policy.get("runtime_supported"):
                        pending_recoveries.append(
                            (
                                strategy.id,
                                run.user_id,
                                run.id,
                                int(policy["auto_recover_cooldown_seconds"]),
                                "service_restart",
                            )
                        )
                        record_audit(
                            db,
                            user_id=run.user_id,
                            action="strategy_recovery_scheduled",
                            resource_type="strategy",
                            resource_id=strategy.id,
                            detail={"run_id": run.id, "reason": "service_restart"},
                        )
                db.commit()
                self._initialized = True
            finally:
                db.close()

        for strategy_id, owner_user_id, failed_run_id, cooldown, reason in pending_recoveries:
            self._schedule_recovery(strategy_id, owner_user_id, failed_run_id, reason=reason, cooldown_seconds=cooldown)

    def is_strategy_running(self, strategy_id: int) -> bool:
        with self._lock:
            return any(handle.strategy_id == strategy_id and handle.trader.is_running() for handle in self._handles.values())

    def start_strategy(
        self,
        strategy_id: int,
        owner_user_id: int,
        *,
        actor_user_id: Optional[int] = None,
        source: str = "manual",
        recovery_of_run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        actor = actor_user_id or owner_user_id
        with self._lock:
            db = SessionLocal()
            try:
                strategy = db.query(Strategy).filter(Strategy.id == strategy_id, Strategy.user_id == owner_user_id).first()
                if not strategy:
                    raise ValueError("策略不存在")
                if self.is_strategy_running(strategy.id):
                    raise ValueError("该策略已经在运行中")

                credential = (
                    db.query(Credential)
                    .filter(Credential.id == strategy.credential_id, Credential.user_id == owner_user_id)
                    .first()
                )
                if not credential:
                    raise ValueError("策略未绑定有效交易账号")

                active_run = (
                    db.query(StrategyRun)
                    .filter(StrategyRun.strategy_id == strategy.id, StrategyRun.status.in_(tuple(ACTIVE_STATUSES)))
                    .order_by(StrategyRun.id.desc())
                    .first()
                )
                if active_run:
                    raise ValueError("该策略存在未结束运行记录，请稍后再试")

                policy = strategy_runtime_policy(strategy, credential)
                if not policy.get("runtime_supported"):
                    raise ValueError(policy.get("runtime_block_reason") or "当前策略组合暂不支持实时运行")

                active_credential_runs = (
                    db.query(StrategyRun)
                    .filter(
                        StrategyRun.credential_id == credential.id,
                        StrategyRun.status.in_(tuple(ACTIVE_STATUSES)),
                    )
                    .order_by(StrategyRun.id.desc())
                    .all()
                )
                self._enforce_isolation_rules(db, strategy, policy, active_credential_runs)

                try:
                    trader = self._build_trader(strategy, credential, run_id=0)
                    initial_snapshot = self._safe_snapshot(trader)
                    start_equity = self._to_float((initial_snapshot or {}).get("total_equity_usdt"))
                except Exception as exc:
                    raise ValueError(str(exc)) from exc

                now = utc_now()
                run = StrategyRun(
                    strategy_id=strategy.id,
                    user_id=owner_user_id,
                    credential_id=credential.id,
                    status="starting",
                    started_at=now,
                    stopped_at=None,
                    last_heartbeat_at=now,
                    stop_reason=None,
                    last_error=None,
                    start_equity_usdt=start_equity,
                    current_equity_usdt=start_equity,
                    available_usdt=self._to_float((initial_snapshot or {}).get("available_usdt")),
                    exposure_ratio=self._to_float((initial_snapshot or {}).get("exposure_ratio")),
                    pnl_usdt=0.0,
                    pnl_pct=0.0,
                    decision_count=0,
                    created_at=now,
                    updated_at=now,
                )
                db.add(run)
                db.commit()
                db.refresh(run)

                trader = self._build_trader(strategy, credential, run_id=run.id)
                handle = RuntimeHandle(
                    run_id=run.id,
                    strategy_id=strategy.id,
                    owner_user_id=owner_user_id,
                    credential_id=credential.id,
                    trader=trader,
                    symbol_set=set(parse_symbols(strategy.symbols)),
                    policy=policy,
                )
                self._handles[run.id] = handle

                trader.start()
                run.status = "running"
                run.updated_at = utc_now()
                record_audit(
                    db,
                    user_id=actor,
                    action="strategy_started" if source == "manual" else "strategy_recovered",
                    resource_type="strategy",
                    resource_id=strategy.id,
                    detail={
                        "run_id": run.id,
                        "credential_id": credential.id,
                        "source": source,
                        "recovery_of_run_id": recovery_of_run_id,
                        "capital_allocation_ratio": policy["capital_allocation_ratio"],
                        "actor_user_id": actor,
                        "exchange": policy.get("exchange"),
                        "market_type": policy.get("market_type"),
                    },
                )
                db.commit()

                monitor = threading.Thread(target=self._monitor_run, args=(run.id,), name=f"strategy-run-{run.id}", daemon=True)
                handle.monitor_thread = monitor
                monitor.start()
                self._publish_user(owner_user_id, "strategy_started", {"strategy_id": strategy.id, "run_id": run.id, "source": source})
                return self.get_run_summary(run.id)
            finally:
                db.close()

    def stop_strategy(self, strategy_id: int, owner_user_id: int, *, actor_user_id: Optional[int] = None) -> Dict[str, Any]:
        actor = actor_user_id or owner_user_id
        with self._lock:
            target: Optional[RuntimeHandle] = None
            for handle in self._handles.values():
                if handle.strategy_id == strategy_id:
                    target = handle
                    break
            if target is None:
                raise ValueError("策略当前未运行")

            db = SessionLocal()
            try:
                run = db.query(StrategyRun).filter(StrategyRun.id == target.run_id, StrategyRun.user_id == owner_user_id).first()
                if not run:
                    raise ValueError("运行记录不存在")

                target.stop_requested = True
                target.trader.stop()
                run.status = "stopping"
                run.stop_reason = "user_requested"
                run.updated_at = utc_now()
                record_audit(
                    db,
                    user_id=actor,
                    action="strategy_stop_requested",
                    resource_type="strategy",
                    resource_id=strategy_id,
                    detail={"run_id": run.id, "actor_user_id": actor},
                )
                db.commit()
                self._publish_user(owner_user_id, "strategy_stopping", {"strategy_id": strategy_id, "run_id": run.id})
                return self.get_run_summary(run.id)
            finally:
                db.close()

    def get_run_summary(self, run_id: int) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            run = db.query(StrategyRun).filter(StrategyRun.id == run_id).first()
            if not run:
                raise ValueError("运行记录不存在")
            return self._serialize_run(run, db)
        finally:
            db.close()

    def _enforce_isolation_rules(
        self,
        db: Any,
        strategy: Strategy,
        policy: Dict[str, Any],
        active_credential_runs: list[StrategyRun],
    ) -> None:
        if not active_credential_runs:
            return

        current_symbols = set(parse_symbols(strategy.symbols))
        existing_allocation = 0.0
        for item in active_credential_runs:
            if item.strategy_id == strategy.id:
                raise ValueError("该策略已经在运行中")
            other_strategy = db.query(Strategy).filter(Strategy.id == item.strategy_id).first()
            other_credential = db.query(Credential).filter(Credential.id == item.credential_id).first() if other_strategy else None
            if not other_strategy:
                continue
            other_policy = strategy_runtime_policy(other_strategy, other_credential)
            if not policy["allow_shared_credential"] or not other_policy["allow_shared_credential"]:
                raise ValueError("该账号当前存在独占运行策略；如需并发，请在策略配置中开启共享账号并设置资金分配比例")
            existing_allocation += float(other_policy["capital_allocation_ratio"])
            if policy["symbol_isolation"] and other_policy["symbol_isolation"]:
                overlap = current_symbols & set(parse_symbols(other_strategy.symbols))
                if overlap:
                    raise ValueError(f"同账号并发策略存在标的重叠：{', '.join(sorted(overlap))}。请拆分标的或关闭共享并发")

        if existing_allocation + float(policy["capital_allocation_ratio"]) > 1.0001:
            used_pct = existing_allocation * 100
            current_pct = float(policy["capital_allocation_ratio"]) * 100
            raise ValueError(f"共享账号资金分配超限：已占用 {used_pct:.1f}%，当前策略申请 {current_pct:.1f}%")

    def _build_trader(self, strategy: Strategy, credential: Credential, run_id: int) -> AutoTrader:
        policy = strategy_runtime_policy(strategy, credential)
        config_map = apply_policy_to_config(load_merged_strategy_config(strategy), policy)
        analyzer_config = AnalyzerConfig(
            cache_ttl_minutes=self._to_int(config_map.get("analyzer_cache_ttl_minutes"), 20),
            min_signal_quality=self._to_float(config_map.get("analyzer_min_signal_quality"), 0.55) or 0.55,
            min_interval_minutes=self._to_int(config_map.get("analyzer_min_interval_minutes"), 15),
            max_output_tokens=self._to_int(config_map.get("analyzer_max_output_tokens"), 220),
            temperature=self._to_float(config_map.get("analyzer_temperature"), 0.45) or 0.45,
            daily_budget=self._optional_float(config_map.get("analyzer_daily_budget")),
            budget_enforcement=str(config_map.get("analyzer_budget_enforcement") or "warn"),
            batch_parse_fallback_single=self._to_bool(config_map.get("analyzer_batch_parse_fallback_single"), True),
            batch_parse_fallback_limit=self._to_int(config_map.get("analyzer_batch_parse_fallback_limit"), 8),
            batch_symbols_per_request=self._to_int(config_map.get("analyzer_batch_symbols_per_request"), 4),
        )
        deepseek_api_key = secret_cipher.decrypt(credential.deepseek_api_key_enc)
        api_key = (credential.okx_api_key or "").strip()
        api_secret = secret_cipher.decrypt(credential.okx_api_secret_enc)
        api_passphrase = secret_cipher.decrypt(credential.okx_passphrase_enc)
        if not deepseek_api_key:
            raise ValueError("缺少 DeepSeek API Key，请先完善账号配置")
        if not (api_key and api_secret):
            raise ValueError("缺少交易所 API 凭证，请先完善账号配置")

        analyzer = StrategyPromptAnalyzer(
            api_key=deepseek_api_key,
            base_url=(credential.deepseek_base_url or "https://api.deepseek.com/v1").strip(),
            config=analyzer_config,
            extra_prompt=str(strategy.prompt_template or ""),
        )

        exchange = str(policy.get("exchange") or credential.exchange or "okx").lower()
        if exchange == "binance":
            client = BinanceClient(
                auth=BinanceAuth(api_key=api_key, api_secret=api_secret),
                simulated_trading=bool(credential.simulated_trading),
                base_url=str((self._credential_config(credential).get("base_url") or "")).strip() or None,
            )
        else:
            if not api_passphrase:
                raise ValueError("缺少 OKX Passphrase，请先完善账号配置")
            client = OKXClient(
                auth=OKXAuth(api_key=api_key, api_secret=api_secret, passphrase=api_passphrase),
                simulated_trading=bool(credential.simulated_trading),
            )
        client.require_auth()

        market_type = str(policy.get("market_type") or "spot")
        inst_ids = normalize_inst_ids_for_market(parse_inst_ids(strategy.symbols or ""), market_type)
        if not inst_ids:
            raise ValueError("策略标的不能为空")

        trade_cfg = TradeConfig(
            inst_ids=inst_ids,
            bar=(strategy.timeframe or "1H").strip(),
            limit=self._to_int(config_map.get("limit"), 200),
            td_mode=str(config_map.get("td_mode") or ("cash" if policy.get("market_type") == "spot" else policy.get("margin_mode") or "cross")),
            leverage=max(1.0, float(strategy.leverage or 1.0)),
            trade_quote=self._to_float(config_map.get("trade_quote"), 20.0) or 20.0,
            spot_tgt_ccy=str(config_map.get("spot_tgt_ccy") or "quote_ccy"),
            conf_threshold=self._to_int(config_map.get("conf_threshold"), 74),
            loop_seconds=self._to_int(config_map.get("loop_seconds"), 180),
            order_check_retries=self._to_int(config_map.get("order_check_retries"), 5),
            order_check_interval_ms=self._to_int(config_map.get("order_check_interval_ms"), 1000),
            stop_loss_pct=self._to_float(config_map.get("stop_loss_pct"), 0.03) or 0.03,
            trailing_stop_pct=self._to_float(config_map.get("trailing_stop_pct"), 0.02) or 0.02,
            trailing_activate_pct=self._to_float(config_map.get("trailing_activate_pct"), 0.05) or 0.05,
            estimated_round_trip_cost_pct=self._to_float(config_map.get("estimated_round_trip_cost_pct"), 0.003) or 0.003,
            entry_cost_buffer_pct=self._to_float(config_map.get("entry_cost_buffer_pct"), 0.0015) or 0.0015,
            min_net_profit_pct=self._to_float(config_map.get("min_net_profit_pct"), 0.01) or 0.01,
            exit_on_ai_sell=self._to_bool(config_map.get("exit_on_ai_sell"), True),
            dynamic_position_enabled=self._to_bool(config_map.get("dynamic_position_enabled"), True),
            market_quality_threshold=self._to_float(config_map.get("market_quality_threshold"), 0.64) or 0.64,
            dynamic_min_factor=self._to_float(config_map.get("dynamic_min_factor"), 0.7) or 0.7,
            dynamic_max_factor=self._to_float(config_map.get("dynamic_max_factor"), 1.25) or 1.25,
            max_total_exposure_ratio=self._to_float(config_map.get("max_total_exposure_ratio"), 0.55) or 0.55,
            max_single_asset_weight=self._to_float(config_map.get("max_single_asset_weight"), 0.28) or 0.28,
            max_order_cash_ratio=self._to_float(config_map.get("max_order_cash_ratio"), 0.18) or 0.18,
            min_cash_reserve_ratio=self._to_float(config_map.get("min_cash_reserve_ratio"), 0.18) or 0.18,
            sync_positions_on_start=self._to_bool(config_map.get("sync_positions_on_start"), True),
            decision_history_limit=self._to_int(config_map.get("decision_history_limit"), 300),
            exchange=exchange,
            market_type=market_type,
        )

        log_fn = (lambda _message: None) if run_id <= 0 else (lambda message: self._record_log(run_id, message))
        return AutoTrader(analyzer=analyzer, okx=client, cfg=trade_cfg, log=log_fn)

    def _credential_config(self, credential: Credential) -> Dict[str, Any]:
        try:
            import json
            raw = json.loads(credential.config_json or "{}")
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _monitor_run(self, run_id: int) -> None:
        while True:
            with self._lock:
                handle = self._handles.get(run_id)
            if handle is None:
                return

            running = handle.trader.is_running()
            snapshot = self._safe_snapshot(handle.trader)
            decisions = handle.trader.get_decision_history(300)
            orders = handle.trader.get_order_history(200)

            db = SessionLocal()
            try:
                run = db.query(StrategyRun).filter(StrategyRun.id == run_id).first()
                if not run:
                    self._remove_handle(run_id)
                    return
                strategy = db.query(Strategy).filter(Strategy.id == run.strategy_id).first()
                credential = db.query(Credential).filter(Credential.id == run.credential_id).first() if strategy else None
                policy = strategy_runtime_policy(strategy, credential) if strategy else handle.policy

                now = utc_now()
                run.last_heartbeat_at = now
                run.updated_at = now
                run.decision_count = len(decisions)
                self._sync_decisions(db, run_id, decisions, handle)
                self._sync_orders(db, run, orders, handle)

                if snapshot:
                    current_equity = self._to_float(snapshot.get("total_equity_usdt"))
                    available_usdt = self._to_float(snapshot.get("available_usdt"))
                    exposure_ratio = self._to_float(snapshot.get("exposure_ratio"))
                    run.current_equity_usdt = current_equity
                    run.available_usdt = available_usdt
                    run.exposure_ratio = exposure_ratio
                    if run.start_equity_usdt not in (None, 0) and current_equity is not None:
                        run.pnl_usdt = float(current_equity) - float(run.start_equity_usdt)
                        run.pnl_pct = float(run.pnl_usdt) / float(run.start_equity_usdt)
                    self._record_snapshot(db, run, now)

                if handle.last_error:
                    run.last_error = handle.last_error

                if not running:
                    run.status = "error" if (handle.last_error and not handle.stop_requested) else "stopped"
                    run.stop_reason = run.stop_reason or ("runtime_error" if handle.last_error else "completed")
                    run.stopped_at = now
                    if run.status == "error":
                        create_alert(
                            db,
                            user_id=run.user_id,
                            strategy_id=run.strategy_id,
                            run_id=run.id,
                            severity="error",
                            category="runtime",
                            source="runtime",
                            title="策略运行异常",
                            message=handle.last_error or "策略异常退出，请检查日志与凭证状态",
                            detail={"stop_reason": run.stop_reason, "strategy_id": run.strategy_id, "run_id": run.id},
                        )
                        record_audit(
                            db,
                            user_id=run.user_id,
                            action="strategy_failed",
                            resource_type="strategy",
                            resource_id=run.strategy_id,
                            detail={"run_id": run.id, "error": handle.last_error, "stop_reason": run.stop_reason},
                        )
                    else:
                        record_audit(
                            db,
                            user_id=run.user_id,
                            action="strategy_stopped",
                            resource_type="strategy",
                            resource_id=run.strategy_id,
                            detail={"run_id": run.id, "stop_reason": run.stop_reason},
                        )
                    db.commit()
                    self._publish_user(run.user_id, "strategy_stopped", {"strategy_id": run.strategy_id, "run_id": run.id, "status": run.status})
                    self._remove_handle(run_id)
                    if run.status == "error" and strategy and policy.get("auto_recover_enabled") and policy.get("runtime_supported"):
                        self._schedule_recovery(
                            run.strategy_id,
                            run.user_id,
                            run.id,
                            reason="runtime_error",
                            cooldown_seconds=int(policy.get("auto_recover_cooldown_seconds") or 30),
                        )
                    return

                run.status = "stopping" if handle.stop_requested else "running"
                db.commit()
                self._publish_user(run.user_id, "runtime_update", {"strategy_id": run.strategy_id, "run_id": run.id, "status": run.status})
            finally:
                db.close()

            time.sleep(RUN_POLL_INTERVAL_SECONDS)

    def _schedule_recovery(
        self,
        strategy_id: int,
        owner_user_id: int,
        failed_run_id: int,
        *,
        reason: str,
        cooldown_seconds: int,
    ) -> None:
        with self._lock:
            timer = self._recovery_timers.get(strategy_id)
            if timer and timer.is_alive():
                return
            next_timer = threading.Timer(
                max(5, int(cooldown_seconds or 30)),
                self._attempt_recovery,
                kwargs={
                    "strategy_id": strategy_id,
                    "owner_user_id": owner_user_id,
                    "failed_run_id": failed_run_id,
                    "reason": reason,
                },
            )
            next_timer.daemon = True
            self._recovery_timers[strategy_id] = next_timer
            next_timer.start()

    def _attempt_recovery(self, *, strategy_id: int, owner_user_id: int, failed_run_id: int, reason: str) -> None:
        with self._lock:
            self._recovery_timers.pop(strategy_id, None)

        db = SessionLocal()
        try:
            strategy = db.query(Strategy).filter(Strategy.id == strategy_id, Strategy.user_id == owner_user_id).first()
            credential = db.query(Credential).filter(Credential.id == strategy.credential_id).first() if strategy else None
            if not strategy:
                return
            policy = strategy_runtime_policy(strategy, credential)
            if not policy.get("auto_recover_enabled") or not policy.get("runtime_supported"):
                return
            if self.is_strategy_running(strategy_id):
                return

            attempt_no = count_recent_recovery_attempts(
                db,
                strategy_id=strategy_id,
                window_minutes=int(policy.get("auto_recover_window_minutes") or 60),
            ) + 1
            limit = int(policy.get("auto_recover_limit") or 0)
            if limit > 0 and attempt_no > limit:
                record_recovery_action(
                    db,
                    strategy_id=strategy_id,
                    user_id=owner_user_id,
                    failed_run_id=failed_run_id,
                    recovered_run_id=None,
                    attempt_no=attempt_no,
                    status="skipped_limit",
                    reason=reason,
                    message="自动恢复次数达到上限，已暂停继续拉起",
                )
                create_alert(
                    db,
                    user_id=owner_user_id,
                    strategy_id=strategy_id,
                    run_id=failed_run_id,
                    severity="warning",
                    category="recovery",
                    source="runtime",
                    title="自动恢复已达上限",
                    message="策略连续恢复失败次数已达上限，请人工检查后再启动",
                    detail={"failed_run_id": failed_run_id, "attempt_no": attempt_no, "limit": limit},
                )
                record_audit(
                    db,
                    user_id=owner_user_id,
                    action="strategy_recovery_skipped",
                    resource_type="strategy",
                    resource_id=strategy_id,
                    detail={"failed_run_id": failed_run_id, "attempt_no": attempt_no, "reason": reason},
                )
                db.commit()
                return
            db.commit()
        finally:
            db.close()

        try:
            run = self.start_strategy(
                strategy_id,
                owner_user_id,
                actor_user_id=owner_user_id,
                source="auto_recover",
                recovery_of_run_id=failed_run_id,
            )
            db = SessionLocal()
            try:
                record_recovery_action(
                    db,
                    strategy_id=strategy_id,
                    user_id=owner_user_id,
                    failed_run_id=failed_run_id,
                    recovered_run_id=int(run["id"]),
                    attempt_no=attempt_no,
                    status="started",
                    reason=reason,
                    message="自动恢复已重新拉起策略",
                )
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            db = SessionLocal()
            try:
                record_recovery_action(
                    db,
                    strategy_id=strategy_id,
                    user_id=owner_user_id,
                    failed_run_id=failed_run_id,
                    recovered_run_id=None,
                    attempt_no=attempt_no,
                    status="failed",
                    reason=reason,
                    message=str(exc),
                )
                create_alert(
                    db,
                    user_id=owner_user_id,
                    strategy_id=strategy_id,
                    run_id=failed_run_id,
                    severity="error",
                    category="recovery",
                    source="runtime",
                    title="自动恢复失败",
                    message=str(exc),
                    detail={"failed_run_id": failed_run_id, "attempt_no": attempt_no},
                )
                record_audit(
                    db,
                    user_id=owner_user_id,
                    action="strategy_recovery_failed",
                    resource_type="strategy",
                    resource_id=strategy_id,
                    detail={"failed_run_id": failed_run_id, "attempt_no": attempt_no, "error": str(exc)},
                )
                db.commit()
            finally:
                db.close()

    def _record_log(self, run_id: int, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return

        with self._lock:
            handle = self._handles.get(run_id)
            if handle:
                handle.last_log = text
                if "❌" in text:
                    handle.last_error = text

        db = SessionLocal()
        try:
            run = db.query(StrategyRun).filter(StrategyRun.id == run_id).first()
            if not run:
                return
            level = "error" if "❌" in text else ("warning" if "⚠️" in text else "info")
            db.add(RunEvent(run_id=run_id, level=level, message=text, created_at=utc_now()))
            run.updated_at = utc_now()
            if level == "error":
                run.last_error = text

            extra_count = db.query(RunEvent).filter(RunEvent.run_id == run_id).count() - 600
            if extra_count > 0:
                old_events = (
                    db.query(RunEvent)
                    .filter(RunEvent.run_id == run_id)
                    .order_by(RunEvent.id.asc())
                    .limit(extra_count)
                    .all()
                )
                for item in old_events:
                    db.delete(item)
            db.commit()
            self._publish_user(run.user_id, "log_update", {"strategy_id": run.strategy_id, "run_id": run_id})
        finally:
            db.close()

    def _sync_decisions(self, db: Any, run_id: int, decisions: list[Any], handle: RuntimeHandle) -> None:
        if len(decisions) <= handle.synced_decision_count:
            return
        new_items = decisions[handle.synced_decision_count :]
        for item in new_items:
            created_at = self._iso_from_timestamp(getattr(item, "ts", time.time()))
            db.add(
                RunDecision(
                    run_id=run_id,
                    inst_id=str(getattr(item, "inst_id", "") or ""),
                    action=str(getattr(item, "action", "") or ""),
                    reason=str(getattr(item, "reason", "") or ""),
                    confidence=int(getattr(item, "confidence", 0) or 0),
                    signal_quality=float(getattr(item, "signal_quality", 0.0) or 0.0),
                    market_quality=float(getattr(item, "market_quality", 0.0) or 0.0),
                    position_factor=float(getattr(item, "position_factor", 0.0) or 0.0),
                    planned_quote=float(getattr(item, "planned_quote", 0.0) or 0.0),
                    created_at=created_at,
                )
            )
        handle.synced_decision_count = len(decisions)

    def _sync_orders(self, db: Any, run: StrategyRun, orders: list[Any], handle: RuntimeHandle) -> None:
        import json

        if len(orders) <= handle.synced_order_count:
            return
        new_items = orders[handle.synced_order_count :]
        for item in new_items:
            raw = getattr(item, "raw", None) or {}
            db.add(
                RunOrder(
                    run_id=run.id,
                    strategy_id=run.strategy_id,
                    inst_id=str(getattr(item, "inst_id", "") or ""),
                    side=str(getattr(item, "side", "") or "").upper(),
                    purpose=str(getattr(item, "purpose", "trade") or "trade"),
                    ord_id=str(getattr(item, "ord_id", "") or "") or None,
                    cl_ord_id=str(getattr(item, "cl_ord_id", "") or "") or None,
                    state=str(getattr(item, "state", "unknown") or "unknown"),
                    ord_type=str(getattr(item, "ord_type", "market") or "market"),
                    requested_quote=self._to_float(getattr(item, "requested_quote", None)),
                    requested_size=self._to_float(getattr(item, "requested_size", None)),
                    filled_size=self._to_float(getattr(item, "filled_size", None)),
                    avg_price=self._to_float(getattr(item, "avg_px", None)),
                    fill_price=self._to_float(getattr(item, "fill_px", None)),
                    fee=self._to_float(getattr(item, "fee", None)),
                    raw_json=json.dumps(raw, ensure_ascii=False),
                    created_at=self._iso_from_timestamp(getattr(item, "ts", time.time())),
                    updated_at=utc_now(),
                )
            )
        handle.synced_order_count = len(orders)
        self._publish_user(run.user_id, "orders_update", {"strategy_id": run.strategy_id, "run_id": run.id})

    def _record_snapshot(self, db: Any, run: StrategyRun, created_at: str) -> None:
        db.add(
            RunSnapshot(
                run_id=run.id,
                equity_usdt=self._to_float(run.current_equity_usdt),
                available_usdt=self._to_float(run.available_usdt),
                exposure_ratio=self._to_float(run.exposure_ratio),
                pnl_usdt=self._to_float(run.pnl_usdt),
                pnl_pct=self._to_float(run.pnl_pct),
                created_at=created_at,
            )
        )
        extra_count = db.query(RunSnapshot).filter(RunSnapshot.run_id == run.id).count() - 1200
        if extra_count > 0:
            old_items = (
                db.query(RunSnapshot)
                .filter(RunSnapshot.run_id == run.id)
                .order_by(RunSnapshot.id.asc())
                .limit(extra_count)
                .all()
            )
            for item in old_items:
                db.delete(item)

    def _remove_handle(self, run_id: int) -> None:
        with self._lock:
            self._handles.pop(run_id, None)

    def _safe_snapshot(self, trader: AutoTrader) -> Optional[Dict[str, Any]]:
        try:
            snapshot = trader._build_portfolio_snapshot()  # type: ignore[attr-defined]
            return snapshot if isinstance(snapshot, dict) else None
        except Exception:
            return None

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        try:
            return int(float(value))
        except Exception:
            return int(default)

    @staticmethod
    def _to_float(value: Any, default: float | None = None) -> Optional[float]:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _to_bool(value: Any, default: bool) -> bool:
        if value in (None, ""):
            return bool(default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    @staticmethod
    def _iso_from_timestamp(timestamp: float) -> str:
        try:
            return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except Exception:
            return utc_now()

    @staticmethod
    def _serialize_run(run: StrategyRun, db: Any) -> Dict[str, Any]:
        return {
            "id": run.id,
            "strategy_id": run.strategy_id,
            "credential_id": run.credential_id,
            "status": run.status,
            "started_at": run.started_at,
            "stopped_at": run.stopped_at,
            "last_heartbeat_at": run.last_heartbeat_at,
            "stop_reason": run.stop_reason,
            "last_error": run.last_error,
            "start_equity_usdt": run.start_equity_usdt,
            "current_equity_usdt": run.current_equity_usdt,
            "available_usdt": run.available_usdt,
            "exposure_ratio": run.exposure_ratio,
            "pnl_usdt": run.pnl_usdt,
            "pnl_pct": run.pnl_pct,
            "decision_count": run.decision_count,
            "updated_at": run.updated_at,
            "stats": run_metrics(db, run),
        }

    @staticmethod
    def _publish_user(user_id: int, message_type: str, payload: Dict[str, Any]) -> None:
        realtime_hub.publish_user(user_id, message_type, payload)


runtime_manager = StrategyRuntimeManager()
