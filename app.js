const state = { mode: 'market', intent: 'open', side: 'buy', venue: 'lighter', marketId: '0', instrumentId: null, bid: null, ask: null, bidSize: null, askSize: null, live: false, tradingEnabled: false, ready: false, durableReady: true, authenticated: false, csrf: null, symbol: '—', marketScope: 'USDC 永续', markets: [], portfolio: null };
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
  if (!response.ok) throw new Error(data.detail || '请求失败');
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
  $('#previewVenue').textContent = venueLabels[state.venue];
  $('#previewMarket').textContent = displaySymbol();
  $('#previewMode').textContent = state.mode === 'market' ? '吃单 · Limit + IOC' : '跟价挂单 · Post Only';
  $('#previewDirection').textContent = action();
  $('#previewPrice').textContent = fmt(orderPrice());
  const button = $('#executeButton');
  button.textContent = enabled ? '预览订单' : (state.authenticated ? (state.durableReady ? '等待 API 解锁' : '等待安全执行账本') : '请先解锁控制台');
  button.className = `preview-button ${state.side === 'sell' ? 'sell' : ''}`;
  button.disabled = !enabled;
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

function updateStatus(data) {
  state.live = Boolean(data.live_enabled); state.tradingEnabled = Boolean(data.trading_enabled); state.ready = Boolean(data.credentials_ready);
  if (data.durable_execution) state.durableReady = !data.durable_execution.required || Boolean(data.durable_execution.ready);
  updateMarket(data); setQuote(data);
  $('#executionState').textContent = state.live && state.ready ? '真实 API 已启用' : '真实交易已锁定';
  $('#liveStatus').textContent = !state.authenticated ? '控制台已锁定' : (!state.tradingEnabled ? '真实交易：关闭' : (state.live && state.ready ? '真实交易：已启用' : '真实交易：交易所未解锁'));
}

function renderMarkets() {
  const needle = $('#marketSearch').value.trim().toUpperCase();
  const filtered = state.markets.filter((market) => !needle || market.symbol.toUpperCase().includes(needle) || String(market.market_id).toUpperCase().includes(needle));
  $('#marketSelect').innerHTML = filtered.length ? filtered.map((market) => `<option value="${escapeHtml(market.internal_instrument_id)}">${escapeHtml(market.symbol)} · ${escapeHtml(market.market_scope || venueScopes[state.venue])} · #${escapeHtml(market.market_id)}</option>`).join('') : '<option value="">未找到对应合约</option>';
  if (filtered.some((market) => market.internal_instrument_id === state.instrumentId)) $('#marketSelect').value = state.instrumentId;
}

function renderPortfolio(data) {
  state.portfolio = data;
  $('#portfolioFrom').textContent = data.from_date || '2026-07-21';
  $('#portfolioNotice').textContent = data.notice || '等待账户同步。';
  const venues = Array.isArray(data.venues) ? data.venues : [];
  const configured = venues.filter((venue) => venue.configured).length;
  const synced = venues.filter((venue) => venue.synced).length;
  const summary = data.summary && typeof data.summary === 'object' ? data.summary : {};
  const total = (field) => Object.entries(summary).map(([currency, values]) => {
    const amount = values && typeof values === 'object' ? values[field] : null;
    return amount == null ? null : `${fmt(amount)} ${currency}`;
  }).filter(Boolean).join(' · ') || EMPTY;
  $('#totalEquity').textContent = total('equity');
  $('#availableMargin').textContent = total('available_margin');
  $('#unrealizedPnl').textContent = total('unrealized_pnl');
  $('#configuredVenues').textContent = `${synced} / ${venues.length || 3}`;
  $('#portfolioState').textContent = synced ? `已同步 ${synced} 个账户` : (configured ? '账户读取失败' : '等待账户 API 配置');
  $('#portfolioVenues').innerHTML = venues.map((venue) => {
    const status = venue.status || (venue.configured ? '待读取' : '未配置账户 API');
    return `<div class="venue-row"><span>${escapeHtml(venue.label)}</span><b>${escapeHtml(venue.currency)}</b><small class="${venue.synced ? 'configured' : ''}">${escapeHtml(status)}</small></div>`;
  }).join('') || '<p class="empty-copy">暂无交易所配置</p>';
  renderPositions(data.positions || [], synced > 0);
}

function renderPositions(positions, hasSyncedAccount = false) {
  const rows = Array.isArray(positions) ? positions : [];
  $('#positionCount').textContent = `${rows.length}`;
  if (!rows.length) { $('#positionsBody').innerHTML = `<tr class="empty"><td colspan="8">${hasSyncedAccount ? '当前无持仓' : '等待交易所账户持仓同步'}</td></tr>`; return; }
  $('#positionsBody').innerHTML = rows.map((position) => `<tr><td>${escapeHtml(position.venue)}</td><td>${escapeHtml(position.symbol)}</td><td>${escapeHtml(position.side)}</td><td>${escapeHtml(position.quantity)}</td><td>${escapeHtml(position.entry_price)}</td><td>${escapeHtml(position.mark_price)}</td><td>${escapeHtml(position.unrealized_pnl)}</td><td>—</td></tr>`).join('');
}

