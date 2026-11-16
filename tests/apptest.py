#!/usr/bin/env python3
"""End to end: the real HTTP server and pages, with a fake Telegram adapter and a fake WeChat store."""
import json, os, shutil, sys, tempfile, threading, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(dir=os.path.join(HERE, '..'))
os.environ['VAR'] = TMP; os.environ['WORK'] = TMP + '/work'; os.environ['WECHAT_DIR'] = TMP + '/none'
sys.path.insert(0, os.path.join(HERE, '..', 'content')); sys.path.insert(0, HERE)
import app, apikeys, tg  # noqa: E402
from tgfake import Fake  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

ok = True
def check(name, cond, extra=''):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name + (f'  [{extra}]' if not cond and extra else ''))
    ok = ok and bool(cond)

class K:  # stand-in for the WeChat key state
    state = 'ok'; capture_note = ''
    pending = staticmethod(lambda: False); capturer_alive = staticmethod(lambda: True)
class IKs: state = 'ok'

try:
    app.init_store(); app.APIKEYS = apikeys.ApiKeys(); app.IK = IKs
    fake = Fake(); app.TGS = tg.Service(TMP, app.db, app.log, adapter=fake)
    app.TGS.set_config(api_id='1', api_hash='h', proxy='socks5://127.0.0.1:1080'); open(TMP + '/tg.session', 'w').write('s')
    c = app.db()   # one WeChat chat
    c.execute("insert into chats(chat,name,last_local) values('wxid_a','小王',1)")
    c.execute("insert into msgs(chat,local_id,ts,type,sender,text) values('wxid_a',1,?,1,'小王','微信消息')", (int(time.time()),))
    c.commit(); c.close()
    key = app.APIKEYS.create('测试')['key']
    srv = ThreadingHTTPServer(('127.0.0.1', 0), app.make_handler(K)); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    B = f'http://127.0.0.1:{port}'

    def call(path, method='GET', body=None, auth=True, raw=False):
        h = {'Authorization': 'Bearer ' + key} if auth else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode(); h['Content-Type'] = 'application/json'
        req = urllib.request.Request(B + path, data=data, headers=h, method=method)
        try:
            r = urllib.request.urlopen(req); code, b = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, b = e.code, e.read()
        if raw: return code, b
        try: return code, json.loads(b)
        except Exception: return code, b.decode()

    def form(path, **kv):
        import urllib.parse
        req = urllib.request.Request(B + path, data=urllib.parse.urlencode(kv).encode(), method='POST')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        try: r = urllib.request.build_opener(NoRedirect).open(req); return r.status, r.read().decode()
        except urllib.error.HTTPError as e: return e.code, e.read().decode()

    # ---- help / auth
    for w in ('wechat', 'telegram', 'all'):
        code, t = call(f'/api/help?for={w}', auth=False)
        check(f'help for={w} needs no key', code == 200 and '安全约定' in t)
    check('telegram help mentions send/approval', '/api/send' in call('/api/help?for=telegram', auth=False)[1])
    check('wechat help does not mention Telegram', 'Telegram' not in call('/api/help?for=wechat', auth=False)[1])
    check('api needs a key', call('/api/chats', auth=False)[0] == 401)

    # ---- chats
    code, wx = call('/api/chats?source=wechat'); check('source=wechat lists only WeChat', code == 200 and [x['chat'] for x in wx] == ['wxid_a'] and wx[0]['source'] == 'wechat')
    code, tgc = call('/api/chats?source=telegram')
    check('source=telegram lists all Telegram dialogs (even unread ones)', code == 200 and [x['chat'] for x in tgc] == ['tg:g9', 'tg:u7', 'tg:me'] and all(x['source'] == 'telegram' for x in tgc))
    code, both = call('/api/chats'); check('no source: both platforms', {x['source'] for x in both} == {'wechat', 'telegram'})
    code, u = call('/api/chats?source=telegram&q=@XiaoMing'); check('search by @username (any case, with or without @)', [x['chat'] for x in u] == ['tg:u7'] and u[0]['username'] == 'xiaoming')
    code, u = call('/api/chats?source=telegram&q=nobody'); check('no match returns an empty list', u == [])
    code, g = call('/api/chats?source=telegram&kind=group'); check('kind=group works for Telegram', [x['chat'] for x in g] == ['tg:g9'])
    code, g = call('/api/chats?source=telegram&kind=private'); check('kind=private works for Telegram', [x['chat'] for x in g] == ['tg:u7'])

    # ---- messages: live read + placeholders + media status
    code, ms = call('/api/messages?chat=tg:u7')
    check('Telegram chat is read on demand', code == 200 and len(ms) == 5 and all(m['source'] == 'telegram' for m in ms))
    check('messages come oldest first', [m['ts'] for m in ms] == sorted(m['ts'] for m in ms))
    img = next(m for m in ms if m.get('media') and m['media']['kind'] == 'image')
    check('media starts as not_downloaded', img['media']['status'] == 'not_downloaded' and img['text'].endswith('[图片]'))
    code, raw = call(f"/api/media/{img['id']}", raw=True)
    check('requesting media downloads it', code == 200 and raw == b'x' * 10)
    code, ms = call('/api/messages?chat=tg:u7&live=0')
    check('status becomes ready after download', next(m for m in ms if m['id'] == img['id'])['media']['status'] == 'ready')
    code, w = call('/api/messages?chat=wxid_a'); check('WeChat messages unaffected, have source', code == 200 and w[0]['source'] == 'wechat' and w[0]['text'] == '微信消息')
    code, s = call('/api/search?q=msg2&source=telegram'); check('search reaches cached Telegram messages', code == 200 and len(s) == 1)

    # ---- errors are structured
    fake.flood = 30; app.TGS.flood_until = 0
    code, e = call('/api/messages?chat=tg:u7&days=1&limit=5')   # cached fresh -> no call
    app.TGS.set_config(); os.system('true')
    import tg as _t
    kv = app.db(); kv.execute("delete from kv where k like 'tg:fetch:%'"); kv.commit(); kv.close()
    code, e = call('/api/messages?chat=tg:u7')
    check('rate limit -> 429 with retry_after', code == 429 and e['error'] == 'rate_limited' and e['retry_after'] == 30, str((code, e)))
    code, ms = call('/api/messages?chat=tg:u7&live=0'); check('live=0 still returns the cache while limited', code == 200 and len(ms) == 5)
    fake.flood = 0; app.TGS.flood_until = 0

    # ---- send
    code, r = call('/api/send', 'POST', {'chat': 'tg:me', 'text': '提醒：明天交材料'})
    check('Saved Messages: sent immediately', code == 200 and r['status'] == 'sent' and fake.sent[-1][1] == '提醒：明天交材料')
    n = len(fake.sent)
    code, r = call('/api/send', 'POST', {'chat': 'tg:u7', 'text': '好的，今晚交'})
    check('other chat: held as a draft, NOT sent', code == 200 and r['status'] == 'pending_approval' and len(fake.sent) == n and r['draft_id'])
    code, r = call('/api/send', 'POST', {'chat': 'tg:u404', 'text': 'x'}); check('unknown chat refused', code == 404 and r['error'] == 'unknown_chat')
    code, r = call('/api/send', 'POST', {'chat': 'wxid_a', 'text': 'x'}); check('WeChat chat refused with a pointer to drafts', code == 400 and r['error'] == 'wechat_send_unsupported')
    code, r = call('/api/send', 'POST', {'chat': 'tg:me', 'text': 'x'}, auth=False); check('send needs a key', code == 401)
    did = call('/api/drafts')[1][0]['id']

    # ---- pages
    code, h = call('/', raw=True); h = h.decode()
    check('home is the WeChat tab with both tabs and settings link', code == 200 and '小王' in h and 'href="/tg"' in h and 'href="/settings"' in h and '项目群' not in h)
    code, h = call('/tg', raw=True); h = h.decode()
    check('Telegram tab lists dialogs and the draft with a send button', code == 200 and '项目群' in h and '/tg/send' in h and '好的，今晚交' in h)
    check('tab does not show WeChat chats', '小王' not in h)
    code, h = call(f'/chat?c=tg:u7', raw=True); h = h.decode()
    check('Telegram chat page works; ready media shown, others offer download', code == 200 and '看这张' in h and '/media/' in h)
    code, h = call('/settings', raw=True); h = h.decode()
    check('settings has WeChat key + Telegram sections', '密钥' in h and 'Telegram' in h and '连接设置' in h)
    check('settings never echoes api_hash or session', 'api_hash' in h and '"h"' not in h and 'value="h"' not in h)
    code, h = call('/keys', raw=True); h = h.decode()
    check('keys page: three agent texts per key', h.count('data-w=') == 3 and 'data-w="telegram"' in h and 'data-w="wechat"' in h)

    # ---- approval flow: click send on the draft
    code, _ = form('/tg/send', id=str(did))
    check('approving sends the draft', code == 303 and fake.sent[-1][1] == '好的，今晚交' and len(fake.sent) == n + 1)
    check('draft is cleared after sending', not [d for d in call('/api/drafts')[1] if d['id'] == did])
    check('sent log lists both', [x['via'] for x in app.TGS.sent_log()] == ['approved', 'direct'])
    code, _ = form('/tg/send', id=str(did)); check('approving twice does not send twice', len(fake.sent) == n + 1)

    # ---- refresh button + auto refresh
    import urllib.parse as up
    def post_form(path, **kv):
        return form(path, **kv)
    h = call('/', raw=True)[1].decode()
    check('WeChat tab has a refresh button and the auto-refresh script', 'action="/refresh"' in h and "/ping?'" in h and 'tab=wx' in h)
    h = call('/tg', raw=True)[1].decode()
    check('Telegram tab has a refresh button and the auto-refresh script', 'action="/refresh"' in h and 'tab=tg' in h)
    h = call('/chat?c=tg:u7', raw=True)[1].decode()
    check('chat page has a refresh button that returns to the same chat', 'name="to" value="/chat?c=tg%3Au7"' in h and 'tab=chat' in h)
    h = call('/settings', raw=True)[1].decode()
    check('settings page does not auto-refresh (would wipe what you type)', '/ping?' not in h)

    n0 = fake.calls.count('dialogs')
    code, _ = post_form('/refresh', to='/tg'); check('refresh on the Telegram tab re-reads the dialog list now', code == 303 and fake.calls.count('dialogs') == n0 + 1)
    kv = app.db(); kv.execute("insert or replace into kv values('tg:fetch:tg:u7', ?)", (str(time.time()),)); kv.commit(); kv.close()
    m0 = sum(1 for x in fake.calls if isinstance(x, tuple) and x[0] == 'messages')
    code, r = post_form('/refresh', to='/chat?c=tg%3Au7'); check('refresh in a Telegram chat forces a re-read even inside the 60 s cache', code == 303 and sum(1 for x in fake.calls if isinstance(x, tuple) and x[0] == 'messages') == m0 + 1)
    app.INGEST_NOW.clear(); t0 = time.time()
    code, _ = post_form('/refresh', to='/'); check('refresh on the WeChat tab wakes the reader', code == 303 and app.INGEST_NOW.is_set())
    for bad in ('//evil.example/x', 'https://evil.example', '/\\evil.example'):
        import urllib.request as ur
        class NR(ur.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        try: ur.build_opener(NR).open(ur.Request(B + '/refresh', data=up.urlencode({'to': bad}).encode(), method='POST'))
        except ur.HTTPError as e: check(f'refresh will not redirect off-site ({bad[:18]})', e.headers.get('Location') == '/')
    check('safe_back keeps same-site paths', app.safe_back('/chat?c=a') == '/chat?c=a' and app.safe_back('') == '/')

    v1 = call('/ping?tab=wx', raw=True)[1].decode()
    c = app.db(); c.execute("insert into msgs(chat,local_id,ts,type,sender,text) values('wxid_a',2,?,1,'小王','又一条')", (int(time.time()),)); c.commit(); c.close()
    v2 = call('/ping?tab=wx', raw=True)[1].decode()
    check('ping token changes when a WeChat message arrives', v1 != v2)
    check('ping token is stable when nothing changed', v2 == call('/ping?tab=wx', raw=True)[1].decode())
    v1 = call('/ping?tab=chat&c=tg%3Au7', raw=True)[1].decode()
    fake.msgs[('u', 7)].insert(0, dict(id=7, ts=int(time.time()), sender='小明', text='刚到的新消息', media=None, name=''))
    kv = app.db(); kv.execute("delete from kv where k like 'tg:fetch:%'"); kv.commit(); kv.close()
    v2 = call('/ping?tab=chat&c=tg%3Au7', raw=True)[1].decode()
    check('ping in a Telegram chat reads Telegram (once per minute) and the token changes', v1 != v2 and '刚到的新消息' in json.dumps(call('/api/messages?chat=tg:u7&live=0')[1], ensure_ascii=False))
    m0 = sum(1 for x in fake.calls if isinstance(x, tuple) and x[0] == 'messages')
    call('/ping?tab=chat&c=tg%3Au7', raw=True); call('/ping?tab=chat&c=tg%3Au7', raw=True)
    check('repeated pings do not hammer Telegram', sum(1 for x in fake.calls if isinstance(x, tuple) and x[0] == 'messages') == m0)
    fake.flood = 30; app.TGS.flood_until = 0
    kv = app.db(); kv.execute("delete from kv where k like 'tg:fetch:%'"); kv.commit(); kv.close()
    check('ping survives Telegram being rate limited', call('/ping?tab=chat&c=tg%3Au7', raw=True)[0] == 200)
    fake.flood = 0; app.TGS.flood_until = 0

    # ---- login state shown on settings
    fake.st = 'logged_out'; os.remove(TMP + '/tg.session')
    code, h = call('/settings', raw=True); check('logged out: offers QR login', '用二维码登录' in h.decode())
    code, st = call('/api/status'); check('status reports telegram', 'telegram' in st and st['telegram']['configured'])
    code, h = call('/tg', raw=True); check('not logged in: Telegram tab points to settings', '去设置' in h.decode())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print('ALL PASS' if ok else 'FAILURES')
sys.exit(0 if ok else 1)
