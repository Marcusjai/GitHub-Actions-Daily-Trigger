/* Independent research UI using the existing dated snapshot. No trading advice or invented data. */
'use strict';
(function (root) {
  const SPEC = {
    AI: {label: 'Artificial intelligence', stocks: ['NVDA','AMD','AVGO','PLTR','MSFT'], etfs: ['ARTY','AIQ','SMH'], nodes: [['Compute & semiconductors',['NVDA','AMD','AVGO']],['Software & cloud',['PLTR','MSFT']]]},
    BTC: {label: 'Bitcoin ecosystem', stocks: ['COIN','MSTR','MARA','RIOT'], etfs: ['IBIT','FBTC','BKCH'], nodes: [['Exchange exposure',['COIN']],['Treasury exposure',['MSTR']],['Mining exposure',['MARA','RIOT']]]}
  };
  const PERIODS = ['1','5','20'];
  const finite = x => typeof x === 'number' && Number.isFinite(x);
  const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (x, unit = '', sign = false) => finite(x) ? `${sign && x > 0 ? '+' : ''}${x.toFixed(2)}${unit}` : 'N/A';
  const dateOK = x => typeof x === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(x) && Number.isFinite(Date.parse(x)) && new Date(x).toISOString().slice(0,10) === x;
  const mean = xs => xs.length ? xs.reduce((a,b) => a+b,0)/xs.length : null;
  function normalize(raw) {
    if (!raw || !dateOK(raw.market_date) || raw.asof !== raw.market_date || !Array.isArray(raw.groups)) throw new Error('Theme snapshot is missing or its dates do not agree. Open Market overview for the core ETF data.');
    if (typeof raw.last_updated !== 'string' || !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$/.test(raw.last_updated) || !dateOK(raw.last_updated.slice(0,10)) || Number(raw.last_updated.slice(11,13)) > 23 || Number(raw.last_updated.slice(14,16)) > 59 || Number(raw.last_updated.slice(17,19)) > 59 || !Number.isFinite(Date.parse(raw.last_updated.replace(' UTC','Z').replace(' ','T')))) throw new Error('Snapshot generation time is invalid.');
    const duplicateGroups = new Set();
    for (const g of raw.groups) {
      if (!g || !Object.hasOwn(SPEC,g.id) || duplicateGroups.has(g.id) || !Array.isArray(g.instruments)) throw new Error('Invalid or duplicate theme group.');
      duplicateGroups.add(g.id);
    }
    const groups = Object.entries(SPEC).map(([id, spec]) => {
      const source = raw.groups.find(g => g.id === id);
      const map = new Map();
      for (const r of source?.instruments || []) {
        if (!r || typeof r.ticker !== 'string' || map.has(r.ticker)) throw new Error('Invalid or duplicate instrument.');
        map.set(r.ticker,r);
      }
      const rows = [...spec.stocks,...spec.etfs].map(ticker => {
        const r = map.get(ticker) || {};
        const ready = r.date === raw.market_date && ['available','partial'].includes(r.status) && finite(r.price) && r.price > 0;
        const returns = {}, relative = {};
        for (const p of PERIODS) {
          returns[p] = ready && finite(r.returns?.[p]) && r.returns[p] > -100 ? r.returns[p] : null;
          relative[p] = returns[p] !== null && finite(r.vs_spy?.[p]) ? r.vs_spy[p] : null;
        }
        return {ticker, name: String(r.name || ticker), theme: id, type: spec.stocks.includes(ticker) ? 'stock' : 'ETF / ETP',
          date: typeof r.date === 'string' ? r.date : null, price: ready ? r.price : null,
          rvol: ready && finite(r.rvol) && r.rvol >= 0 ? r.rvol : null, returns, relative,
          status: ready ? (r.status === 'partial' || PERIODS.some(p => returns[p] === null || relative[p] === null) || !finite(r.rvol) || r.rvol < 0 ? 'partial' : 'available') : 'unavailable', source: ready ? String(r.source || 'Published daily snapshot') : 'Unavailable',
          recovery: ready && Boolean(r.priceRecovery),
          gaps: Array.isArray(r.missing_close_dates) ? r.missing_close_dates.filter(dateOK) : []};
      });
      return {id, label: spec.label, nodes: spec.nodes, rows, stocks: rows.filter(r => r.type === 'stock')};
    });
    return {date: raw.market_date, updated: raw.last_updated, groups, rows: groups.flatMap(g => g.rows)};
  }
  function aggregate(rows, period) {
    if (!PERIODS.includes(String(period))) throw new Error('Unsupported period.');
    const p = String(period), valid = rows.filter(r => finite(r.returns[p]) && finite(r.relative[p]));
    const ahead = valid.filter(r => r.relative[p] > 0).length;
    return {total: rows.length, available: valid.length, full: valid.length === rows.length && rows.length > 0,
      return: mean(valid.map(r => r.returns[p])), relative: mean(valid.map(r => r.relative[p])),
      breadth: valid.length ? ahead / valid.length * 100 : null, ahead,
      leader: [...valid].sort((a,b) => b.relative[p]-a.relative[p] || a.ticker.localeCompare(b.ticker))[0]?.ticker || null};
  }
  function position(rows) {
    // Matched constituents on BOTH axes; partial baskets are not assigned a quadrant.
    const valid = rows.filter(r => finite(r.relative['5']) && finite(r.relative['20']));
    const x = mean(valid.map(r => r.relative['20'])), y = mean(valid.map(r => r.relative['5']));
    let label = 'Incomplete coverage';
    if (valid.length === rows.length && rows.length) label = x === 0 || y === 0 ? 'On benchmark boundary' : x > 0 && y > 0 ? 'Ahead in both windows' : x < 0 && y > 0 ? '5D ahead / 20D behind' : x > 0 && y < 0 ? '5D behind / 20D ahead' : 'Behind in both windows';
    return {x,y,label,available: valid.length,total:rows.length,full:valid.length === rows.length && rows.length > 0};
  }
  function warnings(row) {
    const out = [];
    if (row.status === 'unavailable') return ['No usable, same-date observation. Missing values are not treated as zero.'];
    if (row.status === 'partial') out.push('Partial source history. Some measurements may be unavailable.');
    if (row.recovery) out.push('Latest observation used the existing closing-quote recovery path; check the source metadata.');
    if (finite(row.relative['5']) && row.relative['5'] < 0) out.push('Behind SPY over 5 trading days.');
    if (finite(row.relative['20']) && row.relative['20'] < 0) out.push('Behind SPY over 20 trading days.');
    if (finite(row.returns['1']) && row.returns['1'] < 0 && finite(row.rvol) && row.rvol >= 1.5) out.push('Down day with RVOL at least 1.50x. This is price/volume evidence, not proof of net selling.');
    if (!finite(row.rvol)) out.push('RVOL unavailable: do not assume normal trading volume.');
    return out.length ? out : ['No listed condition triggered. This is not a buy signal or a statement that risk is low.'];
  }
  function sortedRows(rows, period, key = 'relative', direction = 'desc') {
    const p = String(period), dir = direction === 'asc' ? 1 : -1;
    const value = r => key === 'ticker' ? r.ticker : key === 'return' ? r.returns[p] : key === 'rvol' ? r.rvol : r.relative[p];
    return [...rows].sort((a,b) => {
      const x = value(a), y = value(b);
      if (key === 'ticker') return x.localeCompare(y)*dir;
      if (!finite(x)) return finite(y) ? 1 : a.ticker.localeCompare(b.ticker);
      if (!finite(y)) return -1;
      return (x-y)*dir || a.ticker.localeCompare(b.ticker);
    });
  }
  function review(model, period) {
    const p = String(period);
    const lines = [`RESEARCH BRIEF | ${model.date}`,`Generated: ${model.updated}`,`Window: ${p} trading day(s) | benchmark: SPY`, '', 'OBSERVATIONS'];
    for (const group of model.groups) {
      const a = aggregate(group.stocks,p), q = position(group.stocks);
      lines.push(`${group.id}: equal-weight return ${fmt(a.return,'%',true)}; vs SPY ${fmt(a.relative,' pp',true)}. Coverage ${a.available}/${a.total} tracked stocks; ${a.ahead}/${a.available} available stocks ahead of SPY.`,
        `Top relative-return contributor: ${a.leader || 'N/A'}. 5D/20D comparison: ${q.label}.`);
    }
    lines.push('', 'CONDITIONS TO CHECK');
    const flagged = model.rows.filter(r => r.type === 'stock' && (r.status !== 'available' || (finite(r.returns['1']) && r.returns['1'] < 0 && finite(r.rvol) && r.rvol >= 1.5) || (finite(r.relative['5']) && r.relative['5'] < 0)));
    if (!flagged.length) lines.push('No listed stock condition triggered; this does not establish low risk.');
    for (const r of flagged) lines.push(`${r.ticker}: ${warnings(r).join(' ')}`);
    lines.push('', 'SCOPE & LIMITS', 'Rules-based summary of the published snapshot, not an AI forecast or a change log versus yesterday.', 'The theme averages use stocks only; ETF/ETP proxies are shown separately. Partial baskets are not directly comparable.', 'Relative return = instrument return minus SPY return, in percentage points. Breadth = available tracked stocks with positive relative return / available tracked stocks.', 'The 5D and 20D windows overlap. Quadrants do not establish a historical transition, trend duration or a buy/sell signal.', 'Price/volume strength is not verified investor net buying. ARTY/IBIT issuance estimates remain separate in Market overview.', 'No MA60, daily candle history, live quotes, holdings or trading execution are added by this module.');
    return lines.join('\n');
  }
  const tone = x => !finite(x) || x === 0 ? '' : x > 0 ? 'up' : 'down';
  const metric = (label,value,sub = '') => `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong>${sub ? `<small>${esc(sub)}</small>` : ''}</div>`;
  function cards(model,p) {
    return model.groups.map(g => {
      const a = aggregate(g.stocks,p), q = position(g.stocks);
      return `<article class="card theme-card"><div class="row"><div><p class="eyebrow">${g.id} / TRACKED STOCK BASKET</p><h2>${esc(g.label)}</h2></div><span class="badge ${a.full ? '' : 'caution'}">${a.available}/${a.total} available</span></div><div class="hero-number ${tone(a.relative)}">${fmt(a.relative,' pp',true)}</div><p class="muted">vs SPY · ${p} trading day(s) · equal weight</p><div class="metrics">${metric('Basket return',fmt(a.return,'%',true))}${metric('Ahead of SPY',finite(a.breadth) ? `${a.breadth.toFixed(0)}%` : 'N/A',`${a.ahead}/${a.available} available stocks`)}${metric('Top contributor',a.leader || 'N/A')}</div><div class="breadth" aria-label="Share of available stocks ahead of SPY"><span style="width:${finite(a.breadth) ? a.breadth : 0}%"></span></div><p class="muted">${esc(q.label)}${!a.full ? ' · Partial basket: missing stocks can change the result.' : ''}</p><button data-theme="${g.id}" class="link-button">Explore ${g.id} stocks →</button></article>`;
    }).join('');
  }
  function matrix(model) {
    const points = model.groups.map(g => ({...position(g.stocks),id:g.id}));
    const full = points.filter(q => q.full);
    const scale = Math.max(1,...full.flatMap(q => [Math.abs(q.x),Math.abs(q.y)]))*1.3;
    // Both axes use the same pp range; no synthetic price series are drawn.
    const svg = `<svg viewBox="0 0 640 310" role="img" aria-label="Theme comparison: 20 day relative return on x axis, 5 day relative return on y axis"><rect x="72" y="22" width="492" height="226" rx="8" class="plot-bg"/><path d="M318 22V248 M72 135H564" class="axis"/><text x="80" y="40">5D ahead / 20D behind</text><text x="378" y="40">Ahead in both</text><text x="80" y="237">Behind in both</text><text x="374" y="237">5D behind / 20D ahead</text><text x="72" y="269">−${scale.toFixed(1)} pp</text><text x="310" y="269">0</text><text x="518" y="269">+${scale.toFixed(1)} pp</text><text x="175" y="300">20D relative return vs SPY (pp) →</text><text x="8" y="20">5D (pp)</text><text x="8" y="41">+${scale.toFixed(1)}</text><text x="43" y="139">0</text><text x="8" y="247">−${scale.toFixed(1)}</text>${full.map((q,i) => { const x=318+q.x/scale*246,y=135-q.y/scale*113; return `<g><circle cx="${x}" cy="${y}" r="8" class="point-${q.id}"><title>${q.id}: 20D ${fmt(q.x,' pp',true)}, 5D ${fmt(q.y,' pp',true)}</title></circle><text x="${x+12}" y="${y+(i ? 18 : -10)}" class="point-label">${q.id}</text></g>`; }).join('')}</svg>`;
    return svg + `<div class="matrix-values">${points.map(q => `<span><b>${q.id}</b> · 5D ${fmt(q.y,' pp',true)} / 20D ${fmt(q.x,' pp',true)} · ${q.available}/${q.total}${q.full ? '' : ' (not plotted)'}</span>`).join('')}</div>`;
  }
  function nodes(model,p) {
    return model.groups.map(g => `<article class="card"><p class="eyebrow">${g.id} / CURATED EXPOSURE MAP</p><h2>${esc(g.label)}</h2><div class="nodes">${g.nodes.map(([label,tickers]) => {
      const a = aggregate(g.stocks.filter(r => tickers.includes(r.ticker)),p);
      return `<section class="node"><div class="row"><h3>${esc(label)}</h3><strong class="${tone(a.relative)}">${fmt(a.relative,' pp',true)}</strong></div><p class="muted">${a.available}/${a.total} available · ${p}D vs SPY</p><div class="chips">${tickers.map(t => `<button data-stock="${t}">${t}</button>`).join('')}</div></section>`;
    }).join('')}</div><p class="muted">Proxies, not included in stock averages: ${g.rows.filter(r => r.type !== 'stock').map(r => `<button class="link-button" data-stock="${r.ticker}">${r.ticker}</button>`).join(' · ')}</p></article>`).join('');
  }
  function table(rows,p) {
    return rows.length ? rows.map(r => `<tr><td><button class="ticker-button" data-stock="${r.ticker}" aria-label="Inspect ${r.ticker}">${r.ticker}</button><small>${esc(r.name)}</small></td><td>${r.theme}<small>${esc(r.type)}</small></td><td class="${tone(r.returns[p])}">${fmt(r.returns[p],'%',true)}</td><td class="${tone(r.relative[p])}">${fmt(r.relative[p],' pp',true)}</td><td>${fmt(r.rvol,'x')}</td><td><span class="badge ${r.status === 'available' ? '' : 'caution'}">${esc(r.status)}</span></td></tr>`).join('') : '<tr><td colspan="6">No matching tracked instruments.</td></tr>';
  }
  function detail(r,model) {
    return `<div class="row"><div><p class="eyebrow">${r.theme} / ${esc(r.type)}</p><h2>${r.ticker} <span class="muted">${esc(r.name)}</span></h2></div><a class="button secondary" href="https://finance.yahoo.com/quote/${encodeURIComponent(r.ticker)}/" target="_blank" rel="noopener noreferrer">Source quote ↗</a></div><div class="metrics">${metric('Published adjusted price',fmt(r.price,' USD'))}${metric('20D RVOL',fmt(r.rvol,'x'),'Latest volume / preceding 20-session mean')}${metric('Market date',r.date || 'N/A',r.status)}</div><div class="table-wrap"><table><thead><tr><th>Trading days</th><th>Return</th><th>vs SPY</th></tr></thead><tbody>${PERIODS.map(p => `<tr><td>${p}</td><td class="${tone(r.returns[p])}">${fmt(r.returns[p],'%',true)}</td><td class="${tone(r.relative[p])}">${fmt(r.relative[p],' pp',true)}</td></tr>`).join('')}</tbody></table></div><h3>Conditions to check</h3><ul class="conditions">${warnings(r).map(s => `<li>${esc(s)}</li>`).join('')}</ul><p class="muted">Source field: ${esc(r.source)}. Snapshot: ${model.date}. The quote link may show a different, later price.</p><p class="muted">The published snapshot does not contain 60 sessions or a candle series. MA60 and a historical price chart are intentionally not invented.</p>`;
  }
  function mount() {
    const el = id => document.getElementById(id);
    if (!el('research-app')) return;
    const state = {model:null,period:'5',theme:'all',type:'all',query:'',sort:'relative',direction:'desc',selected:'NVDA',tab:'radar',loading:false};
    function activeTab(tab, focus = false) {
      if (!['radar','map','stocks','review'].includes(tab)) return;
      state.tab=tab;
      document.querySelectorAll('[data-tab]').forEach(b => {b.setAttribute('aria-selected',String(b.dataset.tab===tab)); b.tabIndex=b.dataset.tab===tab?0:-1;});
      document.querySelectorAll('[data-panel]').forEach(p => {p.hidden=p.dataset.panel!==tab;});
      if (focus) document.querySelector(`[data-tab="${tab}"]`).focus();
    }
    function drawTable() {
      const rows=state.model.rows.filter(r => (state.theme==='all'||r.theme===state.theme)&&(state.type==='all'||r.type===state.type)&&`${r.ticker} ${r.name}`.toLowerCase().includes(state.query));
      el('stock-rows').innerHTML=table(sortedRows(rows,state.period,state.sort,state.direction),state.period);
      el('results-count').textContent=`${rows.length} tracked instruments · ${state.period}D return window`;
      document.querySelectorAll('[data-sort]').forEach(b => b.closest('th').setAttribute('aria-sort',b.dataset.sort===state.sort ? (state.direction==='asc'?'ascending':'descending') : 'none'));
    }
    function draw() {
      const m=state.model;
      el('snapshot-date').textContent=m.date;
      el('generated-at').textContent=`Generated ${m.updated}`;
      const age=Date.now()-Date.parse(m.updated.replace(' UTC','Z').replace(' ','T'));
      const missing=m.rows.filter(r => r.status!=='available').length;
      el('quality').textContent=`${m.rows.length-missing}/${m.rows.length} instruments with complete source status. Missing measurements stay N/A.${age>96*3600000 ? ' Warning: this snapshot was generated more than 96 hours ago.' : ''} Daily snapshot, not live quotes.`;
      el('radar-cards').innerHTML=cards(m,state.period);
      el('rotation-matrix').innerHTML=matrix(m);
      el('theme-map').innerHTML=nodes(m,state.period);
      drawTable();
      const r=m.rows.find(r => r.ticker===state.selected)||m.rows[0];
      if (r) {state.selected=r.ticker;el('stock-detail').innerHTML=detail(r,m);}
      el('review-text').textContent=review(m,state.period);
      document.querySelectorAll('[data-period]').forEach(b => b.setAttribute('aria-pressed',String(b.dataset.period===state.period)));
      el('research-content').hidden=false;
    }
    async function load() {
      if(state.loading) return;
      state.loading=true;el('reload').disabled=true;el('research-app').setAttribute('aria-busy','true');
      el('load-status').textContent='Loading the published snapshot…';
      const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),15000);
      try {
        const response=await fetch('./data.json',{cache:'no-store',signal:controller.signal});
        if(!response.ok) throw new Error(`Snapshot request failed (${response.status}).`);
        const candidate=normalize(await response.json());
        state.model=candidate;draw();el('load-status').textContent='Published snapshot loaded. Reload does not trigger a new market-data collection.';
      } catch(error) {
        el('load-status').textContent=`${state.model ? 'Showing the previously loaded snapshot. ' : ''}${error.name==='AbortError' ? 'The snapshot request timed out.' : error.message} Retry or open Market overview.`;
      } finally {clearTimeout(timer);state.loading=false;el('reload').disabled=false;el('research-app').setAttribute('aria-busy','false');}
    }
    function openStock(ticker) {
      if(!state.model?.rows.some(r => r.ticker===ticker)) return;
      state.selected=ticker;activeTab('stocks');el('stock-detail').innerHTML=detail(state.model.rows.find(r=>r.ticker===ticker),state.model);
      history.replaceState(null,'',`#stock-${ticker}`);el('stock-detail').focus();
    }
    document.addEventListener('click',event => {
      const b=event.target.closest('button');if(!b) return;
      if(b.dataset.tab) activeTab(b.dataset.tab);
      if(b.dataset.period&&state.model&&PERIODS.includes(b.dataset.period)) {state.period=b.dataset.period;draw();}
      if(b.dataset.stock) openStock(b.dataset.stock);
      if(b.dataset.theme&&Object.hasOwn(SPEC,b.dataset.theme)) {state.theme=b.dataset.theme;state.type='stock';state.query='';el('theme-filter').value=state.theme;el('type-filter').value=state.type;el('stock-search').value='';drawTable();activeTab('stocks',true);}
      if(b.dataset.sort&&state.model) {state.direction=state.sort===b.dataset.sort&&state.direction==='desc'?'asc':'desc';state.sort=b.dataset.sort;drawTable();}
    });
    el('research-tabs').addEventListener('keydown',event => {
      const tabs=['radar','map','stocks','review'];let i=tabs.indexOf(state.tab);
      if(event.key==='ArrowRight') i=(i+1)%4;else if(event.key==='ArrowLeft') i=(i+3)%4;else if(event.key==='Home') i=0;else if(event.key==='End') i=3;else return;
      event.preventDefault();activeTab(tabs[i],true);
    });
    el('theme-filter').addEventListener('change',e => {state.theme=e.target.value;if(state.model)drawTable();});
    el('type-filter').addEventListener('change',e => {state.type=e.target.value;if(state.model)drawTable();});
    el('stock-search').addEventListener('input',e => {state.query=e.target.value.trim().toLowerCase();if(state.model)drawTable();});
    el('reload').addEventListener('click',load);
    el('copy-review').addEventListener('click',async()=>{if(!state.model)return;try{await navigator.clipboard.writeText(review(state.model,state.period));el('copy-status').textContent='Brief copied.';}catch{el('copy-status').textContent='Clipboard unavailable. Use Download brief or select the text below.';}});
    el('download-review').addEventListener('click',()=>{if(!state.model)return;const url=URL.createObjectURL(new Blob([review(state.model,state.period)],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`research-${state.model.date}-${state.period}D.txt`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
    load().then(()=>{const match=location.hash.match(/^#stock-([A-Z]+)$/);if(match)openStock(match[1]);});
  }
  const api={SPEC,normalize,aggregate,position,warnings,sortedRows,review,esc,fmt,cards,matrix,nodes,table,detail};
  if(typeof module==='object'&&module.exports)module.exports=api;
  root.MarketResearch=api;
  if(typeof document!=='undefined') {if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();}
})(typeof globalThis!=='undefined'?globalThis:this);
