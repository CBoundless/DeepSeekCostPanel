#!/usr/bin/env python3
"""
优化版 DeepSeek AI 分析器 - 成本控制
支持缓存、条件触发、批量分析和成本统计
"""

import requests
import json
import hashlib
from typing import Dict, List, Optional, Tuple
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


class OptimizedDeepSeekAnalyzer:
    """优化版 DeepSeek 分析器"""

    # DeepSeek 定价（美元/1K tokens）
    INPUT_COST_PER_1K_TOKENS = 0.14 / 1000  # $0.14 per 1M input tokens
    OUTPUT_COST_PER_1K_TOKENS = 0.28 / 1000  # $0.28 per 1M output tokens

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com/v1"):
        """初始化优化版分析器。

        Args:
            api_key: DeepSeek API Key
            base_url: API 基础 URL
        """
        self.api_key = api_key
        self.base_url = base_url
        self.model = "deepseek-chat"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # 初始化缓存和成本追踪
        self.cache = AnalysisCache(ttl_minutes=30)
        self.cost_tracker = CostTracker()

        # 调用计数（用于条件触发）
        self.call_count = 0
        self.last_analysis_time = {}

        # 刷新统计时的“低成本 ping”防抖，避免频繁点击导致额外花费
        self._last_stats_ping_at: Optional[datetime] = None

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        s = (symbol or "").strip().upper().replace("/", "")
        return s

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        s = OptimizedDeepSeekAnalyzer._normalize_symbol(symbol)
        if not re.fullmatch(r"[A-Z0-9]{3,20}", s):
            raise ValueError(f"无效交易对：{symbol!r}（示例：BTCUSDT）")
        return s

    @staticmethod
    def _validate_timeframe(timeframe: str) -> str:
        tf = (timeframe or "").strip()
        if tf not in _ALLOWED_BINANCE_INTERVALS:
            raise ValueError(f"无效周期：{timeframe!r}（允许：{sorted(_ALLOWED_BINANCE_INTERVALS)}）")
        return tf

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
            if not self._should_analyze(symbol):
                return {
                    "status": "skipped",
                    "reason": "调用频率限制",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "frequency_limit"
                }
            
            # 3. 检查信号质量（只在高质量信号时分析）
            signal_quality = self._assess_signal_quality(indicators)
            if signal_quality < 0.5 and not force_analysis:
                return {
                    "status": "skipped",
                    "reason": f"信号质量低 ({signal_quality:.2f})",
                    "recommendation": "HOLD",
                    "confidence": 0,
                    "source": "low_signal_quality"
                }
            
            # 4. 构建优化的提示词（减少 token 使用）
            prompt = self._build_optimized_prompt(symbol, timeframe, ohlcv_data, indicators)
            
            # 5. 调用 API
            response = self._call_deepseek_api(prompt)
            
            if response:
                analysis = self._parse_analysis_response(response)

                # 记录成本（当前为本地估算；若未来需要更精确，可在调用处接入 API 返回的 usage）

                tokens = self._estimate_tokens(prompt, response)
                cost = self._calculate_cost(tokens)
                self.cost_tracker.record_call(tokens, cost)

                # 缓存结果
                self.cache.set(symbol, timeframe, indicators, analysis)

                analysis["cost"] = round(cost, 6)
                analysis["tokens"] = tokens
                analysis["source"] = "api"

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
    
    def _should_analyze(self, symbol: str) -> bool:
        """
        判断是否应该进行分析（成本控制）
        
        策略：
        - 首次分析：立即进行
        - 后续分析：每 5 分钟最多分析一次
        """
        now = datetime.now()
        
        if symbol not in self.last_analysis_time:
            self.last_analysis_time[symbol] = now
            return True
        
        time_diff = (now - self.last_analysis_time[symbol]).total_seconds() / 60
        
        if time_diff >= 5:  # 5 分钟调用一次
            self.last_analysis_time[symbol] = now
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
        """
        构建优化的提示词（减少 token 使用）
        
        使用简洁的格式和关键信息，而不是冗长的描述
        """
        if not ohlcv_data:
            return ""
        
        latest = ohlcv_data[-1]
        close_price = latest[4]
        
        # 简洁的市场数据格式
        prompt = f"""{symbol} {timeframe}
价格: ${close_price:.2f}
EMA: 9={indicators.get('ema_9', 0):.2f} 20={indicators.get('ema_20', 0):.2f} 50={indicators.get('ema_50', 0):.2f}
RSI: {indicators.get('rsi_14', 0):.2f}
MACD: {indicators.get('macd', 0):.2f}

分析并给出: 建议(BUY/SELL/HOLD) 置信度(0-100) 目标价 止损价"""
        
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

    def _call_deepseek_api(self, prompt: str) -> Optional[str]:
        """调用 DeepSeek API（用于实际分析）。"""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是量化交易分析师。简洁回复。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.5,  # 降低温度以减少 token 使用
            "max_tokens": 200,  # 限制输出 token
            "stream": False,
        }

        data = self._post_chat_completions(payload, timeout=30)
        if not data:
            return None
        try:
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"]
        except Exception:
            return None
        return None
    
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
    
    def _parse_analysis_response(self, response_text: str) -> Dict:
        """解析响应（简化版）"""
        result = {
            "status": "success",
            "raw_analysis": response_text,
            "recommendation": "HOLD",
            "confidence": 50,
            "target_price": None,
            "stop_loss": None
        }
        
        # 快速提取关键信息
        response_upper = response_text.upper()
        
        if "BUY" in response_upper:
            result["recommendation"] = "BUY"
        elif "SELL" in response_upper:
            result["recommendation"] = "SELL"
        
        # 提取数字
        import re
        numbers = re.findall(r'\d+', response_text)
        if numbers:
            result["confidence"] = min(int(numbers[0]), 100)
        
        return result
    
    def get_cost_summary(self) -> Dict:
        """获取成本统计（本地内存累计）。"""
        return self.cost_tracker.get_summary()

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
        # DeepSeek 平均成本：约 $0.0002 - $0.0005 per call
        avg_cost_per_call = 0.0003
        max_daily_calls = int(daily_budget / avg_cost_per_call)
        
        return {
            "daily_budget": daily_budget,
            "avg_cost_per_call": avg_cost_per_call,
            "max_daily_calls": max_daily_calls,
            "recommended_check_interval": 5,  # 分钟
            "recommended_cache_ttl": 30,  # 分钟
            "recommended_symbols": max(1, max_daily_calls // 288),  # 假设每天 288 个周期
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