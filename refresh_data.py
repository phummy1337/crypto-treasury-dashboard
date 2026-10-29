#!/usr/bin/env python3
"""
refresh_data.py — repopulate data.json for TreasuryTracker.NET.

The dashboard (index.html) reads ./data.json on load. Run this script (or
schedule it) to refresh the numbers without editing the HTML.

  python3 refresh_data.py            # update in place
  python3 refresh_data.py --dry-run  # print what would change, write nothing

WHAT WORKS OUT OF THE BOX
  - Live BTC + SOL price and circulating supply -> CoinGecko public API (no key)

  - Stock price, day change, 52-week range, 1-year price history, beta-to-BTC,
    relative volume -> strategytracker (no key). See fetch_strategytracker().
    MSTR's price is then overlaid with strategy.com's official close.

  - BTC holdings + true weekly purchases  -> SEC EDGAR 8-Ks (no key). See
    fetch_holdings(): parses each issuer's weekly purchase 8-K (Strategy's
    "BTC Update" table, Strive's "Bitcoin held" table), rebuilds data["weekly"]
    and refreshes current holdings / % of supply.

  - apxUSD lending APYs for the lending banner -> Morpho API (open GraphQL, no
    key). See fetch_lending().

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

import bisect
import json
import re
import sys
import ssl
import time
import datetime
import urllib.request
import urllib.error
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_PATH = Path(__file__).with_name("data.json")
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


def post_json(url, payload, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return json.loads(r.read().decode())


def log(msg):
    print(f"  {msg}")


# --------------------------------------------------------------------------- #
# WORKING: BTC + SOL price and supply (CoinGecko)
# --------------------------------------------------------------------------- #
def fetch_crypto_market(data):
    try:
        coins = {c["id"]: c for c in get_json(
            "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=bitcoin,solana")}
        btc = coins["bitcoin"]
        data["btcPriceUsd"] = round(btc["current_price"], 2)
        _BTC_PX["usd"] = btc["current_price"]
        data["priceAsOf"] = datetime.date.today().isoformat()
        if btc.get("circulating_supply"):
            data["btcCirculating"] = int(btc["circulating_supply"])
        log(f"BTC price = ${btc['current_price']:,.0f}  (circulating {btc.get('circulating_supply') or 0:,.0f})")
        sol = coins.get("solana")
        if sol and sol.get("current_price"):
            data["solPriceUsd"] = round(sol["current_price"], 2)
            if sol.get("circulating_supply"):
                data["solCirculating"] = int(sol["circulating_supply"])
            log(f"SOL price = ${sol['current_price']:,.2f}  (circulating {sol.get('circulating_supply') or 0:,.0f})")
        # % of supply against each treasury's own asset: BTC's 21M cap, SOL's
        # circulating supply (SOL has no cap)
        for t, c in data["companies"].items():
            base = data.get("solCirculating") if c.get("asset") == "SOL" else data["btcSupply"]
            if base and c.get("holdings"):
                c["pctSupply"] = round(c["holdings"] / base * 100, 4)
        return True
    except Exception as e:
        log(f"[skip] CoinGecko failed: {e}")
        return False


# --------------------------------------------------------------------------- #
# PRIMARY SOURCE: strategytracker.com data API (powers strategy.com /
# treasury.strive.com). Current metrics + real daily history for both names.
# --------------------------------------------------------------------------- #
TRACKER_BASE = "https://data.strategytracker.com/"

# Per-row inputs behind mnavHistory, and the newest filed holdings. The history is
# built from the tracker's daily series in fetch_strategytracker, which runs BEFORE
# fetch_holdings reads the 8-Ks — so the series cannot see a purchase the filings
# already know about. rebase_mnav_holdings() closes that gap afterwards.
_MNAV_INPUTS, _FILED_HOLD = {}, {}
_FILED_DATES = {}       # every filed observation date, oldest first (ASST)
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
# MSTR cash ($mm) as the filings state it, dated to each balance's as-of date, a step only
# where it changed: 10-Q cash until the USD Reserve began on 2025-12-01, then the USD
# Reserve (weekly 8-Ks, the 10-K and 10-Qs) plus USD Cash from 2026-08-23, the balances
# strategy.com's live figure counts. 03-31 and 06-30 are dividend days the 10-Qs catch
# mid-dip. 05-19 is derived: 2,250 less the $1,378.6M the reserve paid for the 2029s,
# which the filed 05-25 balance confirms after a week with no ATM sales, BTC buys or
# dividends. Filings never change; live steps in histStepsLive extend past the last one.
MSTR_CASH_STEPS = [("2025-06-30", 50.1), ("2025-09-30", 54.3),
                   ("2025-12-01", 1440.0), ("2025-12-21", 2190.0), ("2025-12-31", 2250.0),
                   ("2026-03-31", 2140.0), ("2026-04-26", 2250.0), ("2026-05-19", 871.0),
                   ("2026-05-31", 900.0), ("2026-06-07", 1000.0), ("2026-06-14", 1100.0),
                   ("2026-06-21", 1400.0), ("2026-06-28", 2550.0), ("2026-06-30", 2400.0),
                   ("2026-07-05", 2550.0), ("2026-07-12", 3000.0), ("2026-07-19", 3225.0),
                   ("2026-07-24", 3750.0), ("2026-08-02", 4000.0), ("2026-08-09", 4650.0),
                   ("2026-08-16", 4800.0), ("2026-08-23", 6690.0), ("2026-08-30", 6710.0),
                   ("2026-09-07", 6540.0), ("2026-09-13", 6400.0), ("2026-09-20", 6090.0)]
# Convert principal ($mm): all six notes were issued by 2025-02-21; $1.5B of the 2029s
# were repurchased and cancelled 2026-05-19 (Q2 10-Q). Live steps take strategy.com's
# figure after that.
MSTR_DEBT_STEPS = [("2025-02-21", 8213.75), ("2026-05-19", 6713.75)]
# chart windows. DFDV's starts 2026-02-23, the first day its own daily EV series counts
# all of its debt: before that the series left out $125M-180M of it (on Feb 23 the
# $114.6M July 2025 convert came in, with no new borrowing), so both mNAV lines read
# too low (net 0.7x in January against roughly 2x).
MNAV_START = {"MSTR": "2025-10-01", "ASST": "2026-01-01", "DFDV": "2026-02-23"}


def _step(steps, iso):
    """Latest step value effective on or before iso date (steps sorted ascending)."""
    v = None
    for d, x in steps:
        if d <= iso:
            v = x
        else:
            break
    return v


def _save_live_step(live, key, day, value):
    """Persist a live history step, keeping one per day (the latest)."""
    live[key] = [x for x in live.get(key, []) if x[0] != day] + [[day, value]]


def _end_at(steps, point):
    """A (date, value) step series that ends on `point`, the issuer's own current
    figure: a step dated on or after it can only be a lagging source's."""
    if not point:
        return steps
    return [s for s in steps if s[0] < point[0]] + [tuple(point)]

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
# daily share volume per PREFERRED ticker, from the tracker's price log — used for
# the preferreds' days-to-cover. Commons volume is not kept here: fetch_short_interest
# pulls it from Nasdaq (_nasdaq_vol) and consumes it within the loop iteration.
_VOL_HIST = {}
# intraday 15-min candles per preferred ticker, for the ATM tracker
_ATM_CANDLES = {}
# settled daily dollar volume (close x volume) per preferred ticker, [(iso, $)] ascending:
# one series behind both the preferred's rvol and the ATM's absorption denominator
_PREF_DVOL = {}
# 8-K filing index per company: [(filing date, primary doc url)] — lets each
# action in the log link back to the filing that disclosed it
_FILINGS = {}
# filing date of the newest 8-K this run fetched per company, whatever it carried:
# the filing watermark's "ingested"
_READ_8K = {}

def _cboe_last(sym):
    """Delayed last trade from Cboe, for a listing no other source here carries yet."""
    try:
        req = urllib.request.Request(
            f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/{sym}.json",
            headers={"User-Agent": "treasurytracker pete@defidevcorp.com"})
        d = json.loads(urllib.request.urlopen(req, timeout=20, context=_SSL_CTX).read())["data"]
        px = d.get("current_price") or d.get("close")
        return round(float(px), 2) if px else None
    except Exception as e:
        log(f"[skip] Cboe {sym}: {e}")
        return None


PREF_LABEL = {"STRC": "Stretch · {r}% var", "STRK": "Strike · {r}%", "STRF": "Strife · {r}%",
              "STRD": "Stride · {r}%", "STRE": "STRE · {r}% (EUR)", "SATA": "Strive pref · {r}%",
              "SATS": "Strive pref · {r}% (jr)", "CHAD": "CHAD · {r}% var"}

# Strive's SATS closed 2026-10-20: 7,000,000 shares at a $100 stated amount, 15.00%,
# sold at $90 for $630M gross and ~$608.15M net (8-K and 424B5 of 2026-10-16, closing
# 8-K of 2026-10-20). The tracker's preferred list still carries SATA alone and the
# weekly 8-K table will not show SATS until the next filing, so inject it from the
# filed terms. $700M of claim ranking ahead of common is not something to leave out
# of the net reserve for a week — but the cash that came in against it has to land
# at the same time, or the stack reads $608M poorer than it is.
#
# SATS is JUNIOR to SATA and senior to both common classes (closing 8-K, Item 3.03).
SATS = {"ticker": "SATS", "shares": 7_000_000, "stated": 100.0, "rate": 15.0,
        "netProceedsM": 608.15, "closed": "2026-10-20"}
# SATS's row in Strive's weekly table, written like SATA's ("SATA Stock (6) 14,030,176 ...")
_SATS_ROW = r"SATS Stock(?:\s*\(\d+\))?"


def _asst_pref2(co, row, today):
    """co["pref2"] for Strive's second preferred, SATS, from its preferred-list row:
    the feed's once it carries SATS, until then the filed terms above.

    Nasdaq lists SATS as Strive's 15% preferred (checked 2026-10-29; ChartExchange's
    "SATS" is still EchoStar). Its daily closes give the chart, the volume behind days
    to cover, and the previous close the tile's day change needs. Without them the
    chart grows by one snapshot point a session."""
    nM = round(row.get("notionalMillions") or (row.get("notionalUSD") or 0) / 1e6
               or SATS["shares"] * SATS["stated"] / 1e6)
    px = row.get("price")
    p2 = {"ticker": "SATS", "price": px, "rate": row.get("dividendRate") or SATS["rate"],
          "stated": SATS["stated"], "notional": nM}
    try:
        rows = [r for r in _nasdaq_closes("SATS") if r[0] >= SATS["closed"]]
    except Exception as e:
        rows = []
        log(f"[skip] Nasdaq SATS closes: {e} — adding a snapshot point instead")
    if rows:
        co["pref2History"] = {"dates": [_iso_lbl(d, "%b %-d") for d, _c, _v in rows],
                              "iso": [d for d, _c, _v in rows],
                              "px": [round(c, 2) for _d, c, _v in rows],
                              "notional": [nM] * len(rows)}
        _VOL_HIST["SATS"] = [(d, v) for d, _c, v in rows]
        snap_et = datetime.datetime.now(ET)
        settled = _settled_sessions([(d, c) for d, c, _v in rows], snap_et)
        if px and len(settled) >= 2:
            quote = _quote_session(settled, px, snap_et)
            if not quote:                     # an after-hours print: the close is the quote
                p2["price"] = px = settled[-1][1]
                quote = (settled[-1][0], settled[-2][1])
            p2["prevClose"] = round(quote[1], 4)
            p2["changePct"] = round((px / quote[1] - 1) * 100, 2)
    elif px:
        # re-running within a session updates that day's point rather than adding one
        h2 = co.get("pref2History") or {"dates": [], "iso": [], "px": [], "notional": []}
        if h2["iso"] and h2["iso"][-1] == today:
            h2["px"][-1], h2["notional"][-1] = px, nM
        else:
            h2["iso"].append(today)
            h2["dates"].append(_iso_lbl(today, "%b %-d"))
            h2["px"].append(px)
            h2["notional"].append(nM)
        co["pref2History"] = h2
    co["pref2"] = p2


def _check_sats_count(text):
    """Flag the SATS constant once Strive's weekly table counts SATS shares sold.

    Its ATM opened 2026-10-26 and no filing has a SATS row yet, so there is no
    count to read the notional from; this names the filing that first has one."""
    m = re.search(_SATS_ROW + r" ([\d,]+) ([\d,]+)", text or "")
    if m and int(m.group(2).replace(",", "")) != SATS["shares"]:
        log(f"[stale] Strive's weekly table counts {m.group(2)} SATS shares but SATS['shares'] "
            f"is {SATS['shares']:,} — update it in refresh_data.py, or read the row")

def _iso_lbl(iso, fmt):
    y, m, d = iso.split("-")
    return datetime.date(int(y), int(m), int(d)).strftime(fmt)


ET = ZoneInfo("America/New_York")


