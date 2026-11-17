#!/usr/bin/env python3
"""Talking to agents through one bot and a forum group, end to end through the HTTP server.
Fake Telegram account, fake Bot API, scripted BotFather dialogue."""
import json, os, re, shutil, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request

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

GROUP = -1005550001

class BotAPI:
    """Fake Bot API: update queue, sent messages, topics."""
    def __init__(self):
        self.updates, self.sent, self.calls, self.uid, self.next_topic, self.topics = [], [], [], 0, 100, {}
        self.dead, self.offline, self.gone_chats = set(), set(), set()
        self.edited, self.reacted, self.menus, self.deleted_threads = [], [], [], set()
    def push(self, from_id, text, chat_id=GROUP, thread=None, topic=True, **extra):
        self.uid += 1
        m = {'message_id': 1000 + self.uid, 'date': int(time.time()), 'from': {'id': from_id},
             'chat': {'id': chat_id, 'type': 'supergroup' if chat_id == GROUP else 'private'}, **extra}
        if text is not None:
            m['text'] = text
        if thread:
            m['message_thread_id'] = thread
            if topic:
                m['is_topic_message'] = True
        self.updates.append({'update_id': self.uid, 'message': m})
    def call(self, token, method, params=None, proxy='', timeout=30):
        p = params or {}
        self.calls.append((method, p))
        if method == 'getUpdates':
            return [u for u in self.updates if u['update_id'] >= p.get('offset', 0)]
        if method == 'sendMessage' and p.get('message_thread_id') in self.deleted_threads:
            raise tg.TGError('telegram_error', 'Bad Request: message thread not found')
        if method == 'setMyCommands':
            self.menus.append(p); return True
        if method == 'sendMessage':
            self.sent.append((p['chat_id'], p.get('message_thread_id'), p['text']))
            return {'message_id': 500 + len(self.sent), 'date': int(time.time())}
        if method == 'editMessageText':
            self.edited.append((p['chat_id'], p['message_id'], p['text'])); return {'message_id': p['message_id']}
        if method == 'setMessageReaction':
            self.reacted.append((p['chat_id'], p['message_id'], p['reaction'])); return True
        if method == 'createForumTopic':
            self.next_topic += 1; self.topics[self.next_topic] = {'name': p['name'], 'closed': False}
            return {'message_thread_id': self.next_topic, 'name': p['name']}
        if method == 'editForumTopic':
            self.topics[p['message_thread_id']]['name'] = p['name']; return True
        if method in ('closeForumTopic', 'reopenForumTopic'):
            self.topics[p['message_thread_id']]['closed'] = method == 'closeForumTopic'; return True
        if method == 'setMyName':
            return True
        if method == 'getChat':
            if p['chat_id'] in self.gone_chats:
                raise tg.TGError('telegram_error', 'Bad Request: chat not found')
            return {'id': p['chat_id'], 'type': 'supergroup', 'is_forum': True}
        if method == 'getMe':
            if token in self.dead:
                raise tg.TGError('telegram_error', 'Unauthorized')
            if token in self.offline:
                raise tg.TGError('network', '连不上 Telegram Bot API：TimeoutError，请检查代理')
            return {'id': int(token.split(':')[0]), 'is_bot': True}
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
    # an old (0.9.x) pool file with one bot: it must be reused, not recreated
    json.dump({'config': {'size': 5, 'idle_hours': 72, 'fill': False, 'note': '', 'probe': {'done': True}},
               'bots': {'exterior_0a0a_bot': {'token': '7000000001:' + 'X' * 35, 'bot_id': 7000000001, 'owner': fake.owner, 'index': 1}}},
              open(TMP + '/tg_bots.json', 'w'))
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
            return urllib.request.build_opener(NR).open(urllib.request.Request(base + path, data=urllib.parse.urlencode(kv).encode(), method='POST')).status
        except urllib.error.HTTPError as e:
            return e.code

    state = lambda: ctx.BOTS._load()
    token = state()['bot']['token']
    q = urllib.parse.quote

    # ---------------- setup only when the user asks, reusing the bot that already exists
    check('the old pool file is read: its bot is kept', state()['bot']['username'] == 'exterior_0a0a_bot' and 'bots' not in state())
    code, e = call('/api/bots', 'POST', {'purpose': '日报'})
    check('before setup: 409 not_ready, says to set it up', code == 409 and e['error'] == 'not_ready')
    check('nothing happens until the user clicks', ctx.BOTS.setup_step() == 600 and not fake.groups and fake.created == 0)
    check('the API cannot start the setup', call('/api/bots/setup', 'POST', {})[0] == 404)
    check('the setup is a settings-page form', form('/bots/setup') == 303 and ctx.BOTS.config()['setup'] is True)
    ctx.BOTS.setup_step()
    check('setup reuses the existing bot (no new bot through BotFather)', fake.created == 0 and state()['bot']['username'] == 'exterior_0a0a_bot')
    check('setup makes one group with the bot in it', fake.groups == [('exterior', 'exterior_0a0a_bot')] and state()['group']['id'] == GROUP)
    check('setup finishes and says so', ctx.BOTS.ready() and ctx.BOTS.config()['setup'] is False and ctx.BOTS.note == '已建好')
    check('the bot file is 0600', oct(os.stat(TMP + '/tg_bots.json').st_mode & 0o777) == '0o600')

    # a fresh account without a bot: one is made through BotFather; the Telegram limit is explained
    T2 = tempfile.mkdtemp(dir=TMP); f2 = Fake(); f2.groups = []; s2 = tg.Service(T2, ctx.db, ctx.log, adapter=f2)
    s2.set_config(api_id='1', api_hash='h'); open(T2 + '/tg.session', 'w').write('s')
    b2 = tgbot.Bots(T2, ctx.db, ctx.log, s2, key_name=lambda k: None, call=api.call, autolisten=False)
    b2.set_config(setup=True); b2.setup_step()
    check('without a bot, setup makes exactly one, with the plain username exterior_bot', f2.created == 1 and b2.ready() and b2._load()['bot']['username'] == 'exterior_bot')
    T7 = tempfile.mkdtemp(dir=TMP); f7 = Fake(); f7.taken = {'exterior_bot'}; f7.refuse = {'exterior_agent_bot'}
    s7 = tg.Service(T7, ctx.db, ctx.log, adapter=f7); s7.set_config(api_id='1', api_hash='h'); open(T7 + '/tg.session', 'w').write('s')
    b7 = tgbot.Bots(T7, ctx.db, ctx.log, s7, key_name=lambda k: None, call=api.call, autolisten=False)
    b7.set_config(setup=True); b7.setup_step()
    check('a taken name is skipped, a name BotFather refuses moves on to the next plain one', b7.ready() and b7._load()['bot']['username'] == 'exterior_inbox_bot' and f7.created == 1)
    f8 = Fake(); f8.taken = set(tgbot.USERNAMES); T8 = tempfile.mkdtemp(dir=TMP)
    s8 = tg.Service(T8, ctx.db, ctx.log, adapter=f8); s8.set_config(api_id='1', api_hash='h'); open(T8 + '/tg.session', 'w').write('s')
    b8 = tgbot.Bots(T8, ctx.db, ctx.log, s8, key_name=lambda k: None, call=api.call, autolisten=False)
    b8.set_config(setup=True); b8.setup_step()
    check('only when all plain names are taken: a random suffix', re.fullmatch(r'exterior_[0-9a-f]{4}_bot', b8._load()['bot']['username']))
    T3 = tempfile.mkdtemp(dir=TMP); f3 = Fake(); f3.groups = []; s3 = tg.Service(T3, ctx.db, ctx.log, adapter=f3)
    s3.set_config(api_id='1', api_hash='h'); open(T3 + '/tg.session', 'w').write('s')
    f3.botfather_error = tg.TGError('botfather', 'BotFather 没有接受新建：That I cannot do. You come to me asking for more than 20 bots.')
    b3 = tgbot.Bots(T3, ctx.db, ctx.log, s3, key_name=lambda k: None, call=api.call, autolisten=False)
    b3.set_config(setup=True); b3.setup_step()
    check('at the Telegram bot limit: setup stops and explains it', not b3.ready() and b3.config()['setup'] is False and '上限' in b3.note)
    check('the reason survives a restart', '上限' in tgbot.Bots(T3, ctx.db, ctx.log, s3, key_name=lambda k: None, autolisten=False).summary()['note'])

    # a bot the user deleted in BotFather: setup forgets it and starts clean (a network error does not)
    T4 = tempfile.mkdtemp(dir=TMP); f4 = Fake(); f4.groups = []; s4 = tg.Service(T4, ctx.db, ctx.log, adapter=f4)
    s4.set_config(api_id='1', api_hash='h'); open(T4 + '/tg.session', 'w').write('s')
    old = {'bot': {'username': 'exterior_dead_bot', 'token': '7000000009:' + 'D' * 35, 'bot_id': 7000000009, 'owner': f4.owner}}
    json.dump(old, open(T4 + '/tg_bots.json', 'w'))
    b4 = tgbot.Bots(T4, ctx.db, ctx.log, s4, key_name=lambda k: None, call=api.call, autolisten=False)
    def offline_group(*a): raise tg.TGError('network', '连不上 Telegram：TimeoutError，请检查代理')
    real_group, f4.create_group = f4.create_group, offline_group
    api.offline.add(old['bot']['token']); b4.set_config(setup=True); b4.setup_step()
    f4.create_group = real_group
    check('a network error does not make it forget the saved bot', b4._load()['bot']['username'] == 'exterior_dead_bot' and f4.created == 0)
    api.offline.clear(); api.dead.add(old['bot']['token']); b4.set_config(setup=True); b4.setup_step()
    nb = b4._load()['bot']
    check('a bot deleted in BotFather is forgotten; setup makes a new one and a new group', nb['username'] != 'exterior_dead_bot' and f4.created == 1
          and b4.ready() and len(f4.groups) == 1 and f4.groups[0][1] == nb['username'])

    # already set up, then the user deletes things and clicks again: every click re-checks
    T5 = tempfile.mkdtemp(dir=TMP); f5 = Fake(); f5.groups = []; s5 = tg.Service(T5, ctx.db, ctx.log, adapter=f5)
    s5.set_config(api_id='1', api_hash='h'); open(T5 + '/tg.session', 'w').write('s')
    b5 = tgbot.Bots(T5, ctx.db, ctx.log, s5, key_name=lambda k: None, call=api.call, autolisten=False)
    b5.set_config(setup=True); b5.setup_step(); first = b5._load()['bot']['token']
    b5.set_config(setup=True); b5.setup_step()
    check('clicking again when everything exists changes nothing', b5._load()['bot']['token'] == first and f5.created == 1 and len(f5.groups) == 1)
    api.gone_chats.add(b5._load()['group']['id']); b5.set_config(setup=True); b5.setup_step()
    check('only the group deleted: same bot, a new group', b5._load()['bot']['token'] == first and f5.created == 1 and len(f5.groups) == 2 and b5.ready())
    api.gone_chats.clear(); api.dead.add(first); b5.set_config(setup=True); b5.setup_step()
    check('the bot deleted after setup: a new bot and a new group on the next click', b5._load()['bot']['token'] != first and f5.created == 2 and len(f5.groups) == 3 and b5.ready())

    # deleted in Telegram while the page says ready: the periodic check notices, the page offers the setup again
    T6 = tempfile.mkdtemp(dir=TMP); f6 = Fake(); f6.groups = []; s6 = tg.Service(T6, ctx.db, ctx.log, adapter=f6)
    s6.set_config(api_id='1', api_hash='h'); open(T6 + '/tg.session', 'w').write('s')
    b6 = tgbot.Bots(T6, ctx.db, ctx.log, s6, key_name=lambda k: None, call=api.call, autolisten=False)
    b6.set_config(setup=True); b6.setup_step(); tok6 = b6._load()['bot']['token']
    api.offline.add(tok6); b6._forget_deleted_bot(); api.offline.discard(tok6)
    check('the periodic check keeps everything on a network error', b6.ready())
    api.dead.add(tok6); b6._forget_deleted_bot()
    check('the periodic check notices a deleted bot: no longer ready', not b6.ready() and not b6._load().get('bot'))
    check('and the page says why, instead of a stale "已建好"', '已被删除' in b6.note)
    ctx.BOTS.note = '已建好'; real_ready = ctx.BOTS.ready; ctx.BOTS.ready = lambda: False
    h = call('/settings', raw=True)[1]
    check('a stale "已建好" is never shown next to the setup button', '进度：已建好' not in h and '建立' in h)
    ctx.BOTS.ready = real_ready

    # ---------------- opening topics
    code, t1 = call('/api/bots', 'POST', {'purpose': '日报'})
    tid1 = int(t1['chat'].split(':')[1])
    check('POST /api/bots opens a topic "key name · purpose" in the group', code == 200 and api.topics[tid1]['name'] == 'Claude Code · 日报' and t1['name'] == 'Claude Code · 日报')
    check('it returns the chat id, a link and mine=true', t1['chat'] == f'tgbot:{tid1}' and t1['link'] == f'https://t.me/c/5550001/{tid1}' and t1['mine'] and t1['status'] == 'open')
    n = len(api.topics)
    code, again = call('/api/bots', 'POST', {'purpose': '日报'})
    check('same key + same purpose: the same topic, nothing new', again['chat'] == t1['chat'] and len(api.topics) == n)
    code, t2 = call('/api/bots', 'POST', {'purpose': '盯部署'})
    check('same key, another purpose: a second topic', t2['chat'] != t1['chat'] and len(api.topics) == n + 1)
    code, tb = call('/api/bots', 'POST', {'purpose': '日报'}, key=B)
    check('another key, same purpose: its own topic', tb['chat'] not in (t1['chat'], t2['chat']) and api.topics[int(tb['chat'][6:])]['name'] == 'pi · 日报')
    for bad in ('', '   ', '一' * 21):
        code, e = call('/api/bots', 'POST', {'purpose': bad})
        check(f'purpose must be 1-20 characters ({len(bad)})', code == 400 and e['error'] == 'bad_purpose')
    code, lst = call('/api/bots')
    check('GET /api/bots lists topics with holder, purpose and mine', {x['chat'] for x in lst if x['mine']} == {t1['chat'], t2['chat']}
          and next(x for x in lst if x['chat'] == tb['chat'])['holder'] == 'pi')

    # ---------------- sending: only the key that opened the topic
    code, e = call('/api/send', 'POST', {'chat': t1['chat'], 'text': 'hi'}, key=B)
    check('another key cannot write in my topic', code == 403 and e['error'] == 'not_holder')
    code, r = call('/api/send', 'POST', {'chat': t1['chat'], 'text': '日报写好了，要看吗？'})
    api_mid = 500 + len(api.sent)
    check('the opener sends at once into its topic, gets the stored id', code == 200 and r['status'] == 'sent' and api.sent[-1] == (GROUP, tid1, '日报写好了，要看吗？'))
    check('the bot only ever writes into the group', all(c == GROUP for c, _, _ in api.sent))
    code, ms = call('/api/messages?chat=' + q(t1['chat']))
    check('the message is stored in the topic chat, source telegram', ms[-1]['text'] == '日报写好了，要看吗？' and ms[-1]['source'] == 'telegram' and ms[-1]['sender'] == 'Claude Code · 日报')

    # ---------------- receiving: the user only, in known topics only
    api.push(fake.owner, '要，发我', thread=tid1); user_uid = api.uid
    api.push(999, '陌生人', thread=tid1)
    api.push(fake.owner, '私聊里发给 bot', chat_id=fake.owner)
    api.push(fake.owner, '群里的 General', thread=None)
    api.push(fake.owner, '我自己建的话题', thread=999)
    api.push(fake.owner, None, thread=tid1, forum_topic_reopened={})
    kept = ctx.BOTS.poll_once()
    code, inc = call(f"/api/messages?after_id={r['id']}")
    check("only the user's messages in known topics are kept", kept == 1 and [m['text'] for m in inc] == ['要，发我'] and inc[0]['sender'] == '我' and inc[0]['chat'] == t1['chat'])
    check('updates are not read twice', ctx.BOTS.poll_once() == 0)

    # ---------------- editing my own message (streaming) and reacting to the user's (seen)
    code, e = call('/api/edit', 'POST', {'chat': t1['chat'], 'id': r['id'], 'text': '日报写好了，要看吗？（改）'})
    check('the opener edits its own message in place', code == 200 and e['status'] == 'edited' and api.edited[-1] == (GROUP, api_mid, '日报写好了，要看吗？（改）'), str(e))
    code, ms = call('/api/messages?chat=' + q(t1['chat']))
    check('the stored text follows the edit', [m['text'] for m in ms if m['id'] == r['id']] == ['日报写好了，要看吗？（改）'])
    code, e = call('/api/edit', 'POST', {'chat': t1['chat'], 'id': r['id'], 'text': 'x'}, key=B)
    check('another key cannot edit in my topic', code == 403 and e['error'] == 'not_holder')
    code, e = call('/api/edit', 'POST', {'chat': t1['chat'], 'id': inc[0]['id'], 'text': 'x'})
    check("the user's messages cannot be edited", code == 404 and e['error'] == 'unknown_message')
    code, e = call('/api/edit', 'POST', {'chat': t1['chat'], 'id': 'abc', 'text': 'x'})
    check('a bad id is a clear error, not a crash', code == 404 and e['error'] == 'unknown_message')
    code, e = call('/api/react', 'POST', {'chat': t1['chat'], 'id': inc[0]['id'], 'emoji': '👀'})
    check("the opener puts 👀 on the user's message", code == 200 and api.reacted[-1] == (GROUP, 1000 + user_uid, [{'type': 'emoji', 'emoji': '👀'}]), str(e))
    code, e = call('/api/react', 'POST', {'chat': t1['chat'], 'id': r['id'], 'emoji': '👀'})
    check('it cannot react to its own message', code == 404 and e['error'] == 'unknown_message')
    code, e = call('/api/edit', 'POST', {'chat': 'tg:me', 'id': 1, 'text': 'x'})
    check('edit only works in bot topics', code == 404 and e['error'] == 'unknown_chat')

    # ---------------- waiting
    t0 = time.time(); code, got = call(f"/api/wait?chat={q(t1['chat'])}&after_id={r['id']}&timeout=5")
    check('wait returns at once when something is there', [m['text'] for m in got] == ['要，发我'] and time.time() - t0 < 2)
    top = got[-1]['id']
    t0 = time.time(); code, got = call(f"/api/wait?chat={q(t1['chat'])}&after_id={top}&timeout=1")
    check('wait times out with []', got == [] and 0.9 < time.time() - t0 < 4)
    def later():
        time.sleep(1.2); api.push(fake.owner, '标题再改一下', thread=tid1); ctx.BOTS.poll_once()
    threading.Thread(target=later).start()
    t0 = time.time(); code, got = call(f"/api/wait?chat={q(t1['chat'])}&after_id={top}&timeout=10")
    check('wait returns as soon as the user replies', [m['text'] for m in got] == ['标题再改一下'] and 1 < time.time() - t0 < 5, str(got))
    top = got[-1]['id']                                  # after the user's last reply
    code, own = call('/api/send', 'POST', {'chat': t1['chat'], 'text': '我（agent）又补充一句'})
    t0 = time.time(); code, got = call(f"/api/wait?chat={q(t1['chat'])}&after_id={top}&timeout=1")
    check("waiting in a topic: the agent's own message is not a reply", got == [] and time.time() - t0 >= 0.9, str(got))
    code, got = call(f"/api/wait?chat={q(t1['chat'])}&after_id={top}&timeout=1&all=1")
    check('&all=1 returns everything, the agent\'s own messages too', [m['text'] for m in got] == ['我（agent）又补充一句'])
    api.push(fake.owner, '用户的回复', thread=tid1); ctx.BOTS.poll_once()
    code, got = call(f"/api/wait?chat={q(t1['chat'])}&after_id={top}&timeout=5")
    check("and the user's reply after it comes through", [m['text'] for m in got] == ['用户的回复'] and got[0]['sender'] == '我')
    before = state()['topics'][str(tid1)]['last_activity']; clock[0] += 100
    call(f"/api/wait?chat={q(t1['chat'])}&timeout=1")
    check('waiting on your own topic counts as activity', state()['topics'][str(tid1)]['last_activity'] == int(clock[0]) > before)

    # ---------------- idle -> closed (never deleted), reopened by the user or the agent
    clock[0] += 71 * 3600; ctx.BOTS.sweep()
    check('nothing is closed before 72 hours', not any(t.get('closed') for t in state()['topics'].values()))
    clock[0] += 2 * 3600; n = ctx.BOTS.sweep()
    check('after 72 idle hours topics are closed', n == 3 and all(t['closed'] for t in state()['topics'].values()), str(n))
    check('closed topics get "✓ " in front of their title', api.topics[tid1]['name'] == '✓ Claude Code · 日报' and api.topics[tid1]['closed'])
    check('nothing was ever deleted', not any(m.startswith('delete') for m, _ in api.calls))
    code, lst = call('/api/bots')
    check('GET /api/bots shows them as closed', all(x['status'] == 'closed' for x in lst))
    api.push(fake.owner, '我回来了', thread=tid1); ctx.BOTS.poll_once()
    check('the user writing in a closed topic reopens it, title restored', not state()['topics'][str(tid1)]['closed'] and api.topics[tid1]['name'] == 'Claude Code · 日报' and not api.topics[tid1]['closed'])
    check('and it still belongs to the same key', call('/api/send', 'POST', {'chat': t1['chat'], 'text': '好的'})[0] == 200)
    tid2 = int(t2['chat'][6:])
    code, r2 = call('/api/send', 'POST', {'chat': t2['chat'], 'text': '部署完了'})
    check('the agent writing again reopens its closed topic too', code == 200 and not api.topics[tid2]['closed'] and api.topics[tid2]['name'] == 'Claude Code · 盯部署')
    clock[0] += 73 * 3600; ctx.BOTS.sweep()
    code, back = call('/api/bots', 'POST', {'purpose': '日报'})
    check('asking again for the same purpose reopens the old topic', back['chat'] == t1['chat'] and back['status'] == 'open' and not api.topics[tid1]['closed'])
    code, rel = call('/api/bots/release', 'POST', {'chat': t1['chat']}, key=B)
    check('only the opener can close a topic', code == 403 and rel['error'] == 'not_holder')
    code, rel = call('/api/bots/release', 'POST', {'chat': t1['chat']})
    check('release closes it now', code == 200 and api.topics[tid1]['closed'] and state()['topics'][str(tid1)]['closed'])

    # ---------------- topics the user creates himself (exterior adopts them)
    api.push(fake.owner, None, thread=300, forum_topic_created={'name': '先建的'}); ctx.BOTS.poll_once()
    check('without an adopter, topics the user creates are ignored', '300' not in state()['topics'])
    code, e = call('/api/bots/commands', 'POST', {'commands': [{'command': 'cc_cost', 'description': 'claude · 用量'}]})
    check('no menu before adopting', code == 403 and e['error'] == 'not_holder')
    code, ad = call('/api/bots/adopt', 'POST', {})
    check('a key adopts the user\'s topics', code == 200 and ad['status'] == 'adopting')
    api.push(fake.owner, None, thread=301, forum_topic_created={'name': '报销'}); ctx.BOTS.poll_once()
    t301 = state()['topics'].get('301') or {}
    check('a topic the user creates is taken by the adopter, titled as he named it', t301.get('holder') == A['id'] and t301.get('name') == '报销' and t301.get('user_created'), str(t301))
    api.push(fake.owner, '第一句', thread=302, reply_to_message={'forum_topic_created': {'name': 'soulrun'}}); ctx.BOTS.poll_once()
    code, ms = call('/api/messages?chat=tgbot:302')
    check('a first message in a topic created earlier also adopts it, and is kept', (state()['topics'].get('302') or {}).get('name') == 'soulrun' and [m['text'] for m in ms] == ['第一句'], str(ms))
    api.push(fake.owner, None, thread=301, forum_topic_edited={'name': '报销 11月'}); ctx.BOTS.poll_once()
    check('renaming the topic in Telegram renames it here', state()['topics']['301']['name'] == '报销 11月')
    code, lst = call('/api/bots')
    check('GET /api/bots marks them user_created and mine', any(x['chat'] == 'tgbot:301' and x['user_created'] and x['mine'] for x in lst))
    code, r301 = call('/api/send', 'POST', {'chat': 'tgbot:301', 'text': '收到'})
    check('the adopter writes in them', code == 200 and api.sent[-1] == (GROUP, 301, '收到'))
    code, e = call('/api/send', 'POST', {'chat': 'tgbot:301', 'text': 'x'}, key=B)
    check('other keys cannot', code == 403)
    clock[0] += 500 * 3600; ctx.BOTS.sweep()
    check('the user\'s own topics are never closed for being idle', not state()['topics']['301'].get('closed'))
    code, e = call('/api/bots/commands', 'POST', {'commands': [{'command': 'cc_cost', 'description': 'claude · 用量'}]}, key=B)
    check('only the adopter sets the menu', code == 403)
    code, e = call('/api/bots/commands', 'POST', {'commands': [{'command': 'cc_cost', 'description': 'claude · 用量'}, {'command': '/pi_compact', 'description': 'pi · 压缩'}]})
    check('the adopter sets the group\'s "/" menu', code == 200 and e['count'] == 2 and api.menus[-1]['scope'] == {'type': 'chat', 'chat_id': GROUP}
          and [c['command'] for c in api.menus[-1]['commands']] == ['cc_cost', 'pi_compact'], str(e))
    code, e = call('/api/bots/commands', 'POST', {'commands': [{'command': 'deep-research', 'description': 'x'}]})
    check('bad command names are refused', code == 400 and e['error'] == 'bad_commands')
    api.deleted_threads.add(302)
    code, e = call('/api/send', 'POST', {'chat': 'tgbot:302', 'text': '还在吗'})
    check('a topic deleted in Telegram answers 410 topic_deleted', code == 410 and e['error'] == 'topic_deleted', str(e))
    code, lst = call('/api/bots')
    check('and shows as deleted', any(x['chat'] == 'tgbot:302' and x['deleted'] for x in lst))

    # ---------------- secrets and hidden chats
    c = ctx.db()
    c.executemany('insert into tg_dialogs(chat,name,kind,hash,last_ts,username) values(?,?,?,?,?,?)',
                  [('tg:u93372553', 'BotFather', 'u', 1, int(time.time()), 'BotFather'),
                   ('tg:u7000000001', 'exterior', 'u', 1, int(time.time()), 'exterior_0a0a_bot'),
                   ('tg:g5550001', 'exterior', 'g', 1, int(time.time()), None)])
    c.execute("insert or replace into kv values('tg:dialogs_at', ?)", (str(time.time() + 3600),))
    c.execute("insert into msgs(chat,local_id,ts,type,sender,text,backfill) values('tg:u93372553',1,?,1,'BotFather',?,0)", (int(time.time()), 'Use this token: ' + token))
    c.commit(); c.close()
    code, chats = call('/api/chats?source=telegram')
    ids = {x['chat'] for x in chats}
    check('agents do not see the BotFather chat', 'tg:u93372553' not in ids)
    check("agents do not see the account's copies (bot chat, group)", not ({'tg:u7000000001', 'tg:g5550001'} & ids))
    check('the topics are listed under Telegram', {t1['chat'], t2['chat'], tb['chat']} <= ids)
    check('and not under WeChat', not any(x['chat'].startswith('tgbot:') for x in call('/api/chats?source=wechat')[1]))
    check('reading the BotFather chat is refused', call('/api/messages?chat=tg:u93372553')[0] == 403)
    check('sending to BotFather is refused', call('/api/send', 'POST', {'chat': 'tg:u93372553', 'text': '/deletebot'})[0] == 403)
    check('searching never returns it', call('/api/search?q=token')[1] == [])
    everything = ''.join(call(p, raw=True)[1] for p in ('/api/bots', '/api/status', '/api/chats', '/api/messages?limit=2000', '/api/wait?timeout=1', '/api/help'))
    check('the bot token never appears in an API response', token not in everything)

    # ---------------- pages
    h = call('/tg', raw=True)[1]
    check('Telegram tab: one folded "exterior" row, closed by default', 'id="botfold"' in h and '<div id="botlist" hidden>' in h)
    check('closed topics are folded once more under "已结束"', 'id="closedfold"' in h and '已结束 4 个' in h and '1 个进行中' in h and '<div id="closedlist" hidden>' in h)
    check('the folded rows are not <details> (would pause auto refresh)', '<details' not in h[h.index('id="botfold"'):h.index('id="closedlist"')])
    check('your own copies of the bot chat and group are not listed separately', '/chat?c=tg:u7000000001' not in h and '/chat?c=tg:g5550001' not in h)
    h = call('/chat?c=' + q(t2['chat']), raw=True)[1]
    check('topic page: who opened it, the purpose, open or closed; records what you read', '开的话题' in h and '盯部署' in h and '已结束' in h and 'wxb_seen_' in h)
    h = call('/settings', raw=True)[1]
    check('settings: ready, the bot, topic counts, idle timeout, a re-check button', '已建好' in h and '重新检查' in h and 'action="/bots/setup"' in h and '@exterior_0a0a_bot' in h and '进行中' in h and 'name="idle_hours"' in h)
    check('settings changes are page-only forms', form('/bots/config', idle_hours='48') == 303 and ctx.BOTS.config()['idle_hours'] == 48)
    ht = call('/api/help?for=telegram', key=None, raw=True)[1]
    check('help says: wait in the background, never block the foreground; gives a ready loop with the real address', '不要在前台阻塞' in ht and 'while :; do' in ht and base + '/api/wait?chat=tgbot:' in ht and '{base}' not in ht)
    import api as apimod
    check('the text copied to agents says so too', '不要在前台阻塞' in apimod.agent_line(base, 'K', 'telegram') and '不要在前台阻塞' in apimod.agent_line(base, 'K', 'all') and 'tgbot' not in apimod.agent_line(base, 'K', 'wechat'))
    check('help text explains topics (Telegram, not WeChat)', '/api/bots' in call('/api/help?for=telegram', key=None, raw=True)[1]
          and '/api/bots' not in call('/api/help?for=wechat', key=None, raw=True)[1])
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
        return live.create_bot('exterior', 'exterior_ab12_bot'), conv.sent
    except tg.TGError as e:
        return e, conv.sent
