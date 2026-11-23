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
import base64, http.client, json, mimetypes, os, re, secrets, socket, ssl, threading, time, uuid

from tg import TGError
import tgmd

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
    """HTTPS to the Bot API, through one SOCKS5/HTTP proxy if given."""
    def __init__(self, proxy, timeout):
        super().__init__(API_HOST, 443, timeout=timeout, context=ssl.create_default_context())
        self._proxy = proxy

    def connect(self):
        if not self._proxy:
            return super().connect()
        from python_socks.sync import Proxy
        t = min(self.timeout, CONNECT_TIMEOUT)    # a dead proxy must not hold a call for the whole read timeout
        raw = Proxy.from_url(self._proxy).connect(dest_host=API_HOST, dest_port=443, timeout=t)
        raw.settimeout(t)
        self.sock = self._context.wrap_socket(raw, server_hostname=API_HOST)
        self.sock.settimeout(self.timeout)


CONNECT_TIMEOUT = 8      # proxy handshake + TLS; the healthy proxies answer in about a second
COOL_MIN, COOL_MAX = 30, 600
PROBE_EVERY = 15
# Methods that are safe to send twice: a call that broke after the request went out is repeated through the
# next proxy right away. Anything else (send*, edit*, createForumTopic…) is not repeated once sent, since
# Telegram may already have done it; the caller sees a network error and decides.
SAFE_RETRY = re.compile(r'^(get\w*|setMyName|setMyCommands|setMyDescription|setChatMenuButton)$')

_HLOCK = threading.Lock()
_HEALTH = {}     # proxy -> {'fails', 'until', 'err', 'ok_at', 'ms'}
_LOG = [None]


def proxies(setting):
    """The proxy setting may list several proxies (comma or space separated); '' means a direct connection.
    The order is the preference: the first one that works is used."""
    return [p for p in re.split(r'[\s,]+', setting or '') if p] or ['']


def label(p):
    """A proxy for logs and pages: without user:password."""
    return re.sub(r'//[^@/]*@', '//', p) if p else '直连'


def _bad(p):
    h = _HEALTH.get(p)
    return bool(h and h['fails'])


def _order(setting):
    """Proxies in the order to try: the working ones in the listed order, then the failing ones,
    the one whose pause ends first leading (all failing still means: try them all)."""
    ps = proxies(setting)
    with _HLOCK:
        good = [p for p in ps if not _bad(p)]
        bad = sorted((p for p in ps if _bad(p)), key=lambda p: _HEALTH[p]['until'])
    return good + bad


def _mark_ok(p, ms=None):
    with _HLOCK:
        h = _HEALTH.setdefault(p, {'fails': 0, 'until': 0, 'err': '', 'ok_at': 0, 'ms': None})
        was_bad = h['fails'] > 0
        h.update(fails=0, until=0, err='', ok_at=time.time())
        if ms is not None:
            h['ms'] = ms
    if was_bad and _LOG[0]:
        _LOG[0]('telegram proxy', f'{label(p)} 恢复')


def _mark_bad(p, e):
    with _HLOCK:
        h = _HEALTH.setdefault(p, {'fails': 0, 'until': 0, 'err': '', 'ok_at': 0, 'ms': None})
        h['fails'] += 1
        pause = min(COOL_MAX, COOL_MIN * 2 ** min(h['fails'] - 1, 10))
        h['until'], h['err'] = time.time() + pause, type(e).__name__
        first = h['fails'] == 1
    if first and _LOG[0]:
        _LOG[0]('telegram proxy', f'{label(p)} 不可用（{type(e).__name__}），先用其他代理')


def health(setting):
    """[{proxy (no password), state ok|bad|unknown, err, ms, until}] in the listed order, for the settings page."""
    now = time.time()
    out = []
    with _HLOCK:
        for p in proxies(setting):
            h = _HEALTH.get(p)
            st = 'unknown' if not h else ('bad' if h['fails'] else 'ok')
            out.append({'proxy': label(p), 'state': st, 'err': (h or {}).get('err', ''), 'ms': (h or {}).get('ms'),
                        'retry_in': max(0, int(h['until'] - now)) if st == 'bad' else 0})
    return out


