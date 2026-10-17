(function () {
  'use strict';
  const el = id => document.getElementById(id);
  const sample = window.ChartSamples;
  const host = el('sample-chart');
  const controls = ['reset-chart', 'pan-older', 'pan-newer', 'zoom-in', 'zoom-out', 'all-bars', 'sample-style'];
  let chart = null;
  let bars = [];
  let generation = 0;
  let ready = false;
  let renderedSelection = '';
  const selection = () => ['sample-market', 'sample-interval', 'sample-style'].map(id => el(id).value).join('|');

  function enableControls(enabled) {
    ready = enabled;
    controls.forEach(id => { el(id).disabled = !enabled; });
  }

  function describeSamples(spec) {
    const date = time => new Date(time).toISOString().slice(0, 10);
    const price = value => `${spec.currency} ${value.toFixed(2)}`;
    const last = bars[bars.length - 1];
    el('chart-title').replaceChildren(document.createTextNode(spec.symbol));
    const subtitle = document.createElement('span');
    subtitle.className = 'muted';
    subtitle.textContent = ` · Synthetic sample · ${spec.currency}`;
    el('chart-title').appendChild(subtitle);
    host.setAttribute('aria-label', `${spec.symbol} synthetic sample chart in ${spec.currency}`);
    el('sample-date').textContent = `Synthetic ${el('sample-interval').value === '1W' ? 'week starting' : 'date'} ${date(last.time)} (UTC). Not an actual trading session.`;
    el('sample-summary').replaceChildren();
    ['open', 'high', 'low', 'close'].forEach(key => {
      const item = document.createElement('div');
      const label = document.createElement('dt');
      label.textContent = `Sample ${key}`;
      const value = document.createElement('dd');
      value.textContent = price(last[key]);
      item.append(label, value);
      el('sample-summary').appendChild(item);
    });
    el('sample-table-caption').textContent = `${spec.symbol} · All prices in ${spec.currency} · Synthetic ${el('sample-interval').value === '1W' ? 'weekly' : 'daily'} samples only. Volume is invented, not recorded trading activity.`;
    el('sample-rows').replaceChildren();
    bars.slice(-8).forEach(bar => {
      const row = document.createElement('tr');
      [date(bar.time), ...['open', 'high', 'low', 'close'].map(key => bar[key].toFixed(2)), bar.volume.toLocaleString('en-US')].forEach(value => {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.appendChild(cell);
      });
      el('sample-rows').appendChild(row);
    });
  }

  function resetView() {
    if (!ready) return;
    chart.renderer.set('autoScale', true);
    const visible = bars.slice(-Math.min(60, bars.length));
    chart.setVisibleRange({ from: visible[0].time, to: visible[visible.length - 1].time });
  }

  function zoom(factor) {
    if (!ready) return;
    const range = chart.getVisibleRange();
    if (!range || range.to <= range.from) return;
    const step = el('sample-interval').value === '1W' ? 7 * 86400000 : 86400000;
    const span = Math.max(step * 8, Math.min((bars[bars.length - 1].time - bars[0].time) * 1.25, (range.to - range.from) * factor));
    const center = (range.from + range.to) / 2;
    chart.setVisibleRange({ from: center - span / 2, to: center + span / 2 });
  }

  async function render() {
    const current = ++generation;
    renderedSelection = selection();
    enableControls(false);
    el('chart-error').hidden = true;
    el('chart-status').textContent = 'Loading the local sample chart…';
    if (chart) { chart.destroy(); chart = null; }
    host.replaceChildren();
    const spec = sample.MARKETS[el('sample-market').value];
    bars = sample.barsFor(el('sample-market').value, el('sample-interval').value);
    describeSamples(spec);
    try {
      if (!window.Vela || typeof window.Vela.Vela !== 'function') throw new Error('Vela bundle unavailable');
      const instance = new window.Vela.Vela(host, {
        data: bars,
        symbol: spec.symbol,
        timeframe: el('sample-interval').value,
        live: false,
        theme: 'dark',
        drawings: false,
        volume: false,
        animations: false,
        priceStyle: el('sample-style').value,
        upColor: '#73dfb5',
        downColor: '#ffa5aa'
      });
      chart = instance;
      // The host supplies one consistent, described keyboard navigation surface.
      instance.renderer.set({ timezone: 'UTC', countdown: false, keyboard: false });
      await instance.ready();
      if (current !== generation) return;
      enableControls(true);
      resetView();
      el('chart-status').textContent = `${spec.symbol} · ${bars.length} synthetic ${el('sample-interval').value === '1W' ? 'weekly' : 'daily'} candles · Prices in ${spec.currency} · Dates in UTC · No market feed`;
    } catch (error) {
      if (current !== generation) return;
      if (chart) { chart.destroy(); chart = null; }
      enableControls(false);
      el('chart-error').hidden = false;
      el('chart-status').textContent = 'Chart unavailable. The generated sample summary and table remain usable.';
    }
  }

  el('sample-market').addEventListener('change', render);
  el('sample-interval').addEventListener('change', render);
  el('sample-style').addEventListener('change', () => {
    if (ready) {
      chart.renderer.set('priceStyle', el('sample-style').value);
      renderedSelection = selection();
    }
  });
  el('reset-chart').addEventListener('click', resetView);
  el('all-bars').addEventListener('click', () => { if (ready) { chart.renderer.set('autoScale', true); chart.setVisibleRangePreset('ALL'); } });
  el('pan-older').addEventListener('click', () => { if (ready) chart.panBy(-0.25); });
  el('pan-newer').addEventListener('click', () => { if (ready) chart.panBy(0.25); });
  el('zoom-in').addEventListener('click', () => zoom(0.7));
  el('zoom-out').addEventListener('click', () => zoom(1 / 0.7));
  host.addEventListener('keydown', event => {
    if ((event.target !== host && !(event.target instanceof HTMLCanvasElement)) || !ready || event.ctrlKey || event.metaKey || event.altKey) return;
    const actions = { ArrowLeft: () => chart.panBy(-0.25), ArrowRight: () => chart.panBy(0.25), '+': () => zoom(0.7), '=': () => zoom(0.7), '-': () => zoom(1 / 0.7), Home: resetView };
    if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
  });
  host.addEventListener('pointerdown', event => {
    if (event.target instanceof HTMLCanvasElement) host.focus({ preventScroll: true });
  });
  const observer = new ResizeObserver(() => { if (chart) chart.resize(); });
  observer.observe(host);
  // Browsers can restore form selections after deferred scripts have rendered.
  window.addEventListener('pageshow', () => {
    if (renderedSelection !== selection()) render();
    else if (chart) chart.resize();
  });
  // Retain an intact page in the back/forward cache; clean up only on final unload.
  window.addEventListener('pagehide', event => {
    if (event.persisted) return;
    generation++;
    observer.disconnect();
    if (chart) chart.destroy();
  });
  render();
})();