async function loadMarkets() {
  const data = await api(`/api/markets?venue=${encodeURIComponent(state.venue)}`);
  state.markets = data.markets || [];
  if (data.selected_venue === state.venue && data.selected_internal_instrument_id) state.instrumentId = data.selected_internal_instrument_id;
  renderMarkets();
}

async function load() {
  try {
    const [health, portfolio] = await Promise.all([api('/api/health'), api('/api/portfolio')]);
    updateStatus(health); renderPortfolio(portfolio); await loadMarkets();
  } catch (error) { $('#portfolioNotice').textContent = `数据读取失败：${error.message || '后端未启动'}`; }
}

async function selectMarket() {
  const instrumentId = $('#marketSelect').value;
  if (!instrumentId) return;
  try { updateStatus(await api(`/api/market/${encodeURIComponent(state.venue)}/${encodeURIComponent(instrumentId)}`, { method: 'POST' })); }
  catch (error) { $('#orderNote').textContent = `切换合约失败：${error.message}`; }
}

async function selectVenue() {
  state.venue = $('#venueSelect').value; state.instrumentId = null; state.markets = [];
  $('#marketSelect').innerHTML = '<option>加载市场…</option>';
  try {
    await loadMarkets();
    const current = state.markets.find((market) => String(market.market_id) === state.marketId);
    if (!current && state.markets.length) { $('#marketSelect').value = state.markets[0].internal_instrument_id; await selectMarket(); }
  } catch (error) { $('#orderNote').textContent = `加载${venueLabels[state.venue]}市场失败：${error.message}`; }
}

async function execute() {
  const notional = Number($('#quantity').value);
  if (!Number.isFinite(notional) || notional <= 0 || !state.instrumentId) return;
  try {
    const preview = await api('/api/order-intents', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ venue: state.venue, internal_instrument_id: state.instrumentId, side: state.side, intent: state.intent, mode: state.mode, notional_amount: notional }) });
    const confirmed = window.confirm(`确认提交真实订单？\n\n交易所：${venueLabels[preview.venue]}\n合约：${preview.symbol} · ${preview.market_id}\n操作：${preview.position_meaning}\n执行：${preview.order_mode === 'market' ? '吃一价 IOC' : '跟一价 Post Only'}\n金额：${Number(preview.notional_amount).toFixed(2)} ${preview.quote_currency}\n预估数量：${preview.estimated_quantity}\n参考价格：${preview.reference_price}\n\n本确认仅对本次订单有效；盘口变化后必须重新预览。`);
    if (!confirmed) return;
    await api('/api/execute', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order_intent_token: preview.order_intent_token, confirm_live: true, request_id: requestId() }) });
    $('#orderNote').textContent = '订单已提交，等待交易所确认。';
    await load();
  } catch (error) { $('#orderNote').textContent = `交易所拒绝：${error.message}`; }
}

async function unlock() {
  const token = $('#controlToken').value;
  if (!token) return;
  try { const data = await api('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) }); state.csrf = data.csrf; state.authenticated = true; $('#controlToken').value = ''; $('#controlToken').disabled = true; $('#unlockButton').textContent = '控制台已解锁'; await load(); connect(); }
  catch (error) { $('#liveStatus').textContent = `解锁失败：${error.message}`; }
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
document.querySelectorAll('.quick-size button').forEach((button) => button.addEventListener('click', () => { $('#quantity').value = Number(button.dataset.pct).toFixed(2); }));
$('#marketSelect').addEventListener('change', selectMarket); $('#marketSearch').addEventListener('input', renderMarkets); $('#venueSelect').addEventListener('change', selectVenue); $('#unlockButton').addEventListener('click', unlock); $('#executeButton').addEventListener('click', execute); $('#controlToken').addEventListener('keydown', (event) => { if (event.key === 'Enter') unlock(); });

(async () => {
  const status = await fetch('/api/session', { credentials: 'same-origin' }).then((response) => response.json()).catch(() => ({}));
  if (status.authenticated && status.csrf) { state.authenticated = true; state.csrf = status.csrf; $('#controlToken').disabled = true; $('#unlockButton').textContent = '控制台已解锁'; await load(); connect(); }
  else lockUi(status.configured ? '请解锁控制台' : '后端未配置 CONTROL_PLANE_TOKEN');
  showPage(location.hash === '#market' ? 'market' : 'funds'); refreshPreview();
})();
