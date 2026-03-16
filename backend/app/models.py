from __future__ import annotations

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    password_salt = Column(String(64), nullable=False)
    role = Column(String(32), nullable=False, default="admin")
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)

    sessions = relationship("SessionToken", back_populates="user", cascade="all, delete-orphan")
    credentials = relationship("Credential", back_populates="user", cascade="all, delete-orphan")
    strategies = relationship("Strategy", back_populates="user", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="user")
    shared_strategy_memberships = relationship(
        "StrategyMember",
        foreign_keys="StrategyMember.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    published_market_items = relationship("StrategyMarketplaceItem", back_populates="publisher")


class SessionToken(Base):
    __tablename__ = "session_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(128), unique=True, nullable=False, index=True)
    expires_at = Column(String(32), nullable=False)
    created_at = Column(String(32), nullable=False)
    last_seen_at = Column(String(32), nullable=False)

    user = relationship("User", back_populates="sessions")


class Credential(Base):
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    exchange = Column(String(32), nullable=False, default="okx")
    okx_api_key = Column(String(255), nullable=False, default="")
    okx_api_secret_enc = Column(Text, nullable=False, default="")
    okx_passphrase_enc = Column(Text, nullable=False, default="")
    deepseek_api_key_enc = Column(Text, nullable=False, default="")
    deepseek_base_url = Column(String(255), nullable=False, default="https://api.deepseek.com/v1")
    simulated_trading = Column(Boolean, nullable=False, default=True)
    config_json = Column(Text, nullable=False, default="{}")
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)

    user = relationship("User", back_populates="credentials")
    strategies = relationship("Strategy", back_populates="credential")


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    credential_id = Column(Integer, ForeignKey("credentials.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    symbols = Column(Text, nullable=False)
    timeframe = Column(String(32), nullable=False, default="1H")
    risk_preset = Column(String(32), nullable=False, default="medium")
    leverage = Column(Float, nullable=False, default=1.0)
    prompt_template = Column(Text, nullable=False, default="")
    config_json = Column(Text, nullable=False, default="{}")
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)

    user = relationship("User", back_populates="strategies")
    credential = relationship("Credential", back_populates="strategies")
    runs = relationship("StrategyRun", back_populates="strategy", cascade="all, delete-orphan")
    backtests = relationship("BacktestRun", back_populates="strategy", cascade="all, delete-orphan")
    versions = relationship("StrategyVersion", back_populates="strategy", cascade="all, delete-orphan")
    alerts = relationship("AlertEvent", back_populates="strategy")
    recovery_actions = relationship("RecoveryAction", back_populates="strategy", cascade="all, delete-orphan")
    members = relationship("StrategyMember", back_populates="strategy", cascade="all, delete-orphan")
    marketplace_items = relationship("StrategyMarketplaceItem", back_populates="strategy")


class StrategyMember(Base):
    __tablename__ = "strategy_members"
    __table_args__ = (UniqueConstraint("strategy_id", "user_id", name="uq_strategy_member"),)

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(32), nullable=False, default="viewer")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)

    strategy = relationship("Strategy", back_populates="members")
    user = relationship("User", foreign_keys=[user_id], back_populates="shared_strategy_memberships")


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    credential_id = Column(Integer, ForeignKey("credentials.id"), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="stopped")
    started_at = Column(String(32), nullable=False)
    stopped_at = Column(String(32), nullable=True)
    last_heartbeat_at = Column(String(32), nullable=True)
    stop_reason = Column(String(255), nullable=True)
    last_error = Column(Text, nullable=True)
    start_equity_usdt = Column(Float, nullable=True)
    current_equity_usdt = Column(Float, nullable=True)
    available_usdt = Column(Float, nullable=True)
    exposure_ratio = Column(Float, nullable=True)
    pnl_usdt = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    decision_count = Column(Integer, nullable=False, default=0)
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)

    strategy = relationship("Strategy", back_populates="runs")
    events = relationship("RunEvent", back_populates="run", cascade="all, delete-orphan")
    decisions = relationship("RunDecision", back_populates="run", cascade="all, delete-orphan")
    orders = relationship("RunOrder", back_populates="run", cascade="all, delete-orphan")
    snapshots = relationship("RunSnapshot", back_populates="run", cascade="all, delete-orphan")
    alerts = relationship("AlertEvent", back_populates="run")


class RunEvent(Base):
    __tablename__ = "run_events"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("strategy_runs.id"), nullable=False, index=True)
    level = Column(String(16), nullable=False, default="info")
    message = Column(Text, nullable=False)
    created_at = Column(String(32), nullable=False)

    run = relationship("StrategyRun", back_populates="events")


