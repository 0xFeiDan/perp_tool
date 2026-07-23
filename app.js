const state = { mode: 'market', intent: 'open', side: 'buy', venue: 'lighter', marketId: '0', instrumentId: null, bid: null, ask: null, bidSize: null, askSize: null, live: false, tradingEnabled: false, ready: false, durableReady: true, authenticated: false, csrf: null, symbol: 'ETH', sizeDecimals: 4, orders: [], markets: [], mt5Status: null };
const $ = (selector) => document.querySelector(selector);
const EMPTY = '\u2014';
const fmt = (value) => value == null ? EMPTY : Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 });
const now = () => new Date().toLocaleTimeString('zh-CN', { hour12: false });
const venueLabels = { lighter: 'Lighter', hyperliquid: 'Hyperliquid', binance: 'Binance USD-M' };
const escapeHtml = (value) => String(value ?? EMPTY).replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));

function log(message, kind = '') { const item = document.createElement('p'); if (kind) item.className = kind; const time = document.createElement('time'); time.textContent = now(); item.append(time, document.createTextNode(String(message))); $('#log').prepend(item); }
function action() { return state.intent === 'open' ? (state.side === 'buy' ? '\u5f00\u591a' : '\u5f00\u7a7a') : (state.side === 'buy' ? '\u5e73\u7a7a' : '\u5e73\u591a'); }
function target() { return state.side === 'buy' ? (state.mode === 'market' ? '\u5403\u5356\u4e00' : '\u8ddf\u4e70\u4e00') : (state.mode === 'market' ? '\u5403\u4e70\u4e00' : '\u8ddf\u5356\u4e00'); }
function orderPrice() { return state.mode === 'maker' ? (state.side === 'buy' ? state.bid : state.ask) : (state.side === 'buy' ? state.ask : state.bid); }
function requestId() { return window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}-bbo-request`; }

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.csrf && options.method && options.method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  const response = await fetch(path, { credentials: 'same-origin', ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 || response.status === 403) { lockUi(data.detail || '\u63a7\u5236\u53f0\u767b\u5f55\u5df2\u5931\u6548'); }
  if (!response.ok) throw new Error(data.detail || '\u8bf7\u6c42\u5931\u8d25');
  return data;
}

function lockUi(message = '\u8bf7\u5148\u89e3\u9501\u63a7\u5236\u53f0') {
  state.authenticated = false; state.csrf = null;
  $('#controlToken').value = '';
  $('#controlToken').disabled = false; $('#unlockButton').disabled = false;
  $('#unlockButton').textContent = '\u89e3\u9501\u63a7\u5236\u53f0';
  $('#liveStatus').textContent = message;
  $('#connectionText').textContent = '\u63a7\u5236\u53f0\u5df2\u9501\u5b9a';
  renderMt5Locked(message);
  refreshPreview();
}

function refreshPreview() {
  const enabled = state.authenticated && state.live && state.ready && state.durableReady && orderPrice();
  $('#previewLabel').textContent = state.side === 'buy' ? (state.mode === 'market' ? '\u4e70\u5165\u5c06\u5403\u5356\u4e00' : '\u4e70\u5355\u8ddf\u968f\u4e70\u4e00') : (state.mode === 'market' ? '\u5356\u51fa\u5c06\u5403\u4e70\u4e00' : '\u5356\u5355\u8ddf\u968f\u5356\u4e00');
  $('#previewPrice').textContent = fmt(orderPrice());
  $('#orderType').textContent = state.mode === 'market' ? 'Limit + IOC' : 'Limit + Post Only';
  $('#reduceOnly').textContent = state.intent === 'close' ? '\u5f00\u542f' : '\u5173\u95ed';
  const button = $('#executeButton');
  button.textContent = enabled ? `\u63d0\u4ea4\u771f\u5b9e${action()} \u00b7 ${target()}` : (state.authenticated ? (state.durableReady ? '\u7b49\u5f85 API \u89e3\u9501' : '\u7b49\u5f85\u5b89\u5168\u6267\u884c\u8d26\u672c') : '\u8bf7\u5148\u89e3\u9501\u63a7\u5236\u53f0');
  button.className = `execute ${state.side === 'buy' ? 'buy' : 'sell'}`;
  button.disabled = !enabled || !$('#liveConfirm').checked;
}

function updateMarket(data) {
  state.venue = data.venue || state.venue; state.marketId = String(data.market_id ?? data.market_index ?? state.marketId); state.symbol = data.symbol || state.symbol;
  state.instrumentId = data.internal_instrument_id || state.instrumentId;
  state.sizeDecimals = Number(data.size_decimals ?? state.sizeDecimals);
  $('#marketSymbol').textContent = `${state.symbol}${state.venue === 'binance' ? '' : '-USD'}`;
  $('#marketIndex').textContent = `${venueLabels[state.venue]} · ${state.marketId}`;
  $('#quoteCurrency').textContent = state.venue === 'binance' ? 'USDT' : 'USDC';
  $('#bookBaseSymbol').textContent = state.symbol;
  const quantity = $('#quantity'); quantity.min = data.min_quote_amount || quantity.min; quantity.step = '0.01'; if (Number(quantity.value) < Number(quantity.min)) quantity.value = quantity.min;
  const option = state.instrumentId ? $(`#marketSelect option[value="${CSS.escape(state.instrumentId)}"]`) : null; if (option) $('#marketSelect').value = state.instrumentId;
  $('#venueSelect').value = state.venue;
}

