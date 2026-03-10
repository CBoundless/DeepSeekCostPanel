#!/usr/bin/env python3
"""无界面自动交易入口。

用途：
- 适合部署到 Linux / 腾讯云 CVM 后台运行
- 日志同时输出到 stdout 和滚动文件
- 支持通过 --env-file 额外加载环境变量文件
- 支持 --validate-only 只校验配置，不启动交易循环
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from auto_trader import AutoTrader, load_trade_config_from_env
from deepseek_analyzer_optimized import AnalyzerConfig, OptimizedDeepSeekAnalyzer
from okx_rest_client import OKXClient


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_FILE = PROJECT_ROOT / "logs" / "autotrade.log"


def _env_str(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return default


def _env_int(name: str, default: int) -> int:
    value = (os.environ.get(name) or "").strip()
    if not value:
        return int(default)
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    value = (os.environ.get(name) or "").strip()
    if not value:
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def _env_optional_float(name: str) -> Optional[float]:
    value = (os.environ.get(name) or "").strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _env_bool(name: str, default: bool) -> bool:
    value = (os.environ.get(name) or "").strip()
    if not value:
        return bool(default)
    return value not in ("0", "false", "False", "no", "NO")


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _load_env_file(env_file: Path) -> None:
    if not env_file.exists():
        raise FileNotFoundError(f"环境变量文件不存在：{env_file}")

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_wrapping_quotes(value.strip())
        if not key:
            continue
        os.environ[key] = value


def _build_logger() -> logging.Logger:
    level_name = (_env_str("AUTOTRADE_LOG_LEVEL", default="INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger = logging.getLogger("deepseek_autotrade")
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    log_file = Path(_env_str("AUTOTRADE_LOG_FILE", default=str(DEFAULT_LOG_FILE)) or str(DEFAULT_LOG_FILE)).expanduser()
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max(1024, _env_int("AUTOTRADE_LOG_MAX_BYTES", 10 * 1024 * 1024)),
            backupCount=max(1, _env_int("AUTOTRADE_LOG_BACKUP_COUNT", 5)),
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.info("日志文件：%s", log_file)
    except Exception as exc:
        logger.warning("日志文件初始化失败：%s", exc)

    return logger


def _build_analyzer() -> OptimizedDeepSeekAnalyzer:
    api_key = _env_str("DEEPSEEK_API_KEY", "deep_api_key", "DEEP_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 DeepSeek API Key：请设置 DEEPSEEK_API_KEY")

    budget_enforcement = (_env_str("BUDGET_ENFORCEMENT", default="warn") or "warn").strip().lower()
    if budget_enforcement not in ("warn", "block"):
        budget_enforcement = "warn"

    config = AnalyzerConfig(
        cache_ttl_minutes=max(1, _env_int("ANALYZER_CACHE_TTL_MINUTES", 30)),
        min_signal_quality=max(0.0, min(1.0, _env_float("ANALYZER_MIN_SIGNAL_QUALITY", 0.5))),
        min_interval_minutes=max(0, _env_int("ANALYZER_MIN_INTERVAL_MINUTES", 5)),
        max_output_tokens=max(1, _env_int("ANALYZER_MAX_OUTPUT_TOKENS", 200)),
        temperature=max(0.0, min(1.5, _env_float("ANALYZER_TEMPERATURE", 0.5))),
        daily_budget=_env_optional_float("ANALYZER_DAILY_BUDGET"),
        budget_enforcement=budget_enforcement,
    )

    base_url = _env_str("DEEPSEEK_BASE_URL", default="https://api.deepseek.com/v1") or "https://api.deepseek.com/v1"
    return OptimizedDeepSeekAnalyzer(api_key=api_key, base_url=base_url, config=config)


def _build_okx_client() -> OKXClient:
    return OKXClient()


def _log_runtime_summary(logger: logging.Logger, analyzer: OptimizedDeepSeekAnalyzer, okx: OKXClient) -> None:
    cfg = load_trade_config_from_env()
    budget = analyzer.config.daily_budget
    logger.info(
        "启动参数：inst_ids=%s bar=%s loop=%ss trade_quote=%s max_positions=%s simulated=%s",
        cfg.inst_ids,
        cfg.bar,
        cfg.loop_seconds,
        cfg.trade_quote,
        cfg.max_positions,
        okx.simulated_trading,
    )
    logger.info(
        "分析参数：base_url=%s cache_ttl=%s min_signal_quality=%s min_interval=%s max_output_tokens=%s temperature=%s daily_budget=%s budget_enforcement=%s",
        analyzer.base_url,
        analyzer.config.cache_ttl_minutes,
        analyzer.config.min_signal_quality,
        analyzer.config.min_interval_minutes,
        analyzer.config.max_output_tokens,
        analyzer.config.temperature,
        budget if budget is not None else "未设置",
        analyzer.config.budget_enforcement,
    )


def _validate_config(logger: logging.Logger) -> tuple[OptimizedDeepSeekAnalyzer, OKXClient]:
    analyzer = _build_analyzer()
    okx = _build_okx_client()
    okx.require_auth()

    cfg = load_trade_config_from_env()
    if not cfg.inst_ids:
        raise RuntimeError("未配置 OKX_SYMBOLS")
    if cfg.trade_quote <= 0:
        raise RuntimeError("OKX_TRADE_QUOTE 必须大于 0")
    if cfg.loop_seconds <= 0:
        raise RuntimeError("OKX_LOOP_SECONDS 必须大于 0")
    if cfg.max_positions <= 0:
        raise RuntimeError("OKX_MAX_POSITIONS 必须大于 0")

    _log_runtime_summary(logger, analyzer, okx)
    return analyzer, okx


def _install_signal_handlers(trader: AutoTrader, logger: logging.Logger) -> dict[str, bool]:
    state = {"stop_requested": False}

    def _handle_signal(signum, _frame):  # type: ignore[no-untyped-def]
        if state["stop_requested"]:
            logger.warning("重复收到退出信号 signum=%s，继续等待线程结束", signum)
            return
        state["stop_requested"] = True
        logger.info("收到退出信号 signum=%s，准备停止自动交易", signum)
        trader.stop()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, _handle_signal)

    return state


def _run(validate_only: bool = False) -> int:
    logger = _build_logger()
    logger.info("无界面自动交易入口启动")

    analyzer, okx = _validate_config(logger)
    if validate_only:
        logger.info("配置校验通过，未启动交易循环（--validate-only）")
        return 0

    cfg = load_trade_config_from_env()
    trader = AutoTrader(analyzer=analyzer, okx=okx, cfg=cfg, log=lambda message: logger.info(message))
    signal_state = _install_signal_handlers(trader, logger)

    trader.start()
    logger.info("自动交易线程已启动，按 Ctrl+C 或发送 SIGTERM 可优雅停止")

    try:
        while True:
            if not trader.is_running():
                if signal_state["stop_requested"]:
                    logger.info("自动交易线程已停止")
                    return 0
                logger.error("自动交易线程意外退出，主进程结束")
                return 1
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt，准备停止自动交易")
        signal_state["stop_requested"] = True
        trader.stop()
    finally:
        if trader.is_running():
            trader.stop()
            for _ in range(30):
                if not trader.is_running():
                    break
                time.sleep(1)
        logger.info("后台自动交易进程已退出")

    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeepSeekCostPanel 无界面自动交易入口")
    parser.add_argument("--env-file", help="额外加载的环境变量文件路径")
    parser.add_argument("--validate-only", action="store_true", help="只校验配置，不启动自动交易")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.env_file:
        _load_env_file(Path(args.env_file).expanduser())
    return _run(validate_only=bool(args.validate_only))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        raise
