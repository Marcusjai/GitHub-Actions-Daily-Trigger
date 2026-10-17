'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const vm = require('node:vm');
const S = require('../docs/chart-samples.js');
const DOCS = path.join(__dirname, '../docs');
const read = name => fs.readFileSync(path.join(DOCS, name), 'utf8');
const DAY = 86400000;
const weekKey = time => time - ((new Date(time).getUTCDay() + 6) % 7) * DAY;

test('sample markets are frozen, explicitly fictional and use distinct currency units', () => {
  assert.deepEqual(Object.keys(S.MARKETS), ['US', 'HK']);
  assert.ok(Object.isFrozen(S.MARKETS));
  assert.equal(S.MARKETS.US.symbol, 'US-DEMO');
  assert.equal(S.MARKETS.HK.symbol, 'HK-DEMO');
  assert.equal(S.MARKETS.US.currency, 'USD');
  assert.equal(S.MARKETS.HK.currency, 'HKD');
  for (const spec of Object.values(S.MARKETS)) assert.ok(Object.isFrozen(spec));
  assert.notEqual(S.MARKETS.US.seed, S.MARKETS.HK.seed);
  assert.notDeepEqual(S.generateBars('US'), S.generateBars('HK'));
});

for (const market of ['US', 'HK']) {
  test(`${market}: 180 deterministic weekday candles have fixed UTC boundaries`, () => {
    const bars = S.generateBars(market);
    assert.equal(bars.length, 180);
    assert.deepEqual(bars, S.generateBars(market));
    assert.deepEqual(bars, S.barsFor(market, '1D'));
    assert.equal(new Date(bars[0].time).toISOString(), '2024-01-02T00:00:00.000Z');
    assert.equal(new Date(bars.at(-1).time).toISOString(), '2024-09-09T00:00:00.000Z');
    assert.equal(new Set(bars.map(bar => bar.time)).size, bars.length);
    bars.forEach((bar, index) => {
      assert.ok(Number.isSafeInteger(bar.time));
      assert.equal(bar.time % DAY, 0);
      assert.ok([1, 2, 3, 4, 5].includes(new Date(bar.time).getUTCDay()));
      if (index) {
        assert.ok(bar.time > bars[index - 1].time);
        assert.ok([DAY, 3 * DAY].includes(bar.time - bars[index - 1].time));
      }
    });
  });

  test(`${market}: every OHLC value is finite, positive and internally consistent`, () => {
    for (const bar of S.generateBars(market)) {
      assert.deepEqual(Object.keys(bar).sort(), ['close', 'high', 'low', 'open', 'time', 'volume']);
      for (const field of ['open', 'high', 'low', 'close']) {
        assert.ok(Number.isFinite(bar[field]) && bar[field] > 0, field);
        assert.ok(Math.abs(bar[field] * 100 - Math.round(bar[field] * 100)) < 1e-8);
      }
      assert.ok(bar.low <= Math.min(bar.open, bar.close));
      assert.ok(bar.high >= Math.max(bar.open, bar.close));
      assert.ok(Number.isSafeInteger(bar.volume) && bar.volume > 0);
    }
  });

  test(`${market}: 37 weekly candles aggregate all daily bars exactly once`, () => {
    const daily = S.generateBars(market);
    const before = JSON.stringify(daily);
    daily.forEach(Object.freeze);
    Object.freeze(daily);
    const weekly = S.weeklyBars(daily);
    assert.equal(weekly.length, 37);
    assert.deepEqual(weekly, S.barsFor(market, '1W'));
    const groups = new Map();
    for (const bar of daily) {
      const key = weekKey(bar.time);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(bar);
    }
    assert.equal(groups.size, weekly.length);
    [...groups.values()].forEach((members, index) => {
      assert.deepEqual(weekly[index], {
        time: members[0].time, open: members[0].open,
        high: Math.max(...members.map(bar => bar.high)),
        low: Math.min(...members.map(bar => bar.low)),
        close: members.at(-1).close,
        volume: members.reduce((sum, bar) => sum + bar.volume, 0)
      });
      assert.notEqual(weekly[index], members[0]);
    });
    assert.equal([...groups.values()][0].length, 4, 'first week starts Tuesday');
    assert.equal([...groups.values()].at(-1).length, 1, 'last week ends Monday');
    assert.equal(weekly.reduce((sum, bar) => sum + bar.volume, 0), daily.reduce((sum, bar) => sum + bar.volume, 0));
    assert.equal(JSON.stringify(daily), before, 'aggregation must not mutate the input');
  });
}

test('separate callers receive independent arrays and candle objects', () => {
  for (const interval of ['1D', '1W']) {
    const first = S.barsFor('US', interval);
    const expected = S.barsFor('US', interval);
    first[0].close = -999;
    first.pop();
    assert.deepEqual(S.barsFor('US', interval), expected);
  }
});

