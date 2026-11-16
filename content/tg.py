"""Telegram (user account) for wxbrief: on-demand reads, lazy media, and sending.

Nothing runs in the background. A chat is read from Telegram only when somebody asks for it (agent
via the API, or the page), and cached in the same `msgs` table with a `tg:` chat id. We never mark
anything as read. Sending: only to Saved Messages directly; every other chat goes through a draft
that the user approves on the page.

Chat ids: tg:me (Saved Messages), tg:u<id> user/bot, tg:g<id> group/supergroup, tg:c<id> channel.

All Telethon calls live in `Live`; everything else talks to a small adapter interface, so tests use
a fake adapter (tests/tgtest.py).
"""
import asyncio, concurrent.futures, io, json, mimetypes, os, re, shutil, threading, time

TTL = 60               # seconds a chat/dialog list stays fresh before it is read again
IDLE = 60              # disconnect from Telegram after this many idle seconds
MEDIA_DAYS = 30        # cached media older than this is removed
MEDIA_MAX = 2 << 30    # and the cache never grows beyond 2 GB
FETCH_MAX = 500        # most messages read from Telegram in one call


class TGError(Exception):
    def __init__(self, code, msg='', retry_after=None):
        super().__init__(msg or code)
        self.code, self.msg, self.retry_after = code, msg or code, retry_after

    def as_json(self):
        d = {'error': self.code, 'message': self.msg}
        if self.retry_after is not None:
            d['retry_after'] = self.retry_after
        return d


def is_tg(chat):
    return (chat or '').startswith('tg:')


def split_chat(chat):
    """'tg:u123' -> ('u', 123); 'tg:me' -> ('me', 0)."""
    s = chat[3:]
    if s == 'me':
        return 'me', 0
    return s[0], int(s[1:])


