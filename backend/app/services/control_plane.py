from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..core.settings import merge_strategy_config, utc_now
from ..models import (
    AlertEvent,
    AuditLog,
    RecoveryAction,
    RunOrder,
    RunSnapshot,
    Strategy,
    StrategyMarketplaceItem,
    StrategyMember,
    StrategyRun,
    StrategyVersion,
    User,
)
from .realtime import realtime_hub


SUPPORTED_EXCHANGES: Dict[str, Dict[str, Any]] = {
    "okx": {
        "label": "OKX",
        "runtime_supported_markets": ["spot", "margin", "swap"],
        "backtest_supported": True,
        "supports_passphrase": True,
    },
    "binance": {
        "label": "Binance",
        "runtime_supported_markets": ["spot"],
        "backtest_supported": True,
        "supports_passphrase": False,
    },
}
MARKET_TYPES = ["spot", "margin", "swap"]
MARGIN_MODES = ["cash", "cross", "isolated"]
USER_ROLES = ["admin", "operator", "viewer"]
STRATEGY_MEMBER_ROLES = ["owner", "editor", "operator", "viewer"]
MARKET_CATEGORIES = ["community", "trend", "mean_reversion", "breakout", "ai_agent", "research"]

GLOBAL_ROLE_PERMISSIONS: Dict[str, Set[str]] = {
    "admin": {
        "view_dashboard",
        "manage_credentials",
        "manage_strategies",
        "manage_users",
        "view_users",
        "view_alerts",
        "view_audit",
        "publish_market",
    },
    "operator": {
        "view_dashboard",
        "manage_credentials",
        "manage_strategies",
        "view_users",
        "view_alerts",
        "view_audit",
        "publish_market",
    },
    "viewer": {"view_dashboard", "view_users", "view_alerts"},
}

STRATEGY_ROLE_PERMISSIONS: Dict[str, Set[str]] = {
    "owner": {"view", "edit", "delete", "execute", "backtest", "manage_collaboration", "publish_market"},
    "editor": {"view", "edit", "execute", "backtest", "publish_market"},
    "operator": {"view", "execute", "backtest"},
    "viewer": {"view"},
    "none": set(),
}


def safe_json_loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def parse_symbols(symbols: str | None) -> List[str]:
    items = [item.strip().upper().replace("/", "-") for item in str(symbols or "").replace("\n", ",").split(",") if item.strip()]
    dedup: List[str] = []
    seen = set()
    for item in items:
        if item not in seen:
            dedup.append(item)
            seen.add(item)
    return dedup


def normalize_exchange(exchange: str | None) -> str:
    key = (exchange or "okx").strip().lower()
    return key if key in SUPPORTED_EXCHANGES else "okx"


def normalize_market_type(market_type: str | None) -> str:
    key = (market_type or "spot").strip().lower()
    return key if key in MARKET_TYPES else "spot"


def normalize_margin_mode(margin_mode: str | None, market_type: str | None = None) -> str:
    market = normalize_market_type(market_type)
    if market == "spot":
        return "cash"
    key = (margin_mode or "cross").strip().lower()
    return key if key in {"cross", "isolated"} else "cross"


def normalize_user_role(role: str | None) -> str:
    key = (role or "viewer").strip().lower()
    return key if key in USER_ROLES else "viewer"


def normalize_strategy_member_role(role: str | None) -> str:
    key = (role or "viewer").strip().lower()
    return key if key in STRATEGY_MEMBER_ROLES else "viewer"


def has_global_permission(user: User, permission: str) -> bool:
    return permission in GLOBAL_ROLE_PERMISSIONS.get(normalize_user_role(user.role), set())


def has_strategy_permission(access_role: str, permission: str) -> bool:
    return permission in STRATEGY_ROLE_PERMISSIONS.get(normalize_strategy_member_role(access_role), set())


def is_admin(user: Optional[User]) -> bool:
    return bool(user and normalize_user_role(user.role) == "admin")


def load_credential_config(credential: Any) -> Dict[str, Any]:
    return safe_json_loads(getattr(credential, "config_json", "{}"), {}) if credential is not None else {}


