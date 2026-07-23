const state = { mode: 'market', intent: 'open', side: 'buy', venue: 'lighter', marketId: '0', instrumentId: null, bid: null, ask: null, bidSize: null, askSize: null, live: false, tradingEnabled: false, ready: false, durableReady: true, authenticated: false, csrf: null, symbol: '—', marketScope: 'USDC 永续', markets: [], portfolio: null, switchEpoch: 0, switchInFlight: false };
const $ = (selector) => document.querySelector(selector);
const EMPTY = '—';
const venueLabels = { lighter: 'Lighter', hyperliquid: 'Hyperliquid', binance: 'Binance USD-M' };
const venueScopes = { lighter: 'USDC 永续', hyperliquid: 'USDC 永续', binance: 'USDT 永续' };
const fmt = (value) => value == null || !Number.isFinite(Number(value)) ? EMPTY : Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 });
const escapeHtml = (value) => String(value ?? EMPTY).replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));
const now = () => new Date().toLocaleTimeString('zh-CN', { hour12: false });

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.csrf && options.method && options.method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  const response = await fetch(path, { credentials: 'same-origin', ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 || response.status === 403) lockUi(data.detail || '控制台登录已失效');
  if (!response.ok) {
    const error = new Error(data.detail || '请求失败');
    error.status = response.status;
    const retryAfter = Number(response.headers.get('Retry-After'));
    if (Number.isFinite(retryAfter) && retryAfter > 0) error.retryAfter = retryAfter;
    throw error;
  }
  return data;
}

function displaySymbol() { return state.venue === 'binance' ? state.symbol : `${state.symbol}-USDC`; }
function orderPrice() { return state.mode === 'maker' ? (state.side === 'buy' ? state.bid : state.ask) : (state.side === 'buy' ? state.ask : state.bid); }
function action() { return state.intent === 'open' ? (state.side === 'buy' ? '买入开多' : '卖出开空') : (state.side === 'buy' ? '买入平空' : '卖出平多'); }
function requestId() { return window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}-bbo-request`; }

function lockUi(message = '请先解锁控制台') {
  state.authenticated = false; state.csrf = null;
  $('#controlToken').value = ''; $('#controlToken').disabled = false;
  $('#unlockButton').disabled = false; $('#unlockButton').textContent = '解锁控制台';
  $('#liveStatus').textContent = message; $('#connectionText').textContent = '控制台已锁定';
  refreshPreview();
}

function refreshPreview() {
  const enabled = state.authenticated && state.live && state.ready && state.durableReady && orderPrice();
  const mode = state.mode === 'market' ? '吃单' : '跟价挂单';
  const amount = Number($('#quantity').value);
  const amountText = Number.isFinite(amount) && amount > 0 ? `${fmt(amount)} ${$('#quoteCurrency').textContent}` : '请输入金额';
  $('#orderSummary').textContent = `${action()} · ${mode} · ${displaySymbol()} · ${amountText}`;
  const button = $('#executeButton');
  button.textContent = enabled ? action() : (state.authenticated ? (state.durableReady ? '等待行情或 API 就绪' : '服务暂未就绪') : '请先解锁控制台');
  button.className = `preview-button ${state.side === 'sell' ? 'sell' : ''}`;
  button.disabled = !enabled;
}

function setOrderStatus(kind, message, result = '', detail = '') {
  const status = $('#orderStatus');
  status.className = `order-status ${kind}`;
  status.textContent = message;
  $('#orderResult').textContent = result || message;
  $('#executionTechnical').textContent = detail || '没有需要处理的异常。';
  if (kind !== 'error') $('#executionDetails').open = false;
}

function friendlyOrderError(message) {
  const raw = String(message || '提交未完成');
  if (raw.includes('行情已过期') || raw.includes('盘口或预估数量已变化')) return ['价格已变化', '盘口刚刚变动，请再点一次下单即可。'];
  if (raw.includes('quantity exceeds') || raw.includes('MAX_POSITION_BASE')) return ['金额超过交易所限额', '请降低金额后重新提交。'];
  if (raw.includes('credentials') || raw.includes('API key')) return ['交易权限未就绪', '请检查该交易所的 API 配置和交易开关。'];
  if (raw.includes('durable execution') || raw.includes('storage')) return ['服务暂不可下单', '安全订单记录暂不可用，请稍后再试。'];
  return ['订单未提交', '交易所没有接受这笔订单，请确认金额、合约和账户状态后重试。'];
}

function updateMarket(data) {
  state.venue = data.venue || state.venue;
  state.marketId = String(data.market_id ?? data.market_index ?? state.marketId);
  state.symbol = data.symbol || state.symbol;
  state.instrumentId = data.internal_instrument_id || state.instrumentId;
  state.marketScope = data.market_scope || venueScopes[state.venue];
  $('#venueSelect').value = state.venue;
  $('#quoteCurrency').textContent = state.venue === 'binance' ? 'USDT' : 'USDC';
  $('#amountCurrency').textContent = $('#quoteCurrency').textContent;
  $('#marketScope').textContent = state.marketScope;
  $('#marketSymbol').textContent = displaySymbol();
  $('#quantity').min = data.min_quote_amount || '10';
  const option = state.instrumentId ? $(`#marketSelect option[value="${CSS.escape(state.instrumentId)}"]`) : null;
  if (option) $('#marketSelect').value = state.instrumentId;
  refreshPreview();
}

