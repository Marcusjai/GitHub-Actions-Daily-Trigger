"""Exercise the Research Lab in Chromium without touching production data.

Run manually with: pip install playwright==1.55.0 && playwright install chromium
Then: python scripts/smoke_research_browser.py
Only the dedicated browser workflow installs this test-only dependency.
"""
from __future__ import annotations

import copy
import functools
import json
import os
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'test-artifacts'
ARTIFACTS.mkdir(exist_ok=True)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main() -> None:
    raw = json.loads((ROOT / 'docs/data.json').read_text(encoding='utf-8'))
    handler = functools.partial(QuietHandler, directory=str(ROOT / 'docs'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/research.html'
    passed = []
    errors = []

    def done(message):
        passed.append(message)
        print(f'PASS: {message}', flush=True)

    def no_overflow(page, label):
        dimensions = page.evaluate('''() => ({
            viewport: document.documentElement.clientWidth,
            content: document.documentElement.scrollWidth
        })''')
        assert dimensions['content'] <= dimensions['viewport'] + 1, (label, dimensions)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            context.grant_permissions(['clipboard-read', 'clipboard-write'])
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            expect(page.locator('#research-content')).to_be_visible()
            expect(page.locator('#snapshot-date')).to_have_text(raw['market_date'])
            expect(page.locator('#radar-cards .theme-card')).to_have_count(2)
            assert page.locator('#research-app').get_attribute('aria-busy') == 'false'
            no_overflow(page, 'desktop radar')
            page.screenshot(path=str(ARTIFACTS / 'radar-desktop.png'), full_page=True)
            done('Published snapshot loads; two theme baskets render without desktop overflow')

            page.locator('[data-period="20"]').click()
            expect(page.locator('[data-period="20"]')).to_have_attribute('aria-pressed', 'true')
            expect(page.locator('#radar-cards')).to_contain_text('20 trading day(s)')
            page.locator('[data-period="1"]').click()
            expect(page.locator('#radar-cards')).to_contain_text('1 trading day(s)')
            page.locator('[data-period="5"]').click()
            done('All 1D/5D/20D period controls update the view')

            page.locator('#tab-radar').focus()
            page.keyboard.press('ArrowRight')
            expect(page.locator('#panel-map')).to_be_visible()
            expect(page.locator('#tab-map')).to_be_focused()
            expect(page.locator('#theme-map .node')).to_have_count(5)
            page.locator('#theme-map [data-stock="NVDA"]').click()
            expect(page.locator('#panel-stocks')).to_be_visible()
            expect(page.locator('#stock-detail h2')).to_contain_text('NVDA')
            expect(page.locator('#stock-detail')).to_be_focused()
            assert page.url.endswith('#stock-NVDA')
            done('Keyboard tab navigation and exposure-map stock drill-down work')

            expect(page.locator('#stock-rows tr')).to_have_count(15)
            page.locator('#theme-filter').select_option('AI')
            page.locator('#type-filter').select_option('stock')
            expect(page.locator('#stock-rows tr')).to_have_count(5)
            page.locator('#stock-search').fill('NVDA')
            expect(page.locator('#stock-rows tr')).to_have_count(1)
            page.locator('#stock-search').fill('no such ticker')
            expect(page.locator('#stock-rows')).to_contain_text('No matching tracked instruments')
            page.locator('#stock-search').fill('')
            page.locator('[data-sort="ticker"]').click()
            page.locator('[data-sort="ticker"]').click()
            expect(page.locator('#stock-rows tr').first).to_contain_text('AMD')
            page.locator('#theme-filter').select_option('BTC')
            expect(page.locator('#stock-rows tr')).to_have_count(4)
            done('Search, no-result state, stock/ETF filters and ticker sorting work')

            page.locator('#tab-review').click()
            expect(page.locator('#review-text')).to_contain_text(raw['market_date'])
            expect(page.locator('#review-text')).to_contain_text('not an AI forecast')
            page.locator('#copy-review').click()
            expect(page.locator('#copy-status')).to_have_text('Brief copied.')
            clipboard = page.evaluate('navigator.clipboard.readText()')
            assert raw['market_date'] in clipboard
            with page.expect_download() as info:
                page.locator('#download-review').click()
            download = info.value
            assert download.suggested_filename == f'research-{raw["market_date"]}-5D.txt'
            downloaded = Path(download.path()).read_text(encoding='utf-8')
            assert downloaded == page.locator('#review-text').inner_text()
            done('Dated brief copies and downloads exactly the displayed text')

            old_date = page.locator('#snapshot-date').inner_text()
            page.route('**/data.json', lambda route: route.fulfill(status=503, body='unavailable'))
            page.locator('#reload').click()
            expect(page.locator('#load-status')).to_contain_text('Showing the previously loaded snapshot')
            expect(page.locator('#snapshot-date')).to_have_text(old_date)
            expect(page.locator('#reload')).to_be_enabled()
            page.unroute('**/data.json')
            done('Failed refresh preserves the previous snapshot and permits retry')

            page.locator('#reload').click()
            expect(page.locator('#load-status')).to_contain_text('Published snapshot loaded')
            page.locator('#tab-radar').click()
            page.locator('[data-theme="AI"]').click()
            expect(page.locator('#theme-filter')).to_have_value('AI')
            expect(page.locator('#type-filter')).to_have_value('stock')
            expect(page.locator('#stock-rows tr')).to_have_count(5)
            done('Theme exploration resets stale filters and opens the correct basket')

            for width in (390, 320):
                page.set_viewport_size({'width': width, 'height': 844})
                for tab in ('radar', 'map', 'stocks', 'review'):
                    page.locator(f'#tab-{tab}').click()
                    expect(page.locator(f'#panel-{tab}')).to_be_visible()
                    no_overflow(page, f'{width}px {tab}')
                page.locator('#tab-radar').click()
                page.screenshot(path=str(ARTIFACTS / f'radar-mobile-{width}.png'), full_page=True)
            done('All four panels fit 390px and 320px mobile viewports')

            fresh = context.new_page()
            fresh.on('pageerror', lambda error: errors.append(str(error)))
            fresh.goto(url + '#stock-MSTR')
            expect(fresh.locator('#stock-detail h2')).to_contain_text('MSTR')
            expect(fresh.locator('#panel-stocks')).to_be_visible()
            done('Direct stock links open the expected instrument after loading')

            invalid = context.new_page()
            invalid.route('**/data.json', lambda route: route.fulfill(status=200, content_type='application/json', body='{}'))
            invalid.goto(url)
            expect(invalid.locator('#load-status')).to_contain_text('Theme snapshot is missing')
            expect(invalid.locator('#research-content')).to_be_hidden()
            done('Malformed first-load snapshot shows a recoverable error, not fake numbers')

            stale_raw = copy.deepcopy(raw)
            for group in stale_raw['groups']:
                for item in group['instruments']:
                    if item['ticker'] == 'NVDA':
                        item['date'] = '2000-01-01'
            partial = context.new_page()
            partial.route('**/data.json', lambda route: route.fulfill(status=200, content_type='application/json', body=json.dumps(stale_raw)))
            partial.goto(url)
            expect(partial.locator('#radar-cards .theme-card').first).to_contain_text('4/5 available')
            expect(partial.locator('#rotation-matrix .point-AI')).to_have_count(0)
            done('A stale stock reduces coverage and removes the incomplete basket from the plot')

            assert not errors, f'Uncaught browser errors: {errors}'
            done('No uncaught JavaScript errors in the tested interactions')
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        report = {'passed': len(passed), 'checks': passed, 'browser_errors': errors}
        (ARTIFACTS / 'browser-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a', encoding='utf-8') as handle:
                handle.write('## Research Lab browser checks\n\n')
                handle.write('\n'.join(f'- PASS: {item}' for item in passed) + '\n')
        print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