def load_strategy_overrides(strategy: Strategy) -> Dict[str, Any]:
    raw = safe_json_loads(strategy.config_json or "{}", {})
    return raw if isinstance(raw, dict) else {}


def load_merged_strategy_config(strategy: Strategy) -> Dict[str, Any]:
    return merge_strategy_config(strategy.risk_preset, load_strategy_overrides(strategy))


def strategy_trade_profile(strategy: Strategy, credential: Optional[Any] = None) -> Dict[str, Any]:
    config = load_strategy_overrides(strategy)
    exchange = normalize_exchange(config.get("exchange") or getattr(credential, "exchange", None) or "okx")
    market_type = normalize_market_type(config.get("market_type") or "spot")
    margin_mode = normalize_margin_mode(config.get("margin_mode"), market_type)
    leverage = max(1.0, float(strategy.leverage or 1.0))
    runtime_support = determine_runtime_support(exchange, market_type)
    return {
        "exchange": exchange,
        "exchange_label": SUPPORTED_EXCHANGES[exchange]["label"],
        "market_type": market_type,
        "margin_mode": margin_mode,
        "leverage": leverage,
        "runtime_supported": runtime_support["supported"],
        "runtime_block_reason": runtime_support["reason"],
        "supports_backtest": bool(SUPPORTED_EXCHANGES[exchange].get("backtest_supported")),
        "symbol_count": len(parse_symbols(strategy.symbols)),
    }


def strategy_indicator_profile(strategy: Strategy) -> Dict[str, Any]:
    config = load_strategy_overrides(strategy)
    indicator_dsl = str(config.get("indicator_dsl") or "")
    entry_rule = str(config.get("entry_rule") or "")
    exit_rule = str(config.get("exit_rule") or "")
    return {
        "indicator_dsl": indicator_dsl,
        "entry_rule": entry_rule,
        "exit_rule": exit_rule,
        "dsl_enabled": bool(indicator_dsl.strip() and (entry_rule.strip() or exit_rule.strip())),
        "indicator_line_count": len([line for line in indicator_dsl.splitlines() if line.strip()]),
    }


def strategy_runtime_policy(strategy: Strategy, credential: Optional[Any] = None) -> Dict[str, Any]:
    config = load_strategy_overrides(strategy)
    allocation_ratio = _clamp_float(config.get("capital_allocation_ratio"), 1.0, 0.05, 1.0)
    auto_recover_limit = max(0, _to_int(config.get("auto_recover_limit"), 2))
    auto_recover_window_minutes = max(5, _to_int(config.get("auto_recover_window_minutes"), 60))
    auto_recover_cooldown_seconds = max(5, _to_int(config.get("auto_recover_cooldown_seconds"), 30))
    trade_profile = strategy_trade_profile(strategy, credential)
    return {
        "capital_allocation_ratio": allocation_ratio,
        "allow_shared_credential": _to_bool(config.get("allow_shared_credential"), False),
        "symbol_isolation": _to_bool(config.get("symbol_isolation"), True),
        "auto_recover_enabled": _to_bool(config.get("auto_recover_enabled"), True),
        "auto_recover_limit": auto_recover_limit,
        "auto_recover_window_minutes": auto_recover_window_minutes,
        "auto_recover_cooldown_seconds": auto_recover_cooldown_seconds,
        "symbols": parse_symbols(strategy.symbols),
        **trade_profile,
    }


def determine_runtime_support(exchange: str, market_type: str) -> Dict[str, Any]:
    exchange_key = normalize_exchange(exchange)
    market_key = normalize_market_type(market_type)
    supported = market_key in set(SUPPORTED_EXCHANGES.get(exchange_key, {}).get("runtime_supported_markets", []))
    if supported:
        return {"supported": True, "reason": ""}
    if exchange_key == "binance" and market_key != "spot":
        return {"supported": False, "reason": "当前运行时已支持 Binance 现货；杠杆/合约先通过增强回测验证。"}
    return {"supported": False, "reason": "当前组合暂未接入实时运行，请改用增强回测或切换到受支持的交易模式。"}


