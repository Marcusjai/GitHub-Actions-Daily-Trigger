"""Exercise the real, locally vendored Vela chart, without a market feed.

Test-only dependency: playwright==1.55.0 and its Chromium installation.
Run: python scripts/smoke_charts_browser.py
To use an installed browser: PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium
Screenshots and a machine-readable report are written to test-artifacts/chart*.
"""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'test-artifacts'

# Observe actual instances without adding a production debug global or substituting
# a chart mock. All methods and rendering remain those of the pinned Vela bundle.
# Public APIs: Vela.getVisibleRange(), Vela.renderer.get(feature), Vela.destroy().
# Reference: https://github.com/LuxAlgo/Vela (src/Vela.ts, core/RendererControl.ts).
CAPTURE_VELA = r"""(() => {
    window.__chartTest = {instances: [], active: null, violations: []};
    document.addEventListener('securitypolicyviolation', event => {
        const violation = {directive: event.effectiveDirective, blocked: event.blockedURI};
        window.__chartTest.violations.push(violation);
        if (typeof window.__reportChartCspViolation === 'function') {
            window.__reportChartCspViolation(violation);
        }
    });
    let namespace;
    Object.defineProperty(window, 'Vela', {
        configurable: true,
        get: () => namespace,
        set(value) {
            if (!value || typeof value.Vela !== 'function') { namespace = value; return; }
            const Constructor = value.Vela;
            const WrappedConstructor = new Proxy(Constructor, {
                construct(target, args) {
                    const instance = Reflect.construct(target, args, target);
                    const record = {instance, options: JSON.parse(JSON.stringify(args[1])), destroyed: false};
                    const destroy = instance.destroy;
                    instance.destroy = function(...params) {
                        record.destroyed = true;
                        return Reflect.apply(destroy, this, params);
                    };
                    window.__chartTest.instances.push(record);
                    window.__chartTest.active = record;
                    return instance;
                }
            });
            // esbuild exposes the original namespace via getter-only properties.
            // Wrap reads instead of trying to overwrite value.Vela.
            namespace = new Proxy(value, {
                get(target, property, receiver) {
                    return property === 'Vela' ? WrappedConstructor : Reflect.get(target, property, receiver);
                }
            });
        }
    });
})();"""