def _et_date(dt):
    """ET calendar date of an aware/naive-UTC datetime, as an ISO string.

    Sessions are named by their ET date, never UTC: a refresh at 00:04 UTC is
    8:04pm the *previous* ET day, and a UTC stamp would file that evening's
    close under tomorrow's session.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(ET).date().isoformat()


def _stamp_price(co, price, chg_pct, date_et):
    """Record a quote together with the ET session it belongs to and that
    session's previous close.

    A reported day-change is only meaningful for the session it quotes, and the
    dashboard has to keep the figure live between refreshes (GitHub throttles the
    cron to a handful of runs a day). Storing `prevClose` + `priceDateET` lets the
    frontend rebase on the tape instead of unwinding a stale percentage.

    Sources zero their delta once the session is over — strategytracker reports
    +0.00% for ASST every evening — so a previous close already recorded for this
    same session is never overwritten with the price itself; that would erase the
    day's move from the after-hours refresh onward.
    """
    prev = co.get("prevClose")
    if co.get("priceDateET") != date_et:      # new session: last close is the base
        prev = None
    if chg_pct:                               # source still reporting the move
        prev = price / (1 + chg_pct / 100)
    elif prev is None:                        # first quote of a session, nothing moved yet
        prev = price
    co["prevClose"]    = round(prev, 4)
    co["priceDateET"]  = date_et
    co["dayChangePct"] = round((price / prev - 1) * 100, 2) if prev else 0.0


# The tracker's closing print lags the 4:00pm auction and it rebuilds its snapshot
# about every 15 minutes, so a session is not taken as final until well after the bell.
SESSION_OPEN_ET = datetime.time(9, 30)
SESSION_SETTLED_ET = datetime.time(16, 15)


def _settled_sessions(rows, snap_et):
    """The rows of [(iso, ...)] that are finished sessions as of snapshot time snap_et.

    The tracker's tail can't be taken at face value. From the open it grows a row
    for the running session, and from 8pm ET, when UTC rolls over, it adds a row
    dated the NEXT day that repeats the session just closed plus after-hours prints
    (on 2026-09-22 ASST's was an exact copy; MSTR's carried 30,568 more shares).
    Neither is a session. A row counts once its date is past, or once the snapshot
    was taken after that day's close. Weekend dates never count.

    Judged by the snapshot's own timestamp, not the clock this runs on: a run just
    after the bell can read a snapshot built before the closing auction printed.

    Known gap: there is no holiday calendar here, so a weekday market holiday's
    placeholder row counts once the snapshot passes the settle time that day. The
    tracker's history holds none of the last ten holidays, so the row is transient,
    but while it stands rvol, beta, the day change and ATM absorption treat it as a
    session (absorption can count the newest filed window a day early).
    """
    day = snap_et.date().isoformat()
    closed = snap_et.time() >= SESSION_SETTLED_ET
    return [r for r in rows
            if datetime.date.fromisoformat(r[0]).weekday() < 5
            and (r[0] < day or (r[0] == day and closed))]


def _quote_session(closes, price, snap_et):
    """(session, previous close) for a quote taken at snap_et, given settled [(iso, close)].

    While a session trades, the quote is its running price and the base is the last
    settled close. Any other time — pre-market, after the settle, the weekend — the
    quote should be the last settled session's close, based on the session before
    it. When it isn't (a missing row, an after-hours print), the session it belongs
    to is unknown, so None.
    """
    if snap_et.weekday() < 5 and SESSION_OPEN_ET <= snap_et.time() < SESSION_SETTLED_ET:
        return snap_et.date().isoformat(), closes[-1][1]
    if round(closes[-1][1], 2) != price:
        return None
    return closes[-1][0], closes[-2][1]


def _rvol(series, snap_et, window=30):
    """Relative volume: {mult, sessionUsd, avg30Usd, asOf} from [(iso, $vol)] ascending.

    Anchored to the last SETTLED session (see _settled_sessions), never the running
    one: dividing a part-session by full ones reads backwards — at 11:23 ET on
    2026-09-22 MSTR printed 0.55x ("quiet") against 2.15x for the session that had
    actually closed. `asOf` names the session used.

    Holding still between closes also keeps main()'s before/after no-op check
    meaningful — a figure that drifted every run would commit data.json every cron.
    """
    rows = sorted((d, v) for d, v in _settled_sessions(series, snap_et) if v and v > 0)
    if len(rows) < window + 1:
        return None
    day = rows[-1]
    avg = sum(v for _, v in rows[-1 - window:-1]) / window
    if avg <= 0:
        return None
    return {"mult": round(day[1] / avg, 2), "sessionUsd": round(day[1]),
            "avg30Usd": round(avg), "asOf": day[0]}


def _beta(stock, btc):
    """Beta of stock daily returns vs BTC daily returns over shared dates.

    Both are [(date, price)]. Returns run between consecutive dates the two have
    in common, so a Friday-to-Monday stock move is paired with BTC's whole weekend.
    That only holds if the stock series carries real sessions alone — a filled
    weekend or holiday row would pair a 0% stock move with a real BTC one.
    """
    sd, bd = dict(stock), dict(btc)
    days = sorted(set(sd) & set(bd))
    sr, br = [], []
    for i in range(1, len(days)):
        p, q = days[i - 1], days[i]
        if sd[p] and bd[p]:
            sr.append(sd[q] / sd[p] - 1)
            br.append(bd[q] / bd[p] - 1)
    n = len(br)
    if n <= 60:                       # under ~3 months of sessions the slope is noise
        return None
    mb, ms = sum(br) / n, sum(sr) / n
    var = sum((x - mb) ** 2 for x in br) / n
    cov = sum((sr[i] - ms) * (br[i] - mb) for i in range(n)) / n
    return cov / var if var else None


def fetch_strategytracker(data):
    """Refresh current metrics + real history for MSTR/ASST from strategytracker."""
    try:
        idx = get_json(TRACKER_BASE + "latest.json")
        full = get_json(TRACKER_BASE + idx["files"]["full"])
    except Exception as e:
        log(f"[skip] strategytracker failed: {e} — keeping existing values")
        return
    comps = full.get("companies", {})
    try:
        snap = datetime.datetime.fromisoformat(
            (full.get("timestamp") or idx["timestamp"]).replace("Z", "+00:00"))
    except Exception:
        snap = datetime.datetime.now(datetime.timezone.utc)
    tracker_date = _et_date(snap)
    snap_et = (snap if snap.tzinfo else snap.replace(tzinfo=datetime.timezone.utc)).astimezone(ET)
    data.pop("eurUsd", None)    # STRE's USD figure comes from strategy.com; nothing reads the rate
    hist = {}
    for tk in ("MSTR", "ASST"):
        c = comps.get(tk)
        if not c:
            continue
        pm, hd = c["processedMetrics"], c["historicalData"]
        liq = pm.get("historicalLiquidity") or {}     # session $vol + closes: rvol, beta, day change
        co = data["companies"].get(tk)
        if not co:
            continue
        try:
            closes = [(d, float(p)) for d, p in _settled_sessions(
                zip(liq.get("dates") or [], liq.get("prices") or []), snap_et) if p]
        except Exception as e:
            log(f"[skip] {tk} settled closes: {e}")
            closes = []
        co["holdings"]      = int(round(pm["latestBtcBalance"]))
        co["avgCost"]       = round(pm["avgCostPerBtc"])
        co["stockPrice"]    = round(pm["stockPrice"], 2)
        # The tracker's own delta compares its last two calendar rows, which hold the
        # same close whenever no session is trading: ASST read +0.00% on Sat 2026-09-19
        # against Friday's +6.40%. Base the move on the settled sessions instead.
        quote = _quote_session(closes, co["stockPrice"], snap_et) if len(closes) >= 2 else None
        if quote:
            session, base = quote
            _stamp_price(co, co["stockPrice"], (co["stockPrice"] / base - 1) * 100, session)
        else:
            _stamp_price(co, co["stockPrice"],
                         round(pm["stockPriceDelta"]["percent"], 2), tracker_date)
        co["sharesOutstanding"] = round(pm["latestTotalShares"] / 1e6, 2)
        co["floatSharesM"] = round(co["sharesOutstanding"] - CLASS_B_SHARES_M.get(tk, 0), 2)
        # daily shares outstanding (split-adjusted, millions) for per-date float
        _SHARES_HIST[tk] = sorted(
            (dt, mc / px / 1e6)
            for dt, mc, px in zip(hd["dates"], hd["market_cap_basic"], hd["stock_prices"])
            if mc and px)
        co["dilutedShares"] = round(pm["latestDilutedShares"] / 1e6, 2)
        # Per-share metrics. "Assumed diluted" = basic + dilution from ALL converts
        # and convertible preferred regardless of moneyness — the denominator
        # strategy.com publishes as its headline BTC-per-share. The tracker's own
        # share counts only move on filings, so keep the dilution overlay (which is
        # structural and slow-moving) and rebase it onto the live basic count.
        tr_basic = pm["latestTotalShares"] / 1e6
        # EFFECTIVE diluted, not assumed: it drops instruments the issuer flags as
        # out of the money (ASST's 26.6M warrants struck at $27 against a ~$12
        # stock). This is the basis both strategy.com and treasury.strive.com
        # publish — for MSTR the two coincide, for ASST they differ by 30%.
        tr_dil = (pm.get("latestEffectiveDilutedShares") or pm["latestDilutedShares"]) / 1e6
        co["_dilOverlayM"] = round(max(0.0, tr_dil - tr_basic), 4)
        _apply_per_share(co, tk)
        co["btcYieldYtd"]   = round(pm["btcYieldYtd"], 1)
        co["btcYieldQtd"]   = round(pm["btcYieldQuarterly"], 1)
        co["pctSupply"]     = round(co["holdings"] / data["btcSupply"] * 100, 4)
        co["navPremiumBasic"] = round(pm["navPremiumBasic"], 3)
        co["treasuryDate"]  = pm.get("latestTreasuryDate")
        # relative volume. `historicalLiquidity` ships daily_traded_values already
        # equal to volumes x prices, trading-day shaped (no weekend rows) — so this
        # needs no join against stockHistory, which is calendar-shaped and would
        # misalign by a growing offset.
        try:
            rv = _rvol(list(zip(liq.get("dates") or [], liq.get("daily_traded_values") or [])), snap_et)
            if rv:
                co["rvol"] = rv
        except Exception as e:
            log(f"[skip] {tk} rvol: {e} — keeping existing value")
        # the tracker zeroes cash/debt when it values a name on market-cap basis
        # (useEv False) — only trust it when useEv is True; else keep filing values.
        # Not for MSTR: its balance leaves out USD Cash ($5.04B vs strategy.com's $6.09B
        # on 2026-09-25); fetch_holdings and strategy.com set MSTR's cash.
        if pm.get("latestUseEv") and tk != "MSTR":
            co["cash"] = round(pm["latestCashBalance"] / 1e6)
        # preferred: live per-series notionals, prices, and the implied annual dividend
        ps = list(pm.get("preferredStocks") or [])
        sats = next((p for p in ps if p.get("ticker") == "SATS"), None)
        if tk == "ASST" and not sats and str(tracker_date) >= SATS["closed"]:
            sats = {"ticker": "SATS", "dividendRate": SATS["rate"],
                    "notionalMillions": SATS["shares"] * SATS["stated"] / 1e6,
                    "price": _cboe_last("SATS"), "priceChangePercent": 0}
            ps.append(sats)
            log(f"[SATS] not in the feed yet — carrying {SATS['shares']:,} sh "
                f"x ${SATS['stated']:,.0f} = ${SATS['shares']*SATS['stated']/1e6:,.0f}M at "
                f"{SATS['rate']}%" + (f", last ${sats['price']:,.2f}" if sats["price"] else ", no quote"))
        # whether injected or the feed's own, so the tile and chart keep updating
        # once the feed lists SATS
        if tk == "ASST" and sats:
            _asst_pref2(co, sats, tracker_date)
        if ps:
            bd = []
            kpi = _strategy_pref_notional() if tk == "MSTR" else {}
            for p in ps:
                t = p["ticker"]
                # the ticker lands in the "Preferred by Series" hover as HTML (index.html's
                # tipHTML, which escaping can't protect), so it has to be a plain ticker
                if not re.fullmatch(r"[A-Z]{2,6}", str(t or "")):
                    log(f"[skip] {tk} preferred with ticker {str(t)[:20]!r}: not a ticker")
                    continue
                notM = p.get("notionalMillions") or round((p.get("notionalUSD") or 0) / 1e6)
                if t in kpi:
                    # Strategy's own figure, which readers reconcile this page against.
                    # Its STRE moves with EUR/USD: until Oct 2026 strategy.com carried
                    # the €775M at a fixed $899M, and once it stopped, our copy of that
                    # constant pushed the STRC residual below $28M short.
                    notM = kpi[t][1]
                if t == "SATA" and _SATA.get("steps"):
                    filed = _SATA["steps"][-1][1]     # filed count x par; filings win
                    if abs(filed - notM) > 1:
                        log(f"[drift] SATA notional: tracker ${notM:,.1f}M vs filed "
                            f"${filed:,.1f}M (as of {_SATA['steps'][-1][0]}) — using filed")
                    notM = filed
                rate = p.get("dividendRate")
                lab = PREF_LABEL.get(t, "{r}%").format(r=("%g" % rate) if rate is not None else "?")
                bd.append([t, lab, round(notM), rate])     # rate: the calculator's per-series default
                if t == co.get("prefTicker"):
                    co["prefPrice"] = round(p["price"], 2)
                    co["prefChangePct"] = round(p.get("priceChangePercent") or 0, 2)
                    # daily preferred close + notional outstanding (step series from
                    # the tracker's change log; None before the first known change)
                    hp = p.get("historicalPrices") or []
                    chg = sorted((e for e in (p.get("history") or [])
                                  if e.get("effective_date") and e.get("notional_millions") is not None),
                                 key=lambda x: x["effective_date"])
                    if t == "STRC":     # the filed weeks, not the tracker's lagging log
                        chg = [{"effective_date": d, "notional_millions": n} for d, n in _strc_steps(
                            [(e["effective_date"], e["notional_millions"]) for e in chg])]
                        co["strcNotionalSteps"] = [[e["effective_date"], e["notional_millions"]] for e in chg]
                    if t == "SATA" and _SATA.get("steps"):
                        # same stall as the scalar: the tracker's change log ends at
                        # 2026-06-22, so the notional line on the preferred chart ran
                        # flat through every issuance since. Filed levels win.
                        mg = {e["effective_date"]: e["notional_millions"] for e in chg}
                        mg.update({d: n for d, n in _SATA["steps"]})
                        chg = [{"effective_date": d, "notional_millions": n}
                               for d, n in sorted(mg.items())]
                    _VOL_HIST[t] = [(q["date"], q.get("volume") or 0) for q in hp]
                    _ATM_CANDLES[t] = p.get("intradayCandles") or []
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
                        # close and volume sit on one record, so $vol needs no join. Settled
                        # here once: intraday, hp carries the running session's row too
                        try:
                            _PREF_DVOL[t] = sorted(_settled_sessions(
                                [(q["date"], (q.get("volume") or 0) * (q.get("close") or 0)) for q in hp],
                                snap_et))
                            rv = _rvol(_PREF_DVOL[t], snap_et)
                            if rv:
                                co["prefRvol"] = rv
                        except Exception as e:
                            log(f"[skip] {t} rvol: {e} — keeping existing value")
            # STRC is the only series with a live ATM and buyback programme, and the
            # tracker's notional for it runs one filing behind — on 2026-09-18 it still
            # carried the 1,420,467 shares Strategy had already retired, putting our
            # total $132M over theirs. Strategy publishes the authoritative preferred
            # total in its KPI API, so back STRC out as the residual: the other four
            # series are static between issuances, so the residual IS STRC, and this
            # self-corrects every week without us chasing buyback disclosures. With
            # each series read from strategy.com too, this only absorbs the rounding,
            # so the five rows add up to the published total; it still carries STRC
            # through a run where strategy.com's per-series read fails.
            pub_total = (_KPI.get("MSTR") or {}).get("prefTotal")
            if tk == "MSTR" and pub_total and len(bd) >= 4:
                others = sum(x[2] for x in bd if x[0] != "STRC")
                implied = pub_total - others
                for row in bd:
                    if row[0] == "STRC" and implied > 0:
                        if abs(implied - row[2]) > 1:
                            log(f"[drift] STRC notional: per-series ${row[2]:,.0f}M vs "
                                f"${implied:,.0f}M implied by strategy.com's ${pub_total:,.0f}M "
                                f"preferred total — using implied")
                        row[2] = round(implied)
            bd.sort(key=lambda x: -x[2])
            co["prefBreakdown"] = bd
            co["prefNotional"] = round(sum(x[2] for x in bd))
            # The preferred stack at market prices had no reader, and it was wrong: the
            # tracker's STRE quote (10.23) isn't a price per its €100 stated amount
            # (strategy.com: €78.88). Drop the copy earlier refreshes stored.
            co.pop("prefMarket", None)
            corr = {x[0]: x[2] for x in bd}   # FX-corrected notionals
            pref_div = sum(corr.get(p["ticker"], 0) * (p.get("dividendRate") or 0) / 100 for p in ps)
            debt_int = sum(x["principal"] * x["coupon"] / 100 for x in (co.get("debtSchedule") or []))
            co["annualObligations"] = round(pref_div + debt_int)
            # net reserve moved with the notional — rebase the per-share figures that
            # were computed above, before this block knew the preferred stack
            _apply_per_share(co, tk)
        sp = [x for x in hd["stock_prices"][-365:] if x is not None]
        if sp:
            co["week52Low"], co["week52High"] = round(min(sp), 2), round(max(sp), 2)
        # BTC-per-share history (weekly downsample), basic basis — the cleanest read
        # on whether buying outpaced issuance. The tracker's trailing share count is
        # stale between filings, so rebase the flat tail onto the live count the same
        # way the mNAV history does.
        dts, bps = hd["dates"], hd["btc_per_share"]
        bal = hd["btc_balance"]
        shs = [(bal[i]/bps[i]/1e6 if bps[i] and bal[i] else None) for i in range(len(dts))]
        true_dil = (_KPI.get(tk) or {}).get("sharesM") or co.get("sharesOutstanding")
        known = [i for i, v in enumerate(shs) if v]
        if true_dil and known:
            last = known[-1]
            if true_dil > shs[last] + 0.5:
                j0 = last
                while j0 > 0 and shs[j0-1] and abs(shs[j0-1] - shs[last]) < 0.3:
                    j0 -= 1
                span = max(1, last - j0)
                for k in range(j0, last + 1):
                    shs[k] = shs[j0] + (true_dil - shs[j0]) * (k - j0) / span
        od, ov = [], []
        def _pt(i):
            if shs[i] and bal[i]:
                od.append(_iso_lbl(dts[i], "%b '%y")); ov.append(round(bal[i]*1e8/(shs[i]*1e6)))
        for i in range(0, len(dts), 7):
            _pt(i)
        _pt(len(dts) - 1)
        co["bpsHistory"] = {"dates": od, "sats": ov, "basis": "basic"}

        # beta to BTC over the trailing year of TRADING days. historicalData has a
        # row for every calendar day, with weekends and market holidays repeating the
        # prior close, so regressing on its last 253 rows treated each of those as a
        # day the stock ignored BTC — on 2026-09-23, 81 of the 252 stock returns were
        # exactly zero, and the window spanned Jan 14 -> Sep 23 rather than a year.
        # That read 1.40 for both names against MSTR 1.49 / ASST 1.70 on sessions
        # alone. historicalLiquidity's history holds sessions only, with identical
        # closes; its tail doesn't, so `closes` above keeps settled sessions only,
        # and _beta pairs each session with BTC's move over the same interval.
        try:
            btc = [(d, p) for d, p in zip(hd["dates"], hd["btc_prices"]) if p]
            beta = _beta(closes[-253:], btc)
            if beta is not None:
                co["betaBtc"] = round(beta, 2)
        except Exception as e:
            log(f"[skip] {tk} beta: {e} — keeping existing value")

        # ---- daily EV mNAV history: (mcap + debt + pref notional − cash) / BTC NAV ----
        try:
            start = MNAV_START[tk]
            today = datetime.date.today().isoformat()
            if tk == "MSTR":
                # STRC: the SEC backfill and the weeks filed since (_strc_steps); other
                # series: 8-K step constants, extended live (append a step whenever the
                # tracker's current notional moves off the last known step; persisted in
                # data.json). Each ends on strategy.com's current figure, as the valuation
                # tiles do.
                live = co.get("histStepsLive") or {}
                series = {}
                kpi = _strategy_pref_notional()
                for p in ps:
                    t = p["ticker"]
                    lg = sorted(((e["effective_date"], e["notional_millions"])
                                 for e in (p.get("history") or [])
                                 if e.get("effective_date") and e.get("notional_millions") is not None))
                    if t == "STRC":
                        series[t] = _strc_steps(lg)
                        continue
                    cur = p.get("notionalMillions") or (p.get("notionalUSD") or 0) / 1e6
                    # one step per date, the later source winning (as for cash and debt
                    # below); a set would keep both of a day's values, and _step the larger
                    st = sorted(dict(MSTR_PREF_STEPS.get(t, []) + [tuple(x) for x in live.get(t, [])] + lg).items())
                    if st and cur and abs(st[-1][1] - cur) > 0.6:
                        st.append((today, round(cur, 1)))
                        _save_live_step(live, t, today, round(cur, 1))
                    series[t] = _end_at(st, kpi.get(t))
                # live steps only extend the filed constants; those dated before the last
                # include the retired roll-forward's and the tracker's (Jul 9–Aug 24 2026,
                # $1.1–1.6B off). live is co["histStepsLive"], so data.json drops them too
                live["cash"] = [x for x in live.get("cash", []) if x[0] > MSTR_CASH_STEPS[-1][0]]
                cash_st = sorted(dict(MSTR_CASH_STEPS + [tuple(x) for x in live["cash"]]).items())
                # anchor the newest step on strategy.com's USD Reserve + USD Cash, the
                # balance sheet the live headline uses; otherwise the chart's last point
                # drifts from it. With strategy.com down there is no step at all, rather
                # than one from co["cash"], which then holds the previous run's figure.
                cash_now = (_KPI.get(tk) or {}).get("cash") or 0
                if cash_now > 0 and abs(cash_st[-1][1] - cash_now) > 1.5:
                    cash_st.append((today, cash_now)); _save_live_step(live, "cash", today, cash_now)
                debt_st = sorted(dict(MSTR_DEBT_STEPS + [tuple(x) for x in live.get("debt", [])]).items())
                if abs(debt_st[-1][1] - co["seniorDebt"]) > 1.5:
                    debt_st.append((today, co["seniorDebt"])); _save_live_step(live, "debt", today, co["seniorDebt"])
                if live:
                    co["histStepsLive"] = live
                pref_at = lambda d: sum(_step(s, d) or 0 for s in series.values())
                cash_at = lambda d, i: _step(cash_st, d) or 0
                debt_at = lambda d, i: _step(debt_st, d) or 0
            else:
                # ASST: the tracker carries daily cash/debt (useEv basis) + the SATA log
                sata = next((p for p in ps if p["ticker"] == "SATA"), None)
                sata_st = dict((e["effective_date"], e["notional_millions"])
                               for e in ((sata or {}).get("history") or [])
                               if e.get("effective_date") and e.get("notional_millions") is not None)
                sata_st.update({d: n for d, n in _SATA.get("steps") or []})   # filings win
                sata_st = sorted(sata_st.items())
                cb, db = hd.get("cash_balance") or [], hd.get("debt") or []
                _ff = [0.0] * len(hd["dates"])          # forward-filled cash
                lastc = 0.0
                for i in range(len(hd["dates"])):
                    if i < len(cb) and cb[i]:
                        lastc = cb[i] / 1e6
                    _ff[i] = lastc
                # SATS is a claim from its closing date, at the filed constant the live
                # stack uses. Not apply_sats_proceeds' job: that stops once filings
                # cover the close, and the rows after it lost the $700M claim.
                sats_m = SATS["shares"] * SATS["stated"] / 1e6
                pref_at = lambda d: (_step(sata_st, d) or 0) + (sats_m if d >= SATS["closed"] else 0)
                cash_at = lambda d, i: _ff[i]
                debt_at = lambda d, i: (db[i] or 0) / 1e6 if i < len(db) else 0
            # Net framework (Strategy, Jul-2026): net mNAV = fully-diluted mcap /
            # (BTC NAV + cash - OTM converts - preferred). Only out-of-the-money
            # converts are debt-like claims; in-the-money converts dilute the share
            # count instead. Uses today's tranche set for the whole window, plus any
            # debt the steps carry beyond it. ASST: all debt is a claim.
            sched = co.get("debtSchedule") or []
            sched_total = sum(t["principal"] for t in sched)
            rows = [(i, d, hd["market_cap_basic"][i], hd["btc_balance"][i],
                     hd["btc_prices"][i], hd["stock_prices"][i])
                    for i, d in enumerate(hd["dates"])
                    if d >= start and hd["market_cap_basic"][i] and hd["btc_balance"][i]
                    and hd["btc_prices"][i] and hd["stock_prices"][i]]
            # the tracker's share count goes stale between filings while the ATM keeps
            # issuing: interpolate the trailing flat segment toward the live count
            # from strategy.com's KPI API so recent mcap (and both mNAV series) is right
            shs = [mc / sp / 1e6 for (_i, _d, mc, _b, _bp, sp) in rows]
            true_sh = (_KPI.get(tk) or {}).get("sharesM")
            if shs and true_sh and true_sh > shs[-1] + 0.5:
                j0 = len(shs) - 1
                while j0 > 0 and abs(shs[j0 - 1] - shs[-1]) < 0.3:
                    j0 -= 1
                span = max(1, len(shs) - 1 - j0)
                for k in range(j0, len(shs)):
                    shs[k] = shs[j0] + (true_sh - shs[j0]) * (k - j0) / span
            # Strategy divides its net figure by a fully-diluted count that is basic
            # plus in-the-money converts plus vested options/RSUs. The converts are
            # already priced per-point below; the options sliver is not in the feed,
            # so back it out of today's published count and carry it across the
            # window. Without it the history sits ~1% under the live headline and the
            # "today vs trailing average" chips inherit a standing bias.
            # base on the live basic count netDilutedShares is defined against, NOT
            # shs[-1] — the tracker's trailing count can sit a filing behind, which
            # would fold that whole gap into the overlay
            opt_sh, base_sh = 0.0, true_sh or co.get("sharesOutstanding")
            net_dil = (_KPI.get(tk) or {}).get("netSharesM") or co.get("netDilutedShares")
            if net_dil and base_sh:
                itm_now = sum(t["principal"] * t["convRate"] / 1000
                              for t in sched if co["stockPrice"] >= t["convPrice"])
                opt_sh = max(0.0, net_dil - base_sh - itm_now)
            dts_m, px_m, mnv, netv, meta = [], [], [], [], []
            for idx, (i, d, mc, b, bp, sp) in enumerate(rows):
                nav = b * bp / 1e6
                mcap = sp * shs[idx]
                claims = debt_at(d, i) + pref_at(d) - cash_at(d, i)
                ev = mcap + claims
                dts_m.append(_iso_lbl(d, "%b %-d"))
                px_m.append(round(sp, 2))
                mnv.append(round(ev / nav, 3))
                if sched:
                    # debt the steps carry beyond today's schedule is a claim too: the
                    # 2029s repurchased 2026-05-19 (they convert at $672, far out of the
                    # money), and $40M of secured loans in strategy.com's Jul 25–Aug 30 debt
                    otm = (sum(t["principal"] for t in sched if sp < t["convPrice"])
                           + max(0.0, debt_at(d, i) - sched_total))
                    itm_sh = sum(t["principal"] * t["convRate"] / 1000
                                 for t in sched if sp >= t["convPrice"])   # M shares
                else:
                    otm, itm_sh = debt_at(d, i), 0.0
                net_res = nav + cash_at(d, i) - otm - pref_at(d)
                netv.append(round(sp * (shs[idx] + itm_sh + opt_sh) / net_res, 3)
                            if net_res > 0 else None)
                meta.append({"d": d, "sp": sp, "b": b, "bp": bp, "otm": otm,
                             "cash": cash_at(d, i), "pref": pref_at(d),
                             "debt": debt_at(d, i), "itm": itm_sh,
                             "shs": shs[idx], "opt": opt_sh})
            if mnv:
                _MNAV_INPUTS[tk] = meta
                co["mnavHistory"] = {"dates": dts_m, "px": px_m, "mnav": mnv, "net": netv}
                log(f"{tk} mNAV history: {len(mnv)} pts, latest EV {mnv[-1]}x / net {netv[-1]}x")
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
# FALLBACK: daily volume when Nasdaq fails  (Yahoo Finance chart API) — see _yahoo_vol
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
                if i < tries - 1:                     # no try follows the last one
                    time.sleep(2 ** i)                # 1,2,4,8,16s between tries — ~31s total
                continue
            raise
    raise last


# --------------------------------------------------------------------------- #
# WORKING: holdings & weekly purchases  (SEC EDGAR 8-Ks)
# --------------------------------------------------------------------------- #
# SEC asks for a descriptive User-Agent with contact info (fair-access policy).
EDGAR_UA = {"User-Agent": "crypto-treasury-dashboard pete@defidevcorp.com"}
CIK = {"MSTR": "0001050446", "ASST": "0001920406"}


_EDGAR_TXT = {}


def _edgar_text(url):
    # memoised: three passes read the same 8-Ks (SATA notional, holdings, ATM
    # calibration) and EDGAR asks callers not to re-request what they already have
    if url in _EDGAR_TXT:
        return _EDGAR_TXT[url]
    req = urllib.request.Request(url, headers=EDGAR_UA)
    html = urllib.request.urlopen(req, timeout=25, context=_SSL_CTX).read().decode("utf-8", "ignore")
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"&#\d+;|&nbsp;|&#160;", " ", t)
    t = re.sub(r"\s+", " ", t)
    _EDGAR_TXT[url] = t
    return t


def _edgar_json(url):
    req = urllib.request.Request(url, headers=EDGAR_UA)
    return json.loads(urllib.request.urlopen(req, timeout=25, context=_SSL_CTX).read())


# Strategy moved the weekly BTC and ATM tables out of the 8-K body and into
# Exhibit 99.1 on 2026-10-13: the filing switched from Item 8.01 to Item 7.01
# (Regulation FD), leaving a primary document that is a cover page plus an
# exhibit list and parses to nothing. Every pass here reads the primary document,
# so the whole week went missing. Resolve each filing to whichever of its
# documents actually carries the tables.
_WEEKLY_MARKERS = ("Aggregate BTC Holdings", "bitcoin at an average price",
                   "aggregate purchase price", "Bitcoin held")
_DOC_FOR = {}
# Filings whose weekly tables may sit in an exhibit this run couldn't list or fetch.
# _filing_doc hands back the cover page for them, and reading that isn't reading the
# filing: counted as read, a failed exhibit GET would mark the 8-K ingested, clear the
# page's notice and let the workflow's gate skip the retries.
_DOC_PARTIAL = set()


def _filing_doc(base, primary, markers=_WEEKLY_MARKERS):
    """The URL inside a filing that holds the weekly tables: the primary document
    while it still does, else the exhibit it hands them off to."""
    url = base + primary
    if url in _DOC_FOR:
        return _DOC_FOR[url]
    _DOC_FOR[url] = url                       # default to the primary document
    try:
        txt = _edgar_text(url)
    except Exception:
        _DOC_PARTIAL.add(url)                 # its exhibits went unchecked
        return url
    low = txt.lower()
    if any(m.lower() in low for m in markers):
        return url
    # Only chase exhibits when the body reads like a weekly update that points at
    # one; otherwise an 8-K about anything else would cost an index read per run.
    if "99.1" not in txt or "bitcoin" not in low:
        return url
    try:                                      # Archives rejects get_json's UA
        items = _edgar_json(base + "index.json")["directory"]["item"]
    except Exception:
        _DOC_PARTIAL.add(url)
        return url
    for n in [it["name"] for it in items
              if re.search(r"ex-?99", it["name"], re.I)
              and it["name"].lower().endswith((".htm", ".html"))][:3]:
        try:
            t = _edgar_text(base + n)
        except Exception:
            _DOC_PARTIAL.add(url)
            continue
        if any(m.lower() in t.lower() for m in markers):
            _DOC_FOR[url] = base + n
            _DOC_PARTIAL.discard(url)
            break
    return _DOC_FOR[url]


def _pdate(s):
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y"):
        try:
            return datetime.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


_MSTR_SECTION_HEAD = re.compile(r"ATM (?:and BTC )?Updates?|Repurchase Program Update|BTC Updates?"
                                r"|USD Reserve Update|Financial Update|Item \d\.\d\d")


def _period_texts(text):
    """An MSTR 8-K once per reporting period, oldest first, each keeping only that
    period's tables.

    A quarter end splits a week in two, each half with its own ATM and BTC table
    (2026-04-06: Mar 30-31 and Apr 1-5), and every parser here reads the first table
    it finds. The second half went unread: its 4,871 BTC landed in the next week's
    bar, and its $174.6M raise never reached the cash roll-forward or the log. A
    period's block runs from its "During Period" to the next one or the next section
    heading; only periods in the ATM or BTC section count, so a quarter summary under
    "Financial Update" (2025-10-06) doesn't split a filing.
    """
    heads = list(_MSTR_SECTION_HEAD.finditer(text))
    marks = list(_MSTR_PERIOD.finditer(text))
    section = lambda m: next((h.group(0) for h in reversed(heads) if h.start() < m.start()), "")
    periods = list(dict.fromkeys(m.groups() for m in marks if section(m).startswith(("ATM", "BTC"))))
    if len(periods) < 2:
        return [text]
    def block_end(m):
        return min((x.start() for x in marks + heads if x.start() > m.start()), default=len(text))
    def only(period):
        kept, pos = [], 0
        for m in marks:
            if m.groups() in periods and m.groups() != period:
                kept.append(text[pos:m.start()])
                pos = block_end(m)
        return "".join(kept) + text[pos:]
    return [only(p) for p in periods]


# one sale row: "<BTC sold> $<proceeds> $<avg price>". The header before it runs past
# 200 chars in 2026-08-03/08-10, and the count can be 2 digits (32 BTC, 2026-06-01);
# stopping at "As of" keeps a dash-only row from reading the holdings row as a sale.
_BTC_SOLD_ROW = re.compile(r"BTC Sold(?:(?!As of ).){0,220}?([\d,]+)\s*(?:\(\d\))?\s*\$\s*[\d,.]+\s+\$\s*([\d,]+)")


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
    for m in _BTC_SOLD_ROW.finditer(text):
        sold += int(m.group(1).replace(",", "")) * int(m.group(2).replace(",", "")) / 1e6
    return {"raised": raised, "btcSpent": spent, "btcSold": sold}


# "As of <date> Aggregate BTC Holdings … <holdings> $<agg $B> $<avg>" — one per
# period block, so a quarter-split filing yields two. Averages may be fractional.
_AGG_BLOCKS = re.compile(
    r"As of ([A-Z][a-z]+ \d{1,2}, \d{4})\*?\s*Aggregate BTC Holdings.{0,140}?"
    r"([\d,]{6,})\s+\$\s*([\d,.]+)\s+\$\s*([\d,.]+)", re.S)
# the BTC-acquired cell of each period block ("-" on a no-purchase stretch)
_PERIOD_ACQ = re.compile(
    r"BTC (?:Acquired|Purchased).{0,160}?Average Purchase Price\s*\(\d\)\s*([\d,]+|[-–—])", re.S)


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
    # so gaps are bounded non-greedy scans rather than [^0-9]*. 2026-08-03/08-10 put
    # sale and holdings columns under one header.
    if "BTC Sold" in text:
        periods = list(re.finditer(r"During Period\s+(.+?)\s+to\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text))
        asofs = list(re.finditer(r"As of [A-Z][a-z]+ \d{1,2}, \d{4}\*?\s*(?:BTC Sold.{0,120}?)?Aggregate BTC Holdings"
                                 r".{0,120}?([\d,]{7,})\s+\$\s*[\d,.]+\s+\$\s*([\d,]+)", text))
        sold = [int(m.group(1).replace(",", "")) for m in _BTC_SOLD_ROW.finditer(text)]
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

    # Aggregate "As of" blocks. A quarter boundary splits one 8-K into TWO period
    # /"As of" pairs (2026-10-05: Sep 28-30 then Oct 1-4), so take the LAST block
    # for holdings and basis and sum the periods for the week's acquisition. The
    # same filing also started printing fractional averages ("$75,436.6") and
    # dropped the space after "$", both of which the old row regex rejected —
    # it returned None and the filing was skipped whole.
    blocks = _AGG_BLOCKS.findall(text)
    if blocks:
        holdings = int(blocks[-1][1].replace(",", ""))
        avg = round(float(blocks[-1][3].replace(",", "")))
        acquired = sum(0 if a.strip() in "-–—" else int(a.replace(",", ""))
                       for a in _PERIOD_ACQ.findall(text))
        last_end = _pdate(blocks[-1][0]) or end
        return (start, last_end, acquired, holdings, avg)

    # table row (tolerates "$ 101.3" or "$34.9", and "-" for no-purchase weeks)
    tm = re.search(r"Aggregate BTC Holdings.*?([\d,]+|-)\s+\$\s*[\d,.\-]+\s+\$\s*[\d,.\-]+"
                   r"\s+([\d,]{5,})\s+\$\s*[\d,.]+\s+\$\s*([\d,]+)", text)
    if tm:
        acquired = 0 if tm.group(1).strip() == "-" else int(tm.group(1).replace(",", ""))
        return (start, end, acquired, int(tm.group(2).replace(",", "")), int(tm.group(3).replace(",", "")))

    # Prose no-purchase week. The sentence carries the restated cost basis —
    # "...acquired at an aggregate purchase price of $63.73 billion and an average
    # purchase price of approximately $75,412 per bitcoin" — and dropping it let
    # strategytracker's own (higher) avgCostPerBtc stand unchallenged, which is how
    # the basis drifted 75,412 -> 75,678 over two purchase-free weeks.
    pm = re.search(r"holds approximately ([\d,]{5,}) bitcoin", text, re.I)
    if pm and re.search(r"did not (?:purchase|acquire)", text, re.I):
        am = re.search(r"average purchase price of approximately \$\s?([\d,]+)", text, re.I)
        avg = int(am.group(1).replace(",", "")) if am else None
        return (start, end, 0, int(pm.group(1).replace(",", "")), avg)
    return None


def _mstr_cost_basis(text):
    """(aggregate purchase price $M, average $/BTC) from an MSTR 8-K, either shape.

    Used to reconcile holdings x avgCost against the filed aggregate — the check
    that would have caught the 2026-09-11 drift on the day it happened.
    """
    pm = re.search(r"aggregate purchase price of \$\s?([\d,.]+)\s*(billion|million)"
                   r".{0,120}?average purchase price of approximately \$\s?([\d,]+)", text, re.I)
    if pm:
        agg = float(pm.group(1).replace(",", "")) * (1000 if pm.group(2).lower() == "billion" else 1)
        return agg, int(pm.group(3).replace(",", ""))
    # Table shape. On a purchase week the row carries the period columns FIRST and
    # the lifetime ones after:
    #   <bought> $<period agg $M> $<period avg>  <holdings> $<lifetime agg $B> $<lifetime avg>
    # Matching the first pair reads the week's own average as the cost basis — on
    # 2026-09-28 that meant $85,681 against a true $75,437. It only stayed hidden
    # because the preceding weeks were no-purchase weeks whose period columns are
    # "-". Anchor on the holdings figure instead: 6+ digits, with an aggregate
    # that is plausibly in billions.
    # A quarter-split filing carries two aggregate blocks; the LAST is current.
    blocks = _AGG_BLOCKS.findall(text)
    if blocks:
        _, _, agg_b, avg = blocks[-1]
        return float(agg_b.replace(",", "")) * 1000, round(float(avg.replace(",", "")))
    for m in re.finditer(r"([\d,]{6,})\s+\$\s*([\d,.]+)\s+\$\s*([\d,.]{5,})", text):
        holdings = int(m.group(1).replace(",", ""))
        agg_b = float(m.group(2).replace(",", ""))
        if holdings > 100_000 and agg_b < 1000:
            return agg_b * 1000, round(float(m.group(3).replace(",", "")))
    return None, None


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


def _mstr_usd_balances(text):
    """{"reserve": $M, "usdCash": $M or None} as a weekly MSTR 8-K states them, else None.

    Every weekly 8-K since 2026-05-26 gives the balances, in three phrasings:
    "the balance of the USD Reserve is $871 million" ("was", 2026-07-06); the
    2026-08-24 bullets "USD Reserve: $5.10 billion" / "USD Cash: $1.59 billion";
    and from 2026-08-31 one sentence, "the balances of the USD Reserve and USD
    Cash were $5.10 billion and $1.61 billion". The unit's case varies too
    ("$1.0 Billion", 2026-06-08).
    """
    amt = r"\$\s?([\d,.]+)\s*(billion|million)"
    mm = lambda m, i: round(float(m.group(i).replace(",", ""))
                            * (1000 if m.group(i + 1).lower() == "billion" else 1), 1)
    both = re.search(rf"balances of the USD Reserve and USD Cash were {amt}\s*and\s*{amt}", text, re.I)
    if both:
        return {"reserve": mm(both, 1), "usdCash": mm(both, 3)}
    reserve = (re.search(rf"USD Reserve: {amt}", text, re.I)
               or re.search(rf"balance of the USD Reserve (?:is|was) {amt}", text, re.I))
    if not reserve:
        return None
    cash = re.search(rf"USD Cash: {amt}", text, re.I)
    return {"reserve": mm(reserve, 1), "usdCash": mm(cash, 1) if cash else None}


_ATM_LABEL = {"STRC": "STRC", "STRF": "STRF", "STRK": "STRK", "STRD": "STRD", "MSTR": "common stock"}


def _atm_table(text):
    """Just the ATM Update table, so per-series rows can't match narrative prose.

    The 2026-08-24 USD Cash framework text says "repurchasing Strategy's MSTR
    Stock or preferred stock" ~1,900 chars ABOVE the table. An unscoped search
    anchored on that sentence and read $5.10 billion (the USD Reserve balance)
    as the common-stock raise — $5M reported against a real $2,006.5M.
    """
    # boundary is the section HEADING: the allocation footnote itself contains
    # "Digital Credit Securities Repurchase Program", and cutting there dropped
    # every proceeds bucket after the first
    m = re.search(r"ATM Update(.*?)(?:BTC Update|Repurchase Program Update|Item\s+7\.01|$)", text, re.S)
    return m.group(1) if m else ""


def _atm_rows(text):
    """{series: net proceeds $mm} from the ATM table. Rows look like
       '<SER> Stock <shares|-> $ <notional|-> $ <net> (n) $ <available>'
    so the row must start with a share count or a dash to be a row at all."""
    tbl, out = _atm_table(text), {}
    for ser in ("STRC", "STRF", "STRK", "STRD", "STRE", "MSTR"):
        m = re.search(rf"{ser} Stock\s+(?:[\d,]+|[-–—])\s*(.*?)"
                      rf"(?=(?:STRC|STRF|STRK|STRD|STRE|MSTR) Stock|Total)", tbl, re.S)
        if not m:
            continue
        nums = [float(x.replace(",", "")) for x in re.findall(r"\$\s*([\d,.]+)", m.group(1))]
        if len(nums) >= 2 and nums[-2] >= 0.5:     # ... net proceeds, available
            out[ser] = nums[-2]
    return out


def _atm_check(text, rows):
    """The table prints its own Total; the per-series rows must reconcile to it.

    This is the guard the 2026-08-24 filing needed: a narrative "MSTR Stock"
    mention upstream of the table made the common row read $5M against a $2,006M
    Total, and nothing complained. Any future wording change that breaks a row
    now shows up as a [drift] line instead of a quietly wrong number.
    """
    m = re.search(r"Total\s*\$\s*([\d,.]+)", _atm_table(text))
    if not m:
        return
    total, got = float(m.group(1).replace(",", "")), sum(rows.values())
    if abs(total - got) > max(1.0, total * 0.01):
        log(f"[drift] ATM rows sum to ${got:,.1f}M but the table Total is "
            f"${total:,.1f}M — a row failed to parse (rows: {rows})")


def _atm_netM(text):
    """Per-filing ATM net proceeds ($mm): preferred series vs common."""
    out = {"pref": 0.0, "common": 0.0}
    rows = _atm_rows(text)
    _atm_check(text, rows)
    for ser, v in rows.items():
        out["common" if ser == "MSTR" else "pref"] += v
    return out


# Where the ATM footnote says the net proceeds went. Match on the noun, not on
# the verb or word order: Strategy writes "fund bitcoin purchases" one week and
# "used to purchase bitcoin" another, "fund dividends on" here and "pay dividends"
# there. Keyed too tightly, a bucket silently vanishes from the summary — which is
# how the 2026-08-30 filing lost its $369.7M bitcoin and $50.7M dividend lines.
# Most specific first: a buyback phrase also contains the series name.
_ALLOC = [(r"repurchases? of (STRC|STRF|STRK|STRD|MSTR) Stock", lambda m: f"{m.group(1)} buybacks"),
          (r"dividends? on[^.]{0,40}?(STRC|STRF|STRK|STRD)", lambda m: f"{m.group(1)} dividends"),
          (r"convertible", lambda m: "convert retirement"),
          (r"dividend", lambda m: "dividends"),
          (r"bitcoin", lambda m: "bitcoin"),
          (r"USD Cash", lambda m: "USD Cash"),
          (r"USD Reserve", lambda m: "USD Reserve"),
          (r"cash balance", lambda m: "cash")]


def _alloc_label(phrase):
    for pat, lab in _ALLOC:
        m = re.search(pat, phrase)
        if m:
            return lab(m)
    return None


def _atm_allocation(text, raised):
    """['STRC buybacks $136M', 'USD Reserve $300M', 'USD Cash ~$1,570M'] from the
    footnote under the ATM table. The 'remaining net proceeds' bucket carries no
    figure of its own, so it is backed out of the total; so is 'The net proceeds',
    the wording when the whole raise went one way (from Oct 2026)."""
    tbl, parts, named = _atm_table(text), [], 0.0
    # The purpose stops at "$" too: the 2026-09-28 footnote joins its two amounts with
    # a bare "and", and the first purpose swallowed the second's "$103". Aug 2026's
    # footnotes say "in proceeds" and "were added to Strategy's cash balance".
    for m in re.finditer(r"\$\s?([\d,.]+)\s*(million|billion)\s+in (?:net )?proceeds[^.$]{0,80}?"
                         r"were (?:used|added) to ([^,.$]{0,90})", tbl):
        v = float(m.group(1).replace(",", "")) * (1000 if m.group(2) == "billion" else 1)
        lab = _alloc_label(m.group(3))
        if lab:
            parts.append(f"{lab} ${v:,.0f}M"); named += v
    rem = re.search(r"(?:remaining|The) net proceeds[^.]{0,80}?were used to ([^,.$]{0,90})", tbl)
    if rem and (lab := _alloc_label(rem.group(1))):
        left = raised - named
        approx = "~" if named else ""      # backed out of the itemised buckets, or the whole raise
        parts.append(f"{lab} {approx}${left:,.0f}M" if left > 1 else lab)
    elif parts and raised and abs(raised - named) > max(1.0, raised * 0.02):
        # every bucket is itemised (no "remaining" catch-all) yet they don't add up:
        # a purpose phrase we don't recognise got dropped on the floor
        log(f"[drift] ATM allocation buckets total ${named:,.1f}M of ${raised:,.1f}M raised "
            f"— an unrecognised purpose was skipped (got: {parts})")
    return parts


# a convertible note repurchase in prose: "agreed to repurchase approximately $1.50 billion
# aggregate principal amount of the 2029 Notes" (2026-05-15), or "... Convertible Senior
# Notes due March 15, 2030". The note is named by year (2030A) or by maturity; the gap
# before it may cross a coupon's decimal point ("0.875%").
_CONVERT_REPURCHASE = re.compile(
    r"(?:[Rr]epurchased|agreed to repurchase)\b[^.]{0,80}?\$\s?([\d,.]+)\s*(million|billion)[^.]{0,60}?"
    r"principal amount(?:[^.]|\.(?=\d)){0,80}?(?:(20\d\d[AB]?)\s+(?:[Cc]onvertible\s+)?(?:[Ss]enior\s+)?[Nn]otes"
    r"|[Cc]onvertible\s+(?:[Ss]enior\s+)?[Nn]otes\s+due\s+(?:[A-Z][a-z]+\s+\d{1,2},\s+)?(20\d\d))")


def _convert_retirement(text):
    """The activity item for a convertible note repurchase the text states, or None."""
    m = _CONVERT_REPURCHASE.search(text)
    if not m:
        return None
    v = float(m.group(1).replace(",", "")) * (1000 if m.group(2) == "billion" else 1)
    verb = "Agreed to repurchase" if m.group(0).startswith("agreed") else "Repurchased"
    return f"{verb} ~${v:,.0f}M principal of {m.group(3) or m.group(4)} convertible notes"


# The share repurchase programs' table (Jun-2026 framework). Like the ATM table it has a
# "During Period" block per period, two across a quarter end, with rows that read
# "<SER> Stock (1) <shares> $ <amount>" and dashes for a series nothing was bought of.
_MSTR_REPURCHASE = re.compile(r"Repurchase Program Update(.*?)(?:BTC Update|ATM Update|Item\s+7|$)", re.S)


def _mstr_repurchases(text):
    """[(from, to, {series: (shares, $M)})] for each period of an MSTR 8-K's Repurchase
    Program Update table, oldest first, with the dates None where a table states no
    period; [] when the filing has no such table. Dashed rows are left out."""
    sec = _MSTR_REPURCHASE.search(text)
    if not sec:
        return []
    body = sec.group(1)
    marks = list(_MSTR_PERIOD.finditer(body))
    blocks = [(m.groups(), body[m.end():marks[k + 1].start() if k + 1 < len(marks) else len(body)])
              for k, m in enumerate(marks)] or [((None, None), body)]
    out = []
    for (start, end), block in blocks:
        rows = {}
        for s in ("STRC", "STRF", "STRK", "STRD", "MSTR"):
            m = re.search(rf"{s} Stock\s*(?:\(\d\))?\s*([\d,]+)\s*\$\s*([\d,.]+)", block)
            if m:
                rows[s] = (int(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")))
        out.append((start and _pdate(start).isoformat(), end and _pdate(end).isoformat(), rows))
    return out


def _mstr_actions(text, rec, fl, whole_filing=True):
    """Readable weekly actions from an MSTR 8-K period. Only the newest period of a
    split filing (see _period_texts) carries the items that describe the filing as a
    whole — dividend rate, buybacks, balances — so they aren't listed twice."""
    items = []
    if rec[2] > 0:
        avg = round(fl["btcSpent"] * 1e6 / rec[2]) if fl["btcSpent"] else None
        items.append(f"Bought {rec[2]:,} BTC" + (f" (~${fl['btcSpent']:,.0f}M at ~${avg:,}/BTC)" if avg else ""))
    elif rec[2] < 0:
        items.append(f"Sold {-rec[2]:,} BTC for ~${fl['btcSold']:,.0f}M")
    elif re.search(r"No bitcoin purchases or sales were made", text):
        # say it outright — an omitted line reads like a parse failure, and a week
        # Strategy did NOT buy is itself the story
        items.append(f"No bitcoin bought or sold — holdings unchanged at {rec[3]:,} BTC")
    # per-series ATM sales, scoped to the ATM table (see _atm_table)
    rows = _atm_rows(text)
    raises = [f"{_ATM_LABEL[s]} ${v:,.0f}M" for s, v in rows.items() if s in _ATM_LABEL]
    if raises:
        sh = re.search(r"MSTR Stock\s+([\d,]{7,})\s*\$", _atm_table(text))
        via = f" ({', '.join(raises)}" + (f", {int(sh.group(1).replace(',','')) / 1e6:.1f}M shares)" if sh else ")")
        items.append(f"Raised ~${fl['raised']:,.0f}M net via ATM{via}")
        alloc = _atm_allocation(text, fl["raised"])
        if alloc:
            items.append("Proceeds to " + " · ".join(alloc))
    if not whole_filing:
        return items
    rm = re.search(r"dividend rate[^.]{0,200}?from ([\d.]+)% to ([\d.]+)%", text)
    if rm and "STRC" in text:
        items.append(f"{'Raised' if float(rm.group(2)) > float(rm.group(1)) else 'Cut'} STRC dividend rate "
                     f"{rm.group(1)}% → {rm.group(2)}%")
    for _start, _end, rows in _mstr_repurchases(text):
        for s, (sh, amt) in rows.items():
            if sh > 0:
                items.append(f"Repurchased {sh:,} {s} shares (~${amt:,.0f}M)")
    # convertible note retirements (no dedicated table yet — prose disclosure)
    if (retired := _convert_retirement(text)):
        items.append(retired)
    # remaining repurchase headroom — sizes what the program can still do
    # A filing that spans a quarter boundary states each programme's headroom once
    # per period — the Oct 5 8-K carries both $620.9M as of Sep 30 and $547.2M as
    # of Oct 4. Listing every match printed the stale figure first and read as two
    # separate preferred programmes, so keep one entry per programme and let the
    # last (latest period) win.
    auth = {}
    for m in re.finditer(r"\$\s?([\d,.]+)\s*(billion|million) aggregate purchase price of"
                         r"([^.]{0,60}?)remains available", text):
        v = float(m.group(1).replace(",", "")) * (1000 if m.group(2) == "billion" else 1)
        auth["MSTR" if re.search(r"MSTR|class A common", m.group(3), re.I) else "preferred"] = v
    if auth:
        items.append("Buyback capacity left: "
                     + " · ".join(f"${v:,.0f}M {k}" for k, v in auth.items()))
    bal = _mstr_usd_balances(text)
    if bal:
        items.append(f"USD Reserve ${bal['reserve']:,.0f}M"
                     + (f" · Cash ${bal['usdCash']:,.0f}M" if bal["usdCash"] is not None else ""))
    if re.search(r"establishment of\s*[\"“]USD Cash[\"”]", text):
        items.append("Established USD Cash — a flexible liquidity pool alongside the USD Reserve")
    return items


