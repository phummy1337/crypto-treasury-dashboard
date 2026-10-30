// Calculator engine tests. Run: node --test tests/*.test.js
//
// Each fixture is one calculator button's opening snapshot and default inputs, frozen
// from data.json on 2026-10-25 so a data refresh can't move them. The goldens are what
// the engine returns for them: an edit that shifts one has changed what the page shows,
// and its commit should say so. A common's endMnav is that day's EV mNAV to the input's
// 0.05 step. DFDV and CHAD hold the 12 months of CHAD dividends the page reserves for
// both buttons (reserveMo).
const test = require("node:test");
const assert = require("node:assert/strict");
const { crashMonth, pathPrice, reserveFor, reserveMonths, simulate, simulateCredit, breakEven, trancheExit, runoffYears } = require("../calc-engine.js");

const FIXTURES = {
  MSTR: {
    s: {px:146.01, sharesM:422.35, pref:13557, spot:80472, hold:848517, cash:5281, owed:0, debtUsd:6714, intUsd:34.5953, intUnits:0,
        otherNotional:4987, otherDivs:470.66},
    cfg: {target:150000, months:12, prefMo:0, commonMo:1579, rate:12, fundMix:0, endMnav:1.1, runway:0, atmFloor:1, stake:0, opex:0},
    golden: {implied:296.4855431259105, endHold:1013760.6016336514, endSharesM:508.4458499399204, assetSold:0, pausedMo:0, broke:null},
  },
  ASST: {
    s: {px:28.97, sharesM:124.06, pref:2103, spot:80472, hold:36818, cash:976, owed:0, debtUsd:0, intUsd:0, intUnits:0,
        otherNotional:700, otherDivs:105},
    cfg: {target:150000, months:12, prefMo:207, commonMo:334, rate:13, fundMix:0, endMnav:1.6, runway:0, atmFloor:1, stake:0, opex:0},
    golden: {implied:95.06533221898071, endHold:93434.07883711549, endSharesM:193.03434272587322, assetSold:0, pausedMo:0, broke:null},
  },
  DFDV: {
    s: {px:3.78, sharesM:32.62, pref:15.81, spot:101.4, hold:2590266, cash:9.98, owed:562000, debtUsd:149.0532, intUsd:7.5087, intUnits:33725,
        otherNotional:0, otherDivs:0},
    cfg: {target:101.4, months:12, prefMo:0, commonMo:0, rate:13, fundMix:0, endMnav:1.3, runway:0, atmFloor:1, stake:5.07, opex:17, reserveMo:12},
    golden: {implied:3.6056353542145665, endHold:2724687.5748565323, endSharesM:38.65438471327296, assetSold:0, pausedMo:0, broke:null,
             endReserve:2.0553, reserveShort:null},
  },
  STRC: {
    s: {spot:80472, hold:848517, cash:5281, owed:0, debtUsd:6714, intUsd:34.5953, intUnits:0, stated:100, quote:99.36,
        series:8570, rate:12, otherNotional:4987, otherDivs:470.66, tranches:[
      {note:"2028", principal:1010, coupon:0.625, units:0, repay:11, convert:null},
      {note:"2029", principal:1500, coupon:0, units:0, repay:19, convert:null},
      {note:"2030A", principal:800, coupon:0.625, units:0, repay:23, convert:null},
      {note:"2030B", principal:2000, coupon:0, units:0, repay:16, convert:null},
      {note:"2031", principal:603.75, coupon:0.875, units:0, repay:23, convert:null},
      {note:"2032", principal:800, coupon:2.25, units:0, repay:32, convert:null},
    ]},
    cfg: {target:80472, months:60, prefMo:0, rate:12, issuePx:99.36, reqYield:12.0773, stake:0, opex:0},
    golden: {unpaid:0, firstGap:null, cashOut:18, firstSale:18, sold:111859.34464523906, repaid:6713.75, coverEnd:4.372672039662781, breakEven:313.14230393831235},
  },
  // SATS has no button now, but its $700M at 15% still ranks with SATA's $1,403M at 13%:
  // it is the other series here, as in ASST's fixture (otherNotional, otherDivs)
  SATA: {
    s: {spot:80472, hold:36818, cash:976, owed:0, debtUsd:0, intUsd:0, intUnits:0, stated:100, quote:98.02,
        series:1403, rate:13, otherNotional:700, otherDivs:105, tranches:[]},
    cfg: {target:80472, months:60, prefMo:0, rate:13, issuePx:98.02, reqYield:13.2626, stake:0, opex:0},
    golden: {unpaid:0, firstGap:null, cashOut:41, firstSale:41, sold:5728.07933194154, repaid:0, coverEnd:1.1896662368045656, breakEven:4683.269706652449},
  },
  CHAD: {
    s: {spot:101.4, hold:2590266, cash:9.98, owed:562000, debtUsd:149.0532, intUsd:7.5087, intUnits:33725, stated:10, quote:9.9,
        series:15.81, rate:13, otherNotional:0, otherDivs:0, tranches:[
      {note:"Convert Q1 2025", principal:11.471, coupon:2.5, units:0, repay:17, convert:null},
      {note:"Convert July 2025", principal:114.581, coupon:5.5, units:0, repay:44, convert:null},
      {note:"SOL Loan 1", principal:7.605, coupon:12.5, units:75000, repay:null, convert:null},
      {note:"DeFi Loan", principal:23, coupon:4, units:0, repay:null, convert:null},
      {note:"SOL Loan - Evergreen", principal:49.382, coupon:5, units:487000, repay:null, convert:null},
    ]},
    cfg: {target:101.4, months:60, prefMo:0, rate:13, issuePx:9.9, reqYield:13.1313, stake:5.07, opex:17, reserveMo:12},
    golden: {unpaid:0, firstGap:null, cashOut:4, firstSale:4, sold:2550451.7546022367, repaid:126.052, coverEnd:-1.6646207410803922, breakEven:73.26518995988579,
             endReserve:2.0553, reserveShort:null},
  },
};

