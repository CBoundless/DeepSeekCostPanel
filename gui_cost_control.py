#!/usr/bin/env python3
"""
成本控制配置面板
显示 DeepSeek API 调用成本和优化建议
"""

import tkinter as tk
from tkinter import ttk
import json


class CostControlPanel:
    """成本控制面板"""
    
    def __init__(self, parent_frame, analyzer):
        """
        初始化成本控制面板
        
        Args:
            parent_frame: 父容器
            analyzer: DeepSeek 分析器实例
        """
        self.analyzer = analyzer
        self.frame = ttk.Frame(parent_frame)
        self.frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        self._create_widgets()
    
    def _create_widgets(self):
        """创建控件"""
        
        # 标题
        title = ttk.Label(self.frame, text="💰 成本控制面板", 
                         font=("Arial", 12, "bold"))
        title.pack(anchor=tk.W, pady=(0, 10))
        
        # 成本统计区
        stats_frame = ttk.LabelFrame(self.frame, text="成本统计", padding=10)
        stats_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 总调用次数
        ttk.Label(stats_frame, text="总调用次数:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.total_calls_label = ttk.Label(stats_frame, text="0", 
                                          font=("Arial", 10, "bold"))
        self.total_calls_label.grid(row=0, column=1, sticky=tk.W, padx=(20, 0))
        
        # 总成本
        ttk.Label(stats_frame, text="总成本:").grid(row=0, column=2, sticky=tk.W, padx=(40, 0))
        self.total_cost_label = ttk.Label(stats_frame, text="$0.0000", 
                                         font=("Arial", 10, "bold"), foreground="green")
        self.total_cost_label.grid(row=0, column=3, sticky=tk.W, padx=(20, 0))
        
        # 今日调用
        ttk.Label(stats_frame, text="今日调用:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.today_calls_label = ttk.Label(stats_frame, text="0", 
                                          font=("Arial", 10, "bold"))
        self.today_calls_label.grid(row=1, column=1, sticky=tk.W, padx=(20, 0))
        
        # 今日成本
        ttk.Label(stats_frame, text="今日成本:").grid(row=1, column=2, sticky=tk.W, padx=(40, 0))
        self.today_cost_label = ttk.Label(stats_frame, text="$0.0000", 
                                         font=("Arial", 10, "bold"), foreground="blue")
        self.today_cost_label.grid(row=1, column=3, sticky=tk.W, padx=(20, 0))
        
        # 平均成本
        ttk.Label(stats_frame, text="平均成本/次:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.avg_cost_label = ttk.Label(stats_frame, text="$0.000000", 
                                       font=("Arial", 10, "bold"))
        self.avg_cost_label.grid(row=2, column=1, sticky=tk.W, padx=(20, 0))
        
        # 总 Token 数
        ttk.Label(stats_frame, text="总 Token 数:").grid(row=2, column=2, sticky=tk.W, padx=(40, 0))
        self.total_tokens_label = ttk.Label(stats_frame, text="0", 
                                           font=("Arial", 10, "bold"))
        self.total_tokens_label.grid(row=2, column=3, sticky=tk.W, padx=(20, 0))
        
        # 优化建议区
        config_frame = ttk.LabelFrame(self.frame, text="成本优化建议", padding=10)
        config_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 日预算输入
        ttk.Label(config_frame, text="设置日预算 ($):").pack(anchor=tk.W, pady=(0, 5))
        
        budget_input_frame = ttk.Frame(config_frame)
        budget_input_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.budget_entry = ttk.Entry(budget_input_frame, width=15)
        self.budget_entry.pack(side=tk.LEFT, padx=(0, 10))
        self.budget_entry.insert(0, "1.0")
        
        ttk.Button(budget_input_frame, text="计算优化配置", 
                  command=self._calculate_optimization).pack(side=tk.LEFT)
        
        # 优化建议显示
        self.config_text = tk.Text(config_frame, height=8, width=60, 
                                   bg="#f0f0f0", wrap=tk.WORD)
        self.config_text.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        # 更新按钮
        ttk.Button(self.frame, text="🔄 刷新成本统计", 
                  command=self.update_stats).pack(fill=tk.X, pady=(0, 10))
        
        # 初始化显示
        self.update_stats()
        self._calculate_optimization()
    
    def update_stats(self):
        """更新成本统计"""
        summary = self.analyzer.get_cost_summary()
        
        self.total_calls_label.config(text=str(summary["total_calls"]))
        self.total_cost_label.config(text=f"${summary['total_cost']:.4f}")
        self.today_calls_label.config(text=str(summary["today_calls"]))
        self.today_cost_label.config(text=f"${summary['today_cost']:.4f}")
        self.avg_cost_label.config(text=f"${summary['avg_cost_per_call']:.6f}")
        self.total_tokens_label.config(text=str(summary["total_tokens"]))
    
    def _calculate_optimization(self):
        """计算优化配置"""
        try:
            budget = float(self.budget_entry.get())
        except ValueError:
            self.config_text.config(state=tk.NORMAL)
            self.config_text.delete(1.0, tk.END)
            self.config_text.insert(tk.END, "❌ 请输入有效的预算金额")
            self.config_text.config(state=tk.DISABLED)
            return
        
        # 计算优化配置
        from deepseek_analyzer_optimized import CostOptimizationStrategy
        config = CostOptimizationStrategy.get_recommended_config(budget)
        
        # 格式化显示
        self.config_text.config(state=tk.NORMAL)
        self.config_text.delete(1.0, tk.END)
        
        config_text = f"""
📊 基于日预算 ${budget:.2f} 的优化建议：

✓ 平均成本/次: ${config['avg_cost_per_call']:.6f}
✓ 最大日调用次数: {config['max_daily_calls']} 次
✓ 推荐检查间隔: {config['recommended_check_interval']} 分钟
✓ 推荐缓存有效期: {config['recommended_cache_ttl']} 分钟
✓ 可监控交易对数: {config['recommended_symbols']} 个

💡 优化策略：
1. 启用缓存机制 - 相同行情数据不重复分析
2. 条件触发分析 - 只在高质量信号时调用 AI
3. 调用频率限制 - 每个交易对最多 5 分钟调用一次
4. 批量分析 - 一次调用分析多个交易对

⚠️  成本预估：
- 每日 {config['max_daily_calls']} 次调用 = ${budget:.2f}
- 每月约 ${budget * 30:.2f}
- 每年约 ${budget * 365:.2f}
"""
        
        self.config_text.insert(tk.END, config_text)
        self.config_text.config(state=tk.DISABLED)


if __name__ == "__main__":
    # 测试
    from deepseek_analyzer_optimized import OptimizedDeepSeekAnalyzer
    
    root = tk.Tk()
    root.title("成本控制面板")
    root.geometry("700x600")
    
    analyzer = OptimizedDeepSeekAnalyzer("sk-test-key")
    panel = CostControlPanel(root, analyzer)
    
    root.mainloop()