def _asst_actions(text):
    """Readable weekly actions from a Strive 8-K."""
    items = []
    m = re.search(r"purchased ([\d,]+) bitcoin at an average price of approximately \$\s?([\d,]+)", text)
    if m and int(m.group(1).replace(",", "")) > 0:
        # the filings are inconsistent about thousands separators ("1,110" vs "1800")
        n, px = (int(g.replace(",", "")) for g in m.groups())
        items.append(f"Bought {n:,} BTC at ~${px:,}/BTC avg")
    cm = re.search(r"Cash and cash equivalents \(in thousands\)\s*\$\s*([\d,]+)\s*\$\s*([\d,]+)", text)
    if cm:
        a, b = (int(cm.group(i).replace(",", "")) / 1000 for i in (1, 2))
        if abs(b - a) >= 1:
            items.append(f"Cash {'+' if b >= a else '−'}${abs(b-a):,.1f}M (${a:,.1f}M → ${b:,.1f}M)")
    sm = re.search(r"Class A common stock\s*([\d,]+)\s*([\d,]+)\s*([\d,]+)", text)
    if sm and int(sm.group(3).replace(",", "")) > 1000:
        # The table reports Class A growth as one number, but it mixes two very
        # different things: ATM sales (new capital at market) and traditional
        # warrant exercises (shares at the fixed $27 strike, already counted in
        # the overhang — dilutive but not a raise at market). Calling the whole
        # line ATM overstated the ATM by 2.1M shares in the week to Oct 2 alone.
        # The warrant row in the same table gives the exercise count exactly, so
        # back it out. The remainder is "ATM/other" rather than pure ATM: vested
        # employee awards and option exercises also land in Class A, and the
        # filing nets those into the awards rows instead of breaking them out.
        issued = int(sm.group(3).replace(",", ""))
        ex = _asst_warrant_exercises(text)
        if 0 < ex <= issued:
            items.append(
                f"Issued {issued:,} Class A shares ({ex:,} warrant exercises at "
                f"${WARRANT_STRIKE:,.0f} → ~${ex * WARRANT_STRIKE / 1e6:,.1f}M cash · "
                f"{issued - ex:,} ATM/other)")
        else:
            items.append(f"Issued {issued:,} Class A shares (ATM)")
    pm = re.search(_SATA_ROW + r"[^0-9]{0,60}([\d,]{6,})\s*([\d,]{6,})\s*([\d,]+)", text)
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