function setQuote(data) {
  state.bid = data.bid == null ? null : Number(data.bid); state.ask = data.ask == null ? null : Number(data.ask);
  state.bidSize = data.bid_size == null ? null : Number(data.bid_size); state.askSize = data.ask_size == null ? null : Number(data.ask);
  $('#bidPrice').textContent = fmt(state.bid); $('#askPrice').textContent = fmt(state.ask);
  $('#bidSize').textContent = state.bidSize == null ? EMPTY : `${state.bidSize} ${state.symbol}`;
  $('#askSize').textContent = state.askSize == null ? EMPTY : `${state.askSize} ${state.symbol}`;
  $('#spread').textContent = state.bid && state.ask ? fmt(state.ask - state.bid) : EMPTY;
  $('#feedStatus').textContent = data.connected ? '行情已连接' : '等待行情连接';
  $('#feedTime').textContent = data.connected ? now() : '—';
  $('#connectionText').textContent = data.connected ? '已连接' : '正在连接';
  refreshPreview();
}

function isCurrentMarketSnapshot(data) {
  if (data.venue && data.venue !== state.venue) return false;
  const instrumentId = data.internal_instrument_id;
  return !(state.instrumentId && instrumentId && instrumentId !== state.instrumentId);
}

function updateStatus(data, { force = false } = {}) {
  // A WebSocket event can be queued just before a market switch. Never let an
  // old Lighter quote overwrite a Hyperliquid/Binance selection (or vice versa).
  if (!force && !isCurrentMarketSnapshot(data)) return false;
  state.live = Boolean(data.live_enabled); state.tradingEnabled = Boolean(data.trading_enabled); state.ready = Boolean(data.credentials_ready);
  if (data.durable_execution) state.durableReady = !data.durable_execution.required || Boolean(data.durable_execution.ready);
  updateMarket(data); setQuote(data);
  $('#executionState').textContent = state.live && state.ready ? '可以下单' : '暂不可下单';
  $('#liveStatus').textContent = !state.authenticated ? '控制台已锁定' : (!state.tradingEnabled ? '真实交易：关闭' : (state.live && state.ready ? '真实交易：已启用' : '真实交易：交易所未解锁'));
  return true;
}

function renderMarkets() {
  const needle = $('#marketSearch').value.trim().toUpperCase();
  const filtered = state.markets.filter((market) => !needle || market.symbol.toUpperCase().includes(needle) || String(market.market_id).toUpperCase().includes(needle));
  $('#marketSelect').innerHTML = filtered.length ? filtered.map((market) => `<option value="${escapeHtml(market.internal_instrument_id)}">${escapeHtml(market.symbol)} · ${escapeHtml(market.market_scope || venueScopes[state.venue])} · #${escapeHtml(market.market_id)}</option>`).join('') : '<option value="">未找到对应合约</option>';
  if (filtered.some((market) => market.internal_instrument_id === state.instrumentId)) $('#marketSelect').value = state.instrumentId;
}