// numbers to 1e-9, relative; anything else exactly
function near(actual, expected, label){
  if(typeof expected === "number" && typeof actual === "number")
    assert.ok(Math.abs(actual - expected) <= 1e-9*Math.max(1, Math.abs(expected)), `${label}: got ${actual}, want ${expected}`);
  else assert.deepEqual(actual, expected, label);
}

for(const co of ["MSTR", "ASST", "DFDV"]){
  test(`${co} common defaults give the golden results`, () => {
    const {s, cfg, golden} = FIXTURES[co], r = simulate(s, cfg);
    for(const k in golden) near(r[k], golden[k], k);
  });
}
for(const tk of ["STRC", "SATA", "CHAD"]){
  test(`${tk} credit defaults give the golden results`, () => {
    const {s, cfg, golden} = FIXTURES[tk];
    const r = {...simulateCredit(s, cfg), breakEven:breakEven(s, cfg)};
    for(const k in golden) near(r[k], golden[k], k);
  });
}

test("MSTR pays a year of dividends and interest from today's cash, each series at its own rate", () => {
  // BTC and mNAV flat, nothing issued: STRC's $8,570M at 12% plus the other four series
  // ($470.66M) is $1,499.06M, and $34.60M of convert interest makes the $1,534M a year
  // Strategy files. Its $5,281M of cash covers that, so no shares are issued and no BTC sold.
  const {s, cfg} = FIXTURES.MSTR;
  const flatCfg = {...cfg, target:s.spot, commonMo:0, endMnav:simulate(s, cfg).mnav0};
  const r = simulate(s, flatCfg);
  near(r.endSharesM, s.sharesM, "shares");
  near(r.endHold, s.hold, "BTC");
  near(r.endOpenCash, 5281 - (8570*0.12 + 470.66 + 34.5953), "cash left");
  // the rate input moves STRC's dividends only
  near(simulate(s, {...flatCfg, rate:0}).endOpenCash, 5281 - (470.66 + 34.5953), "cash left at a 0% STRC rate");
});

// A small issuer for arithmetic you can check by hand: $100 coin, no debt, no preferred.
const tiny = (over = {}) => ({spot:100, hold:1e6, cash:0, owed:0, debtUsd:0, intUsd:0, intUnits:0,
  stated:10, quote:10, series:0, rate:0, otherNotional:0, otherDivs:0, tranches:[], ...over});