_SATA = {}
# The SATA row of Strive's weekly table, with the footnote marker a filing can add: Oct 19
# 2026 wrote "SATA Stock (6) 14,030,176 14,030,176", and every reader of the row missed it
_SATA_ROW = r"SATA Stock(?:\s*\(\d+\))?"


def _asst_sata_counts(text):
    """[(as-of date, SATA shares outstanding)] from one Strive 8-K.

    Two filing shapes: the weekly position table ("As of <d1> As of <d2>" with a
    "SATA Stock <a> <b> <delta>" row) and the pre-May-2026 prose snapshot. We want
    the ABSOLUTE counts, not the deltas _asst_items already reports — notional has
    to be rebuilt from a filed level, not accumulated from parsed changes.
    """
    out = []
    per = re.search(r"As of ([A-Z][a-z]+ \d{1,2}, \d{4}) As of ([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    m = re.search(_SATA_ROW + r" ([\d,]+) ([\d,]+)", text)
    if per and m:
        for d, v in zip(per.groups(), m.groups()):
            try:
                out.append((_pdate(d).isoformat(), int(v.replace(",", ""))))
            except Exception:
                pass
    else:
        snap = _asst_snapshot(text)
        if snap and snap[1].get("sata"):
            out.append((snap[0].isoformat(), int(snap[1]["sata"])))
    return out


# Strive's PIPE Traditional Warrants, from the 2025-05-26 subscription agreements.
# Strike is "$1.35 per share ($27 split-adjusted)" per the Q2 10-Q, after the
# 1-for-20 reverse split of 2026-02-06. They finished converting on 2026-10-13 —
# 94.3% exercised, the rest never exercised — so the running tally and the
# dashboard box that showed it are gone. What is still needed is the per-week
# exercise count, which is the only way to tell warrant conversions apart from
# ATM sales in the Class A issuance line.
WARRANT_STRIKE = 27.0

# "from the period of October 12, 2026 through October 13, 2026" — a standalone
# 8-K reports its own window rather than carrying the weekly position table.
_ASST_PROSE_PERIOD = re.compile(
    r"(?:from the period of|during the period(?: from)?)\s+([A-Z][a-z]+ \d{1,2}, \d{4})"
    r"\s+(?:through|to)\s+([A-Z][a-z]+ \d{1,2}, \d{4})", re.I)
_ASST_PROSE_BTC = re.compile(
    r"purchased ([\d,]+) bitcoin at an average price", re.I)
# Capital-structure events arrive in their own 8-K with no position table and no
# purchase, so neither the weekly parser nor the prose-purchase path sees them.
# A $1B ATM authorised over a $700M series is exactly what this log exists for.
_ASST_ATM_SIZE = re.compile(r"aggregate sales price of up to \$\s?([\d,]+)", re.I)
_ASST_ATM_COMM = re.compile(r"commission of up to ([\d.]+)\s*%", re.I)


def _asst_capital_events(text):
    """Programme-level actions from a Strive 8-K that reports no week: a preferred
    offering it closed (SATS, 2026-10-20), or an ATM it set up."""
    out = _offering_closed(text)
    if "Sales Agreement" not in text or "Item 1.01" not in text:
        return out
    m = _ASST_ATM_SIZE.search(text)
    if not m:
        return out
    amt = int(m.group(1).replace(",", "")) / 1e6
    pre = text[max(0, m.start() - 800):m.start()]      # which series it covers
    sec = ("SATS" if "SATS Stock" in pre else "SATA" if "SATA Stock" in pre else
           "Class A" if "Class A" in pre else "equity")
    c = _ASST_ATM_COMM.search(text)
    return out + [f"Established ${amt:,.0f}M {sec} ATM"
                  + (f" ({c.group(1)}% commission)" if c else "")]
# "273,493,909 traditional warrants to purchase 13,674,703 shares of Class A common
# stock were exercised for expected gross proceeds of $369.2 million". The weekly
# table cannot source this: in the week the programme closes it reports only the
# fall in warrants outstanding, which mixes exercises with the part that lapsed.
_ASST_PROSE_EX = re.compile(
    r"traditional warrants to purchase\s+([\d,]+)\s+shares of Class A"
    r"[^.]{0,80}?were exercised[^.]{0,60}?gross proceeds of \$\s?([\d,.]+)\s*(million|billion)?",
    re.I)


def _asst_warrant_exercises(text):
    """Shares converted by warrant exercise over the week a Strive 8-K covers.

    The position table carries shares underlying at both ends of the window, and
    while the warrants were live a decline could only be an exercise.
    """
    m = re.search(r"Traditional Warrants \(\d\)\s*([\d,]+)\s+([\d,]+)", text)
    if not m:
        return 0
    begin, end = (int(g.replace(",", "")) for g in m.groups())
    return max(0, begin - end)


def fetch_sata_notional(data):
    """Rebuild Strive's preferred notional from its own 8-Ks.

    prefNotional is passed through from strategytracker's `notionalMillions`, and
    that field stopped stepping on 2026-06-22 at $782.95M — it never picked up the
    Aug 21 issuance, even though our actions parser reads the very same filing
    ("Issued 441,313 SATA preferred shares", 8-K of 2026-08-24). SATA notional is
    just the filed share count x $100 par, so take it from the filings and let them
    win, the way the STRC dividend rate already does in fetch_holdings.

    Runs before fetch_strategytracker so the notional is right everywhere it feeds:
    prefBreakdown, annualObligations, both durations, net reserve and the mNAV
    series. Steps persist in data.json, so later runs stop at the first filing whose
    dates we already hold (typically one fetch).
    """
    co = data["companies"].get("ASST")
    if not co:
        return
    steps = {d: n for d, n in (co.get("sataNotionalSteps") or [])}
    known = set(steps)
    try:
        sub = _edgar_json(f"https://data.sec.gov/submissions/CIK{CIK['ASST']}.json")
    except Exception as e:
        log(f"[skip] SATA notional: {e} — keeping filed steps")
        _SATA["steps"] = co.get("sataNotionalSteps") or []
        return
    r = sub["filings"]["recent"]
    docpat = re.compile(r"^(?:asst-\d{8}\.htm|.*8k.*\.htm)$", re.I)
    scanned = 0
    for i in range(len(r["form"])):          # EDGAR lists newest first
        if r["form"][i] != "8-K" or not docpat.match(r["primaryDocument"][i] or ""):
            continue
        acc = r["accessionNumber"][i].replace("-", "")
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(CIK['ASST'])}/"
               f"{acc}/{r['primaryDocument'][i]}")
        try:
            txt = _edgar_text(url)
            got = _asst_sata_counts(txt)
        except Exception:
            txt, got = "", []
        if not scanned:                      # the newest filing
            _check_sats_count(txt)
        scanned += 1
        if got and all(d in known for d, _ in got):
            break                            # newest-first: everything older is held
        for d, sh in got:
            steps[d] = round(sh / 1e4, 4)    # shares x $100 par, in $mm
        if scanned >= 40:
            break
        time.sleep(0.12)
    co.pop("warrants", None)     # the tracker is gone; drop any stored tally
    if steps:
        co["sataNotionalSteps"] = [[d, n] for d, n in sorted(steps.items())]
        _SATA["steps"] = co["sataNotionalSteps"]
        d, n = co["sataNotionalSteps"][-1]
        log(f"[SATA] notional from filings: ${n:,.1f}M ({n * 1e4:,.0f} sh as of {d}) "
            f"from {len(steps)} filed steps, {scanned} filings scanned")


# STRC's filed weekly steps since STRC_BACKFILL, for _strc_steps
_STRC = {}


def fetch_strc_notional(data):
    """STRC's notional at the end of every week Strategy has filed since STRC_BACKFILL.

    The history took these from strategytracker's change log, which dates each change a
    week or more late, folds some weeks into the next, and on 2026-10-26 still lacked
    the Oct 5-18 buybacks ($387M). Each weekly 8-K states that week's STRC sales (the
    ATM table) and, since the Jun-2026 framework, its STRC buybacks (the Repurchase
    Program Update), so the level rolls forward from the backfill's last week at $100
    stated a share, one step per period end it moves on. Rolled from 2026-03-02 it came
    to within $0.2M of strategy.com's figure on 2026-10-26. The steps stop before the
    first week no filing covers. Nothing persists: fetch_holdings reads the same 8-Ks
    through _edgar_text's memo. Leaves _STRC empty, and the tracker's log in use, when
    no week can be read.
    """
    if not data["companies"].get("MSTR"):
        return
    start, level = STRC_BACKFILL[-1]
    try:
        rec = _edgar_json(f"https://data.sec.gov/submissions/CIK{CIK['MSTR']}.json")["filings"]["recent"]
    except Exception as e:
        log(f"[skip] STRC notional from filings: {e} — using the tracker's change log")
        return
    sold, bought = {}, {}         # (from, to) -> STRC shares; newest filing first, so a later one stands
    for i in range(len(rec["form"])):          # EDGAR lists newest first
        if rec["form"][i] != "8-K":
            continue
        filed = rec["filingDate"][i]
        if filed <= start:
            break
        base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK['MSTR'])}/{rec['accessionNumber'][i].replace('-', '')}/"
        fresh = base + rec["primaryDocument"][i] not in _EDGAR_TXT
        try:
            text = _edgar_text(_filing_doc(base, rec["primaryDocument"][i]))
            for w in _mstr_strc_atm(text):
                sold.setdefault((w["from"], w["to"]), w["shares"])
            for a, b, rows in _mstr_repurchases(text):
                if a and "STRC" in rows:
                    bought.setdefault((a, b), rows["STRC"][0])
        except Exception as e:                  # its week shows up below as a gap
            log(f"[drift] STRC notional: the 8-K filed {filed} couldn't be read: {e}")
        if fresh:
            time.sleep(0.12)
    n, steps, through, expect = level, [], None, _iso_plus(start, 1)
    for a, b, sh in sorted((a, b, sh) for (a, b), sh in sold.items() if b > start):
        if a != expect:
            log(f"[drift] STRC notional: no filed week starts {expect} (the next runs {a} to {b}) — "
                f"the filed steps stop at {through or start}")
            break
        # a quarter end can split a week's buybacks where its ATM line didn't
        back = sum(s for (x, y), s in bought.items() if a <= x and y <= b)
        if sh or back:
            n += (sh - back) * 100 / 1e6        # shares x $100 stated, in $M
            steps.append((b, round(n, 1)))
        through, expect = b, _iso_plus(b, 1)
    if not through:
        log(f"[skip] STRC notional from filings: no week since {start} could be read — "
            f"using the tracker's change log")
        return
    _STRC.update(steps=steps, through=through)
    now = _strategy_pref_notional().get("STRC")
    if now and abs(n - now[1]) > 1:
        log(f"[drift] STRC notional: the filings come to ${n:,.1f}M by {through} but strategy.com "
            f"shows ${now[1]:,.1f}M on {now[0]} — a week's sales or buybacks went unread")
    else:
        log(f"[STRC] notional from filings: ${n:,.1f}M by {through}, {len(steps)} steps since {start}")


