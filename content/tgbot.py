"""A pool of Telegram bots for talking to agents.

- The pool is created in the background through @BotFather from the user account (tg.Service.botfather_create:
  a fixed /newbot dialogue; nothing that edits, revokes or deletes bots exists in this code). It only starts after
  the user turns it on in the settings page; the size and the idle timeout are set there too, never by the API.
- An agent claims a bot for one task (POST /api/bots): the bot's display name becomes "<key name> · <purpose>".
  One key may hold several bots. Only the holding key can send through a bot.
- A bot that has been idle for `idle_hours` goes back to the pool (display name reset). If the owner writes to it
  before someone else claims it, it goes back to its previous holder.
- Each bot talks to its owner only: messages from anyone else are dropped. Tokens stay in VAR/tg_bots.json (0600)
  and are never returned by the API.

Chats are stored as `tgbot:<username>` in the same `msgs` table.
"""
import http.client, json, os, re, secrets, socket, ssl, threading, time

from tg import TGError

PREFIX = 'tgbot:'
API_HOST = 'api.telegram.org'
DEFAULT_CONFIG = {'size': 5, 'idle_hours': 72, 'fill': False}
PURPOSE_MAX = 20


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
        """tgs: tg.Service (creates bots through BotFather). key_name(key_id) -> name, or None if the key is gone.
        autolisten=False (tests): no background listener per bot; call poll_once yourself."""
        self.path = var + '/tg_bots.json'
        self.db, self.log, self.tgs, self.key_name, self.proxy, self.call, self.now = db, log, tgs, key_name, proxy, call, now
        self.lock = threading.RLock()
        self.listeners = {}
        self.fill_note = ''
        self.wake = threading.Event()
        self.autolisten = autolisten

    # ------------------------------------------------------------------ storage (tokens never leave this file)
    def _load(self):
        try:
            d = json.load(open(self.path))
        except Exception:
            d = {}
        d.setdefault('config', {}); d.setdefault('bots', {})
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

    def set_config(self, size=None, idle_hours=None, fill=None):
        """Settings page only (the API cannot change the pool)."""
        with self.lock:
            d = self._load()
            if size is not None:
                if not 0 <= int(size) <= 20:
                    raise TGError('bad_config', '上限要在 0 到 20 之间（Telegram 每个账号最多 20 个 bot）')
                d['config']['size'] = int(size)
            if idle_hours is not None:
                if not 1 <= int(idle_hours) <= 24 * 365:
                    raise TGError('bad_config', '闲置超时要在 1 小时到 1 年之间')
                d['config']['idle_hours'] = int(idle_hours)
            if fill is not None:
                d['config']['fill'] = bool(fill)
            self._save(d)
        self.wake.set()

    # ------------------------------------------------------------------ views
    def _public(self, u, b, key_id=None):
        return {'chat': PREFIX + u, 'username': u, 'link': f'https://t.me/{u}', 'name': b['name'],
                'status': 'busy' if b.get('holder') else 'free', 'holder': b.get('holder_name') if b.get('holder') else None,
                'purpose': b.get('purpose') if b.get('holder') else None, 'last_activity': b.get('last_activity'),
                'mine': bool(key_id and b.get('holder') == key_id)}

    def list(self, key_id=None):
        bots = self._load()['bots']
        return [self._public(u, b, key_id) for u, b in sorted(bots.items(), key=lambda x: (x[1].get('index') or 99, x[1]['created']))]

    def summary(self):
        d = self._load()
        return {'total': len(d['bots']), 'busy': sum(1 for b in d['bots'].values() if b.get('holder')), **d['config'],
                'note': self.fill_note}

    def user_side_chats(self):
        """The user account's own dialogs with the pool bots (duplicates of the tgbot: chats)."""
        return {f"tg:u{b['bot_id']}" for b in self._load()['bots'].values()}

    def get(self, chat):
        b = self._load()['bots'].get(chat[len(PREFIX):]) if is_bot(chat) else None
        return dict(b) if b else None

    # ------------------------------------------------------------------ creating
    def _new_username(self):
        for _ in range(8):
            u = f'exterior_{secrets.token_hex(2)}_bot'
            if self.tgs.username_free(u):
                return u
        raise TGError('botfather', '找不到可用的用户名，请稍后再试')

    def create(self, username=None):
        """Create one bot through BotFather (counts toward the limit). Returns its username."""
        with self.lock:
            d = self._load()
            if len(d['bots']) >= d['config']['size']:
                raise TGError('bot_limit', f"bot 数量已到上限（{d['config']['size']}）。请复用已有的 bot，或请用户在设置页调高上限。")
            used = {b.get('index') for b in d['bots'].values()}
            index = next(i for i in range(1, 100) if i not in used)
        if username:
            username = username.strip().lstrip('@')
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{1,28}[Bb][Oo][Tt]', username) or len(username) < 5:
                raise TGError('bad_username', '用户名要 5 到 32 个字母、数字或下划线，以字母开头，以 bot 结尾')
            if not self.tgs.username_free(username):
                raise TGError('bad_username', f'用户名 {username} 已经被占用')
        else:
            username = self._new_username()
        name = f'exterior {index}'
        token = self.tgs.botfather_create(name, username)
        owner = self.tgs.owner_id()
        with self.lock:
            d = self._load()
            d['bots'][username] = {'token': token, 'bot_id': int(token.split(':')[0]), 'owner': owner, 'index': index,
                                   'default_name': name, 'name': name, 'created': int(self.now()), 'offset': 0,
                                   'holder': None, 'last': None, 'last_activity': None, 'freed_at': int(self.now())}
            self._save(d)
        self._chat_row(username, name)
        try:
            self.tgs.start_bot(username)          # lets the bot message its owner
        except TGError as e:
            self.log('bots: created', username, 'but /start failed:', e.msg)
        self.log('bots: created', username)
        if self.autolisten:
            self.listen(username)
        return username

    def fill_step(self):
        """Create the next bot if the pool is switched on and below its size. Returns seconds to wait."""
        c = self.config()
        if not c['fill'] or len(self._load()['bots']) >= c['size']:
            self.fill_note = ''
            return 600
        if not self.tgs.configured():
            self.fill_note = '等待 Telegram 设置和登录'
            return 60
        try:
            u = self.create()
            self.fill_note = f'刚建好 {u}'
            return 30
        except TGError as e:
            text = e.msg
            m = re.search(r'(\d+)\s*seconds?', text)
            if m:
                self.fill_note = f'BotFather 要求等待 {m.group(1)} 秒'
                return int(m.group(1)) + 5
            if re.search(r'more than \d+ bots|20 bots', text):
                self.fill_note = 'Telegram 账号的 bot 总数已满，请在 BotFather 里删掉不用的 bot，或调低上限'
                self.set_config(fill=False)
                return 600
            if e.code == 'rate_limited' and e.retry_after:
                self.fill_note = f'Telegram 限流，{e.retry_after} 秒后继续'
                return e.retry_after + 5
            self.fill_note = '建 bot 出错：' + text[:120]
            return 600

    # ------------------------------------------------------------------ claiming and releasing
    def claim(self, key, purpose, username=None):
        """key: the API key record ({id, name}). Returns the bot."""
        purpose = re.sub(r'\s+', ' ', (purpose or '').strip())
        if not 1 <= len(purpose) <= PURPOSE_MAX:
            raise TGError('bad_purpose', f'purpose 要写这个 bot 的用途，1 到 {PURPOSE_MAX} 个字，例如“日报”“盯部署”')
        if username:
            username = self.create(username)
        with self.lock:
            d = self._load()
            if username:
                pick = username
            else:
                free = [(b.get('freed_at') or 0, u) for u, b in d['bots'].items() if not b.get('holder')]
                if not d['bots']:
                    raise TGError('pool_empty', 'bot 池还没有建好：需要用户在设置页“和 agent 对话的 bot”里开启。')
                if not free:
                    busy = [self._public(u, b) for u, b in d['bots'].items()]
                    raise TGError('pool_exhausted', f'bot 池（{len(busy)} 个）都在使用中。闲置 {d["config"]["idle_hours"]} 小时的会自动回收；'
                                  f'也可以请用户在设置页调高上限。正在用的：' +
                                  '；'.join(f'{x["name"]}（最后活动 {time.strftime("%m-%d %H:%M", time.localtime(x["last_activity"] or 0))}）' for x in busy))
                pick = min(free)[1]
            b = d['bots'][pick]
            name = f"{key['name']} · {purpose}"[:64]
            b.update(holder=key['id'], holder_name=key['name'], purpose=purpose, name=name,
                     claimed_at=int(self.now()), last_activity=int(self.now()))
            self._save(d)
        self._rename(pick, name)
        self._say(pick, f'—— 新任务：{name} ——')
        self.log('bots:', key['name'], 'claimed', pick, 'for', purpose)
        return self._public(pick, self._load()['bots'][pick], key['id'])

    def release(self, key, chat):
        with self.lock:
            d = self._load()
            b = d['bots'].get(chat[len(PREFIX):]) if is_bot(chat) else None
            if not b:
                raise TGError('unknown_chat', '没有这个 bot')
            if b.get('holder') != key['id']:
                raise TGError('not_holder', '这个 bot 不在你手上')
            self._free(b)
            self._save(d)
        self._rename(chat[len(PREFIX):], b['default_name'])
        return {'status': 'released', 'chat': chat}

    def _free(self, b):
        b['last'] = {'holder': b['holder'], 'holder_name': b.get('holder_name'), 'purpose': b.get('purpose'), 'name': b['name']}
        b.update(holder=None, holder_name=None, purpose=None, name=b['default_name'], freed_at=int(self.now()))

    def sweep(self):
        """Release bots that have been idle for longer than the configured timeout."""
        freed = []
        with self.lock:
            d = self._load()
            limit = self.now() - d['config']['idle_hours'] * 3600
            for u, b in d['bots'].items():
                if b.get('holder') and (b.get('last_activity') or 0) < limit:
                    self._free(b)
                    freed.append((u, b['default_name']))
            if freed:
                self._save(d)
        for u, name in freed:
            self._rename(u, name)
            self.log('bots: idle, back to the pool:', u)
        return len(freed)

    def touch(self, chat):
        with self.lock:
            d = self._load()
            b = d['bots'].get(chat[len(PREFIX):])
            if b and b.get('holder'):
                b['last_activity'] = int(self.now())
                self._save(d)

    # ------------------------------------------------------------------ talking
    def send(self, key, chat, text):
        b = self.get(chat)
        if not b:
            raise TGError('unknown_chat', '没有这个 bot。先 GET /api/bots 看有哪些。')
        if b.get('holder') != key['id']:
            raise TGError('not_holder', '这个 bot 不在你手上：先用 POST /api/bots 领取一个，或用你已经领到的那个。')
        text = (text or '').strip()
        if not text:
            raise TGError('empty', '内容是空的')
        if len(text) > 4096:
            raise TGError('too_long', 'Telegram 单条消息最多 4096 字')
        mid = self._say(chat[len(PREFIX):], text, raise_errors=True)
        self.touch(chat)
        return {'status': 'sent', 'chat': chat, 'id': mid}

    def _say(self, username, text, raise_errors=False):
        """The bot writes to its owner. Returns the stored row id."""
        b = self._load()['bots'][username]
        try:
            m = self.call(b['token'], 'sendMessage', {'chat_id': b['owner'], 'text': text}, self.proxy())
        except TGError as e:
            if raise_errors:
                raise
            self.log('bots:', username, 'send failed:', e.msg)
            return None
        return self._store(username, m['message_id'], m.get('date') or int(self.now()), b['name'], text)

    def _rename(self, username, name):
        b = self._load()['bots'].get(username)
        if b:
            try:
                self.call(b['token'], 'setMyName', {'name': name}, self.proxy())
            except TGError as e:
                self.log('bots:', username, 'rename failed:', e.msg)
        self._chat_row(username, name)

    def _chat_row(self, username, name):
        c = self.db()
        try:
            c.execute('insert into chats(chat, name, last_local) values(?,?,0) on conflict(chat) do update set name=excluded.name',
                      (PREFIX + username, name))
            c.commit()
        finally:
            c.close()

    def _store(self, username, local_id, ts, sender, text):
        c = self.db()
        try:
            cur = c.execute('insert or ignore into msgs(chat, local_id, ts, type, sender, text, backfill) values(?,?,?,1,?,?,0)',
                            (PREFIX + username, local_id, ts, sender, text))
            c.execute('update chats set last_local=max(last_local, ?) where chat=?', (local_id, PREFIX + username))
            c.commit()
            return cur.lastrowid
        finally:
            c.close()

    def poll_once(self, username, timeout=50):
        """One getUpdates round: keep the owner's messages, drop everyone else's. Returns how many were kept."""
        b = self._load()['bots'].get(username)
        if not b:
            return 0
        ups = self.call(b['token'], 'getUpdates', {'offset': b['offset'], 'timeout': timeout, 'allowed_updates': ['message']},
                        self.proxy(), timeout + 15)
        kept, offset = 0, b['offset']
        for up in ups:
            offset = max(offset, up['update_id'] + 1)
            m = up.get('message') or {}
            if not m or (m.get('from') or {}).get('id') != b['owner'] or (m.get('chat') or {}).get('type') != 'private':
                continue                                          # the bot only listens to its owner
            text = m.get('text') or m.get('caption') or ''
            if not m.get('text'):
                kind = next((k for k in ('photo', 'voice', 'video', 'document', 'sticker') if k in m), None)
                text = (text + ' ' if text else '') + {'photo': '[图片]', 'voice': '[语音]', 'video': '[视频]',
                                                       'document': '[文件]', 'sticker': '[贴纸]'}.get(kind, '[消息]')
            if text.strip() == '/start':
                continue
            self._owner_wrote(username)
            self._store(username, m['message_id'], m.get('date') or int(self.now()), '我', text)
            kept += 1
        with self.lock:
            d = self._load()
            if username in d['bots']:
                d['bots'][username]['offset'] = offset
                self._save(d)
        return kept

    def _owner_wrote(self, username):
        """The owner's message counts as activity; a freed bot goes back to its previous holder if nobody took it."""
        rename = None
        with self.lock:
            d = self._load()
            b = d['bots'][username]
            if not b.get('holder') and b.get('last') and self.key_name(b['last']['holder']) is not None:
                last = b['last']
                b.update(holder=last['holder'], holder_name=last['holder_name'], purpose=last['purpose'], name=last['name'],
                         last=None, claimed_at=int(self.now()))
                rename = last['name']
            if b.get('holder'):
                b['last_activity'] = int(self.now())
            self._save(d)
        if rename:
            self._rename(username, rename)
            self.log('bots:', username, 'back to its previous holder after the owner wrote to it')

    # ------------------------------------------------------------------ background
    def listen(self, username):
        t = self.listeners.get(username)
        if t and t.is_alive():
            return
        t = threading.Thread(target=self._listen, args=(username,), daemon=True)
        self.listeners[username] = t
        t.start()

    def _listen(self, username):
        delay = 5
        while username in self._load()['bots']:
            try:
                self.poll_once(username)
                delay = 5
            except Exception as e:
                self.log('bots:', username, getattr(e, 'msg', repr(e))[:160])
                time.sleep(delay)
                delay = min(delay * 2, 300)

    def start(self):
        for u in self._load()['bots']:
            self.listen(u)
        threading.Thread(target=self._housekeeping, daemon=True).start()

    def _housekeeping(self):
        while True:
            try:
                self.sweep()
                wait = self.fill_step()
            except Exception as e:
                self.log('bots: housekeeping', repr(e)[:200])
                wait = 300
            self.wake.wait(min(wait, 300))
            self.wake.clear()
