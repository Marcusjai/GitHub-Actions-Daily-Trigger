(function (root) {
  'use strict';

  // Fictional instruments, fixed dates and seeded values. Never market observations.
  const MARKETS = Object.freeze({
    US: Object.freeze({ symbol: 'US-DEMO', name: 'US sample', currency: 'USD', seed: 1701, start: 120 }),
    HK: Object.freeze({ symbol: 'HK-DEMO', name: 'Hong Kong sample', currency: 'HKD', seed: 8527, start: 280 })
  });
  const DAY = 86400000;
  const START = Date.UTC(2024, 0, 2);
  const round = value => Math.round(value * 100) / 100;

  function generateBars(market) {
    if (!Object.hasOwn(MARKETS, market)) throw new RangeError('Unknown sample market');
    const spec = MARKETS[market];
    let seed = spec.seed;
    const random = () => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      return seed / 4294967296;
    };
    const bars = [];
    let previous = spec.start;
    for (let time = START; bars.length < 180; time += DAY) {
      const weekday = new Date(time).getUTCDay();
      if (weekday === 0 || weekday === 6) continue;
      const open = round(previous * (1 + (random() - 0.5) * 0.008));
      const move = (random() - 0.48) * 0.035 + Math.sin(bars.length / 13) * 0.003;
      const close = round(Math.max(1, open * (1 + move)));
      const high = round(Math.max(open, close) * (1 + 0.002 + random() * 0.009));
      const low = round(Math.max(0.01, Math.min(open, close) * (1 - 0.002 - random() * 0.009)));
      bars.push({ time, open, high, low, close, volume: Math.floor(300000 + random() * 1700000) });
      previous = close;
    }
    return bars;
  }

  // UTC Monday buckets; first/last buckets can be partial. No exchange calendar is implied.
  function weeklyBars(daily) {
    const result = [];
    let weekKey;
    for (const bar of daily) {
      const weekday = new Date(bar.time).getUTCDay();
      const key = bar.time - ((weekday + 6) % 7) * DAY;
      if (key !== weekKey) {
        result.push({ ...bar });
        weekKey = key;
      } else {
        const week = result[result.length - 1];
        week.high = Math.max(week.high, bar.high);
        week.low = Math.min(week.low, bar.low);
        week.close = bar.close;
        week.volume += bar.volume;
      }
    }
    return result;
  }

  function barsFor(market, interval) {
    if (!['1D', '1W'].includes(interval)) throw new RangeError('Unknown sample interval');
    const bars = generateBars(market);
    return interval === '1W' ? weeklyBars(bars) : bars;
  }

  const api = { MARKETS, generateBars, weeklyBars, barsFor };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ChartSamples = api;
})(typeof window !== 'undefined' ? window : globalThis);