function setQuote(data) {
  state.bid = data.bid == null ? null : Number(data.bid); state.ask = data.ask == null ? null : Number(data.ask);
  state.bidSize = data.bid_size == null ? null : Number(data.bid_size); state.askSize = data.ask_size == null ? null : Number(data.ask_size);
  $('#bidPrice').textContent = fmt(state.bid); $('#askPrice').textContent = fmt(state.ask); const midpoint = state.bid && state.ask ? (state.bid + state.ask) / 2 : null;
  $('#markPrice').textContent = fmt(midpoint); $('#midPrice').textContent = fmt(midpoint); $('#midUsd').textContent = midpoint == null ? EMPTY : `\u2248 $${fmt(midpoint)}`;
  $('#bidSize').textContent = state.bidSize == null ? EMPTY : `${state.bidSize} ${state.symbol}`; $('#askSize').textContent = state.askSize == null ? EMPTY : `${state.askSize} ${state.symbol}`;
  $('#feedStatus').textContent = data.connected ? '\u76d8\u53e3\u5df2\u8fde\u63a5' : '\u7b49\u5f85\u5b9e\u65f6\u76d8\u53e3'; $('#feedTime').textContent = data.connected ? '\u521a\u521a\u66f4\u65b0' : '--';
  $('#asks').innerHTML = state.ask == null ? '' : row(state.ask, state.askSize, 'ask'); $('#bids').innerHTML = state.bid == null ? '' : row(state.bid, state.bidSize, 'bid'); refreshPreview();
}

function row(price, size, side) { return `<div class="book-row"><span class="${side === 'ask' ? 'red' : 'green'}">${escapeHtml(fmt(price))}</span><span>${escapeHtml(size)}</span><span>${escapeHtml(size)}</span></div>`; }
function updateStatus(data) {
  state.live = Boolean(data.live_enabled); state.tradingEnabled = Boolean(data.trading_enabled); state.ready = Boolean(data.credentials_ready); if (data.durable_execution) state.durableReady = !data.durable_execution.required || Boolean(data.durable_execution.ready); updateMarket(data); setQuote(data);
  $('#executionState').textContent = state.live && state.ready ? 'LIVE API' : 'API LOCKED';
  $('#liveStatus').textContent = !state.authenticated ? '\u63a7\u5236\u53f0\u5df2\u9501\u5b9a' : (!state.tradingEnabled ? '\u53ea\u8bfb\u6a21\u5f0f\uff1aTRADING_ENABLED=false' : (!state.durableReady ? '\u771f\u5b9e\u6267\u884c\u8d26\u672c\u4e0d\u53ef\u7528\uff0c\u5df2\u9501\u5b9a\u4e0b\u5355' : (state.live && state.ready ? `${venueLabels[state.venue]} \u771f\u5b9e API \u5df2\u542f\u7528` : (state.ready ? `\u8bf7\u8bbe\u7f6e ${state.venue.toUpperCase()}_LIVE_TRADING=true` : '\u7f3a\u5c11 API \u914d\u7f6e'))));
  $('#connectionText').textContent = data.connected ? '\u771f\u5b9e\u884c\u60c5' : '\u6b63\u5728\u8fde\u63a5';
}

