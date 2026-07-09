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
                    dts, px, no = [], [], []
                    for q in hp:
                        dts.append(_iso_lbl(q["date"], "%b %-d"))
                        px.append(round(q["close"], 2))
                        n = None
                        for e in chg:
                            if e["effective_date"] <= q["date"]:
                                n = e["notional_millions"]
                            else:
                                break
                        no.append(round(n) if n is not None else None)
                    if px:
                        co["prefHistory"] = {"dates": dts, "px": px, "notional": no}
            bd.sort(key=lambda x: -x[2])
            co["prefBreakdown"] = bd
            co["prefNotional"] = round(sum(x[2] for x in bd))
            co["prefMarket"] = round(mkt)
            pref_div = sum((p.get("notionalMillions") or (p.get("notionalUSD") or 0) / 1e6)
                           * (p.get("dividendRate") or 0) / 100 for p in ps)
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
            dts_m, px_m, mnv = [], [], []
            for i, d in enumerate(hd["dates"]):
                if d < start:
                    continue
                mc, b, bp, sp = (hd["market_cap_basic"][i], hd["btc_balance"][i],
                                 hd["btc_prices"][i], hd["stock_prices"][i])
                if not (mc and b and bp and sp):
                    continue
                nav = b * bp / 1e6
                ev = mc / 1e6 + debt_at(d, i) + pref_at(d) - cash_at(d, i)
                dts_m.append(_iso_lbl(d, "%b %-d"))
                px_m.append(round(sp, 2))
                mnv.append(round(ev / nav, 3))
            if mnv:
                co["mnavHistory"] = {"dates": dts_m, "px": px_m, "mnav": mnv}
                log(f"{tk} mNAV history: {len(mnv)} pts, latest {mnv[-1]}x")
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


def _pdate(s):
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y"):
        try:
            return datetime.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_mstr(text):
    """Strategy 8-K 'BTC Update' -> (start, end, acquired, holdings).

    Handles three observed shapes: a purchase-week table, a no-purchase-week
    table (acquired shown as '-'), and a prose no-purchase statement.
    """
    if "BTC Update" not in text:
        return None
    dm = re.search(r"During Period\s+(.+?)\s+to\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    if not dm:
        dm = re.search(r"period between\s+(.+?)\s+and\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text, re.I)
    if not dm:
        return None
    start, end = _pdate(dm.group(1)), _pdate(dm.group(2))

    # table row (tolerates "$ 101.3" or "$34.9", and "-" for no-purchase weeks)
    tm = re.search(r"Aggregate BTC Holdings.*?([\d,]+|-)\s+\$\s*[\d,.\-]+\s+\$\s*[\d,.\-]+"
                   r"\s+([\d,]{5,})\s+\$\s*[\d,.]+\s+\$\s*[\d,]+", text)
    if tm:
        acquired = 0 if tm.group(1).strip() == "-" else int(tm.group(1).replace(",", ""))
        return (start, end, acquired, int(tm.group(2).replace(",", "")))

    # prose no-purchase week
    pm = re.search(r"holds approximately ([\d,]{5,}) bitcoin", text, re.I)
    if pm and re.search(r"did not (?:purchase|acquire)", text, re.I):
        return (start, end, 0, int(pm.group(1).replace(",", "")))
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
    return obs


def fetch_holdings(data, max_points=60):
    """Rebuild data['weekly'] per company from the issuers' 8-Ks, from Jan 1 onward.

    MSTR: one parseable purchase 8-K per week (acquired + holdings).
    ASST: observation-based (holdings at each reported date); Strive launched its
    bitcoin treasury mid-Q1, so the series is anchored at Jan 1 = 0 BTC.
    """
    cutoff = datetime.date(datetime.date.today().year, 1, 1)
    f = lambda d: d.strftime("%b %-d")
    weekly = {"illustrative": False}
    for tk, cik in CIK.items():
        try:
            sub = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
        except Exception as e:
            log(f"[skip] EDGAR submissions {tk} failed: {e}")
            continue
        r = sub["filings"]["recent"]
        docpat = re.compile(rf"^{tk.lower()}-\d{{8}}\.htm$")

        def docs():
            for i in range(len(r["form"])):
                if r["form"][i] == "8-K" and docpat.match(r["primaryDocument"][i] or ""):
                    acc = r["accessionNumber"][i].replace("-", "")
                    yield (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/"
                           f"{r['primaryDocument'][i]}")

        if tk == "MSTR":
            pts, seen, fetched = [], set(), 0
            for url in docs():
                try:
                    rec = _parse_mstr(_edgar_text(url))
                except Exception:
                    rec = None
                fetched += 1
                if rec and rec[1] and rec[3] and rec[1] not in seen:
                    seen.add(rec[1]); pts.append(rec)
                    if len(pts) >= max_points: break
                if fetched >= max_points * 3: break
                time.sleep(0.12)
            if not pts:
                log(f"[skip] EDGAR {tk}: no parseable 8-Ks"); continue
            pts.reverse()
            pts = [p for p in pts if p[1] >= cutoff] or pts
            weekly[tk] = {
                "dates":    [f(e) for (_, e, _, _) in pts],
                "ranges":   [f"{f(s)} – {f(e)}" if s else f(e) for (s, e, _, _) in pts],
                "acquired": [a for (_, _, a, _) in pts],
                "holdings": [h for (_, _, _, h) in pts],
            }
            cur = pts[-1][3]
        else:  # ASST — observation-based
            allobs, fetched = {}, 0
            for url in docs():
                try:
                    for d, h in _asst_obs(_edgar_text(url)):
                        allobs[d] = h
                except Exception:
                    pass
                fetched += 1
                if fetched >= max_points: break
                time.sleep(0.12)
            items = sorted((d, h) for d, h in allobs.items() if d >= cutoff)
            if not items:
                log(f"[skip] EDGAR {tk}: no parseable 8-Ks"); continue
            items = [(cutoff, 0)] + items          # held ~0 BTC at the start of the year
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
        log(f"{tk}: {len(weekly[tk]['dates'])} points via EDGAR ({weekly[tk]['dates'][0]} -> "
            f"{weekly[tk]['dates'][-1]}), latest {cur:,} BTC")
    if "MSTR" in weekly or "ASST" in weekly:
        data["weekly"] = weekly


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


def fetch_short_interest(data):
    """Days to cover + short interest history from Nasdaq (both tickers)."""
    for sym, co in data["companies"].items():
        try:
            url = f"https://api.nasdaq.com/api/quote/{sym}/short-interest?assetClass=stocks"
            req = urllib.request.Request(url, headers=NASDAQ_UA)
            raw = urllib.request.urlopen(req, timeout=25, context=_SSL_CTX).read().decode("utf-8", "ignore")
            rows = json.loads(raw)["data"]["shortInterestTable"]["rows"]
            rows = list(reversed(rows))            # oldest -> newest
            dts, dtc, si = [], [], []
            for r in rows:
                d = datetime.datetime.strptime(r["settlementDate"], "%m/%d/%Y").date()
                dts.append(d.strftime("%b %-d"))    # matches stockHistory date labels
                dtc.append(round(float(r["daysToCover"]), 2))
                si.append(int(str(r["interest"]).replace(",", "")))
            if dtc:
                co["shortInterest"] = {"dates": dts, "dtc": dtc, "si": si}
                co["daysToCover"] = dtc[-1]
                log(f"[short interest] {sym}: {len(dtc)} pts, latest DTC {dtc[-1]}")
        except Exception as e:
            log(f"[skip] short interest {sym}: {e} — keeping existing values")


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
