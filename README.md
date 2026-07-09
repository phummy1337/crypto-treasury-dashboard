# BTC Treasury Tracker — Strategy (MSTR/STRC) vs Strive (ASST/SATA)

A single-file dashboard comparing both companies and their preferred equity relative to Bitcoin.

## Files
- **index.html** — the dashboard. Self-contained (HTML/CSS/JS in one file).
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
