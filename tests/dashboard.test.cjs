'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const d = require('../docs/dashboard.js');
const code = fs.readFileSync(path.join(__dirname,'../docs/dashboard.js'),'utf8');
const html = fs.readFileSync(path.join(__dirname,'../docs/index.html'),'utf8');
function fixture() {
  const row = ticker => ({ticker, name:ticker, price:102, return:2, alpha:1, rvol:1.5, rvolLookback:20, signal:'DISTRIBUTION', change:'+2.00', market_date:'2026-09-18', darkPool:43.16, darkPoolStatus:'available', darkPoolDate:'2026-09-18', darkPoolMetric:'off_exchange_day_pct'});
  return {market_date:'2026-09-18', last_updated:'2026-09-18 21:30:00 UTC', indices:['SPY','QQQ','IWM','DIA'].map(row), sectors:['SMH','URA','XLU','XLK','XLI','XLF','XBI','XLE','XLV','XLP'].map(row)};
}
function browser(reply) {
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(m => [m[1], {textContent:'',innerHTML:'',value:'',disabled:false,classList:{add(){},remove(){}}}]));
  const requests = [];
  const context = vm.createContext({document:{getElementById:id=>elements.get(id)}, window:{addEventListener(){}}, console, Date, AbortController,
    setTimeout:()=>1,clearTimeout(){}, fetch:async(url)=>{requests.push(url);return {ok:true,json:async()=>reply};}, navigator:{clipboard:{writeText:async()=>{}}}});
  vm.runInContext(code, context);
  return {context,elements,requests};
}
test('finite formatter preserves real zero and distinguishes null', ()=>{
  assert.equal(d.fmt(0,'%'),'0.00%'); assert.equal(d.fmt(null,'%'),'N/A'); assert.equal(d.fmt(NaN),'N/A'); assert.equal(d.fmt('43.16','%'),'N/A');
});
test('missing percentages never participate in either sort direction', ()=>{
  for (const dir of ['asc','desc']) {
    const values=[{darkPool:null},{darkPool:0},{darkPool:43.16}].sort((a,b)=>d.compareRows(a,b,'darkPool',dir));
    assert.equal(values.at(-1).darkPool,null);
  }
});
test('ticker sort is alphabetical rather than numeric subtraction', ()=>{
  assert.ok(d.compareRows({ticker:'QQQ'},{ticker:'SPY'},'ticker','asc')<0);
});
test('complete dated payload passes', ()=>assert.equal(d.normalizePayload(fixture()).missing_off_exchange.length,0));
test('null percentages become partial data', ()=>{
  const p=fixture();p.sectors[0].darkPool=null;
  const n=d.normalizePayload(p); assert.equal(n.data_quality,'partial');assert.deepEqual(n.missing_off_exchange,['SMH']);
});
test('wrong dates are excluded even when a percentage is supplied', ()=>{
  const p=fixture();p.sectors[0].darkPoolDate='2026-09-17';
  assert.equal(d.normalizePayload(p).sectors[0].darkPool,null);
});
test('numbers without source metadata cannot masquerade as observations', ()=>{
  const p=fixture();delete p.sectors[0].darkPoolMetric;
  assert.equal(d.normalizePayload(p).sectors[0].darkPool,null);
});
test('out of range values are not displayed', ()=>{
  const p=fixture();p.sectors[0].darkPool=101;
  assert.equal(d.normalizePayload(p).sectors[0].darkPool,null);
});
test('old unversioned snapshot is rejected', ()=>{
  const p=fixture();delete p.market_date; assert.throws(()=>d.normalizePayload(p));
});
test('missing ETF, duplicates, nonfinite prices and wrong RVOL window are rejected', ()=>{
  for (const mutate of [p=>p.sectors.pop(),p=>p.sectors[0].ticker='XLK',p=>p.indices[0].price=NaN,p=>p.indices[0].rvolLookback=10]) {
    const p=fixture();mutate(p);assert.throws(()=>d.normalizePayload(p));
  }
});
test('HTML escaping and source link validation', ()=>{
  assert.equal(d.escapeHtml('<img>'),'&lt;img&gt;');assert.equal(d.getChartExchangeUrl('x" onload="evil'),'#');
});
test('invalid calendar dates are rejected', ()=>{
  assert.equal(d.validDate('2026-02-30'),false);assert.equal(d.validDate('2026-09-18'),true);
});
test('no random values, proxy calls, or fake 100% claims remain', ()=>{
  for (const token of ['Math.random','charCodeAt','corsproxy','100% Real']) assert.equal((code+html).includes(token),false);
});
test('all-null browser render shows N/A and distribution label without crashing', async()=>{
  const p=fixture();for(const r of [...p.indices,...p.sectors])r.darkPool=null;
  const b=browser(p);await vm.runInContext('loadDashboardData()',b.context);
  assert.equal(b.elements.get('kpi-darkpool-ticker').textContent,'N/A');
  assert.match(b.elements.get('sector-table-body').innerHTML,/相對偏弱/);
  assert.doesNotMatch(b.elements.get('sector-table-body').innerHTML,/null%|NaN|undefined/);
  assert.match(b.elements.get('data-source-badge').textContent,/部分/);
  assert.equal(b.elements.get('fetch-live-btn').disabled,false);
  assert.equal(b.requests.length,1);assert.match(b.requests[0],/^\.\/data\.json/);
});
test('failed refresh retains previous snapshot and shows failure', async()=>{
  const b=browser(fixture());await vm.runInContext('loadDashboardData()',b.context);
  const before=b.elements.get('sector-table-body').innerHTML;
  b.context.fetch=async()=>{throw new Error('test timeout');};
  await vm.runInContext('loadDashboardData()',b.context);
  assert.equal(b.elements.get('sector-table-body').innerHTML,before);
  assert.match(b.elements.get('data-source-badge').textContent,/載入失敗/);
});
test('missing Chart CDN does not stop prices or table rendering',async()=>{
  const b=browser(fixture());await vm.runInContext('loadDashboardData()',b.context);
  assert.match(b.elements.get('chart-note').textContent,/未載入/);
  assert.match(b.elements.get('broad-indices-grid').innerHTML,/SPY/);
});
test('theme windows show separate issuer issuance and price rotation',async()=>{
  const p=fixture();
  p.asof=p.market_date;
  p.issuer_flows={ARTY:{'5':{status:'available',usd:10400000,start:'2026-09-11',end:p.market_date,observations:6,required:6},
                          '20':{status:'insufficient_history',usd:null,observations:6,required:21}},
                  IBIT:{'5':{status:'insufficient_history',usd:null,observations:1,required:6}}};
  const row=(ticker,kind)=>({ticker,kind,date:p.market_date,returns:{'5':2,'20':5},vs_spy:{'5':1.2,'20':2.3}});
  p.groups=[{id:'AI',label:'AI',flow_proxy:'ARTY',instruments:[row('ARTY','AI ETF'),row('NVDA','股票')]},
            {id:'BTC',label:'Bitcoin',flow_proxy:'IBIT',instruments:[row('IBIT','現貨 Bitcoin ETP'),{ticker:'COIN',kind:'股票',date:p.market_date,status:'unavailable'}]}];
  const b=browser(p);await vm.runInContext('loadDashboardData()',b.context);
  assert.match(b.elements.get('theme-panels').innerHTML,/ARTY.*10.40 百萬美元/s);
  assert.match(b.elements.get('theme-panels').innerHTML,/IBIT.*N\/A/s);
  assert.match(b.elements.get('theme-panels').innerHTML,/COIN.*N\/A/s);
  vm.runInContext("setThemePeriod('20')",b.context);
  assert.doesNotMatch(b.elements.get('theme-panels').innerHTML,/10.40 百萬美元/);
});
test('dated issuer flow stays visible when one session behind and hides when older',async()=>{
  const p=fixture();p.asof=p.market_date;p.sessions=['2026-09-16','2026-09-17',p.market_date];
  p.issuer_flows={ARTY:{'5':{status:'available',usd:1000000,start:'2026-09-10',end:'2026-09-17',required:6,observations:6}},IBIT:{}};
  const row=ticker=>({ticker,kind:'AI ETF',date:p.market_date,returns:{'5':1},vs_spy:{'5':1}});
  p.groups=[{id:'AI',label:'AI',flow_proxy:'ARTY',instruments:[row('ARTY')]},
            {id:'BTC',label:'BTC',flow_proxy:'IBIT',instruments:[row('IBIT')]}];
  const b=browser(p);await vm.runInContext('loadDashboardData()',b.context);
  assert.match(b.elements.get('theme-panels').innerHTML,/\+1\.00 百萬美元/);
  p.issuer_flows.ARTY['5'].end='2026-09-10';await vm.runInContext('loadDashboardData()',b.context);
  assert.doesNotMatch(b.elements.get('theme-panels').innerHTML,/\+1\.00 百萬美元/);
});
