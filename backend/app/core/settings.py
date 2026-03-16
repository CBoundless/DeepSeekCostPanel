from __future__ import annotations

import base64
import copy
import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote_plus

from cryptography.fernet import Fernet


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = PROJECT_ROOT / "backend"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DATA_DIR = BACKEND_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

MASTER_KEY_FILE = DATA_DIR / ".master.key"
TOKEN_TTL_HOURS = max(1, int(float(os.environ.get("ADMIN_PANEL_TOKEN_TTL_HOURS") or 24)))
RUN_POLL_INTERVAL_SECONDS = max(3, int(float(os.environ.get("ADMIN_PANEL_RUN_POLL_INTERVAL_SECONDS") or 5)))


def build_database_url() -> str:
    explicit = (os.environ.get("ADMIN_PANEL_DATABASE_URL") or "").strip()
    if explicit:
        return explicit

    pg_user = (os.environ.get("ADMIN_PANEL_PG_USER") or os.environ.get("USER") or "postgres").strip()
    user = quote_plus(pg_user)
    password = os.environ.get("ADMIN_PANEL_PG_PASSWORD") or ""
    password_part = f":{quote_plus(password)}" if password else ""
    host = (os.environ.get("ADMIN_PANEL_PG_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("ADMIN_PANEL_PG_PORT") or "5432").strip()
    database = (os.environ.get("ADMIN_PANEL_PG_DATABASE") or "deepseek_cost_panel").strip()
    return f"postgresql+psycopg://{user}{password_part}@{host}:{port}/{database}"


DATABASE_URL = build_database_url()


RISK_PRESETS: Dict[str, Dict[str, Any]] = {
    "low": {
        "trade_quote": 15.0,
        "conf_threshold": 82,
        "loop_seconds": 300,
        "stop_loss_pct": 0.025,
        "trailing_stop_pct": 0.015,
        "trailing_activate_pct": 0.04,
        "estimated_round_trip_cost_pct": 0.003,
        "entry_cost_buffer_pct": 0.0015,
        "min_net_profit_pct": 0.012,
        "dynamic_position_enabled": True,
        "market_quality_threshold": 0.72,
        "dynamic_min_factor": 0.6,
        "dynamic_max_factor": 1.0,
        "max_total_exposure_ratio": 0.35,
        "max_single_asset_weight": 0.18,
        "max_order_cash_ratio": 0.12,
        "min_cash_reserve_ratio": 0.25,
        "analyzer_cache_ttl_minutes": 30,
        "analyzer_min_signal_quality": 0.62,
        "analyzer_min_interval_minutes": 20,
        "analyzer_max_output_tokens": 220,
        "analyzer_temperature": 0.35,
        "analyzer_batch_symbols_per_request": 3,
        "analyzer_batch_parse_fallback_limit": 6,
    },
    "medium": {
        "trade_quote": 20.0,
        "conf_threshold": 74,
        "loop_seconds": 180,
        "stop_loss_pct": 0.03,
        "trailing_stop_pct": 0.02,
        "trailing_activate_pct": 0.05,
        "estimated_round_trip_cost_pct": 0.003,
        "entry_cost_buffer_pct": 0.0015,
        "min_net_profit_pct": 0.01,
        "dynamic_position_enabled": True,
        "market_quality_threshold": 0.64,
        "dynamic_min_factor": 0.7,
        "dynamic_max_factor": 1.25,
        "max_total_exposure_ratio": 0.55,
        "max_single_asset_weight": 0.28,
        "max_order_cash_ratio": 0.18,
        "min_cash_reserve_ratio": 0.18,
        "analyzer_cache_ttl_minutes": 20,
        "analyzer_min_signal_quality": 0.55,
        "analyzer_min_interval_minutes": 15,
        "analyzer_max_output_tokens": 220,
        "analyzer_temperature": 0.45,
        "analyzer_batch_symbols_per_request": 4,
        "analyzer_batch_parse_fallback_limit": 8,
    },
    "high": {
        "trade_quote": 30.0,
        "conf_threshold": 68,
        "loop_seconds": 120,
        "stop_loss_pct": 0.04,
        "trailing_stop_pct": 0.025,
        "trailing_activate_pct": 0.06,
        "estimated_round_trip_cost_pct": 0.0035,
        "entry_cost_buffer_pct": 0.002,
        "min_net_profit_pct": 0.006,
        "dynamic_position_enabled": True,
        "market_quality_threshold": 0.58,
        "dynamic_min_factor": 0.75,
        "dynamic_max_factor": 1.5,
        "max_total_exposure_ratio": 0.72,
        "max_single_asset_weight": 0.36,
        "max_order_cash_ratio": 0.24,
        "min_cash_reserve_ratio": 0.1,
        "analyzer_cache_ttl_minutes": 10,
        "analyzer_min_signal_quality": 0.48,
        "analyzer_min_interval_minutes": 8,
        "analyzer_max_output_tokens": 240,
        "analyzer_temperature": 0.55,
        "analyzer_batch_symbols_per_request": 5,
        "analyzer_batch_parse_fallback_limit": 10,
    },
}


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_risk_preset(name: str | None) -> Dict[str, Any]:
    key = (name or "medium").strip().lower()
    return copy.deepcopy(RISK_PRESETS.get(key, RISK_PRESETS["medium"]))


def merge_strategy_config(risk_preset: str | None, overrides: Dict[str, Any] | None) -> Dict[str, Any]:
    merged = get_risk_preset(risk_preset)
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        merged[key] = value
    return merged


def normalize_master_key(raw_value: str) -> bytes:
    raw = (raw_value or "").strip()
    if not raw:
        raise ValueError("master key cannot be empty")
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def load_or_create_master_key() -> bytes:
    from_env = (os.environ.get("ADMIN_PANEL_MASTER_KEY") or "").strip()
    if from_env:
        return normalize_master_key(from_env)

    if MASTER_KEY_FILE.exists():
        return MASTER_KEY_FILE.read_bytes().strip()

    key = Fernet.generate_key()
    MASTER_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MASTER_KEY_FILE.write_bytes(key)
    return key
