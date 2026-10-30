/* The calculator's engine: pure functions of a balance-sheet snapshot and the
   calculator's inputs. index.html builds the snapshots from data.json and loads
   this file as a plain script; tests/calc-engine.test.js requires it under Node.
   Keep it free of DATA, the DOM and other page globals so both can run it. */

// The month a crash bottoms: the one asked for, kept strictly inside the horizon.
function crashMonth(cfg){
  return Math.min(Math.max(1, Math.round(cfg.crashMo || 1)), cfg.months - 1);
}
// The treasury asset's price in month m: a straight line from today's price to the
// target, dipped by a crash that bottoms cfg.crash % below that line in crashMonth()
// and is back on the line by the horizon. Month 0 is today's price and the last month
// is the target either way; a crash of 0, or a one-month horizon, is the straight line.
function pathPrice(spot, cfg, m){
  const line = spot + (cfg.target - spot)*m/cfg.months;
  if(!(cfg.crash > 0) || cfg.months < 2) return line;
  const low = crashMonth(cfg);
  const w = m <= low ? m/low : (cfg.months - m)/(cfg.months - low);
  return line*(1 - cfg.crash/100*w);
}
// A month of preferred dividends in $M: `issued` (the selected series, notional in $M,
// including anything raised of it) at the rate input, every other series at its own.
function prefDivsMo(s, issued, rate){
  return (s.otherDivs + issued*rate/100)/12;
}
// The dividend reserve in $M: cfg.reserveMo months of the selected series' dividends at the
// rate input, `issued` its notional in $M (anything raised included). Only DFDV holds one.
// Both engines carve it out of today's cash. Cash short of it leaves it short, flagged as
// reserveShort 0, rather than filled by selling the treasury: a sale isn't neutral (it gives
// up the mNAV spread and locks in today's price), and DFDV's cash was 4.5× the reserve in
// October 2026.
function reserveFor(issued, cfg){ return (cfg.reserveMo || 0)*issued*cfg.rate/1200; }
function simulate(s, cfg){
  // EV mNAV framing, on basic shares: mNAV = (mcap + debt + pref - cash) / crypto NAV, so
  // each month market cap backs out as mNAV x NAV - claims and price = mcap / shares.
  // Converts are held at notional throughout — the model doesn't flip ITM tranches
  // into share count.
  // `buf` is the dividend cash runway: issuance proceeds parked as cash to
  // pre-fund dividends. It offsets claims at face value and never earns the mNAV
  // multiple, so parked dollars are per-share neutral (vs asset-bought dollars,
  // which capture the mNAV premium spread).
  // `cash` is today's cash: it pays dividends, interest and costs before anything
  // else, and offsets claims at its current balance, so spending it raises claims.
  // `res` is the dividend reserve (reserveFor), carved out of today's cash once today's
  // multiple is read and topped up first by a preferred raise as the series grows. It
  // offsets claims like cash and is never spent: it would pay only once sales of the
  // treasury can't, and a treasury sold out ends the run.
  let cash = s.cash, res = 0;
  const nav = (hold,b)=> hold*b/1e6;                                   // $mm
  const claims = (pref,buf,b) => s.debtUsd + s.owed*b/1e6 + pref - cash - res - buf;   // $mm
  const priceAt = (hold,b,pref,buf,sh,mn)=> Math.max((mn*nav(hold,b) - claims(pref,buf,b)) / sh, 0.01);
  const cebeShare = (hold,b,pref,buf,sh)=> (nav(hold,b) - claims(pref,buf,b)) / sh;
  const nav0 = nav(s.hold, s.spot);
  if(nav0<=0) return null;
  const mnav0 = (s.px*s.sharesM + claims(s.pref,0,s.spot)) / nav0;    // current EV mNAV
  const cebe0 = cebeShare(s.hold, s.spot, s.pref, 0, s.sharesM);
  const resFor = pref => reserveFor(pref - s.otherNotional, cfg);     // pref: the whole stack
  res = Math.max(0, Math.min(cash, resFor(s.pref))); cash -= res;
  const reserveShort = res < resFor(s.pref) ? 0 : null;
  let hold=s.hold, sharesM=s.sharesM, pref=s.pref, buf=0, p=s.px;
  let raised=0, raisedMnWt=0, parked=0, premCap=0;
  let pausedMo=0, forcedDiv=0, assetSold=0, broke=null;
  const idxStock=[100], idxAsset=[100], labels=["0"];
  for(let m=1;m<=cfg.months;m++){
    const b  = pathPrice(s.spot, cfg, m);
    const mn = mnav0 + (cfg.endMnav - mnav0)*m/cfg.months;
    if(broke && broke.exhausted){                                      // the treasury is sold out:
      broke.months++;                                                  // nothing left to recover with
      idxStock.push(0); idxAsset.push(b/s.spot*100); labels.push(String(m)); continue;
    }
    hold *= 1 + cfg.stake/1200;                                        // staking accrues in units (SOL)
    // Is the common ATM open this month? Management issues only while EV mNAV is at or
    // above the floor, judged before this month's issuance — the same information a
    // board would act on. A 1.0x floor is the accretion threshold: there the market cap
    // equals the treasury left after every claim, the line Strategy states as a hard
    // 1.0x of its net multiple (Strive behaves the same). Whatever the floor, the ATM
    // stays shut while claims exceed the treasury or the stock is worth nothing.
    const nav_ = nav(hold,b), cl_ = claims(pref,buf,b);
    const atmOpen = nav_ - cl_ > 0 && mn*nav_ > cl_ && mn >= cfg.atmFloor;
    if(!atmOpen) pausedMo++;
    pref += cfg.prefMo;
    const divMo = prefDivsMo(s, pref - s.otherNotional, cfg.rate);
    // debt interest and operating costs ride the dividends' waterfall below
    const costMo = (s.intUsd + s.intUnits*b/1e6 + cfg.opex)/12;
    // preferred ATM: the reserve's growth first, then the runway, the rest -> the asset
    const rp = Math.min(cfg.prefMo, Math.max(0, resFor(pref) - res)); res += rp;
    let gap = Math.max(0, cfg.runway*divMo - buf);                     // cash still to park
    const pc = Math.min(cfg.prefMo - rp, gap); gap -= pc;
    buf += pc; hold += (cfg.prefMo - rp - pc)*1e6/b;
    premCap += (cfg.prefMo - rp - pc)*(mn-1);
    // Common ATM, the same split. Its proceeds land first and the sale is priced on the
    // result: C raised on M, the market value with the buyers' money in, issues
    // C*S/(M - C) shares, so existing holders keep M - C. Only a floor below 1x can
    // leave M short of C, and then nothing sells.
    let commonMo = atmOpen ? cfg.commonMo : 0;                         // ATM shut below the floor
    const cc = Math.min(commonMo, gap);
    const mcap = commonMo>0 ? priceAt(hold + (commonMo - cc)*1e6/b, b, pref, buf + cc, sharesM, mn)*sharesM : 0;
    if(!(mcap > commonMo)) commonMo = 0;
    if(commonMo>0){
      buf += cc; hold += (commonMo - cc)*1e6/b; sharesM += commonMo*sharesM/(mcap - commonMo);
      parked += cc; premCap += (commonMo - cc)*(mn-1);
    }
    parked += rp + pc;
    raised += cfg.prefMo + commonMo; raisedMnWt += (cfg.prefMo + commonMo)*mn;
    // payment waterfall: today's cash -> runway cash -> common issuance (only while
    // the ATM is open, and only for a bill smaller than the company's market value)
    // -> forced sales of the treasury for whatever is left. The mix
    // slider is a preference, not a guarantee: below the floor, selling coin is the only door.
    let div = divMo + costMo;
    const fc = Math.min(div, cash); cash -= fc; div -= fc;
    const fb = Math.min(div, buf); buf -= fb; div -= fb;
    if(div>0){
      let viaAsset = div*cfg.fundMix, viaEq = div - viaAsset;
      if(!atmOpen && viaEq>0){ forcedDiv += viaEq; viaAsset += viaEq; viaEq = 0; }
      if(viaAsset>0){ hold -= viaAsset*1e6/b; assetSold += viaAsset; }
      if(viaEq>0){
        // Stock sold to pay a bill is priced after its own dilution, as the ATM's raise is
        // above. The money goes out, so the market cap M doesn't move and existing holders
        // bear exactly the bill: X issues X*S/(M - X) shares. A bill the whole company
        // isn't worth can't be paid in stock, so the treasury pays.
        const mcap = priceAt(hold,b,pref,buf,sharesM,mn)*sharesM;
        if(viaEq < mcap) sharesM += viaEq*sharesM/(mcap - viaEq);
        else { forcedDiv += viaEq; hold -= viaEq*1e6/b; assetSold += viaEq; }
      }
    }
    p = priceAt(hold,b,pref,buf,sharesM,mn);
    // Underwater: senior claims have eaten the reserves, so the common is worth nothing
    // this month. If the asset recovers, so can the common; a treasury sold to zero can't.
    // broke keeps the first such month, how many there were, and whether the last is one.
    const exhausted = hold<=0, under = exhausted || (nav(hold,b) - claims(pref,buf,b))<=0;
    if(under){
      broke = broke || { month:m, px:Math.round(b*100)/100, months:0 };
      broke.months++; broke.exhausted = exhausted;
      broke.why = exhausted ? "the treasury is exhausted by sales to pay dividends and costs"
                            : "senior claims exceed total reserves";
      hold = Math.max(hold, 0); p = 0.01;
    }
    if(broke) broke.atEnd = under;
    idxStock.push(p/s.px*100); idxAsset.push(b/s.spot*100); labels.push(String(m));
  }
  const cebeEnd = cebeShare(hold, cfg.target, pref, buf, sharesM);
  const stockX = p/s.px, assetX = cfg.target/s.spot;
  // per-share accretion in the asset's own units, so the decomposition is exact:
  // stock = asset move x CEBE accretion x residual mNAV re-rate
  const unitAcc = cebe0>0 && cebeEnd>0 ? (cebeEnd/cfg.target)/(cebe0/s.spot) : 1;
  return { implied:p, stockX, assetX,
           cebe0, cebeEnd, cebeX:unitAcc, mnav0, mnavX:stockX/(assetX*unitAcc),
           endHold:hold, endSharesM:sharesM, endPref:pref, endCash:buf, endOpenCash:cash,
           endReserve:res, reserveShort, raised, avgIssMn: raised>0 ? raisedMnWt/raised : null, parked, premCap,
           pausedMo, forcedDiv, assetSold, broke,
           endClaims:claims(pref,buf,cfg.target)/nav(hold,cfg.target)*100,
           idxStock, idxAsset, labels };
}