const flat = (over = {}) => ({target:100, months:12, prefMo:0, rate:0, issuePx:10, reqYield:10, stake:0, opex:0, ...over});

test("$1M of CHAD raised at $8 is 125,000 shares: $1.25M of stated notional", () => {
  const r = simulateCredit(tiny({series:15}), flat({months:1, prefMo:1, issuePx:8}));
  near(r.endSeries - 15, 1.25, "notional added");
});

test("a convert out of the money is repaid in cash in its due month", () => {
  const convert = {note:"Convert", principal:50, coupon:0, units:0, repay:3, convert:null};
  const r = simulateCredit(tiny({tranches:[convert]}), flat({months:6}));
  near(r.repaid, 50, "repaid");
  assert.equal(r.firstSale, 3);                       // nothing else is owed before it
});

test("cash pays first, then sales, and what neither covers is a gap", () => {
  // $1M owed a month: $5M of cash, then 20,000 coin at a flat $100 is two months of sales
  const r = simulateCredit(tiny({cash:5, hold:20000}), flat({opex:12}));
  assert.equal(r.cashOut, 6);
  assert.equal(r.firstSale, 6);
  assert.equal(r.firstGap, 8);
  near(r.soldUsd, 2, "sold");
  near(r.unpaid, 5, "unpaid");
  near(5 + r.soldUsd + r.unpaid, 12, "every dollar owed is paid or counted unpaid");
});

test("the break-even sits on the line between funded and a gap", () => {
  const {s, cfg} = FIXTURES.CHAD, be = breakEven(s, cfg);
  assert.ok(simulateCredit(s, {...cfg, target:be*1.001}).unpaid <= 1e-6, "funded just above");
  assert.ok(simulateCredit(s, {...cfg, target:be*0.999}).unpaid > 1e-6, "a gap just below");
});

test("a crash of 0 is the straight line, so every golden holds", () => {
  for(const co of ["MSTR", "ASST", "DFDV"]){
    const {s, cfg, golden} = FIXTURES[co], r = simulate(s, {...cfg, crash:0, crashMo:6});
    for(const k in golden) near(r[k], golden[k], `${co} ${k}`);
  }
  for(const tk of ["STRC", "SATA", "CHAD"]){
    const {s, golden} = FIXTURES[tk], cfg = {...FIXTURES[tk].cfg, crash:0, crashMo:6};
    const r = {...simulateCredit(s, cfg), breakEven:breakEven(s, cfg)};
    for(const k in golden) near(r[k], golden[k], `${tk} ${k}`);
  }
});

test("a crash bottoms where asked, below the line, and still ends at the target", () => {
  const cfg = {target:150, months:24, crash:60, crashMo:6};
  near(pathPrice(100, cfg, 0), 100, "today");
  near(pathPrice(100, cfg, 6), 112.5*0.4, "the low: 60% under the line's $112.50");
  near(pathPrice(100, cfg, 24), 150, "the horizon");
  assert.equal(crashMonth({months:12, crashMo:30}), 11);           // kept inside the horizon
});

test("same ending price, different path: a crash opens a gap the straight line doesn't", () => {
  // $3M owed a month for two years against 1M coin at a flat $100: the straight line
  // sells 720,000 coin. A 70% crash bottoming in month 6 sells them cheap and runs out.
  const s = tiny(), cfg = flat({months:24, opex:36});
  const line = simulateCredit(s, cfg), crashed = simulateCredit(s, {...cfg, crash:70, crashMo:6});
  near(line.unpaid, 0, "straight line unpaid");
  near(line.sold, 720000, "straight line coin sold");
  assert.equal(crashed.firstGap, 17);                              // well after the low, on the way back up
  assert.ok(crashed.unpaid > 21 && crashed.unpaid < 22, `crash unpaid ${crashed.unpaid}`);
});

test("with a crash, the break-even is higher and still separates funded from a gap", () => {
  const {s, cfg} = FIXTURES.CHAD, crash = {...cfg, crash:60, crashMo:6}, be = breakEven(s, crash);
  assert.ok(be > breakEven(s, cfg), "a crash raises the break-even");
  assert.ok(simulateCredit(s, {...crash, target:be*1.001}).unpaid <= 1e-6, "funded just above");
  assert.ok(simulateCredit(s, {...crash, target:be*0.999}).unpaid > 1e-6, "a gap just below");
});