test('weekly aggregation handles empty input, singleton and a year boundary', () => {
  assert.deepEqual(S.weeklyBars([]), []);
  const bar = {time: Date.UTC(2023, 11, 29), open: 10, high: 15, low: 8, close: 11, volume: 10};
  assert.deepEqual(S.weeklyBars([bar]), [bar]);
  assert.notEqual(S.weeklyBars([bar])[0], bar);
  const next = {...bar, time: Date.UTC(2024, 0, 1), volume: 20};
  const last = {...bar, time: Date.UTC(2024, 0, 2), close: 12, volume: 30};
  assert.deepEqual(S.weeklyBars([bar, next, last]), [bar, {...next, close: 12, volume: 50}]);
});

test('unsupported markets and intervals fail instead of silently showing different samples', () => {
  for (const market of ['', 'uk', 'US-DEMO', 'constructor', 'toString', '__proto__', null, undefined]) assert.throws(() => S.generateBars(market), RangeError);
  for (const interval of ['', '1H', '1M', '1d', null, undefined]) assert.throws(() => S.barsFor('US', interval), RangeError);
});

test('browser build exposes the same generator without Node or network dependencies', () => {
  const context = {window: {}};
  vm.runInNewContext(read('chart-samples.js'), context);
  assert.equal(typeof context.window.ChartSamples.barsFor, 'function');
  assert.equal(JSON.stringify(context.window.ChartSamples.barsFor('HK', '1W')), JSON.stringify(S.barsFor('HK', '1W')));
});

test('HTML loads only existing local scripts and styles in the correct order', () => {
  const html = read('charts.html');
  const scripts = [...html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"[^>]*>/g)];
  assert.deepEqual(scripts.map(match => match[1]), ['./chart-samples.js', './vendor/vela-0.8.1/vela.global.min.js', './charts.js']);
  assert.ok(scripts.every(match => /\bdefer\b/.test(match[0])));
  const assets = [...scripts.map(match => match[1]), ...[...html.matchAll(/<link\b[^>]*href="([^"]+)"[^>]*>/g)].map(match => match[1])];
  for (const asset of assets) {
    assert.match(asset, /^\.\//);
    assert.ok(fs.existsSync(path.join(DOCS, asset)), asset);
  }
  assert.doesNotMatch(html, /<script\b[^>]*>\s*\S[^<]*<\/script>|\son\w+\s*=|<iframe\b/i);
});

test('sample page CSP blocks connections and app code never loads market data', () => {
  const html = read('charts.html');
  const csp = html.match(/<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"/i)?.[1];
  assert.ok(csp, 'explicit CSP');
  for (const directive of ["default-src 'none'", "connect-src 'none'", "script-src 'self'", "object-src 'none'", "base-uri 'none'", "form-action 'none'"]) assert.ok(csp.split(';').map(x => x.trim()).includes(directive), directive);
  for (const file of ['chart-samples.js', 'charts.js']) {
    assert.doesNotMatch(read(file), /\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b|\bEventSource\b|data\.json|\bMath\.random\s*\(|\bDate\.now\s*\(/);
  }
  assert.match(read('charts.js'), /live:\s*false/);
  assert.doesNotMatch(read('charts.css'), /@import|url\(\s*['"]?https?:/);
});

test('fictional labeling, accessible fallback table and visible attribution are retained', () => {
  const html = read('charts.html');
  for (const text of ['SYNTHETIC SAMPLE DATA ONLY', 'US-DEMO and HK-DEMO are fictional symbols', 'not actual stock history', 'Invented volume', 'no exchange holidays', 'not currency conversions', 'Vela™ by LuxAlgo', 'Apache-2.0']) assert.ok(html.includes(text), text);
  for (const id of ['sample-disclosure', 'sample-chart', 'sample-market', 'sample-interval', 'sample-style', 'chart-status', 'chart-error', 'sample-rows', 'sample-table-caption']) assert.ok(html.includes(`id="${id}"`), id);
  assert.match(html, /role="status"\s+aria-live="polite"/);
  assert.match(html, /id="sample-chart"[^>]*tabindex="0"/);
  assert.match(html, /<table>[\s\S]*<caption[\s\S]*<th scope="col">/);
  assert.match(html, /href="\.\/vendor\/vela-0\.8\.1\/NOTICE"/);
  assert.match(html, /href="\.\/vendor\/vela-0\.8\.1\/LICENSE"/);
  assert.match(html, /<noscript>[\s\S]*fictional/);
});

test('pinned Vela bundle matches recorded checksum and includes required notices', () => {
  const prefix = 'vendor/vela-0.8.1/';
  const recorded = read(prefix + 'README.md').match(/`([a-f0-9]{64})`/)?.[1];
  assert.ok(recorded);
  const actual = crypto.createHash('sha256').update(fs.readFileSync(path.join(DOCS, prefix + 'vela.global.min.js'))).digest('hex');
  assert.equal(actual, recorded);
  assert.match(read(prefix + 'LICENSE'), /Apache License/);
  assert.match(read(prefix + 'NOTICE'), /LuxAlgo/);
});
