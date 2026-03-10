#!/usr/bin/env python3
"""
优化版 DeepSeek AI 分析器 - 成本控制
支持缓存、条件触发、批量分析和成本统计
"""

import requests
import json
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict
import re


# 公共交易所数据源（无需密钥）
# 备注：部分网络环境会对 `api.binance.com` 出现 TLS 握手被中断（SSLEOFError）。
# 因此这里做“多域名降级”，按顺序尝试。
_DEFAULT_BINANCE_BASE_URLS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]


def _get_binance_base_urls() -> List[str]:
    # 允许用户通过环境变量强制指定（例如公司网络只能访问某个镜像域名）
    # - BINANCE_BASE_URL: 单个 URL
    # - BINANCE_BASE_URLS: 逗号分隔多个 URL
    import os

    one = (os.environ.get("BINANCE_BASE_URL") or "").strip()
    many = (os.environ.get("BINANCE_BASE_URLS") or "").strip()

    if many:
        urls = [u.strip() for u in many.split(",") if u.strip()]
        return urls or list(_DEFAULT_BINANCE_BASE_URLS)

    if one:
        return [one]

    return list(_DEFAULT_BINANCE_BASE_URLS)


_ALLOWED_BINANCE_INTERVALS = {
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
}


def _ema_last(values: List[float], period: int) -> Optional[float]:
    if period <= 0:
        return None
    if not values:
        return None
    alpha = 2.0 / (period + 1)
    ema = float(values[0])
    for v in values[1:]:
        ema = alpha * float(v) + (1.0 - alpha) * ema
    return ema


