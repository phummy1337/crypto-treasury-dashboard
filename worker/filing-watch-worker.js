/**
 * treasurytracker filing watch — Cloudflare Worker
 *
 * WHY THIS EXISTS
 * GitHub treats scheduled workflows as best-effort and silently sheds them under
 * load. Measured on this repo: the refresh cron asked for runs every Monday
 * morning — when Strategy and Strive both file their weekly 8-Ks — and GitHub
 * dropped every one of them on 2026-09-14 and again 2026-09-19..21. The site
 * then sits a filing behind until someone notices the staleness banner.
 *
 * Cloudflare's cron triggers actually fire. So the schedule lives here, and
 * GitHub Actions becomes a thing we *call* rather than something we hope runs.
 *
 * WHAT IT DOES
 * On each tick: ask EDGAR for the newest 8-K for each issuer, ask the live site
 * which filing it has ingested (the filingWatch watermark refresh_data.py
 * stamps), and fire workflow_dispatch only when EDGAR is ahead. No new filing,
 * no run — so this costs almost nothing and cannot spam Actions minutes.
 *
 * SETUP
 *   1. GitHub → fine-grained PAT, repo phummy1337/crypto-treasury-dashboard only,
 *      permission: Actions = Read and write. Nothing else.
 *   2. cd worker && wrangler secret put GH_TOKEN      (paste the PAT)
 *   3. wrangler deploy --config wrangler.filing-watch.toml
 *
 * Manual check (no dispatch):        curl https://<worker-url>/
 * Manual check and force a refresh:  curl https://<worker-url>/?dispatch=1
 */

const REPO = 'phummy1337/crypto-treasury-dashboard';
const WORKFLOW = 'refresh.yml';
const DATA_URL = 'https://treasurytracker.net/data.json';
const CIKS = { MSTR: '0001050446', ASST: '0001920406' };
// SEC's fair-access policy wants a descriptive UA with contact info
const SEC_UA = 'treasurytracker filing-watch pete@defidevcorp.com';

async function newestFiling(cik) {
  const r = await fetch(`https://data.sec.gov/submissions/CIK${cik}.json`, {
    headers: { 'User-Agent': SEC_UA, 'Accept': 'application/json' },
    cf: { cacheTtl: 60, cacheEverything: true },
  });
  if (!r.ok) return null;
  const f = (await r.json()).filings?.recent;
  if (!f) return null;
  let newest = null;
  for (let i = 0; i < f.form.length; i++) {
    if (f.form[i] === '8-K' && (!newest || f.filingDate[i] > newest)) newest = f.filingDate[i];
  }
  return newest;
}

async function ingested() {
  // filingWatch is stamped by refresh_data.py's record_filing_watermark()
  const r = await fetch(DATA_URL, { cache: 'no-store' });
  if (!r.ok) return {};
  const d = await r.json();
  const out = {};
  for (const tk of Object.keys(CIKS)) {
    out[tk] = d.companies?.[tk]?.filingWatch?.ingested || null;
  }
  return out;
}

async function dispatch(env) {
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.GH_TOKEN}`,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'treasurytracker-filing-watch',
    },
    body: JSON.stringify({ ref: 'main' }),
  });
  return { ok: r.ok, status: r.status, body: r.ok ? '' : await r.text() };
}

async function check(env, { force = false } = {}) {
  const [have, mstr, asst] = await Promise.all([
    ingested(), newestFiling(CIKS.MSTR), newestFiling(CIKS.ASST),
  ]);
  const edgar = { MSTR: mstr, ASST: asst };
  const behind = Object.keys(CIKS).filter(
    tk => edgar[tk] && have[tk] && edgar[tk] > have[tk]
  );
  const result = { checkedAt: new Date().toISOString(), edgar, ingested: have, behind, dispatched: false };
  if (behind.length || force) {
    const d = await dispatch(env);
    result.dispatched = d.ok;
    if (!d.ok) result.error = `${d.status} ${d.body}`.slice(0, 300);
  }
  return result;
}

export default {
  // Cron ticks — see wrangler.filing-watch.toml for the schedule
  async scheduled(event, env, ctx) {
    ctx.waitUntil(check(env).then(r => console.log(JSON.stringify(r))));
  },
  // Manual probe: GET reports status; ?dispatch=1 forces a run
  async fetch(request, env) {
    const force = new URL(request.url).searchParams.get('dispatch') === '1';
    const r = await check(env, { force });
    return new Response(JSON.stringify(r, null, 2), {
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  },
};
