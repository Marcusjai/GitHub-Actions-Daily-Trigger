# Research Lab — static-site integration

Entry point: `docs/research.html`, linked from the existing `docs/index.html`.
The existing overview, scraper, issuer calculations, published JSON schema,
recovery safeguards and scheduled run times are unchanged.

## Implemented

- AI and Bitcoin stock-basket radar for 1, 5 and 20 trading days versus SPY.
- Equal-weight returns, explicit coverage, relative-return breadth and the
  highest relative-return contributor among available stocks.
- Fixed 5D/20D comparison plot; only complete, matched baskets are plotted.
- Curated exposure map with ticker drill-down and separate ETF/ETP proxies.
- Search, theme/type filtering, sorting with missing values always last, dated
  stock details, source/recovery disclosure and explicit risk-check rules.
- Deterministic snapshot brief with copy and plain-text export.
- Keyboard-accessible tabs, mobile layouts and refresh error recovery.

## Data rules

All observations come from the existing `docs/data.json`. No substitute market
values, sample data, holdings, secrets, paid APIs, backend or model service were
added. Synthetic fixtures exist only inside tests.

Stock baskets use only the nine configured stocks (AI: five; BTC: four), not
ETF/ETP proxies. A period mean uses the same available members for the absolute
and relative return; missing members remain in the coverage denominator.
Relative return is a difference in percentage points, not risk-adjusted alpha,
net money flow, or a backtested strategy return. The comparison windows overlap
and do not establish a historical transition or trend duration. Both matrix
axes require the same complete constituent set. Partial baskets are not ranked
as whole-market evidence.

The current snapshot has no 60-session history or candle series. MA60, trend
persistence, live AI chat, news aggregation, portfolio inspection and automated
trading are NOT implemented. The daily review is rules-based, not an LLM report
or a historical change log. Existing ARTY/IBIT issuance estimates remain on the
overview with their original source and completeness requirements.

## Maintenance and tests

`docs/research.js` is dependency-free and exports pure functions for Node tests.
Run `node --test tests/*.test.cjs`; the existing Python tests remain unchanged.
The workflow now checks all dashboard JavaScript and both Node test suites.
The research tests cover malformed dates, stale data, missing observations,
duplicate records, stock/ETF separation, coverage, sorting, numeric edge cases,
escaping, export semantics and compatibility with the published snapshot.

To extend the tracked universe, update both `scripts/theme_data.py` and the
explicit `SPEC` configuration in `docs/research.js`; add matching tests. Adding
an exposure label does not itself acquire market data or prove a company's
revenue exposure. No new dependencies are required for the current module.

Product/workflow inspiration: https://github.com/jundizhou/easy-stock.
This is an independently written adaptation, not a bundled copy of that
application's source code, assets, AI runtime or market-data adapters.