def _rsi_last(closes: List[float], period: int = 14) -> Optional[float]:
    if period <= 0:
        return None
    if len(closes) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    # 初始窗口
    for i in range(1, period + 1):
        diff = float(closes[i]) - float(closes[i - 1])
        if diff >= 0:
            gains += diff
        else:
            losses -= diff

    avg_gain = gains / period
    avg_loss = losses / period

    # Wilder 平滑
    for i in range(period + 1, len(closes)):
        diff = float(closes[i]) - float(closes[i - 1])
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd_last(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(closes) < slow + 1:
        return None, None, None

    # 为了得到序列末端的 MACD 与 signal，这里用逐点 EMA 计算（纯 Python）
    def ema_series(vals: List[float], p: int) -> List[float]:
        if not vals:
            return []
        a = 2.0 / (p + 1)
        out = [float(vals[0])]
        for x in vals[1:]:
            out.append(a * float(x) + (1.0 - a) * out[-1])
        return out

    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema_series(macd_line, signal)

    macd_v = macd_line[-1] if macd_line else None
    signal_v = signal_line[-1] if signal_line else None
    hist_v = (macd_v - signal_v) if (macd_v is not None and signal_v is not None) else None
    return macd_v, signal_v, hist_v


class CostTracker:
    """成本追踪器"""
    
    def __init__(self):
        self.total_calls = 0
        self.total_tokens = 0
        self.total_cost = 0.0
        self.daily_calls = defaultdict(int)
        self.daily_cost = defaultdict(float)
        self.call_history = []
        
    def record_call(self, tokens: int, cost: float):
        """记录一次 API 调用"""
        today = datetime.now().strftime("%Y-%m-%d")
        self.total_calls += 1
        self.total_tokens += tokens
        self.total_cost += cost
        self.daily_calls[today] += 1
        self.daily_cost[today] += cost
        
        self.call_history.append({
            "timestamp": datetime.now().isoformat(),
            "tokens": tokens,
            "cost": cost
        })
        
        # 只保留最近 1000 条记录
        if len(self.call_history) > 1000:
            self.call_history = self.call_history[-1000:]
    
    def get_summary(self) -> Dict:
        """获取成本统计摘要"""
        today = datetime.now().strftime("%Y-%m-%d")
        return {
            "total_calls": self.total_calls,
            "total_tokens": self.total_tokens,
            "total_cost": round(self.total_cost, 4),
            "today_calls": self.daily_calls[today],
            "today_cost": round(self.daily_cost[today], 4),
            "avg_cost_per_call": round(self.total_cost / max(1, self.total_calls), 6)
        }


class AnalysisCache:
    """分析结果缓存"""
    
    def __init__(self, ttl_minutes: int = 30):
        """
        初始化缓存
        
        Args:
            ttl_minutes: 缓存有效期（分钟）
        """
        self.cache = {}
        self.ttl = timedelta(minutes=ttl_minutes)
    
    def _get_key(self, symbol: str, timeframe: str, indicators: Dict) -> str:
        """生成缓存键"""
        # 将指标转换为哈希值
        indicators_str = json.dumps(indicators, sort_keys=True)
        key_str = f"{symbol}:{timeframe}:{indicators_str}"
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def get(self, symbol: str, timeframe: str, indicators: Dict) -> Optional[Dict]:
        """获取缓存的分析结果"""
        key = self._get_key(symbol, timeframe, indicators)
        
        if key in self.cache:
            cached_data = self.cache[key]
            if datetime.now() - cached_data["timestamp"] < self.ttl:
                return cached_data["result"]
            else:
                # 缓存过期，删除
                del self.cache[key]
        
        return None
    
    def set(self, symbol: str, timeframe: str, indicators: Dict, result: Dict):
        """缓存分析结果"""
        key = self._get_key(symbol, timeframe, indicators)
        self.cache[key] = {
            "timestamp": datetime.now(),
            "result": result
        }
    
    def clear_expired(self):
        """清除过期缓存"""
        expired_keys = []
        for key, cached_data in self.cache.items():
            if datetime.now() - cached_data["timestamp"] > self.ttl:
                expired_keys.append(key)
        
        for key in expired_keys:
            del self.cache[key]


@dataclass
class AnalyzerConfig:
    """分析器可配置项。

    说明：用于把“缓存 TTL / 信号阈值 / 频率限制 / 预算告警”等从硬编码变为可配置。
    """

    cache_ttl_minutes: int = 30
    min_signal_quality: float = 0.5
    min_interval_minutes: int = 5
    max_output_tokens: int = 200
    temperature: float = 0.5
    daily_budget: Optional[float] = None
    budget_enforcement: str = "warn"  # warn | block


class OptimizedDeepSeekAnalyzer:
    """优化版 DeepSeek 分析器"""

    # DeepSeek 定价（美元/1K tokens）
    INPUT_COST_PER_1K_TOKENS = 0.14 / 1000  # $0.14 per 1M input tokens
    OUTPUT_COST_PER_1K_TOKENS = 0.28 / 1000  # $0.28 per 1M output tokens

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        config: Optional[AnalyzerConfig] = None,
    ):
        """初始化优化版分析器。

        Args:
            api_key: DeepSeek API Key
            base_url: API 基础 URL
            config: 分析器配置（可为空，使用默认值）
        """
        self.api_key = api_key
        self.base_url = base_url
        self.model = "deepseek-chat"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        self.config: AnalyzerConfig = config or AnalyzerConfig()

        # 初始化缓存和成本追踪
        self.cache = AnalysisCache(ttl_minutes=int(self.config.cache_ttl_minutes))
        self.cost_tracker = CostTracker()

        # 调用计数（用于条件触发）
        self.call_count = 0
        # key: (symbol, timeframe)
        self.last_analysis_time: Dict[Tuple[str, str], datetime] = {}

        # 刷新统计时的“低成本 ping”防抖，避免频繁点击导致额外花费
        self._last_stats_ping_at: Optional[datetime] = None

    def apply_config(
        self,
        *,
        cache_ttl_minutes: Optional[int] = None,
        min_signal_quality: Optional[float] = None,
        min_interval_minutes: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        daily_budget: Optional[float] = None,
        budget_enforcement: Optional[str] = None,
    ) -> AnalyzerConfig:
        """应用配置（运行中动态生效）。"""
        if cache_ttl_minutes is not None:
            self.config.cache_ttl_minutes = max(1, int(cache_ttl_minutes))
            self.cache.ttl = timedelta(minutes=int(self.config.cache_ttl_minutes))

        if min_signal_quality is not None:
            try:
                v = float(min_signal_quality)
            except Exception:
                v = self.config.min_signal_quality
            self.config.min_signal_quality = max(0.0, min(1.0, v))

        if min_interval_minutes is not None:
            self.config.min_interval_minutes = max(0, int(min_interval_minutes))

        if max_output_tokens is not None:
            self.config.max_output_tokens = max(1, int(max_output_tokens))

        if temperature is not None:
            try:
                t = float(temperature)
            except Exception:
                t = self.config.temperature
            self.config.temperature = max(0.0, min(1.5, t))

        if daily_budget is not None:
            try:
                b = float(daily_budget)
            except Exception:
                b = None
            self.config.daily_budget = b if (b is not None and b > 0) else None

        if budget_enforcement is not None:
            mode = (budget_enforcement or "").strip().lower()
            self.config.budget_enforcement = mode if mode in ("warn", "block") else "warn"

        return self.config

    def apply_budget(self, daily_budget: float, enforcement: str = "warn") -> Dict:
        """根据日预算应用推荐配置，并写回到 analyzer（让 GUI 的“预算”真正生效）。"""
        cfg = CostOptimizationStrategy.get_recommended_config(float(daily_budget))
        self.apply_config(
            cache_ttl_minutes=int(cfg.get("recommended_cache_ttl") or self.config.cache_ttl_minutes),
            min_interval_minutes=int(cfg.get("recommended_check_interval") or self.config.min_interval_minutes),
            min_signal_quality=float(cfg.get("signal_quality_threshold") or self.config.min_signal_quality),
            daily_budget=float(cfg.get("daily_budget") or daily_budget),
            budget_enforcement=enforcement,
        )
        return cfg

    def _get_budget_state(self) -> Dict:
        today = datetime.now().strftime("%Y-%m-%d")
        budget = self.config.daily_budget
        today_cost = float(self.cost_tracker.daily_cost.get(today, 0.0))
        enabled = budget is not None and float(budget) > 0
        remaining = (float(budget) - today_cost) if enabled else None
        exceeded = bool(enabled and remaining is not None and remaining < 0)
        return {
            "enabled": enabled,
            "daily_budget": float(budget) if enabled else None,
            "today_cost": today_cost,
            "remaining": max(0.0, float(remaining)) if (enabled and remaining is not None) else None,
            "exceeded": exceeded,
            "enforcement": self.config.budget_enforcement,
        }

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """归一化交易对 key。

        目标：让 `BTC-USDT` / `BTC/USDT` / `BTCUSDT` 等写法在解析/白名单匹配时视为同一个 key。
        注意：这里仅用于“key 匹配/解析”，不改变 OKX 下单/拉K线时使用的 instId 原样。
        """
        s = (symbol or "").strip().upper()
        # 统一去掉分隔符（- / _ 空格等），仅保留字母数字，避免 OKX 与示例 key 不一致导致解析丢失。
        return re.sub(r"[^A-Z0-9]", "", s)

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        s = OptimizedDeepSeekAnalyzer._normalize_symbol(symbol)
        if not re.fullmatch(r"[A-Z0-9]{3,20}", s):
            raise ValueError(f"无效交易对：{symbol!r}（示例：BTCUSDT）")
        return s

    @staticmethod
    def _validate_timeframe(timeframe: str) -> str:
        """校验并归一化 timeframe。

        本项目内部以 Binance 风格为准（例如 `1m/1h/1d/1w/1M`），但为了兼容 OKX 的
        `bar` 写法（例如 `1H/4H/1D/1W/1M`），这里会做一次大小写/格式归一化。

        - `1H`/`1h` -> `1h`
        - `1D`/`1d` -> `1d`
        - `1W`/`1w` -> `1w`
        - `1M` 保持为 `1M`（注意：与分钟 `1m` 区分）
        """
        raw = (timeframe or "").strip()
        tf = raw

        # OKX 风格：小时/天/周通常是大写字母
        if re.fullmatch(r"\d+[Hh]", tf):
            tf = tf[:-1] + "h"
        elif re.fullmatch(r"\d+[Dd]", tf):
            tf = tf[:-1] + "d"
        elif re.fullmatch(r"\d+[Ww]", tf):
            tf = tf[:-1] + "w"
        elif re.fullmatch(r"\d+M", tf):
            # 月线：保持大写 M（例如 1M）
            tf = tf

        if tf not in _ALLOWED_BINANCE_INTERVALS:
            raise ValueError(f"无效周期：{timeframe!r}（允许：{sorted(_ALLOWED_BINANCE_INTERVALS)}）")
        return tf

    @staticmethod
    def _map_timeframe_to_okx_bar(timeframe: str) -> str:
        """把本项目使用的 timeframe（偏 Binance 风格）映射到 OKX `bar` 参数。

        说明：OKX 的 bar 常见写法为 `1m/5m/1H/4H/1D/1W/1M`。
        我们尽量兼容现有输入（例如 `1h` -> `1H`）。
        """
        tf = OptimizedDeepSeekAnalyzer._validate_timeframe(timeframe)
        mapping = {
            "1m": "1m",
            "3m": "3m",
            "5m": "5m",
            "15m": "15m",
            "30m": "30m",
            "1h": "1H",
            "2h": "2H",
            "4h": "4H",
            "6h": "6H",
            "8h": "8H",
            "12h": "12H",
            "1d": "1D",
            "3d": "3D",
            "1w": "1W",
            "1M": "1M",
        }
        bar = mapping.get(tf)
        if not bar:
            raise ValueError(f"当前不支持该周期映射到 OKX bar：{timeframe!r}")
        return bar

    @staticmethod
    def _normalize_okx_inst_id(inst_id: str) -> str:
        """尽量把输入标准化成 OKX instId（例如 BTC-USDT）。"""
        s = (inst_id or "").strip().upper().replace("/", "-")
        if not s:
            raise ValueError("instId 不能为空")

        # 常见输入：BTCUSDT -> BTC-USDT
        if "-" not in s and s.endswith("USDT") and len(s) > 4:
            s = s[:-4] + "-USDT"

        # 最宽松校验：至少包含一个分隔符
        if "-" not in s:
            raise ValueError(f"无效 instId：{inst_id!r}（示例：BTC-USDT）")
        return s

    @staticmethod
    def fetch_ohlcv_okx(inst_id: str, timeframe: str, limit: int = 200, okx_client: Any = None) -> List[List]:
        """从 OKX 公共接口拉取 K 线（无需密钥）。

        Args:
            inst_id: OKX instId（例如 BTC-USDT；也可输入 BTCUSDT 会自动转）
            timeframe: 兼容本项目的 timeframe（例如 1m/1h/1d）
            limit: 条数（OKX 常用 100/300/500 等，这里做宽松限制）
            okx_client: 可选，传入 `okx_rest_client.OKXClient` 实例；不传会临时创建（仅 public）。

        Returns:
            List[[open_time_ms, open, high, low, close, volume], ...]（按时间升序）
        """
        from okx_rest_client import OKXClient

        inst = OptimizedDeepSeekAnalyzer._normalize_okx_inst_id(inst_id)
        bar = OptimizedDeepSeekAnalyzer._map_timeframe_to_okx_bar(timeframe)
        lim = int(limit)
        if lim <= 0:
            raise ValueError("limit 需 > 0")
        lim = min(lim, 300)

        c = okx_client if okx_client is not None else OKXClient(auth=None)
        payload = c.get_candles(inst_id=inst, bar=bar, limit=lim)

        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX K线请求失败：{payload}")

        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError("OKX K线返回为空")

        out: List[List] = []
        # OKX data 常见为倒序（最新在前），这里统一成升序
        for row in reversed(data):
            try:
                # [ts, o, h, l, c, vol, ...]
                ts = int(float(row[0]))
                out.append([ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])])
            except Exception:
                continue

        if len(out) < 30:
            raise RuntimeError(f"K线数据不足（{len(out)} 条），无法稳定计算指标")
        return out

    @staticmethod
    def fetch_ohlcv_binance(symbol: str, timeframe: str, limit: int = 200) -> List[List]:
        """从 Binance 公共接口拉取 K 线（无需密钥）。

        Returns:
            List[[open_time_ms, open, high, low, close, volume], ...]
        """
        s = OptimizedDeepSeekAnalyzer._validate_symbol(symbol)
        tf = OptimizedDeepSeekAnalyzer._validate_timeframe(timeframe)
        lim = int(limit)
        if lim <= 0 or lim > 1000:
            raise ValueError("limit 需在 1..1000")

        params = {"symbol": s, "interval": tf, "limit": lim}

        last_errs: List[str] = []
        data = None
        for base in _get_binance_base_urls():
            url = f"{base.rstrip('/')}/api/v3/klines"
            try:
                r = requests.get(
                    url,
                    params=params,
                    timeout=15,
                    headers={"User-Agent": "DeepSeekCostPanel/1.0"},
                )
                if r.status_code != 200:
                    last_errs.append(f"{base} -> HTTP {r.status_code}: {r.text[:120]}")
                    continue

                data = r.json()
                if not isinstance(data, list) or not data:
                    last_errs.append(f"{base} -> empty response")
                    data = None
                    continue

                break
            except Exception as e:
                # 常见：SSLEOFError / ConnectionError / Timeout
                last_errs.append(f"{base} -> {type(e).__name__}: {e}")
                continue

        if data is None:
            hint = (
                "无法连接 Binance K 线接口（可能是网络阻断/公司代理/SSL 库兼容问题）。\n"
                "你可以：\n"
                "1) 换网络（例如手机热点）\n"
                "2) 设置环境变量 BINANCE_BASE_URLS 使用可访问的镜像域名（逗号分隔）\n"
                "   例如：export BINANCE_BASE_URLS=\"https://data-api.binance.vision\"\n"
            )
            details = "\n".join(last_errs[-5:])
            raise RuntimeError(f"Binance K线请求失败（已尝试多个域名）\n{details}\n\n{hint}")

        out: List[List] = []
        for row in data:
            # [0 open_time,1 open,2 high,3 low,4 close,5 volume,...]
            try:
                out.append([
                    int(row[0]),
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                ])
            except Exception:
                continue

        if len(out) < 30:
            raise RuntimeError(f"K线数据不足（{len(out)} 条），无法稳定计算指标")
        return out

    @staticmethod
    def build_indicators_from_ohlcv(ohlcv: List[List]) -> Dict:
        closes = [float(x[4]) for x in ohlcv if len(x) >= 5]
        ema_9 = _ema_last(closes, 9)
        ema_20 = _ema_last(closes, 20)
        ema_50 = _ema_last(closes, 50)
        rsi_14 = _rsi_last(closes, 14)
        macd, macd_signal, macd_hist = _macd_last(closes)

        # 与旧逻辑字段名保持一致
        return {
            "ema_9": float(ema_9) if ema_9 is not None else 0.0,
            "ema_20": float(ema_20) if ema_20 is not None else 0.0,
            "ema_50": float(ema_50) if ema_50 is not None else 0.0,
            "rsi_14": float(rsi_14) if rsi_14 is not None else 0.0,
            "macd": float(macd) if macd is not None else 0.0,
            "macd_signal": float(macd_signal) if macd_signal is not None else 0.0,
            "macd_hist": float(macd_hist) if macd_hist is not None else 0.0,
        }

    def analyze_market_from_okx(
        self,
        inst_id: str,
        timeframe: str,
        *,
        limit: int = 200,
        force_analysis: bool = False,
        okx_client: Any = None,
    ) -> Dict:
        """完整链路（OKX）：拉取 K 线 → 计算指标 → 调用 `analyze_market()`（必要时触发 DeepSeek）。"""
        inst = self._normalize_okx_inst_id(inst_id)
        tf = self._validate_timeframe(timeframe)
        ohlcv = self.fetch_ohlcv_okx(inst, tf, limit=limit, okx_client=okx_client)
        indicators = self.build_indicators_from_ohlcv(ohlcv)

        result = self.analyze_market(inst, tf, ohlcv, indicators, force_analysis=force_analysis)
        result.setdefault("market_source", "okx")
        result.setdefault("ohlcv_points", len(ohlcv))
        result.setdefault("indicators", indicators)
        result.setdefault("inst_id", inst)
        return result

    def analyze_market_from_binance(self, symbol: str, timeframe: str, limit: int = 200, force_analysis: bool = False) -> Dict:
        """完整链路：拉取 K 线 → 计算指标 → 调用 `analyze_market()`（必要时触发 DeepSeek）。"""
        s = self._validate_symbol(symbol)
        tf = self._validate_timeframe(timeframe)
        ohlcv = self.fetch_ohlcv_binance(s, tf, limit=limit)
        indicators = self.build_indicators_from_ohlcv(ohlcv)

        result = self.analyze_market(s, tf, ohlcv, indicators, force_analysis=force_analysis)
        # 附带一些调试信息，便于确认是否真正触发 API
        result.setdefault("market_source", "binance")
        result.setdefault("ohlcv_points", len(ohlcv))
        result.setdefault("indicators", indicators)
        return result

    def analyze_markets_from_okx(
        self,
        *,
        inst_ids: List[str],
        okx_client: Any,
        bar: str,
        limit: int = 200,
        force_analysis: bool = False,
    ) -> Dict:
        """批量市场分析（OKX）：对多个 instId 只调用一次 DeepSeek（显著省钱）。

        Args:
            inst_ids: 例如 ["BTC-USDT", "ETH-USDT"]
            okx_client: `okx_rest_client.OKXClient` 实例（用于拉取 K 线；可无鉴权）
            bar: OKX bar（例如 1H/15m/1D）。为了兼容 GUI，允许你传入本项目 timeframe（1h/1d），这里会自动兼容。
        """
        # 兼容：`bar` 既可能是 OKX bar（1H/1D），也可能是本项目 timeframe（1h/1d）。
        # 统一归一化到内部 timeframe（例如 1H -> 1h），并据此映射出 OKX bar。
        tf = self._validate_timeframe(bar)
        okx_bar = self._map_timeframe_to_okx_bar(tf)
        tf_for_cache = tf

        lim = int(limit)

        norm_ids: List[str] = []
        for x in (inst_ids or []):
            if not x:
                continue
            try:
                norm_ids.append(self._normalize_okx_inst_id(str(x)))
            except Exception:
                continue
        norm_ids = list(dict.fromkeys(norm_ids))

        results: Dict[str, Dict] = {}
        pending: List[Tuple[str, List[List], Dict, float]] = []
        threshold = float(getattr(self.config, "min_signal_quality", 0.5) or 0.5)

        for inst in norm_ids:
            try:
                ohlcv = self.fetch_ohlcv_okx(inst, tf_for_cache, limit=lim, okx_client=okx_client)
                indicators = self.build_indicators_from_ohlcv(ohlcv)
            except Exception as e:
                results[inst] = {
                    "status": "error",
                    "message": str(e),
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "exception",
                }
                continue

            if not force_analysis:
                cached = self.cache.get(inst, tf_for_cache, indicators)
                if cached:
                    cached["source"] = "cache"
                    results[inst] = cached
                    continue

            if not force_analysis and (not self._should_analyze(inst, tf_for_cache)):
                results[inst] = {
                    "status": "skipped",
                    "reason": "调用频率限制",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "frequency_limit",
                }
                continue

            q = self._assess_signal_quality(indicators)
            if (q < threshold) and (not force_analysis):
                results[inst] = {
                    "status": "skipped",
                    "reason": f"信号质量低 ({q:.2f} < {threshold:.2f})",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "low_signal_quality",
                    "signal_quality": q,
                    "signal_threshold": threshold,
                }
                continue

            budget_state = self._get_budget_state()
            if (
                budget_state.get("enabled")
                and budget_state.get("exceeded")
                and budget_state.get("enforcement") == "block"
                and not force_analysis
            ):
                results[inst] = {
                    "status": "skipped",
                    "reason": "已超出日预算（阻断模式）",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "budget_exceeded",
                    "budget": budget_state,
                }
                continue

            pending.append((inst, ohlcv, indicators, q))

        if not pending:
            return {
                "status": "success",
                "source": "no_api_needed",
                "results": results,
                "budget": self._get_budget_state(),
            }

        prompt = self._build_batch_prompt(tf_for_cache, pending)
        max_tokens = min(1200, int(getattr(self.config, "max_output_tokens", 200) or 200) * max(1, len(pending)))
        content, usage = self._call_deepseek_api_ex(prompt, max_tokens=max_tokens)

        if not content:
            for inst, _ohlcv, _ind, _q in pending:
                results[inst] = {
                    "status": "error",
                    "message": "API 调用失败",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "api_error",
                }
            return {
                "status": "error",
                "source": "api_error",
                "results": results,
                "budget": self._get_budget_state(),
            }

        parsed = self._parse_batch_response(content, [x[0] for x in pending])

        prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
        completion_tokens = int((usage or {}).get("completion_tokens") or 0)
        total_tokens = int((usage or {}).get("total_tokens") or (prompt_tokens + completion_tokens) or 0)
        if total_tokens <= 0:
            total_tokens = self._estimate_tokens(prompt, content)

        if (prompt_tokens or completion_tokens) and callable(getattr(self, "_calculate_cost_from_usage", None)):
            cost = self._calculate_cost_from_usage(prompt_tokens, completion_tokens)
        else:
            cost = self._calculate_cost(total_tokens)

        self.cost_tracker.record_call(total_tokens, cost)

        per_cost = float(cost) / max(1, len(pending))
        per_tokens = int(total_tokens / max(1, len(pending)))

        for inst, _ohlcv, indicators, _q in pending:
            item = parsed.get(inst)
            if not isinstance(item, dict) or not item:
                out = {
                    "status": "error",
                    "message": "API 返回未匹配到该标的（key 可能不一致）",
                    "raw_analysis": content,
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "target_price": None,
                    "stop_loss": None,
                }
                out["source"] = "api_parse_miss"
            else:
                out = {
                    "status": item.get("status") or "success",
                    "raw_analysis": item.get("raw_analysis") or content,
                    "recommendation": (item.get("recommendation") or item.get("action") or "HOLD").upper(),
                    "confidence": self._coerce_confidence(item.get("confidence"), default=0),
                    "target_price": item.get("target_price"),
                    "stop_loss": item.get("stop_loss"),
                }
                out["source"] = "api"

            out["cost"] = round(per_cost, 6)
            out["tokens"] = int(per_tokens)
            out["budget"] = self._get_budget_state()
            out["market_source"] = "okx"
            out["inst_id"] = inst

            # 仅在成功解析时缓存
            if out.get("status") == "success":
                self.cache.set(inst, tf_for_cache, indicators, dict(out))
            results[inst] = out

        return {
            "status": "success",
            "source": "batch_api",
            "results": results,
            "batch": {
                "inst_ids": [x[0] for x in pending],
                "okx_bar": okx_bar,
                "tokens": int(total_tokens),
                "cost": round(float(cost), 6),
            },
            "budget": self._get_budget_state(),
        }

    def analyze_markets_from_binance(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int = 200,
        force_analysis: bool = False,
    ) -> Dict:
        """批量市场分析：对多个交易对只调用一次 DeepSeek（显著省钱）。

        返回结构：
        - status/source: 批量调用整体状态
        - results: {symbol -> 单个分析结果}
        """
        tf = self._validate_timeframe(timeframe)
        lim = int(limit)

        norm_symbols: List[str] = []
        for s in (symbols or []):
            if not s:
                continue
            try:
                norm_symbols.append(self._validate_symbol(str(s)))
            except Exception:
                continue
        norm_symbols = list(dict.fromkeys(norm_symbols))  # 去重保持顺序

        results: Dict[str, Dict] = {}
        pending: List[Tuple[str, List[List], Dict, float]] = []
        threshold = float(getattr(self.config, "min_signal_quality", 0.5) or 0.5)

        # 先逐个拉数据与做本地过滤（缓存/频率/信号阈值/预算阻断）
        for sym in norm_symbols:
            try:
                ohlcv = self.fetch_ohlcv_binance(sym, tf, limit=lim)
                indicators = self.build_indicators_from_ohlcv(ohlcv)
            except Exception as e:
                results[sym] = {
                    "status": "error",
                    "message": str(e),
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "exception",
                }
                continue

            if not force_analysis:
                cached = self.cache.get(sym, tf, indicators)
                if cached:
                    cached["source"] = "cache"
                    results[sym] = cached
                    continue

            if not force_analysis and (not self._should_analyze(sym, tf)):
                results[sym] = {
                    "status": "skipped",
                    "reason": "调用频率限制",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "frequency_limit",
                }
                continue

            q = self._assess_signal_quality(indicators)
            if (q < threshold) and (not force_analysis):
                results[sym] = {
                    "status": "skipped",
                    "reason": f"信号质量低 ({q:.2f} < {threshold:.2f})",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "low_signal_quality",
                    "signal_quality": q,
                    "signal_threshold": threshold,
                }
                continue

            budget_state = self._get_budget_state()
            if budget_state.get("enabled") and budget_state.get("exceeded") and budget_state.get("enforcement") == "block" and not force_analysis:
                results[sym] = {
                    "status": "skipped",
                    "reason": "已超出日预算（阻断模式）",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "budget_exceeded",
                    "budget": budget_state,
                }
                continue

            pending.append((sym, ohlcv, indicators, q))

        if not pending:
            return {
                "status": "success",
                "source": "no_api_needed",
                "results": results,
                "budget": self._get_budget_state(),
            }

        # 构造批量 prompt（要求严格 JSON 输出，便于解析）
        prompt = self._build_batch_prompt(tf, pending)
        max_tokens = min(1200, int(getattr(self.config, "max_output_tokens", 200) or 200) * max(1, len(pending)))
        content, usage = self._call_deepseek_api_ex(prompt, max_tokens=max_tokens)

        if not content:
            for sym, _ohlcv, _ind, _q in pending:
                results[sym] = {
                    "status": "error",
                    "message": "API 调用失败",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "api_error",
                }
            return {
                "status": "error",
                "source": "api_error",
                "results": results,
                "budget": self._get_budget_state(),
            }

        parsed = self._parse_batch_response(content, [x[0] for x in pending])

        prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
        completion_tokens = int((usage or {}).get("completion_tokens") or 0)
        total_tokens = int((usage or {}).get("total_tokens") or (prompt_tokens + completion_tokens) or 0)
        if total_tokens <= 0:
            total_tokens = self._estimate_tokens(prompt, content)

        if (prompt_tokens or completion_tokens) and callable(getattr(self, "_calculate_cost_from_usage", None)):
            cost = self._calculate_cost_from_usage(prompt_tokens, completion_tokens)
        else:
            cost = self._calculate_cost(total_tokens)

        self.cost_tracker.record_call(total_tokens, cost)

        per_cost = float(cost) / max(1, len(pending))
        per_tokens = int(total_tokens / max(1, len(pending)))

        for sym, _ohlcv, indicators, _q in pending:
            item = parsed.get(sym)
            if not isinstance(item, dict) or not item:
                out = {
                    "status": "error",
                    "message": "API 返回未匹配到该交易对（key 可能不一致）",
                    "raw_analysis": content,
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "target_price": None,
                    "stop_loss": None,
                }
                out["source"] = "api_parse_miss"
            else:
                # 统一字段（尽量兼容单次分析的输出结构）
                out = {
                    "status": item.get("status") or "success",
                    "raw_analysis": item.get("raw_analysis") or content,
                    "recommendation": (item.get("recommendation") or item.get("action") or "HOLD").upper(),
                    "confidence": self._coerce_confidence(item.get("confidence"), default=0),
                    "target_price": item.get("target_price"),
                    "stop_loss": item.get("stop_loss"),
                }
                out["source"] = "api"

            out["cost"] = round(per_cost, 6)
            out["tokens"] = int(per_tokens)
            out["budget"] = self._get_budget_state()

            # 仅在成功解析时缓存
            if out.get("status") == "success":
                self.cache.set(sym, tf, indicators, dict(out))
            results[sym] = out

        return {
            "status": "success",
            "source": "batch_api",
            "results": results,
            "batch": {
                "symbols": [x[0] for x in pending],
                "tokens": int(total_tokens),
                "cost": round(float(cost), 6),
            },
            "budget": self._get_budget_state(),
        }

    def _build_batch_prompt(self, timeframe: str, items: List[Tuple[str, List[List], Dict, float]]) -> str:
        lines = [
            "你是量化交易分析师。请严格只输出 JSON（不要 markdown、不要额外解释）。",
            "JSON 格式如下：",
            '{"BTCUSDT": {"recommendation": "BUY|SELL|HOLD", "confidence": 0-100, "target_price": number|null, "stop_loss": number|null, "reason": string}}',
            "",
            "强约束：confidence 必须是 0-100 的整数，不能把 50 当默认值（除非你判断确实完全中性）。",
            "",
            f"timeframe: {timeframe}",
            "data:",
        ]
        for sym, ohlcv, indicators, q in items:
            close_price = float(ohlcv[-1][4]) if ohlcv else 0.0
            lines.append(
                f"- {sym}: price={close_price:.4f}, ema9={indicators.get('ema_9', 0):.4f}, ema20={indicators.get('ema_20', 0):.4f}, ema50={indicators.get('ema_50', 0):.4f}, rsi14={indicators.get('rsi_14', 0):.2f}, macd={indicators.get('macd', 0):.4f}, signal_quality={q:.2f}"
            )
        lines.append("\n只输出 JSON。")
        return "\n".join(lines)

    def _parse_batch_response(self, response_text: str, symbols: List[str]) -> Dict[str, Dict]:
        """尽量把批量响应解析成 {symbol -> dict}。

        关键点：模型可能返回 `BTCUSDT` 或 `BTC-USDT`，这里用 `_normalize_symbol()`
        做“等价 key”匹配，并最终**映射回请求时的原始 symbol/instId**，避免上层 `parsed.get(inst)` 拿不到。
        """
        text = (response_text or "").strip()
        if not text:
            return {}

        # 将请求 symbols 做一份“归一化 -> 原样”的白名单映射
        allow_map: Dict[str, str] = {}
        for x in (symbols or []):
            nx = self._normalize_symbol(str(x))
            if nx and nx not in allow_map:
                allow_map[nx] = str(x)

        if not allow_map:
            return {}

        # 1) 优先尝试从全文中截取 JSON 对象
        try:
            s = text
            start = s.find("{")
            end = s.rfind("}")
            if start != -1 and end != -1 and end > start:
                obj = json.loads(s[start : end + 1])
                if isinstance(obj, dict):
                    out: Dict[str, Dict] = {}
                    for k, v in obj.items():
                        nk = self._normalize_symbol(str(k))
                        if nk in allow_map and isinstance(v, dict):
                            out[allow_map[nk]] = v
                    return out
        except Exception:
            pass

        # 2) 解析失败就返回空，让上层走保守兜底
        return {}

    def analyze_market(self, symbol: str, timeframe: str, ohlcv_data: List[List],
                      indicators: Dict, force_analysis: bool = False) -> Dict:
        """
        优化的市场分析
        
        Args:
            symbol: 交易对
            timeframe: 时间框架
            ohlcv_data: OHLCV 数据
            indicators: 技术指标
            force_analysis: 是否强制分析（忽略缓存）
            
        Returns:
            分析结果
        """
        try:
            # 1. 检查缓存
            if not force_analysis:
                cached_result = self.cache.get(symbol, timeframe, indicators)
                if cached_result:
                    cached_result["source"] = "cache"
                    return cached_result
            
            # 2. 检查调用频率（成本控制）
            if not self._should_analyze(symbol, timeframe):
                return {
                    "status": "skipped",
                    "reason": "调用频率限制",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "frequency_limit",
                }

            # 3. 检查信号质量（只在高质量信号时分析）
            signal_quality = self._assess_signal_quality(indicators)
            threshold = float(getattr(self.config, "min_signal_quality", 0.5) or 0.5)
            if signal_quality < threshold and not force_analysis:
                return {
                    "status": "skipped",
                    "reason": f"信号质量低 ({signal_quality:.2f} < {threshold:.2f})",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "low_signal_quality",
                }

            # 3.5 预算告警/阻断
            budget_state = self._get_budget_state()
            if budget_state.get("enabled") and budget_state.get("exceeded") and budget_state.get("enforcement") == "block" and not force_analysis:
                return {
                    "status": "skipped",
                    "reason": "已超出日预算（阻断模式）",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "budget_exceeded",
                }

            # 4. 构建优化的提示词（减少 token 使用）
            prompt = self._build_optimized_prompt(symbol, timeframe, ohlcv_data, indicators)

            # 5. 调用 API（尽量使用 usage 做精确计价）
            response, usage = self._call_deepseek_api_ex(prompt, max_tokens=int(self.config.max_output_tokens))

            if response:
                analysis = self._parse_analysis_response(response)

                prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
                completion_tokens = int((usage or {}).get("completion_tokens") or 0)
                total_tokens = int((usage or {}).get("total_tokens") or (prompt_tokens + completion_tokens) or 0)

                if total_tokens <= 0:
                    total_tokens = self._estimate_tokens(prompt, response)

                if (prompt_tokens or completion_tokens) and callable(getattr(self, "_calculate_cost_from_usage", None)):
                    cost = self._calculate_cost_from_usage(prompt_tokens, completion_tokens)
                else:
                    cost = self._calculate_cost(total_tokens)

                self.cost_tracker.record_call(total_tokens, cost)

                # 缓存结果
                self.cache.set(symbol, timeframe, indicators, analysis)

                analysis["cost"] = round(float(cost), 6)
                analysis["tokens"] = int(total_tokens)
                analysis["source"] = "api"

                # 附带预算状态（用于 GUI 告警展示）
                analysis["budget"] = budget_state

                return analysis
            else:
                return {
                    "status": "error",
                    "message": "API 调用失败",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "api_error"
                }
                
        except Exception as e:
            return {
                "status": "error",
                "message": str(e),
                "recommendation": "HOLD",
                "confidence": 0,
                "source": "exception"
            }
    
    def _should_analyze(self, symbol: str, timeframe: str) -> bool:
        """判断是否应该进行分析（成本控制）。

        策略：
        - 首次分析：立即进行
        - 后续分析：按 `config.min_interval_minutes` 限制频率（默认 5 分钟）

        备注：这里按 (symbol, timeframe) 作为 key，避免不同周期互相干扰。
        """
        now = datetime.now()
        key = (str(symbol), str(timeframe))

        if key not in self.last_analysis_time:
            self.last_analysis_time[key] = now
            return True

        min_int = int(getattr(self.config, "min_interval_minutes", 5) or 0)
        if min_int <= 0:
            self.last_analysis_time[key] = now
            return True

        time_diff = (now - self.last_analysis_time[key]).total_seconds() / 60
        if time_diff >= float(min_int):
            self.last_analysis_time[key] = now
            return True

        return False
    
    def _assess_signal_quality(self, indicators: Dict) -> float:
        """
        评估信号质量（0-1）
        
        只在信号质量高时才调用 AI，节省成本
        """
        quality = 0.5  # 基础分数
        
        rsi = indicators.get("rsi_14", 50)
        ema_9 = indicators.get("ema_9", 0)
        ema_20 = indicators.get("ema_20", 0)
        ema_50 = indicators.get("ema_50", 0)
        
        # RSI 在极端区域（超买/超卖）
        if rsi > 70 or rsi < 30:
            quality += 0.2
        
        # EMA 形成明确的趋势
        if ema_9 > ema_20 > ema_50:
            quality += 0.15  # 上升趋势
        elif ema_9 < ema_20 < ema_50:
            quality += 0.15  # 下降趋势
        
        # MACD 信号
        macd = indicators.get("macd", 0)
        if abs(macd) > 5:
            quality += 0.15
        
        return min(quality, 1.0)
    
    def _build_optimized_prompt(self, symbol: str, timeframe: str, 
                               ohlcv_data: List[List], indicators: Dict) -> str:
        """构建优化提示词（减少 token 使用）。

        关键点：**强制要求 JSON 输出**，避免解析失败后触发“看起来像写死的默认值”。
        """
        if not ohlcv_data:
            return ""

        latest = ohlcv_data[-1]
        close_price = float(latest[4])

        prompt = f"""你是量化交易分析师。请严格只输出 JSON（不要 markdown、不要额外解释）。
JSON 格式：{{"recommendation":"BUY|SELL|HOLD","confidence":0-100,"target_price":number|null,"stop_loss":number|null,"reason":string}}

symbol: {symbol}
timeframe: {timeframe}
price: {close_price:.4f}
ema9: {float(indicators.get('ema_9', 0)):.4f}
ema20: {float(indicators.get('ema_20', 0)):.4f}
ema50: {float(indicators.get('ema_50', 0)):.4f}
rsi14: {float(indicators.get('rsi_14', 0)):.2f}
macd: {float(indicators.get('macd', 0)):.4f}

只输出 JSON。"""

        return prompt
    
    def _post_chat_completions(self, payload: Dict, timeout: int = 30) -> Optional[Dict]:
        """调用 chat/completions，并返回原始 JSON（包含 usage 时可用于更准的成本统计）。"""
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=payload,
                timeout=timeout,
            )
            if response.status_code != 200:
                return None
            return response.json()
        except Exception as e:
            print(f"API 调用异常: {str(e)}")
            return None

    def _call_deepseek_api_ex(self, prompt: str, max_tokens: Optional[int] = None) -> Tuple[Optional[str], Dict]:
        """调用 DeepSeek API，并尽量返回 usage（用于精确计价）。

        Returns:
            (content, usage_dict)
        """
        mt = int(max_tokens) if max_tokens is not None else int(getattr(self.config, "max_output_tokens", 200) or 200)
        temp = float(getattr(self.config, "temperature", 0.5) or 0.5)

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是量化交易分析师。" 
                        "必须严格按用户要求输出 JSON（不要 markdown、不要额外解释）。" 
                        "confidence 必须为 0-100 的整数，不能把 50 当作默认值；" 
                        "只有在你判断确实完全中性时才允许输出 50。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": temp,
            "max_tokens": mt,
            "stream": False,
        }

        data = self._post_chat_completions(payload, timeout=30)
        if not data:
            return None, {}

        usage = {}
        try:
            usage = data.get("usage") or {}
        except Exception:
            usage = {}

        try:
            if "choices" in data and len(data["choices"]) > 0:
                content = data["choices"][0]["message"].get("content")
                return content, usage
        except Exception:
            return None, usage

        return None, usage

    def _call_deepseek_api(self, prompt: str) -> Optional[str]:
        """兼容旧接口：仅返回 content。"""
        content, _usage = self._call_deepseek_api_ex(prompt, max_tokens=int(getattr(self.config, "max_output_tokens", 200) or 200))
        return content
    
    def _estimate_tokens(self, prompt: str, response: str) -> int:
        """估算 token 使用量"""
        # 粗略估算：1 token ≈ 4 个字符
        input_tokens = len(prompt) // 4
        output_tokens = len(response) // 4
        return input_tokens + output_tokens
    
    def _calculate_cost(self, tokens: int) -> float:
        """计算成本（兼容旧逻辑）。

        注意：如果能拿到 API 返回的 usage（prompt/completion tokens），优先用
        `_calculate_cost_from_usage()` 会更准确。
        """
        # 简化计算：假设输入输出比例为 1:1
        cost = (tokens / 2) * self.INPUT_COST_PER_1K_TOKENS + (tokens / 2) * self.OUTPUT_COST_PER_1K_TOKENS
        return cost

    def _calculate_cost_from_usage(self, prompt_tokens: int, completion_tokens: int) -> float:
        """按输入/输出 token 分别计价（更准确）。"""
        return (prompt_tokens * self.INPUT_COST_PER_1K_TOKENS) + (completion_tokens * self.OUTPUT_COST_PER_1K_TOKENS)
    
    @staticmethod
    def _coerce_confidence(v: Any, default: int = 0) -> int:
        """把任意输入转成 0-100 的整数置信度。

        注意：不能用 `or 50` 这类写法，否则 `0` 会被误当成缺省。
        """
        if v is None:
            return int(default)
        try:
            c = int(float(v))
        except Exception:
            return int(default)
        return max(0, min(100, c))

    def _parse_analysis_response(self, response_text: str) -> Dict:
        """解析响应。

        优先解析 JSON；解析失败时再做保守的文本提取。
        """
        text = (response_text or "").strip()
        result: Dict[str, Any] = {
            "status": "success",
            "raw_analysis": text,
            "recommendation": "HOLD",
            "confidence": 0,
            "target_price": None,
            "stop_loss": None,
        }

        if not text:
            result["status"] = "error"
            result["message"] = "empty response"
            return result

        # 1) 尝试截取 JSON 并解析
        try:
            s = text
            start = s.find("{")
            end = s.rfind("}")
            if start != -1 and end != -1 and end > start:
                obj = json.loads(s[start : end + 1])
                if isinstance(obj, dict):
                    rec = (obj.get("recommendation") or obj.get("action") or "HOLD")
                    result["recommendation"] = str(rec).upper()
                    result["confidence"] = self._coerce_confidence(obj.get("confidence"), default=0)
                    result["target_price"] = obj.get("target_price")
                    result["stop_loss"] = obj.get("stop_loss")
                    if obj.get("reason") is not None:
                        result["reason"] = obj.get("reason")
                    return result
        except Exception:
            pass

        # 2) 文本兜底：提取 BUY/SELL/HOLD
        up = text.upper()
        if "BUY" in up:
            result["recommendation"] = "BUY"
        elif "SELL" in up:
            result["recommendation"] = "SELL"

        # 3) 文本兜底：优先找“confidence/置信度”附近的 0-100 数字
        try:
            m = re.search(r"(?:confidence|置信度)\D{0,10}(\d{1,3})", text, flags=re.IGNORECASE)
            if m:
                result["confidence"] = self._coerce_confidence(m.group(1), default=0)
                return result

            # 如果没找到关键词，就选第一个落在 0-100 的数字作为备选
            nums = []
            for x in re.findall(r"\d{1,3}", text):
                try:
                    n = int(x)
                except Exception:
                    continue
                if 0 <= n <= 100:
                    nums.append(n)
            if nums:
                result["confidence"] = self._coerce_confidence(nums[0], default=0)
        except Exception:
            pass

        return result
    
    def get_cost_summary(self) -> Dict:
        """获取成本统计（本地内存累计）。

        附带预算告警信息（若设置了 `daily_budget`）。
        """
        summary = self.cost_tracker.get_summary()
        budget = self._get_budget_state()
        summary["budget"] = budget
        summary["budget_exceeded"] = bool(budget.get("enabled") and budget.get("exceeded"))
        summary["budget_remaining"] = budget.get("remaining")
        summary["daily_budget"] = budget.get("daily_budget")
        summary["budget_enforcement"] = budget.get("enforcement")
        return summary

    def refresh_cost_summary_via_api(self, min_interval_seconds: int = 60) -> Tuple[bool, str]:
        """刷新成本统计（会发起一次真实 API 请求）。

        说明：本项目当前没有实现“查询账号历史用量/账单”的专用接口。
        因此这里采用一个 **低成本 ping**：调用一次 `chat/completions`（极短 prompt + 极小 max_tokens），
        并将 API 返回的 `usage` 计入 `CostTracker`，从而保证你点击“刷新”时：
        - 确实有网络请求
        - 成本统计会立即变化（+1 次调用、+token、+cost）

        Args:
            min_interval_seconds: 最小刷新间隔，防止连续点击导致额外花费。

        Returns:
            (did_call_api, message)
        """
        now = datetime.now()
        if self._last_stats_ping_at is not None:
            diff = (now - self._last_stats_ping_at).total_seconds()
            if diff < float(min_interval_seconds):
                return False, f"已在 {int(diff)} 秒内刷新过，为避免额外花费，本次未再次调用 API"

        budget_state = self._get_budget_state()
        if budget_state.get("enabled") and budget_state.get("exceeded") and budget_state.get("enforcement") == "block":
            return False, "已超出日预算（阻断模式），本次未调用 API" 

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": 0,
            "max_tokens": 1,
            "stream": False,
        }

        data = self._post_chat_completions(payload, timeout=15)
        if not data:
            return False, "刷新失败：API 未返回有效响应"

        usage = {}
        try:
            usage = data.get("usage") or {}
        except Exception:
            usage = {}

        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens) or 0)

        # 如果没有 usage，就用最保守的估算
        if total_tokens <= 0:
            try:
                content = ""
                if "choices" in data and len(data["choices"]) > 0:
                    content = data["choices"][0]["message"].get("content") or ""
                total_tokens = self._estimate_tokens("ping", content)
            except Exception:
                total_tokens = 1

        calc_from_usage = getattr(self, "_calculate_cost_from_usage", None)
        if callable(calc_from_usage) and (prompt_tokens or completion_tokens):
            cost = calc_from_usage(prompt_tokens, completion_tokens)
        else:
            cost = self._calculate_cost(total_tokens)

        self.cost_tracker.record_call(total_tokens, cost)
        self._last_stats_ping_at = now
        return True, "已调用 API 并更新本地统计（+1 次调用）"
    
    def test_connection(self) -> Tuple[bool, str]:
        """测试连接"""
        try:
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 10
            }
            
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=payload,
                timeout=10
            )
            
            if response.status_code == 200:
                return True, "✓ DeepSeek API 连接成功"
            else:
                return False, f"✗ API 错误: {response.status_code}"
                
        except Exception as e:
            return False, f"✗ 连接失败: {str(e)}"