def apply_policy_to_config(config: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    adjusted = dict(config)
    allocation_ratio = _clamp_float(policy.get("capital_allocation_ratio"), 1.0, 0.05, 1.0)
    adjusted["capital_allocation_ratio"] = allocation_ratio
    adjusted["allow_shared_credential"] = _to_bool(policy.get("allow_shared_credential"), False)
    adjusted["symbol_isolation"] = _to_bool(policy.get("symbol_isolation"), True)
    adjusted["auto_recover_enabled"] = _to_bool(policy.get("auto_recover_enabled"), True)
    adjusted["auto_recover_limit"] = max(0, _to_int(policy.get("auto_recover_limit"), 2))
    adjusted["auto_recover_window_minutes"] = max(5, _to_int(policy.get("auto_recover_window_minutes"), 60))
    adjusted["auto_recover_cooldown_seconds"] = max(5, _to_int(policy.get("auto_recover_cooldown_seconds"), 30))
    adjusted["exchange"] = normalize_exchange(policy.get("exchange"))
    adjusted["market_type"] = normalize_market_type(policy.get("market_type"))
    adjusted["margin_mode"] = normalize_margin_mode(policy.get("margin_mode"), adjusted.get("market_type"))
    adjusted["td_mode"] = "cash" if adjusted.get("market_type") == "spot" else adjusted.get("margin_mode")
    adjusted["max_total_exposure_ratio"] = min(_clamp_float(adjusted.get("max_total_exposure_ratio"), allocation_ratio, 0.01, 1.0), allocation_ratio)
    adjusted["max_single_asset_weight"] = min(_clamp_float(adjusted.get("max_single_asset_weight"), allocation_ratio, 0.01, 1.0), allocation_ratio)
    adjusted["max_order_cash_ratio"] = min(_clamp_float(adjusted.get("max_order_cash_ratio"), allocation_ratio, 0.01, 1.0), allocation_ratio)
    adjusted["min_cash_reserve_ratio"] = max(_clamp_float(adjusted.get("min_cash_reserve_ratio"), 0.0, 0.0, 0.98), round(max(0.0, 1.0 - allocation_ratio), 4))
    return adjusted


def merge_strategy_payload(
    *,
    current_config: Optional[Dict[str, Any]] = None,
    incoming_config: Optional[Dict[str, Any]] = None,
    market_type: Optional[str] = None,
    margin_mode: Optional[str] = None,
    indicator_dsl: Optional[str] = None,
    entry_rule: Optional[str] = None,
    exit_rule: Optional[str] = None,
    exchange: Optional[str] = None,
) -> Dict[str, Any]:
    config = dict(current_config or {})
    for key, value in (incoming_config or {}).items():
        if value is None:
            continue
        config[key] = value
    if exchange is not None:
        config["exchange"] = normalize_exchange(exchange)
    if market_type is not None:
        config["market_type"] = normalize_market_type(market_type)
    if margin_mode is not None or market_type is not None:
        config["margin_mode"] = normalize_margin_mode(margin_mode or config.get("margin_mode"), config.get("market_type"))
    if indicator_dsl is not None:
        config["indicator_dsl"] = indicator_dsl
    if entry_rule is not None:
        config["entry_rule"] = entry_rule
    if exit_rule is not None:
        config["exit_rule"] = exit_rule
    return config


def snapshot_strategy_version(
    db: Session,
    strategy: Strategy,
    *,
    operator_user_id: int,
    note: str,
    source: str,
    credential: Optional[Any] = None,
) -> StrategyVersion:
    latest = (
        db.query(StrategyVersion)
        .filter(StrategyVersion.strategy_id == strategy.id)
        .order_by(StrategyVersion.version_no.desc())
        .first()
    )
    version_no = int((latest.version_no if latest else 0) + 1)
    snapshot = {
        "name": strategy.name,
        "description": strategy.description,
        "credential_id": strategy.credential_id,
        "symbols": strategy.symbols,
        "timeframe": strategy.timeframe,
        "risk_preset": strategy.risk_preset,
        "leverage": strategy.leverage,
        "prompt_template": strategy.prompt_template,
        "config": load_strategy_overrides(strategy),
        "trade_profile": strategy_trade_profile(strategy, credential),
        "indicator_profile": strategy_indicator_profile(strategy),
        "updated_at": strategy.updated_at,
    }
    item = StrategyVersion(
        strategy_id=strategy.id,
        user_id=operator_user_id,
        version_no=version_no,
        source=(source or "update").strip() or "update",
        note=(note or "未命名变更").strip() or "未命名变更",
        snapshot_json=json.dumps(snapshot, ensure_ascii=False),
        created_at=utc_now(),
    )
    db.add(item)
    db.flush()
    return item


def serialize_strategy_version(item: StrategyVersion) -> Dict[str, Any]:
    snapshot = safe_json_loads(item.snapshot_json, {})
    config = snapshot.get("config") if isinstance(snapshot, dict) else {}
    trade_profile = snapshot.get("trade_profile") if isinstance(snapshot, dict) else {}
    indicator_profile = snapshot.get("indicator_profile") if isinstance(snapshot, dict) else {}
    return {
        "id": item.id,
        "strategy_id": item.strategy_id,
        "version_no": item.version_no,
        "source": item.source,
        "note": item.note,
        "snapshot": snapshot,
        "config": config if isinstance(config, dict) else {},
        "trade_profile": trade_profile if isinstance(trade_profile, dict) else {},
        "indicator_profile": indicator_profile if isinstance(indicator_profile, dict) else {},
        "created_at": item.created_at,
    }


def record_audit(
    db: Session,
    *,
    user_id: Optional[int],
    action: str,
    resource_type: str,
    resource_id: Any,
    detail: Optional[Dict[str, Any]] = None,
) -> AuditLog:
    item = AuditLog(
        user_id=user_id,
        action=(action or "unknown").strip() or "unknown",
        resource_type=(resource_type or "system").strip() or "system",
        resource_id=str(resource_id) if resource_id not in (None, "") else None,
        detail_json=json.dumps(detail or {}, ensure_ascii=False),
        created_at=utc_now(),
    )
    db.add(item)
    db.flush()
    return item


def serialize_audit_log(item: AuditLog) -> Dict[str, Any]:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "action": item.action,
        "resource_type": item.resource_type,
        "resource_id": item.resource_id,
        "detail": safe_json_loads(item.detail_json, {}),
        "created_at": item.created_at,
    }


