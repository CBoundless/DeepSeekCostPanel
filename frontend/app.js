const state = {
  token: localStorage.getItem('dscp_admin_token') || '',
  user: null,
  summary: null,
  capabilities: null,
  credentials: [],
  users: [],
  strategies: [],
  presets: {},
  selectedStrategyId: null,
  selectedRunId: null,
  editingCredentialId: null,
  editingStrategyId: null,
  activeModule: localStorage.getItem('dscp_admin_module') || 'overview',
  ws: null,
  wsConnected: false,
  wsRetryTimer: null,
  wsRetryCount: 0,
  wsReconnectSuppressed: false,
  wsManualClose: false,
  refreshDebounceTimer: null,
  refreshFlags: {},
  monitor: {
    run: null,
    logs: [],
    orders: [],
    trades: [],
    decisions: [],
  },
  versions: [],
  runHistory: [],
  runHistorySummary: null,
  alerts: [],
  auditLogs: [],
  recoveryActions: [],
  members: [],
  owner: null,
  backtests: [],
  selectedBacktestId: null,
  backtestDetail: null,
  marketplace: [],
  selectedMarketId: null,
  marketplaceDetail: null,
};

const els = {
  flash: document.getElementById('flash'),
  setupView: document.getElementById('setupView'),
  loginView: document.getElementById('loginView'),
  appView: document.getElementById('appView'),
  setupForm: document.getElementById('setupForm'),
  loginForm: document.getElementById('loginForm'),
  logoutBtn: document.getElementById('logoutBtn'),
  currentUser: document.getElementById('currentUser'),
  realtimeStatus: document.getElementById('realtimeStatus'),
  summaryCards: document.getElementById('summaryCards'),
  notesCard: document.getElementById('notesCard'),
  credentialForm: document.getElementById('credentialForm'),
  credentialList: document.getElementById('credentialList'),
  credentialSelect: document.getElementById('credentialSelect'),
  credentialResetBtn: document.getElementById('credentialResetBtn'),
  credentialExchangeSelect: document.getElementById('credentialExchangeSelect'),
  userPanel: document.getElementById('userPanel'),
  userForm: document.getElementById('userForm'),
  userList: document.getElementById('userList'),
  userRoleSelect: document.getElementById('userRoleSelect'),
  refreshUsersBtn: document.getElementById('refreshUsersBtn'),
  strategyForm: document.getElementById('strategyForm'),
  strategyList: document.getElementById('strategyList'),
  strategyResetBtn: document.getElementById('strategyResetBtn'),
  presetBar: document.getElementById('presetBar'),
  marketTypeSelect: document.getElementById('marketTypeSelect'),
  marginModeSelect: document.getElementById('marginModeSelect'),
  logTitle: document.getElementById('logTitle'),
  logPanel: document.getElementById('logPanel'),
  logMeta: document.getElementById('logMeta'),
  refreshLogsBtn: document.getElementById('refreshLogsBtn'),
  detailMeta: document.getElementById('detailMeta'),
  ordersTable: document.getElementById('ordersTable'),
  tradesTable: document.getElementById('tradesTable'),
  decisionList: document.getElementById('decisionList'),
  refreshGovernanceBtn: document.getElementById('refreshGovernanceBtn'),
  refreshOpsBtn: document.getElementById('refreshOpsBtn'),
  versionList: document.getElementById('versionList'),
  recoveryList: document.getElementById('recoveryList'),
  runHistoryMeta: document.getElementById('runHistoryMeta'),
  runHistoryTable: document.getElementById('runHistoryTable'),
  memberForm: document.getElementById('memberForm'),
  memberUserSelect: document.getElementById('memberUserSelect'),
  memberRoleSelect: document.getElementById('memberRoleSelect'),
  memberList: document.getElementById('memberList'),
  publishForm: document.getElementById('publishForm'),
  marketCategorySelect: document.getElementById('marketCategorySelect'),
  alertsList: document.getElementById('alertsList'),
  auditTable: document.getElementById('auditTable'),
  backtestForm: document.getElementById('backtestForm'),
  backtestStrategySelect: document.getElementById('backtestStrategySelect'),
  backtestSymbol: document.getElementById('backtestSymbol'),
  backtestTimeframe: document.getElementById('backtestTimeframe'),
  backtestBars: document.getElementById('backtestBars'),
  backtestCapital: document.getElementById('backtestCapital'),
  backtestEngine: document.getElementById('backtestEngine'),
  backtestList: document.getElementById('backtestList'),
  backtestDetail: document.getElementById('backtestDetail'),
  marketplaceList: document.getElementById('marketplaceList'),
  marketplaceDetail: document.getElementById('marketplaceDetail'),
  refreshMarketBtn: document.getElementById('refreshMarketBtn'),
  marketFilterForm: document.getElementById('marketFilterForm'),
  marketFilterCategory: document.getElementById('marketFilterCategory'),
  moduleNav: document.getElementById('moduleNav'),
  moduleTitle: document.getElementById('moduleTitle'),
  moduleHint: document.getElementById('moduleHint'),
  moduleLinks: Array.from(document.querySelectorAll('[data-module-nav]')),
  moduleSections: Array.from(document.querySelectorAll('.module-section')),
};

const MODULE_META = {
  overview: {
    title: '总览看板',
    hint: '快速查看平台能力、策略规模、告警与当前阶段说明。',
  },
  credentials: {
    title: '交易账号',
    hint: '集中维护 OKX / Binance 凭证、DeepSeek 配置和交易环境。',
  },
  users: {
    title: '用户权限',
    hint: '管理员在这里管理全局用户、角色和账号访问边界。',
  },
  strategies: {
    title: '策略工作台',
    hint: '创建策略、配置市场画像 / DSL / 风控，并从列表选择目标策略。',
  },
  runtime: {
    title: '实时运行',
    hint: '查看运行日志、订单、成交和决策细节，适合盯盘和排障。',
  },
  governance: {
    title: '治理协作',
    hint: '管理策略版本、运行历史、协作成员与市场发布。',
  },
  operations: {
    title: '告警审计',
    hint: '集中处理异常告警、恢复动作和权限 / 协作审计日志。',
  },
  backtests: {
    title: '增强回测',
    hint: '运行多交易所、多市场、DSL 驱动的增强回测并查看明细。',
  },
  marketplace: {
    title: '策略市场',
    hint: '浏览、筛选和导入公开策略版本。',
  },
};

function setActiveModule(moduleId, { persist = true, scroll = false } = {}) {
  const targetModule = MODULE_META[moduleId] ? moduleId : 'overview';
  const meta = MODULE_META[targetModule];
  state.activeModule = targetModule;
  if (persist) {
    localStorage.setItem('dscp_admin_module', targetModule);
  }
  if (els.moduleTitle) els.moduleTitle.textContent = meta.title;
  if (els.moduleHint) els.moduleHint.textContent = meta.hint;
  els.moduleLinks.forEach((button) => {
    button.classList.toggle('active', button.dataset.moduleNav === targetModule);
  });
  els.moduleSections.forEach((section) => {
    section.classList.toggle('module-hidden', section.dataset.module !== targetModule);
  });
  if (scroll) {
    els.moduleNav?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function showFlash(message, type = 'success') {
  els.flash.textContent = message;
  els.flash.className = `flash ${type}`;
  window.clearTimeout(showFlash.timer);
  showFlash.timer = window.setTimeout(() => {
    els.flash.className = 'flash hidden';
    els.flash.textContent = '';
  }, 4200);
}

function showView(view) {
  for (const id of ['setupView', 'loginView', 'appView']) {
    els[id].classList.toggle('hidden', id !== view);
  }
}

function setRealtimeStatus(connected, text) {
  state.wsConnected = !!connected;
  els.realtimeStatus.textContent = text || (connected ? '实时通道已连接' : '实时通道未连接');
  els.realtimeStatus.className = `user-chip ${connected ? 'status-ok' : 'status-offline'}`;
}

function buildQuery(params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    search.set(key, String(value));
  });
  const suffix = search.toString();
  return suffix ? `?${suffix}` : '';
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has('Content-Type') && options.body !== undefined) {
    headers.set('Content-Type', 'application/json');
  }
  if (state.token) {
    headers.set('Authorization', `Bearer ${state.token}`);
  }
  const response = await fetch(`/api${path}`, { ...options, headers });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      detail = payload.detail || JSON.stringify(payload);
    } catch (error) {
      // noop
    }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '--';
  return Number(value).toFixed(digits);
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '--';
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '--';
  const total = Math.max(0, Number(seconds));
  if (total < 60) return `${Math.round(total)} 秒`;
  if (total < 3600) return `${Math.floor(total / 60)} 分 ${Math.round(total % 60)} 秒`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return `${hours} 小时 ${minutes} 分`;
}

function toFormData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function emptyWrap(text) {
  return `<div class="empty-state">${escapeHtml(text)}</div>`;
}

function badgeClass(status) {
  if (['running', 'started', 'published'].includes(status)) return 'running';
  if (['stopping', 'starting', 'scheduled'].includes(status)) return 'stopping';
  if (['error', 'failed', 'rejected'].includes(status)) return 'error';
  if (['warning', 'open', 'skipped_limit'].includes(status)) return 'warning';
  return 'stopped';
}

function pnlClass(value) {
  if (value === null || value === undefined) return 'neutral';
  if (Number(value) > 0) return 'positive';
  if (Number(value) < 0) return 'negative';
  return 'neutral';
}

function configFieldNames() {
  return [
    'trade_quote',
    'conf_threshold',
    'loop_seconds',
    'market_quality_threshold',
    'stop_loss_pct',
    'trailing_stop_pct',
    'trailing_activate_pct',
    'min_net_profit_pct',
    'max_total_exposure_ratio',
    'max_single_asset_weight',
    'max_order_cash_ratio',
    'min_cash_reserve_ratio',
    'capital_allocation_ratio',
    'auto_recover_cooldown_seconds',
    'auto_recover_limit',
    'auto_recover_window_minutes',
    'backtest_fee_rate',
    'backtest_slippage_bps',
  ];
}