test("a convert due in a short month is repaid only as far as the money reaches", () => {
  // $1M of costs a month against 300,000 coin at $100. Months 1-2 sell 20,000 coin; in
  // month 3 the $50M convert and $1M of costs meet the last $28M. Debt ranks first, so
  // the costs go unpaid before the debt: $28M of principal is repaid and $22M stays owed.
  // Months 4-12 leave $9M more of costs unpaid.
  const convert = {note:"Convert", principal:50, coupon:0, units:0, repay:3, convert:null};
  const r = simulateCredit(tiny({hold:300000, tranches:[convert]}), flat({opex:12}));
  assert.equal(r.firstGap, 3);
  near(r.repaid, 28, "repaid");
  near(r.unpaidDebt, 22, "debt unpaid");
  near(r.unpaid, 32, "unpaid");
  near(r.claims[12], 22, "the unpaid principal is still a claim");
});

test("a convert in the money converts into shares instead of being repaid", () => {
  const convert = {note:"Convert", principal:50, coupon:0, units:0, repay:null, convert:3};
  const r = simulateCredit(tiny({tranches:[convert]}), flat({months:6}));
  near(r.repaid, 0, "repaid");
  near(r.claims[2], 50, "a claim before");
  near(r.claims[3], 0, "gone after");
});

// A small common issuer: $100M of coin, $20M of debt, 10M shares at $10.
const tinyCo = (over = {}) => ({px:10, sharesM:10, pref:0, spot:100, hold:1e6, cash:0, owed:0, debtUsd:20,
  intUsd:0, intUnits:0, otherNotional:0, otherDivs:0, ...over});
const flatCo = (over = {}) => ({target:100, months:1, prefMo:0, commonMo:0, rate:0, fundMix:0, endMnav:1.5,
  runway:0, atmFloor:1, stake:0, opex:0, ...over});

test("the ending mNAV prices the whole structure", () => {
  near(simulate(tinyCo(), flatCo()).implied, (1.5*100 - 20)/10, "(1.5 x NAV - claims) / shares");
});

test("with the ATM shut below its floor, costs are paid by selling the treasury", () => {
  // $1M of costs a month, all meant to come from issuance, at a 1.5x multiple
  const open = simulate(tinyCo(), flatCo({months:12, opex:12}));
  near(open.assetSold, 0, "open: nothing sold");
  assert.ok(open.endSharesM > 10, "open: shares issued");
  const shut = simulate(tinyCo(), flatCo({months:12, opex:12, atmFloor:3}));
  near(shut.pausedMo, 12, "shut every month");
  near(shut.assetSold, 12, "sold");
  near(shut.forcedDiv, 12, "all of it forced");
  near(shut.endSharesM, 10, "no shares issued");
});

test("stock sold to pay a bill costs existing holders exactly the bill", () => {
  // $12M of costs in the month, at 1.5x: the company is worth 1.5 x $100M - $20M = $130M,
  // so the sale issues 12 x 10 / (130 - 12) shares and leaves the old 10M worth $118M
  const r = simulate(tinyCo(), flatCo({opex:144}));
  near(r.endSharesM, 10 + 12*10/118, "shares");
  near(r.implied*10, 130 - 12, "the old holders' stake");
  near(r.assetSold, 0, "nothing sold");
});

test("premium captured is what existing holders gain from issuance", () => {
  // $30M of common and $30M of preferred in one month at 1.3x: the coin each buys is
  // priced at 1.3x, so each $30M adds $9M to a company that was worth 1.3 x 100 - 20
  const r = simulate(tinyCo(), flatCo({endMnav:1.3, commonMo:30, prefMo:30}));
  near(r.implied*10 - (1.3*100 - 20), 18, "the old 10M shares' gain");
  near(r.premCap, 18, "premium captured");
});

