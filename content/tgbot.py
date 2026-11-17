"""Talking to agents through one Telegram bot and a private forum group.

- Setup (settings page only): the app makes one bot through @BotFather if there is none (fixed /newbot dialogue;
  nothing that edits, revokes or deletes bots exists in this code), then, from the user account, creates a private
  supergroup with topics, adds the bot and makes it an admin that may manage topics. Only the user and the bot are in it.
- An agent opens a topic for one task (POST /api/bots {purpose}): the topic is called "<key name> · <purpose>".
  The same key with the same purpose gets the same topic back. Only the key that opened a topic can write in it.
- A topic idle for `idle_hours` is closed and its title gets a "✓ " prefix. Nothing is ever deleted. When the user
  writes in it (or its agent writes again) it is reopened and keeps belonging to the same key.
- The bot keeps only the user's messages in the group's topics; everything else is dropped. The token stays in
  VAR/tg_bots.json (0600) and is never returned by the API.

Topics are stored as chats `tgbot:<topic id>` in the same `msgs` table.
"""
import http.client, json, os, re, secrets, socket, ssl, threading, time

from tg import TGError

PREFIX = 'tgbot:'
API_HOST = 'api.telegram.org'
DEFAULT_CONFIG = {'idle_hours': 72, 'setup': False, 'note': ''}
PURPOSE_MAX = 20
CLOSED = '✓ '
GROUP_TITLE = 'exterior'
BOT_NAME = 'exterior'
# There is only one bot: plain usernames first; a random suffix only if all of these are taken (usernames are global)
USERNAMES = ('exterior_bot', 'exterior_agent_bot', 'exterior_inbox_bot', 'exterior_desk_bot')


def is_bot(chat):
    return (chat or '').startswith(PREFIX)


class _Conn(http.client.HTTPSConnection):
    """HTTPS to the Bot API, through the configured SOCKS5/HTTP proxy if any."""
    def __init__(self, proxy, timeout):
        super().__init__(API_HOST, 443, timeout=timeout, context=ssl.create_default_context())
        self._proxy = proxy

    def connect(self):
        if not self._proxy:
            return super().connect()
        from python_socks.sync import Proxy
        raw = Proxy.from_url(self._proxy).connect(dest_host=API_HOST, dest_port=443, timeout=self.timeout)
        raw.settimeout(self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=API_HOST)


def bot_call(token, method, params=None, proxy='', timeout=30):
    """One Bot API call; returns `result` or raises TGError."""
    conn = _Conn(proxy, timeout)
    try:
        conn.request('POST', f'/bot{token}/{method}', json.dumps(params or {}).encode(), {'Content-Type': 'application/json'})
        d = json.loads(conn.getresponse().read() or b'{}')
    except (OSError, socket.timeout, ValueError) as e:
        raise TGError('network', f'连不上 Telegram Bot API：{type(e).__name__}，请检查代理')
    finally:
        conn.close()
    if not d.get('ok'):
        if d.get('error_code') == 429:
            raise TGError('rate_limited', 'Telegram 限流，请稍后再试', int((d.get('parameters') or {}).get('retry_after') or 30))
        raise TGError('telegram_error', str(d.get('description') or d)[:200])
    return d['result']


