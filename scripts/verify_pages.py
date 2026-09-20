"""Confirm the public Pages snapshot equals the file just committed.

Public requests deliberately do not carry a GitHub token.
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
import requests


def verify(url, expected, attempts=30, delay=10):
    if urlsplit(url).scheme != 'https':
        raise ValueError('Expected an HTTPS GitHub Pages URL')
    endpoint = url.rstrip('/') + '/data.json'
    for attempt in range(attempts):
        try:
            response = requests.get(endpoint, params={'verify': time.time_ns()}, timeout=15)
            response.raise_for_status()
            actual = response.json()
            if actual == expected:
                message = (f"Published snapshot verified: market_date={actual['market_date']}; "
                           f"generated={actual['last_updated']}; "
                           f"off-exchange missing={len(actual.get('missing_off_exchange', []))}/14")
                print(message, flush=True)
                summary = os.environ.get('GITHUB_STEP_SUMMARY')
                if summary:
                    with open(summary, 'a', encoding='utf-8') as f:
                        f.write('## Public site verification\n' + message + '\n')
                return
            print(f'Attempt {attempt+1}: Pages still serves a different snapshot.', flush=True)
        except (requests.RequestException, ValueError) as exc:
            print(f'Attempt {attempt+1}: public snapshot not available ({exc}).', flush=True)
        if attempt+1 < attempts:
            time.sleep(delay)
    raise RuntimeError('Public Pages did not serve the committed snapshot before the verification timeout')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python scripts/verify_pages.py HTTPS_PAGES_URL')
    verify(sys.argv[1], json.loads(Path('docs/data.json').read_text(encoding='utf-8')))
