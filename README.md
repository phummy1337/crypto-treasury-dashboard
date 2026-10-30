# TreasuryTracker.NET — Strategy (MSTR/STRC), Strive (ASST/SATA) and DeFi Development (DFDV/CHAD)

A single-file dashboard comparing digital-asset treasury companies and their preferred
equity (digital credit) against the asset each one holds: BTC for Strategy and Strive,
SOL for DeFi Development.

## Files
- **index.html** — the dashboard: HTML, CSS and JS in one file, apart from the calculator's engine.
- **calc-engine.js** — the Implied Price Calculator's engine, which index.html loads.
- **tests/** — `node --test tests/*.test.js` checks that engine. No dependencies; CI runs it on changes.
- **data.json** — the numbers the dashboard reads. Edit by hand or via the scraper.
- **refresh_data.py** — optional updater that repopulates `data.json`.

## Run it
Because the page fetches `data.json`, open it through a tiny web server (not file://):

```bash
cd crypto-treasury-dashboard
python3 -m http.server 4173
# then open http://localhost:4173
```

(Open `index.html` directly and it still works — it just falls back to the
values embedded in the file instead of `data.json`.)

## Sections
1. **Accumulation & Holdings** — BTC held, % of 21M supply, YTD adds, pace/day & /week
2. **Cost & Efficiency** — avg cost basis, unrealized P/L (recomputes at the live BTC price)
3. **Per-Share & Yield** — sats/share (basic & diluted), BTC Yield YTD/QTD
4. **CEBE** — after-senior sats/share, claims %, sats per $100 invested
5. **Valuation & Premium** — mNAV, CEBE mNAV
6. **Capital Structure** — *fully live model from your Excel.* Blue cells are editable;
   every leverage/coverage ratio recomputes instantly. Crypto NAV uses the live BTC price.

The BTC price (top right) is fetched live from CoinGecko in the browser.

## Updating the data (automation)
```bash
pip3 install certifi requests          # certifi fixes macOS SSL; requests optional
python3 refresh_data.py --dry-run      # preview
python3 refresh_data.py                # write data.json
```

What's wired today:
- **Live BTC price + circulating supply** → CoinGecko (works out of the box).
- **Holdings / per-share / CEBE** → stubbed. cebetracker.io, bitcointreasuries.net,
  treasury.strive.com and strategy.com have no clean public API and block browser
  scraping, so those fetchers are isolated `try/except` stubs that keep existing
  values until you wire a source. Sections marked **SEED DATA** are these fields.
  Edit `data.json` directly in the meantime.

### Schedule it (optional, hands-off)
Refresh every morning at 7am via cron:
```
0 7 * * * cd /Users/petehumiston/crypto-treasury-dashboard && /usr/bin/python3 refresh_data.py
```

## Notes
- Not investment advice. Notional/face values used for the ratios — confirm against
  each issuer's latest filing.
- "▲" next to a value marks the more favorable side for common holders.

## Restoring SATS
SATS, Strive's junior 15% preferred ($700M, listed 2026-10-20), is hidden from the page
as an instrument, but its claim still counts.
- **Hidden:** its header tile, the SATA/SATS switches on the Daily Close and Short Interest
  cards, its calculator button, its lines in the Company Actions log, and its name in the
  masthead, the meta and social-card text, the grid headings, the sources, the ATM note,
  the Preferred Price definition and the calculator's notes.
- **Still counted:** ASST's preferred notional, annual obligations and mNAV history include
  its $700M at 15%. The Preferred by Series hover names it, since it reconciles that total.
- **Still collected:** `refresh_data.py` keeps writing ASST's `pref2` (SATS's quote and
  terms) and `pref2History`, `pref2ShortInterest` from its first FINRA settlement, and
  SATS's filings in `actions`. The quotes worker still carries SATS.

The surfaces came in with d377bd6 (tile, masthead, meta), bbb5330 (switches), a7d5d27
(calculator button), 248bb13 (grid headings, the tile's day change, sources), f93f796 (the
switch as the heading, PNG titles) and 4567fe5 (og-card-v3.jpg), with smaller mentions in
4e8bcba, f92ff43, 947b5ce and 50a4b81. `og-card-v3.jpg` is the social card with SATS on
it; the page now points at `og-card-v4.jpg`.

Two commits hid it, one for the page and one for the Company Actions log, and both
subjects start "Hide SATS". Reverting them, newest first, brings it all back (later edits
to the same lines may need merging by hand):

```bash
git revert --no-edit $(git log --format=%h --grep='^Hide SATS')
```