def _strc_steps(tracker_log):
    """STRC's notional steps for the history: STRC_BACKFILL and the weeks filed since
    (fetch_strc_notional), then strategy.com's figure unless the filings are newer (a
    Monday 8-K read before strategy.com's first quote of the day). When no filed week
    could be read, the tracker's change log stands in from where it starts."""
    now = _strategy_pref_notional().get("STRC")
    if _STRC.get("through"):
        steps = STRC_BACKFILL + _STRC["steps"]
        return steps + [tuple(now)] if now and now[0] > _STRC["through"] else steps
    first = tracker_log[0][0] if tracker_log else "9999-99-99"
    return _end_at([x for x in STRC_BACKFILL if x[0] < first] + list(tracker_log), now)


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
            sub = _edgar_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
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
                    url = _filing_doc(base, r["primaryDocument"][i])
                    _FILINGS.setdefault(tk, []).append((r["filingDate"][i], url))
                    yield (url, base, r["filingDate"][i])

        if tk == "MSTR":
            t3 = {"pref": 0.0, "common": 0.0}
            strc_rate = None
            pts, seen, fetched = [], set(), 0
            newest_txt = ""          # EDGAR lists newest first, so the first hit is it
            balances = None          # likewise the newest filing that states them
            for url, base, filed in docs():
                t8 = ""
                try:
                    t8 = _edgar_text(url)
                    if url not in _DOC_PARTIAL:       # a cover page whose exhibit failed isn't read
                        _READ_8K[tk] = max(_READ_8K.get(tk, ""), filed)
                    # newest period first, as EDGAR lists filings; one unless a quarter
                    # end split the week (see _period_texts)
                    periods = [(t, _parse_mstr(t)) for t in reversed(_period_texts(t8))]
                except Exception:
                    periods = []
                fetched += 1
                # a standalone convert repurchase 8-K (2026-05-15) has no holdings table
                if not any(rec for _, rec in periods) and (retired := _convert_retirement(t8)):
                    acts.append({"d": filed, "co": "MSTR", "items": [retired], "filed": filed, "url": url})
                for k, (t, rec) in enumerate(periods):
                    if not (rec and rec[1] and rec[3] and rec[1] not in seen):
                        continue
                    seen.add(rec[1]); pts.append(rec)
                    if not newest_txt:
                        newest_txt = t
                    if k == 0 and not balances:    # a whole-filing statement, dated to its period end
                        bal = _mstr_usd_balances(t)
                        if bal:
                            balances = dict(bal, asOf=rec[1].isoformat())
                    fl = _parse_flows(t)
                    ai = _mstr_actions(t, rec, fl, whole_filing=(k == 0))
                    if ai:
                        acts.append({"d": rec[1].isoformat(), "co": "MSTR", "items": ai,
                                     "filed": filed, "url": url})
                    if rec[1] > datetime.date.today() - datetime.timedelta(days=92):
                        bd = _atm_netM(t)
                        t3["pref"] += bd["pref"]; t3["common"] += bd["common"]
                    if strc_rate is None:
                        rm = (re.search(r"dividend rate per annum on[^.]{0,140}?STRC[^.]{0,200}?to ([\d.]+)%", t8)
                              or re.search(r"maintained[^.]{0,140}?STRC[^.]{0,140}?at ([\d.]+)%", t8))
                        if rm:
                            strc_rate = float(rm.group(1))
                    if len(pts) >= max_points: break
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
            if pts[-1][1]:
                _FILED_HOLD[tk] = (pts[-1][1].isoformat(), pts[-1][3])
            cur = pts[-1][3]
            if pts[-1][4]:      # authoritative avg purchase price from the latest 8-K
                data["companies"][tk]["avgCost"] = pts[-1][4]
            # Reconcile the cost basis against the aggregate the filing states.
            # strategytracker publishes its own avgCostPerBtc and it drifts from the
            # filed figure; without this the two can diverge silently for weeks.
            agg_filed, avg_filed = _mstr_cost_basis(newest_txt) if newest_txt else (None, None)
            if agg_filed and avg_filed:
                co_ = data["companies"][tk]
                co_["avgCost"] = avg_filed
                co_["costBasisFiledM"] = round(agg_filed)
                implied = cur * avg_filed / 1e6          # $M
                if abs(implied - agg_filed) > max(50.0, agg_filed * 0.005):
                    log(f"[drift] {tk} cost basis: {cur:,} BTC x ${avg_filed:,} = "
                        f"${implied:,.0f}M but the filing states ${agg_filed:,.0f}M")
                else:
                    log(f"[cost basis] {tk}: {cur:,} BTC @ ${avg_filed:,} = ${implied/1000:,.2f}B "
                        f"(filed ${agg_filed/1000:,.2f}B)")
            co = data["companies"][tk]
            co.pop("cashFlows", None)    # the retired cash roll-forward; cashFiled carries the filed balances
            co.pop("strcRate", None)     # only that roll-forward read it
            # cash = USD Reserve + USD Cash per the newest 8-K; strategy.com's live figure
            # (same balances) replaces it in main() when it answers
            if balances:
                co["cashFiled"] = balances
            else:
                log(f"[drift] {tk} USD balances: no parsed 8-K states them — keeping the prior cashFiled")
            stated = co.get("cashFiled")
            if isinstance(stated, dict):
                total = round(stated["reserve"] + (stated["usdCash"] or 0))
                co["cash"] = total
                live = (_KPI.get(tk) or {}).get("cash") or 0
                if live > 0 and abs(live - total) > 25:   # the 8-K rounds each balance to $10M
                    log(f"[drift] {tk} cash: the 8-K balances total ${total:,}M (as of "
                        f"{stated['asOf']}) but strategy.com shows ${live:,}M")
            co["trail3m"] = {"prefMo": round(t3["pref"] / 3), "commonMo": round(t3["common"] / 3)}
            # the tracker's STRC dividendRate lags rate-change 8-Ks; the filings win
            if strc_rate:
                fixed = {"STRK": 8.0, "STRF": 10.0, "STRD": 10.0, "STRE": 10.0, "STRC": strc_rate}
                bd = co.get("prefBreakdown") or []
                pref_div = 0.0
                for row in bd:
                    if row[0] == "STRC":
                        row[1] = f"Stretch · {strc_rate:g}% var"
                    row[3:] = [fixed.get(row[0], 10.0)]
                    pref_div += row[2] * row[3] / 100
                coup = sum(x["principal"] * x["coupon"] / 100 for x in (co.get("debtSchedule") or []))
                co["annualObligations"] = round(pref_div + coup)
                log(f"MSTR STRC rate from 8-K: {strc_rate}% -> annualObligations {co['annualObligations']}")
        else:  # ASST — observation-based
            allobs, fetched = {}, 0
            cash_usd = strc_sh = None
            snaps = {}
            t3c = 0.0     # trailing-92d common $ raised (shares issued x that week's price)
            cutoff92 = datetime.date.today() - datetime.timedelta(days=92)
            _sh = data.get("stockHistory") or {}
            pxmap = dict(zip(_sh.get("dates") or [], _sh.get("ASST") or []))
            prose_buys, prose_ex, cap_evts = [], [], []
            for url, base, _filed in docs():
                try:
                    t8 = _edgar_text(url)
                    if url not in _DOC_PARTIAL:       # a cover page whose exhibit failed isn't read
                        _READ_8K[tk] = max(_READ_8K.get(tk, ""), _filed)
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
                    # A standalone 8-K (the Oct 14 2026 exercise/purchase notice)
                    # states a purchase and its window but carries no position
                    # table, so it yielded no observation and the whole entry was
                    # dropped — leaving holdings a purchase behind until the next
                    # Monday table. Hold it and resolve it once allobs is built;
                    # the scan runs newest-first, so the level it builds on has
                    # not been read yet.
                    ce = _asst_capital_events(t8)
                    if ce:
                        cap_evts.append({"d": _filed, "co": "ASST", "items": ce,
                                         "filed": _filed, "url": url})
                    if ai and not obs_dates and not sn:
                        per = _ASST_PROSE_PERIOD.search(t8)
                        nb = _ASST_PROSE_BTC.search(t8)
                        ex = _ASST_PROSE_EX.search(t8)
                        if per and ex:
                            e0, e1 = _pdate(per.group(1)), _pdate(per.group(2))
                            if e1:
                                g = float(ex.group(2).replace(",", "")) * (
                                    1000 if (ex.group(3) or "").lower() == "billion" else 1)
                                prose_ex.append({"end": e1,
                                                 "shares": int(ex.group(1).replace(",", "")),
                                                 "grossM": round(g, 1)})
                        if per and nb:
                            d0, d1 = _pdate(per.group(1)), _pdate(per.group(2))
                            if d0 and d1:
                                prose_buys.append({"start": d0, "end": d1, "items": ai,
                                                   "btc": int(nb.group(1).replace(",", "")),
                                                   "url": url, "filed": _filed})
                    if ai and obs_dates:
                        # carry the document the items were actually read from. Two
                        # 8-Ks can share a filing date — Oct 13 brought the weekly
                        # update and a SATA certificate amendment — and the link
                        # fallback below can only sort by date, so it picked the
                        # wrong one of the pair.
                        acts.append({"d": max(obs_dates).isoformat(), "co": "ASST",
                                     "items": ai, "filed": _filed, "url": url})
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
                # A bare pass here hid a NameError for a whole run: a deleted regex
                # left the prose-purchase branch broken and 2,822 BTC quietly went
                # missing while every other number looked right. Say something.
                except Exception as e:
                    log(f"[skip] ASST 8-K {_filed}: {type(e).__name__}: {e}")
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
            # The week a warrant programme closes, the position table reports only
            # the fall in warrants outstanding, which mixes exercises with the part
            # that simply lapsed — so the Class A line read "13,677,703 (ATM)" for
            # the week to Oct 16 when 13,674,703 of it was warrants converting at
            # $27 and essentially none was ATM. The standalone notice states the
            # exercises outright; apply it to whichever week contains it.
            for pe in prose_ex:
                cands = [a for a in acts if a["co"] == "ASST" and a["d"] >= pe["end"].isoformat()]
                if not cands:
                    continue
                tgt = min(cands, key=lambda a: a["d"])
                for k, it in enumerate(tgt["items"]):
                    m = re.match(r"Issued ([\d,]+) Class A shares \(ATM\)$", it)
                    if not m:
                        continue
                    tot = int(m.group(1).replace(",", ""))
                    if not 0 < pe["shares"] <= tot:
                        continue
                    rest = tot - pe["shares"]
                    tgt["items"][k] = (
                        f"Issued {tot:,} Class A shares ({pe['shares']:,} warrant exercises "
                        f"at ${WARRANT_STRIKE:,.0f} → ~${pe['grossM']:,.1f}M cash"
                        + (f" · {rest:,} ATM/other)" if rest else ")"))
                    log(f"[ASST] week to {tgt['d']}: {pe['shares']:,} of {tot:,} Class A "
                        f"shares were warrant exercises, not ATM")

            # Programme announcements get their own entry, dated to the day they
            # were made rather than to a week they do not cover. If a weekly entry
            # already sits on that date, add to it rather than replacing it.
            for ev in cap_evts:
                same = next((a for a in acts if a["co"] == "ASST" and a["d"] == ev["d"]), None)
                if same:
                    same["items"] = same["items"] + [i for i in ev["items"] if i not in same["items"]]
                else:
                    acts.append(ev)
                log(f"[ASST] capital event {ev['d']}: {'; '.join(ev['items'])}")

            # Fold in purchases that only a prose 8-K reported. Carry the last
            # filed level forward by the stated amount, but only when that level
            # sits immediately before the window — a derived level is worth having
            # for the few days before the next table, not worth guessing across a
            # gap where an unread filing could also have moved holdings.
            for pb in sorted(prose_buys, key=lambda x: x["end"]):
                prior = [d for d in allobs if d < pb["end"]]
                if not prior:
                    continue
                last = max(prior)
                if (pb["start"] - last).days > 7 or pb["end"] in allobs:
                    continue
                allobs[pb["end"]] = allobs[last] + pb["btc"]
                acts.append({"d": pb["end"].isoformat(), "co": "ASST", "items": pb["items"],
                             "filed": pb["filed"], "url": pb["url"]})
                log(f"[ASST] {pb['btc']:,} BTC from the {pb['filed']} notice "
                    f"({pb['start']} to {pb['end']}): {allobs[last]:,} -> {allobs[pb['end']]:,}")

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
            _FILED_HOLD[tk] = (items[-1][0].isoformat(), holds[-1])
            _FILED_DATES[tk] = [d.isoformat() for d, _ in items]
            cur = holds[-1]

        data["companies"][tk]["holdings"] = cur
        data["companies"][tk]["pctSupply"] = round(cur / data["btcSupply"] * 100, 4)
        ye = (data["companies"][tk].get("yearEnd") or {}).get(str(cutoff.year - 1))
        if ye is not None:
            data["companies"][tk]["netChangeYtd"] = cur - ye
        log(f"{tk}: {len(weekly[tk]['dates'])} points via EDGAR ({weekly[tk]['dates'][0]} -> "
            f"{weekly[tk]['dates'][-1]}), latest {cur:,} BTC")
    if "MSTR" in weekly or "ASST" in weekly:
        # carry over every series this run didn't rebuild: DFDV's comes from fetch_dfdv,
        # not EDGAR, and an issuer whose EDGAR read failed keeps its last series rather
        # than losing it because the other issuer's succeeded
        kept = {k: v for k, v in (data.get("weekly") or {}).items() if k not in weekly}
        for k in kept.keys() & CIK.keys():
            log(f"[keep] weekly {k}: EDGAR rebuild failed — keeping the last series")
        weekly.update(kept)
        data["weekly"] = weekly
    if acts:
        # link each action to its source filing: the earliest 8-K filed on or after
        # the period it covers is the one that disclosed it. MSTR's already carry
        # theirs: a split week's first half ends mid-week, and another 8-K (a STRC
        # dividend notice, say) can land before the weekly one that reports it
        for a in acts:
            cand = sorted(x for x in _FILINGS.get(a["co"], []) if x[0] >= a["d"])
            if cand and not a.get("url"):
                a["filed"], a["url"] = cand[0]
        _merge_actions(data, acts)
        linked = sum(1 for a in data["actions"] if a.get("url"))
        log(f"[actions] {len(data['actions'])} entries, {linked} linked to a filing")


ACTIONS_KEPT = 160      # the log's newest entries, across all three issuers


def _merge_actions(data, acts):
    """Fold this run's actions into the stored log by (date, issuer), keeping a link a
    stored entry already had, so weeks this run didn't re-read stay listed."""
    old = {(a["d"], a["co"]): a for a in (data.get("actions") or [])}
    for a in acts:
        prev = old.get((a["d"], a["co"])) or {}
        if not a.get("url") and prev.get("url"):
            a["filed"], a["url"] = prev.get("filed"), prev["url"]
        old[(a["d"], a["co"])] = a
    data["actions"] = sorted(old.values(), key=lambda a: a["d"], reverse=True)[:ACTIONS_KEPT]


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


def _nasdaq_rows(sym, days=420):
    """Nasdaq's daily history rows for sym, newest first (split-adjusted)."""
    frm = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    url = (f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=stocks"
           f"&limit=9999&fromdate={frm}&todate={datetime.date.today().isoformat()}")
    req = urllib.request.Request(url, headers=NASDAQ_UA)
    raw = urllib.request.urlopen(req, timeout=25, context=_SSL_CTX).read().decode("utf-8", "ignore")
    return ((json.loads(raw)["data"] or {}).get("tradesTable") or {}).get("rows") or []


def _nasdaq_vol(sym, days=420):
    """Daily share volume [(iso date, volume), ...] from Nasdaq (split-adjusted)."""
    out = []
    for r in _nasdaq_rows(sym, days):
        d = datetime.datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat()
        v = str(r.get("volume") or "").replace(",", "")
        if v.isdigit() and int(v):
            out.append((d, int(v)))
    return sorted(out)


def _nasdaq_closes(sym, days=420):
    """Daily [(iso date, close, volume), ...] from Nasdaq, oldest first."""
    out = []
    for r in _nasdaq_rows(sym, days):
        try:
            d = datetime.datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat()
            c = float(str(r.get("close") or "").replace("$", "").replace(",", ""))
            v = int(str(r.get("volume") or "0").replace(",", "") or 0)
        except ValueError:
            continue
        out.append((d, c, v))
    return sorted(out)


def _yahoo_vol(symbol):
    """Daily share volume [(iso date, volume), ...] from Yahoo's 1y chart."""
    res = _yahoo_chart(symbol)["chart"]["result"][0]
    ts = res["timestamp"]
    vols = res["indicators"]["quote"][0]["volume"]
    return sorted((datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date().isoformat(), int(v))
                  for t, v in zip(ts, vols) if v)


def _dtc10(sih, volhist):
    """Days to cover on the trailing 10-trading-day average volume at each settlement."""
    vols = sorted(volhist)
    out = []
    for iso, s in zip(sih["iso"], sih["si"]):
        past = [v for d, v in vols if d <= iso][-10:]
        avg = sum(past) / len(past) if len(past) >= 5 else None
        out.append(round(s / avg, 2) if avg else None)
    return out


def _dtc_live(sih, volhist):
    """Live DTC: latest reported shorts / the 10 trading days of volume ending today."""
    last = sorted(volhist)[-10:]
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
        v = str(r["interest"]).replace(",", "")
        if not v.isdigit():     # a settlement with no figure reads "N/A" (CHAD's first, 09/15/2026)
            continue
        shares = int(v)
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
                try:                                   # trailing-10d days to cover
                    try:
                        vols = _nasdaq_vol(sym)
                    except Exception:
                        vols = _yahoo_vol(sym)
                    sih["dtc10"] = _dtc10(sih, vols)
                    sih["dtcLive"] = _dtc_live(sih, vols)
                except Exception as e:
                    log(f"[skip] {sym} volume for DTC-10d: {e} — falling back to Nasdaq DTC")
                co["shortInterest"] = sih
                co["daysToCover"] = sih["dtc"][-1]
                log(f"[short interest] {sym}: {len(sih['dtc'])} pts, latest DTC {sih['dtc'][-1]}, "
                    f"{pct[-1]}% of float")
        except Exception as e:
            log(f"[skip] short interest {sym}: {e} — keeping existing values")
        p2 = co.get("pref2") or {}
        for key, sym, hist, stated in (
                ("prefShortInterest", co.get("prefTicker"), co.get("prefHistory"), co.get("prefStated")),
                ("pref2ShortInterest", p2.get("ticker"), co.get("pref2History"), p2.get("stated"))):
            if not sym:
                continue
            try:
                sih = _pref_short_interest(sym, hist or {}, stated or 100)
                if sih:
                    co[key] = sih
            except Exception as e:
                log(f"[skip] short interest {sym}: {e} — keeping existing values")


def _pref_short_interest(sym, hist, stated):
    """Nasdaq short interest for one preferred series, or None before its first figure.

    % of float: the float is notional outstanding ÷ stated amount ($100; CHAD $10) on
    each settlement date, from the series' own notional history."""
    sih = _nasdaq_si(sym)
    if not sih:
        return None
    steps = [(d, n) for d, n in zip(hist.get("iso") or [], hist.get("notional") or []) if n]
    pct = []
    for iso, s in zip(sih["iso"], sih["si"]):
        ns = [n for d2, n in steps if d2 <= iso]
        pct.append(round(s / (ns[-1] * 1e6 / stated) * 100, 2) if ns else None)
    sih["pctFloat"] = pct
    if sym in _VOL_HIST:
        sih["dtc10"] = _dtc10(sih, _VOL_HIST[sym])
        sih["dtcLive"] = _dtc_live(sih, _VOL_HIST[sym])
    log(f"[short interest] {sym}: {len(sih['dtc'])} pts, latest {sih['si'][-1]:,} sh "
        f"({pct[-1]}% of float)")
    return sih


# live KPI snapshot from strategy.com, shared with the history builder
_KPI = {}
# live BTC price, shared with the per-share helper
_BTC_PX = {}
MSTR_PREF_SERIES = ("STRC", "STRF", "STRK", "STRD", "STRE")


def _strategy_pref_notional():
    """{ticker: (session date, $M)}: each preferred series' notional as strategy.com
    publishes it, read once per run. This is the figure Strategy's own preferred
    total adds up, with STRE's €775M at today's EUR/USD. A series whose read fails
    is left out, and the tracker's figure stands in for it."""
    if "notional" not in _KPI:
        out = {}
        for t in MSTR_PREF_SERIES:
            try:
                k = get_json(f"https://api.strategy.com/btc/{t.lower()}KpiData")[0]
                day = datetime.datetime.strptime(k["timeStamp"], "%m/%d/%Y %I:%M %p").date().isoformat()
                usd_m = float(k["notional"]) / 1e6
                if usd_m > 0:
                    out[t] = (day, round(usd_m, 2))
            except Exception as e:
                log(f"[skip] strategy.com {t} notional: {e} — using the tracker's")
        _KPI["notional"] = out
    return _KPI["notional"]


def _apply_per_share(co, tk):
    """Recompute sats-per-share on the freshest basic count we have.

    Three denominators, matching what the issuers publish:
      basic     — shares outstanding
      diluted   — basic + effective dilution overlay; this is "BTC Per Share" on
                  strategy.com and "Sats Per Diluted Share" on treasury.strive.com
      net       — bitcoin left for common AFTER senior claims, per diluted share;
                  strategy.com's "Net BTC Per Share". Uses a narrower count (below).
    For MSTR the first two are overwritten with strategy.com's own published
    figures in fetch_strategy_kpi, so we never drift from the source.
    """
    live = (_KPI.get(tk) or {}).get("sharesM") or co.get("sharesOutstanding")
    overlay = co.get("_dilOverlayM") or 0.0
    if not (live and co.get("holdings")):
        return
    sats = co["holdings"] * 1e8
    co["satsPerShareBasic"] = round(sats / (live * 1e6))
    co["assumedDilutedShares"] = round(live + overlay, 2)
    co["satsPerShareDiluted"] = round(sats / ((live + overlay) * 1e6))
    # Net BTC per share uses a NARROWER denominator than the gross figure: fully
    # diluted rather than assumed diluted. Converts in the money at today's price count
    # as shares and the rest as debt, as netParts on the page does; STRK's conversion
    # shares (0.1 MSTR share per $100-par STRK, a $1,000 strike, so notional $M / 1000)
    # drop out, since STRK is counted in the preferred. For MSTR that is ~429.8M vs
    # 450.1M, the count strategy.com divides by; ASST has neither, so the two coincide.
    # With no price every convert counts as shares.
    px = co.get("stockPrice")
    itm, otm = [], []
    for t in co.get("debtSchedule") or []:
        if t.get("convPrice"):
            (otm if px and px < t["convPrice"] else itm).append(t)
    otm_sh = sum(t["principal"] * t["convRate"] / 1000 for t in otm)
    strk_sh = sum(row[2] for row in co.get("prefBreakdown") or [] if row[0] == "STRK") / 1000
    co["netDilutedShares"] = round(live + overlay - otm_sh - strk_sh, 2)
    btc = _BTC_PX.get("usd") or 0
    debt_claims = (co.get("seniorDebt") or 0) - sum(t["principal"] for t in itm)
    net_res = (co["holdings"] * btc / 1e6) + (co.get("cash") or 0) \
        - debt_claims - (co.get("prefNotional") or 0)
    if btc and net_res > 0:
        co["netSatsPerShare"] = round((net_res * 1e6 / btc) * 1e8 / (co["netDilutedShares"] * 1e6))


def fetch_strategy_kpi(data):
    """Authoritative MSTR balance-sheet inputs from strategy.com's own KPI API
    (open, no auth): market cap -> current basic shares, convertible debt, and
    USD reserve derived via the EV identity (cash = mcap + debt + pref - EV).
    Called before fetch_strategytracker (so the history builder and step series
    see fresh values) and again after fetch_holdings (which sets cash from the
    8-K balances, and the live figure wins)."""
    try:
        k = _KPI.get("_raw") or get_json("https://api.strategy.com/btc/mstrKpiData")[0]
        _KPI["_raw"] = k
        num = lambda s: float(str(s).replace(",", ""))
        co = data["companies"]["MSTR"]
        px, mcap = num(k.get("ufPrice") or k["price"]), num(k["marketCap"])
        debt, pref, ev = num(k["debt"]), num(k["pref"]), num(k["entVal"])
        cash = round(mcap + debt + pref - ev)
        if px > 0 and mcap > 0:
            co["sharesOutstanding"] = round(mcap / px, 2)
            co["floatSharesM"] = round(co["sharesOutstanding"] - CLASS_B_SHARES_M["MSTR"], 2)
            # strategy.com carries the official 4:00pm consolidated close; the tracker
            # feed lags it by a few minutes after the closing auction settles
            co["stockPrice"] = round(px, 2)
            try:
                # priceVarPerc arrives signed, with a `negative` flag alongside it;
                # honour the flag so a source-side sign drop can't invert the move
                chg = abs(float(str(k["priceVarPerc"]).replace(",", "")))
                if k.get("negative"):
                    chg = -chg
                # timeStamp is ET ("08/28/2026 02:04 PM") — the session this quote is in
                try:
                    d = datetime.datetime.strptime(k["timeStamp"], "%m/%d/%Y %I:%M %p").date().isoformat()
                except Exception:
                    d = _et_date(datetime.datetime.now(datetime.timezone.utc))
                _stamp_price(co, co["stockPrice"], round(chg, 2), d)
            except Exception:
                pass
        co["seniorDebt"] = round(debt)
        if cash > 0:
            co["cash"] = cash
        _KPI["MSTR"] = {"sharesM": co["sharesOutstanding"], "debt": round(debt), "cash": cash,
                        "prefTotal": round(pref)}   # authoritative preferred total
        _apply_per_share(co, "MSTR")      # rebase per-share onto the live count
        # Strategy publishes BTC-per-share and Net-BTC-per-share itself; take those
        # verbatim so our headline can never drift from strategy.com/btc.
        try:
            r = (get_json("https://api.strategy.com/btc/bitcoinKpis") or {}).get("results") or {}
            if r.get("satsPerShare"):
                co["satsPerShareDiluted"] = round(float(r["satsPerShare"]))
            if r.get("netSatsPerShare") and r.get("netBtcReserve") and r.get("ufPrice"):
                co["netSatsPerShare"] = round(float(r["netSatsPerShare"]))
                # back out the fully-diluted count they divide by, so the frontend
                # can keep the figure live as BTC moves instead of freezing it
                net_btc = float(r["netBtcReserve"]) / float(r["ufPrice"])
                co["netDilutedShares"] = round(net_btc * 1e8 / float(r["netSatsPerShare"]) / 1e6, 2)
                # stash it: fetch_strategytracker calls _apply_per_share, which resets
                # the field to our own estimate of this count (on the tracker's price
                # and overlay), and the mNAV history builder should use theirs
                _KPI.setdefault("MSTR", {})["netSharesM"] = co["netDilutedShares"]
            # debtByBN is convertible debt as a % of BTC NAV *after* the USD assets
            # offset it — the figure Strategy markets as "Net Leverage" (it printed
            # 0.0% on 2026-08-30, when USD assets first fully covered the debt).
            for src, dst in (("mNav", "netMnavPub"), ("amplification", "amplificationPub"),
                             ("btcBreakevenArr", "breakevenArrPub"),
                             ("bitcoinHurdleArr", "hurdleArrPub"), ("debtByBN", "netLeveragePub"),
                             ("btcFailureArr", "floorArrPub"), ("totalDuration", "creditDurationPub")):
                if r.get(src) is not None:
                    co[dst] = round(float(r[src]), 4)
            log(f"[strategy.com] MSTR published: {co['satsPerShareDiluted']:,} sats/sh, "
                f"net {co.get('netSatsPerShare',0):,} sats/sh, mNAV {co.get('netMnavPub')}")
        except Exception as e:
            log(f"[skip] strategy.com bitcoinKpis: {e} — using our own per-share math")
        log(f"[strategy.com] MSTR: {co['sharesOutstanding']}M sh, debt ${debt:,.0f}M, "
            f"USD reserve ${cash:,}M, {co.get('satsPerShareDiluted','?'):,} sats/sh diluted")
    except Exception as e:
        log(f"[skip] strategy.com KPI: {e} — keeping existing values")