def _open(setting, timeout, tried=None):
    """A connected _Conn and its proxy: the first in _order, not yet in `tried`, that connects (proxy handshake
    + TLS). Every proxy attempted is appended to `tried`."""
    tried = [] if tried is None else tried
    last = None
    for p in _order(setting):
        if p in tried:
            continue
        tried.append(p)
        conn = _Conn(p, timeout)
        t = time.monotonic()
        try:
            conn.connect()
        except Exception as e:      # OSError, timeouts, python_socks and ssl errors: try the next proxy
            conn.close()
            _mark_bad(p, e)
            last = e
            continue
        _mark_ok(p, int((time.monotonic() - t) * 1000))
        return conn, p
    raise _net_error(last or OSError('no proxy left'))


def _request(setting, timeout, verb, path, body=None, headers=None, safe=False):
    """(status, body bytes). Every proxy is tried at most once: a failure before the request is fully sent
    moves on to the next proxy; after it was sent, only when `safe`."""
    tried, last = [], None
    while len(tried) < len(proxies(setting)):
        conn, p = _open(setting, timeout, tried)
        sent = False
        try:
            conn.request(verb, path, body, headers or {})
            sent = True
            r = conn.getresponse()
            return r.status, r.read()
        except (OSError, http.client.HTTPException) as e:
            _mark_bad(p, e)
            last = e
            if sent and not safe:
                raise _net_error(e)
        finally:
            conn.close()
    raise _net_error(last or OSError('no proxy left'))


def _probe_once(setting):
    """Check the failing proxies whose pause is over (handshake + TLS only), so a recovered preferred proxy
    is used again without a real call paying for the check."""
    now = time.time()
    with _HLOCK:
        due = [p for p in proxies(setting) if _bad(p) and _HEALTH[p]['until'] <= now]
    for p in due:
        conn = _Conn(p, CONNECT_TIMEOUT)
        t = time.monotonic()
        try:
            conn.connect()
            _mark_ok(p, int((time.monotonic() - t) * 1000))
        except Exception as e:
            _mark_bad(p, e)
        finally:
            conn.close()


_PROBER = []


def start_prober(get_setting, log=None):
    """One background thread per process that keeps the proxy health fresh."""
    if log:
        _LOG[0] = log
    if _PROBER:
        return
    def run():
        while True:
            time.sleep(PROBE_EVERY)
            try:
                _probe_once(get_setting())
            except Exception:
                pass
    t = threading.Thread(target=run, name='tg-proxy-probe', daemon=True)
    _PROBER.append(t)
    t.start()


def _net_error(e):
    return TGError('network', f'连不上 Telegram Bot API：{type(e).__name__}，请检查代理')


def _json(data):
    try:
        return json.loads(data or b'{}')
    except ValueError as e:
        raise _net_error(e)


def bot_call(token, method, params=None, proxy='', timeout=30):
    """One Bot API call; returns `result` or raises TGError."""
    _, data = _request(proxy, timeout, 'POST', f'/bot{token}/{method}', json.dumps(params or {}).encode(),
                       {'Content-Type': 'application/json'}, safe=bool(SAFE_RETRY.match(method)))
    d = _json(data)
    if not d.get('ok'):
        if d.get('error_code') == 429:
            raise TGError('rate_limited', 'Telegram 限流，请稍后再试', int((d.get('parameters') or {}).get('retry_after') or 30))
        raise TGError('telegram_error', str(d.get('description') or d)[:200])
    return d['result']


def bot_get_file(token, path, proxy='', timeout=120):
    """Download a file the Bot API points to (getFile → file_path)."""
    status, data = _request(proxy, timeout, 'GET', f'/file/bot{token}/{path}', safe=True)
    if status != 200:
        raise TGError('not_available', f'Telegram 没给文件（{status}）')
    return data


def bot_upload(token, method, fields, field, name, data, proxy='', timeout=120):
    """sendPhoto / sendDocument with the file in the request (multipart)."""
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + str(v).encode() + b'\r\n')
    mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    safe = name.replace('"', '')
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{safe}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + data + b'\r\n')
    body = b''.join(parts) + f'--{b}--\r\n'.encode()
    _, d = _request(proxy, timeout, 'POST', f'/bot{token}/{method}', body,
                    {'Content-Type': f'multipart/form-data; boundary={b}'})
    d = _json(d)
    if not d.get('ok'):
        raise TGError('telegram_error', str(d.get('description') or d)[:200])
    return d['result']


