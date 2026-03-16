from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = Path(__file__).resolve().parent / "data" / "phase3_smoke_test.db"
os.environ["ADMIN_PANEL_DATABASE_URL"] = f"sqlite:///{DB_PATH}"

if DB_PATH.exists():
    DB_PATH.unlink()

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services.backtest import backtest_service


def fake_ohlcv(exchange: str, inst_id: str, timeframe: str, bars: int) -> List[List[Any]]:
    total = max(80, int(bars or 240))
    base_ts = 1_700_000_000_000
    candles: List[List[Any]] = []
    price = 100.0
    for index in range(total):
        drift = 0.45 if index % 18 < 11 else -0.18
        price = max(20.0, price + drift)
        open_price = round(price - 0.2, 6)
        high_price = round(price + 0.9, 6)
        low_price = round(price - 0.9, 6)
        close_price = round(price, 6)
        volume = round(1000 + index * 7.5, 6)
        candles.append([
            base_ts + index * 60_000,
            str(open_price),
            str(high_price),
            str(low_price),
            str(close_price),
            str(volume),
        ])
    return candles


def unwrap_item(payload: Dict[str, Any]) -> Dict[str, Any]:
    return payload.get("item") or payload


def main() -> None:
    backtest_service._fetch_ohlcv = staticmethod(fake_ohlcv)
    result: Dict[str, Any] = {}

    with TestClient(app) as client:
        health = client.get("/api/health")
        result["health"] = {"status": health.status_code, "body": health.json()}

        frontend_index = client.get("/app/")
        frontend_js = client.get("/app/app.js")
        result["frontend_index"] = {
            "status": frontend_index.status_code,
            "has_title": "多交易所、协作权限、DSL 与策略市场控制台" in frontend_index.text,
            "has_dsl": "自定义指标 DSL" in frontend_index.text,
        }
        result["frontend_appjs"] = {
            "status": frontend_js.status_code,
            "has_capability_loader": "loadCapabilities" in frontend_js.text,
            "has_marketplace_install": "handleMarketplaceInstall" in frontend_js.text,
        }

        bootstrap_status = client.get("/api/bootstrap/status")
        result["bootstrap_status_before"] = {"status": bootstrap_status.status_code, "body": bootstrap_status.json()}

        setup = client.post("/api/bootstrap/setup", json={"username": "admin", "password": "Admin12345"})
        result["bootstrap_setup"] = {"status": setup.status_code, "body": setup.json()}

        login = client.post("/api/auth/login", json={"username": "admin", "password": "Admin12345"})
        login_body = login.json()
        token = login_body["token"]
        headers = {"Authorization": f"Bearer {token}"}
        result["login"] = {"status": login.status_code, "body": {"user": login_body.get("user"), "expires_at": login_body.get("expires_at")}}

        capabilities = client.get("/api/meta/platform-capabilities", headers=headers)
        result["capabilities"] = {"status": capabilities.status_code, "body": capabilities.json()}

        credential_resp = client.post(
            "/api/credentials",
            headers=headers,
            json={
                "name": "Binance Smoke",
                "exchange": "binance",
                "api_key": "demo-key",
                "api_secret": "demo-secret",
                "api_passphrase": "",
                "deepseek_api_key": "demo-deepseek",
                "deepseek_base_url": "https://api.deepseek.com/v1",
                "simulated_trading": True,
                "config": {"base_url": "https://api.binance.com"},
            },
        )
        credential = unwrap_item(credential_resp.json())
        result["credential"] = {"status": credential_resp.status_code, "body": credential}

        strategy_resp = client.post(
            "/api/strategies",
            headers=headers,
            json={
                "name": "Smoke Strategy",
                "credential_id": credential["id"],
                "symbols": "BTCUSDT",
                "timeframe": "1h",
                "risk_preset": "medium",
                "leverage": 2,
                "market_type": "margin",
                "margin_mode": "cross",
                "indicator_dsl": "ema_fast = EMA(close, 9)\nema_slow = EMA(close, 21)",
                "entry_rule": "ema_fast > ema_slow",
                "exit_rule": "ema_fast < ema_slow",
                "config": {"trade_quote": 20},
                "version_note": "smoke-create",
            },
        )
        strategy = unwrap_item(strategy_resp.json())
        result["strategy"] = {"status": strategy_resp.status_code, "body": strategy}

        user_resp = client.post(
            "/api/users",
            headers=headers,
            json={"username": "viewer1", "password": "Viewer12345", "role": "viewer"},
        )
        viewer = unwrap_item(user_resp.json())
        result["user"] = {"status": user_resp.status_code, "body": viewer}

        member_resp = client.post(
            f"/api/strategies/{strategy['id']}/members",
            headers=headers,
            json={"user_id": viewer["id"], "role": "viewer"},
        )
        member = unwrap_item(member_resp.json())
        result["member"] = {"status": member_resp.status_code, "body": member}

        publish_resp = client.post(
            f"/api/strategies/{strategy['id']}/market/publish",
            headers=headers,
            json={
                "title": "Smoke Published Strategy",
                "summary": "smoke summary",
                "description": "smoke description",
                "category": "community",
                "tags": ["smoke", "dsl"],
            },
        )
        market_item = unwrap_item(publish_resp.json())
        result["publish"] = {"status": publish_resp.status_code, "body": market_item}

        market_list = client.get("/api/strategy-marketplace?limit=10", headers=headers)
        result["market_list"] = {"status": market_list.status_code, "count": len(market_list.json().get("items", []))}

        market_detail = client.get(f"/api/strategy-marketplace/{market_item['id']}", headers=headers)
        result["market_detail"] = {"status": market_detail.status_code, "body": unwrap_item(market_detail.json())}

        install_resp = client.post(
            f"/api/strategy-marketplace/{market_item['id']}/install",
            headers=headers,
            json={"credential_id": credential["id"], "name": "Imported Strategy", "version_note": "smoke-install"},
        )
        installed = unwrap_item(install_resp.json())
        result["install"] = {"status": install_resp.status_code, "body": installed}

        members_list = client.get(f"/api/strategies/{strategy['id']}/members", headers=headers)
        result["members_list"] = {"status": members_list.status_code, "count": len(members_list.json().get("items", []))}

        backtest_resp = client.post(
            "/api/backtests/run",
            headers=headers,
            json={
                "strategy_id": strategy["id"],
                "symbol": "BTCUSDT",
                "timeframe": "1h",
                "bars": 120,
                "initial_capital_usdt": 1000,
                "engine": "dsl",
            },
        )
        backtest = unwrap_item(backtest_resp.json())
        result["backtest"] = {"status": backtest_resp.status_code, "body": backtest}

        backtest_detail = client.get(f"/api/backtests/{backtest['id']}", headers=headers)
        result["backtest_detail"] = {"status": backtest_detail.status_code, "body": unwrap_item(backtest_detail.json())}

        dashboard = client.get("/api/dashboard/summary", headers=headers)
        result["dashboard"] = {"status": dashboard.status_code, "body": dashboard.json()}

        strategies = client.get("/api/strategies", headers=headers)
        result["strategies"] = {"status": strategies.status_code, "count": len(strategies.json().get("items", []))}

        backtests = client.get("/api/backtests", headers=headers)
        result["backtests"] = {"status": backtests.status_code, "count": len(backtests.json().get("items", []))}

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