function chartDate(timestamp) {
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(timestamp));
}

function renderEquityChart(rawHistory, fromDate) {
  const target = $('#equityChart');
  const start = Date.parse(`${fromDate || '2026-07-21'}T00:00:00Z`);
  const points = (Array.isArray(rawHistory) ? rawHistory : []).map((point) => ({
    time: Number(point.timestamp), value: Number(point.equity), synced: Number(point.synced_venues || 0),
  })).filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value) && point.time >= start).sort((left, right) => left.time - right.time);
  if (!points.length) {
    target.classList.remove('has-chart');
    target.style.display = ''; target.style.padding = '';
    target.innerHTML = '<p>尚无可验证的账户快照；连接任一账户后会立即开始记录。</p>';
    return;
  }
  target.classList.add('has-chart');
  target.style.display = 'block'; target.style.padding = '14px 20px';
  const width = 760; const height = 230; const left = 54; const right = 20; const top = 22; const bottom = 38;
  const domainEnd = Math.max(Date.now(), points[points.length - 1].time, start + 86400000);
  const values = points.map((point) => point.value);
  const low = Math.min(...values); const high = Math.max(...values);
  const pad = Math.max((high - low) * 0.16, Math.max(Math.abs(high) * 0.025, 1));
  const minY = low - pad; const maxY = high + pad;
  const x = (time) => left + ((time - start) / Math.max(1, domainEnd - start)) * (width - left - right);
  const y = (value) => top + ((maxY - value) / Math.max(0.000001, maxY - minY)) * (height - top - bottom);
  const line = points.map((point, index) => `${index ? 'L' : 'M'}${x(point.time).toFixed(1)},${y(point.value).toFixed(1)}`).join(' ');
  const latest = points[points.length - 1];
  const gridY = [0, .5, 1].map((ratio) => top + ratio * (height - top - bottom));
  target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="账户权益趋势" style="width:100%;height:100%;max-height:260px;overflow:visible">
    ${gridY.map((value) => `<line x1="${left}" y1="${value}" x2="${width - right}" y2="${value}" stroke="#263443" stroke-width="1"/>`).join('')}
    <line x1="${x(start)}" y1="${top}" x2="${x(start)}" y2="${height - bottom}" stroke="#3b4e63" stroke-dasharray="4 5"/>
    <path d="${line}" fill="none" stroke="#3d82f6" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
    ${points.map((point) => `<circle cx="${x(point.time).toFixed(1)}" cy="${y(point.value).toFixed(1)}" r="${point === latest ? 5 : 3}" fill="#42d391" stroke="#101721" stroke-width="2"><title>${chartDate(point.time)} · ${fmt(point.value)} USDC/USDT · ${point.synced} 个账户</title></circle>`).join('')}
    <text x="${left}" y="${height - 12}" fill="#8d9bad" font-size="11">${fromDate || '2026-07-21'}</text>
    <text x="${width - right}" y="${height - 12}" text-anchor="end" fill="#8d9bad" font-size="11">${chartDate(latest.time)}</text>
    <text x="${width - right}" y="${top - 5}" text-anchor="end" fill="#bcd3ec" font-size="11">${fmt(latest.value)} USDC/USDT</text>
  </svg>`;
}

function renderPortfolio(data) {
  state.portfolio = data;
  $('#portfolioFrom').textContent = data.from_date || '2026-07-21';
  $('#portfolioNotice').textContent = data.notice || '等待账户同步。';
  const venues = Array.isArray(data.venues) ? data.venues : [];
  const configured = venues.filter((venue) => venue.configured).length;
  const synced = venues.filter((venue) => venue.synced).length;
  const summary = data.summary && typeof data.summary === 'object' ? data.summary : {};
  const currency = data.currency || 'USDC/USDT';
  const total = (field) => summary[field] == null ? EMPTY : `${fmt(summary[field])} ${currency}`;
  $('#totalEquity').textContent = total('equity');
  $('#availableMargin').textContent = total('available_margin');
  $('#unrealizedPnl').textContent = total('unrealized_pnl');
  $('#configuredVenues').textContent = `${synced} / ${venues.length || 3}`;
  $('#portfolioState').textContent = synced ? `已同步 ${synced} 个账户` : (configured ? '账户读取失败' : '等待账户 API 配置');
  $('#portfolioVenues').innerHTML = venues.map((venue) => {
    const status = venue.status || (venue.configured ? '待读取' : '未配置账户 API');
    return `<div class="venue-row"><span>${escapeHtml(venue.label)}</span><b>${escapeHtml(venue.currency)}</b><small class="${venue.synced ? 'configured' : ''}">${escapeHtml(status)}</small></div>`;
  }).join('') || '<p class="empty-copy">暂无交易所配置</p>';
  $('#portfolioHistoryState').textContent = data.history_status || '等待首条快照';
  renderEquityChart(data.history, data.from_date);
  renderPositions(data.positions || [], synced > 0);
}

function renderPositions(positions, hasSyncedAccount = false) {
  const rows = Array.isArray(positions) ? positions : [];
  $('#positionCount').textContent = `${rows.length}`;
  if (!rows.length) { $('#positionsBody').innerHTML = `<tr class="empty"><td colspan="8">${hasSyncedAccount ? '当前无持仓' : '等待交易所账户持仓同步'}</td></tr>`; return; }
  $('#positionsBody').innerHTML = rows.map((position) => `<tr><td>${escapeHtml(position.venue)}</td><td>${escapeHtml(position.symbol)}</td><td>${escapeHtml(position.side)}</td><td>${escapeHtml(position.quantity)}</td><td>${escapeHtml(position.entry_price)}</td><td>${escapeHtml(position.mark_price)}</td><td>${escapeHtml(position.unrealized_pnl)}</td><td>—</td></tr>`).join('');
}

async function loadMarkets(venue = state.venue, epoch = state.switchEpoch) {
  const data = await api(`/api/markets?venue=${encodeURIComponent(venue)}`);
  if (epoch !== state.switchEpoch || venue !== state.venue) return null;
  state.markets = data.markets || [];
  if (!state.instrumentId && data.selected_venue === venue && data.selected_internal_instrument_id) state.instrumentId = data.selected_internal_instrument_id;
  renderMarkets();
  return data;
}

async function load() {
  try {
    const [health, portfolio] = await Promise.all([api('/api/health'), api('/api/portfolio')]);
    updateStatus(health, { force: !state.instrumentId }); renderPortfolio(portfolio); await loadMarkets();
  } catch (error) { $('#portfolioNotice').textContent = `数据读取失败：${error.message || '后端未启动'}`; }
}

function clearQuoteForSwitch() {
  state.marketId = ''; state.symbol = EMPTY; state.bid = state.ask = state.bidSize = state.askSize = null;
  updateMarket({ venue: state.venue, market_scope: venueScopes[state.venue], min_quote_amount: '10' });
  setQuote({ connected: false, bid: null, ask: null, bid_size: null, ask_size: null });
}

async function switchMarket(venue, instrumentId, epoch) {
  if (!instrumentId || epoch !== state.switchEpoch) return;
  state.venue = venue; state.instrumentId = instrumentId; state.switchInFlight = true;
  clearQuoteForSwitch();
  try {
    const snapshot = await api(`/api/market/${encodeURIComponent(venue)}/${encodeURIComponent(instrumentId)}`, { method: 'POST' });
    if (epoch !== state.switchEpoch) return;
    if (snapshot.venue !== venue || snapshot.internal_instrument_id !== instrumentId) throw new Error('后端返回的交易所或合约与本次选择不一致');
    updateStatus(snapshot, { force: true });
  } catch (error) {
    if (epoch === state.switchEpoch) $('#orderNote').textContent = `切换合约失败：${error.message}`;
  } finally {
    if (epoch === state.switchEpoch) state.switchInFlight = false;
  }
}

async function selectMarket() {
  const instrumentId = $('#marketSelect').value;
  if (!instrumentId) return;
  const epoch = ++state.switchEpoch;
  await switchMarket(state.venue, instrumentId, epoch);
}

async function selectVenue() {
  const venue = $('#venueSelect').value;
  const epoch = ++state.switchEpoch;
  state.venue = venue; state.instrumentId = null; state.markets = [];
  $('#marketSelect').innerHTML = '<option>加载市场…</option>';
  clearQuoteForSwitch();
  try {
    await loadMarkets(venue, epoch);
    if (epoch !== state.switchEpoch) return;
    const instrumentId = $('#marketSelect').value || state.markets[0]?.internal_instrument_id;
    if (!instrumentId) throw new Error('该交易所没有可交易合约');
    $('#marketSelect').value = instrumentId;
    await switchMarket(venue, instrumentId, epoch);
  } catch (error) { if (epoch === state.switchEpoch) $('#orderNote').textContent = `加载${venueLabels[venue]}市场失败：${error.message}`; }
}

async function execute() {
  const notional = Number($('#quantity').value);
  if (!Number.isFinite(notional) || notional <= 0 || !state.instrumentId) {
    setOrderStatus('error', '请先检查金额', '请输入大于 0 的金额，并确认已选择合约。');
    return;
  }
  try {
    setOrderStatus('pending', '正在准备订单', '正在核对当前价格和下单金额。');
    const preview = await api('/api/order-intents', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ venue: state.venue, internal_instrument_id: state.instrumentId, side: state.side, intent: state.intent, mode: state.mode, notional_amount: notional }) });
    const confirmed = window.confirm(`确认${preview.position_meaning}？\n\n${venueLabels[preview.venue]} · ${preview.symbol}\n金额：${Number(preview.notional_amount).toFixed(2)} ${preview.quote_currency}\n方式：${preview.order_mode === 'market' ? '吃单' : '跟价挂单'}\n参考价：${preview.reference_price}\n\n确定后立即提交。`);
    if (!confirmed) { setOrderStatus('waiting', '已取消', '订单没有提交。'); return; }
    setOrderStatus('pending', '正在提交', '订单正在发送到交易所。');
    const submittedAt = performance.now();
    const result = await api('/api/execute', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order_intent_token: preview.order_intent_token, confirm_live: true, request_id: requestId() }) });
    const browserRoundTrip = Math.max(0, Math.round(performance.now() - submittedAt));
    const latency = result.latency || {};
    const serverPrepare = Number(latency.server_pre_exchange_ms);
    const exchangeRoundTrip = Number(latency.exchange_round_trip_ms);
    const serverTotal = Number(latency.server_total_ms);
    setOrderStatus(
      'success',
      '订单已提交',
      '交易所已受理。成交或挂单状态会在下方持仓中更新。',
      `浏览器往返 ${browserRoundTrip}ms；服务端准备 ${Number.isFinite(serverPrepare) ? `${serverPrepare}ms` : '—'}；交易所响应 ${Number.isFinite(exchangeRoundTrip) ? `${exchangeRoundTrip}ms` : '—'}；服务端总计 ${Number.isFinite(serverTotal) ? `${serverTotal}ms` : '—'}。交易所响应不等于成交回报。`,
    );
    $('#orderNote').textContent = '订单已提交。';
    await load();
  } catch (error) {
    const raw = error.message || '提交未完成';
    const [title, result] = friendlyOrderError(raw);
    setOrderStatus('error', title, result, `技术详情：${raw}`);
    $('#executionDetails').open = true;
    $('#orderNote').textContent = result;
  }
}

async function unlock() {
  const token = $('#controlToken').value;
  if (!token) return;
  try { const data = await api('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) }); state.csrf = data.csrf; state.authenticated = true; $('#controlToken').value = ''; $('#controlToken').disabled = true; $('#unlockButton').textContent = '控制台已解锁'; await load(); connect(); }
  catch (error) {
    if (error.status === 429 && Number.isFinite(error.retryAfter)) {
      const remaining = error.retryAfter >= 60 ? `${Math.ceil(error.retryAfter / 60)} 分钟` : `${Math.ceil(error.retryAfter)} 秒`;
      $('#liveStatus').textContent = `已连续多次输入失败，为保护账户，请在 ${remaining} 后再试。`;
    } else if (error.status === 401) {
      $('#liveStatus').textContent = '控制台口令不正确，请核对后再试。';
    } else {
      $('#liveStatus').textContent = '暂时无法解锁，请检查服务连接后重试。';
    }
  }
}

function showPage(page) {
  document.querySelectorAll('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.page === page));
  $('#fundsView').classList.toggle('active', page === 'funds'); $('#marketView').classList.toggle('active', page === 'market');
  $('#pageTitle').textContent = page === 'funds' ? '资金统计' : '市场';
  $('#pageSubtitle').textContent = page === 'funds' ? `账户数据从 ${state.portfolio?.from_date || '2026-07-21'} 起统计` : '下单与当前持仓集中管理';
  window.location.hash = page;
}

let socket;
function connect() {
  if (!state.authenticated || (socket && socket.readyState <= 1)) return;
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws'; socket = new WebSocket(`${protocol}://${location.host}/ws`);
  socket.onmessage = (event) => { const message = JSON.parse(event.data); if (message.type === 'ticker' || message.type === 'market') updateStatus(message.data); if (message.type === 'execution') load(); };
  socket.onclose = () => { if (state.authenticated) setTimeout(connect, 2000); };
}

