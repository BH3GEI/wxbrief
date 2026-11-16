#!/usr/bin/env python3
"""The bot pool, end to end through the HTTP server: fake Telegram account, fake Bot API, fake BotFather."""
import asyncio, json, os, re, shutil, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(dir=os.path.join(HERE, '..'))
os.makedirs(TMP + '/none')
os.environ.update(VAR=TMP, WORK=TMP + '/work', WECHAT_DIR=TMP + '/none')
sys.path.insert(0, os.path.join(HERE, '..', 'content')); sys.path.insert(0, HERE)
import app, ctx, apikeys, tg, tgbot  # noqa: E402
from tgfake import Fake  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

ok = True
def check(name, cond, extra=''):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name + (f'  [{extra}]' if not cond and extra else ''))
    ok = ok and bool(cond)


class BotAPI:
    """Fake Bot API: per-token update queues, sent messages, display names."""
    def __init__(self):
        self.updates, self.sent, self.names, self.uid = {}, [], {}, 0
    def push(self, token, from_id, text, chat_type='private', mid=None):
        self.uid += 1
        self.updates.setdefault(token, []).append({'update_id': self.uid, 'message': {
            'message_id': mid or 1000 + self.uid, 'date': int(time.time()), 'text': text,
            'from': {'id': from_id}, 'chat': {'id': from_id, 'type': chat_type}}})
    def call(self, token, method, params=None, proxy='', timeout=30):
        p = params or {}
        if method == 'getUpdates':
            return [u for u in self.updates.get(token, []) if u['update_id'] >= p.get('offset', 0)]
        if method == 'sendMessage':
            self.sent.append((token, p['chat_id'], p['text']))
            return {'message_id': 500 + len(self.sent), 'date': int(time.time())}
        if method == 'setMyName':
            self.names[token] = p['name']; return True
        raise AssertionError(method)


class K:
    state = 'ok'; capture_note = ''
    pending = staticmethod(lambda: False); capturer_alive = staticmethod(lambda: True)
class IKs: state = 'ok'

