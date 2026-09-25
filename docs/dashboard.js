'use strict';
// Only the published daily snapshot is used. Never invent missing observations.
const INDEX_TICKERS = ['SPY', 'QQQ', 'IWM', 'DIA'];
const SECTOR_TICKERS = ['SMH', 'URA', 'XLU', 'XLK', 'XLI', 'XLF', 'XBI', 'XLE', 'XLV', 'XLP'];
const EXCHANGE_MAP = Object.fromEntries([...INDEX_TICKERS, ...SECTOR_TICKERS].map(t => [t, ['QQQ', 'SMH'].includes(t) ? 'nasdaq' : 'nyse']));
let currentData = null;
let sortState = {col: 'alpha', dir: 'desc'};
let alphaChartInstance = null;
let loading = false;
let themePeriod = '5';
let toastTimer;
const finite = value => typeof value === 'number' && Number.isFinite(value);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (value, suffix = '', signed = false) => finite(value) ? `${signed && value >= 0 ? '+' : ''}${value.toFixed(2)}${suffix}` : 'N/A';
function getChartExchangeUrl(ticker) {
  if (!Object.hasOwn(EXCHANGE_MAP, ticker)) return '#';
  return `https://chartexchange.com/symbol/${EXCHANGE_MAP[ticker]}-${ticker.toLowerCase()}/exchange-volume/`;
}
function compareRows(a, b, col, dir) {
  const x = a[col], y = b[col];
  if (col === 'ticker') return String(x).localeCompare(String(y)) * (dir === 'asc' ? 1 : -1);
  if (!finite(x) && !finite(y)) return 0;
  if (!finite(x)) return 1;
  if (!finite(y)) return -1;
  return (x - y) * (dir === 'asc' ? 1 : -1);
}
function validDate(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0,10) === value;
}
function normalizePayload(input) {
  if (!input || !validDate(input.market_date) || typeof input.last_updated !== 'string' ||
      !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$/.test(input.last_updated)) {
    throw new Error('快照欠缺有效來源日期；請先完成新版 GitHub workflow。');
  }
  const seen = new Set();
  function normalizeGroup(rows, expected) {
    if (!Array.isArray(rows) || rows.length !== expected.length) throw new Error('ETF 資料不齊，保留上一份已載入快照。');
    return rows.map(row => {
      if (!row || !expected.includes(row.ticker) || seen.has(row.ticker)) throw new Error('ETF 清單不正確或重複。');
      seen.add(row.ticker);
      if (!['price','return','alpha','rvol'].every(k => finite(row[k])) || row.price <= 0 || row.rvol < 0 ||
          row.rvolLookback !== 20 || row.market_date !== input.market_date) throw new Error(`${row.ticker}: 價量資料或日期不完整。`);
      const offAvailable = finite(row.darkPool) && row.darkPool >= 0 && row.darkPool <= 100 &&
        row.darkPoolStatus === 'available' && row.darkPoolMetric === 'off_exchange_day_pct' &&
        row.darkPoolDate === input.market_date;
      return {...row, name: String(row.name ?? row.ticker), darkPool: offAvailable ? row.darkPool : null,
        darkPoolStatus: offAvailable ? 'available' : (row.darkPoolStatus === 'date_mismatch' ||
          (row.darkPoolDate && row.darkPoolDate !== input.market_date) ? 'date_mismatch' : 'unavailable')};
    });
  }
  const indices = normalizeGroup(input.indices, INDEX_TICKERS);
  const sectors = normalizeGroup(input.sectors, SECTOR_TICKERS);
  const missing = [...indices, ...sectors].filter(r => r.darkPool === null).map(r => r.ticker);
  if (input.groups !== undefined && (!Array.isArray(input.groups) ||
      input.asof !== input.market_date || !input.issuer_flows || typeof input.issuer_flows !== 'object')) {
    throw new Error('主題資料日期或結構不正確。');
  }
  return {...input, indices, sectors, missing_off_exchange: missing,
    data_quality: missing.length ? 'partial' : 'available_not_independently_verified'};
}
const element = id => document.getElementById(id);
function text(id, value) { const e = element(id); if (e) e.textContent = value; }
function missingLabel(row) { return row.darkPoolStatus === 'date_mismatch' ? '來源日期不符' : '来源未取得'; }
function renderUI() {
  if (!currentData) return;
  const sectors = currentData.sectors;
  const count = currentData.missing_off_exchange.length;
  text('update-timestamp', `生成時間: ${currentData.last_updated}`);
  text('market-date', `市場資料日期: ${currentData.market_date}`);
  text('data-source-badge', count ? `部分資料可用 · 場外 ${14-count}/14` : '來源資料齊備 · 未獨立核實');
  const age = Date.now() - Date.parse(currentData.last_updated.replace(' UTC','Z').replace(' ','T'));
  text('data-quality-note', `${count ? `場外資料缺失 ${count}/14：${currentData.missing_off_exchange.join(', ')}。` : '場外資料與價格日期一致。'} N/A 不參與排名；日線並非即時報價。${age > 96*3600000 ? ' 注意：快照生成已超過 96 小時，請檢查 Actions 更新狀態。' : ''}`);
  const top = [...sectors].sort((a,b) => b.alpha-a.alpha)[0];
  const rvol = [...sectors].sort((a,b) => b.rvol-a.rvol)[0];
  const available = sectors.filter(s => finite(s.darkPool)).sort((a,b) => b.darkPool-a.darkPool);
  text('kpi-top-sector', top.ticker);
  text('kpi-top-alpha', `${fmt(top.return, '%', true)} · vs SPY ${fmt(top.alpha, ' pp', true)}`);
  text('kpi-rvol-ticker', rvol.ticker);
  text('kpi-rvol-val', `${fmt(rvol.rvol, 'x')} · 當日 / 前 20 日均量`);
  text('kpi-darkpool-ticker', available.length ? available[0].ticker : 'N/A');
  text('kpi-darkpool-val', available.length ? `${fmt(available[0].darkPool, '%')} · ${currentData.market_date}` : '無同日場外來源，不排名');
  const spy = currentData.indices.find(s => s.ticker === 'SPY');
  text('kpi-regime-text', spy.return > 0 ? 'SPY 上升' : spy.return < 0 ? 'SPY 下跌' : 'SPY 持平');
  element('broad-indices-grid').innerHTML = currentData.indices.map(row => `
    <div class="glass-card border border-gray-800 rounded-xl p-4 flex justify-between gap-2">
      <div><div class="text-xs text-slate-400">${escapeHtml(row.name)}</div><div class="text-xl font-bold">${row.ticker} $${fmt(row.price)}</div><div class="text-xs text-slate-400 mt-1">RVOL: ${fmt(row.rvol, 'x')}</div></div>
      <div class="text-right ${row.return >= 0 ? 'text-emerald-400' : 'text-rose-400'}">${fmt(row.return, '%', true)}<div class="text-xs text-slate-400">${escapeHtml(row.change)}</div></div>
    </div>`).join('');
  element('darkpool-breakdown-list').innerHTML = available.length ? available.slice(0,4).map(row => `
    <div><div class="flex justify-between text-xs mb-1"><a class="text-sky-400 underline" href="${getChartExchangeUrl(row.ticker)}" target="_blank" rel="noopener noreferrer">${row.ticker} ↗</a><span>${fmt(row.darkPool, '%')}</span></div>
    <div class="bg-slate-800 rounded-full h-2"><div class="bg-purple-500 h-2 rounded-full" style="width:${row.darkPool}%"></div></div></div>`).join('') : '<p class="text-sm text-slate-400">N/A — 無同日場外來源資料</p>';
  renderTable();
  renderThemes();
  renderChart();
  if (globalThis.lucide) globalThis.lucide.createIcons();
}
function setThemePeriod(period) {
  if (!['5','20'].includes(period)) return;
  themePeriod = period;
  renderThemes();
}
function flowText(data, ticker) {
  const item = data.issuer_flows?.[ticker]?.[themePeriod];
  if (item?.status !== 'available' || !finite(item.usd) ||
      item.end !== data.market_date || item.required !== Number(themePeriod) + 1 ||
      item.observations !== item.required) {
    const have = Number.isInteger(item?.observations) ? `${item.observations}/${Number(themePeriod)+1} 日` : '來源未取得';
    return `N/A · 有效記錄 ${have}`;
  }
  const millions = item.usd / 1e6;
  return `${millions >= 0 ? '+' : ''}${millions.toFixed(2)} 百萬美元 · ${item.start} → ${item.end}`;
}
function themeRows(instruments, period, type) {
  const rows = instruments.filter(r => (type === 'etf') === (r.kind !== '股票'));
  return rows.map(row => {
    const ready = row.date === currentData.market_date && finite(row.returns?.[period]) && finite(row.vs_spy?.[period]);
    const rel = ready ? row.vs_spy[period] : null;
    const color = ready ? (rel >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-slate-500';
    return `<div class="grid grid-cols-3 gap-2 py-2 border-b border-gray-800 text-xs">
      <div><b class="text-white">${escapeHtml(row.ticker)}</b><span class="text-slate-500 ml-2">${escapeHtml(row.kind)}</span></div>
      <div class="text-right">${fmt(ready ? row.returns[period] : null, '%', true)}</div>
      <div class="text-right ${color}">${fmt(rel, ' pp', true)}</div>
    </div>`;
  }).join('');
}
function renderThemes() {
  const container = element('theme-panels');
  if (!container) return;
  for (const period of ['5','20']) {
    const button = element(`period-${period}`);
    if (button) {
      button.setAttribute?.('aria-pressed', String(themePeriod === period));
      button.className = `${themePeriod === period ? 'bg-sky-700 text-white' : 'bg-slate-800 text-slate-300'} px-3 py-2 rounded-lg text-xs`;
    }
  }
  if (!currentData || !Array.isArray(currentData.groups) || currentData.groups.length !== 2) {
    container.textContent = '主題資料尚未生成，請等候下次每日更新。';
    return;
  }
  container.innerHTML = currentData.groups.map(group => {
    if (!['AI','BTC'].includes(group.id) || !Array.isArray(group.instruments) ||
        !['ARTY','IBIT'].includes(group.flow_proxy)) return '';
    const instruments = group.instruments;
    const available = instruments.filter(r => r.date === currentData.market_date && finite(r.vs_spy?.[themePeriod]));
    const stronger = available.filter(r => r.vs_spy[themePeriod] > 0).length;
    const url = group.flow_proxy === 'ARTY'
      ? 'https://www.ishares.com/us/products/297905/ishares-future-ai-tech-etf'
      : 'https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf';
    return `<article class="glass-card border border-gray-800 rounded-xl p-5">
      <div class="flex justify-between items-baseline"><h3 class="text-base font-bold">${escapeHtml(group.label)}</h3>
        <span class="text-xs text-slate-400">相對 SPY 轉強 ${stronger}/${available.length} 可用</span></div>
      <div class="bg-slate-900 rounded-lg p-3 mt-4">
        <div class="text-xs text-slate-400"><a class="underline text-sky-400" href="${url}" target="_blank" rel="noopener noreferrer">${group.flow_proxy} ↗</a> · ${themePeriod} 日 ETF 淨發行估算</div>
        <div class="text-lg font-semibold mt-1">${escapeHtml(flowText(currentData, group.flow_proxy))}</div>
      </div>
      <div class="grid grid-cols-3 gap-2 mt-4 text-xs text-slate-500"><span>ETF / ETP</span><span class="text-right">${themePeriod} 日回報</span><span class="text-right">vs SPY</span></div>
      ${themeRows(instruments, themePeriod, 'etf')}
      <div class="grid grid-cols-3 gap-2 mt-4 text-xs text-slate-500"><span>相關股票</span><span class="text-right">${themePeriod} 日回報</span><span class="text-right">vs SPY</span></div>
      ${themeRows(instruments, themePeriod, 'stock')}
    </article>`;
  }).join('');
}
function renderTable() {
  if (!currentData) return;
  const search = (element('search-input').value || '').toUpperCase();
  const rows = currentData.sectors.filter(r => r.ticker.includes(search) || r.name.toUpperCase().includes(search));
  rows.sort((a,b) => compareRows(a,b,sortState.col,sortState.dir));
  element('sector-table-body').innerHTML = rows.map(row => {
    const signal = row.signal === 'ACCUMULATION' ? '🟢 量價偏強' : row.signal === 'DISTRIBUTION' ? '🔴 相對偏弱' : '⚪ 中性';
    const off = finite(row.darkPool) ? `${fmt(row.darkPool, '%')}<div class="text-xs text-slate-500">${row.darkPoolDate}</div>` : `N/A<div class="text-xs text-slate-500">${missingLabel(row)}</div>`;
    return `<tr class="hover:bg-slate-800/40"><td class="py-3 px-4"><a class="text-sky-400" href="${getChartExchangeUrl(row.ticker)}" target="_blank" rel="noopener noreferrer">${row.ticker} ↗</a><div class="text-xs font-sans">${escapeHtml(row.name)}</div></td><td class="py-3 px-4">$${fmt(row.price)}</td><td class="py-3 px-4 ${row.return >= 0 ? 'text-emerald-400' : 'text-rose-400'}">${fmt(row.return, '%', true)}</td><td class="py-3 px-4">${fmt(row.alpha, '', true)}</td><td class="py-3 px-4">${fmt(row.rvol, 'x')}</td><td class="py-3 px-4 text-purple-300">${off}</td><td class="py-3 px-4 text-xs">${signal}</td></tr>`;
  }).join('') || '<tr><td colspan="7" class="p-4">沒有符合的 ETF。</td></tr>';
}
function sortTable(col) {
  if (!['ticker','return','alpha','rvol','darkPool'].includes(col)) return;
  sortState = {col, dir: sortState.col === col && sortState.dir === 'desc' ? 'asc' : 'desc'};
  renderTable();
}
function renderChart() {
  if (typeof globalThis.Chart !== 'function') { text('chart-note', '圖表元件未載入；完整數據仍可在下方表格查看。'); return; }
  try {
    const rows = [...currentData.sectors].sort((a,b) => b.alpha-a.alpha);
    if (alphaChartInstance) alphaChartInstance.destroy();
    alphaChartInstance = new globalThis.Chart(element('alphaChart').getContext('2d'), {
      type: 'bar', data: {labels: rows.map(r => r.ticker), datasets: [{label: 'vs SPY (percentage points)', data: rows.map(r => r.alpha), backgroundColor: rows.map(r => r.alpha >= 0 ? '#38bdf8' : '#f43f5e'), borderRadius: 6}]},
      options: {responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, scales:{y:{ticks:{color:'#94a3b8'}}, x:{ticks:{color:'#94a3b8'}}}}
    });
    text('chart-note', '');
  } catch (error) { text('chart-note', '圖表顯示失敗；請使用下方數據表。'); }
}
async function loadDashboardData() {
  if (loading) return;
  loading = true;
  const button = element('fetch-live-btn');
  if (button) button.disabled = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(`./data.json?t=${Date.now()}`, {cache:'no-store', signal:controller.signal});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const next = normalizePayload(await response.json());
    currentData = next;
    renderUI();
    showToast('已載入已發布快照；請留意市場日期及來源完整程度。');
  } catch (error) {
    text('data-source-badge', currentData ? '載入失敗 · 保留上一份快照' : '資料未能載入');
    text('data-quality-note', `未有生成替代數據。${error.message}${currentData ? ` 現時仍顯示 ${currentData.market_date} 的上一份快照。` : ''}`);
    showToast('資料載入失敗；沒有估算或隨機填補。');
  } finally { clearTimeout(timer); loading = false; if (button) button.disabled = false; }
}
function showToast(message) {
  text('toast-text', message);
  const e = element('toast-msg');
  if (!e) return;
  clearTimeout(toastTimer);
  e.classList.remove('opacity-0');
  toastTimer = setTimeout(() => e.classList.add('opacity-0'), 3000);
}
async function copySummaryMarkdown() {
  if (!currentData) return;
  const spy = currentData.indices.find(r => r.ticker === 'SPY');
  const qqq = currentData.indices.find(r => r.ticker === 'QQQ');
  const top = [...currentData.sectors].sort((a,b) => b.alpha-a.alpha)[0];
  const summary = `美股每日快照 (${currentData.market_date})\n生成時間: ${currentData.last_updated}\nSPY: ${fmt(spy.return,'% ',true)}| QQQ: ${fmt(qqq.return,'%',true)}\n相對 SPY 最強板塊: ${top.ticker} (${fmt(top.alpha,' pp',true)})\n場外資料缺失: ${currentData.missing_off_exchange.length}/14；未估算填補。\n訊號是量價規則，不是已核實機構資金流。`;
  try { await navigator.clipboard.writeText(summary); showToast('已複製簡報。'); }
  catch (error) { showToast('剪貼簿權限不足，未能複製。'); }
}
if (typeof window !== 'undefined') window.addEventListener('DOMContentLoaded', loadDashboardData);
if (typeof module !== 'undefined') module.exports = {finite, fmt, escapeHtml, getChartExchangeUrl, compareRows, validDate, normalizePayload};