function applyPreset(name) {
  const preset = state.presets[name];
  if (!preset) return;
  els.strategyForm.elements.risk_preset.value = name;
  document.querySelectorAll('.preset-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.preset === name);
  });
  for (const field of [
    'trade_quote',
    'conf_threshold',
    'loop_seconds',
    'market_quality_threshold',
    'stop_loss_pct',
    'trailing_stop_pct',
    'trailing_activate_pct',
    'min_net_profit_pct',
    'max_total_exposure_ratio',
    'max_single_asset_weight',
    'max_order_cash_ratio',
    'min_cash_reserve_ratio',
  ]) {
    const input = els.strategyForm.elements[field];
    if (input) input.value = preset[field] ?? '';
  }
}

function buildPresetButtons() {
  els.presetBar.innerHTML = '';
  Object.keys(state.presets).forEach((presetName) => {
    const label = presetName === 'low' ? '低风险' : presetName === 'medium' ? '中风险' : '高风险';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ghost preset-btn';
    btn.dataset.preset = presetName;
    btn.textContent = `套用${label}`;
    btn.addEventListener('click', () => applyPreset(presetName));
    els.presetBar.appendChild(btn);
  });
  applyPreset(els.strategyForm.elements.risk_preset.value || 'medium');
}

function renderCapabilityOptions() {
  const capabilities = state.capabilities || {};
  const userRoles = capabilities.user_roles || ['admin', 'operator', 'viewer'];
  const memberRoles = capabilities.strategy_member_roles || ['editor', 'operator', 'viewer'];
  const marketTypes = capabilities.market_types || ['spot', 'margin', 'swap'];
  const marginModes = capabilities.margin_modes || ['cash', 'cross', 'isolated'];
  const marketCategories = capabilities.market_categories || ['community'];

  els.userRoleSelect.innerHTML = userRoles.map((role) => `<option value="${escapeHtml(role)}">${escapeHtml(role)}</option>`).join('');
  els.memberRoleSelect.innerHTML = memberRoles.filter((role) => role !== 'owner').map((role) => `<option value="${escapeHtml(role)}">${escapeHtml(role)}</option>`).join('');
  els.marketTypeSelect.innerHTML = marketTypes.map((role) => `<option value="${escapeHtml(role)}">${escapeHtml(role)}</option>`).join('');
  els.marginModeSelect.innerHTML = marginModes.map((role) => `<option value="${escapeHtml(role)}">${escapeHtml(role)}</option>`).join('');
  els.marketCategorySelect.innerHTML = marketCategories.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join('');
  els.marketFilterCategory.innerHTML = [''].concat(marketCategories).map((item) => `<option value="${escapeHtml(item)}">${item ? escapeHtml(item) : '全部分类'}</option>`).join('');
  syncMarginModeState();
}

function syncMarginModeState() {
  const marketType = els.strategyForm.elements.market_type.value || 'spot';
  if (marketType === 'spot') {
    els.marginModeSelect.value = 'cash';
    els.marginModeSelect.disabled = true;
  } else {
    els.marginModeSelect.disabled = false;
    if (!['cross', 'isolated'].includes(els.marginModeSelect.value)) {
      els.marginModeSelect.value = 'cross';
    }
  }
}

function collectCredentialConfig() {
  const baseUrl = (els.credentialForm.elements.exchange_base_url.value || '').trim();
  const config = {};
  if (baseUrl) config.base_url = baseUrl;
  return config;
}

function collectStrategyConfig() {
  const config = {};
  for (const name of configFieldNames()) {
    const raw = els.strategyForm.elements[name]?.value;
    if (raw === undefined || raw === '') continue;
    config[name] = Number(raw);
  }
  config.allow_shared_credential = !!els.strategyForm.elements.allow_shared_credential.checked;
  config.symbol_isolation = !!els.strategyForm.elements.symbol_isolation.checked;
  config.auto_recover_enabled = !!els.strategyForm.elements.auto_recover_enabled.checked;
  return config;
}

function renderSummary() {
  const totals = state.summary?.totals || {};
  const items = [
    { title: '我的账号', value: totals.credentials ?? 0, hint: '支持 OKX / Binance' },
    { title: '可见策略', value: totals.strategies ?? 0, hint: '含共享给我的策略' },
    { title: '共享给我', value: totals.shared_with_me ?? 0, hint: '多人协作中的策略数量' },
    { title: '运行中策略', value: totals.running_strategies ?? 0, hint: '受实时支持矩阵约束' },
    { title: '累计盈亏', value: `${formatNumber(totals.total_pnl_usdt, 2)} USDT`, hint: '按最近一次运行快照汇总' },
    { title: '开放告警', value: totals.open_alerts ?? 0, hint: '运行或恢复异常待处理' },
    { title: '策略市场发布', value: totals.market_publications ?? 0, hint: '我已发布到市场的版本数' },
    { title: '增强回测', value: totals.backtests ?? 0, hint: '历史回放 / DSL / 杠杆模拟' },
  ];
  els.summaryCards.innerHTML = items.map((item) => `
    <article class="summary-card">
      <p class="eyebrow">${escapeHtml(item.title)}</p>
      <strong>${escapeHtml(item.value)}</strong>
      <p class="subtle">${escapeHtml(item.hint)}</p>
    </article>
  `).join('');

  const notes = state.summary?.notes || [];
  els.notesCard.innerHTML = `
    <p class="eyebrow">当前阶段说明</p>
    <ul>${notes.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>
  `;
}

function renderCredentialOptions() {
  if (!state.credentials.length) {
    els.credentialSelect.innerHTML = '<option value="">请先创建交易账号</option>';
    return;
  }
  els.credentialSelect.innerHTML = state.credentials
    .map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.exchange_label || item.exchange)}</option>`)
    .join('');
}

function renderBacktestStrategyOptions() {
  if (!state.strategies.length) {
    els.backtestStrategySelect.innerHTML = '<option value="">请先创建策略</option>';
    return;
  }
  els.backtestStrategySelect.innerHTML = state.strategies
    .map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.trade_profile?.exchange_label || item.trade_profile?.exchange || '')}</option>`)
    .join('');
}

function renderUserOptions() {
  const options = state.users
    .filter((item) => !state.selectedStrategyId || item.id !== getSelectedStrategy()?.owner_user_id)
    .map((item) => `<option value="${item.id}">${escapeHtml(item.username)} · ${escapeHtml(item.role)}</option>`)
    .join('');
  els.memberUserSelect.innerHTML = options || '<option value="">暂无可选用户</option>';
}

function getSelectedStrategy() {
  return state.strategies.find((item) => item.id === state.selectedStrategyId) || null;
}

function renderCredentials() {
  renderCredentialOptions();
  if (!state.credentials.length) {
    els.credentialList.innerHTML = emptyWrap('还没有交易账号。先录入交易所和 DeepSeek 凭证。');
    return;
  }
  els.credentialList.innerHTML = state.credentials.map((item) => `
    <article class="credential-card">
      <div class="card-head">
        <div>
          <h4>${escapeHtml(item.name)}</h4>
          <div class="card-meta">
            <span>${escapeHtml(item.exchange_label || item.exchange)}</span>
            <span>${item.simulated_trading ? '模拟 / 测试' : '实盘'}</span>
            <span>${escapeHtml(item.masked_api_key || '未配置 Key')}</span>
          </div>
        </div>
        <span class="badge ${item.has_deepseek_api_key ? 'running' : 'stopped'}">${item.has_deepseek_api_key ? 'DeepSeek 已配置' : '缺少 DeepSeek Key'}</span>
      </div>
      <p class="subtle">${escapeHtml(item.description || '暂无描述')}</p>
      <div class="card-meta">
        <span>Secret: ${item.has_api_secret ? '已保存' : '未保存'}</span>
        <span>Passphrase: ${item.has_api_passphrase ? '已保存' : '未保存'}</span>
        <span>Base URL: ${escapeHtml(item.config?.base_url || '默认')}</span>
      </div>
      <div class="card-actions">
        <button type="button" class="ghost" data-action="edit-credential" data-id="${item.id}">编辑</button>
        <button type="button" class="danger ghost" data-action="delete-credential" data-id="${item.id}">删除</button>
      </div>
    </article>
  `).join('');
}

function renderUsers() {
  renderUserOptions();
  if (state.user?.role !== 'admin') {
    els.userPanel.classList.add('hidden');
    return;
  }
  els.userPanel.classList.remove('hidden');
  if (!state.users.length) {
    els.userList.innerHTML = emptyWrap('暂无用户。');
    return;
  }
  els.userList.innerHTML = state.users.map((item) => `
    <article class="member-card">
      <div class="card-head">
        <div>
          <h4>${escapeHtml(item.username)}</h4>
          <div class="card-meta">
            <span>#${item.id}</span>
            <span>${escapeHtml(item.created_at || '--')}</span>
          </div>
        </div>
        <span class="badge ${badgeClass(item.role === 'admin' ? 'running' : item.role === 'operator' ? 'warning' : 'stopped')}">${escapeHtml(item.role)}</span>
      </div>
      <div class="card-actions">
        ${['admin', 'operator', 'viewer'].map((role) => `<button type="button" class="ghost mini" data-action="set-user-role" data-id="${item.id}" data-role="${role}">${role}</button>`).join('')}
        <button type="button" class="ghost mini" data-action="reset-user-password" data-id="${item.id}">重置密码</button>
      </div>
    </article>
  `).join('');
}

