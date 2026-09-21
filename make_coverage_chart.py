#!/usr/bin/env python3
"""APYX share card: USD Assets coverage (months) vs. the STRC market price.

Coverage = total USD assets / (annual preferred dividends + cash interest / 12),
using the balances Strategy discloses in its weekly 8-Ks and holding them flat
between disclosures.

"USD assets" is the USD Reserve plus USD Cash. USD Cash is a second pool created
by the 2026-08-24 Digital Credit Capital Framework update; before that date it
did not exist, so it backfills as zero and the line is the Reserve alone. That
makes one continuous definition — total dollars on hand — across the whole window.

Regenerate weekly:  python3 make_coverage_chart.py
Needs rsvg-convert (brew install librsvg); qlmanage mangles the aspect ratio.
"""
import base64, json, os, re, subprocess, sys

DATA = os.path.expanduser('~/crypto-treasury-dashboard/data.json')
LOGO = os.path.expanduser('~/crypto-treasury-dashboard/apyx-logo.svg')
OUT = os.path.expanduser('~/Downloads')
START = '2026-05-24'

# USD balances exactly as disclosed in the weekly 8-Ks ($mm), by disclosure date.
# Pull new rows from the dashboard's own actions log:
#   jq -r '.actions[]|select(.co=="MSTR")|.d+" "+(.items[]|select(contains("USD")))' data.json
RESERVE = [('2026-05-25', 871), ('2026-05-31', 900), ('2026-06-21', 1400), ('2026-06-28', 2550),
           ('2026-07-12', 3000), ('2026-07-19', 3225), ('2026-07-26', 3750), ('2026-08-02', 4000),
           ('2026-08-09', 4650), ('2026-08-16', 4800), ('2026-08-23', 5100), ('2026-08-30', 5100)]
USDCASH = [('2026-08-23', 1590), ('2026-08-30', 1610)]     # zero before the Aug 24 framework

W, H = 1200, 675
L, R, T, B = 104, 116, 156, 132
PW, PH = W - L - R, H - T - B
CMIN, CMAX, PMIN, PMAX = 0, 55, 70, 101
MON = {'05': 'May', '06': 'Jun', '07': 'Jul', '08': 'Aug', '09': 'Sep', '10': 'Oct',
       '11': 'Nov', '12': 'Dec', '01': 'Jan', '02': 'Feb', '03': 'Mar', '04': 'Apr'}


def step_at(steps, iso, default=0.0):
    v = default
    for d, x in steps:
        if d <= iso:
            v = x
        else:
            break
    return v


def build_rows():
    m = json.load(open(DATA))['companies']['MSTR']
    strc = sorted((d, v) for d, v in m['strcNotionalSteps'])
    fixed = {r[0]: r[2] for r in m['prefBreakdown'] if r[0] != 'STRC'}
    RATE = {'STRK': .08, 'STRD': .10, 'STRF': .10, 'STRE': .10}
    debt_int = sum(t['principal'] * t['coupon'] / 100 for t in m['debtSchedule'])

    def obligations(iso):
        return (step_at(strc, iso, strc[0][1]) * 0.12
                + sum(n * RATE.get(k, .10) for k, n in fixed.items()) + debt_int)

    ph, rows = m['prefHistory'], []
    for iso, px in zip(ph['iso'], ph['px']):
        if iso < START:
            continue
        res, cash = step_at(RESERVE, iso), step_at(USDCASH, iso)   # cash backfills to 0
        ob = obligations(iso)
        rows.append({'d': iso, 'px': px, 'usd': res + cash, 'res': res,
                     'cash': cash, 'ob': ob, 'cov': (res + cash) / (ob / 12)})
    return rows