test("the common ATM shuts below the EV floor, and while claims exceed the treasury", () => {
  // EV mNAV runs from 1.2x today to 0.85x in month 12, so it is under 1.0x from month 7
  near(simulate(tinyCo(), flatCo({months:12, endMnav:0.85})).pausedMo, 6, "months shut");
  // $120M of debt against $100M of coin: the market still values the stock above zero,
  // but the ATM stays shut, so a year of $1M monthly costs is all paid in coin
  const r = simulate(tinyCo({px:1, debtUsd:120}), flatCo({months:12, opex:12}));
  near(r.pausedMo, 12, "shut every month");
  near(r.forcedDiv, 12, "all of it forced");
});

test("a bill bigger than the company's market value is paid by selling the treasury", () => {
  // $100M of coin against $99M of debt at 1.0x: the whole company is worth $1M, so the
  // month's $2M of costs can't be paid in stock and coin pays it, though the ATM is open
  const r = simulate(tinyCo({px:0.1, debtUsd:99}), flatCo({endMnav:1, opex:24}));
  near(r.pausedMo, 0, "the ATM never shut");
  near(r.forcedDiv, 2, "forced");
  near(r.assetSold, 2, "coin sold");
  near(r.endSharesM, 10, "no shares issued");
});

test("SOL owed is marked at each month's SOL price, not today's", () => {
  // DFDV's SOL loans grow in dollars as SOL rises; holding them at today's dollars
  // would overstate what the common keeps at a $200 target
  const {s, cfg} = FIXTURES.DFDV, up = {...cfg, target:200};
  const asDollars = {...s, owed:0, debtUsd:s.debtUsd + s.owed*s.spot/1e6,
                     intUnits:0, intUsd:s.intUsd + s.intUnits*s.spot/1e6};
  assert.ok(simulate(s, up).implied < simulate(asDollars, up).implied*0.9);
});

test("a common underwater in a crash recovers with the asset; one still under at the end doesn't", () => {
  // DFDV with a 40% crash bottoming in month 6: claims exceed the SOL held from month 4
  // to month 9, then SOL climbs back to today's price and the common with it
  const {s, cfg} = FIXTURES.DFDV;
  const dip = simulate(s, {...cfg, crash:40, crashMo:6});
  assert.equal(dip.broke.month, 4);
  assert.equal(dip.broke.months, 6);
  assert.equal(dip.broke.atEnd, false);
  assert.ok(dip.implied > 1, `recovered to ${dip.implied}`);
  // SOL ending at $30 never comes back above the claims
  const down = simulate(s, {...cfg, target:30});
  assert.equal(down.broke.atEnd, true);
  near(down.implied, 0.01, "floor");
});

// CHAD's dividend reserve: months of the series' dividends that DFDV holds apart from its cash
test("the reserve is reserveMo months of the selected series' dividends at the rate input", () => {
  // $15.81M of CHAD at 14% pays $2.2134M a year: 12 months of it
  near(reserveFor(15.81, {reserveMo:12, rate:14}), 2.2134, "CHAD's reserve");
  assert.equal(reserveFor(15.81, {rate:14}), 0);                   // every other button
});

test("the reserve holds reserveMo months of dividends, or the whole months cash can fill", () => {
  const chad = {reserveMo:12, rate:14};
  assert.equal(reserveMonths({series:15.81, cash:9.98}, chad), 12);
  // $1M of cash against $0.184M of dividends a month: 5 whole months
  assert.equal(reserveMonths({series:15.81, cash:1}, chad), 5);
  assert.equal(reserveMonths({series:15.81, cash:9.98}, {rate:14}), 0);   // every other button
});

