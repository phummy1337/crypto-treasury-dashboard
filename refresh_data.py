#!/usr/bin/env python3
"""
refresh_data.py — repopulate data.json for the BTC Treasury Tracker.

The dashboard (index.html) reads ./data.json on load. Run this script (or
schedule it) to refresh the numbers without editing the HTML.

  python3 refresh_data.py            # update in place
  python3 refresh_data.py --dry-run  # print what would change, write nothing

WHAT WORKS OUT OF THE BOX
  - Live BTC price + circulating supply  -> CoinGecko public API (no key)

  - Stock price, day change, 52-week range, 1-year price history, beta-to-BTC
    -> Yahoo Finance chart API (no key), server-side. See fetch_equity().
    (Note: Stooq's CSV endpoint is now behind a JS proof-of-work wall and no
    longer returns plain CSV, so Yahoo is used instead.)

  - BTC holdings + true weekly purchases  -> SEC EDGAR 8-Ks (no key). See
    fetch_holdings(): parses each issuer's weekly purchase 8-K (Strategy's
    "BTC Update" table, Strive's "Bitcoin held" table), rebuilds data["weekly"]
    and refreshes current holdings / % of supply.

WHAT NEEDS WIRING (per-source TODOs below)
  per-share / yield (strategy.com, treasury.strive.com) and CEBE / claims% /
  mNAV-history (cebetracker.io) have no clean public API and are fully
  JavaScript-rendered, so a plain HTTP fetch can't read them. Each fetcher below
  is isolated in try/except: if it can't get a value it leaves the existing one
  untouched, so a partial failure never blanks the dashboard. Their CURRENT
  values in data.json are real reported figures; only the month-by-month path is
  modeled. Edit data.json directly to refresh them, or wire a headless browser.

CHART HISTORY
  Two time-series blocks back the charts, both seeded with illustrative values
  ("illustrative": true) until wired:
    data["weekly"]  -> accumulation chart. TRUE WEEKLY resolution: per company
        {dates, acquired[], holdings[]}. This is the most automatable series:
        Strategy and Strive disclose purchases in 8-Ks ~weekly, and SEC EDGAR
        has a real JSON API (data.sec.gov / efts.sec.gov full-text search) the
        Python side can hit. Append one point per new 8-K.
    data["history"] -> monthly arrays per company: holdings, satsPerShare,
        btcYieldYtd, cebeSatsPerShare, claimsPct, mnav, cebeMnav.
  When a block becomes real, append a new dated point to each of its arrays and
  set that block's "illustrative" = false to drop the orange caption.

Dependencies: requests  (pip3 install requests)
  Optional for HTML scraping: beautifulsoup4  (pip3 install beautifulsoup4)
"""

import json
import re
import sys
import ssl
import time
import datetime
import urllib.request
import urllib.error
from pathlib import Path

DATA_PATH = Path(__file__).with_name("data.json")
DAILY_POINTS = 260          # trailing daily closes kept for the price chart (~1 year)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# macOS system Python often ships without a usable CA bundle, which breaks
# HTTPS with CERTIFICATE_VERIFY_FAILED. Prefer certifi's bundle if installed
# (pip3 install certifi); otherwise fall back to the default context.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()


# --------------------------------------------------------------------------- #
# small fetch helper (stdlib only, so the script runs with no pip installs)
# --------------------------------------------------------------------------- #
def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return json.loads(r.read().decode())


def log(msg):
    print(f"  {msg}")


# --------------------------------------------------------------------------- #
# WORKING: BTC price + supply (CoinGecko)
# --------------------------------------------------------------------------- #
def fetch_btc_market(data):
    try:
        j = get_json(
            "https://api.coingecko.com/api/v3/coins/bitcoin"
            "?localization=false&tickers=false&market_data=true"
            "&community_data=false&developer_data=false"
        )
        price = j["market_data"]["current_price"]["usd"]
        circ = j["market_data"]["circulating_supply"]
        data["btcPriceUsd"] = round(price, 2)
        data["priceAsOf"] = datetime.date.today().isoformat()
        if circ:
            data["btcCirculating"] = int(circ)
        log(f"BTC price = ${price:,.0f}  (circulating {circ:,.0f})")
        # recompute % of 21M supply from current holdings
        for t, c in data["companies"].items():
            c["pctSupply"] = round(c["holdings"] / data["btcSupply"] * 100, 4)
        return True
    except Exception as e:
        log(f"[skip] CoinGecko failed: {e}")
        return False


# --------------------------------------------------------------------------- #
# PRIMARY SOURCE: strategytracker.com data API (powers strategy.com /
# treasury.strive.com). Current metrics + real daily history for both names.
# --------------------------------------------------------------------------- #
TRACKER_BASE = "https://data.strategytracker.com/"
# STRC notional ($mm) before the tracker's change log begins (2026-03-09), built from
# SEC 8-Ks: IPO 28,011,111 sh × $100 stated (closed 2025-07-29), then the weekly ATM
# "available for issuance" column (program $4,200mm). Validated: last step + the week
# ending 2026-03-08 ($377.6mm sold, $3,158.0mm available) = 3,843.1 ≈ tracker's 3,843.
# Historical filings never change, so these are constants rather than re-parsed.
STRC_BACKFILL = [
    ("2025-07-29", 2801.1), ("2025-11-09", 2827.3), ("2025-11-16", 2958.7),
    ("2026-01-11", 3078.0), ("2026-01-19", 3372.7), ("2026-01-25", 3379.7),
    ("2026-02-16", 3458.3), ("2026-03-01", 3465.4),
]

# Notional steps ($mm) for MSTR's non-STRC preferred series, reconstructed from the
# weekly 8-K ATM tables ("Available for Issuance" deltas, anchored to current notionals;
# STRF/STRK/STRD have sold nothing since Dec '25 / Jan '26, so the anchors are exact).
# Cross-validated: total preferred at 2026-03-31 computes to ~$10.0B = the Q1-26 10-Q figure.
MSTR_PREF_STEPS = {
    "STRF": [
        ("2025-08-10", 1035.9), ("2025-08-17", 1054.9), ("2025-08-24", 1081.5), ("2025-09-01", 1108.0),
        ("2025-09-07", 1119.7), ("2025-09-14", 1153.7), ("2025-09-21", 1173.2), ("2025-09-28", 1184.5),
        ("2025-10-19", 1215.5), ("2025-10-26", 1234.9), ("2025-11-02", 1243.3), ("2025-11-09", 1261.6),
        ("2025-11-16", 1266.0), ("2025-12-14", 1284.0),
    ],
    "STRK": [
        ("2025-08-10", 1283.4), ("2025-08-17", 1302.7), ("2025-08-24", 1323.2), ("2025-09-01", 1342.3),
        ("2025-09-07", 1347.5), ("2025-09-14", 1364.8), ("2025-10-19", 1371.5), ("2025-10-26", 1388.6),
        ("2025-11-02", 1393.0), ("2025-11-09", 1397.4), ("2025-11-16", 1397.9), ("2025-12-14", 1398.6),
        ("2026-01-19", 1402.0),
    ],
    "STRD": [
        ("2025-08-10", 1234.8), ("2025-08-17", 1246.9), ("2025-08-24", 1247.0), ("2025-09-01", 1248.0),
        ("2025-09-14", 1265.0), ("2025-09-28", 1265.4), ("2025-10-19", 1273.7), ("2025-10-26", 1280.7),
        ("2025-11-02", 1283.0), ("2025-11-09", 1284.0), ("2025-12-07", 1319.0), ("2025-12-14", 1402.0),
    ],
    "STRE": [("2025-11-17", 899.0)],   # EUR IPO mid-Nov 2025, no ATM — constant since issue
}
# MSTR quarterly cash (SEC XBRL CashAndCashEquivalentsAtCarryingValue) and convert
# principal (all six outstanding notes were issued by 2025-02-21; none redeemed since).
MSTR_CASH_STEPS = [("2025-06-30", 50.1), ("2025-09-30", 54.3),
                   ("2025-12-31", 2301.5), ("2026-03-31", 2207.2)]
MSTR_DEBT_STEPS = [("2025-02-21", 8213.75)]
MNAV_START = {"MSTR": "2025-10-01", "ASST": "2026-01-01"}   # chart windows


def _step(steps, iso):
    """Latest step value effective on or before iso date (steps sorted ascending)."""
    v = None
    for d, x in steps:
        if d <= iso:
            v = x
        else:
            break
    return v