function renderOrders(orders) {
  state.orders = orders || state.orders; $('#orderCount').textContent = state.orders.filter((order) => ['open', 'pending', 'in-progress', 'NEW'].includes(order.status)).length;
  if (!state.orders.length) { $('#ordersBody').innerHTML = `<tr class="empty"><td colspan="7">\u4ea4\u6613\u6240\u5c1a\u672a\u8fd4\u56de\u8ba2\u5355</td></tr>`; return; }
  $('#ordersBody').innerHTML = state.orders.slice(0, 12).map((order) => { const buy = order.is_ask === false || order.side === 'buy'; const status = order.status || 'pending'; return `<tr><td>${escapeHtml(order.updated_at ? new Date(Number(order.updated_at)).toLocaleTimeString('zh-CN', {hour12:false}) : EMPTY)}</td><td>${order.reduce_only ? '\u5e73\u4ed3' : '\u5f00\u4ed3'}</td><td class="${buy ? 'green' : 'red'}">${buy ? '\u4e70\u5165' : '\u5356\u51fa'}</td><td>${escapeHtml(order.time_in_force || order.type || EMPTY)}</td><td>${escapeHtml(order.price)}</td><td>${escapeHtml(order.remaining_base_amount || order.base_size)}</td><td><span class="badge ${status === 'filled' ? 'done' : ['open','pending','in-progress','NEW'].includes(status) ? 'working' : 'cancel'}">${escapeHtml(status)}</span></td></tr>`; }).join('');
}

function renderMarkets() {
  const needle = $('#marketSearch').value.trim().toUpperCase();
  const filtered = state.markets.filter((market) => !needle || market.symbol.toUpperCase().includes(needle) || String(market.market_id).toUpperCase().includes(needle));
  $('#marketSelect').innerHTML = filtered.length ? filtered.map((market) => `<option value="${escapeHtml(market.internal_instrument_id)}">${escapeHtml(market.symbol)} PERP \u00b7 ${escapeHtml(market.market_id)}</option>`).join('') : '<option value="">\u672a\u627e\u5230\u5bf9\u5e94\u5408\u7ea6</option>';
  if (filtered.some((market) => market.internal_instrument_id === state.instrumentId)) $('#marketSelect').value = state.instrumentId;
}

async function loadMarkets() { const data = await api(`/api/markets?venue=${encodeURIComponent(state.venue)}`); state.markets = data.markets; if (data.selected_venue === state.venue && data.selected_internal_instrument_id) state.instrumentId = data.selected_internal_instrument_id; renderMarkets(); }
async function load() { try { const [health, orders] = await Promise.all([api('/api/health'), api('/api/orders')]); updateStatus(health); renderOrders(orders.orders); if (orders.follow_state && orders.follow_state !== 'active') log(`跟价订单已进入 ${orders.follow_state} 状态：${orders.follow_failure_reason || '请先核对交易所并撤销该订单'}`, 'warn'); await loadMarkets(); } catch (error) { log(error.message || '\u540e\u7aef\u5c1a\u672a\u542f\u52a8', 'warn'); } }