class RunDecision(Base):
    __tablename__ = "run_decisions"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("strategy_runs.id"), nullable=False, index=True)
    inst_id = Column(String(64), nullable=False, index=True)
    action = Column(String(32), nullable=False)
    reason = Column(Text, nullable=False, default="")
    confidence = Column(Integer, nullable=False, default=0)
    signal_quality = Column(Float, nullable=False, default=0.0)
    market_quality = Column(Float, nullable=False, default=0.0)
    position_factor = Column(Float, nullable=False, default=0.0)
    planned_quote = Column(Float, nullable=False, default=0.0)
    created_at = Column(String(32), nullable=False)

    run = relationship("StrategyRun", back_populates="decisions")


class RunOrder(Base):
    __tablename__ = "run_orders"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("strategy_runs.id"), nullable=False, index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False, index=True)
    inst_id = Column(String(64), nullable=False, index=True)
    side = Column(String(16), nullable=False)
    purpose = Column(String(32), nullable=False, default="trade")
    ord_id = Column(String(64), nullable=True, index=True)
    cl_ord_id = Column(String(64), nullable=True, index=True)
    state = Column(String(32), nullable=False, default="unknown")
    ord_type = Column(String(32), nullable=True)
    requested_quote = Column(Float, nullable=True)
    requested_size = Column(Float, nullable=True)
    filled_size = Column(Float, nullable=True)
    avg_price = Column(Float, nullable=True)
    fill_price = Column(Float, nullable=True)
    fee = Column(Float, nullable=True)
    raw_json = Column(Text, nullable=False, default="{}")
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)

    run = relationship("StrategyRun", back_populates="orders")


class RunSnapshot(Base):
    __tablename__ = "run_snapshots"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("strategy_runs.id"), nullable=False, index=True)
    equity_usdt = Column(Float, nullable=True)
    available_usdt = Column(Float, nullable=True)
    exposure_ratio = Column(Float, nullable=True)
    pnl_usdt = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    created_at = Column(String(32), nullable=False)

    run = relationship("StrategyRun", back_populates="snapshots")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    credential_id = Column(Integer, ForeignKey("credentials.id"), nullable=False, index=True)
    strategy_name = Column(String(128), nullable=False)
    inst_id = Column(String(64), nullable=False, index=True)
    timeframe = Column(String(32), nullable=False)
    bar_count = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="completed")
    summary_json = Column(Text, nullable=False, default="{}")
    equity_curve_json = Column(Text, nullable=False, default="[]")
    trades_json = Column(Text, nullable=False, default="[]")
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)

    strategy = relationship("Strategy", back_populates="backtests")


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    version_no = Column(Integer, nullable=False)
    source = Column(String(32), nullable=False, default="update")
    note = Column(String(255), nullable=False, default="")
    snapshot_json = Column(Text, nullable=False, default="{}")
    created_at = Column(String(32), nullable=False)

    strategy = relationship("Strategy", back_populates="versions")


class StrategyMarketplaceItem(Base):
    __tablename__ = "strategy_marketplace_items"

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False, index=True)
    version_id = Column(Integer, ForeignKey("strategy_versions.id"), nullable=False, index=True)
    publisher_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(160), nullable=False)
    summary = Column(String(255), nullable=False, default="")
    description = Column(Text, nullable=False, default="")
    category = Column(String(64), nullable=False, default="community")
    tags_json = Column(Text, nullable=False, default="[]")
    snapshot_json = Column(Text, nullable=False, default="{}")
    exchange = Column(String(32), nullable=False, default="okx")
    market_type = Column(String(32), nullable=False, default="spot")
    status = Column(String(16), nullable=False, default="published")
    install_count = Column(Integer, nullable=False, default=0)
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)

    strategy = relationship("Strategy", back_populates="marketplace_items")
    publisher = relationship("User", back_populates="published_market_items")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(64), nullable=False, index=True)
    resource_type = Column(String(64), nullable=False, default="system")
    resource_id = Column(String(64), nullable=True)
    detail_json = Column(Text, nullable=False, default="{}")
    created_at = Column(String(32), nullable=False)

    user = relationship("User", back_populates="audit_logs")


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=True, index=True)
    run_id = Column(Integer, ForeignKey("strategy_runs.id"), nullable=True, index=True)
    severity = Column(String(16), nullable=False, default="warning")
    category = Column(String(32), nullable=False, default="runtime")
    source = Column(String(32), nullable=False, default="runtime")
    title = Column(String(128), nullable=False)
    message = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="open")
    detail_json = Column(Text, nullable=False, default="{}")
    acknowledged_at = Column(String(32), nullable=True)
    acknowledged_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)

    strategy = relationship("Strategy", back_populates="alerts")
    run = relationship("StrategyRun", back_populates="alerts")


class RecoveryAction(Base):
    __tablename__ = "recovery_actions"

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    failed_run_id = Column(Integer, ForeignKey("strategy_runs.id"), nullable=True, index=True)
    recovered_run_id = Column(Integer, ForeignKey("strategy_runs.id"), nullable=True, index=True)
    attempt_no = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="scheduled")
    reason = Column(String(64), nullable=False, default="runtime_error")
    message = Column(Text, nullable=False, default="")
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)

    strategy = relationship("Strategy", back_populates="recovery_actions")