# STRE is EUR-denominated: 7,750,000 shares × €100 stated amount (Nov 13, 2025
# offering, per 8-K) = €775mm, converted at the live ECB EURUSD rate each refresh.
# The tracker carries a stale fixed conversion ($899mm), so we override it.
STRE_EUR_NOTIONAL = 775.0

# Insider super-voting Class B shares (millions) — never part of the float.
# From the 2026-03-31 10-Q covers (iXBRL dei:EntityCommonStockSharesOutstanding):
# MSTR 19,640,250 (unchanged for years) · ASST 9,870,636. Update on a B->A conversion.
CLASS_B_SHARES_M = {"MSTR": 19.640250, "ASST": 9.870636}

# Nasdaq reports historical short interest in as-traded (unadjusted) shares.
# ASST ran a 1-for-20 reverse split effective 2026-02-06 (8-K dp240990), so
# settlements before that date are divided by 20 to match today's share count.
SI_SPLITS = {"ASST": [("2026-02-06", 20)]}

# daily shares-outstanding history (millions) per ticker, filled by
# fetch_strategytracker (mcap / price) and used for per-date float below
_SHARES_HIST = {}
# daily share volume per ticker: preferreds from the tracker's price log,
# commons from Yahoo — used for trailing-20-day days-to-cover
_VOL_HIST = {}

PREF_LABEL = {"STRC": "Stretch · {r}% var", "STRK": "Strike · {r}%", "STRF": "Strife · {r}%",
              "STRD": "Stride · {r}%", "STRE": "STRE · {r}% (EUR)", "SATA": "Strive pref · {r}%"}

def _iso_lbl(iso, fmt):
    y, m, d = iso.split("-")
    return datetime.date(int(y), int(m), int(d)).strftime(fmt)


def fetch_strategytracker(data):
    """Refresh current metrics + real history for MSTR/ASST from strategytracker."""
    try:
        idx = get_json(TRACKER_BASE + "latest.json")
        full = get_json(TRACKER_BASE + idx["files"]["full"])
    except Exception as e:
        log(f"[skip] strategytracker failed: {e} — keeping existing values")
        return
    comps = full.get("companies", {})
    try:    # live EURUSD (ECB) for the EUR-denominated STRE notional
        data["eurUsd"] = round(get_json("https://api.frankfurter.dev/v1/latest?base=EUR&symbols=USD")["rates"]["USD"], 4)
    except Exception:
        log("[skip] EURUSD fetch failed — keeping previous rate")
    hist = {}
    for tk in ("MSTR", "ASST"):
        c = comps.get(tk)
        if not c:
            continue
        pm, hd = c["processedMetrics"], c["historicalData"]
        co = data["companies"].get(tk)
        if not co:
            continue
        co["holdings"]      = int(round(pm["latestBtcBalance"]))
        co["avgCost"]       = round(pm["avgCostPerBtc"])
        co["stockPrice"]    = round(pm["stockPrice"], 2)
        co["dayChangePct"]  = round(pm["stockPriceDelta"]["percent"], 2)
        co["sharesOutstanding"] = round(pm["latestTotalShares"] / 1e6, 2)
        co["floatSharesM"] = round(co["sharesOutstanding"] - CLASS_B_SHARES_M.get(tk, 0), 2)
        # daily shares outstanding (split-adjusted, millions) for per-date float
        _SHARES_HIST[tk] = sorted(
            (dt, mc / px / 1e6)
            for dt, mc, px in zip(hd["dates"], hd["market_cap_basic"], hd["stock_prices"])
            if mc and px)
        co["dilutedShares"] = round(pm["latestDilutedShares"] / 1e6, 2)
        co["satsPerShareBasic"]   = round(pm["btcPerShare"] * 1e8)
        co["satsPerShareDiluted"] = round(pm["btcPerDilutedShare"] * 1e8)
        co["btcYieldYtd"]   = round(pm["btcYieldYtd"], 1)
        co["btcYieldQtd"]   = round(pm["btcYieldQuarterly"], 1)
        co["pctSupply"]     = round(co["holdings"] / data["btcSupply"] * 100, 4)
        co["navPremiumBasic"] = round(pm["navPremiumBasic"], 3)
        co["treasuryDate"]  = pm.get("latestTreasuryDate")
        # the tracker zeroes cash/debt when it values a name on market-cap basis
        # (useEv False) — only trust it when useEv is True; else keep filing values.
        if pm.get("latestUseEv"):
            co["cash"] = round(pm["latestCashBalance"] / 1e6)
        # preferred: live per-series notionals, prices, and the implied annual dividend
        ps = pm.get("preferredStocks") or []
        if ps:
            bd = []
            mkt = 0.0
            for p in ps:
                t = p["ticker"]
                notM = p.get("notionalMillions") or round((p.get("notionalUSD") or 0) / 1e6)
                if t == "STRE" and data.get("eurUsd"):
                    notM = round(STRE_EUR_NOTIONAL * data["eurUsd"])
                rate = p.get("dividendRate")
                lab = PREF_LABEL.get(t, "{r}%").format(r=("%g" % rate) if rate is not None else "?")
                bd.append([t, lab, round(notM)])
                # market value of the series: notional scaled by price/par
                # (par $100 for USD series; STRE is EUR-denominated with a €10 par)
                par = 10 if t == "STRE" else 100
                mkt += notM * (p.get("price") or par) / par
                if t == co.get("prefTicker"):
                    co["prefPrice"] = round(p["price"], 2)
                    co["prefChangePct"] = round(p.get("priceChangePercent") or 0, 2)
                    # daily preferred close + notional outstanding (step series from
                    # the tracker's change log; None before the first known change)
                    hp = p.get("historicalPrices") or []
                    chg = sorted((e for e in (p.get("history") or [])
                                  if e.get("effective_date") and e.get("notional_millions") is not None),
                                 key=lambda x: x["effective_date"])
                    if t == "STRC":     # splice in the SEC-filing backfill before the tracker log starts
                        first = chg[0]["effective_date"] if chg else "9999-99-99"
                        chg = [{"effective_date": d, "notional_millions": n}
                               for d, n in STRC_BACKFILL if d < first] + chg
                        co["strcNotionalSteps"] = [[e["effective_date"], e["notional_millions"]] for e in chg]
                    _VOL_HIST[t] = [(q["date"], q.get("volume") or 0) for q in hp]
                    dts, isod, px, no = [], [], [], []
                    for q in hp:
                        dts.append(_iso_lbl(q["date"], "%b %-d"))
                        isod.append(q["date"])
                        px.append(round(q["close"], 2))
                        n = None
                        for e in chg:
                            if e["effective_date"] <= q["date"]:
                                n = e["notional_millions"]
                            else:
                                break
                        no.append(round(n) if n is not None else None)
                    if px:
                        co["prefHistory"] = {"dates": dts, "iso": isod, "px": px, "notional": no}
            bd.sort(key=lambda x: -x[2])
            co["prefBreakdown"] = bd
            co["prefNotional"] = round(sum(x[2] for x in bd))
            co["prefMarket"] = round(mkt)
            corr = {x[0]: x[2] for x in bd}   # FX-corrected notionals
            pref_div = sum(corr.get(p["ticker"], 0) * (p.get("dividendRate") or 0) / 100 for p in ps)
            debt_int = sum(x["principal"] * x["coupon"] / 100 for x in (co.get("debtSchedule") or []))
            co["annualObligations"] = round(pref_div + debt_int)
        sp = [x for x in hd["stock_prices"][-365:] if x is not None]
        if sp:
            co["week52Low"], co["week52High"] = round(min(sp), 2), round(max(sp), 2)
        # BTC-per-share history (weekly downsample) for the per-share chart
        dts, bps = hd["dates"], hd["btc_per_share"]
        od, ov = [], []
        for i in range(0, len(dts), 5):
            if bps[i] is not None:
                od.append(_iso_lbl(dts[i], "%b '%y")); ov.append(round(bps[i] * 1e8))
        if bps and bps[-1] is not None:
            od.append(_iso_lbl(dts[-1], "%b '%y")); ov.append(round(bps[-1] * 1e8))
        co["bpsHistory"] = {"dates": od, "sats": ov}

        # beta to BTC: regression of trailing-1y daily stock returns on BTC returns
        try:
            sp = hd["stock_prices"][-253:]
            bp = hd["btc_prices"][-253:]
            rs, rb = [], []
            for i in range(1, min(len(sp), len(bp))):
                if sp[i] and sp[i-1] and bp[i] and bp[i-1]:
                    rs.append(sp[i]/sp[i-1] - 1)
                    rb.append(bp[i]/bp[i-1] - 1)
            if len(rb) > 60:
                mb = sum(rb)/len(rb); ms = sum(rs)/len(rs)
                cov = sum((rb[i]-mb)*(rs[i]-ms) for i in range(len(rb)))/len(rb)
                var = sum((x-mb)**2 for x in rb)/len(rb)
                if var > 0:
                    co["betaBtc"] = round(cov/var, 2)
        except Exception:
            pass

        # ---- daily EV mNAV history: (mcap + debt + pref notional − cash) / BTC NAV ----
        try:
            start = MNAV_START[tk]
            today = datetime.date.today().isoformat()
            if tk == "MSTR":
                # STRC: SEC backfill + tracker change log; other series: 8-K step
                # constants, extended live (append a step whenever the tracker's
                # current notional moves off the last known step; persisted in data.json)
                live = co.get("histStepsLive") or {}
                series = {}
                for p in ps:
                    t = p["ticker"]
                    lg = sorted(((e["effective_date"], e["notional_millions"])
                                 for e in (p.get("history") or [])
                                 if e.get("effective_date") and e.get("notional_millions") is not None))
                    if t == "STRC":
                        first = lg[0][0] if lg else "9999-99-99"
                        series[t] = [x for x in STRC_BACKFILL if x[0] < first] + lg
                        continue
                    cur = p.get("notionalMillions") or (p.get("notionalUSD") or 0) / 1e6
                    st = sorted(set(map(tuple, MSTR_PREF_STEPS.get(t, []) + [tuple(x) for x in live.get(t, [])] + lg)))
                    if st and cur and abs(st[-1][1] - cur) > 0.6:
                        st.append((today, round(cur, 1)))
                        live.setdefault(t, []).append([today, round(cur, 1)])
                    series[t] = st
                cash_st = sorted(set(map(tuple, MSTR_CASH_STEPS + [tuple(x) for x in live.get("cash", [])])))
                if abs(cash_st[-1][1] - co["cash"]) > 1.5:
                    cash_st.append((today, co["cash"])); live.setdefault("cash", []).append([today, co["cash"]])
                debt_st = sorted(set(map(tuple, MSTR_DEBT_STEPS + [tuple(x) for x in live.get("debt", [])])))
                if abs(debt_st[-1][1] - co["seniorDebt"]) > 1.5:
                    debt_st.append((today, co["seniorDebt"])); live.setdefault("debt", []).append([today, co["seniorDebt"]])
                if live:
                    co["histStepsLive"] = live
                pref_at = lambda d: sum(_step(s, d) or 0 for s in series.values())
                cash_at = lambda d, i: _step(cash_st, d) or 0
                debt_at = lambda d, i: _step(debt_st, d) or 0
            else:
                # ASST: the tracker carries daily cash/debt (useEv basis) + the SATA log
                sata = next((p for p in ps if p["ticker"] == "SATA"), None)
                sata_st = sorted(((e["effective_date"], e["notional_millions"])
                                  for e in ((sata or {}).get("history") or [])
                                  if e.get("effective_date") and e.get("notional_millions") is not None))
                cb, db = hd.get("cash_balance") or [], hd.get("debt") or []
                _ff = [0.0] * len(hd["dates"])          # forward-filled cash
                lastc = 0.0
                for i in range(len(hd["dates"])):
                    if i < len(cb) and cb[i]:
                        lastc = cb[i] / 1e6
                    _ff[i] = lastc
                pref_at = lambda d: _step(sata_st, d) or 0
                cash_at = lambda d, i: _ff[i]
                debt_at = lambda d, i: (db[i] or 0) / 1e6 if i < len(db) else 0
            dts_m, px_m, mnv, cbv = [], [], [], []
            for i, d in enumerate(hd["dates"]):
                if d < start:
                    continue
                mc, b, bp, sp = (hd["market_cap_basic"][i], hd["btc_balance"][i],
                                 hd["btc_prices"][i], hd["stock_prices"][i])
                if not (mc and b and bp and sp):
                    continue
                nav = b * bp / 1e6
                claims = debt_at(d, i) + pref_at(d) - cash_at(d, i)
                ev = mc / 1e6 + claims
                dts_m.append(_iso_lbl(d, "%b %-d"))
                px_m.append(round(sp, 2))
                mnv.append(round(ev / nav, 3))
                # CEBE mNAV: what the common pays per $ of BTC equity left after claims
                cebe_nav = nav - claims
                cbv.append(round(mc / 1e6 / cebe_nav, 3) if cebe_nav > 0 else None)
            if mnv:
                co["mnavHistory"] = {"dates": dts_m, "px": px_m, "mnav": mnv, "cebe": cbv}
                log(f"{tk} mNAV history: {len(mnv)} pts, latest {mnv[-1]}x (CEBE {cbv[-1]}x)")
        except Exception as e:
            log(f"[skip] mNAV history {tk}: {e} — keeping existing series")
        hist[tk] = hd
        log(f"{tk} via strategytracker: {co['holdings']:,} BTC, ${co['stockPrice']} "
            f"({co['dayChangePct']:+.2f}%), {co['satsPerShareBasic']:,} sats/sh, yld {co['btcYieldYtd']}%")
    # aligned daily stock-price history (trailing 1 year) for the combined price chart
    if "MSTR" in hist and "ASST" in hist:
        mh, ah = hist["MSTR"], hist["ASST"]
        adict = dict(zip(ah["dates"], ah["stock_prices"]))
        md, mp = mh["dates"][-365:], mh["stock_prices"][-365:]
        data["stockHistory"] = {
            "illustrative": False, "daily": True,
            "iso": md,
            "dates": [_iso_lbl(x, "%b %-d") for x in md],
            "MSTR": [round(v, 2) if v is not None else None for v in mp],
            "ASST": [round(adict.get(x), 2) if adict.get(x) is not None else None for x in md],
        }