class Bots:
    def __init__(self, var, db, log, tgs, key_name, proxy=lambda: '', call=bot_call, now=time.time, autolisten=True):
        """tgs: tg.Service. key_name(key_id) -> name, or None if the key is gone.
        autolisten=False (tests): no background listener; call poll_once yourself."""
        self.path = var + '/tg_bots.json'
        self.db, self.log, self.tgs, self.key_name, self.proxy, self.call, self.now = db, log, tgs, key_name, proxy, call, now
        self.lock = threading.RLock()
        self.listener = None
        self.wake = threading.Event()
        self.autolisten = autolisten

    # ------------------------------------------------------------------ storage (the token never leaves this file)
    def _load(self):
        try:
            d = json.load(open(self.path))
        except Exception:
            d = {}
        if 'bots' in d:                                   # 0.9.x pool layout: keep its first bot, drop the rest of it
            if d['bots'] and not d.get('bot'):
                u, b = min(d['bots'].items(), key=lambda x: x[1].get('index') or 99)
                d['bot'] = {'username': u, 'token': b['token'], 'bot_id': b['bot_id'], 'owner': b['owner']}
            d.pop('bots')
            for k in ('size', 'fill', 'probe'):
                d.get('config', {}).pop(k, None)
        d.setdefault('config', {}); d.setdefault('topics', {}); d.setdefault('offset', 0)
        for k, v in DEFAULT_CONFIG.items():
            d['config'].setdefault(k, v)
        return d

    def _save(self, d):
        tmp = self.path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(d, f)
        os.replace(tmp, self.path)

    def config(self):
        return dict(self._load()['config'])

    @property
    def note(self):
        return self._load()['config'].get('note', '')

    @note.setter
    def note(self, v):
        with self.lock:
            d = self._load()
            if d['config'].get('note') != v:
                d['config']['note'] = v
                self._save(d)

    def set_config(self, idle_hours=None, setup=None):
        """Settings page only (the API cannot change these)."""
        with self.lock:
            d = self._load()
            if idle_hours is not None:
                if not 1 <= int(idle_hours) <= 24 * 365:
                    raise TGError('bad_config', '闲置超时要在 1 小时到 1 年之间')
                d['config']['idle_hours'] = int(idle_hours)
            if setup is not None:
                d['config']['setup'] = bool(setup)
                if setup:
                    d['config']['note'] = '正在建立…'
            self._save(d)
        self.wake.set()

    # ------------------------------------------------------------------ views
    def ready(self):
        d = self._load()
        return bool(d.get('bot') and d.get('group'))

    def _public(self, tid, t, key_id=None, d=None):
        d = d or self._load()
        g = d.get('group') or {}
        return {'chat': PREFIX + str(tid), 'name': t['name'], 'status': 'closed' if t.get('closed') else 'open',
                'holder': t.get('holder_name'), 'purpose': t.get('purpose'), 'last_activity': t.get('last_activity'),
                'mine': bool(key_id and t.get('holder') == key_id), 'link': f"https://t.me/c/{g.get('channel_id')}/{tid}"}

    def list(self, key_id=None):
        d = self._load()
        ts = sorted(d['topics'].items(), key=lambda x: (bool(x[1].get('closed')), -(x[1].get('last_activity') or 0)))
        return [self._public(tid, t, key_id, d) for tid, t in ts]

    def summary(self):
        d = self._load()
        return {'ready': bool(d.get('bot') and d.get('group')), 'bot': (d.get('bot') or {}).get('username'),
                'open': sum(1 for t in d['topics'].values() if not t.get('closed')),
                'closed': sum(1 for t in d['topics'].values() if t.get('closed')), **d['config']}

    def user_side_chats(self):
        """The user account's own copies of these conversations (the bot's private chat, the group): hidden from agents."""
        d = self._load()
        out = set()
        if d.get('bot'):
            out.add(f"tg:u{d['bot']['bot_id']}")
        if d.get('group'):
            out.add(f"tg:g{d['group']['channel_id']}")
        return out

    def get(self, chat):
        if not is_bot(chat):
            return None
        t = self._load()['topics'].get(chat[len(PREFIX):])
        return dict(t) if t else None

    # ------------------------------------------------------------------ setup
    def setup_step(self):
        """Run the setup once the user asked for it on the settings page. Returns seconds to wait."""
        c = self.config()
        if not c['setup']:
            return 600
        try:
            self._forget_deleted_bot()        # every click re-checks: the user may have deleted the bot or the group
        except TGError:
            pass
        if self.ready():
            self.set_config(setup=False); self.note = '已建好'
            return 600
        if not self.tgs.configured():
            self.note = '等待 Telegram 设置和登录'
            return 60
        try:
            d = self._load()
            if not d.get('bot'):
                self.note = '正在通过 BotFather 新建 bot…'
                username, token = self._create_bot()
                with self.lock:
                    d = self._load()
                    d['bot'] = {'username': username, 'token': token, 'bot_id': int(token.split(':')[0]), 'owner': self.tgs.owner_id()}
                    self._save(d)
                self.log('bots: created bot', username)
            b = self._load()['bot']
            try:
                self.call(b['token'], 'setMyName', {'name': BOT_NAME}, self.proxy())
            except TGError as e:
                self.log('bots: rename bot', e.msg)
            if not self._load().get('group'):
                self.note = '正在建对话群…'
                cid = self.tgs.create_group(GROUP_TITLE, '和 agent 对话的群：每个任务一个话题。', b['username'])
                with self.lock:
                    d = self._load()
                    d['group'] = {'channel_id': cid, 'id': int(f'-100{cid}')}
                    self._save(d)
                self.log('bots: created group', cid)
            self.set_config(setup=False)
            self.note = '已建好'
            self.listen()
            return 600
        except TGError as e:
            self.set_config(setup=False)
            text = e.msg
            if re.search(r'more than \d+ bots', text):
                self.note = 'Telegram 账号的 bot 总数已到上限（普通账号 20 个，Premium 40 个）。在 BotFather 里删掉一个不用的 bot 后再点“建立”'
            else:
                self.note = '建立失败：' + text[:160]
            self.log('bots: setup failed', text[:200])
            return 600

    def _forget_deleted_bot(self):
        """If Telegram says the saved bot no longer exists (the user deleted it in BotFather), forget it and everything
        made with it, so the setup starts clean. A network error is not a reason: only Telegram's explicit answer."""
        d = self._load()
        b, g = d.get('bot'), d.get('group')
        if not b:
            return
        gone = lambda e: e.code == 'telegram_error' and re.search(r'unauthorized|not found|kicked|forbidden', e.msg, re.I)
        try:
            self.call(b['token'], 'getMe', {}, self.proxy())
        except TGError as e:
            if gone(e):
                self._forget('bot', 'group')
                self.note = f"bot @{b['username']} 已被删除。点“建立”会新建一个 bot 和群"
                self.log('bots: saved bot', b['username'], 'no longer exists; starting clean')
            return
        if g:
            try:
                self.call(b['token'], 'getChat', {'chat_id': g['id']}, self.proxy())
            except TGError as e:
                if gone(e):
                    self._forget('group')
                    self.note = f"对话群已被删除。点“建立”会用 @{b['username']} 新建一个群；想连 bot 也换掉，先在 BotFather 里删掉它"
                    self.log('bots: the group is gone; a new one will be made')

    def _forget(self, *what):
        with self.lock:
            d = self._load()
            for k in what:
                d.pop(k, None)
            d['topics'] = {}
            if 'bot' in what:
                d['offset'] = 0
            self._save(d)

    def _create_bot(self):
        """Make the bot through BotFather: the plain usernames first, then a random suffix. A username BotFather refuses
        (taken or reserved) moves on to the next one; any other refusal stops."""
        candidates = list(USERNAMES) + [f'exterior_{secrets.token_hex(2)}_bot' for _ in range(4)]
        for u in candidates:
            if not self.tgs.username_free(u):
                continue
            try:
                return u, self.tgs.botfather_create(BOT_NAME, u)
            except TGError as e:
                if '没有接受这个用户名' in e.msg:
                    continue
                raise
        raise TGError('botfather', '找不到可用的用户名，请稍后再试')

    # ------------------------------------------------------------------ topics
    def _need_ready(self):
        if not self.ready():
            raise TGError('not_ready', '对话群还没有建好：需要用户在设置页“和 agent 对话的 bot”里点“建立”。')

    def claim(self, key, purpose):
        """key: the API key record ({id, name}). Same key + same purpose -> the same topic, reopened if needed."""
        purpose = re.sub(r'\s+', ' ', (purpose or '').strip())
        if not 1 <= len(purpose) <= PURPOSE_MAX:
            raise TGError('bad_purpose', f'purpose 要写这个话题的用途，1 到 {PURPOSE_MAX} 个字，例如“日报”“盯部署”')
        self._need_ready()
        d = self._load()
        for tid, t in d['topics'].items():
            if t.get('holder') == key['id'] and t.get('purpose') == purpose:
                if t.get('closed'):
                    self._reopen(tid)
                self.touch(PREFIX + tid)
                return self._public(tid, self._load()['topics'][tid], key['id'])
        name = f"{key['name']} · {purpose}"[:120]
        b, g = d['bot'], d['group']
        r = self.call(b['token'], 'createForumTopic', {'chat_id': g['id'], 'name': name}, self.proxy())
        tid = str(r['message_thread_id'])
        with self.lock:
            d = self._load()
            d['topics'][tid] = {'name': name, 'holder': key['id'], 'holder_name': key['name'], 'purpose': purpose,
                                'created': int(self.now()), 'last_activity': int(self.now()), 'closed': False}
            self._save(d)
        self._chat_row(tid, name)
        self.log('bots:', key['name'], 'opened topic', tid, 'for', purpose)
        return self._public(tid, self._load()['topics'][tid], key['id'])

    def release(self, key, chat):
        t = self.get(chat)
        if not t:
            raise TGError('unknown_chat', '没有这个话题')
        if t.get('holder') != key['id']:
            raise TGError('not_holder', '这个话题不是你开的')
        if not t.get('closed'):
            self._close(chat[len(PREFIX):])
        return {'status': 'closed', 'chat': chat}

    def _topic_call(self, method, tid, **extra):
        d = self._load()
        try:
            self.call(d['bot']['token'], method, {'chat_id': d['group']['id'], 'message_thread_id': int(tid), **extra}, self.proxy())
        except TGError as e:
            if 'NOT_MODIFIED' not in e.msg.upper():
                self.log('bots:', method, tid, e.msg)

    def _close(self, tid):
        with self.lock:
            d = self._load()
            t = d['topics'][tid]
            t['closed'] = True
            self._save(d)
        self._topic_call('editForumTopic', tid, name=(CLOSED + t['name'])[:128])
        self._topic_call('closeForumTopic', tid)
        self._chat_row(tid, CLOSED + t['name'])

    def _reopen(self, tid):
        with self.lock:
            d = self._load()
            t = d['topics'][tid]
            t['closed'] = False
            t['last_activity'] = int(self.now())
            self._save(d)
        self._topic_call('reopenForumTopic', tid)
        self._topic_call('editForumTopic', tid, name=t['name'])
        self._chat_row(tid, t['name'])

    def sweep(self):
        """Close topics that have been idle for longer than the configured timeout."""
        d = self._load()
        limit = self.now() - d['config']['idle_hours'] * 3600
        idle = [tid for tid, t in d['topics'].items() if not t.get('closed') and (t.get('last_activity') or 0) < limit]
        for tid in idle:
            self._close(tid)
            self.log('bots: topic idle, closed:', tid)
        return len(idle)

    def touch(self, chat):
        with self.lock:
            d = self._load()
            t = d['topics'].get(chat[len(PREFIX):])
            if t:
                t['last_activity'] = int(self.now())
                self._save(d)

    # ------------------------------------------------------------------ talking
    def send(self, key, chat, text):
        t = self.get(chat)
        if not t:
            raise TGError('unknown_chat', '没有这个话题。先 GET /api/bots 看有哪些，或用 POST /api/bots 开一个。')
        if t.get('holder') != key['id']:
            raise TGError('not_holder', '这个话题不是你开的：用 POST /api/bots 开你自己的话题。')
        text = (text or '').strip()
        if not text:
            raise TGError('empty', '内容是空的')
        if len(text) > 4096:
            raise TGError('too_long', 'Telegram 单条消息最多 4096 字')
        tid = chat[len(PREFIX):]
        if t.get('closed'):
            self._reopen(tid)
        d = self._load()
        m = self.call(d['bot']['token'], 'sendMessage', {'chat_id': d['group']['id'], 'message_thread_id': int(tid), 'text': text},
                      self.proxy())
        mid = self._store(tid, m['message_id'], m.get('date') or int(self.now()), t['name'], text)
        self.touch(chat)
        return {'status': 'sent', 'chat': chat, 'id': mid}

    def _target(self, key, chat, mid, user):
        """A stored message in the key's own topic: the user's (user=True) or the bot's. Returns (tid, local_id)."""
        t = self.get(chat)
        if not t:
            raise TGError('unknown_chat', '没有这个话题')
        if t.get('holder') != key['id']:
            raise TGError('not_holder', '这个话题不是你开的')
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            raise TGError('unknown_message', 'id 要写发送时返回的那个数字')
        c = self.db()
        try:
            row = c.execute('select local_id, sender from msgs where id=? and chat=?', (mid, chat)).fetchone()
        finally:
            c.close()
        if not row or (row[1] == '我') != user:
            raise TGError('unknown_message', '这个话题里没有这条消息' + ('（只能给我发的消息加表情）' if user else '（只能改你自己发的消息）'))
        return chat[len(PREFIX):], row[0]

    def edit(self, key, chat, mid, text):
        """Replace the text of a message the bot sent (for streaming a reply as it is written)."""
        tid, local = self._target(key, chat, mid, user=False)
        text = (text or '').strip()
        if not text:
            raise TGError('empty', '内容是空的')
        if len(text) > 4096:
            raise TGError('too_long', 'Telegram 单条消息最多 4096 字')
        d = self._load()
        try:
            self.call(d['bot']['token'], 'editMessageText', {'chat_id': d['group']['id'], 'message_id': local, 'text': text}, self.proxy())
        except TGError as e:
            if 'NOT MODIFIED' not in e.msg.upper().replace('_', ' '):
                raise
        c = self.db()
        try:
            c.execute('update msgs set text=? where id=?', (text, int(mid)))
            c.commit()
        finally:
            c.close()
        self.touch(chat)
        return {'status': 'edited', 'chat': chat, 'id': int(mid)}

    def react(self, key, chat, mid, emoji='👀'):
        """Put one emoji reaction on a message of the user (e.g. 👀 = seen). An empty emoji removes it."""
        tid, local = self._target(key, chat, mid, user=True)
        emoji = (emoji or '').strip()
        d = self._load()
        self.call(d['bot']['token'], 'setMessageReaction', {'chat_id': d['group']['id'], 'message_id': local,
                  'reaction': [{'type': 'emoji', 'emoji': emoji}] if emoji else []}, self.proxy())
        self.touch(chat)
        return {'status': 'reacted', 'chat': chat, 'id': int(mid), 'emoji': emoji}

    def _chat_row(self, tid, name):
        c = self.db()
        try:
            c.execute('insert into chats(chat, name, last_local) values(?,?,0) on conflict(chat) do update set name=excluded.name',
                      (PREFIX + str(tid), name))
            c.commit()
        finally:
            c.close()

    def _store(self, tid, local_id, ts, sender, text):
        c = self.db()
        try:
            cur = c.execute('insert or ignore into msgs(chat, local_id, ts, type, sender, text, backfill) values(?,?,?,1,?,?,0)',
                            (PREFIX + str(tid), local_id, ts, sender, text))
            c.execute('update chats set last_local=max(last_local, ?) where chat=?', (local_id, PREFIX + str(tid)))
            c.commit()
            return cur.lastrowid
        finally:
            c.close()

    def poll_once(self, timeout=50):
        """One getUpdates round: keep the user's messages in known topics of the group, drop everything else."""
        d = self._load()
        if not d.get('bot'):
            return 0
        b, g = d['bot'], d.get('group') or {}
        ups = self.call(b['token'], 'getUpdates', {'offset': d['offset'], 'timeout': timeout, 'allowed_updates': ['message']},
                        self.proxy(), timeout + 15)
        kept, offset = 0, d['offset']
        for up in ups:
            offset = max(offset, up['update_id'] + 1)
            m = up.get('message') or {}
            tid = str(m.get('message_thread_id') or '')
            if (not m or (m.get('from') or {}).get('id') != b['owner'] or (m.get('chat') or {}).get('id') != g.get('id')
                    or not m.get('is_topic_message') or tid not in d['topics']):
                continue                                          # only the user, only in the group's known topics
            kind = next((k for k in ('photo', 'voice', 'video', 'document', 'sticker') if k in m), None)
            text = m.get('text') or m.get('caption') or ''
            if not text and not kind:
                continue                                          # service messages (topic created / closed ...)
            if kind and not m.get('text'):
                text = (text + ' ' if text else '') + {'photo': '[图片]', 'voice': '[语音]', 'video': '[视频]',
                                                       'document': '[文件]', 'sticker': '[贴纸]'}[kind]
            if self._load()['topics'][tid].get('closed'):
                self._reopen(tid)
            self._store(tid, m['message_id'], m.get('date') or int(self.now()), '我', text)
            self.touch(PREFIX + tid)
            kept += 1
        with self.lock:
            d = self._load()
            d['offset'] = offset
            self._save(d)
        return kept

    # ------------------------------------------------------------------ background
    def listen(self):
        if not self.autolisten or (self.listener and self.listener.is_alive()):
            return
        self.listener = threading.Thread(target=self._listen, daemon=True)
        self.listener.start()

    def _listen(self):
        delay = 5
        while True:
            try:
                self.poll_once()
                delay = 5
            except Exception as e:
                self.log('bots: listen', getattr(e, 'msg', repr(e))[:160])
                time.sleep(delay)
                delay = min(delay * 2, 300)

    def start(self):
        if self._load().get('bot'):
            self.listen()
        threading.Thread(target=self._housekeeping, daemon=True).start()

    def _housekeeping(self):
        while True:
            try:
                self.sweep()
                if self._load().get('bot'):
                    self._forget_deleted_bot()    # deleted in Telegram? then the page offers the setup again
                wait = self.setup_step()
            except Exception as e:
                self.log('bots: housekeeping', repr(e)[:200])
                wait = 300
            self.wake.wait(min(wait, 300))
            self.wake.clear()