function renderStrategies() {
  renderBacktestStrategyOptions();
  if (!state.strategies.length) {
    els.strategyList.innerHTML = emptyWrap('还没有策略。创建后即可做运行、协作、DSL 回测与市场发布。');
    return;
  }
  els.strategyList.innerHTML = state.strategies.map((item) => {
    const run = item.latest_run;
    const status = run?.status || 'stopped';
    const selected = item.id === state.selectedStrategyId;
    const policy = item.runtime_policy || {};
    const stats = run?.stats || {};
    const trade = item.trade_profile || {};
    const indicator = item.indicator_profile || {};
    return `
      <article class="strategy-card ${selected ? 'selected' : ''}">
        <div class="strategy-head">
          <div>
            <h4>${escapeHtml(item.name)}</h4>
            <div class="strategy-meta">
              <span>${escapeHtml(item.owner_username || '未知 owner')} / ${escapeHtml(item.access_role || 'viewer')}</span>
              <span>${escapeHtml(trade.exchange_label || trade.exchange || '--')}</span>
              <span>${escapeHtml(trade.market_type || '--')}</span>
              <span>${escapeHtml(trade.margin_mode || '--')}</span>
              <span>${escapeHtml(item.timeframe)}</span>
              <span>杠杆 ${formatNumber(item.leverage, 1)}x</span>
              <span>版本 ${item.version_count || 0}</span>
            </div>
          </div>
          <span class="badge ${badgeClass(status)}">${escapeHtml(status)}</span>
        </div>
        <p class="subtle">${escapeHtml(item.description || '暂无策略描述')}</p>
        <div class="metric-grid">
          <div class="metric"><span>当前权益</span><strong>${formatNumber(run?.current_equity_usdt, 2)} USDT</strong></div>
          <div class="metric"><span>基础盈亏</span><strong class="${pnlClass(run?.pnl_usdt)}">${formatNumber(run?.pnl_usdt, 2)} USDT</strong></div>
          <div class="metric"><span>最大回撤</span><strong class="negative">${formatPercent(stats.max_drawdown_pct)}</strong></div>
          <div class="metric"><span>成交笔数</span><strong>${stats.trade_count ?? 0}</strong></div>
          <div class="metric"><span>协作成员</span><strong>${item.collaborator_count ?? 0}</strong></div>
          <div class="metric"><span>DSL</span><strong>${indicator.dsl_enabled ? '已启用' : '未启用'}</strong></div>
        </div>
        <div class="card-meta" style="margin-top:12px;">
          <span>运行支持: ${trade.runtime_supported ? '支持' : `仅回测 · ${escapeHtml(trade.runtime_block_reason || '')}`}</span>
          <span>共享账号: ${policy.allow_shared_credential ? '允许' : '独占'}</span>
          <span>资金占比: ${formatPercent(policy.capital_allocation_ratio)}</span>
          <span>自动恢复: ${policy.auto_recover_enabled ? `开启 / ${policy.auto_recover_limit ?? 0} 次` : '关闭'}</span>
          <span>市场条目: ${item.trade_profile?.exchange || '--'} / ${item.trade_profile?.market_type || '--'}</span>
        </div>
        ${run?.last_error ? `<p class="negative" style="margin-top:10px;">${escapeHtml(run.last_error)}</p>` : ''}
        <div class="card-actions">
          <button type="button" class="ghost" data-action="edit-strategy" data-id="${item.id}">编辑</button>
          <button type="button" class="ghost" data-action="view-strategy" data-id="${item.id}">查看治理</button>
          <button type="button" class="ghost" data-action="run-backtest" data-id="${item.id}">回测</button>
          ${item.is_running
            ? `<button type="button" class="warn" data-action="stop-strategy" data-id="${item.id}">停止</button>`
            : `<button type="button" data-action="start-strategy" data-id="${item.id}">启动</button>`}
          <button type="button" class="danger ghost" data-action="delete-strategy" data-id="${item.id}">删除</button>
        </div>
      </article>
    `;
  }).join('');
}

function renderLogs() {
  const strategy = getSelectedStrategy();
  const run = state.monitor.run;
  const suffix = run?.id ? ` · Run #${run.id}` : '';
  els.logTitle.textContent = strategy ? `运行日志 · ${strategy.name}${suffix}` : '最近一次运行输出';
  els.logMeta.textContent = run
    ? `${strategy?.name || '未命名策略'} · ${run.status} · 决策数 ${run.decision_count} · ${run.stop_reason || '运行中'}`
    : '请选择一个策略查看日志。';
  const lines = (state.monitor.logs || []).map((item) => `[${item.created_at}] ${item.message}`);
  els.logPanel.textContent = lines.length ? lines.join('\n') : '暂无日志';
}

function renderTable(items, columns, emptyText = '暂无数据') {
  if (!items.length) return emptyWrap(emptyText);
  const head = columns.map((item) => `<th>${escapeHtml(item.label)}</th>`).join('');
  const body = items.map((row) => `
    <tr>
      ${columns.map((column) => `<td>${column.render(row)}</td>`).join('')}
    </tr>
  `).join('');
  return `
    <table class="data-table">
      <thead><tr>${head}</tr></thead>
      <tbody>${body}</tbody>
    </table>
  `;
}

function renderStrategyDetails() {
  const strategy = getSelectedStrategy();
  const run = state.monitor.run;
  els.detailMeta.textContent = strategy
    ? `${strategy.symbols} · ${strategy.trade_profile?.exchange_label || strategy.trade_profile?.exchange || '--'} · ${strategy.trade_profile?.market_type || '--'} · ${run?.status || '未运行'}${run?.id ? ` · Run #${run.id}` : ''}`
    : '请选择一个策略查看订单、成交和决策明细。';

  els.ordersTable.innerHTML = renderTable(state.monitor.orders || [], [
    { label: '时间', render: (row) => escapeHtml(row.created_at || '--') },
    { label: '标的', render: (row) => escapeHtml(row.inst_id) },
    { label: '方向', render: (row) => escapeHtml(row.side) },
    { label: '状态', render: (row) => escapeHtml(row.state) },
    { label: '请求金额', render: (row) => row.requested_quote ? `${formatNumber(row.requested_quote, 2)} USDT` : '--' },
    { label: '请求数量', render: (row) => row.requested_size ? formatNumber(row.requested_size, 6) : '--' },
    { label: 'ordId', render: (row) => escapeHtml(row.ord_id || '--') },
  ], '暂无订单');

  els.tradesTable.innerHTML = renderTable(state.monitor.trades || [], [
    { label: '时间', render: (row) => escapeHtml(row.created_at || '--') },
    { label: '标的', render: (row) => escapeHtml(row.inst_id) },
    { label: '方向', render: (row) => escapeHtml(row.side) },
    { label: '成交数量', render: (row) => formatNumber(row.filled_size, 6) },
    { label: '均价', render: (row) => row.avg_price ? formatNumber(row.avg_price, 6) : '--' },
    { label: '成交价', render: (row) => row.fill_price ? formatNumber(row.fill_price, 6) : '--' },
    { label: '手续费', render: (row) => row.fee ? formatNumber(row.fee, 6) : '--' },
  ], '暂无成交');

  if (!(state.monitor.decisions || []).length) {
    els.decisionList.innerHTML = emptyWrap('暂无决策');
    return;
  }
  els.decisionList.innerHTML = state.monitor.decisions.map((item) => `
    <article class="decision-card">
      <div class="decision-head">
        <strong>${escapeHtml(item.inst_id)}</strong>
        <span class="badge ${badgeClass(item.action === 'BUY' || item.action === 'BUY_ADD' ? 'running' : item.action.includes('FAILED') ? 'error' : 'stopped')}">${escapeHtml(item.action)}</span>
      </div>
      <div class="card-meta">
        <span>置信度 ${item.confidence}</span>
        <span>signal_q ${formatNumber(item.signal_quality, 2)}</span>
        <span>market_q ${formatNumber(item.market_quality, 2)}</span>
        <span>planned ${item.planned_quote ? `${formatNumber(item.planned_quote, 2)} USDT` : '--'}</span>
      </div>
      <p class="subtle">${escapeHtml(item.reason || '无附加原因')}</p>
      <div class="card-meta"><span>${escapeHtml(item.created_at)}</span></div>
    </article>
  `).join('');
}

function renderVersions() {
  if (!state.selectedStrategyId) {
    els.versionList.innerHTML = emptyWrap('请选择策略查看版本。');
    return;
  }
  if (!state.versions.length) {
    els.versionList.innerHTML = emptyWrap('暂无版本快照。');
    return;
  }
  els.versionList.innerHTML = state.versions.map((item) => {
    const trade = item.trade_profile || {};
    const indicator = item.indicator_profile || {};
    return `
      <article class="version-card">
        <div class="card-head">
          <div>
            <h4>V${item.version_no}</h4>
            <div class="card-meta">
              <span>${escapeHtml(item.source)}</span>
              <span>${escapeHtml(item.created_at)}</span>
            </div>
          </div>
          <span class="badge ${badgeClass(item.source === 'restore' ? 'running' : 'stopped')}">${escapeHtml(item.source)}</span>
        </div>
        <p class="subtle">${escapeHtml(item.note || '未备注')}</p>
        <div class="card-meta">
          <span>${escapeHtml(trade.exchange_label || trade.exchange || '--')}</span>
          <span>${escapeHtml(trade.market_type || '--')}</span>
          <span>杠杆 ${formatNumber(trade.leverage, 1)}x</span>
          <span>DSL ${indicator.dsl_enabled ? '开启' : '关闭'}</span>
        </div>
        <div class="card-actions">
          <button type="button" class="ghost mini" data-action="restore-version" data-id="${item.id}">恢复到此版本</button>
        </div>
      </article>
    `;
  }).join('');
}

function renderRecoveryActions() {
  if (!state.recoveryActions.length) {
    els.recoveryList.innerHTML = emptyWrap('暂无恢复记录。');
    return;
  }
  els.recoveryList.innerHTML = state.recoveryActions.map((item) => `
    <article class="recovery-card">
      <div class="card-head">
        <div>
          <h4>尝试 #${item.attempt_no}</h4>
          <div class="card-meta">
            <span>${escapeHtml(item.created_at)}</span>
            <span>失败 Run #${item.failed_run_id || '--'}</span>
            <span>恢复 Run #${item.recovered_run_id || '--'}</span>
          </div>
        </div>
        <span class="badge ${badgeClass(item.status)}">${escapeHtml(item.status)}</span>
      </div>
      <p class="subtle">${escapeHtml(item.message || item.reason || '无附加信息')}</p>
    </article>
  `).join('');
}