MEDIA_MAX = 20 * 1024 * 1024      # the Bot API's limit for downloads; uploads use the same cap here


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
        self.media_dir = var + '/tgbot_media'
        self.get_file, self.upload = bot_get_file, bot_upload

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
                'mine': bool(key_id and t.get('holder') == key_id), 'link': f"https://t.me/c/{g.get('channel_id')}/{tid}",
                'user_created': bool(t.get('user_created')), 'deleted': bool(t.get('deleted'))}

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
        idle = [tid for tid, t in d['topics'].items()
                if not t.get('closed') and not t.get('user_created') and (t.get('last_activity') or 0) < limit]
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

    # ------------------------------------------------------------------ topics the user creates
    def adopt(self, key):
        """The calling key takes the topics the user creates in the group himself (exterior). One adopter at a time."""
        self._need_ready()
        with self.lock:
            d = self._load()
            d['config']['adopter'] = {'id': key['id'], 'name': key['name']}
            self._save(d)
        self.log('bots: adopter is now', key['name'])
        return {'status': 'adopting', 'holder': key['name']}

    def user_topic(self, key, name, pin=False):
        """The adopter asks for a topic made by the user himself (Telegram only lets the user pin topics, and only
        his topics are adopted): reuse his live topic with that title, else create one as him; pin it only if asked (pin: true)."""
        self._need_ready()
        d = self._load()
        a = d['config'].get('adopter')
        if not a or a['id'] != key['id']:
            raise TGError('not_holder', '只有接管用户话题的 key（POST /api/bots/adopt）能这样建话题')
        name = (name or '').strip()[:120]
        if not name:
            raise TGError('bad_name', 'name 不能为空')
        tid = next((t for t, v in d['topics'].items() if v.get('user_created') and not v.get('deleted') and v.get('name') == name), None)
        if tid is None:
            tid = str(self.tgs.create_topic(d['group']['channel_id'], name))
            self._adopt_topic(tid, name)
            self.log('bots: created topic as the user', tid, name)
        if pin:
            self.tgs.pin_topic(d['group']['channel_id'], int(tid))
        return self._public(tid, self._load()['topics'][tid], key['id'])

    def rename(self, key, chat, name):
        """The adopter renames one of the user's topics (as the user, so it works whatever the bot may do)."""
        self._need_ready()
        d = self._load()
        a = d['config'].get('adopter')
        if not a or a['id'] != key['id']:
            raise TGError('not_holder', '只有接管用户话题的 key（POST /api/bots/adopt）能给话题改名')
        name = (name or '').strip()[:120]
        if not name:
            raise TGError('bad_name', 'name 不能为空')
        tid = chat[len(PREFIX):] if (chat or '').startswith(PREFIX) else ''
        t = d['topics'].get(tid)
        if not t or not t.get('user_created') or t.get('deleted'):
            raise TGError('unknown_chat', '没有这个话题，或它不是我自己建的话题')
        self.tgs.rename_topic(d['group']['channel_id'], int(tid), name)
        self._rename_topic(tid, name)
        self.log('bots: renamed topic', tid, t.get('name'), '->', name)
        return self._public(tid, self._load()['topics'][tid], key['id'])

    def _adopt_topic(self, tid, name):
        d = self._load()
        a = d['config'].get('adopter')
        if not a or tid in d['topics']:
            return False
        name = (name or f'话题 {tid}')[:120]
        with self.lock:
            d = self._load()
            d['topics'][tid] = {'name': name, 'holder': a['id'], 'holder_name': a['name'], 'purpose': name[:PURPOSE_MAX],
                                'created': int(self.now()), 'last_activity': int(self.now()), 'closed': False,
                                'user_created': True}
            self._save(d)
        self._chat_row(tid, name)
        self.log('bots: user created topic', tid, name)
        return True

    def _rename_topic(self, tid, name):
        with self.lock:
            d = self._load()
            t = d['topics'].get(tid)
            if not t or not t.get('user_created') or not name:
                return
            t['name'], t['purpose'] = name[:120], name[:PURPOSE_MAX]
            self._save(d)
        self._chat_row(tid, name[:120])

    def _mark_deleted(self, tid):
        with self.lock:
            d = self._load()
            if tid in d['topics']:
                d['topics'][tid]['deleted'] = True
                d['topics'][tid]['closed'] = True
                self._save(d)

    def commands(self, key, commands):
        """The adopter sets the "/" menu of the group: [{command, description}]. An empty list clears it."""
        a = self._load()['config'].get('adopter') or {}
        if a.get('id') != key['id']:
            raise TGError('not_holder', '只有接管用户话题的 key（POST /api/bots/adopt）能设置命令菜单')
        if not isinstance(commands, list) or len(commands) > 100:
            raise TGError('bad_commands', 'commands 要是列表，最多 100 条')
        clean = []
        for c in commands:
            name, desc = str((c or {}).get('command', '')).lstrip('/'), str((c or {}).get('description', '')).strip()
            if not re.fullmatch(r'[a-z0-9_]{1,32}', name) or not 1 <= len(desc) <= 256:
                raise TGError('bad_commands', f'命令名只能是 1-32 个小写字母、数字、下划线，说明 1-256 字：{name[:40]}')
            clean.append({'command': name, 'description': desc})
        d = self._load()
        self.call(d['bot']['token'], 'setMyCommands', {'commands': clean, 'scope': {'type': 'chat', 'chat_id': d['group']['id']}},
                  self.proxy())
        return {'status': 'set', 'count': len(clean)}

    # ------------------------------------------------------------------ talking
    @staticmethod
    def _keyboard(buttons):
        """buttons: rows of {text, data} (or plain strings, text = data). Pressing one comes back as the user's message `data`."""
        if not buttons:
            return None
        if not isinstance(buttons, list) or not all(isinstance(r, list) for r in buttons):
            raise TGError('bad_buttons', 'buttons 要写成按行的列表：[[{"text": "显示的字", "data": "按下后当作用户发的话"}], ...]')
        rows = []
        for r in buttons:
            row = []
            for b in r:
                if isinstance(b, str):
                    b = {'text': b, 'data': b}
                text, data = str(b.get('text') or b.get('data') or '').strip(), str(b.get('data') or b.get('text') or '').strip()
                if not text or not data:
                    raise TGError('bad_buttons', '每个按钮要有 text 或 data')
                if len(data.encode()) > 64:
                    raise TGError('bad_buttons', f'按钮的 data 最多 64 字节：{data[:30]}')
                row.append({'text': text, 'callback_data': data})
            if row:
                rows.append(row)
        if len(rows) > 50:
            raise TGError('bad_buttons', '按钮太多')
        return {'inline_keyboard': rows} if rows else None

    def send(self, key, chat, text, buttons=None):
        t = self.get(chat)
        if not t:
            raise TGError('unknown_chat', '没有这个话题。先 GET /api/bots 看有哪些，或用 POST /api/bots 开一个。')
        if t.get('holder') != key['id']:
            raise TGError('not_holder', '这个话题不是你开的：用 POST /api/bots 开你自己的话题。')
        text = (text or '').strip()
        if not text:
            raise TGError('empty', '内容是空的')
        if len(text) > tgmd.RICH_MAX:
            raise TGError('too_long', f'Telegram 单条消息最多 {tgmd.RICH_MAX} 字')
        tid = chat[len(PREFIX):]
        if t.get('closed'):
            self._reopen(tid)
        d = self._load()
        try:
            base = {'chat_id': d['group']['id'], 'message_thread_id': int(tid)}
            kb = self._keyboard(buttons)
            if kb:
                base['reply_markup'] = kb
            m = self._send_text(d['bot']['token'], base, text)
        except TGError as e:
            if 'THREAD NOT FOUND' in e.msg.upper().replace('_', ' '):
                self._mark_deleted(tid)
                raise TGError('topic_deleted', '这个话题已经在 Telegram 里删掉了')
            raise
        mid = self._store(tid, m['message_id'], m.get('date') or int(self.now()), t['name'], text)
        self.touch(chat)
        return {'status': 'sent', 'chat': chat, 'id': mid}

    def _send_text(self, token, base, text):
        """A rich message (Telegram lays out the Markdown itself: tables, formulas, headings). If Telegram refuses
        it, the same text as plain messages with our own entities, split at 4096; the last one is returned."""
        try:
            return self.call(token, 'sendRichMessage', dict(base, rich_message={'markdown': tgmd.for_rich(text)}), self.proxy())
        except TGError as e:
            if e.code in ('rate_limited', 'network') or 'THREAD NOT FOUND' in e.msg.upper().replace('_', ' '):
                raise
            self.log('tgbot: rich message refused, sending plain:', e.msg)
        m = None
        for part in tgmd.chunks(text):
            shown, ents = tgmd.render(part)
            p = dict(base, text=shown or part)
            if ents:
                p['entities'] = ents
            m = self.call(token, 'sendMessage', p, self.proxy())
        return m

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

    def edit(self, key, chat, mid, text, buttons=None):
        """Replace the text of a message the bot sent (for streaming a reply as it is written)."""
        tid, local = self._target(key, chat, mid, user=False)
        text = (text or '').strip()
        if not text:
            raise TGError('empty', '内容是空的')
        if len(text) > tgmd.RICH_MAX:
            raise TGError('too_long', f'Telegram 单条消息最多 {tgmd.RICH_MAX} 字')
        d = self._load()
        try:
            base = {'chat_id': d['group']['id'], 'message_id': local}
            kb = self._keyboard(buttons)
            if kb:
                base['reply_markup'] = kb
            try:
                self.call(d['bot']['token'], 'editMessageText', dict(base, rich_message={'markdown': tgmd.for_rich(text)}), self.proxy())
            except TGError as e:
                if e.code in ('rate_limited', 'network') or 'NOT MODIFIED' in e.msg.upper().replace('_', ' ') or len(text) > tgmd.PLAIN_MAX:
                    raise
                shown, ents = tgmd.render(text)
                p = dict(base, text=shown or text)
                if ents:
                    p['entities'] = ents
                self.call(d['bot']['token'], 'editMessageText', p, self.proxy())
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

    def _store(self, tid, local_id, ts, sender, text, res=None):
        c = self.db()
        try:
            cur = c.execute('insert or ignore into msgs(chat, local_id, ts, type, sender, text, backfill, res) values(?,?,?,1,?,?,0,?)',
                            (PREFIX + str(tid), local_id, ts, sender, text, res))
            c.execute('update chats set last_local=max(last_local, ?) where chat=?', (local_id, PREFIX + str(tid)))
            c.commit()
            return cur.lastrowid
        finally:
            c.close()

    def media_file(self, msg_id):
        """(path, mime, name) of a file the user sent in a topic (or the agent sent), downloaded on first request."""
        c = self.db()
        try:
            r = c.execute('select res from msgs where id=?', (msg_id,)).fetchone()
        finally:
            c.close()
        if not r or not (r['res'] or '').startswith('bot:'):
            raise TGError('no_media', '这条消息没有文件')
        _, _, file_id, name = (r['res'].split(':', 3) + ['', '', '', ''])[:4]
        name = re.sub(r'[\\/\x00-\x1f]', '_', name) or 'file'
        os.makedirs(self.media_dir, exist_ok=True)
        path = os.path.join(self.media_dir, f'{msg_id}_{name}')
        if not os.path.isfile(path):
            d = self._load()
            info = self.call(d['bot']['token'], 'getFile', {'file_id': file_id}, self.proxy())
            if (info.get('file_size') or 0) > MEDIA_MAX or not info.get('file_path'):
                raise TGError('not_available', '文件太大（Bot API 最多 20MB）')
            data = self.get_file(d['bot']['token'], info['file_path'], self.proxy())
            with open(path + '.part', 'wb') as fh:
                fh.write(data)
            os.replace(path + '.part', path)
        return path, mimetypes.guess_type(path)[0] or 'application/octet-stream', name

    def send_file(self, key, chat, name, data, text=''):
        """Send a picture (shown inline) or any file into the key's own topic."""
        t = self.get(chat)
        if not t:
            raise TGError('unknown_chat', '没有这个话题')
        if t.get('holder') != key['id']:
            raise TGError('not_holder', '这个话题不是你开的')
        name = re.sub(r'[\\/\x00-\x1f]', '_', (name or '').strip()) or 'file'
        if not data:
            raise TGError('empty', '文件是空的')
        if len(data) > MEDIA_MAX:
            raise TGError('too_long', '文件最大 20MB')
        text = (text or '').strip()[:1024]
        tid = chat[len(PREFIX):]
        if t.get('closed'):
            self._reopen(tid)
        d = self._load()
        photo = (mimetypes.guess_type(name)[0] or '').startswith('image/') and not name.lower().endswith('.svg') and len(data) <= 10 * 1024 * 1024
        fields = {'chat_id': d['group']['id'], 'message_thread_id': int(tid)}
        if text:
            shown, ents = tgmd.render(text)
            fields['caption'] = shown or text
            if ents:
                fields['caption_entities'] = json.dumps(ents, ensure_ascii=False)
        m = self.upload(d['bot']['token'], 'sendPhoto' if photo else 'sendDocument', fields, 'photo' if photo else 'document', name, data, self.proxy())
        f = (m.get('photo') or [{}])[-1] if photo else (m.get('document') or {})
        kept = (os.path.splitext(name)[0] + '.jpg') if photo else name      # Telegram re-encodes photos as JPEG
        res = f"bot:{'img' if photo else 'file'}:{f.get('file_id', '')}:{kept.replace(':', '_')}"
        mid = self._store(tid, m['message_id'], m.get('date') or int(self.now()), t['name'], (text + ' ' if text else '') + ('[图片]' if photo else f'[文件] {name}'), res)
        self.touch(chat)
        return {'status': 'sent', 'chat': chat, 'id': mid}

    def poll_once(self, timeout=50):
        """One getUpdates round: keep the user's messages in known topics of the group, drop everything else."""
        d = self._load()
        if not d.get('bot'):
            return 0
        b, g = d['bot'], d.get('group') or {}
        ups = self.call(b['token'], 'getUpdates', {'offset': d['offset'], 'timeout': timeout, 'allowed_updates': ['message', 'callback_query']},
                        self.proxy(), timeout + 15)
        kept, offset = 0, d['offset']
        for up in ups:
            offset = max(offset, up['update_id'] + 1)
            q = up.get('callback_query')
            if q:                                                 # a button pressed: it counts as the user saying its data
                qm = q.get('message') or {}
                qtid = str(qm.get('message_thread_id') or '')
                try:
                    self.call(b['token'], 'answerCallbackQuery', {'callback_query_id': q['id']}, self.proxy())
                except TGError:
                    pass
                if ((q.get('from') or {}).get('id') == b['owner'] and (qm.get('chat') or {}).get('id') == g.get('id')
                        and qtid in d['topics'] and q.get('data')):
                    if self._load()['topics'][qtid].get('closed'):
                        self._reopen(qtid)
                    self._store(qtid, -up['update_id'], int(self.now()), '我', q['data'])
                    self.touch(PREFIX + qtid)
                    kept += 1
                continue
            m = up.get('message') or {}
            tid = str(m.get('message_thread_id') or '')
            if m and (m.get('from') or {}).get('id') == b['owner'] and (m.get('chat') or {}).get('id') == g.get('id') and tid:
                made = m.get('forum_topic_created') or ((m.get('reply_to_message') or {}).get('forum_topic_created') or {})
                if (m.get('forum_topic_created') or m.get('is_topic_message')) and tid not in d['topics']:
                    if self._adopt_topic(tid, made.get('name')):
                        d = self._load()
                if (m.get('forum_topic_edited') or {}).get('name'):
                    self._rename_topic(tid, m['forum_topic_edited']['name'])
                    d = self._load()
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
            res = None
            if kind:
                f = m['photo'][-1] if kind == 'photo' else m[kind]
                ext = {'photo': '.jpg', 'voice': '.ogg', 'video': '.mp4', 'sticker': '.webp'}.get(kind, '')
                fname = f.get('file_name') or f"{kind}_{m['message_id']}{ext}"
                short = {'photo': 'img', 'video': 'vid', 'voice': 'voice', 'sticker': 'img'}.get(kind, 'file')
                res = f"bot:{short}:{f['file_id']}:{fname.replace(':', '_')}"
            if self._load()['topics'][tid].get('closed'):
                self._reopen(tid)
            self._store(tid, m['message_id'], m.get('date') or int(self.now()), '我', text, res)
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
        start_prober(self.proxy, self.log)
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
                delay = min(delay * 2, 30)    # back soon once the network is back

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