document.querySelectorAll('[data-page]').forEach((item) => item.addEventListener('click', () => showPage(item.dataset.page)));
document.querySelectorAll('.mode').forEach((button) => button.addEventListener('click', () => { state.mode = button.dataset.mode; document.querySelectorAll('.mode').forEach((item) => item.classList.toggle('active', item === button)); refreshPreview(); }));
document.querySelectorAll('#intentSwitch button').forEach((button) => button.addEventListener('click', () => { state.intent = button.dataset.intent; document.querySelectorAll('#intentSwitch button').forEach((item) => item.classList.toggle('selected', item === button)); document.querySelectorAll('.side-actions button').forEach((item) => item.textContent = state.intent === 'open' ? (item.dataset.side === 'buy' ? '↗ 买入开多' : '↘ 卖出开空') : (item.dataset.side === 'buy' ? '↗ 买入平空' : '↘ 卖出平多')); refreshPreview(); }));
document.querySelectorAll('.side-actions button').forEach((button) => button.addEventListener('click', () => { state.side = button.dataset.side; refreshPreview(); }));
document.querySelectorAll('.quick-size button').forEach((button) => button.addEventListener('click', () => { $('#quantity').value = Number(button.dataset.pct).toFixed(2); refreshPreview(); }));
$('#quantity').addEventListener('input', refreshPreview);
$('#marketSelect').addEventListener('change', selectMarket); $('#marketSearch').addEventListener('input', renderMarkets); $('#venueSelect').addEventListener('change', selectVenue); $('#unlockButton').addEventListener('click', unlock); $('#executeButton').addEventListener('click', execute); $('#controlToken').addEventListener('keydown', (event) => { if (event.key === 'Enter') unlock(); });

// Keep a real account timeline while the authenticated console is open.  The
// backend coalesces these into one durable sample per five-minute bucket.
setInterval(() => {
  if (!state.authenticated) return;
  api('/api/portfolio').then(renderPortfolio).catch(() => {});
}, 5 * 60 * 1000);

(async () => {
  const status = await fetch('/api/session', { credentials: 'same-origin' }).then((response) => response.json()).catch(() => ({}));
  if (status.authenticated && status.csrf) { state.authenticated = true; state.csrf = status.csrf; $('#controlToken').disabled = true; $('#unlockButton').textContent = '控制台已解锁'; await load(); connect(); }
  else lockUi(status.configured ? '请解锁控制台' : '后端未配置 CONTROL_PLANE_TOKEN');
  showPage(location.hash === '#market' ? 'market' : 'funds'); refreshPreview();
})();