function setMt5Text(selector, value) { const element = $(selector); if (element) element.textContent = value; }
function mt5Value(value, fallback = EMPTY) { return value === null || value === undefined || value === '' ? fallback : String(value); }
function mt5Number(value, maximumFractionDigits = 2) { const numeric = Number(value); return value === null || value === undefined || value === '' || !Number.isFinite(numeric) ? EMPTY : numeric.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits }); }
function mt5ReadonlyVerified(status) { return Boolean(status && status.enabled && status.initialized && status.readonly_verified && status.terminal_trade_allowed === false && status.account_trade_allowed === false); }
function clearMt5Positions(message, count = EMPTY) { const body = $('#mt5PositionsBody'); body.replaceChildren(); const row = document.createElement('tr'); row.className = 'empty'; const cell = document.createElement('td'); cell.colSpan = 7; cell.textContent = message; row.append(cell); body.append(row); setMt5Text('#mt5PositionCount', count); }
function renderMt5Locked(message = '\u8bf7\u5148\u89e3\u9501\u63a7\u5236\u53f0') { state.mt5Status = null; setMt5Text('#mt5ModeBadge', 'LOCKED'); $('#mt5ModeBadge')?.classList.remove('verified', 'unsafe'); setMt5Text('#mt5StatusNote', `${message}\uff1bMT5 \u53ea\u8bfb\u9875\u9762\u4e0d\u63d0\u4f9b\u4e0b\u5355\u3001\u6539\u5355\u6216\u64a4\u5355\u64cd\u4f5c\u3002`); setMt5Text('#mt5Connection', '\u7b49\u5f85\u767b\u5f55'); setMt5Text('#mt5AccountLogin', EMPTY); setMt5Text('#mt5Balance', EMPTY); setMt5Text('#mt5Equity', EMPTY); setMt5Text('#mt5Profit', EMPTY); clearMt5Positions('\u767b\u5f55\u540e\u53ef\u52a0\u8f7d\u53ea\u8bfb\u6301\u4ed3'); }
function renderMt5Status(status) {
  state.mt5Status = status || null;
  const badge = $('#mt5ModeBadge'); const verified = mt5ReadonlyVerified(status);
  badge.classList.toggle('verified', verified); badge.classList.toggle('unsafe', Boolean(status?.enabled) && !verified);
  if (!status?.enabled) { setMt5Text('#mt5ModeBadge', 'DISABLED'); setMt5Text('#mt5Connection', '\u672a\u542f\u7528'); setMt5Text('#mt5StatusNote', 'MT5 \u53ea\u8bfb Sidecar \u672a\u542f\u7528\u3002\u5b83\u4fdd\u6301\u4e0d\u8fde\u63a5\u3001\u4e0d\u8bfb\u53d6\u3001\u4e0d\u4ea4\u6613\u7684\u5b89\u5168\u9ed8\u8ba4\u72b6\u6001\u3002'); clearMt5Positions('MT5 \u53ea\u8bfb Sidecar \u672a\u542f\u7528'); return false; }
  if (!verified) { setMt5Text('#mt5ModeBadge', 'READ ONLY CHECK FAILED'); setMt5Text('#mt5Connection', '\u5df2\u62d2\u7edd'); setMt5Text('#mt5StatusNote', 'MT5 \u672a\u901a\u8fc7\u53ea\u8bfb\u9a8c\u8bc1\u3002\u53ea\u6709 terminal \u4e0e account \u5747\u660e\u786e\u62a5\u544a trade_allowed=false \u65f6\uff0c\u7cfb\u7edf\u624d\u4f1a\u8bfb\u53d6\u6570\u636e\u3002'); clearMt5Positions('MT5 \u53ea\u8bfb\u9a8c\u8bc1\u672a\u901a\u8fc7'); return false; }
  setMt5Text('#mt5ModeBadge', 'READ ONLY VERIFIED'); setMt5Text('#mt5Connection', '\u5df2\u9a8c\u8bc1\u4e3a\u53ea\u8bfb'); setMt5Text('#mt5StatusNote', 'MT5 \u7ec8\u7aef\u4e0e\u8d26\u6237\u5747\u5df2\u9a8c\u8bc1 trade_allowed=false\u3002\u672c\u9762\u677f\u53ea\u8c03\u7528\u72b6\u6001\u3001\u8d26\u6237\u548c\u6301\u4ed3\u8bfb\u53d6\u63a5\u53e3\u3002'); return true;
}
function renderMt5Account(account) {
  const currency = mt5Value(account?.currency, ''); const suffix = currency ? ` ${currency}` : '';
  setMt5Text('#mt5AccountLogin', mt5Value(account?.login)); setMt5Text('#mt5Balance', `${mt5Number(account?.balance)}${suffix}`.trim()); setMt5Text('#mt5Equity', `${mt5Number(account?.equity)}${suffix}`.trim());
  const profit = Number(account?.profit); const profitText = `${mt5Number(account?.profit)}${suffix}`.trim(); const profitElement = $('#mt5Profit'); profitElement.textContent = profitText; profitElement.classList.toggle('green', Number.isFinite(profit) && profit > 0); profitElement.classList.toggle('red', Number.isFinite(profit) && profit < 0);
}
function mt5PositionSide(position) { const value = String(position?.type ?? '').toLowerCase(); if (value === '0' || value.includes('buy') || value.includes('\u4e70')) return { text: '\u4e70\u5165', className: 'green' }; if (value === '1' || value.includes('sell') || value.includes('\u5356')) return { text: '\u5356\u51fa', className: 'red' }; return { text: mt5Value(position?.type), className: '' }; }
function renderMt5Positions(positions) {
  const normalized = Array.isArray(positions) ? positions : []; const body = $('#mt5PositionsBody'); body.replaceChildren(); setMt5Text('#mt5PositionCount', `${normalized.length} \u7b14`);
  if (!normalized.length) { clearMt5Positions('\u5f53\u524d\u6ca1\u6709\u6301\u4ed3', '0 \u7b14'); return; }
  normalized.slice(0, 100).forEach((position) => {
    const row = document.createElement('tr'); const side = mt5PositionSide(position); const profit = Number(position?.profit); const values = [mt5Value(position?.symbol), side.text, mt5Number(position?.volume, 4), mt5Number(position?.price_open, 8), mt5Number(position?.price_current, 8), mt5Number(position?.profit), mt5Value(position?.ticket)];
    values.forEach((value, index) => { const cell = document.createElement('td'); cell.textContent = value; if (index === 1) cell.className = side.className; if (index === 5 && Number.isFinite(profit)) cell.className = profit > 0 ? 'green' : profit < 0 ? 'red' : ''; row.append(cell); }); body.append(row);
  });
}
async function loadMt5() {
  if (!state.authenticated) { renderMt5Locked(); return; }
  const button = $('#refreshMt5Button'); button.disabled = true;
  try {
    const status = await api('/api/mt5/status');
    if (!renderMt5Status(status)) return;
    const [accountData, positionsData] = await Promise.all([api('/api/mt5/account'), api('/api/mt5/positions')]);
    if (!renderMt5Status(positionsData.status || accountData.status || status)) return;
    renderMt5Account(accountData.account || {}); renderMt5Positions(positionsData.positions || []);
  } catch (error) {
    setMt5Text('#mt5ModeBadge', 'UNAVAILABLE'); $('#mt5ModeBadge')?.classList.add('unsafe'); setMt5Text('#mt5Connection', '\u4e0d\u53ef\u7528'); setMt5Text('#mt5StatusNote', `MT5 \u53ea\u8bfb\u6570\u636e\u6682\u4e0d\u53ef\u7528\uff1a${error.message || '\u672a\u77e5\u9519\u8bef'}`); clearMt5Positions('MT5 \u53ea\u8bfb\u6570\u636e\u6682\u4e0d\u53ef\u7528');
  } finally { button.disabled = false; }
}