CE_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
         "Origin": "https://chartexchange.com"}


def _ce_borrow(sym):
    """Latest IBKR indicative borrow fee for one symbol, via ChartExchange.
    The borrow-fee page server-renders the newest reading of the IBKR shortstock
    file (updated ~15 min) as a summary line; ?source=ib pins IBKR, since the page
    can show Robinhood's rate instead. The page used to embed a cxuid for a second
    request to /xhr/tabledata/, until ChartExchange's Oct 2026 rebuild dropped it."""
    page = f"https://chartexchange.com/symbol/nasdaq-{sym.lower()}/borrow-fee/?source=ib"
    req = urllib.request.Request(page, headers={**CE_UA, "Referer": "https://chartexchange.com/"})
    html = urllib.request.urlopen(req, timeout=25, context=_SSL_CTX).read().decode("utf-8", "ignore")
    # As of <strong>2026-10-25 08:52:43 AM EDT</strong>, there were <strong>10,000,000</strong>
    # shares available with a fee of <strong>0.25<!-- -->%</strong>.
    m = re.search(r'bf-summary">As of <strong>([^<]+)</strong>, there were <strong>([\d,]+)</strong>'
                  r' shares available with a fee of <strong>([\d,.]+)', html)
    if not m:
        raise ValueError("borrow-fee summary not found in page")
    as_of = datetime.datetime.strptime(m.group(1).rsplit(" ", 1)[0], "%Y-%m-%d %I:%M:%S %p")
    return {"pct": round(float(m.group(3).replace(",", "")), 2),
            "avail": int(m.group(2).replace(",", "")),
            "asOf": as_of.strftime("%Y-%m-%d %H:%M:%S")}      # ET, the page's own clock


# --------------------------------------------------------------------------- #
# preferred-equity ATM tracker
# --------------------------------------------------------------------------- #
# These preferreds are engineered to trade at a $100 stated amount, and the ATM
# can only issue while the stock is at/above that level. So dollar volume printed
# at-or-above par is a proxy for how much the company could have sold, and a
# "capture rate" maps that proxy onto what it actually sold.
#
# Public trackers threshold at exactly $100. That works for a preferred trading
# clearly above par, but SATA hugs par to the cent, and a strict cutoff misses
# most of the real issuance: for the week of Aug 14-21 2026 Strive issued 441,313
# SATA shares (~$44.1M) while volume strictly >= $100.00 was only $10.4M (a 426%
# implied capture, i.e. the proxy was undercounting ~4x). Volume at >= $99.95 was
# $44.3M against that $44.1M actual — a ~100% capture. So we threshold a nickel
# below par and calibrate the capture rate per security against the filings.
ATM_PAR = 100.0
ATM_THRESHOLD = 99.95          # a nickel below par — see note above
ATM_DEFAULT_CAPTURE = 0.75     # prior when we have no confirmed week yet
                               # (bitcointreasuries.net publishes 74.4%)
ATM_SPAN_DAYS = 182            # absorption covers the newest 26 weeks of filed windows...
ATM_LOOKBACK_DAYS = 200        # ...so read a little further back, to find the one that closes them
ATM_MAX_FILINGS = 60           # 8-Ks per company; 200 days of MSTR's come to about 40
ATM_MAX_WINDOW_DAYS = 15       # longest window filed: MSTR Nov 17-30 2025 (13 days); longest
                               # a filing trails a window's end: 6 days (MSTR 2026-04-06)


def _atm_daily(candles):
    """[(iso, total $vol, ATM-eligible $vol)] per session from 15-min candles."""
    by = {}
    for c in candles or []:
        v = c.get("volume") or 0
        lo, hi = c.get("low"), c.get("high")
        if not (v and lo and hi):
            continue
        mid = (hi + lo) / 2
        row = by.setdefault(c["date"], [0.0, 0.0])
        row[0] += v * mid
        if lo >= ATM_THRESHOLD:                       # whole bar printed at/above
            row[1] += v * mid
        elif hi > ATM_THRESHOLD:                      # straddles — prorate by range
            row[1] += v * ((hi - ATM_THRESHOLD) / (hi - lo)) * ((hi + ATM_THRESHOLD) / 2)
    return sorted((d, r[0], r[1]) for d, r in by.items())


_DATE = r"([A-Z][a-z]+ \d{1,2}, \d{4})"
_DASH = "[-–—]"
# A week with no sales often gets no table at all, just a sentence, and under at least
# three headings ("ATM Update", "ATM Updates", "ATM and BTC Update for the Period ..."),
# so this one searches the whole filing. 8-K of 2026-09-21: "...during the period between
# September 14, 2026 and September 20, 2026, Strategy did not sell any shares under its
# at-the-market offering program." Six of the 27 weeks filed from Mar 23 to Sep 20 2026
# read this way.
_MSTR_ATM_NIL = re.compile(
    r"[Pp]eriod (?:between|from) " + _DATE + r" (?:and|to|through) " + _DATE +
    r",?[^.]{0,60}?did not sell any shares under (?:its|the) at.the.market offering program")
_MSTR_PERIOD = re.compile(r"During Period " + _DATE + r" to " + _DATE)    # one period's table
# Tolerant on purpose: an optional footnote "(1)", an optional description ending
# "Preferred Stock" between the name and the numbers, and blank or dashed cells. The
# 2026-03-09 filing wrote "STRC Stock Variable Rate Series A Perpetual Stretch Preferred
# Stock 3,776,205 $ 377.6 $ 377.1 $ 3,158.0" and left zero cells blank.
_MSTR_STRC_ROW = re.compile(
    r"STRC Stock(?:\s*\(\d\))?\s+(?:[A-Za-z%.\d ]{0,80}?Preferred Stock\s+)?"
    rf"([\d,]+|{_DASH})?\s*\$\s*([\d,.]+|{_DASH})?\s*\$\s*([\d,.]+|{_DASH})?\s*\$\s*([\d,.]+)")
# A zero week can also print the capacity cell alone. 8-K of 2025-12-01: "STRC Stock
# $ 4,042.4 Variable Rate Series A Perpetual Stretch Preferred Stock STRK Stock ...".
# The lookahead keeps it off a normal row with its shares cell left blank.
_MSTR_STRC_CAPACITY_ONLY = re.compile(r"STRC Stock(?:\s*\(\d\))?\s+\$\s*([\d,.]+)(?![\d,.]|\s*\$)")
# "This filing reports on the ATM", whether or not a window could be read from it —
# a hit with no window is format drift, not a filing about something else. Each looks
# for two independent marks, so rewording one is a loud miss rather than a quiet fall
# back to the week before. Strategy's heading has read four ways since Aug 2025 ("ATM
# Update", "ATM Updates", "ATM and BTC Update", "... for the Period"), so its second mark
# is the "During Period" line every weekly table carries; Strive's are the table's header
# pair and its SATA row, footnoted or not (_SATA_ROW; "As of Sept. 4" would slip the first). In
# the 80 newest 8-Ks of either company, none fires on a filing that isn't an ATM report.
_MSTR_ATM_SECTION = re.compile(r"ATM (?:and BTC )?Updates?|did not sell any shares under"
                               r"|During Period [A-Z][a-z]+ \d{1,2}, \d{4} to")
_ASST_ATM_SECTION = re.compile(r"As of [A-Z][a-z]+ \d{1,2}, \d{4} As of [A-Z][a-z]+ \d{1,2}, \d{4}"
                               r"|" + _SATA_ROW + r"\s+[\d,]{6,}")


def _mstr_strc_atm(text):
    """Every STRC window an MSTR 8-K reports: [{from, to, shares, proceedsM, capacityM}].

    Each "During Period" block of the ATM table is one window, and a quarter end splits
    a week into two (2026-04-06: Mar 30-31 and Apr 1-5). Zero weeks may be prose only;
    where a week appears both ways the table wins, since it states capacity. A table
    period with no readable STRC row raises: that filing is unparsed, not a zero week.
    """
    iso = lambda s: _pdate(s).isoformat()
    num = lambda x: 0.0 if not x or re.fullmatch(_DASH, x) else float(x.replace(",", ""))
    found = {}
    for m in _MSTR_ATM_NIL.finditer(text):
        found[(iso(m.group(1)), iso(m.group(2)))] = {"shares": 0, "proceedsM": 0.0, "capacityM": None}
    tbl = _atm_table(text)
    periods = list(_MSTR_PERIOD.finditer(tbl))
    for k, per in enumerate(periods):
        block = tbl[per.end():periods[k + 1].start() if k + 1 < len(periods) else len(tbl)]
        row = _MSTR_STRC_ROW.search(block)
        if row:
            shares, _notional, net, available = row.groups()
        elif capacity_only := _MSTR_STRC_CAPACITY_ONLY.search(block):
            shares, net, available = None, None, capacity_only.group(1)
        else:
            raise ValueError(f"no STRC row for {per.group(1)} to {per.group(2)}")
        found[(iso(per.group(1)), iso(per.group(2)))] = {
            "shares": int(num(shares)), "proceedsM": num(net), "capacityM": num(available)}
    return [{"from": start, "to": end, **sale} for (start, end), sale in found.items()]


def _asst_sata_atm(text):
    """The SATA window of a Strive 8-K's weekly holdings table, as a list of at most one.

    Proceeds are the share-count change x $100 par; the filing states no dollars.
    """
    per = re.search(r"As of ([A-Z][a-z]+ \d+, \d{4}) As of ([A-Z][a-z]+ \d+, \d{4})", text)
    m = re.search(_SATA_ROW + r" ([\d,]+) ([\d,]+)", text)
    if not (per and m):
        return []
    a, b = (int(x.replace(",", "")) for x in m.groups())
    start, end = _pdate(per.group(1)).isoformat(), _pdate(per.group(2)).isoformat()
    if b < a:       # a buyback or conversion, not an ATM sale — book it as no issuance
        log(f"[drift] SATA count fell {a:,} -> {b:,} between {start} and {end} — "
            f"counting 0 ATM proceeds for that window")
    sold = max(b - a, 0)
    return [{"from": start, "to": end, "shares": sold,
             "proceedsM": round(sold * ATM_PAR / 1e6, 1), "capacityM": None}]


def _iso_plus(iso, days):
    return (datetime.date.fromisoformat(iso) + datetime.timedelta(days=days)).isoformat()


def _plausible_window(w, filing_date):
    """Whether a filed window runs forwards for at most ATM_MAX_WINDOW_DAYS and ends within
    ATM_MAX_WINDOW_DAYS before its filing, so a typo'd year can't become `confirmed`."""
    span = datetime.date.fromisoformat(w["to"]) - datetime.date.fromisoformat(w["from"])
    return (0 <= span.days <= ATM_MAX_WINDOW_DAYS
            and _iso_plus(filing_date, -ATM_MAX_WINDOW_DAYS) <= w["to"] <= filing_date)


def _filed_atm_windows(pref, cik, parser, reports_atm, first_day_offset):
    """Every ATM window in the 8-Ks back to ATM_LOOKBACK_DAYS before the newest one, newest
    first, each with the firstDay its sales can fall on; None when the newest ATM filing
    can't be read.

    Calibration and absorption both lean on the newest week, and falling back to the week
    before without a word is how STRC's confirmed week sat on Aug 24-30 2026 through three
    newer 8-Ks. A network error raises, so the caller keeps the last values rather than
    publishing a span that one failed fetch cut short.
    """
    rec = _edgar_json(f"https://data.sec.gov/submissions/CIK{cik}.json")["filings"]["recent"]
    found, examined, oldest_filing_date = {}, 0, ""
    for i in range(len(rec["form"])):          # EDGAR lists newest first
        if rec["form"][i] != "8-K":
            continue
        filing_date = rec["filingDate"][i]
        if filing_date < oldest_filing_date:
            break
        if examined >= ATM_MAX_FILINGS:
            log(f"[drift] ATM {pref}: read {ATM_MAX_FILINGS} 8-Ks without getting back to "
                f"{oldest_filing_date or 'a filed window'} — the span may end early")
            break
        examined += 1
        url = _filing_doc(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                          f"{rec['accessionNumber'][i].replace('-', '')}/",
                          rec["primaryDocument"][i])
        memoised = url in _EDGAR_TXT            # fetch_holdings has read nearly all of them
        text = _edgar_text(url)
        if not memoised:
            time.sleep(0.12)
        try:                                    # [] for an 8-K about something else
            windows = parser(text)
            if not windows and reports_atm.search(text):
                raise ValueError("an ATM section, but no window could be read from it")
            for w in windows:
                if not _plausible_window(w, filing_date):
                    raise ValueError(f"implausible window {w['from']} -> {w['to']}")
        except Exception as e:
            log(f"[drift] ATM {pref} 8-K filed {filing_date}: {e}" + ("" if found else
                " — it is the newest ATM filing, so keeping the last filed week and absorption"))
            if not found:
                return None
            continue
        for w in windows:
            w["firstDay"] = _iso_plus(w["from"], first_day_offset)
            kept = found.setdefault((w["from"], w["to"]), w)
            if kept is not w and (kept["shares"], kept["proceedsM"]) != (w["shares"], w["proceedsM"]):
                log(f"[drift] ATM {pref}: {w['from']} -> {w['to']} was filed on {filing_date} as "
                    f"${w['proceedsM']:,.1f}M ({w['shares']:,} sh) and later as ${kept['proceedsM']:,.1f}M "
                    f"({kept['shares']:,} sh) — the later filing stands")
        if windows and not oldest_filing_date:
            oldest_filing_date = _iso_plus(max(w["to"] for w in windows), -ATM_LOOKBACK_DAYS)
    if not found:
        raise ValueError(f"no ATM window in the newest {examined} 8-Ks")
    return sorted(found.values(), key=lambda w: w["to"], reverse=True)


def _absorption(pref, windows, dvol):
    """Σ filed proceeds ÷ Σ session dollar volume over the newest filed windows the tape
    covers, with a ±1-session band and the newest run of windows that sold nothing; None
    when no window counts.

    A window counts once a session AFTER its `to` has settled: Strategy files on Monday
    morning with Friday settled, and counting that week at once would leave the band's +1
    side missing until Monday's close, then move absorption a second time (a quarter-end
    split still does: on 2026-04-06, Mar 30-31 counted that morning and Apr 1-5 after the
    close). The span runs back ATM_SPAN_DAYS from the newest window counted, or to where
    the price history starts, and ends sooner, with a [drift], at a window that doesn't
    tile with the one after it or whose proceeds exceed its tape. So the counted sessions
    are one slice of the tape, and the band slides it a session either way, leaving out a
    side that would run off the series. The zero run is a filing fact and reads every
    window, counted or not: on the Monday a paused ATM sells again, "none since May 17"
    would otherwise sit beside a Confirmed tile showing the new week.
    """
    days = [d for d, _ in dvol]
    covered = [w for w in windows if w["to"] < days[-1]]
    if not covered:
        return None
    start = max(_iso_plus(covered[0]["to"], -ATM_SPAN_DAYS), days[0])
    counted, stop = [], "start"                 # "start": the filings read run out first
    for w in covered:
        if counted and _iso_plus(w["to"], 1) != counted[-1]["firstDay"]:
            stop = "gap"
            log(f"[drift] ATM {pref}: the filed window {w['firstDay']} -> {w['to']} doesn't end the day "
                f"before {counted[-1]['firstDay']} — absorption leaves it and everything older out")
            break
        if w["firstDay"] < start:
            stop = "span" if start > days[0] else "history"
            break
        tape = sum(v for d, v in dvol if w["firstDay"] <= d <= w["to"])
        if w["proceedsM"] * 1e6 > tape:
            stop = "tape"
            log(f"[drift] ATM {pref}: ${w['proceedsM']:,.1f}M filed for {w['firstDay']} -> {w['to']} against "
                f"${tape / 1e6:,.1f}M traded — absorption leaves it and everything older out")
            break
        counted.append({"firstDay": w["firstDay"], "to": w["to"], "proceedsM": round(w["proceedsM"], 1),
                        "dollarVolM": round(tape / 1e6, 1)})
    if not counted:
        return None
    first, end = bisect.bisect_left(days, counted[-1]["firstDay"]), bisect.bisect_right(days, counted[0]["to"])
    tapes = [sum(v for _, v in dvol[first + s:end + s])
             for s in (0, -1, 1) if first + s >= 0 and end + s <= len(dvol)]
    if not all(tapes):                          # a feed without volume: nothing to divide by
        return None
    proceeds = sum(w["proceedsM"] for w in counted) * 1e6
    ratios = [proceeds / t for t in tapes]
    zero_run = next((k for k, w in enumerate(windows) if w["proceedsM"] > 0), len(windows))
    log(f"[ATM] {pref} absorption {ratios[0]:.2%} (±1 session {min(ratios):.2%}-{max(ratios):.2%}) over "
        f"{len(counted)} filed windows {counted[-1]['firstDay']} -> {counted[0]['to']}, ended by {stop}")
    return {"agg": round(ratios[0], 4), "lo": round(min(ratios), 4), "hi": round(max(ratios), 4),
            "n": len(counted), "fromDate": counted[-1]["firstDay"], "toDate": counted[0]["to"],
            "zeroRun": zero_run, "lastSaleTo": windows[zero_run]["to"] if zero_run < len(windows) else None,
            "windows": counted}


def _confirmed_and_absorption(prior, pref, cik, parser, reports_atm, first_day_offset, proceeds_basis):
    """(confirmed, absorption) for one preferred from its filed ATM windows, each kept whole
    from the previous run wherever the filings or the tape can't replace it."""
    try:
        windows = _filed_atm_windows(pref, cik, parser, reports_atm, first_day_offset)
    except Exception as e:                      # a network error, or no ATM window at all
        log(f"[skip] ATM {pref} filings: {e} — keeping the last filed week and absorption")
        return prior.get("confirmed"), prior.get("absorption")
    if windows is None:                         # the newest ATM filing is unreadable, as logged
        return prior.get("confirmed"), prior.get("absorption")
    # prose weeks state no capacity, and a new programme was once announced inside one
    # (Mar 23-29 2026: $1,975.8M available before it, $22,748.2M after)
    newest, stated = windows[0], next((w for w in windows if w["capacityM"] is not None), None)
    confirmed = {"from": newest["from"], "to": newest["to"], "shares": newest["shares"],
                 "proceedsM": newest["proceedsM"], "capacityM": stated and stated["capacityM"],
                 "capacityAsOf": stated and stated["to"]}
    absorption = _PREF_DVOL.get(pref) and _absorption(pref, windows, _PREF_DVOL[pref])
    if not absorption:
        log(f"[skip] ATM {pref} absorption: no settled tape to count a filed window against — "
            f"keeping the last value")
        return confirmed, prior.get("absorption")
    return confirmed, {**absorption, "basis": proceeds_basis}