# --------------------------------------------------------------------------- #
# FALLBACK: equity stats & 1-year price  (Yahoo Finance chart API)
# --------------------------------------------------------------------------- #
def _yahoo_chart(symbol, tries=6):
    """Fetch a Yahoo 1y daily chart, retrying across hosts on 429/5xx."""
    last = None
    for i in range(tries):
        host = "query1" if i % 2 == 0 else "query2"   # rotate hosts
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
               f"{symbol}?range=1y&interval=1d")
        try:
            return get_json(url)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503):
                time.sleep(2 ** i)                    # 1s, 2s, 4s, 8s backoff
                continue
            raise
    raise last


def _yahoo_daily(symbol):
    """Return ([(date, close), ...] ascending, meta dict) for a 1y daily chart."""
    res = _yahoo_chart(symbol)["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    series = [(datetime.datetime.utcfromtimestamp(t).date(), float(c))
              for t, c in zip(ts, closes) if c is not None]
    series.sort()
    return series, res.get("meta", {})


def _beta(stock, btc):
    """Beta of stock daily returns vs BTC daily returns over shared dates."""
    sd, bd = dict(stock), dict(btc)
    days = sorted(set(sd) & set(bd))
    sr, br = [], []
    for i in range(1, len(days)):
        p, q = days[i - 1], days[i]
        if sd[p] and bd[p]:
            sr.append(sd[q] / sd[p] - 1)
            br.append(bd[q] / bd[p] - 1)
    n = len(br)
    if n < 30:
        return None
    mb, ms = sum(br) / n, sum(sr) / n
    var = sum((x - mb) ** 2 for x in br) / n
    cov = sum((sr[i] - ms) * (br[i] - mb) for i in range(n)) / n
    return cov / var if var else None


