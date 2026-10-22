"""Rebrickable compliance harness for lego-to-bricklink-xml/index.html.

Runs the real page in headless Chromium against a mocked Rebrickable and
records the timestamp of every request that reaches "Rebrickable", across
all tabs. Each scenario asserts the request pattern stays within the
documented limit (~1 request/sec per account) and that 429s are honoured.
"""
import json, re, sys, time, random
from playwright.sync_api import sync_playwright

HTML = open(sys.argv[1] if len(sys.argv) > 1 else __import__('os').path.join(__import__('os').path.dirname(__file__), '..', 'index.html')).read()
MIN_GAP = 1.0           # seconds: 1 req/sec average => consecutive requests >= 1s apart
LATENCY = (0.05, 0.25)  # simulated Rebrickable response time

log = []                # (t, method, element_id, tab)
state = {}
results = []

def reset(**kw):
    log.clear(); state.clear()
    state.update(dict(throttle=set(), transient={}, wait=2, abort=set(), status={}, seen={}))
    state.update(kw)

def handler(tab):
    def handle(route):
        req = route.request; url = req.url
        if 'rebrickable.com' not in url:
            return route.fulfill(status=200, body='')
        m = re.search(r'elements/([^/]+)/', url); eid = m.group(1) if m else '?'
        entry = [time.time(), req.method, eid, tab, url, None]; log.append(entry)
        state['seen'][eid] = state['seen'].get(eid, 0) + 1
        time.sleep(random.uniform(*LATENCY))
        h = {'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json'}
        if eid in state['abort']:
            entry[5] = 'abort'; return route.abort('connectionrefused')
        if eid in state['status']:
            entry[5] = state['status'][eid]
            return route.fulfill(status=state['status'][eid], headers=h, body='{}')
        t = state['transient'].get(eid, 0)
        if eid in state['throttle'] or state['seen'][eid] <= t:
            entry[5] = 429; entry.append(state['wait'])
            return route.fulfill(status=429, headers=h, body=json.dumps(
                {'detail': 'Request was throttled. Expected available in %d seconds.' % state['wait']}))
        return route.fulfill(status=200, headers=h, body=json.dumps(
            {'part': {'external_ids': {'BrickLink': ['p' + eid]}},
             'color': {'external_ids': {'BrickLink': {'ext_ids': [5]}}}}))
    return handle

def gaps(entries=None):
    ts = [e[0] for e in (log if entries is None else entries) if e[1] == 'GET']
    return [ts[i+1] - ts[i] for i in range(len(ts) - 1)]

def max_in_window(w):
    ts = sorted(e[0] for e in log if e[1] == 'GET'); best = 0; j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] >= w: j += 1
        best = max(best, i - j + 1)
    return best

def check(scn, name, cond, detail=''):
    results.append((scn, name, bool(cond), detail))
    print(('  PASS ' if cond else '  FAIL ') + name + (('  [' + detail + ']') if detail else ''))

def rate_checks(scn):
    g = gaps()
    mg = min(g) if g else None
    check(scn, 'min gap between requests >= %.1fs' % MIN_GAP, mg is None or mg >= MIN_GAP,
          'min gap %s, %d requests' % ('n/a' if mg is None else '%.2fs' % mg, len([e for e in log if e[1]=='GET'])))
    check(scn, 'never more than 1 request in any 1s window', max_in_window(1.0) <= 1, 'max %d' % max_in_window(1.0))
    check(scn, 'no CORS preflight (OPTIONS) requests', not any(e[1] != 'GET' for e in log))
    bad = []
    for e in log:
        if e[5] == 429:
            inside = [x for x in log if e[0] < x[0] < e[0] + e[6]]
            if inside: bad.append('%s/%s %d within %ds' % (e[3], e[2], len(inside), e[6]))
    if any(e[5] == 429 for e in log):
        check(scn, 'every 429 honoured: no request from ANY tab within the stated wait', not bad, '; '.join(bad[:3]))

def load(pg):
    pg.goto('https://bb-converter.test/')
    pg.set_content(HTML)

def status(pg): return pg.text_content('#statusLine') or ''

def wait_status(pg, prefixes, timeout=180000):
    js = "(p) => p.some(x => (document.getElementById('statusLine').textContent||'').startsWith(x))"
    pg.wait_for_function(js, arg=prefixes, timeout=timeout)

def convert(pg, text, key='k'):
    pg.fill('#apiKey', key); pg.fill('#idsInput', text); pg.click('#convertBtn')

def fake_time(pg, offset_ms):
    # Shift this page's clock so cooldowns can be expired without waiting.
    pg.evaluate("""(o) => { if (!window.__realNow) { window.__realNow = Date.now.bind(Date); }
                            Date.now = () => window.__realNow() + o; }""", offset_ms)