# Vela auto-selects Canvas2D or WebGL2. Its public screenshot API synchronously
# paints and composites both, including non-preserved WebGL drawing buffers.
# Inspect those real pixels while also requiring on-page canvases to be visible.
PAINT_METRICS = r"""async () => {
    const metrics = {canvases: 0, nontransparent: 0, candlePixels: 0, candleRows: 0};
    for (const canvas of document.querySelectorAll('#sample-chart canvas')) {
        const bounds = canvas.getBoundingClientRect();
        const style = getComputedStyle(canvas);
        if (bounds.width >= 100 && bounds.height >= 100 && style.display !== 'none'
            && style.visibility !== 'hidden' && Number(style.opacity) !== 0) metrics.canvases++;
    }
    const active = window.__chartTest.active;
    if (!metrics.canvases || !active || active.destroyed) return metrics;
    const source = active.instance.renderer.screenshot();
    if (!source) return metrics;
    const image = new Image();
    image.src = source;
    await image.decode();
    const sample = document.createElement('canvas');
    sample.width = image.naturalWidth;
    sample.height = image.naturalHeight;
    const context = sample.getContext('2d');
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(0, 0, sample.width, sample.height).data;
    const rows = new Set();
    for (let i = 0; i < pixels.length; i += 4) {
        if (pixels[i + 3] < 128) continue;
        metrics.nontransparent++;
        const near = (r, g, b) => Math.abs(pixels[i] - r) < 12
            && Math.abs(pixels[i + 1] - g) < 12 && Math.abs(pixels[i + 2] - b) < 12;
        if (near(115, 223, 181) || near(255, 165, 170)) {
            metrics.candlePixels++;
            rows.add(Math.floor(i / 4 / sample.width));
        }
    }
    metrics.candleRows = rows.size;
    return metrics;
}"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main() -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    passed, errors, external_requests, console_errors, requests = [], [], [], [], []
    csp_violations = []
    paints = []
    failure = None
    server = browser = page = None

    def done(message):
        passed.append(message)
        print(f'PASS: {message}', flush=True)

    def no_overflow(target, label):
        dimensions = target.evaluate('''() => ({
            viewport: document.documentElement.clientWidth,
            content: document.documentElement.scrollWidth
        })''')
        assert dimensions['content'] <= dimensions['viewport'] + 1, (label, dimensions)

    def ready(target, market='US', interval='1D'):
        symbol, currency = ('US-DEMO', 'USD') if market == 'US' else ('HK-DEMO', 'HKD')
        count, unit = (180, 'daily') if interval == '1D' else (37, 'weekly')
        expect(target.locator('#chart-status')).to_contain_text(f'{symbol} · {count} synthetic {unit}')
        expect(target.locator('#chart-status')).to_contain_text(f'Prices in {currency}')
        expect(target.locator('#chart-error')).to_be_hidden()
        expect(target.locator('#reset-chart')).to_be_enabled()
        expect(target.locator('#sample-rows tr')).to_have_count(8)
        expect(target.locator('#sample-summary')).to_contain_text(currency)
        assert target.evaluate('''() => {
            const active = window.__chartTest.active;
            return active && !active.destroyed && active.instance.getVisibleRange() !== null;
        }''')

    def assert_painted(target, label, candles=True):
        target.wait_for_function(f'''async () => {{
            const m = await ({PAINT_METRICS})();
            return m.canvases > 0 && m.nontransparent > 1000
                {'&& m.candlePixels > 100 && m.candleRows > 20' if candles else ''};
        }}''', timeout=15000)
        metrics = target.evaluate(PAINT_METRICS)
        paints.append({'view': label, **metrics})

    def visible_range(target):
        result = target.evaluate('window.__chartTest.active.instance.getVisibleRange()')
        assert result and result['to'] > result['from'], result
        return result

    def wait_range(target, previous, condition):
        target.wait_for_function('''({previous, condition}) => {
            const range = window.__chartTest.active.instance.getVisibleRange();
            if (!range) return false;
            const span = range.to - range.from;
            const oldSpan = previous.to - previous.from;
            if (condition === 'older') return range.from < previous.from - 1000;
            if (condition === 'newer') return range.from > previous.from + 1000;
            if (condition === 'in') return span < oldSpan * .95;
            if (condition === 'out') return span > oldSpan * 1.05;
            if (condition === 'reset') return Math.abs(range.from - previous.from) < 1000
                && Math.abs(range.to - previous.to) < 1000;
            return false;
        }''', arg={'previous': previous, 'condition': condition})
        return visible_range(target)

    try:
        handler = functools.partial(QuietHandler, directory=str(ROOT / 'docs'))
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        Thread(target=server.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{server.server_port}'
        url = origin + '/charts.html'
        with sync_playwright() as p:
            executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
            browser = p.chromium.launch(**({'executable_path': executable} if executable else {}))
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            context.expose_binding('__reportChartCspViolation', lambda source, violation: csp_violations.append(violation))
            context.add_init_script(CAPTURE_VELA)

            def route_request(route):
                request = route.request
                requests.append(request.url)
                if urlparse(request.url).netloc != urlparse(origin).netloc:
                    external_requests.append(request.url)
                    route.abort()
                else:
                    route.continue_()

            context.route('**/*', route_request)

            def observe(target):
                target.on('pageerror', lambda error: errors.append(str(error)))
                target.on('console', lambda message: console_errors.append(message.text)
                          if message.type == 'error' else None)

            page = context.new_page()
            observe(page)
            page.goto(url)
            ready(page)
            assert_painted(page, 'US daily desktop')
            no_overflow(page, 'desktop')
            expect(page.locator('#sample-disclosure')).to_be_visible()
            expect(page.locator('#sample-disclosure')).to_contain_text('SYNTHETIC SAMPLE DATA ONLY')
            expect(page.locator('.sample-watermark')).to_be_visible()
            expect(page.locator('.chart-credit')).to_be_visible()
            expect(page.locator('.chart-credit')).to_contain_text('Vela™ by LuxAlgo')
            expect(page.locator('.chart-credit a[href$="/NOTICE"]')).to_be_visible()
            page.screenshot(path=str(ARTIFACTS / 'chart-desktop.png'), full_page=True)
            done('Real Vela paints colored sample candles, with visible disclosure, watermark and credits')

            initial = visible_range(page)
            page.locator('#pan-older').click()
            older = wait_range(page, initial, 'older')
            assert abs((older['to'] - older['from']) / (initial['to'] - initial['from']) - 1) < .05
            page.locator('#pan-newer').click()
            wait_range(page, older, 'newer')
            before = visible_range(page)
            page.locator('#zoom-in').click()
            zoomed = wait_range(page, before, 'in')
            page.locator('#zoom-out').click()
            wait_range(page, zoomed, 'out')
            page.locator('#reset-chart').click()
            wait_range(page, initial, 'reset')
            page.locator('#all-bars').click()
            expanded = wait_range(page, initial, 'out')
            assert expanded['to'] - expanded['from'] > (initial['to'] - initial['from']) * 2
            page.locator('#reset-chart').click()
            wait_range(page, initial, 'reset')
            done('Older/newer, zoom in/out, all bars and reset change the actual Vela visible range')

            host = page.locator('#sample-chart')
            page.locator('#reset-chart').focus()
            page.keyboard.press('Tab')
            expect(host).to_be_focused()
            expect(page.locator('#sample-chart canvas[tabindex="0"]')).to_have_count(0)
            page.keyboard.press('ArrowLeft')
            keyboard_older = wait_range(page, initial, 'older')
            page.keyboard.press('ArrowRight')
            wait_range(page, keyboard_older, 'newer')
            before = visible_range(page)
            page.keyboard.press('Equal')
            wait_range(page, before, 'in')
            page.keyboard.press('Home')
            wait_range(page, initial, 'reset')
            host.click(position={'x': 120, 'y': 150})
            expect(host).to_be_focused()
            page.keyboard.press('ArrowLeft')
            wait_range(page, initial, 'older')
            page.keyboard.press('Home')
            wait_range(page, initial, 'reset')
            done('Tab and pointer focus share consistent keyboard pan, zoom and Home reset')

            first_summary = page.locator('#sample-summary').inner_text()
            for market, interval in [('HK', '1D'), ('HK', '1W'), ('US', '1W'), ('US', '1D'), ('HK', '1D'), ('US', '1D')]:
                page.locator('#sample-market').select_option(market)
                page.locator('#sample-interval').select_option(interval)
                ready(page, market, interval)
                assert_painted(page, f'{market} {interval}')
                data = page.evaluate('window.__chartTest.active.options')
                assert data['symbol'] == f'{market}-DEMO'
                assert data['timeframe'] == interval and data['live'] is False
                assert len(data['data']) == (180 if interval == '1D' else 37)
                assert page.evaluate('window.__chartTest.instances.filter(x => !x.destroyed).length') == 1
                if market == 'HK':
                    assert page.locator('#sample-summary').inner_text() != first_summary
            assert page.locator('#sample-summary').inner_text() == first_summary
            done('US/HK and daily/weekly switches use distinct deterministic datasets and destroy stale charts')

            for style in ('line', 'candles', 'line', 'candles'):
                page.locator('#sample-style').select_option(style)
                page.wait_for_function('style => window.__chartTest.active.instance.renderer.get("priceStyle") === style', arg=style)
                assert_painted(page, style, candles=style == 'candles')
            page.locator('#sample-style').select_option('line')
            page.locator('#sample-market').select_option('HK')
            ready(page, 'HK')
            assert page.evaluate('window.__chartTest.active.instance.renderer.get("priceStyle")') == 'line'
            page.locator('#sample-style').select_option('candles')
            done('Repeated style changes update the renderer and selected style survives a market switch')

            # Trigger a genuine supersession while earlier instance.ready() is pending.
            page.evaluate('''() => {
                const market = document.getElementById('sample-market');
                for (const value of ['US', 'HK', 'US', 'HK']) {
                    market.value = value;
                    market.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }''')
            ready(page, 'HK')
            assert_painted(page, 'rapid switches')
            assert page.evaluate('window.__chartTest.instances.filter(x => !x.destroyed).length') == 1
            done('Interrupted rapid market switches leave the latest chart live and no stale instance')

            for width in (390, 320):
                page.set_viewport_size({'width': width, 'height': 844})
                for market, interval in [('US', '1D'), ('HK', '1W')]:
                    page.locator('#sample-market').select_option(market)
                    page.locator('#sample-interval').select_option(interval)
                    ready(page, market, interval)
                    assert_painted(page, f'{width}px {market} {interval}')
                    no_overflow(page, f'{width}px {market} {interval}')
                    expect(page.locator('.chart-credit')).to_be_visible()
                page.screenshot(path=str(ARTIFACTS / f'chart-mobile-{width}.png'), full_page=True)
            done('Daily and weekly charts paint at 390px and 320px with no page overflow')

            # Navigate away and back through actual history, using a local test page
            # so unrelated Market Overview integrations cannot affect this test.
            context.route('**/chart-test-away.html', lambda route: route.fulfill(
                status=200, content_type='text/html', body='<title>History test</title><p id="away">Away</p>'))
            for _ in range(2):
                page.goto(origin + '/chart-test-away.html')
                expect(page.locator('#away')).to_be_visible()
                page.go_back()
                page.wait_for_url(url)
                market = page.locator('#sample-market').input_value()
                interval = page.locator('#sample-interval').input_value()
                ready(page, market, interval)
                assert_painted(page, 'history return')
                page.locator('#reset-chart').click()
                page.go_forward()
                expect(page.locator('#away')).to_be_visible()
                page.go_back()
                page.wait_for_url(url)
                ready(page, page.locator('#sample-market').input_value(), page.locator('#sample-interval').input_value())
            done('Back/forward navigation restores a functioning chart on repeated returns')

            fallback = context.new_page()
            observe(fallback)
            fallback.route('**/vela.global.min.js', lambda route: route.abort())
            fallback.goto(url)
            expect(fallback.locator('#chart-error')).to_be_visible()
            expect(fallback.locator('#chart-status')).to_contain_text('summary and table remain usable')
            expect(fallback.locator('#sample-rows tr')).to_have_count(8)
            expect(fallback.locator('#sample-summary')).to_contain_text('USD')
            for selector in ('#reset-chart', '#pan-older', '#pan-newer', '#zoom-in', '#zoom-out', '#all-bars', '#sample-style'):
                expect(fallback.locator(selector)).to_be_disabled()
            fallback.locator('#sample-market').select_option('HK')
            fallback.locator('#sample-interval').select_option('1W')
            expect(fallback.locator('#sample-table-caption')).to_contain_text('HKD')
            expect(fallback.locator('#sample-table-caption')).to_contain_text('weekly')
            expect(fallback.locator('#sample-rows tr')).to_have_count(8)
            expect(fallback.locator('#sample-disclosure')).to_be_visible()
            fallback.screenshot(path=str(ARTIFACTS / 'chart-fallback.png'), full_page=True)
            fallback.unroute('**/vela.global.min.js')
            fallback.reload()
            ready(fallback, fallback.locator('#sample-market').input_value(), fallback.locator('#sample-interval').input_value())
            assert_painted(fallback, 'reload after missing bundle')
            done('Blocked Vela keeps a usable labeled fallback table and reload restores the chart')

            assert not csp_violations, f'CSP violations: {csp_violations}'
            assert not external_requests, f'External network requests: {external_requests}'
            assert not any(re.search(r'/data\.json(?:\?|$)', item) for item in requests), requests
            assert not errors, f'Uncaught browser errors: {errors}'
            done('No external requests, market snapshot fetches, CSP violations or uncaught JavaScript errors')
            browser.close()
            browser = None
    except Exception as error:
        failure = f'{type(error).__name__}: {error}'
        if page is not None and not page.is_closed():
            try:
                page.screenshot(path=str(ARTIFACTS / 'chart-failure.png'), full_page=True, timeout=5000)
            except Exception:
                pass
        raise
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if server is not None:
            server.shutdown()
            server.server_close()
        report = {'status': 'failed' if failure else 'passed', 'passed': len(passed),
                  'checks': passed, 'failure': failure, 'browser_errors': errors,
                  'console_errors': console_errors, 'external_requests': external_requests,
                  'csp_violations': csp_violations,
                  'paint_checks': paints}
        (ARTIFACTS / 'chart-browser-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a', encoding='utf-8') as handle:
                handle.write('## Chart Lab browser checks\n\n')
                handle.write('\n'.join(f'- PASS: {item}' for item in passed) + '\n')
                if failure:
                    handle.write(f'\nFAILED: {failure}\n')
        print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
