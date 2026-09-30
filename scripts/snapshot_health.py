"""Decide whether scheduled recovery is needed using the actual NYSE calendar.

Missing optional sources request another bounded scheduled attempt. They never
turn a current, validated core snapshot into invented or forward-filled data.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

import requests

from generate_data import WATCHLIST
from scripts.theme_data import THEMES
from scripts.yahoo_history import completed_sessions


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def inspect_snapshot(payload, asof=None):
    """Return core validity and reasons to refresh; timestamps alone are insufficient."""
    dates = completed_sessions(asof)
    expected = str(dates[-1].date())
    reasons = []
    if not isinstance(payload, dict) or payload.get('market_date') != expected:
        return False, [f'Expected market session {expected}; snapshot is absent or stale.']
    validation = payload.get('history_validation', {})
    if (not isinstance(validation, dict)
            or validation.get('expected_market_date') != expected
            or validation.get('missing_required_observations') != 0):
        return False, ['Core history validation is absent or inconsistent.']
    rows = []
    for key in ('indices', 'sectors'):
        group = payload.get(key)
        if not isinstance(group, list) or not all(isinstance(row, dict) for row in group):
            return False, [f'Invalid core group: {key}.']
        rows.extend(group)
    symbols = [row.get('ticker') for row in rows]
    if len(symbols) != len(WATCHLIST) or set(symbols) != set(WATCHLIST):
        return False, ['Core ticker coverage is incomplete or duplicated.']
    for row in rows:
        if (row.get('market_date') != expected or row.get('rvolLookback') != 20
                or not all(_finite(row.get(k)) for k in ('price', 'return', 'alpha', 'rvol'))
                or row['price'] <= 0 or row['rvol'] < 0
                or type(row.get('volume')) is not int or row['volume'] < 0):
            return False, [f"Invalid core price/volume/date for {row.get('ticker')}."]
        if (row.get('darkPoolStatus') != 'available' or row.get('darkPoolDate') != expected
                or row.get('darkPoolMetric') != 'off_exchange_day_pct'
                or not _finite(row.get('darkPool')) or not 0 <= row['darkPool'] <= 100):
            reasons.append(f"{row['ticker']}: same-session off-exchange source still missing.")
    groups = payload.get('groups')
    if (payload.get('asof') != expected or not isinstance(groups, list)
            or not all(isinstance(group, dict) for group in groups)
            or len(groups) != len(THEMES)
            or {group.get('id') for group in groups} != set(THEMES)):
        reasons.append('AI/BTC theme coverage is absent or inconsistent.')
    else:
        for group in groups:
            required = THEMES[group['id']]['etfs'] + THEMES[group['id']]['stocks']
            instruments = group.get('instruments')
            if (not isinstance(instruments, list)
                    or not all(isinstance(row, dict) for row in instruments)
                    or len(instruments) != len(required)
                    or {row.get('ticker') for row in instruments} != set(required)):
                reasons.append(f"{group['id']}: incomplete instrument coverage.")
                continue
            for row in instruments:
                metrics = (row.get('returns'), row.get('vs_spy'))
                if (row.get('date') != expected or row.get('status') != 'available'
                        or not _finite(row.get('price')) or row['price'] <= 0
                        or not _finite(row.get('rvol'))
                        or not all(isinstance(m, dict) and all(_finite(m.get(p))
                                   for p in ('1', '5', '20')) for m in metrics)):
                    reasons.append(f"{row.get('ticker')}: complete theme history still missing.")
    return True, reasons


def public_snapshot_matches(payload, url):
    try:
        response = requests.get(url.rstrip('/') + '/data.json',
                                params={'health': payload.get('last_updated')}, timeout=15)
        response.raise_for_status()
        return response.json() == payload
    except (requests.RequestException, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--github-output', action='store_true')
    parser.add_argument('--require-current', action='store_true')
    parser.add_argument('--path', type=Path, default=Path('docs/data.json'))
    args = parser.parse_args()
    try:
        payload = json.loads(args.path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        payload = None
    current, reasons = inspect_snapshot(payload)
    if args.require_current and not current:
        raise SystemExit('Refusing to publish: ' + ' '.join(reasons))
    scheduled = os.environ.get('GITHUB_EVENT_NAME') == 'schedule'
    refresh = not current or bool(reasons) or not scheduled
    if args.github_output and not refresh:
        url = os.environ.get('PAGES_URL')
        if not url or not public_snapshot_matches(payload, url):
            reasons.append('Public Pages does not serve the current source snapshot.')
            refresh = True
    message = f"Core current={current}; refresh={refresh}. " + (
        ' '.join(reasons) if reasons else 'All required source observations are current.'
    )
    print(message, flush=True)
    if args.github_output:
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write(f'refresh={str(refresh).lower()}\n')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as output:
            output.write('## Snapshot health\n' + message + '\n')


if __name__ == '__main__':
    main()