def main():
    with sync_playwright() as p:
        b = p.chromium.launch()

        def fresh_ctx(tabs=1):
            ctx = b.new_context(); pages = []
            for t in range(tabs):
                pg = ctx.new_page(); pg.route('**/*', handler('tab%d' % (t+1)))
                errs = []; pg.on('pageerror', lambda e, errs=errs: errs.append(str(e))); pg._errs = errs
                pages.append(pg)
            return ctx, pages

        # S1: long list with duplicates and junk lines
        print('\nS1 long list (40 unique IDs, duplicates, junk)')
        reset(); ctx, (pg,) = fresh_ctx(); load(pg)
        ids = [str(6000000 + i) for i in range(40)]
        lines = [i + ' x2' for i in ids] + [ids[0] + ' x1', ids[5] + '*3', 'not an id', '1 2 3']
        random.seed(1); random.shuffle(lines)
        t0 = time.time(); convert(pg, '\n'.join(lines)); wait_status(pg, ['Done', 'Stopped'])
        rate_checks('S1')
        check('S1', 'one request per unique ID (duplicates cached)', len(log) == 40, '%d requests' % len(log))
        check('S1', 'API key sent as query param on the elements endpoint only',
              all(re.match(r'https://rebrickable\.com/api/v3/lego/elements/\d+/\?key=k$', e[4]) for e in log))
        print('    took %.0fs for 40 lookups' % (time.time() - t0)); ctx.close()

        # S2: transient 429 (2 throttles then success) - waits must honour the stated time
        print('\nS2 transient 429s mid-run (Rebrickable says wait 2s)')
        reset(transient={'6100003': 2}, wait=2); ctx, (pg,) = fresh_ctx(); load(pg)
        convert(pg, '\n'.join(str(6100000 + i) for i in range(6))); wait_status(pg, ['Done', 'Stopped'])
        rate_checks('S2')
        idx = [i for i, e in enumerate(log) if e[2] == '6100003']
        after = [log[idx[k]+1][0] - log[idx[k]][0] for k in range(len(idx)-1)]
        check('S2', 'retry after a 429 waits at least the stated 2s', all(a >= 2.0 for a in after), ', '.join('%.1fs' % a for a in after))
        check('S2', 'run completes once throttle clears', status(pg).startswith('Done'), status(pg)); ctx.close()

        # S3: persistent 429 - must stop, then send nothing during cooldown however hard the user tries
        print('\nS3 persistent 429 -> stop + cooldown, user hammering buttons')
        reset(throttle={'6200002'}, wait=2); ctx, (pg,) = fresh_ctx(); load(pg)
        convert(pg, '\n'.join(str(6200000 + i) for i in range(10))); wait_status(pg, ['Stopped', 'Done'])
        rate_checks('S3')
        first429 = next(i for i, e in enumerate(log) if e[2] == '6200002')
        check('S3', 'nothing sent after the throttled ID gives up', len(log) - first429 == 3, '%d requests from first 429' % (len(log) - first429))
        n = len(log)
        for _ in range(5):  # determined user: force-enable and click everything
            pg.evaluate("document.querySelectorAll('button').forEach(b => b.disabled = false)")
            pg.click('#convertBtn')
            if pg.locator('[data-retry=all]').count(): pg.click('[data-retry=all]')
            pg.wait_for_timeout(200)
        check('S3', 'Convert/Retry during cooldown send no requests', len(log) == n, '%d extra' % (len(log) - n))
        load(pg); pg.fill('#apiKey', 'k'); pg.fill('#idsInput', '6299999');
        pg.evaluate("document.getElementById('convertBtn').disabled = false"); pg.click('#convertBtn'); pg.wait_for_timeout(500)
        check('S3', 'page refresh does not bypass cooldown', len(log) == n, '%d extra' % (len(log) - n)); ctx.close()

        # S4: Rebrickable unreachable (connection refused, e.g. IP blocked) - must stop
        print('\nS4 connection refused on every request')
        reset(abort={str(6300000 + i) for i in range(10)}); ctx, (pg,) = fresh_ctx(); load(pg)
        convert(pg, '\n'.join(str(6300000 + i) for i in range(10))); wait_status(pg, ['Stopped', 'Done'])
        rate_checks('S4')
        check('S4', 'stops after 3 attempts on the first ID', len(log) == 3, '%d requests' % len(log)); ctx.close()

        # S5: long wait demanded (looks like a ban) - one request then stop
        print('\nS5 Rebrickable asks for a 1-hour wait')
        reset(throttle={str(6400000 + i) for i in range(5)}, wait=3600); ctx, (pg,) = fresh_ctx(); load(pg)
        convert(pg, '\n'.join(str(6400000 + i) for i in range(5))); wait_status(pg, ['Stopped', 'Done'])
        check('S5', 'exactly one request then stop', len(log) == 1, '%d requests' % len(log))
        check('S5', 'locked for the full hour', '60 min' in status(pg) or '1 h' in status(pg), status(pg)); ctx.close()

        # S6: 401/403 - stop at once
        print('\nS6 API key rejected / blocked (403)')
        reset(status={str(6500000 + i): 403 for i in range(5)}); ctx, (pg,) = fresh_ctx(); load(pg)
        convert(pg, '\n'.join(str(6500000 + i) for i in range(5))); wait_status(pg, ['API key', 'Done', 'Stopped'])
        check('S6', 'exactly one request then stop', len(log) == 1, '%d requests' % len(log)); ctx.close()

        # S7: user clicking single-row Retry buttons as fast as they re-enable, then Convert straight after
        print('\nS7 rapid single-row Retry clicks, then Convert immediately')
        reset(throttle={'6600001'}, wait=1); ctx, (pg,) = fresh_ctx(); load(pg)
        convert(pg, '\n'.join(str(6600000 + i) for i in range(8))); wait_status(pg, ['Stopped', 'Done'])
        fake_time(pg, 10 * 60 * 1000); state['throttle'] = set(); log.clear()
        pg.evaluate("document.querySelectorAll('button').forEach(b => b.disabled = false)")
        clicks = 0
        while pg.locator('.btn-retry').count() and clicks < 10:
            pg.locator('.btn-retry:not([disabled])').first.click(); clicks += 1
            pg.wait_for_function("!document.getElementById('convertBtn').disabled || document.querySelectorAll('.btn-retry:not([disabled])').length", timeout=30000)
            pg.wait_for_function("document.querySelectorAll('.btn-retry').length === 0 || document.querySelectorAll('.btn-retry:not([disabled])').length > 0", timeout=30000)
        pg.fill('#idsInput', '6699999'); pg.click('#convertBtn'); wait_status(pg, ['Done', 'Stopped'])
        rate_checks('S7'); ctx.close()

        # S8: two tabs (same browser) converting at the same time
        print('\nS8 two tabs converting at the same time')
        reset(); ctx, (a, bb) = fresh_ctx(2); load(a); load(bb)
        a.fill('#apiKey', 'k'); a.fill('#idsInput', '\n'.join(str(6700000 + i) for i in range(8)))
        bb.fill('#apiKey', 'k'); bb.fill('#idsInput', '\n'.join(str(6710000 + i) for i in range(8)))
        a.click('#convertBtn'); bb.click('#convertBtn')
        wait_status(a, ['Done', 'Stopped']); wait_status(bb, ['Done', 'Stopped'])
        rate_checks('S8'); ctx.close()

        # S9: tab A gets throttled -> tab B (mid-run) must stop too
        print('\nS9 tab A throttled while tab B is mid-run')
        reset(throttle={'6800001'}, wait=1); ctx, (a, bb) = fresh_ctx(2); load(a); load(bb)
        a.fill('#apiKey', 'k'); a.fill('#idsInput', '\n'.join(str(6800000 + i) for i in range(4)))
        bb.fill('#apiKey', 'k'); bb.fill('#idsInput', '\n'.join(str(6810000 + i) for i in range(30)))
        bb.click('#convertBtn'); a.click('#convertBtn')
        wait_status(a, ['Stopped', 'Done']); t_stop = time.time()
        wait_status(bb, ['Done', 'Stopped'], timeout=240000)
        after = [e for e in log if e[3] == 'tab2' and e[0] > t_stop + 1.5]
        check('S9', 'tab B sends nothing once tab A is locked', not after, '%d tab B requests after lock' % len(after))
        rate_checks('S9'); ctx.close()

        # S10: tab A gets a transient 429 -> tab B must pause for the stated wait too
        print('\nS10 transient 429 in tab A pauses tab B')
        reset(transient={'6900002': 1}, wait=3); ctx, (a, bb) = fresh_ctx(2); load(a); load(bb)
        a.fill('#apiKey', 'k'); a.fill('#idsInput', '\n'.join(str(6900000 + i) for i in range(5)))
        bb.fill('#apiKey', 'k'); bb.fill('#idsInput', '\n'.join(str(6910000 + i) for i in range(8)))
        a.click('#convertBtn'); bb.click('#convertBtn')
        wait_status(a, ['Done', 'Stopped']); wait_status(bb, ['Done', 'Stopped'])
        rate_checks('S10')
        check('S10', 'both tabs still finish', status(a).startswith('Done') and status(bb).startswith('Done'), status(a) + ' | ' + status(bb)); ctx.close()
        b.close()

    fails = [r for r in results if not r[2]]
    print('\n%d checks, %d failed' % (len(results), len(fails)))
    for f in fails: print('  FAIL %s: %s [%s]' % f[:1] + f[1:2] + f[3:4] if False else '  FAIL %s: %s [%s]' % (f[0], f[1], f[3]))
    sys.exit(1 if fails else 0)

main()
