from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from deepseek_analyzer_optimized import OptimizedDeepSeekAnalyzer


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOWED_AST_NODES = (
    ast.Expression,
    ast.Assign,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.IfExp,
    ast.Tuple,
    ast.List,
)


class IndicatorDslError(ValueError):
    pass


@dataclass
class IndicatorDslProfile:
    indicator_dsl: str = ""
    entry_rule: str = ""
    exit_rule: str = ""


@dataclass
class IndicatorDslState:
    history: Dict[str, List[float]] = field(default_factory=dict)
    last_values: Dict[str, float] = field(default_factory=dict)


@dataclass
class IndicatorDslResult:
    values: Dict[str, float]
    entry_signal: bool
    exit_signal: bool


class IndicatorDslEngine:
    def evaluate(
        self,
        profile: IndicatorDslProfile,
        ohlcv: List[List[Any]],
        state: Optional[IndicatorDslState] = None,
    ) -> IndicatorDslResult:
        if not profile.indicator_dsl.strip() and not profile.entry_rule.strip() and not profile.exit_rule.strip():
            return IndicatorDslResult(values={}, entry_signal=False, exit_signal=False)
        state = state or IndicatorDslState()
        env = self._build_base_env(ohlcv, state)
        values: Dict[str, float] = {}

        for raw_line in profile.indicator_dsl.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" not in line:
                raise IndicatorDslError(f"DSL 语句缺少 '='：{raw_line}")
            name, expr = [item.strip() for item in line.split("=", 1)]
            if not IDENTIFIER_RE.match(name):
                raise IndicatorDslError(f"非法指标名：{name}")
            value = self._safe_eval(expr, env)
            numeric_value = self._coerce_numeric(value, name)
            values[name] = numeric_value
            state.last_values[name] = numeric_value
            state.history.setdefault(name, []).append(numeric_value)
            env[name] = numeric_value
            env[f"{name}_series"] = list(state.history.get(name) or [])

        entry_signal = bool(self._safe_eval(profile.entry_rule, env)) if profile.entry_rule.strip() else False
        exit_signal = bool(self._safe_eval(profile.exit_rule, env)) if profile.exit_rule.strip() else False
        return IndicatorDslResult(values=values, entry_signal=entry_signal, exit_signal=exit_signal)

    def _build_base_env(self, ohlcv: List[List[Any]], state: IndicatorDslState) -> Dict[str, Any]:
        opens = [float(item[1]) for item in ohlcv if len(item) >= 6]
        highs = [float(item[2]) for item in ohlcv if len(item) >= 6]
        lows = [float(item[3]) for item in ohlcv if len(item) >= 6]
        closes = [float(item[4]) for item in ohlcv if len(item) >= 6]
        volumes = [float(item[5]) for item in ohlcv if len(item) >= 6]
        base_indicators = OptimizedDeepSeekAnalyzer.build_indicators_from_ohlcv(ohlcv)
        env: Dict[str, Any] = {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "open_now": opens[-1] if opens else 0.0,
            "high_now": highs[-1] if highs else 0.0,
            "low_now": lows[-1] if lows else 0.0,
            "close_now": closes[-1] if closes else 0.0,
            "volume_now": volumes[-1] if volumes else 0.0,
            "EMA": self._fn_ema,
            "SMA": self._fn_sma,
            "RSI": self._fn_rsi,
            "CHANGE": self._fn_change,
            "VOL": self._fn_volatility,
            "REF": self._fn_ref,
            "MAX": self._fn_max,
            "MIN": self._fn_min,
            "ABS": abs,
            "IF": self._fn_if,
            "CROSSOVER": self._fn_crossover,
            "CROSSUNDER": self._fn_crossunder,
        }
        env.update(base_indicators)
        for key, value in state.last_values.items():
            env[key] = value
            env[f"{key}_series"] = list(state.history.get(key) or [])
        return env

    def _safe_eval(self, expression: str, env: Dict[str, Any]) -> Any:
        text = (expression or "").strip()
        if not text:
            return 0
        try:
            parsed = ast.parse(text, mode="eval")
        except SyntaxError as exc:
            raise IndicatorDslError(f"DSL 语法错误：{exc}") from exc
        for node in ast.walk(parsed):
            if not isinstance(node, ALLOWED_AST_NODES):
                raise IndicatorDslError(f"DSL 包含不允许的语法：{type(node).__name__}")
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name):
                    raise IndicatorDslError("函数调用仅允许使用白名单函数")
                if node.func.id not in env or not callable(env[node.func.id]):
                    raise IndicatorDslError(f"不支持的函数：{node.func.id}")
        return eval(compile(parsed, "<indicator-dsl>", "eval"), {"__builtins__": {}}, env)

    @staticmethod
    def _coerce_numeric(value: Any, name: str) -> float:
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        try:
            return float(value)
        except Exception as exc:
            raise IndicatorDslError(f"指标 {name} 结果不是数值：{value!r}") from exc

    @staticmethod
    def _series(value: Any) -> List[float]:
        if isinstance(value, list):
            return [float(item) for item in value]
        return [float(value)]

    def _fn_ema(self, series: Any, period: Any) -> float:
        values = self._series(series)
        lookback = max(1, int(float(period)))
        if len(values) < lookback:
            return float(values[-1]) if values else 0.0
        return float(OptimizedDeepSeekAnalyzer.build_indicators_from_ohlcv([[0, 0, 0, 0, value, 0] for value in values]).get(f"ema_{lookback}") or values[-1])

    def _fn_sma(self, series: Any, period: Any) -> float:
        values = self._series(series)
        lookback = max(1, int(float(period)))
        window = values[-lookback:] if len(values) >= lookback else values
        return sum(window) / max(1, len(window))

    def _fn_rsi(self, series: Any, period: Any) -> float:
        values = self._series(series)
        lookback = max(2, int(float(period)))
        fake_ohlcv = [[index, 0, 0, 0, value, 0] for index, value in enumerate(values[-(lookback * 4):], start=1)]
        indicators = OptimizedDeepSeekAnalyzer.build_indicators_from_ohlcv(fake_ohlcv)
        return float(indicators.get("rsi_14") or indicators.get(f"rsi_{lookback}") or 50.0)

    def _fn_change(self, series: Any, lookback: Any = 1) -> float:
        values = self._series(series)
        step = max(1, int(float(lookback)))
        if len(values) <= step:
            return 0.0
        base = float(values[-step - 1])
        if base == 0:
            return 0.0
        return (float(values[-1]) - base) / base

    def _fn_volatility(self, series: Any, lookback: Any = 20) -> float:
        values = self._series(series)
        step = max(2, int(float(lookback)))
        if len(values) <= step:
            return 0.0
        recent = values[-(step + 1):]
        returns: List[float] = []
        for prev, curr in zip(recent, recent[1:]):
            if float(prev) <= 0:
                continue
            returns.append((float(curr) - float(prev)) / float(prev))
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((item - mean) ** 2 for item in returns) / len(returns)
        return variance ** 0.5

    def _fn_ref(self, series: Any, steps: Any = 1) -> float:
        values = self._series(series)
        shift = max(0, int(float(steps)))
        if len(values) <= shift:
            return float(values[0]) if values else 0.0
        return float(values[-shift - 1])

    def _fn_max(self, *values: Any) -> float:
        expanded: List[float] = []
        for item in values:
            expanded.extend(self._series(item))
        return max(expanded) if expanded else 0.0

    def _fn_min(self, *values: Any) -> float:
        expanded: List[float] = []
        for item in values:
            expanded.extend(self._series(item))
        return min(expanded) if expanded else 0.0

    @staticmethod
    def _fn_if(condition: Any, when_true: Any, when_false: Any) -> Any:
        return when_true if bool(condition) else when_false

    def _fn_crossover(self, left: Any, right: Any) -> bool:
        left_series = self._series(left)
        right_series = self._series(right)
        if len(left_series) < 2 or len(right_series) < 2:
            return float(left_series[-1]) > float(right_series[-1])
        return left_series[-2] <= right_series[-2] and left_series[-1] > right_series[-1]

    def _fn_crossunder(self, left: Any, right: Any) -> bool:
        left_series = self._series(left)
        right_series = self._series(right)
        if len(left_series) < 2 or len(right_series) < 2:
            return float(left_series[-1]) < float(right_series[-1])
        return left_series[-2] >= right_series[-2] and left_series[-1] < right_series[-1]


indicator_dsl_engine = IndicatorDslEngine()