def create_alert(
    db: Session,
    *,
    user_id: int,
    strategy_id: Optional[int],
    run_id: Optional[int],
    severity: str,
    category: str,
    source: str,
    title: str,
    message: str,
    detail: Optional[Dict[str, Any]] = None,
) -> AlertEvent:
    now = utc_now()
    item = AlertEvent(
        user_id=user_id,
        strategy_id=strategy_id,
        run_id=run_id,
        severity=(severity or "warning").strip() or "warning",
        category=(category or "runtime").strip() or "runtime",
        source=(source or "runtime").strip() or "runtime",
        title=(title or "系统告警").strip() or "系统告警",
        message=(message or "").strip() or "发生未指定异常",
        status="open",
        detail_json=json.dumps(detail or {}, ensure_ascii=False),
        acknowledged_at=None,
        acknowledged_by_user_id=None,
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    db.flush()
    realtime_hub.publish_user(user_id, "alerts_changed", {"alert_id": item.id, "strategy_id": strategy_id, "run_id": run_id})
    return item


def acknowledge_alert(db: Session, *, alert: AlertEvent, user_id: int) -> AlertEvent:
    now = utc_now()
    alert.status = "acknowledged"
    alert.acknowledged_at = now
    alert.acknowledged_by_user_id = user_id
    alert.updated_at = now
    db.flush()
    realtime_hub.publish_user(alert.user_id, "alerts_changed", {"alert_id": alert.id, "status": alert.status})
    return alert


def serialize_alert(item: AlertEvent) -> Dict[str, Any]:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "strategy_id": item.strategy_id,
        "run_id": item.run_id,
        "severity": item.severity,
        "category": item.category,
        "source": item.source,
        "title": item.title,
        "message": item.message,
        "status": item.status,
        "detail": safe_json_loads(item.detail_json, {}),
        "acknowledged_at": item.acknowledged_at,
        "acknowledged_by_user_id": item.acknowledged_by_user_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def record_recovery_action(
    db: Session,
    *,
    strategy_id: int,
    user_id: int,
    failed_run_id: Optional[int],
    recovered_run_id: Optional[int],
    attempt_no: int,
    status: str,
    reason: str,
    message: str,
) -> RecoveryAction:
    now = utc_now()
    item = RecoveryAction(
        strategy_id=strategy_id,
        user_id=user_id,
        failed_run_id=failed_run_id,
        recovered_run_id=recovered_run_id,
        attempt_no=max(1, int(attempt_no or 1)),
        status=(status or "scheduled").strip() or "scheduled",
        reason=(reason or "runtime_error").strip() or "runtime_error",
        message=(message or "").strip(),
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    db.flush()
    realtime_hub.publish_user(user_id, "recovery_changed", {"strategy_id": strategy_id, "failed_run_id": failed_run_id, "recovered_run_id": recovered_run_id})
    return item


def serialize_recovery_action(item: RecoveryAction) -> Dict[str, Any]:
    return {
        "id": item.id,
        "strategy_id": item.strategy_id,
        "user_id": item.user_id,
        "failed_run_id": item.failed_run_id,
        "recovered_run_id": item.recovered_run_id,
        "attempt_no": item.attempt_no,
        "status": item.status,
        "reason": item.reason,
        "message": item.message,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def count_recent_recovery_attempts(db: Session, *, strategy_id: int, window_minutes: int) -> int:
    window_start = (
        datetime.now(timezone.utc) - timedelta(minutes=max(5, int(window_minutes or 60)))
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return (
        db.query(RecoveryAction)
        .filter(RecoveryAction.strategy_id == strategy_id, RecoveryAction.created_at >= window_start)
        .count()
    )


def strategy_access_role(db: Session, strategy: Strategy, user: User) -> str:
    if is_admin(user):
        return "owner"
    if strategy.user_id == user.id:
        return "owner"
    member = db.query(StrategyMember).filter(StrategyMember.strategy_id == strategy.id, StrategyMember.user_id == user.id).first()
    return normalize_strategy_member_role(member.role) if member else "none"


def list_accessible_strategy_ids(db: Session, user: User) -> List[int]:
    if is_admin(user):
        return [item[0] for item in db.query(Strategy.id).all()]
    owner_ids = [item[0] for item in db.query(Strategy.id).filter(Strategy.user_id == user.id).all()]
    member_ids = [item[0] for item in db.query(StrategyMember.strategy_id).filter(StrategyMember.user_id == user.id).all()]
    return list(dict.fromkeys(owner_ids + member_ids))


def accessible_strategy_query(db: Session, user: User):
    if is_admin(user):
        return db.query(Strategy)
    member_subquery = db.query(StrategyMember.strategy_id).filter(StrategyMember.user_id == user.id)
    return db.query(Strategy).filter(or_(Strategy.user_id == user.id, Strategy.id.in_(member_subquery)))


def strategy_watcher_user_ids(db: Session, strategy_id: int) -> Set[int]:
    watchers: Set[int] = set()
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if strategy:
        watchers.add(int(strategy.user_id))
    member_ids = db.query(StrategyMember.user_id).filter(StrategyMember.strategy_id == strategy_id).all()
    for user_id, in member_ids:
        watchers.add(int(user_id))
    return watchers


def serialize_member(item: StrategyMember, user: Optional[User] = None) -> Dict[str, Any]:
    return {
        "id": item.id,
        "strategy_id": item.strategy_id,
        "user_id": item.user_id,
        "username": user.username if user else None,
        "role": item.role,
        "created_by_user_id": item.created_by_user_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def serialize_marketplace_item(item: StrategyMarketplaceItem, publisher: Optional[User] = None) -> Dict[str, Any]:
    snapshot = safe_json_loads(item.snapshot_json, {})
    config = snapshot.get("config") if isinstance(snapshot, dict) else {}
    trade_profile = snapshot.get("trade_profile") if isinstance(snapshot, dict) else {}
    indicator_profile = snapshot.get("indicator_profile") if isinstance(snapshot, dict) else {}
    return {
        "id": item.id,
        "strategy_id": item.strategy_id,
        "version_id": item.version_id,
        "publisher_user_id": item.publisher_user_id,
        "publisher_username": publisher.username if publisher else None,
        "title": item.title,
        "summary": item.summary,
        "description": item.description,
        "category": item.category,
        "tags": safe_json_loads(item.tags_json, []),
        "exchange": item.exchange,
        "market_type": item.market_type,
        "status": item.status,
        "install_count": item.install_count,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "snapshot": snapshot,
        "config": config if isinstance(config, dict) else {},
        "trade_profile": trade_profile if isinstance(trade_profile, dict) else {},
        "indicator_profile": indicator_profile if isinstance(indicator_profile, dict) else {},
        "dsl_enabled": bool((indicator_profile or {}).get("dsl_enabled")),
    }


def serialize_user_profile(user: User) -> Dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "role": normalize_user_role(user.role),
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def run_metrics(db: Session, run: StrategyRun) -> Dict[str, Any]:
    orders = db.query(RunOrder).filter(RunOrder.run_id == run.id).all()
    snapshots = db.query(RunSnapshot).filter(RunSnapshot.run_id == run.id).order_by(RunSnapshot.id.asc()).all()

    fees = 0.0
    order_count = len(orders)
    trade_count = 0
    buy_turnover = 0.0
    sell_turnover = 0.0
    for order in orders:
        fee = abs(float(order.fee or 0.0))
        fees += fee
        filled_size = float(order.filled_size or 0.0)
        price = float(order.avg_price or order.fill_price or 0.0)
        if filled_size > 0:
            trade_count += 1
            turnover = filled_size * price
            if str(order.side or "").upper() == "SELL":
                sell_turnover += turnover
            else:
                buy_turnover += turnover

    runtime_seconds = elapsed_seconds(run.started_at, run.stopped_at)
    equities = [float(item.equity_usdt) for item in snapshots if item.equity_usdt is not None]
    peak_equity = max(equities) if equities else (float(run.current_equity_usdt or run.start_equity_usdt or 0.0) or None)
    max_drawdown_pct = 0.0
    running_peak = None
    for equity in equities:
        if running_peak is None or equity > running_peak:
            running_peak = equity
        if running_peak and running_peak > 0:
            drawdown = (equity - running_peak) / running_peak
            max_drawdown_pct = min(max_drawdown_pct, drawdown)

    start_equity = float(run.start_equity_usdt or 0.0)
    pnl_per_day = None
    if runtime_seconds > 0 and run.pnl_usdt is not None:
        pnl_per_day = float(run.pnl_usdt or 0.0) / (runtime_seconds / 86400.0)

    return {
        "order_count": order_count,
        "trade_count": trade_count,
        "fees_usdt": round(fees, 6),
        "buy_turnover_usdt": round(buy_turnover, 4),
        "sell_turnover_usdt": round(sell_turnover, 4),
        "turnover_usdt": round(buy_turnover + sell_turnover, 4),
        "cashflow_pnl_proxy_usdt": round(sell_turnover - buy_turnover - fees, 4),
        "runtime_seconds": runtime_seconds,
        "runtime_hours": round(runtime_seconds / 3600.0, 4),
        "max_drawdown_pct": round(abs(max_drawdown_pct), 6),
        "peak_equity_usdt": round(float(peak_equity), 4) if peak_equity is not None else None,
        "snapshot_count": len(snapshots),
        "pnl_per_day_usdt": round(float(pnl_per_day), 4) if pnl_per_day is not None else None,
        "start_equity_usdt": round(start_equity, 4) if start_equity else run.start_equity_usdt,
    }


def elapsed_seconds(started_at: Optional[str], stopped_at: Optional[str]) -> int:
    if not started_at:
        return 0
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(stopped_at).replace("Z", "+00:00")) if stopped_at else datetime.now(timezone.utc)
        return max(0, int((end - start).total_seconds()))
    except Exception:
        return 0


def _to_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    return max(minimum, min(maximum, parsed))
