#!/usr/bin/env python3
"""End to end: the real HTTP server and pages, with a fake Telegram adapter and a fake WeChat store."""
import json, os, shutil, sys, tempfile, threading, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(dir=os.path.join(HERE, '..'))
os.environ['VAR'] = TMP; os.environ['WORK'] = TMP + '/work'; os.environ['WECHAT_DIR'] = TMP + '/none'
sys.path.insert(0, os.path.join(HERE, '..', 'content')); sys.path.insert(0, HERE)
import app, ctx, pages, wechat, apikeys, tg  # noqa: E402
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
    ctx.init_store(); ctx.APIKEYS = apikeys.ApiKeys(); ctx.IK = IKs
    fake = Fake(); ctx.TGS = tg.Service(TMP, ctx.db, ctx.log, adapter=fake)
    ctx.TGS.set_config(api_id='1', api_hash='h', proxy='socks5://127.0.0.1:1080'); open(TMP + '/tg.session', 'w').write('s')
    c = ctx.db()   # one WeChat chat
    c.execute("insert into chats(chat,name,last_local) values('wxid_a','小王',1)")
    c.execute("insert into msgs(chat,local_id,ts,type,sender,text) values('wxid_a',1,?,1,'小王','微信消息')", (int(time.time()),))
    c.commit(); c.close()
    key = ctx.APIKEYS.create('测试')['key']
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

    def settle(timeout=8):
        """Wait until background Telegram reads (chat sync, dialog list) are done."""
        end = time.time() + timeout
        while time.time() < end:
            if not pages.TG_BG['running'] and not any(x.get('running') for x in pages.SYNC.values()):
                return True
            time.sleep(0.05)
        return False

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
    fake.flood = 30; ctx.TGS.flood_until = 0
    code, e = call('/api/messages?chat=tg:u7&days=1&limit=5')   # cached fresh -> no call
    import tg as _t
    kv = ctx.db(); kv.execute("delete from kv where k like 'tg:fetch:%'"); kv.commit(); kv.close()
    code, e = call('/api/messages?chat=tg:u7')
    check('rate limit -> 429 with retry_after', code == 429 and e['error'] == 'rate_limited' and e['retry_after'] == 30, str((code, e)))
    code, ms = call('/api/messages?chat=tg:u7&live=0'); check('live=0 still returns the cache while limited', code == 200 and len(ms) == 5)
    fake.flood = 0; ctx.TGS.flood_until = 0

    # ---- send
    code, r = call('/api/send', 'POST', {'chat': 'tg:me', 'text': '提醒：明天交材料'})
    check('Saved Messages: sent immediately', code == 200 and r['status'] == 'sent' and fake.sent[-1][1] == '提醒：明天交材料')
    n = len(fake.sent)
    code, r = call('/api/send', 'POST', {'chat': 'tg:u7', 'text': '好的，今晚交'})
    check('other chat: held as a draft, NOT sent', code == 200 and r['status'] == 'pending_approval' and len(fake.sent) == n and r['draft_id'])
    code, r = call('/api/send', 'POST', {'chat': 'tg:u404', 'text': 'x'}); check('unknown chat refused', code == 404 and r['error'] == 'unknown_chat')
    code, r = call('/api/send', 'POST', {'chat': 'wxid_a', 'text': 'x'}); check('WeChat chat refused with a pointer to drafts', code == 400 and r['error'] == 'wechat_send_unsupported')
    for empty in ('', '   ', None):
        code, r = call('/api/send', 'POST', {'chat': empty, 'text': 'x'})
        check(f'empty chat ({empty!r}) -> 400 missing_chat, not "WeChat is read-only"', code == 400 and r['error'] == 'missing_chat', str(r))
    code, r = call('/api/send', 'POST', {'text': 'x'}); check('no chat field at all -> missing_chat', code == 400 and r['error'] == 'missing_chat')
    code, r = call('/api/send', 'POST', {'chat': 'no_such_chat', 'text': 'x'}); check('a chat id that does not exist -> 404 unknown_chat', code == 404 and r['error'] == 'unknown_chat')
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
    check('settings has the allowlist section with the chats to pick from', '直接发送白名单' in h and 'action="/tg/allow"' in h and 'value="tg:u7"' in h)
    check('settings has the built-in proxy section, off by default', '自带代理' in h and '未启用' in h and 'action="/proxy/config"' in h)
    check('settings never echoes api_hash or session', 'api_hash' in h and '"h"' not in h and 'value="h"' not in h)
    code, h = call('/keys', raw=True); h = h.decode()
    check('keys page: three agent texts per key', h.count('data-w=') == 3 and 'data-w="telegram"' in h and 'data-w="wechat"' in h)

    # ---- approval flow: click send on the draft
    code, _ = form('/tg/send', id=str(did))
    check('approving sends the draft', code == 303 and fake.sent[-1][1] == '好的，今晚交' and len(fake.sent) == n + 1)
    check('draft is cleared after sending', not [d for d in call('/api/drafts')[1] if d['id'] == did])
    check('sent log lists both', [x['via'] for x in ctx.TGS.sent_log()] == ['approved', 'direct'])
    code, _ = form('/tg/send', id=str(did)); check('approving twice does not send twice', len(fake.sent) == n + 1)

    # ---- refresh button + auto refresh
    # ---- allowlist: chats agents may send to without approval
    ctx.TGS.set_allowed(['tg:u7', 'tg:me', 'nonsense'])
    check('allowlist keeps only Telegram chats other than Saved Messages', ctx.TGS.allowed() == ['tg:u7'])
    n2 = len(fake.sent)
    code, r = call('/api/send', 'POST', {'chat': 'tg:u7', 'text': '白名单直发'})
    check('allowlisted chat: sent immediately, no draft', code == 200 and r['status'] == 'sent' and len(fake.sent) == n2 + 1 and fake.sent[-1][1] == '白名单直发', str(r))
    ctx.TGS.set_allowed([])
    code, r = call('/api/send', 'POST', {'chat': 'tg:u7', 'text': '移出后'})
    check('removed from the allowlist: back to a draft', code == 200 and r['status'] == 'pending_approval' and len(fake.sent) == n2 + 1)
    import api as _api; _api.draft_dismiss(r['draft_id'])
    code, _ = form('/tg/allow', add='tg:u7'); check('settings page adds a chat to the allowlist', code == 303 and ctx.TGS.allowed() == ['tg:u7'])
    code, _ = form('/tg/allow', add='tg:u93372553'); check('BotFather can never be allowlisted', ctx.TGS.allowed() == ['tg:u7'])
    code, _ = form('/tg/allow', **{'del': 'tg:u7'}); check('settings page removes it', code == 303 and ctx.TGS.allowed() == [])
    # ---- send_file from the user account: Saved Messages and the allowlist only
    import base64 as b64
    def sf(chat, name, data=b'IMG', text=''):
        return call('/api/send_file', 'POST', {'chat': chat, 'name': name, 'data': b64.b64encode(data).decode(), 'text': text})
    code, r = sf('tg:me', 'chart.png', text='图表')
    check('file to Saved Messages: sent as a photo with the caption', code == 200 and r['kind'] == 'photo' and fake.files[-1][0] == ('me', 0, None) and fake.files[-1][3] == '图表', str(r))
    code, r = sf('tg:me', 'report.pdf'); check('other files go as documents', code == 200 and r['kind'] == 'file')
    code, r = sf('tg:u7', 'a.png'); nf = len(fake.files)
    check('not allowlisted: file refused, nothing sent, no draft', code == 404 and r['error'] == 'unknown_chat' and nf == 2, str(r))
    ctx.TGS.set_allowed(['tg:u7'])
    code, r = sf('tg:u7', 'clip.mp4', b'not really a video')
    check('allowlisted chat: mp4 goes as a video', code == 200 and r['kind'] == 'video' and fake.files[-1][0][:2] == ('u', 7) and fake.files[-1][4] == 'video', str(r))
    check('sent log records the file', ctx.TGS.sent_log()[0]['body'] == '[视频] clip.mp4' and ctx.TGS.sent_log()[0]['via'] == 'allowlist')
    code, r = sf('tg:u7', 'big.bin', b'x' * (50 * 1024 * 1024 + 1))
    check('over 50 MB refused', code == 400 and r['error'] == 'too_long', str(code) + str(r)[:200])
    code, r = sf('tg:u7', 'ok.bin', b'x' * (25 * 1024 * 1024))
    check('25 MB is fine from the user account', code == 200 and fake.files[-1][2] == 25 * 1024 * 1024, str(code))
    ctx.TGS.set_allowed([])
    check('WeChat chats are refused', sf('wxid_a', 'a.png')[0] == 404)
    check('help documents send_file for tg:me', 'send_file' in call('/api/help?for=all', raw=True)[1].decode() and '50MB' in call('/api/help?for=all', raw=True)[1].decode())
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
    h = call('/chat?c=tg:u7', raw=True)[1].decode()
    check('chat page: scrolls to the latest, has the auto-refresh switch and the new-messages pill logic', 'scrollTo(0,document.documentElement.scrollHeight)' in h and 'id="autot"' in h and 'newpill' in h)
    check('home and Telegram tabs also have the auto-refresh switch', all('id="autot"' in call(u, raw=True)[1].decode() for u in ('/', '/tg')))

    # ---- built-in proxy: saving a subscription puts it first in the Telegram proxy list; off takes it out
    ctx.TGS.set_config(proxy='socks5://r:1')
    code, _ = post_form('/proxy/config', sub='https://example.invalid/sub')
    check('proxy: subscription saved, built-in proxy first, old one kept', code == 303 and ctx.TGS.config()['proxy'] == 'socks5://proxy:7890, socks5://r:1')
    h = call('/settings', raw=True)[1].decode()
    check('proxy: page says enabled, never echoes the URL', '未启用' not in h and 'example.invalid' not in h and '停用' in h)
    code, _ = post_form('/proxy/config', sub='')
    check('proxy: saving empty keeps the URL', open(os.path.join(os.environ['VAR'], 'mihomo', 'sub_url')).read().strip() == 'https://example.invalid/sub')
    code, _ = post_form('/proxy/config', off='1')
    check('proxy: off empties the URL and restores the list', code == 303 and ctx.TGS.config()['proxy'] == 'socks5://r:1' and '未启用' in call('/settings', raw=True)[1].decode())
    code, _ = post_form('/proxy/config', sub='ftp://nope')
    check('proxy: bad URL is refused', code == 400)
    ctx.TGS.set_config(proxy='')

    n0 = fake.calls.count('dialogs')
    code, _ = post_form('/refresh', to='/tg'); settle(); check('refresh on the Telegram tab re-reads the dialog list (in the background)', code == 303 and fake.calls.count('dialogs') == n0 + 1)
    kv = ctx.db(); kv.execute("insert or replace into kv values('tg:fetch:tg:u7', ?)", (str(time.time()),)); kv.commit(); kv.close()
    m0 = sum(1 for x in fake.calls if isinstance(x, tuple) and x[0] == 'messages')
    code, r = post_form('/refresh', to='/chat?c=tg%3Au7'); settle(); check('refresh in a Telegram chat forces a re-read even inside the 60 s cache', code == 303 and sum(1 for x in fake.calls if isinstance(x, tuple) and x[0] == 'messages') == m0 + 1)
    ctx.INGEST_NOW.clear(); t0 = time.time()
    code, _ = post_form('/refresh', to='/'); check('refresh on the WeChat tab wakes the reader', code == 303 and ctx.INGEST_NOW.is_set())
    for bad in ('//evil.example/x', 'https://evil.example', '/\\evil.example'):
        import urllib.request as ur
        class NR(ur.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        try: ur.build_opener(NR).open(ur.Request(B + '/refresh', data=up.urlencode({'to': bad}).encode(), method='POST'))
        except ur.HTTPError as e: check(f'refresh will not redirect off-site ({bad[:18]})', e.headers.get('Location') == '/')
    check('safe_back keeps same-site paths', pages.safe_back('/chat?c=a') == '/chat?c=a' and pages.safe_back('') == '/')

    v1 = call('/ping?tab=wx', raw=True)[1].decode()
    c = ctx.db(); c.execute("insert into msgs(chat,local_id,ts,type,sender,text) values('wxid_a',2,?,1,'小王','又一条')", (int(time.time()),)); c.commit(); c.close()
    v2 = call('/ping?tab=wx', raw=True)[1].decode()
    check('ping token changes when a WeChat message arrives', v1 != v2)
    check('ping token is stable when nothing changed', v2 == call('/ping?tab=wx', raw=True)[1].decode())
    v1 = call('/ping?tab=chat&c=tg%3Au7', raw=True)[1].decode()
    fake.msgs[('u', 7)].insert(0, dict(id=7, ts=int(time.time()), sender='小明', text='刚到的新消息', media=None, name=''))
    kv = ctx.db(); kv.execute("delete from kv where k like 'tg:fetch:%'"); kv.commit(); kv.close()
    v2 = call('/ping?tab=chat&c=tg%3Au7', raw=True)[1].decode()
    check('ping in a Telegram chat reads Telegram (once per minute) and the token changes', v1 != v2 and '刚到的新消息' in json.dumps(call('/api/messages?chat=tg:u7&live=0')[1], ensure_ascii=False))
    m0 = sum(1 for x in fake.calls if isinstance(x, tuple) and x[0] == 'messages')
    call('/ping?tab=chat&c=tg%3Au7', raw=True); call('/ping?tab=chat&c=tg%3Au7', raw=True)
    check('repeated pings do not hammer Telegram', sum(1 for x in fake.calls if isinstance(x, tuple) and x[0] == 'messages') == m0)
    fake.flood = 30; ctx.TGS.flood_until = 0
    kv = ctx.db(); kv.execute("delete from kv where k like 'tg:fetch:%'"); kv.commit(); kv.close()
    check('ping survives Telegram being rate limited', call('/ping?tab=chat&c=tg%3Au7', raw=True)[0] == 200)
    fake.flood = 0; ctx.TGS.flood_until = 0

    # ---- pages never wait for Telegram; nothing reloads while you are leaving a page
    real_messages = fake.messages
    def slow_messages(*a, **k):
        time.sleep(2); return real_messages(*a, **k)
    fake.messages = slow_messages
    kv = ctx.db(); kv.execute("delete from kv where k like 'tg:fetch:%'"); kv.commit(); kv.close()
    t0 = time.time(); h = call('/chat?c=tg:u7', raw=True)[1].decode(); dt = time.time() - t0
    check('a Telegram chat page opens at once even when Telegram takes 2 s', dt < 1 and '看这张' in h and 'data-maxid=' in h, f'{dt:.2f}s')
    code, d = call('/sync?c=tg:u7&after=999999')
    check('meanwhile /sync reports the background read', code == 200 and d['running'] is True)
    settle(); code, d = call('/sync?c=tg:u7&after=999999')
    check('and when it is done says so', d['running'] is False and d['error'] == '')
    fake.messages = real_messages
    top = d['maxid']
    fake.msgs[('u', 7)].insert(0, dict(id=50, ts=int(time.time()), sender='小明', text='同步来的新消息', media=None, name=''))
    call(f'/sync?c=tg:u7&after={top}&force=1'); settle()
    code, d = call(f'/sync?c=tg:u7&after={top}')
    check('/sync returns only what is new, as ready-made message rows', '同步来的新消息' in d['html'] and d['html'].count('class="m"') == 1 and d['maxid'] > top)
    check('asking again with the new maxid returns nothing', call(f"/sync?c=tg:u7&after={d['maxid']}")[1]['html'] == '')
    n_tg = sum(1 for x in fake.calls if isinstance(x, tuple) and x[0] == 'messages')
    code, d = call('/sync?c=wxid_a&after=0')
    check('/sync on a WeChat chat reads the local store only', code == 200 and '小王' in d['html'] and sum(1 for x in fake.calls if isinstance(x, tuple) and x[0] == 'messages') == n_tg)
    fake.flood = 30; ctx.TGS.flood_until = 0
    call('/sync?c=tg:u7&after=0&force=1'); settle(); code, d = call('/sync?c=tg:u7&after=999999')
    check('a Telegram error during the background read is shown, not swallowed', d['error'] and d['retry_after'] == 30 and d['running'] is False, str(d))
    fake.flood = 0; ctx.TGS.flood_until = 0
    h = call('/', raw=True)[1].decode()
    check('every page marks a tap as leaving, and the list refresh checks it first', 'window.__leaving=true' in h and 'if(window.__leaving||!on()' in h)
    check('timed reloads never fire while leaving', 'if(!window.__leaving)location.reload()' in pages.RELOAD3_JS and 'if(!window.__leaving)location.reload()' in pages.RELOAD_JS)
    h = call('/chat?c=tg:u7', raw=True)[1].decode()
    check('chat pages do not reload themselves; they merge new messages in place',
          "if(!chat){setInterval" in h and "tab=chat" in h and '/sync?c=' in h and 'location.reload' not in pages.SYNC_JS)
    pages.TG_BG['running'] = True
    h = call('/tg', raw=True)[1].decode()
    check('Telegram tab while the list is read: a note and a faster check, no 3 s reload loop', '正在更新会话列表' in h and 'fast=1' in h and 'location.reload()},3000' not in h)
    pages.TG_BG['running'] = False

    # ---- search: names + text, highlighted, jumps to the hit even in old history
    c = ctx.db(); c.execute("insert into chats(chat,name,last_local) values('wxid_big','资料群',1)")
    for i in range(1, 401):
        c.execute("insert into msgs(chat,local_id,ts,type,sender,text) values('wxid_big',?,?,1,'小王',?)", (i, int(time.time()) - 9000 + i, f'第{i}条 ' + ('针对报价的讨论 ' if i == 20 else '普通内容')))
    c.commit(); target = c.execute("select id from msgs where chat='wxid_big' and local_id=20").fetchone()[0]; c.close()
    h = call('/?q=' + up.quote('报价'), raw=True)[1].decode()
    check('WeChat search finds message text, highlighted, with a link that jumps to it', '<mark>报价</mark>' in h and f'around={target}#m{target}' in h)
    h = call('/?q=' + up.quote('资料'), raw=True)[1].decode()
    check('WeChat search also finds chats by name', '资料群' in h and '会话' in h)
    h = call(f'/chat?c=wxid_big&around={target}', raw=True)[1].decode()
    check('old hit: the chat page loads the window around it, not just the latest 300', f'id="m{target}"' in h and '第20条' in h and '跳到最新消息' in h)
    check('window around a hit does not drag in the whole chat', '第400条' not in h)
    h = call('/chat?c=wxid_big', raw=True)[1].decode()
    check('without around the page is the latest messages', '第400条' in h and '第20条' not in h)
    h = call('/tg?q=msg2', raw=True)[1].decode()
    check('Telegram search finds cached message text, highlighted', '<mark>msg2</mark>' in h and '只搜已经读取过的会话' in h)
    h = call('/tg?q=' + up.quote('项目'), raw=True)[1].decode()
    check('Telegram search still matches chat names', '项目群' in h)
    check('snippet shows the area around a late match', '<mark>X</mark>' in pages.snippet('a' * 200 + 'X' + 'b' * 200, 'X') and pages.snippet('a' * 200 + 'X', 'X').startswith('…'))
    check('snippet escapes html', '&lt;b&gt;' in pages.snippet('<b>hi</b>', 'hi') and '<b>' not in pages.snippet('<b>hi</b>', 'hi'))

    # ---- pinned chats: display only
    c = ctx.db(); c.execute("insert into chats(chat,name,last_local) values('filehelper','文件传输助手',1)")
    c.execute("insert into msgs(chat,local_id,ts,type,sender,text) values('filehelper',1,?,1,'我','很久以前的一条')", (int(time.time()) - 86400 * 400,))
    c.commit(); before = [tuple(r) for r in c.execute('select chat, local_id, id from msgs order by id')]; nchats = c.execute('select count(*) from chats').fetchone()[0]; c.close()
    h = call('/', raw=True)[1].decode()
    check('WeChat tab: 文件传输助手 is first, even though it is the oldest chat', h.index('文件传输助手') < h.index('小王') < h.index('资料群') and '置顶' in h)
    h = call('/tg', raw=True)[1].decode()
    check('Telegram tab: Saved Messages is first, even though it is the oldest', h.index('Saved Messages') < h.index('项目群') < h.index('小明') and '置顶' in h)
    c = ctx.db(); after = [tuple(r) for r in c.execute('select chat, local_id, id from msgs order by id')]; n2 = c.execute('select count(*) from chats').fetchone()[0]; c.close()
    check('pinning writes nothing to the database', before == after and nchats == n2)
    check('the API keeps its time order (Saved Messages last, not pinned)', [x['chat'] for x in call('/api/chats?source=telegram')[1]] == ['tg:g9', 'tg:u7', 'tg:me'])
    check('WeChat API order also unchanged (filehelper is oldest)', [x['chat'] for x in call('/api/chats?source=wechat')[1]][-1] == 'filehelper')

    # ---- history is shown in time order even when older messages were filled in later
    now2 = int(time.time())
    fake.msgs[('g', 9)] = [dict(id=i, ts=now2 - (6 * 86400 if i <= 2 else 3600 * (10 - i)), sender='甲', text=f'群消息{i}', media=None, name='') for i in range(6, 0, -1)]
    call('/api/messages?chat=tg:g9&days=1&limit=2'); code, ms = call('/api/messages?chat=tg:g9&days=30&limit=50')
    check('a small first request does not stop the full history from loading', code == 200 and len(ms) == 6, str(len(ms)))
    check('API returns messages in time order after a backfill', [m['text'] for m in ms] == [f'群消息{i}' for i in range(1, 7)])
    h = call('/chat?c=tg:g9', raw=True)[1].decode()
    check('chat page shows them in time order too', h.index('群消息1') < h.index('群消息3') < h.index('群消息6'))

    # ---- scroll to the top loads older messages
    h = call('/chat?c=wxid_big', raw=True)[1].decode()
    check('chat page has the container, per-message cursors and the loader', 'id="msgs"' in h and 'data-ts=' in h and "'/older?c='" in h)
    first_id = int(re.search(r'<div class="m" id="m(\d+)"', h).group(1)) if (re := __import__('re')) else 0
    c = ctx.db(); r = c.execute('select ts, id, local_id from msgs where id=?', (first_id,)).fetchone(); c.close()
    code, d = call(f'/older?c=wxid_big&ts={r["ts"]}&id={r["id"]}')
    check('WeChat: older page returns the 100 messages before the first shown', code == 200 and d['count'] == 100 and '第1条' in d['html'] and '第100条' in d['html'] and d['more'] is False, str((r['local_id'], d.get('count'), d.get('more'))))
    check('older messages come oldest first', d['html'].index('第1条') < d['html'].index('第50条') < d['html'].index('第100条'))
    c = ctx.db(); mid = c.execute("select ts, id from msgs where chat='wxid_big' and local_id=300").fetchone(); c.close()
    seen, ts_, id_, rounds, more = [], mid['ts'], mid['id'], 0, True
    while more and rounds < 10:
        code, d = call(f'/older?c=wxid_big&ts={ts_}&id={id_}')
        nums = [int(x) for x in re.findall(r'第(\d+)条', d['html'])]
        seen += nums; rounds += 1; more = d['more']
        c = ctx.db(); row = c.execute("select ts, id from msgs where chat='wxid_big' and local_id=?", (min(nums),)).fetchone(); c.close(); ts_, id_ = row['ts'], row['id']
    check('paging up repeatedly walks the whole history with no gaps or repeats', sorted(seen) == list(range(1, 300)) and len(set(seen)) == len(seen) and more is False, f'{len(seen)} {rounds}')

    fake.msgs[('me', 0)] = [dict(id=i, ts=int(time.time()) - 90 * 86400 + i, sender='我', text=f'收藏{i}', media=None, name='') for i in range(300, 0, -1)]
    kv = ctx.db(); kv.execute('delete from msgs where chat="tg:me"'); kv.execute("delete from kv where k like 'tg:%:tg:me'"); kv.commit(); kv.close()
    h = call('/chat?c=tg:me', raw=True)[1].decode()
    check('Telegram: a chat never read before opens at once with a "reading" note', '正在从 Telegram 读取最近的消息' in h and 'id="msgs"' in h)
    settle(); code, d = call('/sync?c=tg:me&after=0')
    check('Telegram: the background read brings only the newest 200', code == 200 and '收藏300' in d['html'] and '收藏101' in d['html'] and '收藏100' not in d['html'] and d['running'] is False)
    h = call('/chat?c=tg:me', raw=True)[1].decode()
    check('Telegram: opened again, the page shows them straight from the cache', '收藏300' in h and '收藏101' in h)
    first_id = int(re.search(r'<div class="m" id="m(\d+)"', h).group(1))
    c = ctx.db(); r = c.execute('select ts, id from msgs where id=?', (first_id,)).fetchone(); c.close()
    code, d = call(f'/older?c=tg:me&ts={r["ts"]}&id={r["id"]}')
    check('Telegram: scrolling up reads older messages from Telegram on demand', code == 200 and d['count'] == 100 and '收藏1' in d['html'] and '收藏100' in d['html'], str(d.get('count')))
    c = ctx.db(); r = c.execute('select ts, id from msgs where chat="tg:me" order by ts limit 1').fetchone(); c.close()
    code, d = call(f'/older?c=tg:me&ts={r["ts"]}&id={r["id"]}')
    check('Telegram: the start of the chat is reached and reported', d['more'] is False and d['count'] == 0)
    fake.flood = 30; ctx.TGS.flood_until = 0
    c = ctx.db(); c.execute('delete from msgs where chat="tg:me" and local_id<100'); c.execute("delete from kv where k='tg:end:tg:me'"); c.commit(); c.close()
    c = ctx.db(); r = c.execute('select ts, id from msgs where chat="tg:me" order by ts limit 1').fetchone(); c.close()
    code, d = call(f'/older?c=tg:me&ts={r["ts"]}&id={r["id"]}')
    check('Telegram: rate limit while scrolling gives a message the page can show', d.get('error') == 'rate_limited' and d.get('retry_after') == 30 and 'message' in d, str(d))
    fake.flood = 0; ctx.TGS.flood_until = 0

    # ---- contacts
    c = ctx.db()
    wechat.save_contacts(c, [('wxid_a', '老王', '王哥', 'wang01', 1), ('wxid_big', '', '资料群友', None, 1), ('chat9@chatroom', '', '读书群', None, 2),
                          ('gh_news', '', '某公众号', None, 4), ('wxid_nobody', '', '从没聊过', None, 1), ('weixin', '', '微信团队', None, 4),
                          ('wxid_member', '', '群里见过的人', None, 3)])
    c.close()
    code, cs = call('/api/contacts?source=wechat')
    names = {x['id']: x for x in cs}
    check('WeChat contacts: people and groups by default, official accounts and system entries hidden',
          code == 200 and {'wxid_a', 'wxid_nobody', 'chat9@chatroom'} <= set(names) and not ({'gh_news', 'weixin'} & set(names)))
    check('kind=other reaches official accounts, kind=all everything', [x['id'] for x in call('/api/contacts?source=wechat&kind=other')[1]].count('gh_news') == 1
          and {'gh_news', 'weixin', 'wxid_a'} <= {x['id'] for x in call('/api/contacts?source=wechat&kind=all')[1]})
    w = names['wxid_a']
    check('a contact carries name, remark, nickname, alias and the chat id', w['name'] == '老王' and w['remark'] == '老王' and w['nickname'] == '王哥' and w['alias'] == 'wang01' and w['chat'] == 'wxid_a' and w['kind'] == 'person')
    check('the raw WeChat type is returned and can be filtered on', w['type'] == 1 and [x['id'] for x in call('/api/contacts?source=wechat&type=3')[1]] == ['wxid_member'])
    check('people only met in groups (type 3) are hidden by default, shown with kind=all', 'wxid_member' not in names and 'wxid_member' in {x['id'] for x in call('/api/contacts?source=wechat&kind=all')[1]})
    check('group chats (type 2) are in the default list', names['chat9@chatroom']['type'] == 2)
    check('has_chat tells people you talked to from people you never did', w['has_chat'] is True and names['wxid_nobody']['has_chat'] is False)
    check('search by remark, nickname, alias', all([x['id'] for x in call('/api/contacts?source=wechat&q=' + up.quote(q))[1]] == ['wxid_a'] for q in ('老王', '王哥', 'WANG01')))
    code, tc = call('/api/contacts?source=telegram')
    check('Telegram contacts include people you never opened a chat with', code == 200 and {x['id'] for x in tc} == {'tg:u7', 'tg:u50'})
    t7 = next(x for x in tc if x['id'] == 'tg:u7'); t50 = next(x for x in tc if x['id'] == 'tg:u50')
    check('Telegram contact has @username, has_chat is accurate', t7['username'] == 'xiaoming' and t7['has_chat'] is True and t50['has_chat'] is False and t50['source'] == 'telegram')
    check('no phone numbers anywhere in the contact data', 'phone' not in json.dumps(tc).lower())
    check('search Telegram contacts by @username', [x['id'] for x in call('/api/contacts?source=telegram&q=@NoChat')[1]] == ['tg:u50'])
    n0 = fake.calls.count('contacts'); call('/api/contacts?source=telegram'); call('/api/contacts')
    check('the Telegram address book is cached', fake.calls.count('contacts') == n0)
    check('no source: both platforms in one list', {x['source'] for x in call('/api/contacts')[1]} == {'wechat', 'telegram'})
    check('contacts need a key', call('/api/contacts', auth=False)[0] == 401)

    # ---- Telegram live search
    c = ctx.db(); n_before = c.execute('select count(*) from msgs').fetchone()[0]; c.close()
    code, lv = call('/api/search?q=' + up.quote('很久以前') + '&source=telegram&live=1')
    check('live search through the API reaches history that is not cached', code == 200 and len(lv) == 2 and all(x['live'] and x['message_id'] and 'id' not in x for x in lv), str(lv)[:200])
    code, cached = call('/api/search?q=' + up.quote('很久以前') + '&source=telegram')
    check('the normal search still only sees the cache', code == 200 and cached == [])
    c = ctx.db(); check('live search does not change the database', c.execute('select count(*) from msgs').fetchone()[0] == n_before); c.close()
    code, one = call('/api/search?q=' + up.quote('很久以前') + '&chat=tg:u7&live=1&limit=1')
    check('live search can be limited to a chat and a count', code == 200 and len(one) == 1 and one[0]['chat'] == 'tg:u7')
    code, bad = call('/api/search?q=&source=telegram&live=1'); check('empty keyword is a clear 400', code == 400 and bad['error'] == 'empty')
    fake.flood = 30; ctx.TGS.flood_until = 0
    code, bad = call('/api/search?q=' + up.quote('没搜过的词') + '&source=telegram&live=1')
    check('live search rate limit -> 429 with retry_after', code == 429 and bad['retry_after'] == 30)
    fake.flood = 0; ctx.TGS.flood_until = 0
    h = call('/tgsearch?q=' + up.quote('很久以前'), raw=True)[1].decode()
    check('Telegram search page lists hits with the match highlighted', '<mark>很久以前</mark>' in h and '没有存进库里' in h)
    h = call('/tg?q=' + up.quote('很久以前'), raw=True)[1].decode()
    check('Telegram tab offers the full-history search after the cached results', '在 Telegram 里搜全部历史' in h and '/tgsearch?q=' in h)
    check('WeChat help does not mention live search, Telegram help does', 'live=1' not in call('/api/help?for=wechat', auth=False)[1] and 'live=1' in call('/api/help?for=telegram', auth=False)[1])

    # ---- after_id only delivers messages that really arrived later, never backfilled history
    nowb = int(time.time())
    kv = ctx.db(); kv.execute('delete from msgs where chat="tg:g9"'); kv.execute("delete from kv where k like 'tg:%:tg:g9'"); kv.commit(); kv.close()
    fake.msgs[('g', 9)] = ([dict(id=i, ts=nowb - 400 * 86400 + i, sender='甲', text=f'远古{i}', media=None, name='') for i in range(1, 6)] +
                           [dict(id=i, ts=nowb - 3600 + i, sender='甲', text=f'最近{i}', media=None, name='') for i in range(10, 13)])
    call('/api/messages?chat=tg:g9')                     # first read of the chat: recent window
    c = ctx.db(); cursor = c.execute('select max(id) from msgs').fetchone()[0]
    first = c.execute('select ts, id from msgs where chat="tg:g9" order by ts, id limit 1').fetchone(); c.close()
    call(f'/older?c=tg:g9&ts={first["ts"]}&id={first["id"]}')     # scroll up: 400 day old history comes in
    c = ctx.db(); n_old = c.execute("select count(*) from msgs where chat='tg:g9' and text like '远古%'").fetchone()[0]; c.close()
    code, inc = call(f'/api/messages?after_id={cursor}')
    check('backfilled history is stored but not handed out as new via after_id', n_old == 5 and code == 200 and not [m for m in inc if '远古' in m['text']], str((n_old, [m['text'] for m in inc][:5])))
    fake.msgs[('g', 9)].append(dict(id=99, ts=int(time.time()), sender='甲', text='真正的新消息', media=None, name=''))
    kv = ctx.db(); kv.execute("delete from kv where k='tg:fetch:tg:g9'"); kv.commit(); kv.close()
    call('/api/messages?chat=tg:g9')
    code, inc = call(f'/api/messages?after_id={cursor}')
    check('a message that really arrives later does come through after_id', [m['text'] for m in inc if m['chat'] == 'tg:g9'] == ['真正的新消息'], str([m['text'] for m in inc]))
    code, ms = call('/api/messages?chat=tg:g9&live=0&limit=2000')
    c = ctx.db(); hit = c.execute("select id from msgs where chat='tg:g9' and text='远古3'").fetchone()[0]; c.close()
    h = call(f'/chat?c=tg:g9&around={hit}', raw=True)[1].decode()
    check('a search hit in backfilled history opens at that message', f'id="m{hit}"' in h and '远古3' in h)
    check('reading the chat still shows the backfilled history in time order', [m['text'] for m in ms] == [f'远古{i}' for i in range(1, 6)] + ['最近10', '最近11', '最近12', '真正的新消息'], str([m['text'] for m in ms]))

    import runenv
    check('a WECHAT_DIR that does not exist is used as is, never another WeChat on this machine', runenv.find_wechat_dir() == TMP + '/none' and ctx.WECHAT == TMP + '/none')

    # ---- login state shown on settings
    fake.st = 'logged_out'; os.remove(TMP + '/tg.session')
    code, h = call('/settings', raw=True); check('logged out: offers QR login', '用二维码登录' in h.decode())
    code, st = call('/api/status'); check('status reports telegram', 'telegram' in st and st['telegram']['configured'])
    code, h = call('/tg', raw=True); check('not logged in: Telegram tab points to settings', '去设置' in h.decode())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print('ALL PASS' if ok else 'FAILURES')
sys.exit(0 if ok else 1)