tok = '7123456789:' + 'A' * 35
r, sent = dialogue(['Alright, a new bot. How are we going to call it? Please choose a name for your bot.',
                    "Good. Now let's choose a username for your bot. It must end in `bot`.",
                    f'Done! Congratulations on your new bot. Use this token to access the HTTP API:\n{tok}\nKeep your token secure'])
check('BotFather: /newbot, name, username -> token', r == tok and sent == ['/newbot', 'exterior', 'exterior_ab12_bot'])
r, sent = dialogue(['Alright, a new bot. Please choose a name for your bot.', 'Now choose a username for your bot.',
                    'Sorry, this username is already taken. Please try something different.'])
check('BotFather: a refused username cancels the dialogue', isinstance(r, tg.TGError) and sent == ['/newbot', 'exterior', 'exterior_ab12_bot', '/cancel'])
r, sent = dialogue(['That I cannot do. You come to me asking for more than 20 bots. Please delete one of your old bots first.'])
check('BotFather: refusing /newbot stops at once, nothing else is sent', isinstance(r, tg.TGError) and sent == ['/newbot'] and '20 bots' in r.msg)
tgsrc = open(os.path.join(HERE, '..', 'content', 'tg.py')).read(); botsrc = open(os.path.join(HERE, '..', 'content', 'tgbot.py')).read()
said = set(re.findall(r"(?:say|send_message)\('([^']*)'\)", tgsrc[tgsrc.index('def create_bot'):tgsrc.index('def create_group')]))
check('the only fixed texts ever sent to BotFather are /newbot and /cancel', said == {'/newbot', '/cancel'}, str(said))
methods = set(re.findall(r"self\.call\([^,]+, '(\w+)'", botsrc)) | set(re.findall(r"_topic_call\('(\w+)'", botsrc))
check('Bot API methods used: send, edit own messages, react, read, rename, create/edit/close/reopen topics; nothing that deletes',
      methods == {'getMe', 'getChat', 'sendMessage', 'editMessageText', 'setMessageReaction', 'setMyCommands', 'setMyName', 'getUpdates', 'createForumTopic', 'editForumTopic', 'closeForumTopic', 'reopenForumTopic'}, str(methods))
check('the group is made with manage_topics only', 'ChatAdminRights(manage_topics=True)' in tgsrc)

print('ALL PASS' if ok else 'FAILURES')
sys.exit(0 if ok else 1)