function renderRunHistory() {
  if (!state.selectedStrategyId) {
    els.runHistoryMeta.textContent = '请选择策略查看运行历史。';
    els.runHistoryTable.innerHTML = emptyWrap('暂无运行历史。');
    return;
  }
  const summary = state.runHistorySummary || {};
  els.runHistoryMeta.textContent = `累计 ${summary.run_count || 0} 次 · 错误 ${summary.error_runs || 0} 次 · 累计盈亏 ${formatNumber(summary.total_pnl_usdt, 2)} USDT · 累计手续费 ${formatNumber(summary.total_fees_usdt, 4)} USDT`;
  els.runHistoryTable.innerHTML = renderTable(state.runHistory || [], [
    { label: 'Run', render: (row) => `#${row.id}` },
    { label: '状态', render: (row) => `<span class="badge ${badgeClass(row.status)}">${escapeHtml(row.status)}</span>` },
    { label: '开始', render: (row) => escapeHtml(row.started_at || '--') },
    { label: '结束', render: (row) => escapeHtml(row.stopped_at || '--') },
    { label: '时长', render: (row) => formatDuration(row.stats?.runtime_seconds) },
    { label: '盈亏', render: (row) => `<span class="${pnlClass(row.pnl_usdt)}">${formatNumber(row.pnl_usdt, 2)} USDT</span>` },
    { label: '最大回撤', render: (row) => formatPercent(row.stats?.max_drawdown_pct) },
    { label: '手续费', render: (row) => `${formatNumber(row.stats?.fees_usdt, 4)} USDT` },
    { label: '操作', render: (row) => `<button type="button" class="ghost mini" data-action="view-run" data-run-id="${row.id}">查看</button>` },
  ], '暂无运行历史');
}