// The issuer's cash flows from the selected preferred's seat, month by month: debt
// interest and any principal due come first, then operating costs, then every
// preferred series' dividends; opening cash pays first, then sales of the treasury,
// then the dividend reserve (reserveFor), held apart from the cash until both run out.
// Whatever is left unpaid is a modeled funding gap. No new money comes in unless
// cfg.prefMo raises more of the selected series.
function simulateCredit(s, cfg){
  let hold = s.hold, cash = s.cash, series = s.series;
  // reserveShort: 0 if opening cash can't fill the reserve, else the first month it pays, if any
  let res = Math.max(0, Math.min(cash, reserveFor(series, cfg))), reserveShort = res < reserveFor(series, cfg) ? 0 : null;
  cash -= res;
  const live = s.tranches.map(t => ({...t}));
  let unpaid = 0, unpaidDebt = 0, firstGap = null, gapPx = null, cashOut = null, firstSale = null, sold = 0, soldUsd = 0, repaid = 0;
  // treasury + cash (the reserve's included), and debt + preferred, in $M at price P
  const mark = P => [hold*P/1e6 + cash + res,
    live.reduce((d, t) => d + (t.units ? t.units*P/1e6 : t.principal), 0) + s.otherNotional + series];
  const labels = ["0"], assets = [mark(s.spot)[0]], claims = [mark(s.spot)[1]];
  for(let m = 1; m <= cfg.months; m++){
    const P = pathPrice(s.spot, cfg, m);                             // same path as simulate()
    hold *= 1 + cfg.stake/1200;                                      // staking accrues in units
    if(cfg.prefMo > 0){                                              // raise more of the series:
      series += cfg.prefMo*s.stated/cfg.issuePx;                     // shares × stated amount
      const top = Math.min(cfg.prefMo, Math.max(0, reserveFor(series, cfg) - res));
      res += top;                                                    // proceeds top up the reserve,
      hold += (cfg.prefMo - top)*1e6/P;                              // then buy the treasury asset
    }
    let interest = 0, principal = 0;
    const due = [];
    for(let i = live.length - 1; i >= 0; i--){
      const t = live[i], value = t.units ? t.units*P/1e6 : t.principal;
      interest += value*t.coupon/1200;
      if(t.repay === m){ principal += value; due.push(t); }
      else if(t.convert === m) live.splice(i, 1);                    // in the money: becomes shares
    }
    const junior = cfg.opex/12 + prefDivsMo(s, series, cfg.rate);   // costs and dividends
    let need = interest + principal + junior;
    const fromCash = Math.min(cash, need); cash -= fromCash; need -= fromCash;
    if(need > 1e-9){
      if(cashOut == null) cashOut = m;
      const units = Math.min(hold, need*1e6/P);
      if(units > 0){
        if(firstSale == null) firstSale = m;
        hold -= units; sold += units; soldUsd += units*P/1e6; need -= units*P/1e6;
      }
      if(need > 1e-9 && res > 0){                                    // the reserve pays last
        const fromRes = Math.min(res, need); res -= fromRes; need -= fromRes;
        if(reserveShort == null) reserveShort = m;
      }
    }
    const short = need > 1e-6 ? need : 0;
    if(short){ unpaid += short; if(firstGap == null){ firstGap = m; gapPx = P; } }
    // Debt ranks first, so a shortfall falls on costs and dividends before it reaches
    // the debt, and interest is met before principal. A convert is repaid only as far
    // as the money reaches; what's left of it stays owed, and keeps its coupon.
    const debtShort = Math.max(0, short - junior);
    unpaidDebt += debtShort;
    const principalPaid = Math.max(0, principal - debtShort);
    repaid += principalPaid;
    const left = principal > 0 ? 1 - principalPaid/principal : 0;
    due.forEach(t => {
      if(left > 1e-9){ t.principal *= left; t.units *= left; t.repay = null; }
      else live.splice(live.indexOf(t), 1);
    });
    const [a, c] = mark(P); assets.push(a); claims.push(c); labels.push(String(m));
  }
  const [aEnd, cEnd] = mark(cfg.target), prefEnd = s.otherNotional + series;
  return { unpaid, unpaidDebt, firstGap, gapPx, cashOut, firstSale, sold, soldUsd, repaid, endSeries:series,
           endReserve:res, reserveShort,
           coverEnd: prefEnd > 0 ? (aEnd - (cEnd - prefEnd))/prefEnd : null,
           assets, claims, labels };
}
// The lowest ending price of the treasury asset that still funds every payment through
// the horizon, holding the other inputs (a crash keeps its depth and month): 0 when one
// 99.99% below today's still does — cash and staking alone, or with a dividend reserve,
// the treasury sold on the way down and the reserve paying the final months — and null
// when no price up to 20× today does. A higher ending price raises every month's price,
// so it never makes a payment harder.
function breakEven(s, cfg){
  const gap = target => simulateCredit(s, {...cfg, target}).unpaid > 1e-6;
  let lo = s.spot*1e-4, hi = s.spot*20;
  if(!gap(lo)) return 0;
  if(gap(hi)) return null;
  for(let i = 0; i < 40; i++){ const mid = (lo + hi)/2; if(gap(mid)) lo = mid; else hi = mid; }
  return hi;
}

if(typeof module !== "undefined") module.exports = { crashMonth, pathPrice, prefDivsMo, reserveFor, simulate, simulateCredit, breakEven };
