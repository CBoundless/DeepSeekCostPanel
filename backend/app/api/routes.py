from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..core.crypto import secret_cipher
from ..core.security import hash_password, hash_token, issue_session_token, mask_secret, verify_password
from ..core.settings import RISK_PRESETS, TOKEN_TTL_HOURS, utc_now
from ..db import SessionLocal
from ..models import (
    AlertEvent,
    AuditLog,
    BacktestRun,
    Credential,
    RecoveryAction,
    RunDecision,
    RunEvent,
    RunOrder,
    SessionToken,
    Strategy,
    StrategyMarketplaceItem,
    StrategyMember,
    StrategyRun,
    StrategyVersion,
    User,
)
from ..schemas import (
    BacktestRunRequest,
    BootstrapSetupRequest,
    CredentialCreateRequest,
    CredentialUpdateRequest,
    LoginRequest,
    MarketplaceInstallRequest,
    StrategyCreateRequest,
    StrategyMemberUpsertRequest,
    StrategyPublishRequest,
    StrategyRestoreVersionRequest,
    StrategyUpdateRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from ..services.backtest import backtest_service
from ..services.control_plane import (
    MARKET_CATEGORIES,
    MARGIN_MODES,
    MARKET_TYPES,
    STRATEGY_MEMBER_ROLES,
    SUPPORTED_EXCHANGES,
    USER_ROLES,
    accessible_strategy_query,
    acknowledge_alert,
    has_global_permission,
    has_strategy_permission,
    is_admin,
    list_accessible_strategy_ids,
    load_credential_config,
    load_strategy_overrides,
    merge_strategy_payload,
    normalize_exchange,
    normalize_strategy_member_role,
    normalize_user_role,
    parse_symbols,
    record_audit,
    safe_json_loads,
    serialize_alert,
    serialize_audit_log,
    serialize_marketplace_item,
    serialize_member,
    serialize_recovery_action,
    serialize_strategy_version,
    serialize_user_profile,
    snapshot_strategy_version,
    strategy_access_role,
    strategy_indicator_profile,
    strategy_runtime_policy,
    strategy_trade_profile,
    run_metrics,
)
from ..services.realtime import realtime_hub
from ..services.trading_runtime import ACTIVE_STATUSES, runtime_manager


router = APIRouter(prefix="/api")


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def extract_token(authorization: Optional[str]) -> str:
    header = (authorization or "").strip()
    if not header:
        return ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return header


def resolve_user_from_token(db: Session, token: str) -> User:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    session = db.query(SessionToken).filter(SessionToken.token_hash == hash_token(token)).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")

    now = datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00"))
    if expires_at <= now:
        db.delete(session)
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")

    user = db.query(User).filter(User.id == session.user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")

    session.last_seen_at = utc_now()
    db.commit()
    return user


def get_current_user(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    return resolve_user_from_token(db, extract_token(authorization))


def require_global(current_user: User, permission: str) -> None:
    if not has_global_permission(current_user, permission):
        raise HTTPException(status_code=403, detail="当前角色没有该操作权限")


def get_strategy_or_404(db: Session, current_user: User, strategy_id: int, permission: str = "view") -> tuple[Strategy, str]:
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")
    access_role = strategy_access_role(db, strategy, current_user)
    if not has_strategy_permission(access_role, permission):
        raise HTTPException(status_code=403, detail="你没有该策略的访问权限")
    return strategy, access_role


def get_owned_credential(db: Session, current_user: User, credential_id: int) -> Credential:
    credential = db.query(Credential).filter(Credential.id == credential_id, Credential.user_id == current_user.id).first()
    if not credential:
        raise HTTPException(status_code=400, detail="请先选择有效的交易账号")
    return credential


@router.websocket("/ws")
async def websocket_updates(websocket: WebSocket) -> None:
    token = extract_token(websocket.query_params.get("token"))
    db = SessionLocal()
    try:
        try:
            user = resolve_user_from_token(db, token)
        except HTTPException:
            await websocket.close(code=4401)
            return
    finally:
        db.close()

    connection_id = await realtime_hub.register(user.id, websocket)
    try:
        await realtime_hub.pump(connection_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        realtime_hub.unregister(connection_id)


@router.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@router.get("/bootstrap/status")
def bootstrap_status(db: Session = Depends(get_db)) -> Dict[str, bool]:
    return {"setup_needed": db.query(User).count() == 0}


@router.post("/bootstrap/setup")
def bootstrap_setup(payload: BootstrapSetupRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if db.query(User).count() > 0:
        raise HTTPException(status_code=400, detail="系统已初始化")

    username = (payload.username or "").strip()
    password = payload.password or ""
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="用户名至少 3 个字符")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密码至少 8 位")

    password_hash, salt = hash_password(password)
    now = utc_now()
    user = User(
        username=username,
        password_hash=password_hash,
        password_salt=salt,
        role="admin",
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.flush()
    record_audit(
        db,
        user_id=user.id,
        action="bootstrap_setup",
        resource_type="user",
        resource_id=user.id,
        detail={"username": username},
    )
    db.commit()
    db.refresh(user)
    return {"ok": True, "user": serialize_user_profile(user)}


@router.post("/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    user = db.query(User).filter(User.username == (payload.username or "").strip()).first()
    if not user or not verify_password(payload.password or "", user.password_hash, user.password_salt):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    token, token_hash_value = issue_session_token()
    now = utc_now()
    expires_at = (
        (datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    session = SessionToken(
        user_id=user.id,
        token_hash=token_hash_value,
        expires_at=expires_at,
        created_at=now,
        last_seen_at=now,
    )
    db.add(session)
    record_audit(
        db,
        user_id=user.id,
        action="auth_login",
        resource_type="session",
        resource_id=user.id,
        detail={"expires_at": expires_at},
    )
    db.commit()
    publish_user(user.id, "audit_changed", {"resource": "session"})
    return {"token": token, "user": serialize_user_profile(user), "expires_at": expires_at}


@router.post("/auth/logout")
def logout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, bool]:
    token = extract_token(authorization)
    if token:
        session = db.query(SessionToken).filter(SessionToken.token_hash == hash_token(token)).first()
        if session:
            db.delete(session)
    record_audit(
        db,
        user_id=current_user.id,
        action="auth_logout",
        resource_type="session",
        resource_id=current_user.id,
        detail={},
    )
    db.commit()
    publish_user(current_user.id, "audit_changed", {"resource": "session"})
    return {"ok": True}


@router.get("/auth/me")
def me(current_user: User = Depends(get_current_user)) -> Dict[str, Any]:
    return {"user": serialize_user_profile(current_user)}


@router.get("/meta/risk-presets")
def risk_presets(current_user: User = Depends(get_current_user)) -> Dict[str, Any]:
    _ = current_user
    return {"presets": RISK_PRESETS}


@router.get("/meta/platform-capabilities")
def platform_capabilities(current_user: User = Depends(get_current_user)) -> Dict[str, Any]:
    _ = current_user
    return {
        "exchanges": SUPPORTED_EXCHANGES,
        "market_types": MARKET_TYPES,
        "margin_modes": MARGIN_MODES,
        "user_roles": USER_ROLES,
        "strategy_member_roles": STRATEGY_MEMBER_ROLES,
        "market_categories": MARKET_CATEGORIES,
    }


@router.get("/dashboard/summary")
def dashboard_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    owned_credentials = db.query(Credential).filter(Credential.user_id == current_user.id).all()
    strategy_ids = list_accessible_strategy_ids(db, current_user)
    strategies = accessible_strategy_query(db, current_user).order_by(Strategy.id.desc()).all()
    latest_runs: list[StrategyRun] = []
    total_fee = 0.0
    total_turnover = 0.0
    total_trade_count = 0
    for strategy in strategies:
        latest_run = get_latest_run(db, strategy.id)
        if latest_run:
            latest_runs.append(latest_run)
            metrics = run_metrics(db, latest_run)
            total_fee += float(metrics.get("fees_usdt") or 0.0)
            total_turnover += float(metrics.get("turnover_usdt") or 0.0)
            total_trade_count += int(metrics.get("trade_count") or 0)

    total_pnl = sum(float(run.pnl_usdt or 0.0) for run in latest_runs)
    running = sum(1 for run in latest_runs if run.status in ACTIVE_STATUSES)
    recent_runs = sorted(latest_runs, key=lambda item: item.id, reverse=True)[:6]
    open_alerts_query = db.query(AlertEvent)
    if not is_admin(current_user):
        open_alerts_query = open_alerts_query.filter(or_(AlertEvent.user_id == current_user.id, AlertEvent.strategy_id.in_(strategy_ids or [-1])))
    open_alerts = open_alerts_query.filter(AlertEvent.status == "open").count()
    total_runs = db.query(StrategyRun.id).filter(StrategyRun.strategy_id.in_(strategy_ids or [-1])).count()
    backtests = db.query(BacktestRun.id).filter(BacktestRun.strategy_id.in_(strategy_ids or [-1])).count()
    recovery_actions = db.query(RecoveryAction).filter(RecoveryAction.strategy_id.in_(strategy_ids or [-1])).count()
    shared_with_me = db.query(StrategyMember).filter(StrategyMember.user_id == current_user.id).count()
    market_publications = db.query(StrategyMarketplaceItem).filter(StrategyMarketplaceItem.publisher_user_id == current_user.id).count()

    return {
        "user": serialize_user_profile(current_user),
        "totals": {
            "credentials": len(owned_credentials),
            "strategies": len(strategies),
            "shared_with_me": shared_with_me,
            "running_strategies": running,
            "total_pnl_usdt": round(total_pnl, 4),
            "open_alerts": open_alerts,
            "run_history_count": total_runs,
            "total_runs": total_runs,
            "recovery_actions": recovery_actions,
            "fees_usdt": round(total_fee, 4),
            "turnover_usdt": round(total_turnover, 4),
            "trade_count": total_trade_count,
            "market_publications": market_publications,
            "backtests": backtests,
        },
        "recent_runs": [serialize_run(run, db) for run in recent_runs],
        "notes": [
            "第三阶段已支持多交易所画像：当前后台统一管理 OKX / Binance，并在增强回测里覆盖现货、杠杆与合约场景。",
            "实时运行当前支持 OKX 现货/杠杆与 Binance 现货；合约先通过增强回测和 DSL 规则验证，再逐步放开实时交易。",
            "策略现在可以附带自定义指标 DSL、多人协作成员、角色权限和策略市场发布信息。",
            "管理员可创建用户并分配 admin / operator / viewer 角色；策略 owner 还可以对单个策略授予 editor / operator / viewer 协作权限。",
        ],
    }


@router.get("/users")
def list_users(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    _ = current_user
    items = db.query(User).order_by(User.id.asc()).all()
    return {"items": [serialize_user_profile(item) for item in items]}


@router.post("/users")
def create_user(
    payload: UserCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    require_global(current_user, "manage_users")
    username = (payload.username or "").strip()
    password = payload.password or ""
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="用户名至少 3 个字符")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密码至少 8 位")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="用户名已存在")
    password_hash, salt = hash_password(password)
    now = utc_now()
    item = User(
        username=username,
        password_hash=password_hash,
        password_salt=salt,
        role=normalize_user_role(payload.role),
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    db.flush()
    record_audit(
        db,
        user_id=current_user.id,
        action="user_created",
        resource_type="user",
        resource_id=item.id,
        detail={"username": item.username, "role": item.role},
    )
    db.commit()
    db.refresh(item)
    return {"item": serialize_user_profile(item)}


@router.put("/users/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    require_global(current_user, "manage_users")
    item = db.query(User).filter(User.id == user_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="用户不存在")
    if payload.role is not None:
        item.role = normalize_user_role(payload.role)
    if payload.password:
        password_hash, salt = hash_password(payload.password)
        item.password_hash = password_hash
        item.password_salt = salt
    item.updated_at = utc_now()
    record_audit(
        db,
        user_id=current_user.id,
        action="user_updated",
        resource_type="user",
        resource_id=item.id,
        detail={"username": item.username, "role": item.role},
    )
    db.commit()
    db.refresh(item)
    return {"item": serialize_user_profile(item)}


@router.get("/credentials")
def list_credentials(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    items = db.query(Credential).filter(Credential.user_id == current_user.id).order_by(Credential.id.desc()).all()
    return {"items": [serialize_credential(item) for item in items]}


@router.post("/credentials")
def create_credential(
    payload: CredentialCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    require_global(current_user, "manage_credentials")
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="账号名称不能为空")
    exchange = normalize_exchange(payload.exchange)
    api_key, api_secret, api_passphrase = extract_api_payload(payload)
    now = utc_now()
    item = Credential(
        user_id=current_user.id,
        name=name,
        description=(payload.description or "").strip(),
        exchange=exchange,
        okx_api_key=api_key,
        okx_api_secret_enc=secret_cipher.encrypt(api_secret),
        okx_passphrase_enc=secret_cipher.encrypt(api_passphrase),
        deepseek_api_key_enc=secret_cipher.encrypt(payload.deepseek_api_key),
        deepseek_base_url=(payload.deepseek_base_url or "https://api.deepseek.com/v1").strip(),
        simulated_trading=bool(payload.simulated_trading),
        config_json=json.dumps(payload.config or {}, ensure_ascii=False),
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    db.flush()
    record_audit(
        db,
        user_id=current_user.id,
        action="credential_created",
        resource_type="credential",
        resource_id=item.id,
        detail={"name": item.name, "exchange": item.exchange, "simulated_trading": bool(item.simulated_trading)},
    )
    db.commit()
    db.refresh(item)
    publish_user(current_user.id, "credentials_changed", {"credential_id": item.id})
    publish_user(current_user.id, "audit_changed", {"resource": "credential"})
    return {"item": serialize_credential(item)}


@router.put("/credentials/{credential_id}")
def update_credential(
    credential_id: int,
    payload: CredentialUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    require_global(current_user, "manage_credentials")
    item = db.query(Credential).filter(Credential.id == credential_id, Credential.user_id == current_user.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="账号不存在")

    if payload.name is not None:
        item.name = payload.name.strip()
    if payload.description is not None:
        item.description = payload.description.strip()
    if payload.exchange is not None:
        item.exchange = normalize_exchange(payload.exchange)
    api_key, api_secret, api_passphrase = extract_api_payload(payload)
    if api_key:
        item.okx_api_key = api_key
    if api_secret:
        item.okx_api_secret_enc = secret_cipher.encrypt(api_secret)
    if api_passphrase:
        item.okx_passphrase_enc = secret_cipher.encrypt(api_passphrase)
    if payload.deepseek_api_key is not None and payload.deepseek_api_key.strip():
        item.deepseek_api_key_enc = secret_cipher.encrypt(payload.deepseek_api_key)
    if payload.deepseek_base_url is not None and payload.deepseek_base_url.strip():
        item.deepseek_base_url = payload.deepseek_base_url.strip()
    if payload.simulated_trading is not None:
        item.simulated_trading = bool(payload.simulated_trading)
    if payload.config is not None:
        item.config_json = json.dumps(payload.config, ensure_ascii=False)
    item.updated_at = utc_now()
    record_audit(
        db,
        user_id=current_user.id,
        action="credential_updated",
        resource_type="credential",
        resource_id=item.id,
        detail={"name": item.name, "exchange": item.exchange, "simulated_trading": bool(item.simulated_trading)},
    )
    db.commit()
    db.refresh(item)
    publish_user(current_user.id, "credentials_changed", {"credential_id": item.id})
    publish_user(current_user.id, "audit_changed", {"resource": "credential"})
    return {"item": serialize_credential(item)}


@router.delete("/credentials/{credential_id}")
def delete_credential(
    credential_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, bool]:
    require_global(current_user, "manage_credentials")
    item = db.query(Credential).filter(Credential.id == credential_id, Credential.user_id == current_user.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="账号不存在")
    using_count = db.query(Strategy).filter(Strategy.credential_id == item.id).count()
    if using_count > 0:
        raise HTTPException(status_code=409, detail="该账号仍被策略引用，请先删除或改绑相关策略")
    record_audit(
        db,
        user_id=current_user.id,
        action="credential_deleted",
        resource_type="credential",
        resource_id=item.id,
        detail={"name": item.name},
    )
    db.delete(item)
    db.commit()
    publish_user(current_user.id, "credentials_changed", {"credential_id": credential_id, "deleted": True})
    publish_user(current_user.id, "audit_changed", {"resource": "credential"})
    return {"ok": True}


@router.get("/strategies")
def list_strategies(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategies = accessible_strategy_query(db, current_user).order_by(Strategy.id.desc()).all()
    return {"items": [serialize_strategy(strategy, db, current_user) for strategy in strategies]}


@router.post("/strategies")
def create_strategy(
    payload: StrategyCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    require_global(current_user, "manage_strategies")
    credential = get_owned_credential(db, current_user, payload.credential_id)
    if not (payload.name or "").strip():
        raise HTTPException(status_code=400, detail="策略名称不能为空")
    if not (payload.symbols or "").strip():
        raise HTTPException(status_code=400, detail="策略标的不能为空")

    config = merge_strategy_payload(
        current_config={},
        incoming_config=payload.config,
        market_type=payload.market_type,
        margin_mode=payload.margin_mode,
        indicator_dsl=payload.indicator_dsl,
        entry_rule=payload.entry_rule,
        exit_rule=payload.exit_rule,
        exchange=credential.exchange,
    )

    now = utc_now()
    strategy = Strategy(
        user_id=current_user.id,
        credential_id=payload.credential_id,
        name=payload.name.strip(),
        description=(payload.description or "").strip(),
        symbols=payload.symbols.strip(),
        timeframe=(payload.timeframe or "1H").strip(),
        risk_preset=(payload.risk_preset or "medium").strip().lower(),
        leverage=max(1.0, float(payload.leverage or 1.0)),
        prompt_template=(payload.prompt_template or "").strip(),
        config_json=json.dumps(config, ensure_ascii=False),
        created_at=now,
        updated_at=now,
    )
    db.add(strategy)
    db.flush()
    snapshot_strategy_version(
        db,
        strategy,
        operator_user_id=current_user.id,
        note=payload.version_note or "创建策略",
        source="create",
        credential=credential,
    )
    record_audit(
        db,
        user_id=current_user.id,
        action="strategy_created",
        resource_type="strategy",
        resource_id=strategy.id,
        detail={
            "name": strategy.name,
            "credential_id": strategy.credential_id,
            "symbols": parse_symbols(strategy.symbols),
            "exchange": credential.exchange,
            "market_type": config.get("market_type"),
        },
    )
    db.commit()
    db.refresh(strategy)
    publish_user(current_user.id, "strategies_changed", {"strategy_id": strategy.id})
    publish_user(current_user.id, "audit_changed", {"resource": "strategy"})
    return {"item": serialize_strategy(strategy, db, current_user)}


@router.put("/strategies/{strategy_id}")
def update_strategy(
    strategy_id: int,
    payload: StrategyUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="edit")
    latest_run = get_latest_run(db, strategy.id)
    if latest_run and latest_run.status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="请先停止运行中的策略，再修改配置")

    credential = db.query(Credential).filter(Credential.id == strategy.credential_id).first()
    if payload.credential_id is not None:
        credential = get_owned_credential(db, current_user, payload.credential_id)
        strategy.credential_id = payload.credential_id
    if not credential:
        raise HTTPException(status_code=400, detail="策略绑定账号不存在")

    if payload.name is not None:
        strategy.name = payload.name.strip()
    if payload.description is not None:
        strategy.description = payload.description.strip()
    if payload.symbols is not None:
        strategy.symbols = payload.symbols.strip()
    if payload.timeframe is not None:
        strategy.timeframe = payload.timeframe.strip()
    if payload.risk_preset is not None:
        strategy.risk_preset = payload.risk_preset.strip().lower()
    if payload.leverage is not None:
        strategy.leverage = max(1.0, float(payload.leverage))
    if payload.prompt_template is not None:
        strategy.prompt_template = payload.prompt_template.strip()
    config = merge_strategy_payload(
        current_config=load_strategy_overrides(strategy),
        incoming_config=payload.config,
        market_type=payload.market_type,
        margin_mode=payload.margin_mode,
        indicator_dsl=payload.indicator_dsl,
        entry_rule=payload.entry_rule,
        exit_rule=payload.exit_rule,
        exchange=credential.exchange,
    )
    strategy.config_json = json.dumps(config, ensure_ascii=False)
    strategy.updated_at = utc_now()
    snapshot_strategy_version(
        db,
        strategy,
        operator_user_id=current_user.id,
        note=(payload.version_note or "更新策略").strip() or "更新策略",
        source="update",
        credential=credential,
    )
    record_audit(
        db,
        user_id=current_user.id,
        action="strategy_updated",
        resource_type="strategy",
        resource_id=strategy.id,
        detail={
            "name": strategy.name,
            "credential_id": strategy.credential_id,
            "symbols": parse_symbols(strategy.symbols),
            "exchange": credential.exchange,
            "market_type": config.get("market_type"),
        },
    )
    db.commit()
    db.refresh(strategy)
    publish_user(strategy.user_id, "strategies_changed", {"strategy_id": strategy.id})
    publish_user(current_user.id, "audit_changed", {"resource": "strategy"})
    return {"item": serialize_strategy(strategy, db, current_user)}


@router.delete("/strategies/{strategy_id}")
def delete_strategy(
    strategy_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, bool]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="delete")
    latest_run = get_latest_run(db, strategy.id)
    if latest_run and latest_run.status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="请先停止运行中的策略，再删除")
    record_audit(
        db,
        user_id=current_user.id,
        action="strategy_deleted",
        resource_type="strategy",
        resource_id=strategy.id,
        detail={"name": strategy.name},
    )
    db.delete(strategy)
    db.commit()
    publish_user(strategy.user_id, "strategies_changed", {"strategy_id": strategy_id, "deleted": True})
    publish_user(current_user.id, "audit_changed", {"resource": "strategy"})
    return {"ok": True}


@router.get("/strategies/{strategy_id}/versions")
def strategy_versions(
    strategy_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="view")
    items = (
        db.query(StrategyVersion)
        .filter(StrategyVersion.strategy_id == strategy.id)
        .order_by(StrategyVersion.version_no.desc())
        .all()
    )
    return {"strategy_id": strategy.id, "items": [serialize_strategy_version(item) for item in items]}


@router.post("/strategies/{strategy_id}/versions/{version_id}/restore")
def restore_strategy_version(
    strategy_id: int,
    version_id: int,
    payload: StrategyRestoreVersionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="edit")
    latest_run = get_latest_run(db, strategy.id)
    if latest_run and latest_run.status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="请先停止运行中的策略，再恢复版本")

    version = (
        db.query(StrategyVersion)
        .filter(StrategyVersion.id == version_id, StrategyVersion.strategy_id == strategy.id)
        .first()
    )
    if not version:
        raise HTTPException(status_code=404, detail="版本不存在")

    snapshot = safe_json_loads(version.snapshot_json, {})
    if not isinstance(snapshot, dict):
        raise HTTPException(status_code=400, detail="版本快照损坏")

    credential_id = int(snapshot.get("credential_id") or strategy.credential_id)
    credential = db.query(Credential).filter(Credential.id == credential_id, Credential.user_id == strategy.user_id).first()
    if not credential:
        raise HTTPException(status_code=409, detail="版本绑定的交易账号已不存在，请先恢复或改绑账号")

    strategy.name = str(snapshot.get("name") or strategy.name).strip()
    strategy.description = str(snapshot.get("description") or "").strip()
    strategy.credential_id = credential_id
    strategy.symbols = str(snapshot.get("symbols") or strategy.symbols).strip()
    strategy.timeframe = str(snapshot.get("timeframe") or strategy.timeframe).strip()
    strategy.risk_preset = str(snapshot.get("risk_preset") or strategy.risk_preset).strip().lower()
    strategy.leverage = max(1.0, float(snapshot.get("leverage") or strategy.leverage or 1.0))
    strategy.prompt_template = str(snapshot.get("prompt_template") or "").strip()
    strategy.config_json = json.dumps(snapshot.get("config") or {}, ensure_ascii=False)
    strategy.updated_at = utc_now()
    snapshot_strategy_version(
        db,
        strategy,
        operator_user_id=current_user.id,
        note=(payload.note or f"恢复自 V{version.version_no}").strip() or f"恢复自 V{version.version_no}",
        source="restore",
        credential=credential,
    )
    record_audit(
        db,
        user_id=current_user.id,
        action="strategy_version_restored",
        resource_type="strategy",
        resource_id=strategy.id,
        detail={"restored_version_id": version.id, "restored_version_no": version.version_no},
    )
    db.commit()
    db.refresh(strategy)
    publish_user(strategy.user_id, "strategies_changed", {"strategy_id": strategy.id, "restored_version_id": version.id})
    publish_user(current_user.id, "audit_changed", {"resource": "strategy"})
    return {"item": serialize_strategy(strategy, db, current_user)}


@router.post("/strategies/{strategy_id}/start")
def start_strategy(
    strategy_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="execute")
    try:
        run = runtime_manager.start_strategy(strategy.id, strategy.user_id, actor_user_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": run}


@router.post("/strategies/{strategy_id}/stop")
def stop_strategy(
    strategy_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="execute")
    try:
        run = runtime_manager.stop_strategy(strategy.id, strategy.user_id, actor_user_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": run}


@router.get("/strategies/{strategy_id}/runs")
def strategy_run_history(
    strategy_id: int,
    limit: int = Query(default=30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="view")
    items = (
        db.query(StrategyRun)
        .filter(StrategyRun.strategy_id == strategy.id)
        .order_by(StrategyRun.id.desc())
        .limit(limit)
        .all()
    )
    serialized = [serialize_run(item, db) for item in items]
    total_pnl = sum(float(item.get("pnl_usdt") or 0.0) for item in serialized)
    total_fees = sum(float((item.get("stats") or {}).get("fees_usdt") or 0.0) for item in serialized)
    return {
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "summary": {
            "run_count": len(serialized),
            "error_runs": sum(1 for item in serialized if item.get("status") == "error"),
            "stopped_runs": sum(1 for item in serialized if item.get("status") == "stopped"),
            "total_pnl_usdt": round(total_pnl, 4),
            "total_fees_usdt": round(total_fees, 4),
        },
        "items": serialized,
    }


@router.get("/strategies/{strategy_id}/logs")
def strategy_logs(
    strategy_id: int,
    run_id: Optional[int] = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="view")
    target_run = resolve_strategy_run(db, strategy, run_id)
    if not target_run:
        return {"strategy_id": strategy.id, "strategy_name": strategy.name, "run": None, "events": []}

    events = (
        db.query(RunEvent)
        .filter(RunEvent.run_id == target_run.id)
        .order_by(RunEvent.id.desc())
        .limit(300)
        .all()
    )
    ordered_events = list(reversed(events))
    return {
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "run": serialize_run(target_run, db),
        "events": [
            {"id": item.id, "level": item.level, "message": item.message, "created_at": item.created_at}
            for item in ordered_events
        ],
    }


@router.get("/strategies/{strategy_id}/orders")
def strategy_orders(
    strategy_id: int,
    run_id: Optional[int] = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="view")
    target_run = resolve_strategy_run(db, strategy, run_id)
    if not target_run:
        return {"strategy_id": strategy.id, "strategy_name": strategy.name, "run": None, "orders": [], "trades": []}

    orders = (
        db.query(RunOrder)
        .filter(RunOrder.run_id == target_run.id)
        .order_by(RunOrder.id.desc())
        .limit(200)
        .all()
    )
    ordered = list(reversed(orders))
    serialized = [serialize_order(item) for item in ordered]
    trades = [item for item in serialized if item.get("filled_size") and item.get("filled_size", 0) > 0]
    return {
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "run": serialize_run(target_run, db),
        "orders": serialized,
        "trades": trades,
    }


@router.get("/strategies/{strategy_id}/decisions")
def strategy_decisions(
    strategy_id: int,
    run_id: Optional[int] = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="view")
    target_run = resolve_strategy_run(db, strategy, run_id)
    if not target_run:
        return {"strategy_id": strategy.id, "strategy_name": strategy.name, "run": None, "items": []}
    items = (
        db.query(RunDecision)
        .filter(RunDecision.run_id == target_run.id)
        .order_by(RunDecision.id.desc())
        .limit(300)
        .all()
    )
    ordered = list(reversed(items))
    return {
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "run": serialize_run(target_run, db),
        "items": [serialize_decision(item) for item in ordered],
    }


@router.get("/strategies/{strategy_id}/members")
def list_strategy_members(
    strategy_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="view")
    items = db.query(StrategyMember).filter(StrategyMember.strategy_id == strategy.id).order_by(StrategyMember.id.asc()).all()
    users = {item.id: item for item in db.query(User).filter(User.id.in_([member.user_id for member in items] or [-1])).all()}
    owner = db.query(User).filter(User.id == strategy.user_id).first()
    return {
        "owner": serialize_user_profile(owner) if owner else None,
        "items": [serialize_member(item, users.get(item.user_id)) for item in items],
    }


@router.post("/strategies/{strategy_id}/members")
def upsert_strategy_member(
    strategy_id: int,
    payload: StrategyMemberUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="manage_collaboration")
    if payload.user_id == strategy.user_id:
        raise HTTPException(status_code=400, detail="策略 owner 已默认拥有全部权限")
    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="协作用户不存在")
    role = normalize_strategy_member_role(payload.role)
    item = db.query(StrategyMember).filter(StrategyMember.strategy_id == strategy.id, StrategyMember.user_id == payload.user_id).first()
    now = utc_now()
    if item:
        item.role = role
        item.updated_at = now
        action = "strategy_member_updated"
    else:
        item = StrategyMember(
            strategy_id=strategy.id,
            user_id=payload.user_id,
            role=role,
            created_by_user_id=current_user.id,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        action = "strategy_member_added"
    record_audit(
        db,
        user_id=current_user.id,
        action=action,
        resource_type="strategy",
        resource_id=strategy.id,
        detail={"member_user_id": payload.user_id, "member_username": user.username, "member_role": role},
    )
    db.commit()
    db.refresh(item)
    publish_user(strategy.user_id, "strategies_changed", {"strategy_id": strategy.id})
    publish_user(payload.user_id, "strategies_changed", {"strategy_id": strategy.id})
    publish_user(current_user.id, "audit_changed", {"resource": "strategy"})
    return {"item": serialize_member(item, user)}


@router.delete("/strategies/{strategy_id}/members/{member_user_id}")
def delete_strategy_member(
    strategy_id: int,
    member_user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, bool]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="manage_collaboration")
    item = db.query(StrategyMember).filter(StrategyMember.strategy_id == strategy.id, StrategyMember.user_id == member_user_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="协作成员不存在")
    record_audit(
        db,
        user_id=current_user.id,
        action="strategy_member_removed",
        resource_type="strategy",
        resource_id=strategy.id,
        detail={"member_user_id": member_user_id},
    )
    db.delete(item)
    db.commit()
    publish_user(strategy.user_id, "strategies_changed", {"strategy_id": strategy.id})
    publish_user(member_user_id, "strategies_changed", {"strategy_id": strategy.id})
    publish_user(current_user.id, "audit_changed", {"resource": "strategy"})
    return {"ok": True}


@router.post("/strategies/{strategy_id}/market/publish")
def publish_strategy_marketplace(
    strategy_id: int,
    payload: StrategyPublishRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="publish_market")
    version_query = db.query(StrategyVersion).filter(StrategyVersion.strategy_id == strategy.id)
    if payload.version_id is not None:
        version_query = version_query.filter(StrategyVersion.id == payload.version_id)
    version = version_query.order_by(StrategyVersion.version_no.desc()).first()
    if not version:
        raise HTTPException(status_code=404, detail="待发布的版本不存在")
    snapshot = safe_json_loads(version.snapshot_json, {})
    if not isinstance(snapshot, dict):
        raise HTTPException(status_code=400, detail="版本快照损坏")

    trade_profile = snapshot.get("trade_profile") or {}
    now = utc_now()
    item = StrategyMarketplaceItem(
        strategy_id=strategy.id,
        version_id=version.id,
        publisher_user_id=current_user.id,
        title=(payload.title or strategy.name).strip() or strategy.name,
        summary=(payload.summary or strategy.description or "").strip(),
        description=(payload.description or strategy.description or "").strip(),
        category=(payload.category or "community").strip() or "community",
        tags_json=json.dumps(payload.tags or [], ensure_ascii=False),
        snapshot_json=json.dumps(snapshot, ensure_ascii=False),
        exchange=str(trade_profile.get("exchange") or strategy_trade_profile(strategy, db.query(Credential).filter(Credential.id == strategy.credential_id).first()).get("exchange")),
        market_type=str(trade_profile.get("market_type") or "spot"),
        status="published",
        install_count=0,
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    db.flush()
    record_audit(
        db,
        user_id=current_user.id,
        action="strategy_market_published",
        resource_type="marketplace",
        resource_id=item.id,
        detail={"strategy_id": strategy.id, "version_id": version.id, "title": item.title},
    )
    db.commit()
    db.refresh(item)
    publish_user(current_user.id, "audit_changed", {"resource": "marketplace"})
    return {"item": serialize_marketplace_item(item, current_user)}


@router.get("/strategy-marketplace")
def list_strategy_marketplace(
    category: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    limit: int = Query(default=60, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    _ = current_user
    query = db.query(StrategyMarketplaceItem).filter(StrategyMarketplaceItem.status == "published")
    if category:
        query = query.filter(StrategyMarketplaceItem.category == category)
    if keyword:
        like = f"%{keyword.strip()}%"
        query = query.filter(or_(StrategyMarketplaceItem.title.like(like), StrategyMarketplaceItem.summary.like(like), StrategyMarketplaceItem.description.like(like)))
    items = query.order_by(StrategyMarketplaceItem.id.desc()).limit(limit).all()
    user_ids = [item.publisher_user_id for item in items]
    users = {item.id: item for item in db.query(User).filter(User.id.in_(user_ids or [-1])).all()}
    return {"items": [serialize_marketplace_item(item, users.get(item.publisher_user_id)) for item in items]}


@router.get("/strategy-marketplace/{marketplace_id}")
def get_strategy_marketplace_item(
    marketplace_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    _ = current_user
    item = db.query(StrategyMarketplaceItem).filter(StrategyMarketplaceItem.id == marketplace_id, StrategyMarketplaceItem.status == "published").first()
    if not item:
        raise HTTPException(status_code=404, detail="策略市场条目不存在")
    publisher = db.query(User).filter(User.id == item.publisher_user_id).first()
    return {"item": serialize_marketplace_item(item, publisher)}


@router.post("/strategy-marketplace/{marketplace_id}/install")
def install_strategy_marketplace_item(
    marketplace_id: int,
    payload: MarketplaceInstallRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    require_global(current_user, "manage_strategies")
    item = db.query(StrategyMarketplaceItem).filter(StrategyMarketplaceItem.id == marketplace_id, StrategyMarketplaceItem.status == "published").first()
    if not item:
        raise HTTPException(status_code=404, detail="策略市场条目不存在")
    credential = get_owned_credential(db, current_user, payload.credential_id)
    if credential.exchange != item.exchange:
        raise HTTPException(status_code=409, detail=f"当前条目需要绑定 {item.exchange.upper()} 账号")

    snapshot = safe_json_loads(item.snapshot_json, {})
    if not isinstance(snapshot, dict):
        raise HTTPException(status_code=400, detail="市场条目快照损坏")
    config = dict(snapshot.get("config") or {})
    config["exchange"] = credential.exchange
    now = utc_now()
    strategy = Strategy(
        user_id=current_user.id,
        credential_id=credential.id,
        name=(payload.name or snapshot.get("name") or item.title or "导入策略").strip(),
        description=str(snapshot.get("description") or item.summary or "").strip(),
        symbols=str(snapshot.get("symbols") or "BTC-USDT"),
        timeframe=str(snapshot.get("timeframe") or "1H"),
        risk_preset=str(snapshot.get("risk_preset") or "medium"),
        leverage=max(1.0, float(snapshot.get("leverage") or 1.0)),
        prompt_template=str(snapshot.get("prompt_template") or ""),
        config_json=json.dumps(config, ensure_ascii=False),
        created_at=now,
        updated_at=now,
    )
    db.add(strategy)
    db.flush()
    snapshot_strategy_version(
        db,
        strategy,
        operator_user_id=current_user.id,
        note=payload.version_note or "从策略市场导入",
        source="install",
        credential=credential,
    )
    item.install_count = int(item.install_count or 0) + 1
    item.updated_at = now
    record_audit(
        db,
        user_id=current_user.id,
        action="strategy_market_installed",
        resource_type="marketplace",
        resource_id=item.id,
        detail={"strategy_id": strategy.id, "source_marketplace_id": item.id},
    )
    db.commit()
    db.refresh(strategy)
    publish_user(current_user.id, "strategies_changed", {"strategy_id": strategy.id})
    publish_user(current_user.id, "audit_changed", {"resource": "marketplace"})
    return {"item": serialize_strategy(strategy, db, current_user)}


@router.get("/alerts")
def list_alerts(
    status_filter: str = Query(default="open"),
    limit: int = Query(default=60, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    query = db.query(AlertEvent)
    if not is_admin(current_user):
        strategy_ids = list_accessible_strategy_ids(db, current_user)
        query = query.filter(or_(AlertEvent.user_id == current_user.id, AlertEvent.strategy_id.in_(strategy_ids or [-1])))
    if status_filter != "all":
        query = query.filter(AlertEvent.status == status_filter)
    items = query.order_by(AlertEvent.id.desc()).limit(limit).all()
    return {"items": [serialize_alert(item) for item in items]}


@router.post("/alerts/{alert_id}/ack")
def ack_alert(
    alert_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    alert = db.query(AlertEvent).filter(AlertEvent.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="告警不存在")
    if not is_admin(current_user):
        if alert.strategy_id:
            strategy, _ = get_strategy_or_404(db, current_user, alert.strategy_id, permission="view")
            _ = strategy
        elif alert.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="你没有该告警的访问权限")
    acknowledge_alert(db, alert=alert, user_id=current_user.id)
    record_audit(
        db,
        user_id=current_user.id,
        action="alert_acknowledged",
        resource_type="alert",
        resource_id=alert.id,
        detail={"strategy_id": alert.strategy_id, "run_id": alert.run_id},
    )
    db.commit()
    publish_user(current_user.id, "audit_changed", {"resource": "alert"})
    return {"item": serialize_alert(alert)}


@router.get("/audit-logs")
def list_audit_logs(
    limit: int = Query(default=80, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    query = db.query(AuditLog)
    if not is_admin(current_user):
        query = query.filter(AuditLog.user_id == current_user.id)
    items = query.order_by(AuditLog.id.desc()).limit(limit).all()
    return {"items": [serialize_audit_log(item) for item in items]}


@router.get("/recovery-actions")
def list_recovery_actions(
    strategy_id: Optional[int] = Query(default=None),
    limit: int = Query(default=60, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    query = db.query(RecoveryAction)
    if not is_admin(current_user):
        query = query.filter(RecoveryAction.strategy_id.in_(list_accessible_strategy_ids(db, current_user) or [-1]))
    if strategy_id is not None:
        query = query.filter(RecoveryAction.strategy_id == strategy_id)
    items = query.order_by(RecoveryAction.id.desc()).limit(limit).all()
    return {"items": [serialize_recovery_action(item) for item in items]}


@router.get("/backtests")
def list_backtests(
    strategy_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    if strategy_id is not None:
        strategy, _ = get_strategy_or_404(db, current_user, strategy_id, permission="view")
        ids = [strategy.id]
    else:
        ids = list_accessible_strategy_ids(db, current_user)
    return {"items": backtest_service.list_backtests(ids, strategy_id=strategy_id)}


@router.post("/backtests/run")
def run_backtest(
    payload: BacktestRunRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    strategy, _ = get_strategy_or_404(db, current_user, payload.strategy_id, permission="backtest")
    credential = db.query(Credential).filter(Credential.id == strategy.credential_id).first()
    if not credential:
        raise HTTPException(status_code=400, detail="策略绑定账号不存在")
    try:
        item = backtest_service.run_backtest(
            strategy=strategy,
            credential=credential,
            symbol=payload.symbol,
            timeframe=payload.timeframe,
            bars=payload.bars,
            initial_capital_usdt=payload.initial_capital_usdt,
            engine=payload.engine,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        user_id=current_user.id,
        action="backtest_run",
        resource_type="strategy",
        resource_id=payload.strategy_id,
        detail={"backtest_id": item["id"], "symbol": item["inst_id"], "timeframe": item["timeframe"], "engine": item.get("summary", {}).get("engine")},
    )
    db.commit()
    publish_user(strategy.user_id, "backtests_changed", {"backtest_id": item["id"], "strategy_id": item["strategy_id"]})
    publish_user(current_user.id, "audit_changed", {"resource": "backtest"})
    return {"item": item}


@router.get("/backtests/{backtest_id}")
def get_backtest(
    backtest_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    try:
        item = backtest_service.get_backtest(backtest_id, list_accessible_strategy_ids(db, current_user))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"item": item}


def resolve_strategy_run(db: Session, strategy: Strategy, run_id: Optional[int]) -> Optional[StrategyRun]:
    if run_id is not None:
        return (
            db.query(StrategyRun)
            .filter(StrategyRun.id == run_id, StrategyRun.strategy_id == strategy.id)
            .first()
        )
    return get_latest_run(db, strategy.id)


def serialize_credential(item: Credential) -> Dict[str, Any]:
    config = load_credential_config(item)
    masked_key = mask_secret(item.okx_api_key)
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "exchange": item.exchange,
        "exchange_label": SUPPORTED_EXCHANGES.get(item.exchange, {}).get("label", item.exchange.upper()),
        "deepseek_base_url": item.deepseek_base_url,
        "simulated_trading": bool(item.simulated_trading),
        "config": config,
        "masked_api_key": masked_key,
        "masked_okx_api_key": masked_key,
        "has_api_secret": bool(item.okx_api_secret_enc),
        "has_api_passphrase": bool(item.okx_passphrase_enc),
        "has_okx_secret": bool(item.okx_api_secret_enc),
        "has_okx_passphrase": bool(item.okx_passphrase_enc),
        "has_deepseek_api_key": bool(item.deepseek_api_key_enc),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def get_latest_run(db: Session, strategy_id: int) -> Optional[StrategyRun]:
    return db.query(StrategyRun).filter(StrategyRun.strategy_id == strategy_id).order_by(StrategyRun.id.desc()).first()


def serialize_run(run: StrategyRun, db: Session) -> Dict[str, Any]:
    strategy = db.query(Strategy).filter(Strategy.id == run.strategy_id).first()
    credential = db.query(Credential).filter(Credential.id == run.credential_id).first() if strategy else None
    policy = strategy_runtime_policy(strategy, credential) if strategy else {}
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
        "policy": policy,
        "stats": run_metrics(db, run),
    }


def serialize_order(item: RunOrder) -> Dict[str, Any]:
    raw = safe_json_loads(item.raw_json, {})
    return {
        "id": item.id,
        "inst_id": item.inst_id,
        "side": item.side,
        "purpose": item.purpose,
        "ord_id": item.ord_id,
        "cl_ord_id": item.cl_ord_id,
        "state": item.state,
        "ord_type": item.ord_type,
        "requested_quote": item.requested_quote,
        "requested_size": item.requested_size,
        "filled_size": item.filled_size,
        "avg_price": item.avg_price,
        "fill_price": item.fill_price,
        "fee": item.fee,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "raw": raw,
    }


def serialize_decision(item: RunDecision) -> Dict[str, Any]:
    return {
        "id": item.id,
        "inst_id": item.inst_id,
        "action": item.action,
        "reason": item.reason,
        "confidence": item.confidence,
        "signal_quality": item.signal_quality,
        "market_quality": item.market_quality,
        "position_factor": item.position_factor,
        "planned_quote": item.planned_quote,
        "created_at": item.created_at,
    }


def serialize_strategy(strategy: Strategy, db: Session, current_user: User) -> Dict[str, Any]:
    latest_run = get_latest_run(db, strategy.id)
    credential = db.query(Credential).filter(Credential.id == strategy.credential_id).first()
    latest_backtest = db.query(BacktestRun.id).filter(BacktestRun.strategy_id == strategy.id).order_by(BacktestRun.id.desc()).first()
    version_count = db.query(StrategyVersion).filter(StrategyVersion.strategy_id == strategy.id).count()
    runtime_policy = strategy_runtime_policy(strategy, credential)
    trade_profile = strategy_trade_profile(strategy, credential)
    indicator_profile = strategy_indicator_profile(strategy)
    config = load_strategy_overrides(strategy)
    access_role = strategy_access_role(db, strategy, current_user)
    owner = db.query(User).filter(User.id == strategy.user_id).first()
    collaborator_count = db.query(StrategyMember).filter(StrategyMember.strategy_id == strategy.id).count()
    return {
        "id": strategy.id,
        "name": strategy.name,
        "description": strategy.description,
        "credential_id": strategy.credential_id,
        "credential_name": credential.name if credential else "",
        "owner_user_id": strategy.user_id,
        "owner_username": owner.username if owner else None,
        "symbols": strategy.symbols,
        "symbols_list": parse_symbols(strategy.symbols),
        "timeframe": strategy.timeframe,
        "risk_preset": strategy.risk_preset,
        "leverage": strategy.leverage,
        "prompt_template": strategy.prompt_template,
        "config": config,
        "runtime_policy": runtime_policy,
        "trade_profile": trade_profile,
        "indicator_profile": indicator_profile,
        "version_count": version_count,
        "collaborator_count": collaborator_count,
        "access_role": access_role,
        "created_at": strategy.created_at,
        "updated_at": strategy.updated_at,
        "latest_run": serialize_run(latest_run, db) if latest_run else None,
        "is_running": bool(latest_run and latest_run.status in ACTIVE_STATUSES),
        "latest_backtest_id": latest_backtest[0] if latest_backtest else None,
    }


def publish_user(user_id: int, message_type: str, payload: Dict[str, Any]) -> None:
    realtime_hub.publish_user(user_id, message_type, payload)


def extract_api_payload(payload: Any) -> tuple[str, str, str]:
    api_key = (getattr(payload, "api_key", None) or getattr(payload, "okx_api_key", None) or "").strip()
    api_secret = (getattr(payload, "api_secret", None) or getattr(payload, "okx_api_secret", None) or "").strip()
    api_passphrase = (getattr(payload, "api_passphrase", None) or getattr(payload, "okx_passphrase", None) or "").strip()
    return api_key, api_secret, api_passphrase
