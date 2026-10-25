// Calculator engine tests. Run: node --test tests/*.test.js
//
// Each fixture is one calculator button's opening snapshot and default inputs, frozen
// from data.json on 2026-10-25 so a data refresh can't move them. The goldens are what
// the engine returned for them when it moved out of index.html unchanged: an edit that
// shifts one has changed what the page shows, and its commit should say so.
const test = require("node:test");
const assert = require("node:assert/strict");
const { crashMonth, pathPrice, simulate, simulateCredit, breakEven } = require("../calc-engine.js");

const FIXTURES = {
  MSTR: {
    s: {px:146.01, sharesM:422.35, pref:13557, spot:80472, hold:848517, cash:5281, owed:0, debtUsd:6714, intUsd:34.5953, intUnits:0},
    cfg: {target:150000, months:12, prefMo:0, commonMo:1579, rate:12, fundMix:0, endMnav:1, runway:0, basis:"net", atmFloor:1, stake:0, opex:0},
    golden: {implied:263.0091652990986, endHold:1013760.6016336514, endSharesM:521.1760969970939, assetSold:0, pausedMo:0, broke:null},
  },
  ASST: {
    s: {px:28.97, sharesM:124.06, pref:2103, spot:80472, hold:36818, cash:976, owed:0, debtUsd:0, intUsd:0, intUnits:0},
    cfg: {target:150000, months:12, prefMo:207, commonMo:334, rate:13, fundMix:0, endMnav:1, runway:0, basis:"net", atmFloor:1, stake:0, opex:0},
    golden: {implied:45.400577273337326, endHold:90876.12883711548, endSharesM:220.7112756571065, assetSold:49.692499999999995, pausedMo:1, broke:null},
  },
  DFDV: {
    s: {px:3.78, sharesM:32.62, pref:15.81, spot:101.4, hold:2590266, cash:9.98, owed:562000, debtUsd:149.0532, intUsd:7.5087, intUnits:33725},
    cfg: {target:101.4, months:12, prefMo:0, commonMo:0, rate:13, fundMix:0, endMnav:1, runway:0, basis:"net", atmFloor:1, stake:5.07, opex:17},
    golden: {implied:1.4273348098755327, endHold:2724687.5748565323, endSharesM:45.12838868973522, assetSold:0, pausedMo:0, broke:null},
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
    cfg: {target:150000, months:60, prefMo:0, rate:12, issuePx:99.36, reqYield:12.0773, stake:0, opex:0},
    golden: {unpaid:0, firstGap:null, cashOut:18, firstSale:18, sold:77117.8300949206, repaid:6713.75, coverEnd:8.535064946947111, breakEven:313.14230393831235},
  },
  SATA: {
    s: {spot:80472, hold:36818, cash:976, owed:0, debtUsd:0, intUsd:0, intUnits:0, stated:100, quote:98.02,
        series:1403, rate:13, otherNotional:700, otherDivs:105, tranches:[]},
    cfg: {target:150000, months:60, prefMo:0, rate:13, issuePx:98.02, reqYield:13.2626, stake:0, opex:0},
    golden: {unpaid:0, firstGap:null, cashOut:41, firstSale:41, sold:3313.2299344867993, repaid:0, coverEnd:2.389783884844024, breakEven:4683.269706652449},
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
    cfg: {target:101.4, months:60, prefMo:0, rate:13, issuePx:9.9, reqYield:13.1313, stake:5.07, opex:17},
    golden: {unpaid:0, firstGap:null, cashOut:4, firstSale:4, sold:2530182.5238330057, repaid:126.052, coverEnd:-1.6300013586043436, breakEven:73.34536690751364},
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