def fetch_atm(data):
    """Live ATM issuance estimate per preferred, calibrated against the filings."""
    btc = _BTC_PX.get("usd") or data.get("btcPriceUsd") or 0
    # The session rule, per filer, in one place: the day offset from a filed window's
    # `from` to the first day its sales can fall on. Strategy's "During Period Mon to Sun"
    # is inclusive on a trade-date basis, so its Monday counts; Strive's "As of A / As of
    # B" are share counts at A's and B's close, so the sales between them fall in (A, B].
    # proceeds_basis names what the proceeds are: STRC's net proceeds as filed, SATA's
    # share delta x $100 par.
    for tk, doc_cik, parser, reports_atm, first_day_offset, proceeds_basis in (
            ("MSTR", "0001050446", _mstr_strc_atm, _MSTR_ATM_SECTION, 0, "net"),
            ("ASST", "0001920406", _asst_sata_atm, _ASST_ATM_SECTION, 1, "par")):
        co = data["companies"].get(tk)
        if not co:
            continue
        pref = co.get("prefTicker")
        daily = _atm_daily(_ATM_CANDLES.get(pref))
        if not daily:
            log(f"[skip] ATM {pref}: no intraday candles")
            continue
        # newest filed window -> confirmed issuance to calibrate against
        confirmed, absorption = _confirmed_and_absorption(
            co.get("atm") or {}, pref, doc_cik, parser, reports_atm, first_day_offset, proceeds_basis)
        # calibrate: actual proceeds over eligible volume across the confirmed window
        cap, basis = ATM_DEFAULT_CAPTURE, "default (no confirmed week yet)"
        if confirmed and confirmed["from"]:
            first_day = _iso_plus(confirmed["from"], first_day_offset)
            elig = sum(e for d, _t, e in daily if first_day <= d <= confirmed["to"])
            if elig > 1e5 and confirmed["proceedsM"] > 0:
                raw = confirmed["proceedsM"] * 1e6 / elig
                cap = max(0.2, min(1.5, raw))
                basis = (f"calibrated: ${confirmed['proceedsM']:,.1f}M issued "
                         f"{confirmed['from']} to {confirmed['to']}")
            elif confirmed["proceedsM"] == 0:
                basis = f"no issuance in the week to {confirmed['to']}"
        today = daily[-1]
        # sessions since the confirmed window: none yet on a Monday morning, rather than
        # Friday again (it sits inside the window the 8-K just confirmed)
        wk = [r for r in daily if r[0] > ((confirmed or {}).get("to") or "")]
        est = lambda e: (e * cap, (e * cap / btc) if btc else None)   # 0 is a real value, not "unknown"
        t_usd, t_btc = est(today[2])
        w_usd, w_btc = est(sum(r[2] for r in wk))
        px = co.get("prefPrice") or 0
        co["atm"] = {
            "ticker": pref, "par": ATM_PAR, "threshold": ATM_THRESHOLD,
            "status": "active" if px >= ATM_THRESHOLD else "standby",
            "vsPar": round(px - ATM_PAR, 2) if px else None,
            "captureRate": round(cap, 3), "captureBasis": basis,
            "asOf": today[0],
            "todayTotalUsd": round(today[1]), "todayEligUsd": round(today[2]),
            "todayEstUsd": round(t_usd), "todayEstBtc": round(t_btc, 2) if t_btc is not None else None,
            "weekEligUsd": round(sum(r[2] for r in wk)),
            "weekEstUsd": round(w_usd), "weekEstBtc": round(w_btc, 2) if w_btc is not None else None,
            "confirmed": confirmed,
            "absorption": absorption,
            "daily": [{"d": d, "tot": round(t), "elig": round(e)} for d, t, e in daily[-30:]],
        }
        log(f"[ATM] {pref}: {co['atm']['status']} | capture {cap:.0%} ({basis}) | "
            f"today ${today[2]/1e6:,.1f}M eligible -> {co['atm']['todayEstBtc']} BTC est")


def fetch_borrow_fees(data):
    """Annualized cost to short (IBKR indicative rate) for commons + preferreds.
    Also accumulates a daily history in data.json (one point per calendar day)."""
    # SATS has no row: ChartExchange's nasdaq-sats is EchoStar, not Strive's preferred
    targets = [("MSTR", "MSTR", "borrowFee"), ("ASST", "ASST", "borrowFee"),
               ("DFDV", "DFDV", "borrowFee"),
               ("STRC", "MSTR", "prefBorrowFee"), ("SATA", "ASST", "prefBorrowFee"),
               ("CHAD", "DFDV", "prefBorrowFee")]
    for sym, co_tk, key in targets:
        co = data["companies"].get(co_tk)
        if not co:
            continue
        try:
            bf = _ce_borrow(sym)
            co[key] = bf
            hist = co.setdefault(key + "Hist", {"iso": [], "pct": []})
            day = bf["asOf"][:10]
            if hist["iso"] and hist["iso"][-1] == day:
                hist["pct"][-1] = bf["pct"]
            else:
                hist["iso"].append(day)
                hist["pct"].append(bf["pct"])
            hist["iso"], hist["pct"] = hist["iso"][-400:], hist["pct"][-400:]
            log(f"[borrow fee] {sym}: {bf['pct']}%/yr, {bf['avail']:,} shares available")
        except Exception as e:
            log(f"[skip] borrow fee {sym}: {e} — keeping existing values")
        time.sleep(1)                       # be polite: one request per symbol


# --------------------------------------------------------------------------- #
# apxUSD lending banner: Morpho API (open GraphQL, no key)
# --------------------------------------------------------------------------- #
MORPHO_GRAPHQL = "https://api.morpho.org/graphql"
# (kind, Morpho id, label). The banner's "Up to" quotes the best of these, and its
# "on 4 lending options" counts them. The ids are the ones app.morpho.org links to.
APXUSD_LENDING = [
    ("market", "0xebd23a871b52e0a8c92bd719f4413600101b69e946cb72923b3a33fb5bc6ec85", "apxUSD / PT-apyUSD-5NOV2026"),
    ("market", "0xd86df29b48f2d88a0453027212247a29e68ff52c6589c46054f57285da2fa7e3", "apxUSD / PT-apxUSD-5NOV2026"),
    ("market", "0xe23380494e365453f72f736f2d941959ae945773eb67a06cf4f538c7c4201264", "apxUSD / apyUSD"),
    ("vault", "0x3a618E9D4159dff94E90bd6239161dE4dd7C0082", "Apyx apxUSD vault"),
]


def fetch_lending(data):
    """The APY of each apxUSD lending option in APXUSD_LENDING, from the Morpho API.

    Markets report avgNetSupplyApy, the figure api.apyx.fi/v1/lending/markets
    republishes (matched to 4 dp on 2026-09-28), so the banner agrees with the lending
    tab its button opens. That feed lists no vault, hence Morpho. The vault is a Vault
    V2 and reports avgNetApy, net of its performance fee. All or nothing: one missing
    option keeps the last full set, so "Up to" is never the best of three.
    """
    try:
        fields = [f'o{i}: marketById(marketId: "{ident}", chainId: 1) {{ state {{ avgNetSupplyApy }} }}'
                  if kind == "market" else
                  f'o{i}: vaultV2ByAddress(address: "{ident}", chainId: 1) {{ avgNetApy }}'
                  for i, (kind, ident, _) in enumerate(APXUSD_LENDING)]
        res = post_json(MORPHO_GRAPHQL, {"query": "{ " + " ".join(fields) + " }"})["data"]
        options = []
        for i, (kind, _, label) in enumerate(APXUSD_LENDING):
            node = res[f"o{i}"]
            apy = node["state"]["avgNetSupplyApy"] if kind == "market" else node["avgNetApy"]
            options.append({"name": label, "apy": round(float(apy) * 100, 2)})
        data["lending"] = {"asOf": datetime.date.today().isoformat(), "options": options}
        best = max(options, key=lambda o: o["apy"])
        log(f"[lending] apxUSD up to {best['apy']:.2f}% ({best['name']}) across {len(options)} options")
    except Exception as e:
        log(f"[skip] Morpho lending APY: {e} — keeping existing values")


# --------------------------------------------------------------------------- #
# DFDV: DeFi Development Corp, a SOL treasury, and its CHAD preferred
# --------------------------------------------------------------------------- #
# DFDV's own dashboard API (defidevcorp.com/dashboard) is the primary source. It
# sends no CORS header, so only this script can read it; between runs the page ticks
# DFDV and CHAD quotes through the Cboe relay instead. Routes usually answer in a
# second or two but stall for 10-30s at times, so each gets a long timeout and a
# retry, and a figure whose route fails keeps the previous run's value. The API has
# no stock price history, so DFDV's daily closes come from Nasdaq.
DFDV_API = "https://defidevcorp.com/api/dashboard/"

# CHAD terms the API doesn't serve (DFDV's own CHAD page hard-codes them). The rate
# is variable and declared monthly by 8-K; fetch_dfdv_filings reads those, and this
# table is the fallback when EDGAR can't be read. A month neither covers is logged
# [stale]. $10 stated and 13.00% at the Sep 8 2026 IPO (424B5 dated 2026-09-03);
# 14.00% for periods from Nov 1 2026 (8-K filed 2026-10-15).
# The notional steps date the IPO close (1,375,000 sh) and the over-allotment
# (206,250 sh, press release 2026-09-21); /chad/notional carries the latest.
CHAD = {"stated": 10.0,
        "rates": [("2026-09-08", 13.0), ("2026-11-01", 14.0)],
        "notional": [("2026-09-08", 13.75), ("2026-09-20", 15.8125)]}

# Not in the API either: the Q2 2026 shareholder letter puts operating costs at about
# $17M a year, before roughly $10M of interest. The calculator's default for DFDV.
DFDV_OPEX_M = 17.0

# DFDV redefined its adjusted share count on 2026-04-06: nav/sps-daily puts it at about
# 0.86x basic before that date and 1.04x after. Only the later definition is the
# fully-diluted count Net mNAV uses, so earlier history scales basic by today's ratio.
DFDV_ADJ_FROM = "2026-04-06"


def _dfdv(route, timeout=30, tries=2):
    """`data` from one DFDV dashboard route, or None once every try has failed."""
    err = None
    for _ in range(tries):
        try:
            j = get_json(DFDV_API + route, timeout=timeout)
            if j.get("success"):
                return j.get("data")
            err = "success=false"
        except Exception as e:
            err = e
    log(f"[skip] DFDV {route}: {err} — keeping existing values")
    return None


# DFDV's API is a third party, and some of what it sends reaches the page as HTML, so
# every string it supplies is checked against a narrow shape before it is stored.
def _iso(v):
    """v if it is an ISO date (yyyy-mm-dd), else None."""
    return v if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None


def _guard(what, fn, *args):
    """fn(*args), logging and swallowing any error, so one bad route can't end the run."""
    try:
        return fn(*args)
    except Exception as e:
        log(f"[skip] {what}: {e} — keeping existing values")
        return None


def _last_weekday(iso):
    """iso itself on a weekday, else the Friday before it (no holiday calendar)."""
    d = datetime.date.fromisoformat(iso)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.isoformat()


def _dfdv_balance_sheet(co, data):
    """Price, shares, SOL held, 52-week range, cash, and the debt schedule.

    Two converts (put/maturity, conversion price) and three loans, two of them in
    SOL; SOL loans are marked at the live SOL price, matching the net debt DFDV
    publishes. Straight debt carries no convPrice."""
    d = _dfdv("dfdv")
    if d and d.get("price") and d.get("shares_outstanding") and d.get("sol_count"):
        stamp = datetime.datetime.fromisoformat(str(d["updated_at"]).replace("Z", "+00:00"))
        co["stockPrice"] = round(float(d["price"]), 2)
        _stamp_price(co, co["stockPrice"], round(float(d.get("price_change_percent") or 0), 2),
                     _last_weekday(_et_date(stamp)))
        co["sharesOutstanding"] = round(float(d["shares_outstanding"]) / 1e6, 2)
        co["holdings"] = int(d["sol_count"])
        if data.get("solCirculating"):
            co["pctSupply"] = round(co["holdings"] / data["solCirculating"] * 100, 4)
    si = _dfdv("investor-relations/stock-info")
    if si and si.get("week_52_low") and si.get("week_52_high"):
        co["week52Low"] = round(float(si["week_52_low"]), 2)
        co["week52High"] = round(float(si["week_52_high"]), 2)
    nav = _dfdv("nav")
    if nav and nav.get("total_cash_including_eloc") is not None:
        co["cash"] = round(float(nav["total_cash_including_eloc"]) / 1e6, 2)
    # DFDV's adjusted count (basic plus RSUs, vested options, and in-the-money warrants
    # and converts) is the fully-diluted count Net mNAV divides by, as strategy.com's
    # is for MSTR. Basic shares read DFDV's net mNAV about 5% low.
    adj = float((nav or {}).get("adjusted_shares") or 0) / 1e6
    if adj:
        if co.get("sharesOutstanding") and 0.9 <= adj / co["sharesOutstanding"] <= 2:
            co["netDilutedShares"] = round(adj, 2)
        else:
            log(f"[skip] DFDV adjusted shares {adj:,.2f}M against {co.get('sharesOutstanding')}M "
                f"basic — keeping {co.get('netDilutedShares')}")
    debt, sol_px = _dfdv("debt"), data.get("solPriceUsd")
    if debt and sol_px:
        sched = []
        for r in debt:
            name = r.get("name") if re.fullmatch(r"[\w .%$/()&-]{1,60}", str(r.get("name") or "")) else "Debt"
            sol_amt = float(r["sol_amount"]) if r.get("sol_amount") else None
            conv = float(r["conversion_price"]) if r.get("conversion_price") else None
            t = {"note": name, "coupon": float(r.get("coupon_rate") or 0),
                 "maturity": _iso(r.get("maturity_date")), "put": _iso(r.get("put_date")),
                 "principal": round((sol_amt * sol_px if sol_amt else float(r.get("notional_amount") or 0)) / 1e6, 3),
                 "convPrice": conv, "convRate": round(1000 / conv, 4) if conv else None}
            if sol_amt:
                t["solAmount"] = sol_amt
            sched.append(t)
        co["debtSchedule"] = sched
        co["seniorDebt"] = round(sum(t["principal"] for t in sched), 2)
        co["capStructAsOf"] = f"{datetime.date.today().isoformat()} (DFDV dashboard)"


def _dfdv_chad(co):
    """CHAD's notional, rate, quote and daily closes, plus annual obligations.
    Returns the notional steps, which the mNAV history needs."""
    today = datetime.date.today().isoformat()
    rates = _chad_rates()
    rate = _step(rates, today)
    if rates[-1][0][:7] < today[:7]:
        log(f"[stale] CHAD rate steps end {rates[-1][0]} — no 8-K has declared this month's "
            f"rate; add it to CHAD['rates'] if DFDV announced it elsewhere")
    steps = list(CHAD["notional"])
    cn = _dfdv("chad/notional")
    if cn and cn.get("notional_usd"):
        n = round(float(cn["notional_usd"]) / 1e6, 4)
        if abs(steps[-1][1] - n) > 0.001:
            steps.append((_iso(cn.get("as_of_date")) or today, n))
    notional = steps[-1][1]
    co["prefNotional"] = round(notional, 2)
    co["prefBreakdown"] = [["CHAD", PREF_LABEL["CHAD"].format(r="%g" % rate), round(notional, 2), rate]]
    # A declared-but-not-yet-effective rate. Everything above is deliberately on
    # today's rate — obligations, hurdle and duration all describe the stack as it
    # stands — but the calculator runs forward months, so seeding it from today
    # understates the cost from the day the new rate starts. CHAD was raised
    # 13.00% -> 14.00% for periods from 2026-11-01 (8-K filed 2026-10-15).
    nxt = next(((d, r) for d, r in rates if d > today), None)
    if nxt:
        co["prefRateNext"] = {"rate": nxt[1], "from": nxt[0]}
    else:
        co.pop("prefRateNext", None)
    interest = sum(t["principal"] * t["coupon"] / 100 for t in co.get("debtSchedule") or [])
    co["annualObligations"] = round(notional * rate / 100 + interest, 2)
    q = _dfdv("chad/quote", timeout=20)
    if q and q.get("price"):
        co["prefPrice"] = round(float(q["price"]), 2)
        co["prefChangePct"] = round(float(q.get("price_change_percent") or 0), 2)
    # Daily closes and volume come from Nasdaq, as DFDV's do. DFDV's chad/history is
    # not the consolidated tape: it gave 2,364 shares for 2026-10-23 against 33,422 in
    # its own chad/quote and on Nasdaq, and a "close" that was the day's low. Divided
    # into FINRA's short interest, its volume read 6.3 days to cover, not about 0.3.
    # If Nasdaq fails, its prices still draw the chart, without the volume.
    snap_et = datetime.datetime.now(ET)
    rows = _nasdaq_closes("CHAD")
    if not rows:
        ch = _dfdv("chad/history", timeout=40, tries=3)
        rows = sorted((r["date"], float(r["close"]), None)
                      for r in ch or [] if _iso(r.get("date")) and r.get("close"))
    if rows:
        co["prefHistory"] = {"dates": [_iso_lbl(d, "%b %-d") for d, _c, _v in rows],
                             "iso": [d for d, _c, _v in rows],
                             "px": [round(c, 2) for _d, c, _v in rows],
                             "notional": [_step(steps, d) for d, _c, _v in rows]}
    if rows and rows[-1][2] is not None:
        _VOL_HIST["CHAD"] = [(d, v) for d, _c, v in rows]
        rv = _rvol([(d, v * c) for d, c, v in rows], snap_et)
        if rv:
            co["prefRvol"] = rv
        # Once the session is over chad/quote rolls previous_close to the close, so its
        # change reads 0.00 all evening and weekend; base it on the settled sessions.
        settled = _settled_sessions([(d, c) for d, c, _v in rows], snap_et)
        if co.get("prefPrice") and len(settled) >= 2:
            quote = _quote_session(settled, co["prefPrice"], snap_et)
            if not quote:                     # an after-hours print: the close is the quote
                co["prefPrice"] = settled[-1][1]
                quote = (settled[-1][0], settled[-2][1])
            co["prefChangePct"] = round((co["prefPrice"] / quote[1] - 1) * 100, 2)
    return steps


def _dfdv_holdings(co, data):
    """Date of the latest SOL update, year-end balance, YTD change and the weekly
    accumulation series. Returns DFDV's daily SOL prices, for beta."""
    year = datetime.date.today().year
    h = _dfdv("history", timeout=40) or []
    held = sorted((r["date"], int(r["total_sol_holdings"])) for r in h
                  if _iso(r.get("date")) and r.get("total_sol_holdings"))
    # A row's sol_price is CoinGecko's 00:00 UTC print on its date: 8pm ET the evening
    # before (it matched exactly on 2026-10-18, -20 and -23). File each under that ET
    # evening, four hours after its session's close, so beta pairs a close with SOL that
    # night rather than twenty hours earlier, which read beta 0.32 against about 1.3.
    day_before = lambda iso: (datetime.date.fromisoformat(iso) - datetime.timedelta(days=1)).isoformat()
    sol_hist = sorted((day_before(r["date"]), float(r["sol_price"])) for r in h
                      if _iso(r.get("date")) and r.get("sol_price"))
    # A filed week wins on its own date; the dashboard supplies any later day. With
    # the dashboard as new as the filing or older, holdings are the filed figure.
    filed = sorted((_DFDV_FILED.get("sol") or {}).items())
    if filed:
        fd, fv = filed[-1]
        api = dict(held)
        if fd in api and api[fd] != fv:
            log(f"[drift] DFDV SOL on {fd}: dashboard {api[fd]:,} vs 8-K {fv:,} — using the 8-K")
        if not held or fd >= held[-1][0]:
            co["holdings"] = fv
            if data.get("solCirculating"):
                co["pctSupply"] = round(fv / data["solCirculating"] * 100, 4)
            held = [x for x in held if x[0] < fd] + [(fd, fv)]
    if not held:
        return sol_hist
    i = len(held) - 1
    while i > 0 and held[i - 1][1] == held[-1][1]:
        i -= 1
    co["treasuryDate"] = held[i][0]
    ye = _step(held, f"{year - 1}-12-31")
    if ye:
        co["yearEnd"] = {str(year - 1): ye}
        co["netChangeYtd"] = co.get("holdings", held[-1][1]) - ye
    # from Jan 1, like the EDGAR series: each week's last reading, the change since
    # the week before (the first week's since year-end). DFDV publishes a daily SOL
    # balance, so a week's change includes staking rewards and loans, not only buys.
    # A week with a filed table ends on its filed Friday instead, as MSTR's and ASST's
    # end on their 8-K weeks.
    weeks = {}                                     # Monday -> the week's last reading

    def monday(day):
        dt = datetime.date.fromisoformat(day)
        return dt - datetime.timedelta(days=dt.weekday())
    for day, v in held:
        if day > f"{year - 1}-12-31":
            weeks[monday(day)] = (day, v)
    for day, v in filed:
        if day > f"{year - 1}-12-31":
            weeks[monday(day)] = (day, v)
    jan1 = datetime.date(year, 1, 1)
    wk = {"dates": [], "ranges": [], "acquired": [], "holdings": []}
    prev = ye or 0
    for mon in sorted(weeks):
        day, v = weeks[mon]
        wk["dates"].append(_iso_lbl(day, "%b %-d"))
        wk["ranges"].append(f"{_iso_lbl(max(mon, jan1).isoformat(), '%b %-d')} – {_iso_lbl(day, '%b %-d')}")
        wk["acquired"].append(v - prev)
        wk["holdings"].append(v)
        prev = v
    if wk["dates"]:
        data.setdefault("weekly", {})["DFDV"] = wk
    return sol_hist


def _dfdv_per_share(co):
    """SOL per share (DFDV's headline, basic shares) and its growth since Jan 1, the
    realized staking yield, and the share count per date for short interest.
    Returns adjusted ÷ basic shares per date since DFDV_ADJ_FROM, for net mNAV."""
    year = datetime.date.today().year
    sps = _dfdv("nav/sps-daily", timeout=40)
    pts = sorted((r["date"], float(r["headline_sps"])) for r in sps or []
                 if _iso(r.get("date")) and r.get("headline_sps"))
    # both are SOL ÷ shares, so their ratio is adjusted shares ÷ basic shares
    adj_ratio = sorted((r["date"], float(r["headline_sps"]) / float(r["adjusted_sps"]))
                       for r in sps or [] if _iso(r.get("date")) and r["date"] >= DFDV_ADJ_FROM
                       and r.get("headline_sps") and r.get("adjusted_sps"))
    if pts:
        idx = sorted(set(range(0, len(pts), 7)) | {len(pts) - 1})
        co["bpsHistory"] = {"dates": [_iso_lbl(pts[i][0], "%b '%y") for i in idx],
                            "sol": [round(pts[i][1], 5) for i in idx], "basis": "basic"}
        base = _step(pts, f"{year - 1}-12-31")
        if base:
            co["assetYieldYtd"] = round((pts[-1][1] / base - 1) * 100, 1)
    # realized staking yield: dfdvSOL's exchange rate to SOL over the trailing ~90 days
    rt = _dfdv("dfdvsol/rate")
    rates = sorted((r["epoch_date"], float(r["dfdvsol_sol_rate"])) for r in rt or []
                   if _iso(r.get("epoch_date")) and r.get("dfdvsol_sol_rate"))
    if rates:
        end_d, end_r = rates[-1]
        frm = (datetime.date.fromisoformat(end_d) - datetime.timedelta(days=90)).isoformat()
        past = [x for x in rates if x[0] <= frm]
        if past:
            days = (datetime.date.fromisoformat(end_d) - datetime.date.fromisoformat(past[-1][0])).days
            co["organicYield"] = round(((end_r / past[-1][1]) ** (365 / days) - 1) * 100, 2)
    shs = _dfdv("shares")
    if shs:
        _SHARES_HIST["DFDV"] = sorted((r["date"], float(r["shares_outstanding"]) / 1e6)
                                      for r in shs if _iso(r.get("date")) and r.get("shares_outstanding"))
    return adj_ratio


