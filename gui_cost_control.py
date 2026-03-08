#!/usr/bin/env python3
"""成本控制配置面板（GUI）

你这边出现过多次“窗口空白/按钮看不见/透明”的根因：
- macOS + Tk 8.5 下，`ttk` 的 `aqua` 主题会使用系统颜色名（例如 `systemWindowBody`），在某些主题/显示设置下可能渲染异常。

本文件策略：
- **默认仍使用最开始的 ttk 布局**，但启动时强制切到 `clam` 主题（显式颜色，稳定可见）
- 从环境变量读取 API Key（不再硬编码 `sk-test-key`）
- 如仍遇到不可见，可用 `FORCE_FALLBACK_UI=1` 强制走兜底 UI

环境变量：
- `DEEPSEEK_API_KEY`（推荐） / `deep_api_key` / `DEEP_API_KEY`
- `FORCE_FALLBACK_UI=1`：强制兜底 UI（不依赖 ttk 渲染）
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import simpledialog, ttk


def _get_api_key() -> str | None:
    return os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("deep_api_key") or os.environ.get("DEEP_API_KEY")


def _is_macos_dark_mode() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import subprocess

        p = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return p.returncode == 0 and "Dark" in (p.stdout or "")
    except Exception:
        return False


def _should_use_fallback_ui() -> bool:
    """决定是否走兜底 UI。

    结论（基于你这台机器的现象）：macOS + Tk 8.5（<8.6）下，ttk 在深/浅色模式都有概率出现
    控件透明/不可见/空白窗。

    因此默认策略更保守：
    - 只要是 macOS 且 Tk<8.6，就默认走兜底 UI（保证你能看到东西）
    - 如你想强制使用原版 ttk UI：设置 `FORCE_TTK_UI=1`
    """
    if os.environ.get("FORCE_FALLBACK_UI") == "1":
        return True
    if os.environ.get("FORCE_TTK_UI") == "1":
        return False

    if sys.platform == "darwin" and float(getattr(tk, "TkVersion", 0.0)) < 8.6:
        return True

    # 其他平台/更高 Tk 版本：默认原版 UI
    return False


class CostControlPanel:
    """最开始的原版样式（ttk 布局）"""

    def __init__(self, parent_frame, analyzer):
        self.analyzer = analyzer
        self.frame = ttk.Frame(parent_frame)
        self.frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self._create_widgets()

    def _create_widgets(self):
        # 标题
        title = ttk.Label(self.frame, text="💰 成本控制面板", font=("Arial", 12, "bold"))
        title.pack(anchor=tk.W, pady=(0, 10))

        # 成本统计区
        stats_frame = ttk.LabelFrame(self.frame, text="成本统计", padding=10)
        stats_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(stats_frame, text="总调用次数:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.total_calls_label = ttk.Label(stats_frame, text="0", font=("Arial", 10, "bold"))
        self.total_calls_label.grid(row=0, column=1, sticky=tk.W, padx=(20, 0))

        ttk.Label(stats_frame, text="总成本:").grid(row=0, column=2, sticky=tk.W, padx=(40, 0))
        self.total_cost_label = ttk.Label(stats_frame, text="$0.0000", font=("Arial", 10, "bold"), foreground="green")
        self.total_cost_label.grid(row=0, column=3, sticky=tk.W, padx=(20, 0))

        ttk.Label(stats_frame, text="今日调用:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.today_calls_label = ttk.Label(stats_frame, text="0", font=("Arial", 10, "bold"))
        self.today_calls_label.grid(row=1, column=1, sticky=tk.W, padx=(20, 0))

        ttk.Label(stats_frame, text="今日成本:").grid(row=1, column=2, sticky=tk.W, padx=(40, 0))
        self.today_cost_label = ttk.Label(stats_frame, text="$0.0000", font=("Arial", 10, "bold"), foreground="blue")
        self.today_cost_label.grid(row=1, column=3, sticky=tk.W, padx=(20, 0))

        ttk.Label(stats_frame, text="平均成本/次:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.avg_cost_label = ttk.Label(stats_frame, text="$0.000000", font=("Arial", 10, "bold"))
        self.avg_cost_label.grid(row=2, column=1, sticky=tk.W, padx=(20, 0))

        ttk.Label(stats_frame, text="总 Token 数:").grid(row=2, column=2, sticky=tk.W, padx=(40, 0))
        self.total_tokens_label = ttk.Label(stats_frame, text="0", font=("Arial", 10, "bold"))
        self.total_tokens_label.grid(row=2, column=3, sticky=tk.W, padx=(20, 0))

        # 优化建议区
        config_frame = ttk.LabelFrame(self.frame, text="成本优化建议", padding=10)
        config_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(config_frame, text="设置日预算 ($):").pack(anchor=tk.W, pady=(0, 5))

        budget_input_frame = ttk.Frame(config_frame)
        budget_input_frame.pack(fill=tk.X, pady=(0, 10))

        self.budget_entry = ttk.Entry(budget_input_frame, width=15)
        self.budget_entry.pack(side=tk.LEFT, padx=(0, 10))
        self.budget_entry.insert(0, "1.0")

        ttk.Button(budget_input_frame, text="计算优化配置", command=self._calculate_optimization).pack(side=tk.LEFT)
        ttk.Button(budget_input_frame, text="📈 市场分析", command=self.run_market_analysis).pack(side=tk.LEFT, padx=(8, 0))

        self.config_text = tk.Text(config_frame, height=8, width=60, bg="#f0f0f0", wrap=tk.WORD)
        self.config_text.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        ttk.Button(self.frame, text="🔄 刷新成本统计（调用 API）", command=self.refresh_stats).pack(fill=tk.X, pady=(0, 6))
        self.stats_status = ttk.Label(self.frame, text="")
        self.stats_status.pack(anchor=tk.W, pady=(0, 10))

        self.update_stats()
        self._calculate_optimization()

    def run_market_analysis(self):
        """在原版 ttk UI 下触发一次市场分析（会调用 API）。"""
        try:
            symbol = self.budget_entry.get().strip()  # 复用输入框不合适，这里改为弹窗
            symbol = simpledialog.askstring("市场分析", "请输入交易对（例如 BTCUSDT）：", parent=self.frame.winfo_toplevel())
            if not symbol:
                return
            timeframe = simpledialog.askstring("市场分析", "请输入周期（例如 1m/1h/1d）：", parent=self.frame.winfo_toplevel())
            if not timeframe:
                return

            result = self.analyzer.analyze_market_from_binance(symbol=symbol, timeframe=timeframe, limit=200, force_analysis=True)
            txt = (
                f"📈 市场分析结果（{symbol.upper()} {timeframe} / source={result.get('source')}）\n\n"
                f"recommendation: {result.get('recommendation')}\n"
                f"confidence: {result.get('confidence')}\n"
                f"status: {result.get('status')}\n\n"
                f"raw_analysis:\n{result.get('raw_analysis') or ''}"
            )

            self.config_text.config(state=tk.NORMAL)
            self.config_text.delete(1.0, tk.END)
            self.config_text.insert(tk.END, txt)
            self.config_text.config(state=tk.DISABLED)

            self.update_stats()
        except Exception as e:
            self.config_text.config(state=tk.NORMAL)
            self.config_text.delete(1.0, tk.END)
            self.config_text.insert(tk.END, f"❌ 市场分析失败：{e}")
            self.config_text.config(state=tk.DISABLED)

    def refresh_stats(self):
        # 刷新时真实调用一次 API（低成本 ping），并将 usage 计入本地统计
        try:
            did_call, msg = self.analyzer.refresh_cost_summary_via_api()
        except Exception as e:
            did_call, msg = False, f"刷新失败：{e}"

        self.update_stats()
        try:
            self.stats_status.config(text=msg)
        except Exception:
            pass

    def update_stats(self):
        summary = self.analyzer.get_cost_summary()
        self.total_calls_label.config(text=str(summary["total_calls"]))
        self.total_cost_label.config(text=f"${summary['total_cost']:.4f}")
        self.today_calls_label.config(text=str(summary["today_calls"]))
        self.today_cost_label.config(text=f"${summary['today_cost']:.4f}")
        self.avg_cost_label.config(text=f"${summary['avg_cost_per_call']:.6f}")
        self.total_tokens_label.config(text=str(summary["total_tokens"]))

    def _calculate_optimization(self):
        try:
            budget = float(self.budget_entry.get())
        except ValueError:
            self.config_text.config(state=tk.NORMAL)
            self.config_text.delete(1.0, tk.END)
            self.config_text.insert(tk.END, "❌ 请输入有效的预算金额")
            self.config_text.config(state=tk.DISABLED)
            return

        from deepseek_analyzer_optimized import CostOptimizationStrategy

        config = CostOptimizationStrategy.get_recommended_config(budget)

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


class FallbackPanel:
    """兜底 UI：尽量不依赖 ttk。

    备注：在 macOS Tk 8.5 下，`ttk` 的渲染问题更常见；兜底 UI 尽量用原生 Tk 控件。
    """

    def __init__(self, parent: tk.Tk, analyzer):
        self.root = parent
        self.analyzer = analyzer
        self.budget = 1.0

        # 输出区后端：默认用 Canvas（你之前那版能显示就是这套），可用环境变量切到 listbox。
        self._output_backend = (os.environ.get("FALLBACK_OUTPUT_BACKEND") or "canvas").strip().lower()
        # Canvas 行控件：默认用 Button（Label 在你环境里可能不可见）
        self._output_widget_kind = (os.environ.get("FALLBACK_OUTPUT_WIDGET") or "button").strip().lower()

        # 兜底配色：避免深色模式下“黑底黑字”
        if _is_macos_dark_mode():
            self.bg = "#1f1f1f"
            self.fg = "#f2f2f2"
            self.hint_bg = "#2b2b2b"
        else:
            self.bg = "#ffffff"
            self.fg = "#111111"
            self.hint_bg = "#f5f5f5"

        self.frame = tk.Frame(parent, bg=self.bg)
        self.frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        self._create_widgets()
        self.update_stats()
        self._calculate_optimization()

    def _set_status(self, msg: str):
        try:
            self.status.configure(text=msg)
        except Exception:
            pass

    def _as_label_button(self, parent, text: str, bold: bool = False):
        font = ("Arial", 11, "bold") if bold else ("Arial", 11)
        b = tk.Button(
            parent,
            text=text,
            font=font,
            relief="flat",
            bd=0,
            highlightthickness=0,
            takefocus=0,
            anchor="w",
            justify="left",
            command=lambda: None,
        )
        # 显式设置前景/背景色，避免某些系统主题下“看起来像空白”。
        try:
            if getattr(self, "bg", None) is not None:
                b.configure(bg=self.bg)
            if getattr(self, "fg", None) is not None:
                b.configure(fg=self.fg)
            b.configure(activebackground=b.cget("background"), activeforeground=b.cget("foreground"))
        except Exception:
            pass
        return b

    def _create_widgets(self):
        self._as_label_button(self.frame, "成本控制面板（兜底显示）", bold=True).pack(anchor="w", pady=(0, 10))

        stats = tk.Frame(self.frame, bg=self.bg)
        stats.pack(fill=tk.X, pady=(0, 12))
        self._as_label_button(stats, "成本统计", bold=True).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        def pair(row, c0, v0, c1, v1):
            self._as_label_button(stats, c0).grid(row=row, column=0, sticky="w", padx=(0, 6), pady=2)
            v0.grid(row=row, column=1, sticky="w", padx=(0, 18), pady=2)
            self._as_label_button(stats, c1).grid(row=row, column=2, sticky="w", padx=(0, 6), pady=2)
            v1.grid(row=row, column=3, sticky="w", pady=2)

        self.total_calls = self._as_label_button(stats, "0", bold=True)
        self.total_cost = self._as_label_button(stats, "$0.0000", bold=True)
        self.today_calls = self._as_label_button(stats, "0", bold=True)
        self.today_cost = self._as_label_button(stats, "$0.0000", bold=True)
        self.avg_cost = self._as_label_button(stats, "$0.000000", bold=True)
        self.total_tokens = self._as_label_button(stats, "0", bold=True)

        pair(1, "总调用次数:", self.total_calls, "总成本:", self.total_cost)
        pair(2, "今日调用:", self.today_calls, "今日成本:", self.today_cost)
        pair(3, "平均成本/次:", self.avg_cost, "总 Token 数:", self.total_tokens)

        cfg = tk.Frame(self.frame, bg=self.bg)
        cfg.pack(fill=tk.BOTH, expand=True, pady=(0, 12))
        self._as_label_button(cfg, "成本优化建议", bold=True).pack(anchor="w", pady=(0, 6))

        row = tk.Frame(cfg, bg=self.bg)
        row.pack(fill=tk.X, pady=(0, 8))

        # 重要：在 macOS Tk 8.5 深色模式下，Entry/Text 的文字可能不可见。
        # 因此预算值和输出文本都用 Button 渲染，保证“必可见”。
        self._as_label_button(row, "设置日预算 ($):").pack(side=tk.LEFT)
        self.budget_value = self._as_label_button(row, f"{self.budget:.2f}", bold=True)
        self.budget_value.pack(side=tk.LEFT, padx=(6, 12))

        tk.Button(row, text="修改预算", command=self._on_prompt_budget).pack(side=tk.LEFT)
        tk.Button(row, text="计算优化配置", command=self._on_calculate).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(row, text="📈 市场分析", command=self._on_market_analyze).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(row, text="🔄 刷新成本统计", command=self._on_refresh).pack(side=tk.LEFT, padx=(8, 0))

        # 状态提示：也用 Button（Label 在你环境里可能不可见）
        self.status = self._as_label_button(row, "就绪")
        self.status.pack(side=tk.LEFT, padx=(12, 0), fill=tk.X, expand=True)

        # 输出区：默认用 Listbox + Scrollbar（在你这套 Tk 环境里比 Label/Text 更稳），必要时回退到 Canvas。
        # 说明：你这台机器上曾出现过 grid 相关的“控件存在但不绘制/尺寸异常”，这里统一用 pack。
        out_frame = tk.Frame(cfg, bg=self.bg)
        out_frame.pack(fill=tk.BOTH, expand=True)

        # 输出区标题栏：用 Button（Label 在你环境里可能不可见）
        self.output_header = self._as_label_button(out_frame, "输出区", bold=True)
        self.output_header.configure(bg=self.hint_bg)
        self.output_header.pack(fill=tk.X, pady=(0, 6))

        out_body = tk.Frame(out_frame, bg=self.hint_bg, bd=1, relief="groove")
        out_body.pack(fill=tk.BOTH, expand=True)

        self.output_backend = None
        self.output_listbox = None
        self.output_canvas = None
        self.output_inner = None
        self.output_window = None
        self.output_scroll = None

        if self._output_backend not in ("canvas",):
            try:
                self.output_listbox = tk.Listbox(
                    out_body,
                    bg=self.hint_bg,
                    fg=self.fg,
                    highlightthickness=0,
                    bd=0,
                    activestyle="none",
                    selectbackground=self.hint_bg,
                    selectforeground=self.fg,
                )
                self.output_scroll = tk.Scrollbar(out_body, orient="vertical", command=self.output_listbox.yview)
                self.output_listbox.configure(yscrollcommand=self.output_scroll.set)

                self.output_scroll.pack(side=tk.RIGHT, fill=tk.Y)
                self.output_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
                self.output_backend = "listbox"
                try:
                    self.output_header.configure(text="输出区（backend=listbox）")
                except Exception:
                    pass
            except Exception as e:
                self.output_backend = None
                self.output_listbox = None
                self._set_status(f"Listbox 输出区初始化失败，回退 Canvas：{e}")

        if self.output_backend != "listbox":
            # Canvas 后端：兼容回退。
            self.output_canvas = tk.Canvas(out_body, highlightthickness=0, bd=0, relief="flat", bg=self.hint_bg)
            self.output_scroll = tk.Scrollbar(out_body, orient="vertical", command=self.output_canvas.yview)
            self.output_canvas.configure(yscrollcommand=self.output_scroll.set)

            self.output_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self.output_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            self.output_inner = tk.Frame(self.output_canvas, bg=self.hint_bg)
            self.output_window = self.output_canvas.create_window((0, 0), window=self.output_inner, anchor="nw")
            self._output_line_widgets = []

            def _sync_width(_evt=None):
                try:
                    w = int(self.output_canvas.winfo_width() or 0)
                    self.output_canvas.itemconfigure(self.output_window, width=w)

                    # 仅更新 wraplength，不重建内容（避免滚动后花屏/错乱）
                    wrap_w = max(200, w - 30)
                    for ww in list(getattr(self, "_output_line_widgets", []) or []):
                        try:
                            ww.configure(wraplength=wrap_w)
                        except Exception:
                            pass
                except Exception:
                    pass

            def _sync_scrollregion(_evt=None):
                try:
                    self.output_canvas.configure(scrollregion=self.output_canvas.bbox("all"))
                except Exception:
                    pass

            self.output_canvas.bind("<Configure>", _sync_width)
            self.output_inner.bind("<Configure>", _sync_scrollregion)

            # 绑定滚轮/触控板滚动
            self._bind_output_mousewheel()
            self.output_backend = "canvas"
            try:
                self.output_header.configure(text="输出区（backend=canvas）")
            except Exception:
                pass

        # 初始占位
        self._render_output("（点击“计算优化配置”或“市场分析”生成输出）")

        # 底部占位
        bottom = tk.Frame(self.frame, bg=self.bg)
        bottom.pack(fill=tk.X)

    def _bind_output_mousewheel(self):
        # Canvas 后端：为了让滚轮在子控件（每行 Button）上也生效，这里仍用 bind_all，
        # 但只在鼠标进入输出区时启用，并且不做重绘，避免错乱。
        if not getattr(self, "output_canvas", None):
            return

        def _on_enter(_evt=None):
            try:
                self.output_canvas.focus_set()
            except Exception:
                pass
            self.root.bind_all("<MouseWheel>", self._on_output_mousewheel)
            self.root.bind_all("<Button-4>", self._on_output_mousewheel)
            self.root.bind_all("<Button-5>", self._on_output_mousewheel)

        def _on_leave(_evt=None):
            try:
                self.root.unbind_all("<MouseWheel>")
                self.root.unbind_all("<Button-4>")
                self.root.unbind_all("<Button-5>")
            except Exception:
                pass

        self.output_canvas.bind("<Enter>", _on_enter)
        self.output_canvas.bind("<Leave>", _on_leave)

    def _on_output_mousewheel(self, event):
        try:
            # Linux(X11) 传统滚轮事件
            if getattr(event, "num", None) == 4:
                self.output_canvas.yview_scroll(-3, "units")
                return "break"
            if getattr(event, "num", None) == 5:
                self.output_canvas.yview_scroll(3, "units")
                return "break"

            delta = int(getattr(event, "delta", 0) or 0)
            if delta == 0:
                return "break"

            # macOS 触控板 delta 往往比较小；Windows 常见 120 的倍数。
            steps = max(1, abs(delta) // 120) if abs(delta) >= 120 else 1
            direction = -1 if delta > 0 else 1
            self.output_canvas.yview_scroll(direction * steps * 3, "units")
            return "break"
        except Exception:
            return "break"

    def _render_output(self, txt: str, keep_view: bool = False):
        """渲染输出区。

        - 默认后端：Text（跨平台稳定）
        - 兼容回退：Canvas + 多行 Label/Button（可用 `FALLBACK_OUTPUT_BACKEND=canvas` 强制）
        - 行控件：`FALLBACK_OUTPUT_WIDGET=button` 可强制用 Button
        """
        self._last_output = txt

        # 1) Listbox 后端
        if getattr(self, "output_backend", None) == "listbox" and getattr(self, "output_listbox", None) is not None:
            try:
                old_view = None
                if keep_view:
                    try:
                        old_view = self.output_listbox.yview()
                    except Exception:
                        old_view = None

                self.output_listbox.delete(0, tk.END)

                # 尝试按当前宽度做一个轻量换行（Listbox 不支持 wrap）。
                try:
                    import textwrap

                    w_px = int(self.output_listbox.winfo_width() or 0)
                    max_chars = max(60, int(w_px / 7)) if w_px > 0 else 120
                    for line in (txt or "").splitlines() or [""]:
                        if not line:
                            self.output_listbox.insert(tk.END, "")
                            continue
                        chunks = textwrap.wrap(line, width=max_chars, replace_whitespace=False, drop_whitespace=False)
                        for c in chunks or [line]:
                            self.output_listbox.insert(tk.END, c)
                except Exception:
                    for line in (txt or "").splitlines() or [""]:
                        self.output_listbox.insert(tk.END, line)

                if old_view and keep_view:
                    self.output_listbox.yview_moveto(old_view[0])
                else:
                    self.output_listbox.yview_moveto(0.0)
                return
            except Exception as e:
                try:
                    self._set_status(f"Listbox 输出渲染失败，回退 Canvas：{e}")
                except Exception:
                    pass
                self.output_backend = "canvas"

        # 2) Canvas 后端
        if not getattr(self, "output_canvas", None) or not getattr(self, "output_inner", None):
            return

        old_view = None
        if keep_view:
            try:
                old_view = self.output_canvas.yview()
            except Exception:
                old_view = None

        for w in list(self.output_inner.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass

        wrap_w = max(200, int(self.output_canvas.winfo_width() or 0) - 30)
        lines = (txt or "").splitlines() or [""]

        # 你这台机器上 Label 可能不可见，因此 Canvas 行控件默认用 Button。
        use_button = True if getattr(self, "_output_widget_kind", "button") == "button" else False
        self._output_line_widgets = []

        for line in lines:
            show = line if line.strip() else " "
            if use_button:
                w = tk.Button(
                    self.output_inner,
                    text=show,
                    relief="flat",
                    bd=0,
                    highlightthickness=0,
                    takefocus=0,
                    anchor="w",
                    justify="left",
                    wraplength=wrap_w,
                    bg=self.hint_bg,
                    fg=self.fg,
                    activebackground=self.hint_bg,
                    activeforeground=self.fg,
                    padx=6,
                    pady=2,
                    command=lambda: None,
                )
            else:
                w = tk.Label(
                    self.output_inner,
                    text=show,
                    anchor="w",
                    justify="left",
                    wraplength=wrap_w,
                    bg=self.hint_bg,
                    fg=self.fg,
                    padx=6,
                    pady=2,
                )
            w.pack(fill=tk.X, anchor="w")
            self._output_line_widgets.append(w)

        try:
            self.output_canvas.update_idletasks()
            self.output_canvas.configure(scrollregion=self.output_canvas.bbox("all"))
            if old_view and keep_view:
                self.output_canvas.yview_moveto(old_view[0])
            else:
                self.output_canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _on_prompt_budget(self):
        """修改预算。

        你当前环境（macOS + Tk 8.5 深色模式）下，Tk 自带 `simpledialog` 的输入框/默认值也可能看不见。
        所以：
        - macOS 上优先用系统 `osascript` 对话框获取输入（最稳）
        - 失败再回退到 Tk 的 `askfloat`
        """
        # 1) macOS：用系统弹窗（可见、可靠）
        if sys.platform == "darwin":
            try:
                import re
                import subprocess

                script = (
                    'display dialog "请输入日预算（美元）：" '
                    f'default answer "{self.budget:.2f}" buttons {{"取消","确定"}} default button "确定"'
                )
                p = subprocess.run(
                    ["osascript", "-e", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if p.returncode != 0:
                    self._set_status("已取消修改预算")
                    return

                m = re.search(r"text returned:(.*)$", (p.stdout or "").strip())
                raw = (m.group(1).strip() if m else (p.stdout or "").strip())
                v = float(raw)

                self.budget = v
                try:
                    self.budget_value.configure(text=f"{self.budget:.2f}")
                except Exception:
                    pass
                self._set_status(f"预算已更新：{self.budget:.2f}")
                self._calculate_optimization()
                return
            except Exception as e:
                # 继续回退 Tk 弹窗
                self._set_status(f"系统弹窗失败，改用 Tk 弹窗：{e}")

        # 2) 回退：Tk 弹窗
        try:
            v = simpledialog.askfloat(
                "设置预算",
                "请输入日预算（美元）：",
                parent=self.root,
                initialvalue=float(self.budget),
                minvalue=0.0,
            )
            if v is None:
                self._set_status("已取消修改预算")
                return

            self.budget = float(v)
            try:
                self.budget_value.configure(text=f"{self.budget:.2f}")
            except Exception:
                pass
            self._set_status(f"预算已更新：{self.budget:.2f}")
            self._calculate_optimization()
        except Exception as e:
            self._set_status(f"修改预算失败：{e}")
            self._render_output(f"❌ 修改预算失败：{e}")

    def _on_calculate(self):
        try:
            self._calculate_optimization()
            self._set_status("已计算优化建议")
        except Exception as e:
            self._set_status(f"计算失败：{e}")
            self._render_output(f"❌ 计算失败：{e}")

    def _on_market_analyze(self):
        """触发一次完整的市场分析：拉 K 线 → 算指标 → 调 DeepSeek。"""
        try:
            # 1) 让用户输入交易对/周期（macOS 下优先用系统弹窗，保证可见）
            if sys.platform == "darwin":
                import subprocess

                p1 = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'text returned of (display dialog "请输入交易对（例如 BTCUSDT）" default answer "BTCUSDT")',
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if p1.returncode != 0:
                    self._set_status("已取消市场分析")
                    return
                symbol = (p1.stdout or "").strip()

                p2 = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'text returned of (display dialog "请输入周期（例如 1m/5m/15m/1h/1d）" default answer "1h")',
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if p2.returncode != 0:
                    self._set_status("已取消市场分析")
                    return
                timeframe = (p2.stdout or "").strip()
            else:
                symbol = simpledialog.askstring("市场分析", "请输入交易对（例如 BTCUSDT）：", parent=self.root)
                if not symbol:
                    self._set_status("已取消市场分析")
                    return
                timeframe = simpledialog.askstring("市场分析", "请输入周期（例如 1m/1h/1d）：", parent=self.root)
                if not timeframe:
                    self._set_status("已取消市场分析")
                    return

            # 2) 执行分析
            self._set_status("正在分析中（会调用 API）...")
            r = self.analyzer.analyze_market_from_binance(symbol=symbol, timeframe=timeframe, limit=200, force_analysis=True)

            # 3) 渲染输出
            lines = [
                f"📈 市场分析结果（{symbol.upper()} {timeframe} / source={r.get('source')}）",
                f"- recommendation: {r.get('recommendation')}",
                f"- confidence: {r.get('confidence')}",
                f"- status: {r.get('status')}",
            ]
            if r.get("target_price") is not None:
                lines.append(f"- target_price: {r.get('target_price')}")
            if r.get("stop_loss") is not None:
                lines.append(f"- stop_loss: {r.get('stop_loss')}")
            if r.get("raw_analysis"):
                lines.append("\n--- raw_analysis ---")
                lines.append(str(r.get("raw_analysis")))

            self._render_output("\n".join(lines))
            self._set_status("市场分析完成")

            # 4) 刷新成本统计
            try:
                self.update_stats()
            except Exception:
                pass

        except Exception as e:
            self._set_status(f"市场分析失败：{e}")
            self._render_output(f"❌ 市场分析失败：{e}")

    def _on_refresh(self):
        try:
            try:
                from datetime import datetime

                ts = datetime.now().strftime("%H:%M:%S")
            except Exception:
                ts = ""

            did_call, msg = self.analyzer.refresh_cost_summary_via_api()
            self.update_stats()

            suffix = f" {ts}" if ts else ""
            flag = "已调用 API" if did_call else "未调用 API"
            self._set_status(f"已刷新成本统计（{flag}）{suffix}｜{msg}")
        except Exception as e:
            self._set_status(f"刷新失败：{e}")
            self._render_output(f"❌ 刷新失败：{e}")

    def update_stats(self):
        summary = self.analyzer.get_cost_summary()
        self.total_calls.configure(text=str(summary["total_calls"]))
        self.total_cost.configure(text=f"${summary['total_cost']:.4f}")
        self.today_calls.configure(text=str(summary["today_calls"]))
        self.today_cost.configure(text=f"${summary['today_cost']:.4f}")
        self.avg_cost.configure(text=f"${summary['avg_cost_per_call']:.6f}")
        self.total_tokens.configure(text=str(summary["total_tokens"]))

    def _calculate_optimization(self):
        try:
            from deepseek_analyzer_optimized import CostOptimizationStrategy

            config = CostOptimizationStrategy.get_recommended_config(float(self.budget))
            txt = (
                f"📊 基于日预算 ${self.budget:.2f} 的优化建议：\n\n"
                f"✓ 平均成本/次: ${config['avg_cost_per_call']:.6f}\n"
                f"✓ 最大日调用次数: {config['max_daily_calls']} 次\n"
                f"✓ 推荐检查间隔: {config['recommended_check_interval']} 分钟\n"
                f"✓ 推荐缓存有效期: {config['recommended_cache_ttl']} 分钟\n"
                f"✓ 可监控交易对数: {config['recommended_symbols']} 个\n\n"
                "💡 优化策略：\n"
                "1. 启用缓存机制 - 相同行情数据不重复分析\n"
                "2. 条件触发分析 - 只在高质量信号时调用 AI\n"
                "3. 调用频率限制 - 每个交易对最多 5 分钟调用一次\n"
                "4. 批量分析 - 一次调用分析多个交易对\n\n"
                "⚠️  成本预估：\n"
                f"- 每日 {config['max_daily_calls']} 次调用 = ${self.budget:.2f}\n"
                f"- 每月约 ${self.budget * 30:.2f}\n"
                f"- 每年约 ${self.budget * 365:.2f}\n"
            )
            self._render_output(txt)
        except Exception as e:
            self._set_status(f"计算失败：{e}")
            self._render_output(f"❌ 计算失败：{e}")


if __name__ == "__main__":
    from deepseek_analyzer_optimized import OptimizedDeepSeekAnalyzer

    api_key = _get_api_key()
    if not api_key:
        msg = (
            "未检测到 API Key（环境变量 DEEPSEEK_API_KEY）。\n\n"
            "macOS/Linux：\n"
            "  export DEEPSEEK_API_KEY=\"你的key\"\n\n"
            "Windows（PowerShell，永久）：\n"
            "  setx DEEPSEEK_API_KEY \"你的key\"\n\n"
            "设置后请重新启动程序。"
        )

        # 打包成 Windows EXE（--noconsole）后，控制台不可见，这里尽量弹窗提示。
        try:
            from tkinter import messagebox

            tmp = tk.Tk()
            tmp.withdraw()
            messagebox.showerror("缺少 API Key", msg)
            try:
                tmp.destroy()
            except Exception:
                pass
        except Exception:
            pass

        raise SystemExit(msg)

    # 启动诊断：帮你确认到底走了什么路径
    try:
        print(f"Python executable: {sys.executable}")
        print(f"Tk version: {tk.TkVersion} | Tcl version: {tk.TclVersion}")
        print(f"macOS dark mode: {_is_macos_dark_mode()}")
        print(f"FORCE_TTK_UI: {os.environ.get('FORCE_TTK_UI')!r}")
        print(f"FORCE_FALLBACK_UI: {os.environ.get('FORCE_FALLBACK_UI')!r}")
        print(f"fallback ui: {_should_use_fallback_ui()}")
    except Exception:
        pass

    root = tk.Tk()
    root.title("成本控制面板")
    root.geometry("700x600")

    # 自动决定是否走兜底 UI（也可用环境变量强制）
    use_fallback = _should_use_fallback_ui()

    # 如果走 ttk，则强制 clam：避免 aqua 的系统颜色名导致“控件透明/不可见”
    if not use_fallback:
        try:
            style = ttk.Style(root)
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            # 主题切换失败也不要崩，后面还有兜底
            pass

    analyzer = OptimizedDeepSeekAnalyzer(api_key)

    def _switch_to_fallback(reason: str | None = None):
        # 清空现有控件
        for w in list(root.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass

        # 用 Button 作为提示（在你的 Tk 8.5 环境里 Button 渲染相对最可靠）
        msg = "已切换兜底 UI" + (f"：{reason}" if reason else "")
        try:
            tk.Button(root, text=msg, relief="flat", bd=0, command=lambda: None, anchor="w").pack(
                fill=tk.X, padx=10, pady=(10, 6)
            )
        except Exception:
            pass

        return FallbackPanel(root, analyzer)

    # 永远放一个“切换兜底 UI”按钮（就算 ttk 渲染抽风也能救场）
    try:
        tk.Button(
            root,
            text="切换兜底 UI",
            command=lambda: _switch_to_fallback("手动切换"),
        ).place(x=10, y=10)
    except Exception:
        pass

    try:
        if use_fallback:
            panel = _switch_to_fallback("自动检测：macOS + Tk<8.6")
        else:
            panel = CostControlPanel(root, analyzer)

        # 强制刷新一次，尽量触发真实绘制
        try:
            root.update_idletasks()
            root.update()
        except Exception:
            pass

        # 如果仍然“看起来像空窗”，自动兜底：
        # - root 的子控件存在，但尺寸全是 1x1/0x0 时，基本等价于没渲染
        try:
            sizes = [(w.winfo_width(), w.winfo_height()) for w in root.winfo_children()]
            if sizes and all((w <= 1 or h <= 1) for (w, h) in sizes):
                raise RuntimeError(f"widgets not rendered, sizes={sizes}")
        except Exception as e:
            # 任何探测异常也直接兜底
            panel = _switch_to_fallback(str(e))

    except Exception as e:
        panel = _switch_to_fallback(f"初始化异常: {e}")

    root.mainloop()
