from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class BootstrapSetupRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class CredentialCreateRequest(BaseModel):
    name: str
    description: str = ""
    exchange: str = "okx"
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_passphrase: str = ""
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    simulated_trading: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


class CredentialUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    exchange: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    okx_api_key: Optional[str] = None
    okx_api_secret: Optional[str] = None
    okx_passphrase: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    simulated_trading: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None


class StrategyCreateRequest(BaseModel):
    name: str
    description: str = ""
    credential_id: int
    symbols: str
    timeframe: str = "1H"
    risk_preset: str = "medium"
    leverage: float = 1.0
    prompt_template: str = ""
    market_type: str = "spot"
    margin_mode: str = "cash"
    indicator_dsl: str = ""
    entry_rule: str = ""
    exit_rule: str = ""
    config: Dict[str, Any] = Field(default_factory=dict)
    version_note: str = "创建策略"


class StrategyUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    credential_id: Optional[int] = None
    symbols: Optional[str] = None
    timeframe: Optional[str] = None
    risk_preset: Optional[str] = None
    leverage: Optional[float] = None
    prompt_template: Optional[str] = None
    market_type: Optional[str] = None
    margin_mode: Optional[str] = None
    indicator_dsl: Optional[str] = None
    entry_rule: Optional[str] = None
    exit_rule: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    version_note: Optional[str] = None


class StrategyRestoreVersionRequest(BaseModel):
    note: Optional[str] = None


class BacktestRunRequest(BaseModel):
    strategy_id: int
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    bars: int = 240
    initial_capital_usdt: float = 1000.0
    engine: Optional[str] = None


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class UserUpdateRequest(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None


class StrategyMemberUpsertRequest(BaseModel):
    user_id: int
    role: str = "viewer"


class StrategyPublishRequest(BaseModel):
    version_id: Optional[int] = None
    title: str
    summary: str = ""
    description: str = ""
    category: str = "community"
    tags: List[str] = Field(default_factory=list)


class MarketplaceInstallRequest(BaseModel):
    credential_id: int
    name: Optional[str] = None
    version_note: str = "从策略市场导入"