async function unlock() {
  const token = $('#controlToken').value;
  if (!token) return log('\u8bf7\u8f93\u5165 CONTROL_PLANE_TOKEN', 'warn');
  try {
    $('#unlockButton').disabled = true;
    const data = await api('/api/session', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({token}) });
    state.csrf = data.csrf; state.authenticated = true; $('#controlToken').value = ''; $('#controlToken').disabled = true; $('#unlockButton').textContent = '\u63a7\u5236\u53f0\u5df2\u89e3\u9501'; log('\u63a7\u5236\u53f0\u5df2\u5b89\u5168\u89e3\u9501\uff0c\u4f1a\u8bdd\u5c06\u81ea\u52a8\u8fc7\u671f', 'ok'); await load(); if (window.location.hash === '#mt5') await loadMt5(); connect();
  } catch (error) { log(`\u89e3\u9501\u5931\u8d25\uff1a${error.message}`, 'warn'); $('#unlockButton').disabled = false; }
}

async function selectMarket() { const instrumentId = $('#marketSelect').value; if (!instrumentId) return; try { const data = await api(`/api/market/${encodeURIComponent(state.venue)}/${encodeURIComponent(instrumentId)}`, {method: 'POST'}); updateStatus(data); renderOrders([]); log(`\u5df2\u5207\u6362\u4e3a ${venueLabels[state.venue]} ${data.symbol} \u00b7 ${data.market_id}`, 'ok'); } catch (error) { log(`\u5207\u6362\u5931\u8d25\uff1a${error.message}`, 'warn'); } }
async function selectVenue() { state.venue = $('#venueSelect').value; state.instrumentId = null; state.markets = []; $('#marketSelect').innerHTML = '<option>\u52a0\u8f7d\u5e02\u573a\u2026</option>'; try { await loadMarkets(); const current = state.markets.find((market) => String(market.market_id) === state.marketId); if (!current && state.markets.length) { $('#marketSelect').value = state.markets[0].internal_instrument_id; await selectMarket(); } } catch (error) { log(`\u52a0\u8f7d${venueLabels[state.venue]}\u5e02\u573a\u5931\u8d25\uff1a${error.message}`, 'warn'); } }
async function execute() {
  const quote = $('#quoteCurrency').textContent; const notional = Number($('#quantity').value); if (!Number.isFinite(notional) || notional <= 0) return log(`${quote} \u91d1\u989d\u5fc5\u987b\u5927\u4e8e 0`, 'warn');
  try {
    if (!state.instrumentId) throw new Error('\u8bf7\u5148\u4ece\u540e\u7aef\u5408\u7ea6\u5217\u8868\u91cd\u65b0\u9009\u62e9\u5408\u7ea6');
    const preview = await api('/api/order-intents', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({venue:state.venue,internal_instrument_id:state.instrumentId,side:state.side,intent:state.intent,mode:state.mode,notional_amount:notional})});
    const confirmed = window.confirm(`\u786e\u8ba4\u63d0\u4ea4\u771f\u5b9e\u8ba2\u5355\uff1f\n\n\u4ea4\u6613\u6240\uff1a${venueLabels[preview.venue]}\n\u5408\u7ea6\uff1a${preview.symbol} \u00b7 ${preview.market_id}\n\u64cd\u4f5c\uff1a${preview.position_meaning} (${preview.side === 'buy' ? '\u4e70\u5165' : '\u5356\u51fa'})\n\u6267\u884c\uff1a${preview.order_mode === 'market' ? '\u5403\u4e00\u4ef7 IOC' : '\u8ddf\u4e00\u4ef7 Post Only'}\n\u91d1\u989d\uff1a${Number(preview.notional_amount).toFixed(2)} ${preview.quote_currency}\n\u9884\u8ba1\u6570\u91cf\uff1a${preview.estimated_quantity}\n\u53c2\u8003\u4ef7\uff1a${preview.reference_price}\n\u4e70\u4e00 / \u5356\u4e00\uff1a${preview.best_bid} / ${preview.best_ask}\nReduce Only\uff1a${preview.reduce_only ? '\u662f' : '\u5426'}\nPost Only\uff1a${preview.post_only ? '\u662f' : '\u5426'}\n\n\u8be5\u786e\u8ba4\u4ec5\u5bf9\u6b64\u6b21\u8ba2\u5355\u6709\u6548\uff0c\u76d8\u53e3\u53d8\u5316\u9700\u91cd\u65b0\u786e\u8ba4\u3002`);
    if (!confirmed) return log('\u5df2\u53d6\u6d88\u63d0\u4ea4');
    const data = await api('/api/execute', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order_intent_token:preview.order_intent_token,confirm_live:$('#liveConfirm').checked,request_id:requestId()})});
    log(`\u771f\u5b9e\u8ba2\u5355\u5df2\u63d0\u4ea4\uff1a${preview.position_meaning} ${data.notional_amount} ${data.quote_currency} (${data.quantity} ${data.symbol}) @ ${data.price}`, 'ok');
  } catch (error) { log(`\u4ea4\u6613\u6240\u62d2\u7edd\uff1a${error.message}`, 'warn'); }
}
async function cancelFollow() { try { const data = await api('/api/cancel-follow', {method:'POST'}); log(data.canceled ? '\u8ddf\u4ef7\u5355\u64a4\u5355\u5df2\u63d0\u4ea4' : '\u6ca1\u6709\u6d3b\u8dc3\u8ddf\u4ef7\u5355', 'ok'); } catch (error) { log(`\u64a4\u5355\u5931\u8d25\uff1a${error.message}`, 'warn'); } }
let socket;
function connect() { if (!state.authenticated || (socket && socket.readyState <= 1)) return; const protocol = location.protocol === 'https:' ? 'wss' : 'ws'; socket = new WebSocket(`${protocol}://${location.host}/ws`); socket.onopen = () => log('\u5df2\u5b89\u5168\u8fde\u63a5\u672c\u5730\u6267\u884c\u540e\u7aef', 'ok'); socket.onmessage = (event) => { const message = JSON.parse(event.data); if (message.type === 'ticker' || message.type === 'market') updateStatus(message.data); if (message.type === 'orders') renderOrders(message.data.orders); if (message.type === 'system') log(message.data.message, 'warn'); if (message.type === 'execution') log(`\u8ba2\u5355\u5df2\u53d1\u9001\uff1a${message.data.order_id || message.data.tx_hash || message.data.client_order_index || 'accepted'}`, 'ok'); }; socket.onclose = () => { if (state.authenticated) { $('#connectionText').textContent = '\u8fde\u63a5\u5df2\u65ad\u5f00'; setTimeout(connect, 2000); } }; }