def fetch_equity(data):
    """stockPrice, dayChangePct, week52High/Low, betaBtc, and stockHistory."""
    try:
        btc_series, _ = _yahoo_daily("BTC-USD")
    except Exception as e:
        log(f"[skip] equity: BTC history failed ({e}) — keeping existing values")
        return

    daily, ok = {}, []
    for tk in data["companies"]:
        try:
            series, meta = _yahoo_daily(tk)
            if len(series) < 2:
                raise ValueError("insufficient data")
            c = data["companies"][tk]
            price = meta.get("regularMarketPrice") or series[-1][1]
            c["stockPrice"] = round(price, 2)
            c["dayChangePct"] = round((series[-1][1] / series[-2][1] - 1) * 100, 2)
            if meta.get("fiftyTwoWeekHigh"):
                c["week52High"] = round(meta["fiftyTwoWeekHigh"], 2)
            if meta.get("fiftyTwoWeekLow"):
                c["week52Low"] = round(meta["fiftyTwoWeekLow"], 2)
            beta = _beta(series, btc_series)
            if beta is not None:
                c["betaBtc"] = round(beta, 2)
            daily[tk] = dict(series)                 # {date: close}
            daily[tk][series[-1][0]] = round(price, 2)  # last point = current price
            ok.append(tk)
            log(f"{tk}: ${c['stockPrice']:,.2f}  ({c['dayChangePct']:+.2f}%)  "
                f"52w {c['week52Low']}-{c['week52High']}  betaBTC {c.get('betaBtc')}")
        except Exception as e:
            log(f"[skip] equity {tk} failed: {e} — keeping existing values")

        # preferred (STRC / SATA) latest price + day change for the header boxes
        pref = data["companies"][tk].get("prefTicker")
        if pref:
            try:
                ps, pm = _yahoo_daily(pref)
                pprice = pm.get("regularMarketPrice") or ps[-1][1]
                data["companies"][tk]["prefPrice"] = round(pprice, 2)
                if len(ps) >= 2:
                    data["companies"][tk]["prefChangePct"] = round((ps[-1][1] / ps[-2][1] - 1) * 100, 2)
                log(f"  {pref}: ${pprice:,.2f} ({data['companies'][tk].get('prefChangePct')}%)")
            except Exception as e:
                log(f"  [skip] {pref} pref price failed: {e}")

    # rebuild the shared DAILY stockHistory from dates common to all companies
    if len(daily) == len(data["companies"]) and daily:
        common = sorted(set.intersection(*(set(m) for m in daily.values())))[-DAILY_POINTS:]
        if common:
            sh = data.setdefault("stockHistory", {})
            sh["illustrative"] = False
            sh["daily"] = True
            sh["dates"] = [d.strftime("%b %-d") for d in common]
            for tk, mp in daily.items():
                sh[tk] = [round(mp[d], 2) for d in common]
            log(f"stockHistory rebuilt: {len(common)} daily points for {', '.join(ok)}")
            log(f"stockHistory rebuilt: {len(common)} months for {', '.join(ok)}")


# --------------------------------------------------------------------------- #
# WORKING: holdings & weekly purchases  (SEC EDGAR 8-Ks)
# --------------------------------------------------------------------------- #
# SEC asks for a descriptive User-Agent with contact info (fair-access policy).
EDGAR_UA = {"User-Agent": "crypto-treasury-dashboard pete@defidevcorp.com"}
CIK = {"MSTR": "0001050446", "ASST": "0001920406"}


def _edgar_text(url):
    req = urllib.request.Request(url, headers=EDGAR_UA)
    html = urllib.request.urlopen(req, timeout=25, context=_SSL_CTX).read().decode("utf-8", "ignore")
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"&#\d+;|&nbsp;|&#160;", " ", t)
    return re.sub(r"\s+", " ", t)


def _edgar_json(url):
    req = urllib.request.Request(url, headers=EDGAR_UA)
    return json.loads(urllib.request.urlopen(req, timeout=25, context=_SSL_CTX).read())


def _pdate(s):
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y"):
        try:
            return datetime.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_flows(text):
    """Weekly cash flows from an MSTR 8-K: ATM net proceeds in, BTC spend out,
    BTC sale proceeds in (all $mm). BTC dollar amounts are derived as
    quantity × average price — the aggregate column switches between
    (in millions) and (in billions) across filings, the avg price never does."""
    raised = spent = sold = 0.0
    am = re.search(r"ATM (?:Program Summary|Updates?).{0,6000}?Total\s*\$\s*([\d,.]+)", text)
    if am:
        raised = float(am.group(1).replace(",", ""))
    # period purchase row: "<acquired> $<aggregate> $<avg price> <holdings> ..."
    tm = re.search(r"Aggregate BTC Holdings.*?([\d,]+)\s+\$\s*[\d,.]+\s+\$\s*([\d,]+)"
                   r"\s+[\d,]{5,}\s+\$\s*[\d,.]+\s+\$\s*[\d,]+", text)
    if tm:
        spent = int(tm.group(1).replace(",", "")) * int(tm.group(2).replace(",", "")) / 1e6
    for m in re.finditer(r"BTC Sold.{0,140}?([\d,]{3,})\s*(?:\(\d\))?\s*\$\s*[\d,.]+\s+\$\s*([\d,]+)", text):
        sold += int(m.group(1).replace(",", "")) * int(m.group(2).replace(",", "")) / 1e6
    return {"raised": raised, "btcSpent": spent, "btcSold": sold}


