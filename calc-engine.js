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
function simulate(s, cfg){
  // EV mNAV framing: mNAV = (mcap + debt + pref - cash) / crypto NAV, so each month
  // market cap backs out as mNAV x NAV - claims and price = mcap / shares.
  // `buf` is the dividend cash runway: issuance proceeds parked as cash to
  // pre-fund dividends. It offsets claims at face value. On the EV basis it never
  // earns the mNAV multiple, so parked dollars are per-share neutral (vs asset-bought
  // dollars, which capture the mNAV premium spread); on the net basis it sits in net
  // NAV and earns the multiple like the asset.
  // `cash` is today's cash: it pays dividends, interest and costs before anything
  // else, and offsets claims at its current balance, so spending it raises claims.
  let cash = s.cash;
  const nav = (hold,b)=> hold*b/1e6;                                   // $mm
  const claims = (pref,buf,b) => s.debtUsd + s.owed*b/1e6 + pref - cash - buf;   // $mm
  // ending-multiple basis: EV prices the whole structure (mcap = mn×NAV − claims);
  // net prices the common's residual (mcap = mn×(NAV − claims)). Converts are held
  // at notional throughout — the model doesn't flip ITM tranches into share count.
  // Net mNAV divides a fully-diluted market cap, as the page's header does, so on that
  // basis every share count carries `dil`: options, RSUs and the like beyond basic
  // (s.dilM, held constant). EV mNAV is on basic shares, as the page's is.
  const dil = cfg.basis==="net" ? (s.dilM || 0) : 0;
  const priceAt = (hold,b,pref,buf,sh,mn)=> Math.max(
    (cfg.basis==="net" ? mn*(nav(hold,b) - claims(pref,buf,b))
                       : mn*nav(hold,b) - claims(pref,buf,b)) / (sh + dil), 0.01);
  const cebeShare = (hold,b,pref,buf,sh)=> (nav(hold,b) - claims(pref,buf,b)) / sh;
  const nav0 = nav(s.hold, s.spot);
  if(nav0<=0) return null;
  const mnav0 = (s.px*s.sharesM + claims(s.pref,0,s.spot)) / nav0;    // current EV mNAV
  const netNav0 = nav0 - claims(s.pref,0,s.spot);
  const net0 = netNav0>0 ? (s.px*(s.sharesM + dil))/netNav0 : null;    // current net mNAV
  const mn0 = (cfg.basis==="net" && net0) ? net0 : mnav0;              // interpolation start
  const cebe0 = cebeShare(s.hold, s.spot, s.pref, 0, s.sharesM);
  let hold=s.hold, sharesM=s.sharesM, pref=s.pref, buf=0, p=s.px;
  let raised=0, raisedMnWt=0, parked=0, premCap=0;
  let pausedMo=0, forcedDiv=0, assetSold=0, broke=null;
  const idxStock=[100], idxAsset=[100], labels=["0"];
  for(let m=1;m<=cfg.months;m++){
    const b  = pathPrice(s.spot, cfg, m);
    const mn = mn0 + (cfg.endMnav - mn0)*m/cfg.months;
    if(broke && broke.exhausted){                                      // the treasury is sold out:
      broke.months++;                                                  // nothing left to recover with
      idxStock.push(0); idxAsset.push(b/s.spot*100); labels.push(String(m)); continue;
    }
    hold *= 1 + cfg.stake/1200;                                        // staking accrues in units (SOL)
    // Is the common ATM open this month? Management issues only when the stock
    // trades above the accretion threshold (Strategy states a hard 1.0x on the
    // net multiple; Strive behaves the same). Evaluated on the NET multiple
    // regardless of which basis the user is modelling in, before this month's
    // issuance — the same information a board would act on.
    const nav_ = nav(hold,b), cl_ = claims(pref,buf,b), netNav_ = nav_ - cl_;
    const netMn = netNav_>0 ? (cfg.basis==="net" ? mn : (mn*nav_ - cl_)/netNav_) : 0;
    const atmOpen = netNav_>0 && netMn >= cfg.atmFloor;
    if(!atmOpen) pausedMo++;
    pref += cfg.prefMo;
    const divMo = prefDivsMo(s, pref - s.otherNotional, cfg.rate);
    // debt interest and operating costs ride the dividends' waterfall below
    const costMo = (s.intUsd + s.intUnits*b/1e6 + cfg.opex)/12;
    let gap = Math.max(0, cfg.runway*divMo - buf);                     // cash still to park
    const pc = Math.min(cfg.prefMo, gap); gap -= pc;                   // preferred ATM:
    buf += pc; hold += (cfg.prefMo - pc)*1e6/b;                        // runway first, rest -> the asset
    premCap += (cfg.basis==="net" ? 0 : cfg.prefMo - pc)*(mn-1);       // net: its claim offsets its cash
    // Common ATM, the same split. Its proceeds land first and the sale is priced on the
    // result: C raised on M, the market value with the buyers' money in, issues
    // C*S/(M - C) shares, so existing holders keep M - C. S and M count the diluted
    // shares too (dil), which own their slice of M. Only a floor below 1x can leave
    // M short of C, and then nothing sells.
    let commonMo = atmOpen ? cfg.commonMo : 0;                         // ATM shut below the floor
    const cc = Math.min(commonMo, gap);
    const mcap = commonMo>0 ? priceAt(hold + (commonMo - cc)*1e6/b, b, pref, buf + cc, sharesM, mn)*(sharesM + dil) : 0;
    if(!(mcap > commonMo)) commonMo = 0;
    if(commonMo>0){
      buf += cc; hold += (commonMo - cc)*1e6/b; sharesM += commonMo*(sharesM + dil)/(mcap - commonMo);
      parked += cc; premCap += (cfg.basis==="net" ? commonMo : commonMo - cc)*(mn-1);   // net: parked cash earns it too
    }
    parked += pc;
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
        const mcap = priceAt(hold,b,pref,buf,sharesM,mn)*(sharesM + dil);
        if(viaEq < mcap) sharesM += viaEq*(sharesM + dil)/(mcap - viaEq);
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
           cebe0, cebeEnd, cebeX:unitAcc, mnav0, net0, mn0, mnavX:stockX/(assetX*unitAcc),
           endHold:hold, endSharesM:sharesM, endPref:pref, endCash:buf, endOpenCash:cash,
           raised, avgIssMn: raised>0 ? raisedMnWt/raised : null, parked, premCap,
           pausedMo, forcedDiv, assetSold, broke,
           endClaims:claims(pref,buf,cfg.target)/nav(hold,cfg.target)*100,
           idxStock, idxAsset, labels };
}