test("the reserve is carved out of today's cash and held, and today's multiple doesn't move", () => {
  // $20M of preferred at 10% pays $2M a year, so the reserve is $2M of the $5M of cash. The
  // other $3M pays 12 months of $1/6M and leaves $1M. Cash and the reserve offset claims
  // alike, so claims end at $20M of debt + $20M of preferred − $1M − $2M = $37M with or
  // without it, and at 1.5x the price is (150 − 37)/10 = $11.30 either way
  const s = tinyCo({pref:20, cash:5}), cfg = flatCo({months:12, rate:10});
  const held = simulate(s, {...cfg, reserveMo:12}), none = simulate(s, cfg);
  assert.ok(Object.is(held.mnav0, none.mnav0), "today's multiple");
  near(held.endReserve, 2, "reserve held");
  assert.equal(held.reserveShort, null);
  near(held.endOpenCash, 1, "cash left outside it");
  near(held.implied, 11.3, "implied");
  near(none.implied, 11.3, "implied without it");
  // $1M of cash against a $2M reserve: the reserve holds all of it, flagged short from the
  // start, and no coin is sold to fill the rest. The common pays the month's $1/6M
  // dividend in stock; from CHAD's seat coin pays it, and only it.
  const short = simulate(tinyCo({pref:20, cash:1}), flatCo({rate:10, reserveMo:12}));
  assert.equal(short.reserveShort, 0);
  near(short.endReserve, 1, "all of today's cash");
  near(short.assetSold, 0, "coin sold");
  const seat = simulateCredit(tiny({cash:1, series:20}), flat({months:1, rate:10, reserveMo:12}));
  assert.equal(seat.reserveShort, 0);
  near(seat.endReserve, 1, "all of today's cash, from CHAD's seat");
  near(seat.soldUsd, 1/6, "coin sold for the dividend alone");
});

test("a preferred raise fills the reserve's growth first, then the runway, then buys the asset", () => {
  // $3M raised on $20M at 10% grows the reserve from $2M to $2.3M, so $0.3M of it tops the
  // reserve up, six months of the $23M's dividends ($1.15M) park as runway, and the last
  // $1.55M buys 15,500 coin at $100. The $1M of cash outside the reserve pays the dividend.
  const r = simulate(tinyCo({pref:20, cash:3}), flatCo({prefMo:3, runway:6, rate:10, reserveMo:12}));
  near(r.endReserve, 2.3, "reserve");
  near(r.endCash, 1.15, "runway");
  near(r.endHold, 1015500, "coin");
  near(r.parked, 0.3 + 1.15, "parked: the reserve's share and the runway");
  near(r.premCap, 1.55*(1.5 - 1), "premium captured on the $1.55M of coin alone");
  // From CHAD's seat: $1M raised at $9.99 adds $10/9.99M of stated notional, so a 12-month
  // reserve at 14% takes 0.14 × 10/9.99 of the $1M and the rest buys coin
  const c = simulateCredit(tiny({cash:3, series:15.81}), flat({months:1, prefMo:1, issuePx:9.99, rate:14, reserveMo:12}));
  near(c.endReserve - 2.2134, 0.14*10/9.99, "reserve topped up");
});

test("the reserve pays only once the cash outside it and the treasury can't", () => {
  // $20M of preferred at 10% holds $2M of the $12M of cash in reserve. $10M a year of costs
  // and $2M of dividends are $1M a month: the other $10M pays months 1-10 and the 20,000
  // coin ($2M at a flat $100) months 11-12, so a year leaves the reserve whole
  const s = tiny({cash:12, hold:20000, series:20}), cfg = flat({rate:10, opex:10, reserveMo:12});
  const year = simulateCredit(s, cfg);
  assert.equal(year.cashOut, 11);
  assert.equal(year.firstSale, 11);
  near(year.sold, 20000, "every coin sold");
  near(year.endReserve, 2, "the reserve whole");
  assert.equal(year.reserveShort, null);
  // three months more: the reserve pays months 13-14, and month 15 is the first gap
  const more = simulateCredit(s, {...cfg, months:15});
  assert.equal(more.reserveShort, 13);
  assert.equal(more.firstGap, 15);
  near(more.unpaid, 1, "unpaid");
});

test("with no reserve, DFDV and CHAD give exactly the goldens they had before it", () => {
  const pre = {
    DFDV: {implied:3.603919409501438, endHold:2724687.5748565323, endSharesM:38.10249356729776, assetSold:0, pausedMo:0, broke:null},
    CHAD: {unpaid:0, firstGap:null, cashOut:4, firstSale:4, sold:2530182.5238330057, repaid:126.052, coverEnd:-1.6300013586043436, breakEven:73.34536690751364},
  };
  const d = simulate(FIXTURES.DFDV.s, {...FIXTURES.DFDV.cfg, reserveMo:0});
  for(const k in pre.DFDV) assert.equal(d[k], pre.DFDV[k], k);
  const {s} = FIXTURES.CHAD, cfg = {...FIXTURES.CHAD.cfg, reserveMo:0};
  const c = {...simulateCredit(s, cfg), breakEven:breakEven(s, cfg)};
  for(const k in pre.CHAD) assert.equal(c[k], pre.CHAD[k], k);
});