clock = [time.time()]
try:
    ctx.init_store(); ctx.APIKEYS = apikeys.ApiKeys(); ctx.IK = IKs
    fake = Fake(); ctx.TGS = tg.Service(TMP, ctx.db, ctx.log, adapter=fake)
    ctx.TGS.set_config(api_id='1', api_hash='h'); open(TMP + '/tg.session', 'w').write('s')
    api = BotAPI()
    ctx.BOTS = tgbot.Bots(TMP, ctx.db, ctx.log, ctx.TGS, key_name=lambda kid: (ctx.APIKEYS.get(kid) or {}).get('name'),
                          call=api.call, now=lambda: clock[0], autolisten=False)
    A = ctx.APIKEYS.create('Claude Code'); B = ctx.APIKEYS.create('pi')
    srv = ThreadingHTTPServer(('127.0.0.1', 0), app.make_handler(K)); base = f'http://127.0.0.1:{srv.server_address[1]}'
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(path, method='GET', body=None, key=A, raw=False):
        h = {'Authorization': 'Bearer ' + key['key']} if key else {}
        data = json.dumps(body).encode() if body is not None else None
        if data: h['Content-Type'] = 'application/json'
        try:
            r = urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=h, method=method), timeout=30)
            code, b = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, b = e.code, e.read()
        if raw:
            return code, b.decode()
        try:
            return code, json.loads(b or b'null')
        except ValueError:
            return code, b.decode()

    def form(path, **kv):
        class NR(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        try:
            r = urllib.request.build_opener(NR).open(urllib.request.Request(base + path, data=urllib.parse.urlencode(kv).encode(), method='POST'))
            return r.status
        except urllib.error.HTTPError as e:
            return e.code

    tokens = lambda: [b['token'] for b in json.load(open(TMP + '/tg_bots.json'))['bots'].values()]
    by_user = lambda: json.load(open(TMP + '/tg_bots.json'))['bots']

    # ---------------- the pool only starts when the user turns it on (settings page)
    check('the pool is off until the user turns it on', ctx.BOTS.fill_step() == 600 and by_user() == {} if os.path.exists(TMP + '/tg_bots.json') else ctx.BOTS.fill_step() == 600 and not fake.created)
    code, e = call('/api/bots', 'POST', {'purpose': '日报'})
    check('an empty pool says so (pool_empty), not "all busy"', code == 409 and e['error'] == 'pool_empty')
    check('the API cannot change the pool settings', call('/api/bots/config', 'POST', {'size': 20})[0] == 404)
    check('turning it on is a settings-page form', form('/bots/fill', on='1') == 303 and ctx.BOTS.config()['fill'] is True)
    waits = [ctx.BOTS.fill_step() for _ in range(7)]
    bots = by_user()
    check('the pool fills up to the limit (5) and then stops', len(bots) == 5 and fake.created == 5 and waits[-1] == 600, str(waits))
    check('default names exterior 1..5, usernames exterior_…_bot', sorted(b['default_name'] for b in bots.values()) == [f'exterior {i}' for i in range(1, 6)]
          and all(re.fullmatch(r'exterior_[0-9a-f]{4}_bot', u) for u in bots))
    check('each new bot got /start from the owner account', sorted(fake.started) == sorted(bots))
    check('tokens are kept in a 0600 file', oct(os.stat(TMP + '/tg_bots.json').st_mode & 0o777) == '0o600')
    check('owner id comes from the account', all(b['owner'] == fake.owner for b in bots.values()))
    check('BotFather was only ever asked to create bots', all(c[0] == 'create_bot' for c in fake.calls if isinstance(c, tuple) and c[0].startswith('create')))

    # BotFather pacing and the Telegram-wide limit
    ctx.BOTS.set_config(size=7)
    fake.botfather_error = tg.TGError('botfather', 'BotFather 没有接受新建：Sorry, too many attempts. Please try again in 8 seconds.')
    w = ctx.BOTS.fill_step()
    check('BotFather asking to wait is respected', w == 13 and '8 秒' in ctx.BOTS.fill_note, str((w, ctx.BOTS.fill_note)))
    fake.botfather_error = tg.TGError('botfather', 'BotFather 没有接受新建：That I cannot do. You come to me asking for more than 20 bots.')
    ctx.BOTS.fill_step()
    check('the Telegram-wide limit switches the pool off and says so', ctx.BOTS.config()['fill'] is False and '已到上限' in ctx.BOTS.fill_note)
    ctx.BOTS.fill_step(); ctx.BOTS.fill_step()
    check('the reason stays visible after the pool stopped', '已到上限' in ctx.BOTS.fill_note and '已到上限' in call('/api/status')[1]['bots']['note'])
    again = tgbot.Bots(TMP, ctx.db, ctx.log, ctx.TGS, key_name=lambda k: None, call=api.call, autolisten=False)
    check('the reason survives a restart', '已到上限' in again.summary()['note'])
    ctx.BOTS.set_config(fill=True); check('turning it on again clears the old reason', ctx.BOTS.fill_note == '')
    ctx.BOTS.set_config(fill=False); check('pausing it says paused', ctx.BOTS.fill_note == '已暂停')
    ctx.BOTS.set_config(size=50)
    try: ctx.BOTS.set_config(size=51); check('the limit can go up to 50, not beyond', False)
    except tg.TGError as e: check('the limit can go up to 50 (Premium accounts), not beyond', e.code == 'bad_config' and ctx.BOTS.config()['size'] == 50)
    ctx.BOTS.set_config(size=5)

    # ---------------- claiming
    code, b1 = call('/api/bots', 'POST', {'purpose': '日报'})
    check('claim returns a chat, username and link', code == 200 and b1['chat'].startswith('tgbot:exterior_') and b1['link'].startswith('https://t.me/') and b1['mine'])
    tok1 = by_user()[b1['username']]['token']
    check('display name becomes "key name · purpose" (on Telegram too)', b1['name'] == 'Claude Code · 日报' and api.names[tok1] == 'Claude Code · 日报')
    check('a "new task" line is sent to the owner', api.sent[-1] == (tok1, fake.owner, '—— 新任务：Claude Code · 日报 ——'))
    for bad in ('', '   ', '一' * 21):
        code, e = call('/api/bots', 'POST', {'purpose': bad})
        check(f'purpose must be 1-20 characters ({len(bad)})', code == 400 and e['error'] == 'bad_purpose')
    code, b2 = call('/api/bots', 'POST', {'purpose': '盯部署'})
    check('one key may hold several bots', code == 200 and b2['chat'] != b1['chat'])
    code, lst = call('/api/bots')
    mine = {x['chat'] for x in lst if x['mine']}
    check('GET /api/bots: status, holder, purpose, mine', mine == {b1['chat'], b2['chat']} and sum(x['status'] == 'busy' for x in lst) == 2
          and next(x for x in lst if x['chat'] == b1['chat'])['purpose'] == '日报')
    check('another key sees them as not its own', not any(x['mine'] for x in call('/api/bots', key=B)[1]))

    # ---------------- sending: only the holder, only to the owner
    code, e = call('/api/send', 'POST', {'chat': b1['chat'], 'text': 'hi'}, key=B)
    check('a key that does not hold the bot cannot send through it', code == 403 and e['error'] == 'not_holder')
    free = next(x for x in lst if x['status'] == 'free')
    code, e = call('/api/send', 'POST', {'chat': free['chat'], 'text': 'hi'})
    check('nobody can send through a free bot', code == 403 and e['error'] == 'not_holder')
    code, r = call('/api/send', 'POST', {'chat': b1['chat'], 'text': '日报写好了，要看吗？'})
    check('the holder sends at once, gets the stored id back', code == 200 and r['status'] == 'sent' and isinstance(r['id'], int))
    check('the bot only ever writes to its owner', all(chat_id == fake.owner for _, chat_id, _ in api.sent))
    code, ms = call('/api/messages?chat=' + urllib.parse.quote(b1['chat']))
    check('the message is in the chat, sender is the bot, source telegram', ms[-1]['text'] == '日报写好了，要看吗？' and ms[-1]['sender'] == 'Claude Code · 日报' and ms[-1]['source'] == 'telegram')

    # ---------------- receiving: the owner only
    api.push(tok1, fake.owner, '要，发我')
    api.push(tok1, 999, '我是陌生人')
    api.push(tok1, fake.owner, '/start')
    api.push(tok1, fake.owner, '群里的消息', chat_type='group')
    kept = ctx.BOTS.poll_once(b1['username'])
    code, inc = call(f"/api/messages?after_id={r['id']}")
    check("only the owner's private messages are kept (stranger, /start and groups dropped)", kept == 1 and [m['text'] for m in inc] == ['要，发我'] and inc[0]['sender'] == '我')
    check('updates are not read twice', ctx.BOTS.poll_once(b1['username']) == 0)

    # ---------------- waiting for a reply
    t0 = time.time(); code, got = call(f"/api/wait?chat={urllib.parse.quote(b1['chat'])}&after_id={r['id']}&timeout=5")
    check('wait returns at once when something is already there', code == 200 and [m['text'] for m in got] == ['要，发我'] and time.time() - t0 < 2)
    top = got[-1]['id']
    t0 = time.time(); code, got = call(f"/api/wait?chat={urllib.parse.quote(b1['chat'])}&after_id={top}&timeout=1")
    check('wait times out with an empty list', code == 200 and got == [] and 0.9 < time.time() - t0 < 4)
    def later():
        time.sleep(1.2); api.push(tok1, fake.owner, '再改一下标题'); ctx.BOTS.poll_once(b1['username'])
    threading.Thread(target=later).start()
    t0 = time.time(); code, got = call(f"/api/wait?chat={urllib.parse.quote(b1['chat'])}&after_id={top}&timeout=10")
    check('wait returns as soon as the owner replies', [m['text'] for m in got] == ['再改一下标题'] and 1 < time.time() - t0 < 5, str((got, time.time() - t0)))
    ctx.BOTS.touch(b1['chat']); before = by_user()[b1['username']]['last_activity']; clock[0] += 100
    call(f"/api/wait?chat={urllib.parse.quote(b1['chat'])}&timeout=1")
    check('waiting on your own bot counts as activity', by_user()[b1['username']]['last_activity'] == int(clock[0]) > before)

    # ---------------- pool exhausted, explicit username
    for p in ('三', '四', '五'):
        call('/api/bots', 'POST', {'purpose': p}, key=B)
    code, e = call('/api/bots', 'POST', {'purpose': '六'})
    check('when all bots are in use: 409 pool_exhausted listing who uses them', code == 409 and e['error'] == 'pool_exhausted' and 'pi · 三' in e['message'])
    code, e = call('/api/bots', 'POST', {'purpose': '六', 'username': 'my_special_bot'})
    check('asking for a specific username at the limit: bot_limit', code == 409 and e['error'] == 'bot_limit')

    # ---------------- release and idle timeout
    code, e = call('/api/bots/release', 'POST', {'chat': b1['chat']}, key=B)
    check('only the holder can release', code == 403 and e['error'] == 'not_holder')
    code, r2 = call('/api/bots/release', 'POST', {'chat': b2['chat']})
    tok2 = by_user()[b2['username']]['token']
    check('release frees the bot and resets its name', code == 200 and by_user()[b2['username']]['holder'] is None and api.names[tok2].startswith('exterior '))
    clock[0] += 71 * 3600; ctx.BOTS.sweep()
    check('nothing is taken back before 72 hours', sum(1 for b in by_user().values() if b['holder']) == 4)
    clock[0] += 2 * 3600; n = ctx.BOTS.sweep()
    check('after 72 idle hours busy bots go back to the pool', n == 4 and all(not b['holder'] for b in by_user().values()))
    check('their names are reset on Telegram', api.names[tok1] == by_user()[b1['username']]['default_name'])
    code, e = call('/api/send', 'POST', {'chat': b1['chat'], 'text': 'x'})
    check('the old holder can no longer send through it', code == 403 and e['error'] == 'not_holder')
    api.push(tok1, fake.owner, '我回来了'); ctx.BOTS.poll_once(b1['username'])
    b = by_user()[b1['username']]
    check('the owner writing to a freed bot gives it back to its previous holder', b['holder'] == A['id'] and b['name'] == 'Claude Code · 日报' and api.names[tok1] == 'Claude Code · 日报')
    check('and the old holder can send again', call('/api/send', 'POST', {'chat': b1['chat'], 'text': '好的'})[0] == 200)
    # a freed bot that someone else took does not go back
    clock[0] += 73 * 3600; ctx.BOTS.sweep()
    code, lst = call('/api/bots')
    oldest_free = min((by_user()[x['username']]['freed_at'], x['username']) for x in lst)[1]
    code, nb = call('/api/bots', 'POST', {'purpose': '新任务'}, key=B)
    check('a new claim takes the bot that has been free the longest', nb['username'] == oldest_free)
    if nb['username'] == b1['username']:
        api.push(tok1, fake.owner, '还在吗'); ctx.BOTS.poll_once(b1['username'])
        check('a bot claimed by someone else does not go back to its previous holder', by_user()[b1['username']]['holder'] == B['id'])
    # previous holder whose key was revoked
    ctx.BOTS.sweep(); clock[0] += 73 * 3600; ctx.BOTS.sweep()
    u3 = call('/api/bots', 'POST', {'purpose': '临时'}, key=B)[1]['username']
    clock[0] += 73 * 3600; ctx.BOTS.sweep(); ctx.APIKEYS.revoke(B['id'])
    t3 = by_user()[u3]['token']; api.push(t3, fake.owner, '在吗'); ctx.BOTS.poll_once(u3)
    check('a freed bot does not go back to a revoked key', by_user()[u3]['holder'] is None)

    # ---------------- secrets and hidden chats
    c = ctx.db()
    c.executemany('insert into tg_dialogs(chat,name,kind,hash,last_ts,username) values(?,?,?,?,?,?)',
                  [('tg:u93372553', 'BotFather', 'u', 1, int(time.time()), 'BotFather')] +
                  [(f"tg:u{b['bot_id']}", b['default_name'], 'u', 1, int(time.time()), u) for u, b in by_user().items()])
    c.execute("insert into kv values('tg:dialogs_at', ?)", (str(time.time() + 3600),))
    c.execute("insert into msgs(chat,local_id,ts,type,sender,text,backfill) values('tg:u93372553',1,?,1,'BotFather','Use this token: " + tokens()[0] + "',0)", (int(time.time()),))
    c.commit(); c.close()
    code, chats = call('/api/chats?source=telegram')
    ids = {x['chat'] for x in chats}
    check('agents do not see the BotFather chat', 'tg:u93372553' not in ids)
    check("agents do not see the account's duplicate chats with the pool bots", not any(f"tg:u{b['bot_id']}" in ids for b in by_user().values()))
    check('the bot chats are listed under Telegram', {f'tgbot:{u}' for u in by_user()} <= ids)
    check('and not under WeChat', not any(x['chat'].startswith('tgbot:') for x in call('/api/chats?source=wechat')[1]))
    check('reading the BotFather chat is refused', call('/api/messages?chat=tg:u93372553')[0] == 403)
    check('searching never returns it', call('/api/search?q=token')[1] == [])
    code, inc = call('/api/messages?after_id=0&limit=2000')
    check('nor does an incremental pull', not any(m['chat'] == 'tg:u93372553' for m in inc))
    check('sending to BotFather is refused', call('/api/send', 'POST', {'chat': 'tg:u93372553', 'text': '/deletebot'})[0] == 403)
    everything = ''.join(call(p, raw=True)[1] for p in ('/api/bots', '/api/status', '/api/chats', '/api/messages?limit=2000', '/api/wait?timeout=1', '/api/help'))
    check('no bot token ever appears in an API response', not any(t in everything for t in tokens()))
    check('WeChat messages exclude bot chats', not any(m['chat'].startswith('tgbot:') for m in call('/api/messages?source=wechat&limit=2000')[1]))

    # ---------------- pages
    h = call('/tg', raw=True)[1]
    check('Telegram tab: one folded "exterior" row, closed by default', 'id="botfold"' in h and '<div id="botlist" hidden>' in h and h.count('class="fold"') == 1)
    check('the folded row is not a <details> (would pause auto refresh)', '<details' not in h[h.index('id="botfold"'):h.index('id="botlist"')])
    check('the page itself still shows the BotFather chat to you', 'BotFather' in h)
    check("the account's duplicate bot chats are not listed separately", not any(f"/chat?c=tg:u{b['bot_id']}" in h for b in by_user().values()))
    busy = call('/api/bots', 'POST', {'purpose': '看页面'})[1]
    h = call('/chat?c=' + urllib.parse.quote(busy['chat']), raw=True)[1]
    check('bot chat page: who uses it, and it records what you have read', 'wxb_seen_' in h and '用途' in h)
    h = call('/settings', raw=True)[1]
    check('settings: counts, limit and idle timeout', '已建 <b>5</b> / 上限 5' in h and 'name="idle_hours"' in h and 'value="72"' in h)
    check('settings changes are page-only forms', form('/bots/config', size='6', idle_hours='48') == 303 and ctx.BOTS.config()['size'] == 6 and ctx.BOTS.config()['idle_hours'] == 48)
    check('the help text explains the bots (Telegram, not WeChat)', '/api/bots' in call('/api/help?for=telegram', key=None, raw=True)[1] and '/api/bots' not in call('/api/help?for=wechat', key=None, raw=True)[1])
finally:
    shutil.rmtree(TMP, ignore_errors=True)


# ---------------- the BotFather dialogue itself (tg.Live.create_bot) against a scripted conversation
class Conv:
    def __init__(self, replies): self.replies, self.sent = list(replies), []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def send_message(self, t): self.sent.append(t)
    async def get_response(self):
        class R: pass
        r = R(); r.raw_text = self.replies.pop(0); return r
class Client:
    def __init__(self, conv): self.conv = conv
    def conversation(self, who, **kw): assert who == 'BotFather'; return self.conv
def dialogue(replies):
    live = tg.Live(lambda *a: None); conv = Conv(replies)
    async def client(): return Client(conv)
    live._client = client
    try:
        return live.create_bot('exterior 1', 'exterior_ab12_bot'), conv.sent
    except tg.TGError as e:
        return e, conv.sent
token = '7123456789:' + 'A' * 35
r, sent = dialogue(['Alright, a new bot. How are we going to call it? Please choose a name for your bot.',
                    "Good. Now let's choose a username for your bot. It must end in `bot`.",
                    f'Done! Congratulations on your new bot. Use this token to access the HTTP API:\n{token}\nKeep your token secure'])
check('BotFather: /newbot, name, username -> token', r == token and sent == ['/newbot', 'exterior 1', 'exterior_ab12_bot'])
r, sent = dialogue(['Alright, a new bot. Please choose a name for your bot.', 'Now choose a username for your bot.',
                    'Sorry, this username is already taken. Please try something different.'])
check('BotFather: a refused username cancels the dialogue', isinstance(r, tg.TGError) and sent == ['/newbot', 'exterior 1', 'exterior_ab12_bot', '/cancel'])
r, sent = dialogue(['That I cannot do. You come to me asking for more than 20 bots. Please delete one of your old bots first.'])
check('BotFather: refusing /newbot stops at once, nothing else is sent', isinstance(r, tg.TGError) and sent == ['/newbot'] and '20 bots' in r.msg)
tgsrc = open(os.path.join(HERE, '..', 'content', 'tg.py')).read(); botsrc = open(os.path.join(HERE, '..', 'content', 'tgbot.py')).read()
said = set(re.findall(r"(?:say|send_message)\('([^']*)'\)", tgsrc[tgsrc.index('def create_bot'):tgsrc.index('def start_bot')]))
check('the only fixed texts ever sent to BotFather are /newbot and /cancel', said == {'/newbot', '/cancel'}, str(said))
methods = set(re.findall(r"self\.call\(b\['token'\], '(\w+)'", botsrc))
check('the only Bot API methods used are sendMessage, setMyName, getUpdates', methods == {'sendMessage', 'setMyName', 'getUpdates'}, str(methods))

print('ALL PASS' if ok else 'FAILURES')
sys.exit(0 if ok else 1)