# ---------------------------------------------------------------- Telethon adapter
class Live:
    """The only code that imports Telethon. Sync methods; an asyncio loop runs in its own thread."""

    def __init__(self, log):
        self.log = log
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.client = None
        self.cfg = None            # (api_id, api_hash, proxy, session)
        self.state = 'logged_out'  # logged_out | qr | password | ready
        self.qr = None
        self.qr_url = ''
        self.last = time.time()
        self.err = ''
        self.on_session = None     # callback(session_string) to persist the login
        self.lock = threading.RLock()
        threading.Thread(target=self._idle_loop, daemon=True).start()

    # -- plumbing
    def _run(self, coro, timeout=30, login=False):
        """login=True marks calls that only succeed for a logged-in account (reads, sends)."""
        self.last = time.time()
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            res = fut.result(timeout)
            if login and self.state == 'logged_out':
                self.state = 'ready'
            return res
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise TGError('timeout', 'Telegram 没有及时响应，请检查代理')
        except TGError:
            raise
        except Exception as e:
            raise self._wrap(e)

    @staticmethod
    def _wrap(e):
        from telethon import errors
        if isinstance(e, errors.FloodWaitError):
            return TGError('rate_limited', 'Telegram 限流，请稍后再试', int(getattr(e, 'seconds', 30)))
        if isinstance(e, (errors.AuthKeyError, errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
                          errors.UserDeactivatedError)):
            return TGError('not_logged_in', 'Telegram 登录已失效，请到设置页重新登录')
        if isinstance(e, (ConnectionError, OSError, asyncio.TimeoutError)):
            return TGError('network', f'连不上 Telegram：{type(e).__name__}，请检查代理')
        return TGError('telegram_error', f'{type(e).__name__}: {str(e)[:120]}')

    def configure(self, api_id, api_hash, proxy, session):
        with self.lock:
            new = (api_id, api_hash, proxy, session)
            if new == self.cfg:
                return
            self.cfg = new
            if self.client is not None:
                self._run(self._drop(), 10)

    async def _drop(self):
        try:
            await self.client.disconnect()
        except Exception:
            pass
        self.client = None
        self.qr = None

    async def _client(self):
        if self.client is None:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            api_id, api_hash, proxy, session = self.cfg
            self.client = TelegramClient(StringSession(session or ''), int(api_id), api_hash,
                                         proxy=parse_proxy(proxy), connection_retries=2, retry_delay=1,
                                         timeout=10, device_model='wxbrief', app_version='1.0')
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    def _need_cfg(self):
        if not self.cfg or not self.cfg[0] or not self.cfg[1]:
            raise TGError('not_configured', '还没有填写 Telegram 的 api_id 和 api_hash（设置页）')

    def _idle_loop(self):
        while True:
            time.sleep(15)
            try:
                if (self.client is not None and self.state in ('ready', 'logged_out') and
                        time.time() - self.last > IDLE and self.client.is_connected()):
                    self._run(self.client.disconnect(), 10)
            except Exception:
                pass

    # -- login (QR)
    def status(self):
        with self.lock:
            return {'state': self.state, 'qr_url': self.qr_url, 'error': self.err}

    def check(self):
        """Is the saved session still logged in? Updates state."""
        with self.lock:
            self._need_cfg()
            if self.state in ('qr', 'password'):
                return self.state
            async def go():
                c = await self._client()
                return await c.is_user_authorized()
            ok = self._run(go(), 25)
            self.state = 'ready' if ok else 'logged_out'
            return self.state

    def qr_begin(self):
        with self.lock:
            self._need_cfg()
            if self.state == 'qr':
                return
            self.err = ''
            self.state = 'qr'
            async def first():
                c = await self._client()
                self.qr = await c.qr_login()
                self.qr_url = self.qr.url
            self._run(first(), 25)
            asyncio.run_coroutine_threadsafe(self._qr_loop(), self.loop)

    async def _qr_loop(self):
        from telethon import errors
        deadline = time.time() + 300           # give up after 5 minutes
        while self.state == 'qr' and time.time() < deadline:
            self.last = time.time()
            try:
                await self.qr.wait(timeout=25)
                await self._finish_login()
                return
            except asyncio.TimeoutError:
                try:
                    await self.qr.recreate()
                    self.qr_url = self.qr.url
                except Exception as e:
                    self.err = str(self._wrap(e)); break
            except errors.SessionPasswordNeededError:
                self.state = 'password'; return
            except Exception as e:
                self.err = str(self._wrap(e)); break
        if self.state == 'qr':
            self.state = 'logged_out'

    async def _finish_login(self):
        self.state = 'ready'; self.qr_url = ''; self.err = ''
        if self.on_session:
            self.on_session(self.client.session.save())

    def submit_password(self, pw):
        with self.lock:
            if self.state != 'password':
                raise TGError('bad_state', '现在不需要输入密码')
            async def go():
                from telethon import errors
                try:
                    await self.client.sign_in(password=pw)
                except errors.PasswordHashInvalidError:
                    raise TGError('bad_password', '两步验证密码不对')
                await self._finish_login()
            self._run(go(), 25)

    def logout(self):
        with self.lock:
            if self.cfg and self.client is not None and self.state == 'ready':
                try:
                    self._run(self.client.log_out(), 20)
                except TGError:
                    pass
            if self.client is not None:
                self._run(self._drop(), 10)
            self.state = 'logged_out'; self.qr_url = ''; self.qr = None

    # -- data
    def _peer(self, ref):
        from telethon.tl import types
        kind, i, h = ref
        if kind == 'me':
            return 'me'
        if kind == 'u':
            return types.InputPeerUser(i, h)
        if h is None:
            return types.InputPeerChat(i)
        return types.InputPeerChannel(i, h)

    def me(self):
        async def go():
            c = await self._client()
            m = await c.get_me()
            return (m.first_name or '') + (' ' + m.last_name if m.last_name else '') or (m.username or str(m.id))
        return self._run(go(), 20, login=True)

    def dialogs(self):
        async def go():
            from telethon import utils
            from telethon.tl import types
            c = await self._client()
            out = []
            async for d in c.iter_dialogs():
                e = d.entity
                ts = int(d.date.timestamp()) if d.date else 0
                if isinstance(e, types.User):
                    if e.is_self:
                        out.append(('tg:me', 'Saved Messages（收藏夹）', 'me', None, ts, None)); continue
                    out.append((f'tg:u{e.id}', d.name or str(e.id), 'u', e.access_hash, ts, e.username))
                elif isinstance(e, types.Channel):
                    k = 'g' if e.megagroup else 'c'
                    out.append((f'tg:{k}{e.id}', d.name or str(e.id), k, e.access_hash, ts, e.username))
                elif isinstance(e, types.Chat):
                    out.append((f'tg:g{e.id}', d.name or str(e.id), 'g', None, ts, None))
            return out
        return self._run(go(), 60, login=True)

    def messages(self, ref, since_ts, min_id, limit):
        async def go():
            from telethon import utils
            c = await self._client()
            me = await c.get_me()
            out = []
            async for m in c.iter_messages(self._peer(ref), limit=limit, min_id=min_id):
                if m.date.timestamp() < since_ts:
                    break
                if m.action is not None:
                    out.append(dict(id=m.id, ts=int(m.date.timestamp()), sender='', text='[系统消息]', media=None, name=''))
                    continue
                s = await m.get_sender() if m.sender is None else m.sender
                who = '我' if (s is not None and getattr(s, 'id', None) == me.id) else (utils.get_display_name(s) if s else '')
                kind, name = None, ''
                if m.photo:
                    kind = 'img'
                elif m.voice:
                    kind = 'voice'
                elif m.video or m.video_note or m.gif:
                    kind, name = 'vid', (m.file.name if m.file and m.file.name else '')
                elif m.sticker:
                    pass
                elif m.document:
                    kind, name = 'file', (m.file.name if m.file and m.file.name else 'file')
                out.append(dict(id=m.id, ts=int(m.date.timestamp()), sender=who,
                                text=(m.message or ''), media=kind, name=name,
                                sticker=(m.file.emoji if m.sticker and m.file else None)))
            return out
        return self._run(go(), 60, login=True)

    def send(self, ref, text):
        async def go():
            c = await self._client()
            m = await c.send_message(self._peer(ref), text)
            return m.id, int(m.date.timestamp())
        return self._run(go(), 30, login=True)

    def download(self, ref, msg_id, destdir):
        """Download into destdir (Telethon names the file); returns the path or None."""
        async def go():
            c = await self._client()
            m = await c.get_messages(self._peer(ref), ids=msg_id)
            if m is None or not m.media:
                return None
            return await c.download_media(m, file=destdir.rstrip('/') + '/')
        return self._run(go(), 600, login=True)