def _dfdv_cost(co):
    """Average cost per SOL from the purchase log DFDV publishes on its dashboard, which
    the page takes as the record. The log lists purchases only and doesn't reconcile with
    the SOL DFDV holds (2,148,968 logged vs 2,590,266 held on 2026-10-25), so
    costBasisSol carries the SOL the cost covers, and the page's P/L counts only that."""
    rows = [(r["date"], float(r["sol_amount"]), float(r["usd_amount"]))
            for r in _dfdv("purchases") or []
            if _iso(r.get("date")) and (r.get("sol_amount") or 0) > 0 and (r.get("usd_amount") or 0) > 0]
    if rows:
        sol, usd = sum(r[1] for r in rows), sum(r[2] for r in rows)
        co["avgCost"] = round(usd / sol, 2)
        co["costBasisSol"] = round(sol)
        co["costBasisAsOf"] = max(r[0] for r in rows)


def _dfdv_closes(co, data, steps, sol_hist, adj_ratio):
    """Nasdaq daily closes: stockHistory's calendar axis, beta to SOL, relative volume,
    and the price beside DFDV's own EV series. That series leaves CHAD out of EV; it
    goes back in here, as preferred does for MSTR and ASST. Net mNAV is rebuilt from
    the same series, since DFDV publishes no net history."""
    closes = _nasdaq_closes("DFDV")
    if not closes:
        return
    snap_et = datetime.datetime.now(ET)
    # DFDV's quote is its latest trade, after-hours included, with the change on the
    # previous close: on Sunday 2026-10-25 it read 3.78, +2.02% for Friday, whose close
    # was 3.705, -1.33%. Stamp it against the settled sessions, as MSTR's and ASST's
    # are, and outside a session take the last settled close and its own change.
    settled = _settled_sessions([(d, c) for d, c, _v in closes], snap_et)
    if co.get("stockPrice") and len(settled) >= 2:
        quote = _quote_session(settled, co["stockPrice"], snap_et)
        if not quote:                         # an after-hours print: the close is the quote
            co["stockPrice"] = settled[-1][1]   # unrounded: Friday's 3.705 on 3.755 is -1.33%
            quote = (settled[-1][0], settled[-2][1])
        _stamp_price(co, co["stockPrice"], (co["stockPrice"] / quote[1] - 1) * 100, quote[0])
    sh = data.get("stockHistory") or {}
    if sh.get("iso"):
        out, last, j = [], None, 0
        for iso in sh["iso"]:
            while j < len(closes) and closes[j][0] <= iso:
                last = closes[j][1]
                j += 1
            out.append(round(last, 2) if last is not None else None)
        sh["DFDV"] = out
    beta = _beta(settled[-253:], sol_hist)
    if beta is not None:
        co["betaAsset"] = round(beta, 2)
    rv = _rvol([(d, c * v) for d, c, v in closes], snap_et)
    if rv:
        co["rvol"] = rv
    mn = _dfdv("nav/mnav-enterprise", timeout=40)
    cl = [(d, c) for d, c, _v in closes]
    basic, ratio_now = _SHARES_HIST.get("DFDV") or [], (adj_ratio[-1][1] if adj_ratio else None)
    dts, px, ev, net = [], [], [], []
    for r in mn or []:
        day = _iso(r.get("date"))
        last = _step(cl, day) if day else None
        if not day or day < MNAV_START["DFDV"] or last is None or not r.get("solNav"):
            continue
        ev_m, sol_m, chad = float(r["enterpriseValue"]) / 1e6, float(r["solNav"]) / 1e6, _step(steps, day) or 0
        dts.append(_iso_lbl(day, "%b %-d"))
        px.append(round(last, 2))
        ev.append(round((ev_m + chad) / sol_m, 3))
        # DFDV's EV is basic market cap plus net debt, so net debt is EV less the close
        # times basic shares, and the net reserve is SOL less that and CHAD. Its EV
        # prices the stock at its own time of day, so this is good to about ±5%. Blank
        # where nothing is left for common (Jun 2 – Aug 18 2026) and above 5x, where
        # the multiple only spikes toward a zero crossing.
        sh = _step(basic, day)
        ratio = (_step(adj_ratio, day) if day >= DFDV_ADJ_FROM else None) or ratio_now
        n = None
        if sh and ratio:
            net_res = sol_m - (ev_m - last * sh) - chad
            n = last * sh * ratio / net_res if net_res > 0 else None
        net.append(round(n, 3) if n is not None and n <= 5 else None)
    if ev:
        co["mnavHistory"] = {"dates": dts, "px": px, "mnav": ev}
        if any(v is not None for v in net):
            co["mnavHistory"]["net"] = net
        log(f"DFDV mNAV history: {len(ev)} pts, latest EV {ev[-1]}x / net {net[-1]}x, "
            f"{sum(v is None for v in net)} blank")


# DeFi Development's own 8-Ks. It files most Mondays, and since 2026-09-14 each weekly
# one carries a "SOL and SOL equivalents" table for the last two Fridays. Others state
# CHAD's rate, its offerings and its ATM. They anchor what they report; the dashboard
# API covers the rest (shares, cash, debt, CHAD notional), and the days after a filing.
DFDV_CIK = "0001805526"
_D = r"([A-Z][a-z]+ \d{1,2}, \d{4})"
_DFDV_SOL = re.compile(_D + r" " + _D + r" (?:% Change )?SOL and SOL equivalents ([\d,]+) ([\d,]+)")
# "( CHAD Stock ) from 13.00% to 14.00%, effective for periods commencing on or after
# November 1, 2026": _edgar_text turns the curly quotes into spaces
_RATE_CHANGE = re.compile(r"\(\s*(?:the\s+)?([A-Z]{3,5})(?: Stock)?\s*\)[^.%]{0,60}?from ([\d.]+)% to ([\d.]+)%,?"
                          r" effective (?:for (?:monthly )?periods commencing on or after )?" + _D)
# a monthly declaration: "for the period from November 1, 2026 through November 30, 2026
# ... which represents a per annum dividend rate of 14.00%"
_RATE_DECLARED = re.compile(r"period from " + _D + r" (?:through|to) " + _D
                            + r"[^%]{0,300}?per annum dividend rate of ([\d.]+)%")
_SALES_AGREEMENT = re.compile(r"sales agreement .{0,400}?up to ([\d,]+) shares \(the Shares \) of the"
                              r" Company.{0,200}?\(\s*(?:the\s+)?([A-Z]{3,5}) Stock\s*\)", re.I)
_OVERALLOT = re.compile(r"closing of .{0,120}?exercise of its over-allotment option", re.I)
# Readings from DFDV's 8-Ks this run: {"sol": {iso: SOL}, "rates": {iso: rate}}
_DFDV_FILED = {}


def _offering_closed(text):
    """'Closed <series> offering: N shares at $P' from an 8-K reporting a closed
    underwritten preferred offering (Strive's SATS, DFDV's CHAD), else []."""
    m = re.search(r"underwritten offering \(the Offering \).{0,160}? of ([\d,]+) shares \(the Shares \)"
                  r" of the Company.{0,200}?\(the ([A-Z]{3,5}) Stock \)"
                  r"(?: at a public offering price of \$\s?([\d.]+))?", text)
    # closed, not just priced: the pricing 8-K says when the Offering "is expected to close"
    if not m or not re.search(r"At the closing of the Offering|net proceeds from the Offering were", text):
        return []
    n, ser, px = int(m.group(1).replace(",", "")), m.group(2), m.group(3)
    return [f"Closed {ser} offering: {n:,} shares" + (f" at ${float(px):,.2f}" if px else "")]


def _dfdv_filing(text, items_field):
    """(actions, period end or None, SOL there, {(series, from, to): (effective, rate)},
    {month start: rate}) from one DFDV 8-K."""
    acts, end, sol, changes, declared = [], None, None, {}, {}
    m = _DFDV_SOL.search(text)
    if m:
        a, b = (int(x.replace(",", "")) for x in m.group(3, 4))
        end, sol = _pdate(m.group(2)).isoformat(), b
        acts.append(f"SOL & equivalents {'+' if b >= a else '−'}{abs(b - a):,} → {b:,}")
    for m in _RATE_CHANGE.finditer(text):
        changes[(m.group(1), m.group(2), m.group(3))] = (_pdate(m.group(4)).isoformat(), float(m.group(3)))
    for m in _RATE_DECLARED.finditer(text):
        declared[_pdate(m.group(1)).isoformat()] = float(m.group(3))
    acts += _offering_closed(text)
    if "1.01" in items_field and (sa := _SALES_AGREEMENT.search(text)):
        acts.append(f"Established {sa.group(2)} ATM: up to {int(sa.group(1).replace(',', '')):,} shares")
    if _OVERALLOT.search(text) and "Series C" in text:
        acts.append("Closed the CHAD offering's over-allotment option")
    return acts, end, sol, changes, declared


def fetch_dfdv_filings(data):
    """DFDV's 8-Ks since Jan 1: the filed SOL weeks and CHAD rate steps that fetch_dfdv
    anchors on, and their entries in the actions log, each linked to its 8-K."""
    try:
        r = _edgar_json(f"https://data.sec.gov/submissions/CIK{DFDV_CIK}.json")["filings"]["recent"]
    except Exception as e:
        log(f"[skip] EDGAR submissions DFDV: {e} — DFDV keeps its dashboard figures")
        return
    year0 = f"{datetime.date.today().year}-01-01"
    acts, sol, rates, seen = {}, {}, {}, set()
    for i in reversed(range(len(r["form"]))):      # oldest first, so a repeat names its first filing
        filed = r["filingDate"][i]
        if r["form"][i] != "8-K" or filed < year0:
            continue
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(DFDV_CIK)}/"
               f"{r['accessionNumber'][i].replace('-', '')}/{r['primaryDocument'][i]}")
        try:
            text = _edgar_text(url)
        except Exception as e:
            log(f"[skip] DFDV 8-K {filed}: {e}")
            continue
        _READ_8K["DFDV"] = max(_READ_8K.get("DFDV", ""), filed)
        items, end, held, changes, declared = _dfdv_filing(text, r["items"][i] or "")
        if end:
            sol[end] = held
        rates.update(declared)
        for (ser, a, b), (eff, rate) in changes.items():
            if ser == "CHAD" and (ser, a, b) not in seen:      # the press releases repeat it
                seen.add((ser, a, b))
                rates.setdefault(eff, rate)
                items.append(f"{'Raised' if float(b) > float(a) else 'Cut'} CHAD dividend rate "
                             f"{a}% → {b}% from {_iso_lbl(eff, '%b %-d')}")
        # The log keys entries by date and issuer, and a filing can land on the day a
        # filed week ends (the CHAD ATM, Fri 2026-09-11): one entry, led by the week.
        d = end or filed
        if items and d in acts:
            prev = acts[d]
            if end:
                acts[d] = {"d": d, "co": "DFDV", "items": items + prev["items"], "filed": filed, "url": url}
            else:
                prev["items"] += items
        elif items:
            acts[d] = {"d": d, "co": "DFDV", "items": items, "filed": filed, "url": url}
        time.sleep(0.12)
    _DFDV_FILED.update({"sol": sol, "rates": rates})
    if acts:
        _merge_actions(data, list(acts.values()))
    log(f"[DFDV 8-K] {len(acts)} entries; SOL weeks {min(sol, default='-')} → {max(sol, default='-')}; "
        f"CHAD rate steps {sorted(rates.items())}")


def _chad_rates():
    """CHAD's rate steps: the hand table, with every step DFDV's 8-Ks state winning on its date."""
    return sorted(dict(CHAD["rates"] + list((_DFDV_FILED.get("rates") or {}).items())).items())


def fetch_dfdv(data):
    """DFDV + CHAD from DFDV's dashboard API, in the shape the page reads for MSTR and
    ASST. Each part is guarded on its own, so one failing route keeps only its figures."""
    co = data["companies"].setdefault("DFDV", {})
    co.update({"name": "DeFi Development", "ticker": "DFDV", "prefName": "DFDV Preferred",
               "prefTicker": "CHAD", "asset": "SOL", "accent": "#a970ff",
               "prefStated": CHAD["stated"], "opexYr": DFDV_OPEX_M})
    co.setdefault("trail3m", {"prefMo": 0, "commonMo": 0})
    _guard("DFDV balance sheet", _dfdv_balance_sheet, co, data)
    steps = _guard("CHAD", _dfdv_chad, co) or list(CHAD["notional"])
    sol_hist = _guard("DFDV holdings", _dfdv_holdings, co, data) or []
    adj_ratio = _guard("DFDV per share", _dfdv_per_share, co) or []
    _guard("DFDV cost basis", _dfdv_cost, co)
    _guard("DFDV closes", _dfdv_closes, co, data, steps, sol_hist, adj_ratio)
    if co.get("holdings") and co.get("stockPrice") and co.get("prefBreakdown"):
        log(f"DFDV via its dashboard: {co['holdings']:,} SOL, ${co['stockPrice']} "
            f"({co.get('dayChangePct', 0):+.2f}%), debt ${co.get('seniorDebt', 0):,.1f}M, "
            f"cash ${co.get('cash', 0):,.1f}M, CHAD ${co['prefNotional']:,.2f}M at "
            f"{co['prefBreakdown'][0][3]:g}%")


def apply_sats_proceeds(data):
    """Put the SATS cash next to the SATS claim.

    fetch_strategytracker adds the $700M stated claim to the preferred stack, but
    fetch_holdings runs afterwards and resets cash from the weekly 8-K — which is
    as of a date before the offering closed. Left there the page would carry the
    claim without the ~$608.15M of net proceeds that came in against it and read
    the net reserve $608M poorer than it is. Add the cash once the filings have
    the claim, and stop as soon as a filing covers the closing date and reports
    the balance itself.
    """
    co = data["companies"].get("ASST")
    if not co or not any(r[0] == "SATS" for r in (co.get("prefBreakdown") or [])):
        return
    # the first filed balance from the close on reports the proceeds itself
    reported = min((d for d in _FILED_DATES.get("ASST", []) if d >= SATS["closed"]), default=None)
    if reported:
        log(f"[SATS] filings report the cash from {reported}")
    else:
        co["cash"] = round((co.get("cash") or 0) + SATS["netProceedsM"])
        log(f"[SATS] +${SATS['netProceedsM']:,.0f}M net proceeds to cash (no filing since the close)")
    # The history rows carry the claim from the close (fetch_strategytracker's pref_at),
    # so the cash goes in beside it until that filing: without it Oct 20-22 2026 read
    # 2.9x, the claim in and the $608M that came in against it out.
    for m in _MNAV_INPUTS.get("ASST") or []:
        if m["d"] >= SATS["closed"] and (reported is None or m["d"] < reported):
            m["cash"] += SATS["netProceedsM"]
            m["dirty"] = True


def rebase_mnav_holdings(data):
    """Carry filed holdings into the trailing mNAV history.

    The tracker's daily btc_balance goes stale between its own updates. It still
    read 32,773 for Oct 14-18 2026 while the Oct 14 8-K had already taken Strive
    to 35,595, and because the history is built before fetch_holdings reads the
    filings, the series kept the stale balance while the headline tile used the
    filed one — the same page showing net mNAV 1.69x on the chart and 1.47x on
    the tile. The tile was the right one; a smaller BTC balance shrinks the net
    reserve and lifts the multiple.

    Past the newest filed observation the tracker cannot know better than the
    filings, so recompute those rows on the filed figure. Only that trailing
    stretch is touched: earlier rows are left alone, where the tracker's daily
    balance is the finer record.
    """
    for tk, rows in _MNAV_INPUTS.items():
        co = data["companies"].get(tk)
        filed = _FILED_HOLD.get(tk)
        hist = (co or {}).get("mnavHistory")
        if not (co and filed and hist and rows):
            continue
        since, held = filed
        touched, was = 0, None
        for idx, m in enumerate(rows):
            stale = m["d"] >= since and m["b"] and m["b"] < held
            if not (stale or m.get("dirty")):
                continue
            if stale:
                was = m["b"] if was is None else min(was, m["b"])
            nav = (held if stale else m["b"]) * m["bp"] / 1e6
            if nav <= 0:
                continue
            ev = (m["sp"] * m["shs"] + m["debt"] + m["pref"] - m["cash"]) / nav
            net_res = nav + m["cash"] - m["otm"] - m["pref"]
            hist["mnav"][idx] = round(ev, 3)
            hist["net"][idx] = (round(m["sp"] * (m["shs"] + m["itm"] + m["opt"]) / net_res, 3)
                                if net_res > 0 else None)
            touched += 1
        if touched:
            log(f"[mnav] {tk}: {touched} trailing row(s) from {since} rebased to the filed "
                f"{held:,} BTC (tracker had {was:,.0f})" if was else
                f"[mnav] {tk}: {touched} row(s) recomputed")


def record_filing_watermark(data):
    """Stamp the newest 8-K on EDGAR next to the newest one we actually ingested.

    Filing-derived figures (cash, holdings, share counts, preferred notional)
    only move when this script runs, and GitHub delivers a fraction of the cron
    schedule — so the dashboard can sit a filing behind with nothing on the page
    saying so. The frontend compares these two dates and shows a notice when
    EDGAR is ahead, instead of presenting last week's balance sheet as current.
    """
    for tk, cik in {**CIK, "DFDV": DFDV_CIK}.items():
        co = data["companies"].get(tk)
        if not co:
            continue
        newest = None
        try:
            r = _edgar_json(f"https://data.sec.gov/submissions/CIK{cik}.json")["filings"]["recent"]
            newest = max((r["filingDate"][i] for i in range(len(r["form"]))
                          if r["form"][i] == "8-K"), default=None)
            # Nothing here reads an amendment: they're rare (Strategy's one, 2026-07-31,
            # replaced an earnings exhibit), and parsing them would mean deciding, reader by
            # reader, which filing stands. Name a recent one so a restatement gets checked.
            since = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
            for i in range(len(r["form"])):
                if r["form"][i] == "8-K/A" and r["filingDate"][i] >= since:
                    log(f"[drift] {tk} filed an 8-K/A on {r['filingDate'][i]}, amending its "
                        f"{r['reportDate'][i]} report — amendments aren't read; check whether "
                        f"it restates a figure")
        except Exception as e:
            log(f"[skip] filing watermark {tk}: {e}")
        acted = max((a["filed"] for a in (data.get("actions") or [])
                     if a["co"] == tk and a.get("filed")), default=None)
        # An 8-K counts once this run has read it, whether or not it carried any
        # figures. Counting only those that yielded an action kept ASST "behind" from
        # a 2026-10-21 SATS dividend notice until its next weekly 8-K, and fired the
        # page's notice, the workflow gate and filing-watch on every check meanwhile.
        read = _READ_8K.get(tk)
        ingested = max(filter(None, (acted, read)), default=None)
        if read and acted and read > acted:
            log(f"[watermark] {tk}: newest 8-K read ({read}) gave no action; "
                f"the newest that did is {acted}")
        co["filingWatch"] = {"latestOnEdgar": newest, "ingested": ingested}
        if newest and ingested and newest > ingested:
            log(f"[stale] {tk}: EDGAR has an 8-K filed {newest} but the newest "
                f"ingested is {ingested} — filing-derived figures are behind")


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
    fetch_crypto_market(data)         # CoinGecko: BTC + SOL price and supply
    fetch_strategy_kpi(data)          # strategy.com API: shares, debt, USD reserve (MSTR)
    fetch_sata_notional(data)         # SEC EDGAR: filed SATA count (the tracker's stalls)
    fetch_strc_notional(data)         # SEC EDGAR: STRC's filed weekly sales and buybacks (the tracker's log lags)
    fetch_strategytracker(data)       # PRIMARY: current metrics + real history (both names)
    fetch_holdings(data)             # SEC EDGAR: weekly accumulation (8-K period ranges)
    fetch_strategy_kpi(data)          # re-apply: holdings sets cash from the 8-K balances; live wins
    fetch_dfdv_filings(data)          # SEC EDGAR: DFDV's filed SOL weeks, CHAD rate, offerings
    fetch_dfdv(data)                  # DFDV dashboard API: the SOL treasury and CHAD
    fetch_short_interest(data)        # Nasdaq: days to cover (semi-monthly)
    fetch_borrow_fees(data)           # ChartExchange/IBKR: annualized cost to short
    fetch_atm(data)                   # preferred ATM issuance estimate + filing calibration
    apply_sats_proceeds(data)         # SATS cash belongs with the SATS claim
    rebase_mnav_holdings(data)        # filings beat the tracker's stale daily BTC balance
    fetch_lending(data)               # Morpho API: apxUSD lending APYs for the banner
    # debt schedule + preferred breakdown are parsed from the 10-Q (see notes);
    # cebe / per-share / valuation are computed live in the dashboard.

    data["asOf"] = datetime.date.today().isoformat()
    record_filing_watermark(data)

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