document.querySelectorAll('.mode').forEach((button) => button.addEventListener('click', () => { state.mode = button.dataset.mode; document.querySelectorAll('.mode').forEach((item) => item.classList.toggle('active', item === button)); $('#marketRule').classList.toggle('hidden', state.mode !== 'market'); $('#makerRule').classList.toggle('hidden', state.mode !== 'maker'); refreshPreview(); }));
document.querySelectorAll('#intentSwitch button').forEach((button) => button.addEventListener('click', () => { state.intent = button.dataset.intent; document.querySelectorAll('#intentSwitch button').forEach((item) => item.classList.toggle('selected', item === button)); refreshPreview(); }));
document.querySelectorAll('.side-actions button').forEach((button) => button.addEventListener('click', () => { state.side = button.dataset.side; refreshPreview(); }));
document.querySelectorAll('.quick-size button').forEach((button) => button.addEventListener('click', () => { $('#quantity').value = Number(button.dataset.pct).toFixed(2); }));
document.querySelectorAll('.nav-item').forEach((item) => item.addEventListener('click', () => { document.querySelectorAll('.nav-item').forEach((nav) => nav.classList.toggle('active', nav === item)); if (item.id === 'mt5Nav') loadMt5(); }));
$('#liveConfirm').addEventListener('change', refreshPreview); $('#executeButton').addEventListener('click', execute); $('#cancelAll').addEventListener('click', cancelFollow); $('#clearLog').addEventListener('click', () => { $('#log').innerHTML = ''; }); $('#liveStatus').addEventListener('click', load); $('#marketSelect').addEventListener('change', selectMarket); $('#marketSearch').addEventListener('input', renderMarkets); $('#venueSelect').addEventListener('change', selectVenue); $('#unlockButton').addEventListener('click', unlock); $('#refreshMt5Button').addEventListener('click', loadMt5); $('#controlToken').addEventListener('keydown', (event) => { if (event.key === 'Enter') unlock(); });
(async () => { try { const status = await fetch('/api/session', {credentials:'same-origin'}).then((response) => response.json()); if (status.authenticated && status.csrf) { state.authenticated = true; state.csrf = status.csrf; $('#controlToken').value = ''; $('#controlToken').disabled = true; $('#unlockButton').disabled = false; $('#unlockButton').textContent = '\u63a7\u5236\u53f0\u5df2\u89e3\u9501'; await load(); connect(); } else lockUi(status.configured ? '\u8bf7\u89e3\u9501\u63a7\u5236\u53f0' : '\u540e\u7aef\u672a\u914d\u7f6e CONTROL_PLANE_TOKEN'); } catch { lockUi('\u540e\u7aef\u8fde\u63a5\u5931\u8d25'); } if (window.location.hash === '#mt5') { $('#mt5Nav')?.classList.add('active'); loadMt5(); } refreshPreview(); })();