// The issuer's cash flows from the selected preferred's seat, month by month: debt
// interest and any principal due come first, then operating costs, then every
// preferred series' dividends; opening cash pays first, then sales of the treasury.
// Whatever is left unpaid is a modeled funding gap. No new money comes in unless
// cfg.prefMo raises more of the selected series.
function simulateCredit(s, cfg){
  let hold = s.hold, cash = s.cash, series = s.series;
  const live = s.tranches.map(t => ({...t}));
  let unpaid = 0, unpaidDebt = 0, firstGap = null, gapPx = null, cashOut = null, firstSale = null, sold = 0, soldUsd = 0, repaid = 0;
  // treasury + cash, and debt + preferred, in $M at price P
  const mark = P => [hold*P/1e6 + cash,
    live.reduce((d, t) => d + (t.units ? t.units*P/1e6 : t.principal), 0) + s.otherNotional + series];
  const labels = ["0"], assets = [mark(s.spot)[0]], claims = [mark(s.spot)[1]];
  for(let m = 1; m <= cfg.months; m++){
    const P = pathPrice(s.spot, cfg, m);                             // same path as simulate()
    hold *= 1 + cfg.stake/1200;                                      // staking accrues in units
    if(cfg.prefMo > 0){                                              // raise more of the series:
      series += cfg.prefMo*s.stated/cfg.issuePx;                     // shares × stated amount
      hold += cfg.prefMo*1e6/P;                                      // proceeds buy the treasury asset
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
           coverEnd: prefEnd > 0 ? (aEnd - (cEnd - prefEnd))/prefEnd : null,
           assets, claims, labels };
}
// The lowest ending price of the treasury asset that still funds every payment through
// the horizon, holding the other inputs (a crash keeps its depth and month): 0 when cash
// and staking alone do, null when no price up to 20× today does. A higher ending price
// raises every month's price, so it never makes a payment harder.
function breakEven(s, cfg){
  const gap = target => simulateCredit(s, {...cfg, target}).unpaid > 1e-6;
  let lo = s.spot*1e-4, hi = s.spot*20;
  if(!gap(lo)) return 0;
  if(gap(hi)) return null;
  for(let i = 0; i < 40; i++){ const mid = (lo + hi)/2; if(gap(mid)) lo = mid; else hi = mid; }
  return hi;
}

if(typeof module !== "undefined") module.exports = { crashMonth, pathPrice, prefDivsMo, simulate, simulateCredit, breakEven };