def render(rows):
    last = rows[-1]
    x = lambda i: L + PW * i / (len(rows) - 1)
    yc = lambda v: T + PH * (1 - (v - CMIN) / (CMAX - CMIN))
    yp = lambda v: T + PH * (1 - (v - PMIN) / (PMAX - PMIN))
    line = lambda vals, ym: ''.join(
        f"{'M' if i == 0 else 'L'}{x(i):.1f},{ym(v):.1f}" for i, v in enumerate(vals))

    grid = ''.join(f'<line x1="{L}" y1="{yc(v):.1f}" x2="{L+PW}" y2="{yc(v):.1f}" class="grid"/>'
                   f'<text x="{L-16}" y="{yc(v)+5:.1f}" class="ax" text-anchor="end">{v}</text>'
                   for v in range(0, CMAX + 1, 10))
    rax = ''.join(f'<text x="{L+PW+16}" y="{yp(v)+5:.1f}" class="ax rd" text-anchor="start">${v}</text>'
                  for v in range(PMIN, PMAX, 10))
    xt = ''.join(f'<text x="{x(i):.1f}" y="{T+PH+32}" class="ax" text-anchor="middle">'
                 f'{MON[r["d"][5:7]]} {int(r["d"][8:])}</text>'
                 for i, r in enumerate(rows) if i % 10 == 0 or i == len(rows) - 1)
    # mark where USD Cash was introduced — the line steps there for a structural
    # reason, not a market one, and an unexplained jump invites the wrong read
    i0 = next((i for i, r in enumerate(rows) if r['cash']), None)
    mark = ''
    if i0 is not None:
        # sit the label low in the plot: the top-right corner belongs to the callout
        mark = (f'<line x1="{x(i0):.1f}" y1="{T+14}" x2="{x(i0):.1f}" y2="{T+PH}" '
                f'stroke="rgba(255,255,255,.22)" stroke-width="1" stroke-dasharray="3 4"/>'
                f'<text x="{x(i0)-10:.1f}" y="{yc(21):.1f}" class="note" text-anchor="end">USD CASH ADDED</text>')

    # End-of-line callouts. Both series happen to finish at nearly the same height
    # (coverage 47.3 sits where STRC $97.78 maps to ~49.3), so anchoring each to its
    # own line stacks them on top of each other. Park them at fixed, well-separated
    # heights instead, each on a dark plate with a leader dot so it still reads over
    # the lines behind it.
    def callout(head, sub, y_units, colour, size, dot_y, xr=None):
        """Right-aligned callout: headline, with the $ figure indented onto a second
        line beneath it. Sited in open space rather than masked with a plate — an
        opaque plate cut a visible break through the white line's Aug 24 step.
        No leader: a dot on the line end plus matching colour pairs them."""
        xr, y = (L + PW - 4) if xr is None else xr, yc(y_units)
        out = (f'<circle cx="{L+PW-1:.1f}" cy="{dot_y:.1f}" r="4.5" fill="{colour}"/>'
               f'<text x="{xr:.1f}" y="{y:.1f}" fill="{colour}" text-anchor="end" '
               f'style="font-size:{size}px;font-weight:800">{head}</text>')
        if sub:
            out += (f'<text x="{xr:.1f}" y="{y+size+3:.1f}" fill="{colour}" text-anchor="end" '
                    f'style="font-size:{size*0.8:.0f}px;font-weight:700" opacity=".8">{sub}</text>')
        return out

    # STRC on top, coverage beneath — matches the order the two lines actually
    # finish in, so the eye pairs each label with the nearer line
    # STRC clears both lines at the top right. The coverage label goes in the open
    # corridor just left of the Aug 24 step — between the pre-step line and the red
    # one — so it sits near its own line without anything needing to be masked.
    callouts = (callout(f'STRC ${last["px"]:.0f}', '', 53.5, '#ed1946', 20, yp(last['px']))
                + callout(f'{last["cov"]:.1f} mo', f'(${last["usd"]/1000:.2f}B)', 41.0,
                          '#ffffff', 21, yc(last['cov']),
                          xr=(x(i0) - 16) if i0 is not None else None))
    logo = base64.b64encode(open(LOGO, 'rb').read()).decode()
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">
<defs><style>
 text {{ font-family:"Google Sans Flex","Google Sans",Inter,system-ui,sans-serif; }}
 .grid {{ stroke:rgba(255,255,255,.075); stroke-width:1; }}
 .ax {{ fill:#8a8f96; font-size:15px; font-weight:600; }}
 .rd {{ fill:#ed1946; }}
 .ttl {{ fill:#fff; font-size:33px; font-weight:800; }}
 .eyebrow {{ fill:#6f757c; font-size:12.5px; font-weight:700; letter-spacing:2.3px; }}
 .lbl {{ fill:#c8ccd2; font-size:16px; font-weight:600; }}
 .foot {{ fill:#5d636a; font-size:11px; font-weight:600; letter-spacing:.9px; }}
 .cal {{ fill:#fff; font-size:21px; font-weight:800; }}
 .calr {{ fill:#ed1946; font-size:19px; font-weight:800; }}
 .minlbl {{ fill:#7d838a; font-size:11.5px; font-weight:700; letter-spacing:1.5px; }}
 .note {{ fill:#7d838a; font-size:10.5px; font-weight:700; letter-spacing:1.3px; }}
 .axttl {{ fill:#8a8f96; font-size:12.5px; font-weight:700; }}
</style></defs>
<rect width="{W}" height="{H}" fill="#0a0a0b"/>
<text x="{L-46}" y="54" class="ttl">USD Assets Coverage<tspan class="rd" dx="20">vs. STRC Price</tspan></text>
<text x="{L-46}" y="84" class="eyebrow">MONTHS OF DIVIDEND + INTEREST COVERAGE AGAINST THE STRC MARKET PRICE</text>
<image href="data:image/svg+xml;base64,{logo}" x="{W-238}" y="28" width="180" height="38"/>
{grid}{rax}{xt}{mark}
<line x1="{L}" y1="{yc(12):.1f}" x2="{L+PW}" y2="{yc(12):.1f}" stroke="#7d838a" stroke-width="1.4" stroke-dasharray="7 6"/>
<text x="{L+10}" y="{yc(12)-11:.1f}" class="minlbl">12-MONTH BOARD MINIMUM</text>
<path d="{line([r['px'] for r in rows], yp)}" fill="none" stroke="#ed1946" stroke-width="2.6"/>
<path d="{line([r['cov'] for r in rows], yc)}" fill="none" stroke="#fff" stroke-width="3.2"/>
{callouts}
<text transform="translate(30,{T+PH/2}) rotate(-90)" class="axttl" text-anchor="middle">Months of dividend + interest coverage</text>
<text transform="translate({W-32},{T+PH/2}) rotate(-90)" class="axttl rd" text-anchor="middle">STRC price ($)</text>
<g transform="translate({L-46},{H-78})">
  <line x1="0" y1="-5" x2="26" y2="-5" stroke="#fff" stroke-width="3.2"/>
  <text x="36" y="0" class="lbl">USD assets coverage — Reserve + USD Cash (months)</text>
  <line x1="470" y1="-5" x2="496" y2="-5" stroke="#ed1946" stroke-width="2.6"/>
  <text x="506" y="0" class="lbl">STRC price</text>
</g>
<line x1="{L-46}" y1="{H-58}" x2="{W-58}" y2="{H-58}" stroke="rgba(255,255,255,.10)"/>
<text x="{L-46}" y="{H-36}" class="foot">USD RESERVE + USD CASH FROM OFFICIAL STRATEGY 8-K DISCLOSURES, HELD FLAT BETWEEN DISCLOSURES</text>
<text x="{L-46}" y="{H-19}" class="foot">USD CASH CREATED BY THE AUG 24 FRAMEWORK UPDATE, NIL BEFORE IT &#183; ${last['usd']/1000:.2f}B AS OF AUG 30 ON ${last['ob']/1000:.2f}B ANNUAL OBLIGATIONS</text>
<text x="{W-58}" y="{H-19}" class="foot" text-anchor="end">APYX.FI</text>
</svg>'''


def embed_font(svg):
    """Inline the latin woff2 so the SVG rasterises standalone."""
    UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
    css = subprocess.run(['curl', '-s', '-A', UA, 'https://fonts.googleapis.com/css2?'
                          'family=Google+Sans+Flex:opsz,wght@6..144,300..800&display=swap'],
                         capture_output=True, text=True).stdout
    for name, body in re.findall(r"/\* (\S+) \*/\s*@font-face\s*\{(.*?)\}", css, re.S):
        if name == 'latin':
            url = re.search(r"url\((https://[^)]+\.woff2)\)", body).group(1)
            woff = subprocess.run(['curl', '-s', '-A', UA, url], capture_output=True).stdout
            face = ("@font-face{font-family:'Google Sans Flex';font-style:normal;"
                    "font-weight:300 800;src:url(data:font/woff2;base64,"
                    f"{base64.b64encode(woff).decode()}) format('woff2');}}")
            return svg.replace('<defs><style>', '<defs><style>' + face, 1)
    return svg


if __name__ == '__main__':
    rows = build_rows()
    last, stem = rows[-1], f'{OUT}/APYX_USD_Assets_Coverage_vs_STRC_{rows[-1]["d"]}'
    open(stem + '.svg', 'w').write(render(rows))
    open('/tmp/_cov_embed.svg', 'w').write(embed_font(render(rows)))
    if subprocess.run(['rsvg-convert', '-w', '2400', '-h', '1350', '-b', '#0a0a0b',
                       '/tmp/_cov_embed.svg', '-o', stem + '.png']).returncode:
        sys.exit('rsvg-convert failed — brew install librsvg')
    print(f'{len(rows)} pts  {rows[0]["d"]} -> {last["d"]}')
    print(f'  {last["cov"]:.1f} mo  (${last["usd"]:,}M = ${last["res"]:,}M reserve + '
          f'${last["cash"]:,}M cash) / ${last["ob"]:,.0f}M obligations   STRC ${last["px"]}')
    print('  ' + stem + '.png')