class CostOptimizationStrategy:
    """成本优化策略"""
    
    @staticmethod
    def get_recommended_config(daily_budget: float) -> Dict:
        """
        根据日预算推荐配置
        
        Args:
            daily_budget: 每日预算（美元）
            
        Returns:
            推荐配置
        """
        # DeepSeek 平均成本：约 $0.0002 - $0.0005 per call（这里用保守值做估算）
        avg_cost_per_call = 0.0003
        max_daily_calls = int(max(0.0, float(daily_budget)) / avg_cost_per_call) if daily_budget else 0

        # 按预算档位给出更“像人类会用”的默认策略（与你朋友那份描述对齐）
        b = float(daily_budget)
        if b <= 0.5:
            recommended_cache_ttl = 30
            recommended_check_interval = 5
            signal_quality_threshold = 0.6
            recommended_symbols = 2
        elif b <= 1.0:
            recommended_cache_ttl = 30
            recommended_check_interval = 5
            signal_quality_threshold = 0.5
            recommended_symbols = 5
        elif b <= 2.0:
            recommended_cache_ttl = 20
            recommended_check_interval = 3
            signal_quality_threshold = 0.4
            recommended_symbols = 10
        else:
            recommended_cache_ttl = 15
            recommended_check_interval = 2
            signal_quality_threshold = 0.35
            recommended_symbols = max(10, max_daily_calls // 288)

        return {
            "daily_budget": float(daily_budget),
            "avg_cost_per_call": avg_cost_per_call,
            "max_daily_calls": int(max_daily_calls),
            "recommended_check_interval": int(recommended_check_interval),  # 分钟
            "recommended_cache_ttl": int(recommended_cache_ttl),  # 分钟
            "recommended_symbols": int(recommended_symbols),
            "signal_quality_threshold": float(signal_quality_threshold),
        }


if __name__ == "__main__":
    # 测试代码（不要在代码里硬编码真实密钥）
    import os

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit('请先设置环境变量：export DEEPSEEK_API_KEY="你的key"')

    analyzer = OptimizedDeepSeekAnalyzer(api_key)

    # 测试连接
    success, message = analyzer.test_connection()
    print(message)

    # 刷新（会调用一次 API ping 并计入统计）
    did_call, msg = analyzer.refresh_cost_summary_via_api()
    print(f"\n刷新结果: {msg} (did_call_api={did_call})")

    # 获取成本统计
    summary = analyzer.get_cost_summary()
    print(f"\n成本统计: {json.dumps(summary, indent=2, ensure_ascii=False)}")

    # 获取成本优化建议
    config = CostOptimizationStrategy.get_recommended_config(daily_budget=1.0)
    print(f"\n成本优化配置 (日预算 $1.0): {json.dumps(config, indent=2, ensure_ascii=False)}")