test("a break-even of 0 can rest on the reserve: 12 months are funded at any price, 13 at none", () => {
  // No coin to sell, $12M of cash and $1M owed a month, $2M of it the reserve. The cash
  // outside it runs out after month 10, so cash alone would leave $2M unpaid; the reserve
  // pays months 11-12. Nothing is left for month 13, whatever the coin does.
  const s = tiny({cash:12, hold:0, series:20}), cfg = flat({rate:10, opex:10, reserveMo:12});
  const r = simulateCredit(s, cfg);
  assert.equal(r.cashOut, 11);
  assert.equal(r.reserveShort, 11);
  near(r.unpaid, 0, "unpaid");
  assert.equal(breakEven(s, cfg), 0);
  assert.equal(breakEven(s, {...cfg, months:13}), null);
});

// The gross durations' run-off. $100M paying $10M a year makes the arithmetic checkable.
test("with no debt falling due, the run-off is the reserve over the obligations", () => {
  near(runoffYears(100, 8, []), 12.5, "100 / 8");
});

test("a repaid convert's principal leaves the reserve on its date, and its coupon stops", () => {
  // $20M paid by year 2 leaves $80M; the $30M convert leaves $50M at $9M a year
  near(runoffYears(100, 10, [{at:2, principal:30, coupon:1}]), 2 + 50/9, "years");
});

test("a converting tranche takes no principal, only its coupon stops", () => {
  near(runoffYears(100, 10, [{at:2, principal:0, coupon:1}]), 2 + 80/9, "years");
});

test("exits are taken in date order, whatever order they come in", () => {
  // year 1: 90 left, 20 repaid, 70 at $8M a year; year 3: 54 left, 10 repaid, 44 at $7M a year
  near(runoffYears(100, 10, [{at:3, principal:10, coupon:1}, {at:1, principal:20, coupon:2}]), 3 + 44/7, "years");
});

test("a principal the reserve can't meet ends the run on its date", () => {
  near(runoffYears(100, 10, [{at:2, principal:90, coupon:1}]), 2, "years");
});

test("the reserve can run out before a tranche falls due", () => {
  near(runoffYears(15, 10, [{at:2, principal:30, coupon:1}]), 1.5, "years");
});

test("a tranche leaves at its next put or maturity: repaid out of the money, converted in it", () => {
  const april = {put:"2028-04-06", maturity:"2030-04-06", convPrice:9.74143};
  near(trancheExit(april, 3.99, "2026-10-30"), {date:"2028-04-06", converts:false}, "put ahead, out of the money");
  near(trancheExit(april, 10, "2026-10-30"), {date:"2028-04-06", converts:true}, "put ahead, in the money");
  near(trancheExit(april, 3.99, "2028-05-01"), {date:"2030-04-06", converts:false}, "put passed: maturity");
  near(trancheExit({put:null, maturity:"2027-06-30", convPrice:null}, 100, "2026-10-30"),
       {date:"2027-06-30", converts:false}, "straight debt is repaid");
  near(trancheExit({put:null, maturity:null, convPrice:null}, 3.99, "2026-10-30"), null, "an undated loan rolls over");
});

test("DFDV on 2026-10-30: its converts' $126M falls due, so its SOL lasts 15.4 years, not 19.1", () => {
  // 2,595,612 SOL at $95.19 against $12.93M a year (CHAD at 14%). The April notes are put
  // on 2028-04-06 and the July notes mature on 2030-07-01, both far out of the money at
  // $3.99; after them only CHAD and the undated loans are left, $6.34M a year.
  const yrs = d => (Date.parse(d) - Date.parse("2026-10-30"))/(365.25*864e5);
  const exits = [{at:yrs("2028-04-06"), principal:11.471, coupon:11.471*2.5/100},
                 {at:yrs("2030-07-01"), principal:114.581, coupon:114.581*5.5/100}];
  near(runoffYears(2595612*95.19/1e6, 12.93, exits), 15.374330453640416, "years");
});