def parse_proxy(s):
    """'socks5://host:port' (also socks4, http, user:pass@) -> Telethon proxy tuple, or None."""
    s = (s or '').strip()
    if not s:
        return None
    m = re.match(r'^(socks5|socks4|http)://(?:([^:@/]+):([^@/]*)@)?([^:/]+):(\d+)/?$', s)
    if not m:
        raise TGError('bad_proxy', '代理格式应为 socks5://地址:端口')
    scheme, user, pw, host, port = m.groups()
    t = (scheme, host, int(port))
    return t + (True, user, pw) if user else t


# ---------------------------------------------------------------- service (cache, rules)
class Service:
    def __init__(self, var, db, log, adapter=None):
        self.var, self.db, self.log = var, db, log
        self.cfg_path = var + '/tg.json'
        self.sess_path = var + '/tg.session'
        self.media_dir = var + '/tgmedia'
        self.adapter = adapter or Live(log)
        self.adapter.on_session = self._save_session
        self.flood_until = 0
        self.me_name = ''
        self.lock = threading.RLock()
        self._apply()

    # -- config (all 0600)
    def config(self):
        try:
            return json.load(open(self.cfg_path))
        except Exception:
            return {}

    def set_config(self, api_id=None, api_hash=None, proxy=None):
        c = self.config()
        if api_id is not None and str(api_id).strip():
            if not str(api_id).strip().isdigit():
                raise TGError('bad_config', 'api_id 应该是一串数字')
            c['api_id'] = str(api_id).strip()
        if api_hash is not None and api_hash.strip():
            c['api_hash'] = api_hash.strip()
        if proxy is not None:
            parse_proxy(proxy)
            c['proxy'] = proxy.strip()
        _write(self.cfg_path, json.dumps(c))
        self._apply()

    def _session(self):
        try:
            return open(self.sess_path).read().strip()
        except OSError:
            return ''

    def _save_session(self, s):
        _write(self.sess_path, s)

    def _apply(self):
        c = self.config()
        self.adapter.configure(c.get('api_id'), c.get('api_hash'), c.get('proxy', ''), self._session())

    def configured(self):
        c = self.config()
        return bool(c.get('api_id') and c.get('api_hash'))

    # -- status / login
    def status(self):
        c = self.config()
        st = self.adapter.status()
        return {'configured': self.configured(), 'api_hash_set': bool(c.get('api_hash')),
                'api_id': c.get('api_id', ''), 'proxy': c.get('proxy', ''), 'state': st['state'],
                'qr_url': st['qr_url'], 'error': st['error'], 'me': self.me_name,
                'has_session': bool(self._session())}

    def refresh_state(self):
        """Ask Telegram whether the saved login still works (connects once)."""
        if not self.configured() or not self._session():
            return self.adapter.status()['state']
        s = self.adapter.check()
        if s == 'ready' and not self.me_name:
            try:
                self.me_name = self.adapter.me()
            except TGError:
                pass
        return s

    def login_qr(self):
        self.adapter.qr_begin()

    def password(self, pw):
        self.adapter.submit_password(pw)
        self._after_login()

    def _after_login(self):
        try:
            self.me_name = self.adapter.me()
        except TGError:
            pass

    def poll_login(self):
        """Called by the page while a QR is on screen."""
        st = self.adapter.status()['state']
        if st == 'ready' and not self.me_name:
            self._after_login()
        return st

    def logout(self):
        self.adapter.logout()
        try:
            os.remove(self.sess_path)
        except OSError:
            pass
        self.me_name = ''
        self._apply()

    # -- guards
    def _ready(self):
        if not self.configured():
            raise TGError('not_configured', '还没有填写 Telegram 的 api_id 和 api_hash（设置页）')
        if not self._session() and self.adapter.status()['state'] != 'ready':
            raise TGError('not_logged_in', '还没有登录 Telegram，请到设置页扫码登录')
        left = int(self.flood_until - time.time())
        if left > 0:
            raise TGError('rate_limited', 'Telegram 限流，请稍后再试', left)

    def _call(self, fn, *a):
        self._ready()
        try:
            return fn(*a)
        except TGError as e:
            if e.code == 'rate_limited' and e.retry_after:
                self.flood_until = time.time() + e.retry_after
            raise

    # -- dialogs
    def dialogs(self, force=False):
        c = self.db()
        try:
            age = time.time() - float(self._kv(c, 'tg:dialogs_at', 0))
            if force or age > TTL or not c.execute('select 1 from tg_dialogs limit 1').fetchone():
                rows = self._call(self.adapter.dialogs)
                c.execute('delete from tg_dialogs')
                c.executemany('insert into tg_dialogs(chat, name, kind, hash, last_ts, username) values(?,?,?,?,?,?)', rows)
                c.executemany('insert into chats(chat, name) values(?,?) on conflict(chat) do update set name=excluded.name',
                              [(r[0], r[1]) for r in rows])
                c.execute('insert or replace into kv values(?,?)', ('tg:dialogs_at', str(time.time())))
                c.commit()
            if not self.me_name:
                try:
                    self.me_name = self.adapter.me()
                except TGError:
                    pass
            return [dict(r) for r in c.execute(
                'select d.chat, d.name, d.kind, d.last_ts, d.username, (select count(*) from msgs m where m.chat=d.chat) n '
                'from tg_dialogs d order by d.last_ts desc')]
        finally:
            c.close()

    def cached_dialogs(self):
        c = self.db()
        try:
            return [dict(r) for r in c.execute(
                'select d.chat, d.name, d.kind, d.last_ts, d.username, (select count(*) from msgs m where m.chat=d.chat) n '
                'from tg_dialogs d order by d.last_ts desc')], float(self._kv(c, 'tg:dialogs_at', 0))
        finally:
            c.close()

    @staticmethod
    def _kv(c, k, d):
        r = c.execute('select v from kv where k=?', (k,)).fetchone()
        return r[0] if r else d

    def _ref(self, c, chat):
        kind, i = split_chat(chat)
        if kind == 'me':
            return ('me', 0, None)
        r = c.execute('select hash from tg_dialogs where chat=?', (chat,)).fetchone()
        if r is None:
            raise TGError('unknown_chat', '没有这个 Telegram 会话，先刷新会话列表')
        return (kind, i, r[0])

    # -- messages
    def fetch(self, chat, days=30, limit=200, force=False):
        """Read a chat from Telegram if the cache is older than TTL; returns how many new rows."""
        c = self.db()
        try:
            at = float(self._kv(c, 'tg:fetch:' + chat, 0))
            if not force and time.time() - at < TTL:
                return 0
            ref = self._ref(c, chat)
            hi = c.execute('select coalesce(max(local_id),0) from msgs where chat=?', (chat,)).fetchone()[0]
            since = time.time() - max(1, int(days)) * 86400
            got = self._call(self.adapter.messages, ref, since, hi, max(1, min(int(limit), FETCH_MAX)))
            rows = []
            for m in got:
                text = m['text']
                res = None
                if m['media']:
                    res = 'tg:' + m['media'] + (':' + m['name'] if m['name'] else '')
                    ph = {'img': '[图片]', 'vid': '[视频]', 'voice': '[语音]', 'file': '[文件] ' + m['name']}[m['media']]
                    text = (text + ' ' + ph).strip() if text else ph
                elif m.get('sticker'):
                    text = f"[贴纸 {m['sticker']}]"
                rows.append((chat, m['id'], m['ts'], 1, m['sender'], text, res))
            rows.sort(key=lambda r: (r[2], r[1]))      # Telegram returns newest first; store oldest first so ids follow time
            c.executemany('insert or ignore into msgs(chat, local_id, ts, type, sender, text, res) values(?,?,?,?,?,?,?)', rows)
            top = max([r[1] for r in rows], default=hi)
            c.execute('insert into chats(chat, name, last_local) values(?,?,?) on conflict(chat) do update set '
                      'last_local=max(last_local, excluded.last_local)',
                      (chat, (c.execute('select name from tg_dialogs where chat=?', (chat,)).fetchone() or [chat])[0], top))
            c.execute('insert or replace into kv values(?,?)', ('tg:fetch:' + chat, str(time.time())))
            c.commit()
            return len(rows)
        finally:
            c.close()

    # -- media (downloaded only when asked, cached, trimmed)
    def media_file(self, msg_id):
        """Return (path, mime, filename) for a Telegram message's media, downloading it first if needed."""
        c = self.db()
        try:
            r = c.execute('select chat, local_id, res from msgs where id=?', (msg_id,)).fetchone()
            if not r or not r['res'] or not r['res'].startswith('tg:'):
                raise TGError('no_media', '这条消息没有原件')
            ref = self._ref(c, r['chat'])
            lid, name = r['local_id'], (r['res'].split(':', 2) + [''])[2]
        finally:
            c.close()
        os.makedirs(self.media_dir, exist_ok=True)
        hit = self._cached(msg_id)
        if hit:
            os.utime(hit, None)
            return hit, mimetypes.guess_type(hit)[0] or 'application/octet-stream', os.path.basename(hit).split('_', 1)[1]
        tmp = os.path.join(self.media_dir, f'.dl{msg_id}')
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            got = self._call(self.adapter.download, ref, lid, tmp)
            if not got or not os.path.isfile(got):
                raise TGError('not_available', 'Telegram 上已经没有这个文件了')
            base = _safe(os.path.basename(got)) or _safe(name) or 'media'
            final = os.path.join(self.media_dir, f'{msg_id}_{base}')
            os.replace(got, final)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.cleanup()
        return final, mimetypes.guess_type(final)[0] or 'application/octet-stream', base

    def _cached(self, msg_id):
        try:
            for f in os.listdir(self.media_dir):
                p = os.path.join(self.media_dir, f)
                if f.startswith(f'{msg_id}_') and os.path.isfile(p):
                    return p
        except OSError:
            pass
        return None

    def media_ready(self, msg_id):
        return self._cached(msg_id) is not None

    def cleanup(self, now=None, days=MEDIA_DAYS, cap=MEDIA_MAX):
        """Drop cached media older than `days`, then oldest first until under `cap` bytes."""
        now = now or time.time()
        try:
            files = [(os.path.getmtime(p), os.path.getsize(p), p)
                     for p in (os.path.join(self.media_dir, f) for f in os.listdir(self.media_dir))
                     if os.path.isfile(p)]
        except OSError:
            return 0
        files.sort()
        total, removed = sum(s for _, s, _ in files), 0
        for mt, sz, p in files:
            if now - mt > days * 86400 or total > cap:
                try:
                    os.remove(p); total -= sz; removed += 1
                except OSError:
                    pass
        return removed

    # -- sending
    def send_direct(self, chat, text):
        """Saved Messages only. Everything else must be approved on the page."""
        if chat != 'tg:me':
            raise TGError('needs_approval', '只有发给收藏夹才能直接发送')
        return self._send(chat, text, 'direct')

    def send_approved(self, chat, text):
        if not is_tg(chat):
            raise TGError('bad_chat', '不是 Telegram 会话')
        return self._send(chat, text, 'approved')

    def _send(self, chat, text, via):
        text = (text or '').strip()
        if not text:
            raise TGError('empty', '内容是空的')
        if len(text) > 4096:
            raise TGError('too_long', 'Telegram 单条消息最多 4096 字')
        c = self.db()
        try:
            ref = self._ref(c, chat)
            name = (c.execute('select name from tg_dialogs where chat=?', (chat,)).fetchone() or ['Saved Messages'])[0]
        finally:
            c.close()
        mid, ts = self._call(self.adapter.send, ref, text)
        c = self.db()
        try:
            c.execute('insert into tg_sent(ts, chat, chat_name, body, via) values(?,?,?,?,?)', (ts, chat, name, text, via))
            c.commit()
        finally:
            c.close()
        self.log('telegram: sent', via, 'to', chat)
        return {'status': 'sent', 'chat': chat, 'message_id': mid, 'ts': ts}

    def sent_log(self, n=30):
        c = self.db()
        try:
            return [dict(r) for r in c.execute('select * from tg_sent order by id desc limit ?', (n,))]
        finally:
            c.close()


def _safe(name):
    return re.sub(r'[^\w.\- ()一-鿿]', '_', os.path.basename(name or ''))[:80]


def _write(path, data):
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(data)
    os.replace(tmp, path)


def qr_svg(url):
    import segno
    buf = io.BytesIO()
    segno.make(url, error='m').save(buf, kind='svg', scale=5, border=3, dark='#000', light='#fff',
                                    xmldecl=False, svgns=True)
    return buf.getvalue().decode()