function renderMembers() {
  if (!state.selectedStrategyId) {
    els.memberList.innerHTML = emptyWrap('请选择策略查看协作成员。');
    return;
  }
  const cards = [];
  if (state.owner) {
    cards.push(`
      <article class="member-card">
        <div class="card-head">
          <div>
            <h4>${escapeHtml(state.owner.username)}</h4>
            <div class="card-meta"><span>owner</span><span>#${state.owner.id}</span></div>
          </div>
          <span class="badge running">owner</span>
        </div>
      </article>
    `);
  }
  for (const item of state.members || []) {
    cards.push(`
      <article class="member-card">
        <div class="card-head">
          <div>
            <h4>${escapeHtml(item.username || `用户 #${item.user_id}`)}</h4>
            <div class="card-meta"><span>#${item.user_id}</span><span>${escapeHtml(item.created_at || '--')}</span></div>
          </div>
          <span class="badge ${badgeClass(item.role === 'editor' ? 'running' : item.role === 'operator' ? 'warning' : 'stopped')}">${escapeHtml(item.role)}</span>
        </div>
        <div class="card-actions">
          ${['editor', 'operator', 'viewer'].map((role) => `<button type="button" class="ghost mini" data-action="set-member-role" data-id="${item.user_id}" data-role="${role}">${role}</button>`).join('')}
          <button type="button" class="danger ghost mini" data-action="remove-member" data-id="${item.user_id}">移除</button>
        </div>
      </article>
    `);
  }
  els.memberList.innerHTML = cards.length ? cards.join('') : emptyWrap('暂无协作成员。');
}

function renderAlerts() {
  if (!state.alerts.length) {
    els.alertsList.innerHTML = emptyWrap('暂无告警。');
    return;
  }
  els.alertsList.innerHTML = state.alerts.map((item) => `
    <article class="alert-card ${escapeHtml(item.status)}">
      <div class="card-head">
        <div>
          <h4>${escapeHtml(item.title)}</h4>
          <div class="card-meta">
            <span>${escapeHtml(item.severity)}</span>
            <span>${escapeHtml(item.category)}</span>
            <span>${escapeHtml(item.created_at)}</span>
            <span>策略 #${item.strategy_id || '--'}</span>
            <span>Run #${item.run_id || '--'}</span>
          </div>
        </div>
        <span class="badge ${badgeClass(item.severity === 'error' ? 'error' : item.status)}">${escapeHtml(item.status)}</span>
      </div>
      <p class="subtle">${escapeHtml(item.message)}</p>
      <div class="card-actions">
        ${item.status === 'open' ? `<button type="button" class="ghost mini" data-action="ack-alert" data-id="${item.id}">确认</button>` : ''}
      </div>
    </article>
  `).join('');
}

function renderAuditLogs() {
  els.auditTable.innerHTML = renderTable(state.auditLogs || [], [
    { label: '时间', render: (row) => escapeHtml(row.created_at || '--') },
    { label: '动作', render: (row) => escapeHtml(row.action) },
    { label: '资源', render: (row) => `${escapeHtml(row.resource_type)}${row.resource_id ? ` #${escapeHtml(row.resource_id)}` : ''}` },
    { label: '详情', render: (row) => `<code>${escapeHtml(JSON.stringify(row.detail || {}))}</code>` },
  ], '暂无审计日志');
}

function renderBacktests() {
  if (!state.backtests.length) {
    els.backtestList.innerHTML = emptyWrap('还没有回测记录。');
    return;
  }
  els.backtestList.innerHTML = state.backtests.map((item) => `
    <article class="credential-card ${state.selectedBacktestId === item.id ? 'selected' : ''}">
      <div class="card-head">
        <div>
          <h4>#${item.id} · ${escapeHtml(item.strategy_name)}</h4>
          <div class="card-meta">
            <span>${escapeHtml(item.inst_id)}</span>
            <span>${escapeHtml(item.timeframe)}</span>
            <span>${item.bar_count} bars</span>
            <span>${escapeHtml(item.summary?.exchange || '--')} / ${escapeHtml(item.summary?.market_type || '--')}</span>
          </div>
        </div>
        <span class="badge ${item.summary?.return_pct > 0 ? 'running' : item.summary?.return_pct < 0 ? 'error' : 'stopped'}">${formatPercent(item.summary?.return_pct)}</span>
      </div>
      <div class="card-meta">
        <span>引擎 ${escapeHtml(item.summary?.engine || '--')}</span>
        <span>杠杆 ${formatNumber(item.summary?.leverage, 1)}x</span>
        <span>最大回撤 ${formatPercent(item.summary?.max_drawdown_pct)}</span>
      </div>
      <div class="card-actions">
        <button type="button" class="ghost" data-action="view-backtest" data-id="${item.id}">查看详情</button>
      </div>
    </article>
  `).join('');
}

function renderSparkline(points) {
  if (!points.length) return '';
  const width = 720;
  const height = 180;
  const values = points.map((item) => Number(item.equity || 0));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(max - min, 1e-6);
  const coords = points.map((item, index) => {
    const x = (index / Math.max(points.length - 1, 1)) * width;
    const y = height - ((Number(item.equity || 0) - min) / range) * height;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  return `
    <svg viewBox="0 0 ${width} ${height}" class="sparkline" preserveAspectRatio="none">
      <polyline fill="none" stroke="#57a6ff" stroke-width="3" points="${coords}" />
    </svg>
  `;
}

function renderBacktestDetail() {
  const item = state.backtestDetail;
  if (!item) {
    els.backtestDetail.className = 'backtest-detail empty-state';
    els.backtestDetail.innerHTML = '运行一次回测后，这里会显示收益曲线、市场类型和成交记录。';
    return;
  }
  els.backtestDetail.className = 'backtest-detail';
  const summary = item.summary || {};
  els.backtestDetail.innerHTML = `
    <div class="detail-grid">
      <div class="panel-inner stack">
        <div class="grid summary-grid">
          <article class="summary-card"><p class="eyebrow">最终权益</p><strong>${formatNumber(summary.final_equity_usdt, 2)} USDT</strong></article>
          <article class="summary-card"><p class="eyebrow">收益率</p><strong class="${pnlClass(summary.return_pct)}">${formatPercent(summary.return_pct)}</strong></article>
          <article class="summary-card"><p class="eyebrow">市场 / 杠杆</p><strong>${escapeHtml(summary.exchange || '--')} · ${escapeHtml(summary.market_type || '--')} · ${formatNumber(summary.leverage, 1)}x</strong></article>
          <article class="summary-card"><p class="eyebrow">引擎 / 强平</p><strong>${escapeHtml(summary.engine || '--')} · ${summary.liquidations ?? 0}</strong></article>
        </div>
        <div class="chart-card">
          <div class="panel-header compact">
            <div>
              <p class="eyebrow">权益曲线</p>
              <h4>${escapeHtml(item.strategy_name)} · ${escapeHtml(item.inst_id)}</h4>
            </div>
          </div>
          ${renderSparkline(item.equity_curve || [])}
        </div>
      </div>
      <div class="panel-inner stack">
        <div>
          <p class="eyebrow">回测成交</p>
          ${renderTable(item.trades || [], [
            { label: '时间戳', render: (row) => row.ts || '--' },
            { label: '方向', render: (row) => escapeHtml(row.side) },
            { label: '价格', render: (row) => formatNumber(row.price, 6) },
            { label: '数量', render: (row) => formatNumber(row.size, 6) },
            { label: '金额', render: (row) => `${formatNumber(row.quote, 2)} USDT` },
            { label: '原因', render: (row) => escapeHtml(row.reason || '--') },
          ])}
        </div>
      </div>
    </div>
  `;
}

function filteredMarketCredentials(exchange) {
  return state.credentials.filter((item) => item.exchange === exchange);
}

function renderMarketplaceList() {
  if (!state.marketplace.length) {
    els.marketplaceList.innerHTML = emptyWrap('暂无策略市场条目。');
    return;
  }
  els.marketplaceList.innerHTML = state.marketplace.map((item) => `
    <article class="market-card ${state.selectedMarketId === item.id ? 'selected' : ''}">
      <div class="card-head">
        <div>
          <h4>${escapeHtml(item.title)}</h4>
          <div class="card-meta">
            <span>${escapeHtml(item.publisher_username || `用户 #${item.publisher_user_id}`)}</span>
            <span>${escapeHtml(item.exchange || '--')} / ${escapeHtml(item.market_type || '--')}</span>
            <span>安装 ${item.install_count || 0}</span>
          </div>
        </div>
        <span class="badge ${item.dsl_enabled ? 'running' : 'stopped'}">${item.dsl_enabled ? 'DSL' : 'Builtin'}</span>
      </div>
      <p class="subtle">${escapeHtml(item.summary || '暂无简介')}</p>
      <div class="tag-list">${(item.tags || []).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join('')}</div>
      <div class="card-actions">
        <button type="button" class="ghost" data-action="view-market" data-id="${item.id}">查看详情</button>
      </div>
    </article>
  `).join('');
}

function renderMarketplaceDetail() {
  const item = state.marketplaceDetail;
  if (!item) {
    els.marketplaceDetail.className = 'empty-state';
    els.marketplaceDetail.innerHTML = '请选择一个策略市场条目查看详情。';
    return;
  }
  const exchangeCredentials = filteredMarketCredentials(item.exchange);
  els.marketplaceDetail.className = '';
  els.marketplaceDetail.innerHTML = `
    <div class="stack">
      <div class="card-head">
        <div>
          <h3 style="margin:0;">${escapeHtml(item.title)}</h3>
          <div class="card-meta">
            <span>${escapeHtml(item.publisher_username || `用户 #${item.publisher_user_id}`)}</span>
            <span>${escapeHtml(item.exchange || '--')} / ${escapeHtml(item.market_type || '--')}</span>
            <span>${escapeHtml(item.category || '--')}</span>
          </div>
        </div>
        <span class="badge ${item.dsl_enabled ? 'running' : 'stopped'}">${item.dsl_enabled ? 'DSL' : 'Builtin'}</span>
      </div>
      <p class="subtle">${escapeHtml(item.summary || '暂无简介')}</p>
      <div class="tag-list">${(item.tags || []).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join('')}</div>
      <div class="inline-grid">
        <div class="panel-inner stack">
          <p class="eyebrow">策略画像</p>
          <div class="card-meta">
            <span>交易所 ${escapeHtml(item.trade_profile?.exchange_label || item.trade_profile?.exchange || '--')}</span>
            <span>市场 ${escapeHtml(item.trade_profile?.market_type || '--')}</span>
            <span>杠杆 ${formatNumber(item.trade_profile?.leverage, 1)}x</span>
          </div>
          <p class="subtle">${escapeHtml(item.description || '暂无详细说明')}</p>
        </div>
        <div class="panel-inner stack">
          <p class="eyebrow">DSL 摘要</p>
          <div class="code-block">${escapeHtml(item.indicator_profile?.indicator_dsl || '未启用自定义 DSL')}</div>
        </div>
      </div>
      <div class="panel-inner stack">
        <p class="eyebrow">导入到我的工作台</p>
        <label>
          <span>绑定账号（需与条目交易所一致）</span>
          <select id="marketInstallCredentialSelect">${exchangeCredentials.map((cred) => `<option value="${cred.id}">${escapeHtml(cred.name)} · ${escapeHtml(cred.exchange_label || cred.exchange)}</option>`).join('') || '<option value="">暂无可用账号</option>'}</select>
        </label>
        <label>
          <span>导入后的策略名称</span>
          <input id="marketInstallName" type="text" value="${escapeHtml(item.snapshot?.name || item.title || '')}" />
        </label>
        <div class="form-actions">
          <button type="button" data-action="install-market" data-id="${item.id}" ${exchangeCredentials.length ? '' : 'disabled'}>导入此策略</button>
        </div>
      </div>
    </div>
  `;
}

function resetCredentialForm() {
  els.credentialForm.reset();
  els.credentialForm.elements.credentialId.value = '';
  els.credentialForm.elements.exchange.value = 'okx';
  els.credentialForm.elements.deepseek_base_url.value = 'https://api.deepseek.com/v1';
  els.credentialForm.elements.simulated_trading.checked = true;
  state.editingCredentialId = null;
}

function resetStrategyForm() {
  els.strategyForm.reset();
  els.strategyForm.elements.strategyId.value = '';
  els.strategyForm.elements.timeframe.value = '1H';
  els.strategyForm.elements.market_type.value = 'spot';
  els.strategyForm.elements.margin_mode.value = 'cash';
  els.strategyForm.elements.leverage.value = '1';
  els.strategyForm.elements.version_note.value = '创建策略';
  els.strategyForm.elements.allow_shared_credential.checked = false;
  els.strategyForm.elements.symbol_isolation.checked = true;
  els.strategyForm.elements.auto_recover_enabled.checked = true;
  els.strategyForm.elements.capital_allocation_ratio.value = '1';
  els.strategyForm.elements.auto_recover_cooldown_seconds.value = '30';
  els.strategyForm.elements.auto_recover_limit.value = '2';
  els.strategyForm.elements.auto_recover_window_minutes.value = '60';
  state.editingStrategyId = null;
  syncMarginModeState();
  applyPreset('medium');
}

function fillCredentialForm(item) {
  state.editingCredentialId = item.id;
  els.credentialForm.elements.credentialId.value = item.id;
  els.credentialForm.elements.name.value = item.name || '';
  els.credentialForm.elements.description.value = item.description || '';
  els.credentialForm.elements.exchange.value = item.exchange || 'okx';
  els.credentialForm.elements.api_key.value = '';
  els.credentialForm.elements.api_secret.value = '';
  els.credentialForm.elements.api_passphrase.value = '';
  els.credentialForm.elements.deepseek_api_key.value = '';
  els.credentialForm.elements.deepseek_base_url.value = item.deepseek_base_url || 'https://api.deepseek.com/v1';
  els.credentialForm.elements.exchange_base_url.value = item.config?.base_url || '';
  els.credentialForm.elements.simulated_trading.checked = !!item.simulated_trading;
}

function fillStrategyForm(item) {
  state.editingStrategyId = item.id;
  els.strategyForm.elements.strategyId.value = item.id;
  els.strategyForm.elements.name.value = item.name || '';
  els.strategyForm.elements.description.value = item.description || '';
  els.strategyForm.elements.credential_id.value = String(item.credential_id || '');
  els.strategyForm.elements.symbols.value = item.symbols || '';
  els.strategyForm.elements.timeframe.value = item.timeframe || '1H';
  els.strategyForm.elements.risk_preset.value = item.risk_preset || 'medium';
  els.strategyForm.elements.leverage.value = item.leverage || 1;
  els.strategyForm.elements.prompt_template.value = item.prompt_template || '';
  els.strategyForm.elements.market_type.value = item.trade_profile?.market_type || 'spot';
  els.strategyForm.elements.margin_mode.value = item.trade_profile?.margin_mode || 'cash';
  els.strategyForm.elements.indicator_dsl.value = item.indicator_profile?.indicator_dsl || '';
  els.strategyForm.elements.entry_rule.value = item.indicator_profile?.entry_rule || '';
  els.strategyForm.elements.exit_rule.value = item.indicator_profile?.exit_rule || '';
  els.strategyForm.elements.version_note.value = '调整策略配置';
  const config = item.config || {};
  for (const field of configFieldNames()) {
    if (config[field] !== undefined && els.strategyForm.elements[field]) {
      els.strategyForm.elements[field].value = config[field];
    }
  }
  const policy = item.runtime_policy || {};
  els.strategyForm.elements.allow_shared_credential.checked = !!policy.allow_shared_credential;
  els.strategyForm.elements.symbol_isolation.checked = policy.symbol_isolation !== false;
  els.strategyForm.elements.auto_recover_enabled.checked = policy.auto_recover_enabled !== false;
  syncMarginModeState();
}

function primeBacktestForm(strategy) {
  if (!strategy) return;
  els.backtestStrategySelect.value = String(strategy.id);
  const firstSymbol = String(strategy.symbols || '').split(',').map((item) => item.trim()).filter(Boolean)[0] || '';
  els.backtestSymbol.value = firstSymbol;
  els.backtestTimeframe.value = strategy.timeframe || '1H';
  els.backtestEngine.value = strategy.indicator_profile?.dsl_enabled ? 'dsl' : '';
}

function syncPublishForm(strategy) {
  if (!strategy) return;
  els.publishForm.elements.title.value = strategy.name || '';
  els.publishForm.elements.summary.value = strategy.description || '';
  els.publishForm.elements.description.value = strategy.description || '';
}

async function loadSummary() {
  state.summary = await api('/dashboard/summary');
  renderSummary();
  state.user = state.summary.user;
  els.currentUser.textContent = `${state.user.username} · ${state.user.role}`;
  els.currentUser.classList.remove('hidden');
  els.logoutBtn.classList.remove('hidden');
  renderUsers();
}

async function loadCredentials() {
  const payload = await api('/credentials');
  state.credentials = payload.items || [];
  renderCredentials();
}

async function loadUsers() {
  const payload = await api('/users');
  state.users = payload.items || [];
  renderUsers();
}

async function loadStrategies() {
  const payload = await api('/strategies');
  state.strategies = payload.items || [];
  if (state.selectedStrategyId && !state.strategies.some((item) => item.id === state.selectedStrategyId)) {
    state.selectedStrategyId = null;
    state.selectedRunId = null;
    state.members = [];
    state.owner = null;
  }
  renderStrategies();
}

async function loadPresets() {
  const payload = await api('/meta/risk-presets');
  state.presets = payload.presets || {};
  buildPresetButtons();
}

async function loadCapabilities() {
  const payload = await api('/meta/platform-capabilities');
  state.capabilities = payload || {};
  renderCapabilityOptions();
}

async function loadVersions() {
  if (!state.selectedStrategyId) {
    state.versions = [];
    renderVersions();
    return;
  }
  const payload = await api(`/strategies/${state.selectedStrategyId}/versions`);
  state.versions = payload.items || [];
  renderVersions();
}

async function loadRunHistory() {
  if (!state.selectedStrategyId) {
    state.runHistory = [];
    state.runHistorySummary = null;
    renderRunHistory();
    return;
  }
  const payload = await api(`/strategies/${state.selectedStrategyId}/runs${buildQuery({ limit: 30 })}`);
  state.runHistory = payload.items || [];
  state.runHistorySummary = payload.summary || null;
  renderRunHistory();
}

async function loadRecoveryActions() {
  const payload = await api(`/recovery-actions${buildQuery({ strategy_id: state.selectedStrategyId || undefined, limit: 30 })}`);
  state.recoveryActions = payload.items || [];
  renderRecoveryActions();
}

async function loadMembers() {
  if (!state.selectedStrategyId) {
    state.members = [];
    state.owner = null;
    renderMembers();
    return;
  }
  const payload = await api(`/strategies/${state.selectedStrategyId}/members`);
  state.members = payload.items || [];
  state.owner = payload.owner || null;
  renderMembers();
}

async function loadAlerts() {
  const payload = await api(`/alerts${buildQuery({ status_filter: 'all', limit: 60 })}`);
  state.alerts = payload.items || [];
  renderAlerts();
}

async function loadAuditLogs() {
  const payload = await api(`/audit-logs${buildQuery({ limit: 80 })}`);
  state.auditLogs = payload.items || [];
  renderAuditLogs();
}

async function refreshLogs(force = false) {
  if (!state.selectedStrategyId && !force) return;
  if (!state.selectedStrategyId) {
    state.monitor.logs = [];
    state.monitor.run = null;
    renderLogs();
    return;
  }
  const payload = await api(`/strategies/${state.selectedStrategyId}/logs${buildQuery({ run_id: state.selectedRunId || undefined })}`);
  state.monitor.run = payload.run;
  state.monitor.logs = payload.events || [];
  renderLogs();
}

async function loadOrders() {
  if (!state.selectedStrategyId) {
    state.monitor.orders = [];
    state.monitor.trades = [];
    renderStrategyDetails();
    return;
  }
  const payload = await api(`/strategies/${state.selectedStrategyId}/orders${buildQuery({ run_id: state.selectedRunId || undefined })}`);
  state.monitor.orders = payload.orders || [];
  state.monitor.trades = payload.trades || [];
  state.monitor.run = payload.run || state.monitor.run;
  renderStrategyDetails();
}

async function loadDecisions() {
  if (!state.selectedStrategyId) {
    state.monitor.decisions = [];
    renderStrategyDetails();
    return;
  }
  const payload = await api(`/strategies/${state.selectedStrategyId}/decisions${buildQuery({ run_id: state.selectedRunId || undefined })}`);
  state.monitor.decisions = payload.items || [];
  state.monitor.run = payload.run || state.monitor.run;
  renderStrategyDetails();
}

async function loadBacktests() {
  const payload = await api('/backtests');
  state.backtests = payload.items || [];
  renderBacktests();
  if (state.selectedBacktestId && !state.backtests.some((item) => item.id === state.selectedBacktestId)) {
    state.selectedBacktestId = null;
    state.backtestDetail = null;
    renderBacktestDetail();
  }
}

async function loadBacktestDetail(backtestId) {
  if (!backtestId) {
    state.selectedBacktestId = null;
    state.backtestDetail = null;
    renderBacktestDetail();
    return;
  }
  const payload = await api(`/backtests/${backtestId}`);
  state.selectedBacktestId = backtestId;
  state.backtestDetail = payload.item;
  renderBacktests();
  renderBacktestDetail();
}

async function loadMarketplace(filters = {}) {
  const payload = await api(`/strategy-marketplace${buildQuery({ limit: 60, ...filters })}`);
  state.marketplace = payload.items || [];
  renderMarketplaceList();
  if (state.selectedMarketId && !state.marketplace.some((item) => item.id === state.selectedMarketId)) {
    state.selectedMarketId = null;
    state.marketplaceDetail = null;
    renderMarketplaceDetail();
  }
}

async function loadMarketplaceDetail(marketId) {
  if (!marketId) {
    state.selectedMarketId = null;
    state.marketplaceDetail = null;
    renderMarketplaceDetail();
    return;
  }
  const payload = await api(`/strategy-marketplace/${marketId}`);
  state.selectedMarketId = marketId;
  state.marketplaceDetail = payload.item;
  renderMarketplaceList();
  renderMarketplaceDetail();
}

async function loadStrategyMonitor(strategyId, { runId = null, forceLog = false } = {}) {
  state.selectedStrategyId = strategyId;
  state.selectedRunId = runId;
  await Promise.all([refreshLogs(forceLog), loadOrders(), loadDecisions()]);
}

async function loadStrategyGovernance(strategyId) {
  state.selectedStrategyId = strategyId;
  await Promise.all([loadVersions(), loadRunHistory(), loadRecoveryActions(), loadMembers()]);
  const strategy = getSelectedStrategy();
  syncPublishForm(strategy);
}

async function refreshAll() {
  await Promise.all([loadSummary(), loadCredentials(), loadUsers(), loadStrategies(), loadPresets(), loadCapabilities(), loadBacktests(), loadAlerts(), loadAuditLogs(), loadMarketplace()]);
  if (state.selectedStrategyId) {
    await Promise.all([
      loadStrategyMonitor(state.selectedStrategyId, { runId: state.selectedRunId, forceLog: true }).catch(() => {}),
      loadStrategyGovernance(state.selectedStrategyId).catch(() => {}),
    ]);
  } else {
    renderLogs();
    renderStrategyDetails();
    renderVersions();
    renderRunHistory();
    renderRecoveryActions();
    renderMembers();
  }
  if (state.selectedBacktestId) {
    await loadBacktestDetail(state.selectedBacktestId).catch(() => {});
  } else {
    renderBacktestDetail();
  }
  if (state.selectedMarketId) {
    await loadMarketplaceDetail(state.selectedMarketId).catch(() => {});
  } else {
    renderMarketplaceDetail();
  }
}

function disconnectRealtime() {
  if (state.wsRetryTimer) {
    window.clearTimeout(state.wsRetryTimer);
    state.wsRetryTimer = null;
  }
  state.wsRetryCount = 0;
  state.wsReconnectSuppressed = false;
  if (state.ws) {
    state.wsManualClose = true;
    try {
      state.ws.close();
    } catch (error) {
      state.wsManualClose = false;
    }
  }
  state.ws = null;
  setRealtimeStatus(false, '实时通道未连接');
}

function scheduleRealtimeReconnect(event) {
  if (state.wsManualClose) {
    state.wsManualClose = false;
    return;
  }
  if (!state.token) return;
  if (event?.code === 4401) {
    state.wsReconnectSuppressed = true;
    setRealtimeStatus(false, '实时登录已失效，请重新登录');
    return;
  }
  if (state.wsReconnectSuppressed) return;
  state.wsRetryCount += 1;
  if (state.wsRetryCount > 6) {
    state.wsReconnectSuppressed = true;
    setRealtimeStatus(false, '实时通道暂不可用，已切换轮询同步');
    return;
  }
  const delay = Math.min(2000 * (2 ** Math.max(state.wsRetryCount - 1, 0)), 15000);
  setRealtimeStatus(false, `实时通道已断开，${Math.round(delay / 1000)} 秒后重连...`);
  state.wsRetryTimer = window.setTimeout(() => {
    state.wsRetryTimer = null;
    connectRealtime();
  }, delay);
}

function queueRealtimeRefresh(flags = {}) {
  state.refreshFlags = { ...state.refreshFlags, ...flags };
  if (state.refreshDebounceTimer) return;
  state.refreshDebounceTimer = window.setTimeout(async () => {
    const activeFlags = { ...state.refreshFlags };
    state.refreshFlags = {};
    state.refreshDebounceTimer = null;
    try {
      const tasks = [];
      if (activeFlags.summary) tasks.push(loadSummary());
      if (activeFlags.credentials) tasks.push(loadCredentials());
      if (activeFlags.users) tasks.push(loadUsers());
      if (activeFlags.strategies) tasks.push(loadStrategies());
      if (activeFlags.backtests) tasks.push(loadBacktests());
      if (activeFlags.alerts) tasks.push(loadAlerts());
      if (activeFlags.audit) tasks.push(loadAuditLogs());
      if (activeFlags.market) tasks.push(loadMarketplace());
      if (tasks.length) await Promise.all(tasks);
      if (activeFlags.logs && state.selectedStrategyId) await refreshLogs(true);
      if (activeFlags.orders && state.selectedStrategyId) await Promise.all([loadOrders(), loadDecisions()]);
      if (activeFlags.history && state.selectedStrategyId) await Promise.all([loadRunHistory(), loadRecoveryActions(), loadMembers()]);
      if (activeFlags.versions && state.selectedStrategyId) await loadVersions();
    } catch (error) {
      console.error(error);
    }
  }, 250);
}

function connectRealtime() {
  if (!state.token || state.ws || state.wsReconnectSuppressed) return;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${protocol}//${window.location.host}/api/ws?token=${encodeURIComponent(state.token)}`);
  state.ws = ws;
  setRealtimeStatus(false, '实时通道连接中...');

  ws.addEventListener('open', () => {
    state.wsRetryCount = 0;
    state.wsReconnectSuppressed = false;
    state.wsManualClose = false;
    setRealtimeStatus(true, '实时通道已连接');
  });

  ws.addEventListener('message', (event) => {
    try {
      const message = JSON.parse(event.data);
      const type = message.type;
      if (type === 'connected' || type === 'heartbeat') return;
      if (['runtime_update', 'strategy_started', 'strategy_stopping', 'strategy_stopped'].includes(type)) {
        queueRealtimeRefresh({ summary: true, strategies: true, logs: true, orders: true, history: true });
        return;
      }
      if (type === 'log_update') {
        queueRealtimeRefresh({ logs: true });
        return;
      }
      if (type === 'orders_update') {
        queueRealtimeRefresh({ orders: true, strategies: true, summary: true, history: true });
        return;
      }
      if (type === 'credentials_changed') {
        queueRealtimeRefresh({ credentials: true, summary: true, strategies: true, audit: true });
        return;
      }
      if (type === 'strategies_changed') {
        queueRealtimeRefresh({ strategies: true, summary: true, logs: true, orders: true, backtests: true, versions: true, history: true, audit: true });
        return;
      }
      if (type === 'backtests_changed') {
        queueRealtimeRefresh({ backtests: true, summary: true, strategies: true, audit: true });
        return;
      }
      if (type === 'alerts_changed') {
        queueRealtimeRefresh({ alerts: true, summary: true, audit: true });
        return;
      }
      if (type === 'recovery_changed') {
        queueRealtimeRefresh({ history: true, alerts: true, audit: true, summary: true });
        return;
      }
      if (type === 'audit_changed') {
        queueRealtimeRefresh({ audit: true });
      }
    } catch (error) {
      console.error(error);
    }
  });

  ws.addEventListener('close', (event) => {
    state.ws = null;
    scheduleRealtimeReconnect(event);
  });

  ws.addEventListener('error', () => {
    setRealtimeStatus(false, state.wsRetryCount ? '实时通道异常，等待重连' : '实时通道异常，尝试恢复中');
  });
}

async function handleSetup(event) {
  event.preventDefault();
  const data = toFormData(els.setupForm);
  try {
    await api('/bootstrap/setup', { method: 'POST', body: JSON.stringify(data) });
    showFlash('初始化完成，请使用新账号登录。', 'success');
    els.setupForm.reset();
    showView('loginView');
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleLogin(event) {
  event.preventDefault();
  const data = toFormData(els.loginForm);
  try {
    const payload = await api('/auth/login', { method: 'POST', body: JSON.stringify(data) });
    state.token = payload.token;
    state.wsRetryCount = 0;
    state.wsReconnectSuppressed = false;
    state.wsManualClose = false;
    localStorage.setItem('dscp_admin_token', state.token);
    showView('appView');
    connectRealtime();
    await refreshAll();
    showFlash('登录成功。', 'success');
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleLogout() {
  try {
    await api('/auth/logout', { method: 'POST', body: JSON.stringify({}) });
  } catch (error) {
    // noop
  }
  disconnectRealtime();
  state.token = '';
  state.user = null;
  state.selectedStrategyId = null;
  state.selectedRunId = null;
  state.selectedBacktestId = null;
  state.selectedMarketId = null;
  localStorage.removeItem('dscp_admin_token');
  els.currentUser.classList.add('hidden');
  els.logoutBtn.classList.add('hidden');
  showView('loginView');
}

async function handleCredentialSubmit(event) {
  event.preventDefault();
  const data = toFormData(els.credentialForm);
  const payload = {
    name: data.name,
    description: data.description,
    exchange: data.exchange,
    api_key: data.api_key,
    api_secret: data.api_secret,
    api_passphrase: data.api_passphrase,
    deepseek_api_key: data.deepseek_api_key,
    deepseek_base_url: data.deepseek_base_url,
    simulated_trading: els.credentialForm.elements.simulated_trading.checked,
    config: collectCredentialConfig(),
  };
  try {
    const credentialId = data.credentialId;
    if (credentialId) {
      await api(`/credentials/${credentialId}`, { method: 'PUT', body: JSON.stringify(payload) });
      showFlash('交易账号已更新。', 'success');
    } else {
      await api('/credentials', { method: 'POST', body: JSON.stringify(payload) });
      showFlash('交易账号已创建。', 'success');
    }
    resetCredentialForm();
    await Promise.all([loadSummary(), loadCredentials(), loadStrategies(), loadAuditLogs()]);
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleUserSubmit(event) {
  event.preventDefault();
  const data = toFormData(els.userForm);
  try {
    await api('/users', {
      method: 'POST',
      body: JSON.stringify({ username: data.username, password: data.password, role: data.role }),
    });
    els.userForm.reset();
    showFlash('用户已创建。', 'success');
    await Promise.all([loadUsers(), loadAuditLogs()]);
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleStrategySubmit(event) {
  event.preventDefault();
  const data = toFormData(els.strategyForm);
  const payload = {
    name: data.name,
    description: data.description,
    credential_id: Number(data.credential_id),
    symbols: data.symbols,
    timeframe: data.timeframe,
    risk_preset: data.risk_preset,
    leverage: Number(data.leverage || 1),
    prompt_template: data.prompt_template,
    market_type: data.market_type,
    margin_mode: data.margin_mode,
    indicator_dsl: data.indicator_dsl,
    entry_rule: data.entry_rule,
    exit_rule: data.exit_rule,
    version_note: data.version_note || (data.strategyId ? '调整策略配置' : '创建策略'),
    config: collectStrategyConfig(),
  };
  try {
    const strategyId = data.strategyId;
    if (strategyId) {
      await api(`/strategies/${strategyId}`, { method: 'PUT', body: JSON.stringify(payload) });
      showFlash('策略已更新，并生成新版本。', 'success');
    } else {
      await api('/strategies', { method: 'POST', body: JSON.stringify(payload) });
      showFlash('策略已创建。', 'success');
    }
    resetStrategyForm();
    await Promise.all([loadSummary(), loadStrategies(), loadBacktests(), loadAuditLogs()]);
    if (state.selectedStrategyId) {
      await loadStrategyGovernance(state.selectedStrategyId).catch(() => {});
    }
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleMemberSubmit(event) {
  event.preventDefault();
  if (!state.selectedStrategyId) {
    showFlash('请先选择一个策略。', 'error');
    return;
  }
  const data = toFormData(els.memberForm);
  try {
    await api(`/strategies/${state.selectedStrategyId}/members`, {
      method: 'POST',
      body: JSON.stringify({ user_id: Number(data.user_id), role: data.role }),
    });
    showFlash('协作成员已更新。', 'success');
    await Promise.all([loadMembers(), loadStrategies(), loadAuditLogs()]);
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handlePublishSubmit(event) {
  event.preventDefault();
  if (!state.selectedStrategyId) {
    showFlash('请先选择一个策略再发布。', 'error');
    return;
  }
  const data = toFormData(els.publishForm);
  try {
    await api(`/strategies/${state.selectedStrategyId}/market/publish`, {
      method: 'POST',
      body: JSON.stringify({
        title: data.title,
        summary: data.summary,
        description: data.description,
        category: data.category,
        tags: String(data.tags || '').split(',').map((item) => item.trim()).filter(Boolean),
      }),
    });
    showFlash('策略已发布到市场。', 'success');
    await Promise.all([loadMarketplace(), loadAuditLogs(), loadSummary()]);
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleBacktestSubmit(event) {
  event.preventDefault();
  const data = toFormData(els.backtestForm);
  const payload = {
    strategy_id: Number(data.strategy_id),
    symbol: data.symbol || null,
    timeframe: data.timeframe || null,
    engine: data.engine || null,
    bars: Number(data.bars || 320),
    initial_capital_usdt: Number(data.initial_capital_usdt || 1000),
  };
  try {
    const result = await api('/backtests/run', { method: 'POST', body: JSON.stringify(payload) });
    showFlash('回测已完成。', 'success');
    await loadBacktests();
    await loadBacktestDetail(result.item.id);
    await loadAuditLogs();
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleCredentialAction(action, credentialId) {
  const item = state.credentials.find((row) => row.id === credentialId);
  if (!item) return;
  if (action === 'edit-credential') {
    fillCredentialForm(item);
    return;
  }
  if (action === 'delete-credential') {
    if (!window.confirm(`确认删除账号「${item.name}」吗？`)) return;
    try {
      await api(`/credentials/${credentialId}`, { method: 'DELETE' });
      showFlash('交易账号已删除。', 'success');
      if (state.editingCredentialId === credentialId) resetCredentialForm();
      await Promise.all([loadSummary(), loadCredentials(), loadStrategies(), loadAuditLogs()]);
    } catch (error) {
      showFlash(error.message, 'error');
    }
  }
}

async function handleUserAction(action, userId, role) {
  const item = state.users.find((row) => row.id === userId);
  if (!item) return;
  try {
    if (action === 'set-user-role') {
      await api(`/users/${userId}`, { method: 'PUT', body: JSON.stringify({ role }) });
      showFlash(`已将 ${item.username} 调整为 ${role}。`, 'success');
    }
    if (action === 'reset-user-password') {
      const password = window.prompt(`请输入用户 ${item.username} 的新密码（至少 8 位）`);
      if (!password) return;
      await api(`/users/${userId}`, { method: 'PUT', body: JSON.stringify({ password }) });
      showFlash(`已重置 ${item.username} 的密码。`, 'success');
    }
    await Promise.all([loadUsers(), loadAuditLogs()]);
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleStrategyAction(action, strategyId) {
  const strategy = state.strategies.find((item) => item.id === strategyId);
  if (!strategy) return;
  try {
    if (action === 'edit-strategy') {
      fillStrategyForm(strategy);
      return;
    }
    if (action === 'view-strategy') {
      setActiveModule('governance', { scroll: true });
      state.selectedRunId = null;
      await Promise.all([
        loadStrategyMonitor(strategyId, { runId: null, forceLog: true }),
        loadStrategyGovernance(strategyId),
      ]);
      syncPublishForm(strategy);
      return;
    }
    if (action === 'run-backtest') {
      setActiveModule('backtests', { scroll: true });
      primeBacktestForm(strategy);
      els.backtestForm.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    if (action === 'start-strategy') {
      await api(`/strategies/${strategyId}/start`, { method: 'POST', body: JSON.stringify({}) });
      state.selectedStrategyId = strategyId;
      state.selectedRunId = null;
      showFlash(`策略「${strategy.name}」已启动。`, 'success');
    }
    if (action === 'stop-strategy') {
      await api(`/strategies/${strategyId}/stop`, { method: 'POST', body: JSON.stringify({}) });
      showFlash(`策略「${strategy.name}」已发出停止请求。`, 'success');
    }
    if (action === 'delete-strategy') {
      if (!window.confirm(`确认删除策略「${strategy.name}」吗？`)) return;
      await api(`/strategies/${strategyId}`, { method: 'DELETE' });
      showFlash(`策略「${strategy.name}」已删除。`, 'success');
      if (state.selectedStrategyId === strategyId) {
        state.selectedStrategyId = null;
        state.selectedRunId = null;
        state.monitor = { run: null, logs: [], orders: [], trades: [], decisions: [] };
        state.versions = [];
        state.runHistory = [];
        state.runHistorySummary = null;
        state.recoveryActions = [];
        state.members = [];
        state.owner = null;
        renderLogs();
        renderStrategyDetails();
        renderVersions();
        renderRunHistory();
        renderRecoveryActions();
        renderMembers();
      }
    }
    await Promise.all([loadSummary(), loadStrategies(), loadBacktests(), loadAuditLogs(), loadAlerts()]);
    if (state.selectedStrategyId) {
      await Promise.all([
        loadStrategyMonitor(state.selectedStrategyId, { runId: state.selectedRunId, forceLog: true }).catch(() => {}),
        loadStrategyGovernance(state.selectedStrategyId).catch(() => {}),
      ]);
    }
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleVersionAction(versionId) {
  if (!state.selectedStrategyId) return;
  const version = state.versions.find((item) => item.id === versionId);
  if (!version) return;
  if (!window.confirm(`确认将当前策略恢复到 V${version.version_no} 吗？系统会生成一个新的恢复版本。`)) return;
  try {
    await api(`/strategies/${state.selectedStrategyId}/versions/${versionId}/restore`, {
      method: 'POST',
      body: JSON.stringify({ note: `恢复自 V${version.version_no}` }),
    });
    showFlash(`已恢复到 V${version.version_no}，并生成新版本快照。`, 'success');
    await Promise.all([loadStrategies(), loadVersions(), loadAuditLogs()]);
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleRunHistoryAction(runId) {
  if (!state.selectedStrategyId) return;
  state.selectedRunId = runId;
  try {
    await loadStrategyMonitor(state.selectedStrategyId, { runId, forceLog: true });
    showFlash(`已切换到 Run #${runId} 的明细视图。`, 'success');
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleAlertAction(alertId) {
  try {
    await api(`/alerts/${alertId}/ack`, { method: 'POST', body: JSON.stringify({}) });
    showFlash('告警已确认。', 'success');
    await Promise.all([loadAlerts(), loadAuditLogs(), loadSummary()]);
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleMemberAction(action, userId, role) {
  if (!state.selectedStrategyId) return;
  setActiveModule('governance', { scroll: true });
  try {
    if (action === 'set-member-role') {
      await api(`/strategies/${state.selectedStrategyId}/members`, {
        method: 'POST',
        body: JSON.stringify({ user_id: userId, role }),
      });
      showFlash('成员角色已更新。', 'success');
    }
    if (action === 'remove-member') {
      if (!window.confirm('确认移除该协作成员吗？')) return;
      await api(`/strategies/${state.selectedStrategyId}/members/${userId}`, { method: 'DELETE' });
      showFlash('协作成员已移除。', 'success');
    }
    await Promise.all([loadMembers(), loadStrategies(), loadAuditLogs()]);
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function handleMarketplaceInstall() {
  const item = state.marketplaceDetail;
  if (!item) return;
  const credentialSelect = document.getElementById('marketInstallCredentialSelect');
  const nameInput = document.getElementById('marketInstallName');
  const credentialId = Number(credentialSelect?.value || 0);
  if (!credentialId) {
    showFlash('请先选择一个可用账号。', 'error');
    return;
  }
  try {
    await api(`/strategy-marketplace/${item.id}/install`, {
      method: 'POST',
      body: JSON.stringify({ credential_id: credentialId, name: nameInput?.value || undefined }),
    });
    showFlash('策略已导入到当前工作台。', 'success');
    await Promise.all([loadStrategies(), loadSummary(), loadAuditLogs()]);
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

function bindCardActions() {
  els.moduleNav.addEventListener('click', (event) => {
    const button = event.target.closest('[data-module-nav]');
    if (!button) return;
    setActiveModule(button.dataset.moduleNav, { scroll: true });
  });

  els.credentialList.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    handleCredentialAction(button.dataset.action, Number(button.dataset.id));
  });

  els.userList.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    handleUserAction(button.dataset.action, Number(button.dataset.id), button.dataset.role);
  });

  els.strategyList.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    handleStrategyAction(button.dataset.action, Number(button.dataset.id));
  });

  els.versionList.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action="restore-version"]');
    if (!button) return;
    handleVersionAction(Number(button.dataset.id));
  });

  els.runHistoryTable.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action="view-run"]');
    if (!button) return;
    handleRunHistoryAction(Number(button.dataset.runId));
  });

  els.memberList.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    handleMemberAction(button.dataset.action, Number(button.dataset.id), button.dataset.role);
  });

  els.alertsList.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action="ack-alert"]');
    if (!button) return;
    handleAlertAction(Number(button.dataset.id));
  });

  els.backtestList.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action="view-backtest"]');
    if (!button) return;
    loadBacktestDetail(Number(button.dataset.id)).catch((error) => showFlash(error.message, 'error'));
  });

  els.marketplaceList.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action="view-market"]');
    if (!button) return;
    loadMarketplaceDetail(Number(button.dataset.id)).catch((error) => showFlash(error.message, 'error'));
  });

  els.marketplaceDetail.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action="install-market"]');
    if (!button) return;
    handleMarketplaceInstall();
  });
}

async function boot() {
  bindCardActions();
  els.setupForm.addEventListener('submit', handleSetup);
  els.loginForm.addEventListener('submit', handleLogin);
  els.logoutBtn.addEventListener('click', handleLogout);
  els.credentialForm.addEventListener('submit', handleCredentialSubmit);
  els.userForm.addEventListener('submit', handleUserSubmit);
  els.strategyForm.addEventListener('submit', handleStrategySubmit);
  els.memberForm.addEventListener('submit', handleMemberSubmit);
  els.publishForm.addEventListener('submit', handlePublishSubmit);
  els.backtestForm.addEventListener('submit', handleBacktestSubmit);
  els.marketFilterForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const data = toFormData(els.marketFilterForm);
    loadMarketplace({ category: data.category || undefined, keyword: data.keyword || undefined }).catch((error) => showFlash(error.message, 'error'));
  });
  els.credentialResetBtn.addEventListener('click', resetCredentialForm);
  els.strategyResetBtn.addEventListener('click', resetStrategyForm);
  els.refreshLogsBtn.addEventListener('click', () => {
    if (!state.selectedStrategyId) return;
    loadStrategyMonitor(state.selectedStrategyId, { runId: state.selectedRunId, forceLog: true }).catch((error) => showFlash(error.message, 'error'));
  });
  els.refreshGovernanceBtn.addEventListener('click', () => {
    if (!state.selectedStrategyId) return;
    loadStrategyGovernance(state.selectedStrategyId).catch((error) => showFlash(error.message, 'error'));
  });
  els.refreshOpsBtn.addEventListener('click', () => {
    Promise.all([loadAlerts(), loadAuditLogs(), loadSummary()]).catch((error) => showFlash(error.message, 'error'));
  });
  els.refreshUsersBtn.addEventListener('click', () => loadUsers().catch((error) => showFlash(error.message, 'error')));
  els.refreshMarketBtn.addEventListener('click', () => loadMarketplace().catch((error) => showFlash(error.message, 'error')));
  els.strategyForm.elements.risk_preset.addEventListener('change', (event) => applyPreset(event.target.value));
  els.strategyForm.elements.market_type.addEventListener('change', syncMarginModeState);

  resetCredentialForm();
  resetStrategyForm();
  setActiveModule(state.activeModule, { persist: false });
  renderLogs();
  renderStrategyDetails();
  renderVersions();
  renderRunHistory();
  renderRecoveryActions();
  renderMembers();
  renderAlerts();
  renderAuditLogs();
  renderBacktestDetail();
  renderMarketplaceDetail();

  try {
    const bootState = await api('/bootstrap/status', { headers: {} });
    if (bootState.setup_needed) {
      showView('setupView');
      return;
    }

    if (!state.token) {
      showView('loginView');
      return;
    }

    showView('appView');
    connectRealtime();
    await refreshAll();
  } catch (error) {
    state.token = '';
    localStorage.removeItem('dscp_admin_token');
    disconnectRealtime();
    showView('loginView');
    showFlash(error.message, 'error');
  }

  window.setInterval(async () => {
    if (!state.token || els.appView.classList.contains('hidden')) return;
    if (state.wsConnected) return;
    try {
      await Promise.all([loadSummary(), loadStrategies(), loadAlerts()]);
      if (state.selectedStrategyId) {
        await Promise.all([
          loadStrategyMonitor(state.selectedStrategyId, { runId: state.selectedRunId, forceLog: true }).catch(() => {}),
          loadStrategyGovernance(state.selectedStrategyId).catch(() => {}),
        ]);
      }
    } catch (error) {
      console.error(error);
    }
  }, 20000);
}

boot();
