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
        """
        初始化优化版分析器
        
        Args:
            api_key: DeepSeek API Key
            base_url: API 基础 URL
        """
        self.api_key = api_key
        self.base_url = base_url
        self.model = "deepseek-chat"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        # 初始化缓存和成本追踪
        self.cache = AnalysisCache(ttl_minutes=30)
        self.cost_tracker = CostTracker()
        
        # 调用计数（用于条件触发）
        self.call_count = 0
        self.last_analysis_time = {}
        
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
                
                # 记录成本
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
    
    def _call_deepseek_api(self, prompt: str) -> Optional[str]:
        """调用 DeepSeek API"""
        try:
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是量化交易分析师。简洁回复。"
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "temperature": 0.5,  # 降低温度以减少 token 使用
                "max_tokens": 200,   # 限制输出 token
                "stream": False
            }
            
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                if "choices" in data and len(data["choices"]) > 0:
                    return data["choices"][0]["message"]["content"]
            
        except Exception as e:
            print(f"API 调用异常: {str(e)}")
        
        return None
    
    def _estimate_tokens(self, prompt: str, response: str) -> int:
        """估算 token 使用量"""
        # 粗略估算：1 token ≈ 4 个字符
        input_tokens = len(prompt) // 4
        output_tokens = len(response) // 4
        return input_tokens + output_tokens
    
    def _calculate_cost(self, tokens: int) -> float:
        """计算成本"""
        # 简化计算：假设输入输出比例为 1:1
        cost = (tokens / 2) * self.INPUT_COST_PER_1K_TOKENS + \
               (tokens / 2) * self.OUTPUT_COST_PER_1K_TOKENS
        return cost
    
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
        """获取成本统计"""
        return self.cost_tracker.get_summary()
    
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
    # 测试代码
    api_key = "sk-your-api-key"
    analyzer = OptimizedDeepSeekAnalyzer(api_key)
    
    # 测试连接
    success, message = analyzer.test_connection()
    print(message)
    
    # 获取成本统计
    summary = analyzer.get_cost_summary()
    print(f"\n成本统计: {json.dumps(summary, indent=2, ensure_ascii=False)}")
    
    # 获取成本优化建议
    config = CostOptimizationStrategy.get_recommended_config(daily_budget=1.0)
    print(f"\n成本优化配置 (日预算 $1.0): {json.dumps(config, indent=2, ensure_ascii=False)}")