def _parse_mstr(text):
    """Strategy 8-K 'BTC Update' -> (start, end, acquired, holdings, avg_cost).

    Handles four observed shapes: a purchase-week table, a no-purchase-week
    table (acquired shown as '-'), a prose no-purchase statement, and the
    sale-week format first seen 2026-07-06 ("BTC Sold ... As of DATE
    Aggregate BTC Holdings N"). acquired is negative for sale weeks.
    avg_cost is the reported aggregate average purchase price when present.
    """
    if "BTC Update" not in text:
        return None
    # sale weeks (first seen 2026-07-06): use the LAST period block + the final
    # "As of" holdings figure; column headers carry footnote digits like "(2)",
    # so gaps are bounded non-greedy scans rather than [^0-9]*
    if "BTC Sold" in text:
        periods = list(re.finditer(r"During Period\s+(.+?)\s+to\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text))
        asofs = list(re.finditer(r"As of [A-Z][a-z]+ \d{1,2}, \d{4}\*?\s*Aggregate BTC Holdings"
                                 r".{0,120}?([\d,]{7,})\s+\$\s*[\d,.]+\s+\$\s*([\d,]+)", text))
        sold = [int(m.group(1).replace(",", "")) for m in
                re.finditer(r"BTC Sold.{0,140}?([\d,]{3,})\s*(?:\(\d\))?\s*\$\s*[\d,.]+\s+\$\s*[\d,]+", text)]
        if periods and asofs:
            p = periods[-1]
            h = int(asofs[-1].group(1).replace(",", ""))
            avg = int(asofs[-1].group(2).replace(",", ""))
            return (_pdate(p.group(1)), _pdate(p.group(2)), -sum(sold), h, avg)
    dm = re.search(r"During Period\s+(.+?)\s+to\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    if not dm:
        dm = re.search(r"period between\s+(.+?)\s+and\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text, re.I)
    if not dm:
        return None
    start, end = _pdate(dm.group(1)), _pdate(dm.group(2))

    # table row (tolerates "$ 101.3" or "$34.9", and "-" for no-purchase weeks)
    tm = re.search(r"Aggregate BTC Holdings.*?([\d,]+|-)\s+\$\s*[\d,.\-]+\s+\$\s*[\d,.\-]+"
                   r"\s+([\d,]{5,})\s+\$\s*[\d,.]+\s+\$\s*([\d,]+)", text)
    if tm:
        acquired = 0 if tm.group(1).strip() == "-" else int(tm.group(1).replace(",", ""))
        return (start, end, acquired, int(tm.group(2).replace(",", "")), int(tm.group(3).replace(",", "")))

    # prose no-purchase week
    pm = re.search(r"holds approximately ([\d,]{5,}) bitcoin", text, re.I)
    if pm and re.search(r"did not (?:purchase|acquire)", text, re.I):
        return (start, end, 0, int(pm.group(1).replace(",", "")), None)
    return None


def _asst_obs(text):
    """Strive 8-K -> [(date, holdings), ...]. Handles the 'Bitcoin held' table AND
    the prose 'bitcoin treasury totaled N bitcoin [as of DATE]' format used earlier."""
    obs = []
    dm = re.findall(r"As of ([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    bm = re.search(r"Bitcoin held\s+([\d,]{3,})\s+([\d,]{3,})", text)
    if len(dm) >= 2 and bm:
        a, b = _pdate(dm[0]), _pdate(dm[1])
        if a: obs.append((a, int(bm.group(1).replace(",", ""))))
        if b: obs.append((b, int(bm.group(2).replace(",", ""))))
    for m in re.finditer(r"bitcoin treasury totaled\s+([\d,]+)\s+bitcoin"
                         r"(?:\s+as of\s+([A-Z][a-z]+ \d{1,2}, \d{4}))?", text, re.I):
        n = int(m.group(1).replace(",", ""))
        dt = _pdate(m.group(2)) if m.group(2) else None
        if not dt:
            pre = re.findall(r"as of ([A-Z][a-z]+ \d{1,2}, \d{4})", text[:m.start()], re.I)
            dt = _pdate(pre[-1]) if pre else None
        if dt:
            obs.append((dt, n))
    # press-release boilerplate ("holds approximately N bitcoin(s) as of DATE") and
    # earnings-release phrasing ("Accumulated a total of N bitcoin as of DATE")
    for m in re.finditer(r"(?:holds approximately|[Aa]ccumulated a total of)\s+([\d,]+(?:\.\d+)?)\s+bitcoins?"
                         r"\s+as of\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text):
        dt = _pdate(m.group(2))
        if dt:
            obs.append((dt, int(float(m.group(1).replace(",", "")))))
    # founding-era (Sep 2025 – Jan 2026) one-off phrasings; dated by a preceding
    # "as of" or, failing that, the 8-K's event-report date
    for m in re.finditer(r"([\d,]+(?:\.\d+)?)\s+bitcoins?"
                         r"(?:\s+acquired at an average cost|,\s+with a total acquisition cost)"
                         r"|holdings increased to approximately\s+([\d,]+(?:\.\d+)?)\s+bitcoins?", text):
        n = m.group(1) or m.group(2)
        pre = re.findall(r"[Aa]s of ([A-Z][a-z]+ \d{1,2}, \d{4})", text[:m.start()])
        dt = _pdate(pre[-1]) if pre else None
        if not dt:
            rm = re.search(r"Date of Report \(Date of earliest event reported\)\s*:?\s*([A-Z][a-z]+ \d{1,2}, \d{4})", text)
            dt = _pdate(rm.group(1)) if rm else None
        if dt and n:
            obs.append((dt, int(float(n.replace(",", "")))))
    return obs


# MSTR cash roll-forward: anchor at the last filed balance-sheet cash, then add
# weekly 8-K flows (ATM net proceeds + BTC sale proceeds − BTC purchases) and
# subtract scheduled preferred dividends / convert coupons.
MSTR_CASH_FILED = ("2026-03-31", 2207.2)      # Q1-26 10-Q — bump when the next 10-Q lands
STRC_DIV_RATE = 0.115                          # current per-annum rate (monthly payer)
QTRLY_PREF_DIV = (1402 * .08 + 1284 * .10 + 1402 * .10) / 4   # STRK/STRF/STRD, $mm per quarter
CONVERT_COUPONS = {"06-15": 800 * .0225 / 2, "12-15": 800 * .0225 / 2,     # 2032s
                   "03-15": (1010 * .00625 + 800 * .00625 + 603.66 * .00875) / 2,
                   "09-15": (1010 * .00625 + 800 * .00625 + 603.66 * .00875) / 2}


def _mstr_dividends_paid(data, start, end):
    """Scheduled MSTR dividend/coupon cash out between (start, end], $mm (approx)."""
    steps = [tuple(x) for x in (data["companies"]["MSTR"].get("strcNotionalSteps") or [])]
    stre = next((x[2] for x in data["companies"]["MSTR"].get("prefBreakdown", []) if x[0] == "STRE"), 886)
    total = 0.0
    d = start
    while d < end:
        d += datetime.timedelta(days=1)
        nxt = d + datetime.timedelta(days=1)
        if nxt.day == 1:                                   # d is a month end
            rate = (data["companies"]["MSTR"].get("strcRate") or STRC_DIV_RATE * 100) / 100
            total += (_step(steps, d.isoformat()) or 0) * rate / 12   # STRC monthly
            if d.month in (3, 6, 9, 12):                   # quarter-end payers
                total += QTRLY_PREF_DIV + stre * .10 / 4
        total += CONVERT_COUPONS.get(d.strftime("%m-%d"), 0)
    return total


_ATM_LABEL = {"STRC": "STRC", "STRF": "STRF", "STRK": "STRK", "STRD": "STRD", "MSTR": "common stock"}


def _atm_netM(text):
    """Per-filing ATM net proceeds ($mm): preferred series vs common."""
    out = {"pref": 0.0, "common": 0.0}
    for ser in ("STRC", "STRF", "STRK", "STRD", "STRE", "MSTR"):
        m = re.search(rf"{ser} (?:ATM|Stock)\s*(.*?)(?=(?:STRC|STRF|STRK|STRD|STRE|MSTR)\s+(?:ATM|Stock)|Total)", text)
        if not m:
            continue
        seg = m.group(1)
        if "billion of" in seg:
            seg = seg[:seg.rfind("$", 0, seg.find("billion of"))]
        nums = [float(x.replace(",", "")) for x in re.findall(r"\$\s*([\d,.]+)", seg)]
        if len(nums) >= 2 and nums[-2] >= 0.5:
            out["common" if ser == "MSTR" else "pref"] += nums[-2]
    return out


def _mstr_actions(text, rec, fl):
    """Readable weekly actions from an MSTR 8-K."""
    items = []
    if rec[2] > 0:
        avg = round(fl["btcSpent"] * 1e6 / rec[2]) if fl["btcSpent"] else None
        items.append(f"Bought {rec[2]:,} BTC" + (f" (~${fl['btcSpent']:,.0f}M at ~${avg:,}/BTC)" if avg else ""))
    elif rec[2] < 0:
        items.append(f"Sold {-rec[2]:,} BTC for ~${fl['btcSold']:,.0f}M")
    # per-series ATM sales: segment each row, net proceeds = second-to-last $ figure
    raises = []
    for s in ("STRC", "STRF", "STRK", "STRD", "MSTR"):
        m = re.search(rf"{s} (?:ATM|Stock)\s*(.*?)(?=(?:STRC|STRF|STRK|STRD|STRE|MSTR)\s+(?:ATM|Stock)|Total)", text)
        if not m:
            continue
        seg = m.group(1)
        if "billion of" in seg:
            seg = seg[:seg.rfind("$", 0, seg.find("billion of"))]
        nums = [float(x.replace(",", "")) for x in re.findall(r"\$\s*([\d,.]+)", seg)]
        if len(nums) >= 2 and nums[-2] >= 0.5:
            raises.append(f"{_ATM_LABEL[s]} ${nums[-2]:,.0f}M")
    if raises:
        items.append(f"Raised ~${fl['raised']:,.0f}M net via ATM ({', '.join(raises)})")
    rm = re.search(r"dividend rate[^.]{0,200}?from ([\d.]+)% to ([\d.]+)%", text)
    if rm and "STRC" in text:
        items.append(f"{'Raised' if float(rm.group(2)) > float(rm.group(1)) else 'Cut'} STRC dividend rate "
                     f"{rm.group(1)}% → {rm.group(2)}%")
    return items


def _asst_actions(text):
    """Readable weekly actions from a Strive 8-K."""
    items = []
    m = re.search(r"purchased ([\d,]+) bitcoin at an average price of approximately \$\s?([\d,]+)", text)
    if m and int(m.group(1).replace(",", "")) > 0:
        items.append(f"Bought {m.group(1)} BTC at ~${m.group(2)}/BTC avg")
    cm = re.search(r"Cash and cash equivalents \(in thousands\)\s*\$\s*([\d,]+)\s*\$\s*([\d,]+)", text)
    if cm:
        a, b = (int(cm.group(i).replace(",", "")) / 1000 for i in (1, 2))
        if abs(b - a) >= 1:
            items.append(f"Cash {'+' if b >= a else '−'}${abs(b-a):,.1f}M (${a:,.1f}M → ${b:,.1f}M)")
    sm = re.search(r"Class A common stock\s*([\d,]+)\s*([\d,]+)\s*([\d,]+)", text)
    if sm and int(sm.group(3).replace(",", "")) > 1000:
        items.append(f"Issued {sm.group(3)} Class A shares (ATM)")
    pm = re.search(r"SATA Stock[^0-9]{0,60}([\d,]{6,})\s*([\d,]{6,})\s*([\d,]+)", text)
    if pm and int(pm.group(3).replace(",", "")) > 1000:
        items.append(f"Issued {pm.group(3)} SATA preferred shares")
    rm = re.search(r"dividend rate[^.]{0,200}?from ([\d.]+)% to ([\d.]+)%", text)
    if rm and "SATA" in text:
        items.append(f"{'Raised' if float(rm.group(2)) > float(rm.group(1)) else 'Cut'} SATA dividend rate "
                     f"{rm.group(1)}% → {rm.group(2)}%")
    return items


def _asst_snapshot(text):
    """Point-in-time snapshot from Strive's pre-May-2026 prose 8-Ks:
    (date, {cash, strc, btc, classA, sata}). Two phrasings observed."""
    TAIL = r"Strive had ([\d,]+) (?:and [\d,]+ )?shares of Class A.{0,90}?([\d,]+) shares of (?:its )?(?:Variable Rate|SATA)"
    m = re.search(r"[Aa]s of ([A-Z][a-z]+ \d{1,2}, \d{4}), Strive held \$\s?([\d,.]+) million of cash"
                  r".{0,120}?held \$\s?([\d,.]+) million in the Variable Rate.{0,160}?held ([\d,]+) bitcoin"
                  r".{0,40}?" + TAIL, text)
    if m:
        g = lambda i: float(m.group(i).replace(",", ""))
        return (_pdate(m.group(1)), {"cash": g(2), "strc": g(3), "btc": int(g(4)),
                                     "classA": int(g(5)), "sata": int(g(6))})
    m = re.search(r"[Aa]s of ([A-Z][a-z]+ \d{1,2}, \d{4}), the Company.?s bitcoin treasury totaled ([\d,]+) bitcoin"
                  r" and the Company.?s cash and cash equivalents and holdings (?:in|of) .{0,160}?totaled"
                  r" \$\s?([\d,.]+) million and \$\s?([\d,.]+) million.{0,60}?" + TAIL, text)
    if m:
        g = lambda i: float(m.group(i).replace(",", ""))
        return (_pdate(m.group(1)), {"cash": g(3), "strc": g(4), "btc": int(g(2)),
                                     "classA": int(g(5)), "sata": int(g(6))})
    m = re.search(r"[Aa]s of ([A-Z][a-z]+ \d{1,2}, \d{4}), Strive held \$\s?([\d,.]+) million of cash"
                  r".{0,220}?(?:and|,)\s*([\d,]+) bitcoin.{0,40}?" + TAIL, text)
    if m:      # cash-only phrasing (no STRC value clause)
        g = lambda i: float(m.group(i).replace(",", ""))
        return (_pdate(m.group(1)), {"cash": g(2), "strc": None, "btc": int(g(3)),
                                     "classA": int(g(4)), "sata": int(g(5))})
    return None


def fetch_holdings(data, max_points=60):
    """Rebuild data['weekly'] per company from the issuers' 8-Ks, from Jan 1 onward.

    MSTR: one parseable purchase 8-K per week (acquired + holdings).
    ASST: observation-based (holdings at each reported date); Strive launched its
    bitcoin treasury mid-Q1, so the series is anchored at Jan 1 = 0 BTC.
    """
    cutoff = datetime.date(datetime.date.today().year, 1, 1)
    f = lambda d: d.strftime("%b %-d")
    weekly = {"illustrative": False}
    acts = []
    for tk, cik in CIK.items():
        try:
            sub = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
        except Exception as e:
            log(f"[skip] EDGAR submissions {tk} failed: {e}")
            continue
        r = sub["filings"]["recent"]
        docpat = re.compile(rf"^(?:{tk.lower()}-\d{{8}}\.htm|.*8k.*\.htm)$", re.I)

        def docs():
            for i in range(len(r["form"])):
                if r["form"][i] == "8-K" and docpat.match(r["primaryDocument"][i] or ""):
                    acc = r["accessionNumber"][i].replace("-", "")
                    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/"
                    yield (base + r["primaryDocument"][i], base)

        if tk == "MSTR":
            t3 = {"pref": 0.0, "common": 0.0}
            strc_rate = None
            anchor = datetime.date.fromisoformat(MSTR_CASH_FILED[0])
            flows = {"raised": 0.0, "btcSpent": 0.0, "btcSold": 0.0}
            flow_asof = anchor
            pts, seen, fetched = [], set(), 0
            for url, base in docs():
                try:
                    t8 = _edgar_text(url)
                    rec = _parse_mstr(t8)
                except Exception:
                    rec = None; t8 = ""
                fetched += 1
                if rec and rec[1] and rec[3] and rec[1] not in seen:
                    seen.add(rec[1]); pts.append(rec)
                    fl = _parse_flows(t8) if t8 else {"raised": 0, "btcSpent": 0, "btcSold": 0}
                    if rec[1] > anchor:                    # roll cash forward from filed anchor
                        for k in flows: flows[k] += fl[k]
                        flow_asof = max(flow_asof, rec[1])
                    ai = _mstr_actions(t8, rec, fl) if t8 else []
                    if ai:
                        acts.append({"d": rec[1].isoformat(), "co": "MSTR", "items": ai})
                    if t8 and rec[1] > datetime.date.today() - datetime.timedelta(days=92):
                        bd = _atm_netM(t8)
                        t3["pref"] += bd["pref"]; t3["common"] += bd["common"]
                    if strc_rate is None and t8:
                        rm = (re.search(r"dividend rate per annum on[^.]{0,140}?STRC[^.]{0,200}?to ([\d.]+)%", t8)
                              or re.search(r"maintained[^.]{0,140}?STRC[^.]{0,140}?at ([\d.]+)%", t8))
                        if rm:
                            strc_rate = float(rm.group(1))
                    if len(pts) >= max_points: break
                if fetched >= max_points * 3: break
                time.sleep(0.12)
            if not pts:
                log(f"[skip] EDGAR {tk}: no parseable 8-Ks"); continue
            pts.reverse()
            pts = [p for p in pts if p[1] >= cutoff] or pts
            # net change from successive holdings is more robust than the reported
            # acquired column (a fiscal-boundary 8-K can report two periods; sales
            # split across them would otherwise be understated)
            acq = [pts[0][2]] + [pts[i][3] - pts[i-1][3] for i in range(1, len(pts))]
            weekly[tk] = {
                "dates":    [f(e) for (_, e, _, _, _) in pts],
                "ranges":   [f"{f(s)} – {f(e)}" if s else f(e) for (s, e, _, _, _) in pts],
                "acquired": acq,
                "holdings": [h for (_, _, _, h, _) in pts],
            }
            cur = pts[-1][3]
            if pts[-1][4]:      # authoritative avg purchase price from the latest 8-K
                data["companies"][tk]["avgCost"] = pts[-1][4]
            # estimated current cash: filed anchor + weekly flows − scheduled dividends
            divs = _mstr_dividends_paid(data, anchor, flow_asof)
            est = MSTR_CASH_FILED[1] + flows["raised"] + flows["btcSold"] - flows["btcSpent"] - divs
            co = data["companies"][tk]
            co["cashFlows"] = {"anchor": MSTR_CASH_FILED[1], "anchorDate": MSTR_CASH_FILED[0],
                               "raised": round(flows["raised"]), "btcSold": round(flows["btcSold"]),
                               "btcSpent": round(flows["btcSpent"]), "divs": round(divs),
                               "asOf": flow_asof.isoformat()}
            co["cashFiled"] = MSTR_CASH_FILED[1]
            co["cash"] = round(est)
            co["trail3m"] = {"prefMo": round(t3["pref"] / 3), "commonMo": round(t3["common"] / 3)}
            # the tracker's STRC dividendRate lags rate-change 8-Ks; the filings win
            if strc_rate:
                co["strcRate"] = strc_rate
                fixed = {"STRK": 8.0, "STRF": 10.0, "STRD": 10.0, "STRE": 10.0, "STRC": strc_rate}
                bd = co.get("prefBreakdown") or []
                pref_div = 0.0
                for row in bd:
                    if row[0] == "STRC":
                        row[1] = f"Stretch · {strc_rate:g}% var"
                    pref_div += row[2] * fixed.get(row[0], 10.0) / 100
                coup = sum(x["principal"] * x["coupon"] / 100 for x in (co.get("debtSchedule") or []))
                co["annualObligations"] = round(pref_div + coup)
                log(f"MSTR STRC rate from 8-K: {strc_rate}% -> annualObligations {co['annualObligations']}")
            log(f"MSTR cash est: {MSTR_CASH_FILED[1]} filed + {flows['raised']:,.0f} raised "
                f"+ {flows['btcSold']:,.0f} BTC sold - {flows['btcSpent']:,.0f} BTC bought "
                f"- {divs:,.0f} divs = ${est:,.0f}M (as of {flow_asof})")
        else:  # ASST — observation-based
            allobs, fetched = {}, 0
            cash_usd = strc_sh = None
            snaps = {}
            t3c = 0.0     # trailing-92d common $ raised (shares issued x that week's price)
            cutoff92 = datetime.date.today() - datetime.timedelta(days=92)
            _sh = data.get("stockHistory") or {}
            pxmap = dict(zip(_sh.get("dates") or [], _sh.get("ASST") or []))
            for url, base in docs():
                try:
                    t8 = _edgar_text(url)
                    obs_dates = []
                    for d, h in _asst_obs(t8):
                        allobs[d] = h
                        obs_dates.append(d)
                    ai = _asst_actions(t8)
                    sn = _asst_snapshot(t8)
                    if not (obs_dates or sn):
                        # early 2026 filings put the treasury update in a press-release
                        # exhibit (ex-99) rather than the 8-K body — check there too
                        try:
                            idx = _edgar_json(base + "index.json")
                            for fobj in idx["directory"]["item"]:
                                n = fobj["name"]
                                if not n.endswith(".htm") or not re.search(r"ex.{0,2}99", n, re.I):
                                    continue
                                te = _edgar_text(base + n)
                                time.sleep(0.1)
                                for d, h in _asst_obs(te):
                                    allobs[d] = h
                                    obs_dates.append(d)
                                sn = sn or _asst_snapshot(te)
                                ai = ai or _asst_actions(te)
                                if obs_dates or sn:
                                    break
                        except Exception:
                            pass
                    if ai and obs_dates:
                        acts.append({"d": max(obs_dates).isoformat(), "co": "ASST", "items": ai})
                    if obs_dates and max(obs_dates) > cutoff92:
                        shm = re.search(r"Class A common stock\s*[\d,]+\s*[\d,]+\s*([\d,]+)", t8)
                        if shm:
                            dsh = int(shm.group(1).replace(",", ""))
                            px8 = pxmap.get(max(obs_dates).strftime("%b %-d")) or data["companies"]["ASST"].get("stockPrice") or 0
                            if 1000 < dsh < 5e7 and px8:
                                t3c += dsh * px8 / 1e6
                    if sn and sn[0]:
                        snaps[sn[0]] = sn[1]
                        allobs.setdefault(sn[0], sn[1]["btc"])
                    # Strive's weekly 8-K splits its reserve into true USD cash and a
                    # 505k-share STRC position at fair value (their site lumps both as
                    # "cash"); capture the components from the newest filing that has them
                    if cash_usd is None:
                        cm = re.search(r"Cash and cash equivalents \(in thousands\)\s*\$\s*[\d,]+\s*\$\s*([\d,]+)", t8)
                        sm = re.search(r"Shares of STRC held\s*[\d,]+\s*([\d,]+)", t8)
                        if cm and sm:
                            cash_usd = round(int(cm.group(1).replace(",", "")) / 1000, 1)
                            strc_sh = int(sm.group(1).replace(",", ""))
                except Exception:
                    pass
                fetched += 1
                if fetched >= max_points: break
                time.sleep(0.12)
            if cash_usd is not None:
                co = data["companies"][tk]
                co["cashUsd"], co["strcShares"] = cash_usd, strc_sh
                strc_px = data["companies"]["MSTR"].get("prefPrice") or 0
                if strc_px:     # reserve = USD cash + STRC marked at the live price
                    co["cash"] = round(cash_usd + strc_sh * strc_px / 1e6)
                log(f"ASST cash: ${cash_usd}M USD + {strc_sh:,} STRC @ ${strc_px} -> ${co['cash']}M")
            # trailing-3-month issuance pace (calculator defaults)
            co = data["companies"][tk]
            ph = co.get("prefHistory") or {}
            no = [x for x in (ph.get("notional") or []) if x is not None]
            pref3 = round((no[-1] - no[-64]) / 3) if len(no) > 64 else 0
            co["trail3m"] = {"prefMo": max(pref3, 0), "commonMo": round(t3c / 3)}
            # genesis anchor: Strive announced its bitcoin treasury pivot on
            # Sep 9, 2025 with no BTC held; lets the first buys register as deltas
            allobs.setdefault(datetime.date(2025, 9, 9), 0)
            # pre-May-2026 filings are prose snapshots, not change tables — derive
            # the weekly actions from consecutive snapshot deltas instead
            covered = {a["d"] for a in acts if a["co"] == "ASST"}
            sd = sorted(snaps)
            for i in range(1, len(sd)):
                d0, d1 = sd[i - 1], sd[i]
                if d1.isoformat() in covered or (d1 - d0).days > 70:
                    continue
                a, b = snaps[d0], snaps[d1]
                its = []
                if b["btc"] > a["btc"]:
                    its.append(f"Bought {b['btc']-a['btc']:,} BTC")
                elif b["btc"] < a["btc"]:
                    its.append(f"Sold {a['btc']-b['btc']:,} BTC")
                dc = b["cash"] - a["cash"]
                if abs(dc) >= 1:
                    its.append(f"Cash {'+' if dc >= 0 else '−'}${abs(dc):,.1f}M (${a['cash']:,.1f}M → ${b['cash']:,.1f}M)")
                if b["classA"] - a["classA"] > 1000:
                    its.append(f"Issued {b['classA']-a['classA']:,} Class A shares (ATM)")
                if b["sata"] - a["sata"] > 1000:
                    its.append(f"Issued {b['sata']-a['sata']:,} SATA preferred shares")
                if its:
                    acts.append({"d": d1.isoformat(), "co": "ASST", "items": its})
            # earliest weeks disclosed only BTC counts — fall back to holdings deltas
            covered = {a["d"] for a in acts if a["co"] == "ASST"}
            obs_sorted = sorted(allobs.items())
            for i in range(1, len(obs_sorted)):
                (d0, h0), (d1, h1) = obs_sorted[i - 1], obs_sorted[i]
                if d1.isoformat() in covered or (d1 - d0).days > 70 or h1 == h0:
                    continue
                acts.append({"d": d1.isoformat(), "co": "ASST",
                             "items": [f"{'Bought' if h1 > h0 else 'Sold'} {abs(h1-h0):,} BTC"]})
            items = sorted((d, h) for d, h in allobs.items() if d >= cutoff)
            if not items:
                log(f"[skip] EDGAR {tk}: no parseable 8-Ks"); continue
            ye0 = (data["companies"][tk].get("yearEnd") or {}).get(str(cutoff.year - 1)) or 0
            items = [(cutoff, ye0)] + items        # anchor at last year-end holdings
            holds = [h for _, h in items]
            # bars only for weekly-cadence filings; pre-weekly jumps show on the line only
            wk = datetime.timedelta(days=14)
            acq = [None]
            for i in range(1, len(items)):
                gap = items[i][0] - items[i-1][0]
                acq.append(holds[i] - holds[i-1] if gap <= wk else None)
            weekly[tk] = {
                "dates":    [f(d) for d, _ in items],
                "ranges":   ["Jan 1"] + [f"{f(items[i-1][0])} – {f(items[i][0])}" for i in range(1, len(items))],
                "acquired": acq,
                "holdings": holds,
            }
            cur = holds[-1]

        data["companies"][tk]["holdings"] = cur
        data["companies"][tk]["pctSupply"] = round(cur / data["btcSupply"] * 100, 4)
        ye = (data["companies"][tk].get("yearEnd") or {}).get(str(cutoff.year - 1))
        if ye is not None:
            data["companies"][tk]["netChangeYtd"] = cur - ye
        log(f"{tk}: {len(weekly[tk]['dates'])} points via EDGAR ({weekly[tk]['dates'][0]} -> "
            f"{weekly[tk]['dates'][-1]}), latest {cur:,} BTC")
    if "MSTR" in weekly or "ASST" in weekly:
        data["weekly"] = weekly
    if acts:    # merge with previously stored actions so old weeks never drop off
        old = {(a["d"], a["co"]): a for a in (data.get("actions") or [])}
        for a in acts:
            old[(a["d"], a["co"])] = a
        data["actions"] = sorted(old.values(), key=lambda a: a["d"], reverse=True)[:120]


# --------------------------------------------------------------------------- #
# TODO: cost basis & per-share / yield  (strategy.com, treasury.strive.com)
# --------------------------------------------------------------------------- #
def fetch_per_share(data):
    """avgCost, satsPerShareBasic/Diluted, btcYieldYtd/Qtd, btcGain."""
    try:
        raise NotImplementedError
    except Exception:
        log("[skip] per-share / yield source not wired — keeping existing values")


# --------------------------------------------------------------------------- #
# TODO: CEBE metrics  (cebetracker.io)
# --------------------------------------------------------------------------- #
def fetch_cebe(data):
    """cebeSatsPerShare, claimsPct, satsPer100, cebeMnav."""
    try:
        raise NotImplementedError
    except Exception:
        log("[skip] CEBE source not wired — keeping existing values")


# --------------------------------------------------------------------------- #
# Nasdaq short interest -> days to cover (semi-monthly settlement dates)
# --------------------------------------------------------------------------- #
NASDAQ_UA = {"User-Agent": UA["User-Agent"], "Accept": "application/json",
             "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def _nasdaq_vol(sym, days=420):
    """Daily share volume [(iso date, volume), ...] from Nasdaq (split-adjusted)."""
    frm = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    url = (f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=stocks"
           f"&limit=9999&fromdate={frm}&todate={datetime.date.today().isoformat()}")
    req = urllib.request.Request(url, headers=NASDAQ_UA)
    raw = urllib.request.urlopen(req, timeout=25, context=_SSL_CTX).read().decode("utf-8", "ignore")
    rows = ((json.loads(raw)["data"] or {}).get("tradesTable") or {}).get("rows") or []
    out = []
    for r in rows:
        d = datetime.datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat()
        v = str(r.get("volume") or "").replace(",", "")
        if v.isdigit() and int(v):
            out.append((d, int(v)))
    return sorted(out)


def _yahoo_vol(symbol):
    """Daily share volume [(iso date, volume), ...] from Yahoo's 1y chart."""
    res = _yahoo_chart(symbol)["chart"]["result"][0]
    ts = res["timestamp"]
    vols = res["indicators"]["quote"][0]["volume"]
    return sorted((datetime.datetime.utcfromtimestamp(t).date().isoformat(), int(v))
                  for t, v in zip(ts, vols) if v)


def _dtc20(sih, volhist):
    """Days to cover on the trailing 20-trading-day average volume at each settlement."""
    vols = sorted(volhist)
    out = []
    for iso, s in zip(sih["iso"], sih["si"]):
        past = [v for d, v in vols if d <= iso][-20:]
        avg = sum(past) / len(past) if len(past) >= 5 else None
        out.append(round(s / avg, 2) if avg else None)
    return out


def _dtc_live(sih, volhist):
    """Live DTC: latest reported shorts / the 20 trading days of volume ending today."""
    last = sorted(volhist)[-20:]
    if len(last) < 5:
        return None
    avg = sum(v for _, v in last) / len(last)
    return round(sih["si"][-1] / avg, 2) if avg else None


def _nasdaq_si(sym):
    """Semi-monthly short-interest history for one symbol from Nasdaq."""
    url = f"https://api.nasdaq.com/api/quote/{sym}/short-interest?assetClass=stocks"
    req = urllib.request.Request(url, headers=NASDAQ_UA)
    raw = urllib.request.urlopen(req, timeout=25, context=_SSL_CTX).read().decode("utf-8", "ignore")
    rows = list(reversed(json.loads(raw)["data"]["shortInterestTable"]["rows"]))  # oldest -> newest
    dts, isod, dtc, si = [], [], [], []
    for r in rows:
        d = datetime.datetime.strptime(r["settlementDate"], "%m/%d/%Y").date()
        shares = int(str(r["interest"]).replace(",", ""))
        for eff, ratio in SI_SPLITS.get(sym, []):     # normalize pre-split settlements
            if d.isoformat() < eff:
                shares = round(shares / ratio)
        dts.append(d.strftime("%b %-d"))
        isod.append(d.isoformat())
        dtc.append(round(float(r["daysToCover"]), 2))
        si.append(shares)
    return {"dates": dts, "iso": isod, "dtc": dtc, "si": si} if dtc else None


def fetch_short_interest(data):
    """Days to cover + short interest history from Nasdaq (common + preferred)."""
    for sym, co in data["companies"].items():
        try:
            sih = _nasdaq_si(sym)
            if sih:
                # % of float at each settlement: shares outstanding on that date
                # (tracker daily history) minus the insider Class B block
                b = CLASS_B_SHARES_M.get(sym, 0)
                sh = _SHARES_HIST.get(sym) or []
                pct = []
                for iso, s in zip(sih["iso"], sih["si"]):
                    past = [v for k, v in sh if k <= iso]
                    tot = past[-1] if past else co.get("sharesOutstanding")
                    flt = (tot - b) if tot else 0
                    pct.append(round(s / (flt * 1e6) * 100, 2) if flt > 0.5 else None)
                sih["pctFloat"] = pct
                try:                                   # trailing-20d days to cover
                    try:
                        vols = _nasdaq_vol(sym)
                    except Exception:
                        vols = _yahoo_vol(sym)
                    sih["dtc20"] = _dtc20(sih, vols)
                    sih["dtcLive"] = _dtc_live(sih, vols)
                except Exception as e:
                    log(f"[skip] {sym} volume for DTC-20d: {e} — falling back to Nasdaq DTC")
                co["shortInterest"] = sih
                co["daysToCover"] = sih["dtc"][-1]
                log(f"[short interest] {sym}: {len(sih['dtc'])} pts, latest DTC {sih['dtc'][-1]}, "
                    f"{pct[-1]}% of float")
        except Exception as e:
            log(f"[skip] short interest {sym}: {e} — keeping existing values")
        pref = co.get("prefTicker")
        if pref:
            try:
                sih = _nasdaq_si(pref)
                if sih:
                    # % of float: preferred float = notional outstanding / $100 par
                    ph = co.get("prefHistory") or {}
                    steps = [(d, n) for d, n in zip(ph.get("iso") or [], ph.get("notional") or []) if n]
                    pct = []
                    for iso, s in zip(sih["iso"], sih["si"]):
                        ns = [n for d2, n in steps if d2 <= iso]
                        pct.append(round(s / (ns[-1] * 1e4) * 100, 2) if ns else None)
                    sih["pctFloat"] = pct
                    if pref in _VOL_HIST:
                        sih["dtc20"] = _dtc20(sih, _VOL_HIST[pref])
                        sih["dtcLive"] = _dtc_live(sih, _VOL_HIST[pref])
                    co["prefShortInterest"] = sih
                    log(f"[short interest] {pref}: {len(sih['dtc'])} pts, latest {sih['si'][-1]:,} sh "
                        f"({pct[-1]}% of float)")
            except Exception as e:
                log(f"[skip] short interest {pref}: {e} — keeping existing values")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    dry = "--dry-run" in sys.argv
    if not DATA_PATH.exists():
        sys.exit(f"data.json not found at {DATA_PATH}")

    data = json.loads(DATA_PATH.read_text())
    before = json.dumps(data, sort_keys=True)

    print("Refreshing treasury data…")
    fetch_btc_market(data)            # CoinGecko: BTC price + supply
    fetch_strategytracker(data)       # PRIMARY: current metrics + real history (both names)
    fetch_holdings(data)             # SEC EDGAR: weekly accumulation (8-K period ranges)
    fetch_short_interest(data)        # Nasdaq: days to cover (semi-monthly)
    # debt schedule + preferred breakdown are parsed from the 10-Q (see notes);
    # cebe / per-share / valuation are computed live in the dashboard.

    data["asOf"] = datetime.date.today().isoformat()

    after = json.dumps(data, sort_keys=True)
    if before == after:
        print("No changes.")
    elif dry:
        print("\n[dry-run] would write updated data.json (BTC market + asOf).")
    else:
        DATA_PATH.write_text(json.dumps(data, indent=2) + "\n")
        # also emit data.js so the dashboard works when opened as a file:// (no CORS)
        DATA_PATH.with_name("data.js").write_text(
            "window.__DATA__ = " + json.dumps(data, separators=(",", ":")) + ";\n")
        print(f"\nWrote {DATA_PATH} (+ data.js)")


if __name__ == "__main__":
    main()
