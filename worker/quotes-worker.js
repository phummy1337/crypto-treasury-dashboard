/**
 * treasurytracker quote relay — Cloudflare Worker
 *
 * WHY THIS EXISTS
 * Two problems the browser cannot solve on its own:
 *   1. strategy.com can sit on a pre-close last trade after the bell (on
 *      2026-09-18 it still showed 150.52 at 5:20pm against a 153.92 close), and
 *      it covers only MSTR and STRC.
 *   2. The strategytracker feed republishes yesterday's close for the first
 *      ~30-45 minutes of each session, which is where ASST gets stuck, and SATA
 *      has no browser-reachable quote source at all.
 *
 * Cboe publishes exactly what is needed for all four tickers — an explicit
 * `close` field carrying the official 4:00pm print, separate from the
 * after-hours last trade — but sends no Access-Control-Allow-Origin and 403s
 * preflight, so a page cannot read it. This relays it.
 *
 * NOT AN OPEN PROXY. The symbol list is hardcoded and the upstream is a single
 * fixed host. A caller cannot ask it to fetch anything else. That distinction
 * matters here: routing through a public CORS relay is what got an earlier
 * domain flagged by reputation scanners.
 *
 * Deploy:
 *   wrangler deploy
 * Then point the frontend's QUOTE_RELAY at the deployed URL.
 */

const UPSTREAM = 'https://cdn.cboe.com/api/global/delayed_quotes/quotes/';
const SYMBOLS = ['MSTR', 'STRC', 'ASST', 'SATA'];      // allowlist — do not make dynamic
const ALLOWED_ORIGINS = [
  'https://treasurytracker.net',
  'https://www.treasurytracker.net',
  'http://localhost:4173',                              // local dev
];
const EDGE_TTL = 10;      // seconds; Cboe itself serves s-maxage=5

function corsHeaders(origin) {
  const allow = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    'Access-Control-Allow-Origin': allow,
    'Vary': 'Origin',
    'Cache-Control': `public, max-age=${EDGE_TTL}`,
    'Content-Type': 'application/json; charset=utf-8',
  };
}

async function quote(sym, ctx) {
  const req = new Request(UPSTREAM + sym + '.json', { cf: { cacheTtl: EDGE_TTL, cacheEverything: true } });
  const r = await fetch(req);
  if (!r.ok) return null;
  const d = (await r.json()).data;
  if (!d) return null;
  return {
    // the official 4:00pm consolidated print; present and stable after the bell
    close: d.close ?? null,
    // 15-minute delayed last trade — moves during the session and after hours
    last: d.current_price ?? null,
    prevClose: d.prev_day_close ?? null,
    lastTradeTime: d.last_trade_time ?? null,   // ET, e.g. "2026-09-18T15:59:59"
    volume: d.volume ?? null,
  };
}

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get('Origin') || '';
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: { ...corsHeaders(origin), 'Access-Control-Allow-Methods': 'GET, OPTIONS' },
      });
    }
    if (request.method !== 'GET') {
      return new Response('method not allowed', { status: 405, headers: corsHeaders(origin) });
    }

    const out = {};
    const results = await Promise.all(SYMBOLS.map(s => quote(s, ctx).catch(() => null)));
    SYMBOLS.forEach((s, i) => { if (results[i]) out[s] = results[i]; });

    return new Response(JSON.stringify({
      source: 'cboe',
      delayedMinutes: 15,
      fetchedAt: new Date().toISOString(),
      quotes: out,
    }), { headers: corsHeaders(origin) });
  },
};
