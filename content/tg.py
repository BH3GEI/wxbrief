"""Telegram (user account) for wxbrief: on-demand reads, lazy media, and sending.

Nothing runs in the background. A chat is read from Telegram only when somebody asks for it (agent
via the API, or the page), and cached in the same `msgs` table with a `tg:` chat id. We never mark
anything as read. Sending (text or a file): Saved Messages and the user's allowlist directly; every
other chat goes through a draft that the user approves on the page.

Chat ids: tg:me (Saved Messages), tg:u<id> user/bot, tg:g<id> group/supergroup, tg:c<id> channel.

All Telethon calls live in `Live`; everything else talks to a small adapter interface, so tests use
a fake adapter (tests/tgtest.py).
"""
import asyncio, concurrent.futures, io, json, mimetypes, os, re, shutil, subprocess, tempfile, threading, time

TTL = 60               # seconds a chat/dialog list stays fresh before it is read again
IDLE = 60              # disconnect from Telegram after this many idle seconds
MEDIA_DAYS = 30        # cached media older than this is removed
MEDIA_MAX = 2 << 30    # and the cache never grows beyond 2 GB
FETCH_MAX = 500        # most messages read from Telegram in one call
FIRST = 200            # a chat read for the first time: this many newest messages; older ones on request
SEND_FILE_MAX = 50 * 1024 * 1024   # files sent from the user account (Telegram itself allows far more)
BUDGET = 25            # seconds a request may spend filling a days= window before it returns what it has


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
        self.busy = 0              # calls in flight: never disconnect under a running upload
        self.err = ''
        self.on_session = None     # callback(session_string) to persist the login
        self.lock = threading.RLock()
        threading.Thread(target=self._idle_loop, daemon=True).start()

    # -- plumbing
    def _run(self, coro, timeout=30, login=False):
        """login=True marks calls that only succeed for a logged-in account (reads, sends)."""
        self.last = time.time()
        with self.lock:            # the idle disconnect checks busy under the same lock
            self.busy += 1
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
        finally:
            with self.lock:
                self.busy -= 1
            self.last = time.time()     # idle counts from the end of a call, not its start

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
        self._me = None

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
                with self.lock:
                    if (self.client is not None and self.state in ('ready', 'logged_out') and not self.busy and
                            time.time() - self.last > IDLE and self.client.is_connected()):
                        asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.loop).result(10)
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

    def messages(self, ref, since_ts, min_id, limit, offset_id=0):
        """Newest first: ids > min_id, and (when offset_id) ids < offset_id, stopping at since_ts."""
        async def go():
            from telethon import utils
            c = await self._client()
            me_id = await self._my_id(c)
            out = []
            async for m in c.iter_messages(self._peer(ref), limit=limit, min_id=min_id, offset_id=offset_id):
                if m.date.timestamp() < since_ts:
                    break
                if m.action is not None:
                    out.append(dict(id=m.id, ts=int(m.date.timestamp()), sender='', text='[系统消息]', media=None, name=''))
                    continue
                s = m.sender          # comes with the batch; never one extra request per message
                if s is not None:
                    who = '我' if getattr(s, 'id', None) == me_id else utils.get_display_name(s)
                else:
                    who = m.post_author or (utils.get_display_name(m.chat) if m.chat else '')
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

    def search(self, ref, q, limit):
        """Server-side search: inside one chat (ref) or across all chats (ref None). Not stored anywhere."""
        async def go():
            from telethon import utils
            from telethon.tl import types
            c = await self._client()
            me = await c.get_me()
            out = []
            async for m in c.iter_messages(self._peer(ref) if ref else None, search=q, limit=limit):
                pid = m.peer_id
                if isinstance(pid, types.PeerUser):
                    cid = 'tg:me' if pid.user_id == me.id else f'tg:u{pid.user_id}'
                elif isinstance(pid, types.PeerChat):
                    cid = f'tg:g{pid.chat_id}'
                elif isinstance(pid, types.PeerChannel):
                    cid = ('tg:g' if m.is_group else 'tg:c') + str(pid.channel_id)
                else:
                    continue
                s = m.sender
                who = '我' if (s is not None and getattr(s, 'id', None) == me.id) else (utils.get_display_name(s) if s else '')
                kind, name = None, ''
                if m.photo:
                    kind = 'img'
                elif m.voice:
                    kind = 'voice'
                elif m.video or m.video_note or m.gif:
                    kind, name = 'vid', (m.file.name if m.file and m.file.name else '')
                elif m.document and not m.sticker:
                    kind, name = 'file', (m.file.name if m.file and m.file.name else 'file')
                out.append(dict(id=m.id, chat=cid, ts=int(m.date.timestamp()), sender=who, text=(m.message or ''), media=kind, name=name))
            return out
        return self._run(go(), 60, login=True)

    def contacts(self):
        """The address book: (chat id, name, username, access_hash). Phone numbers are deliberately not read."""
        async def go():
            from telethon.tl import functions
            c = await self._client()
            res = await c(functions.contacts.GetContactsRequest(hash=0))
            return [(f'tg:u{u.id}', ' '.join(x for x in (u.first_name, u.last_name) if x) or u.username or str(u.id),
                     u.username, u.access_hash) for u in getattr(res, 'users', [])]
        return self._run(go(), 60, login=True)

    async def _my_id(self, c):
        if not getattr(self, '_me', None):
            self._me = (await c.get_me()).id
        return self._me

    def me_id(self):
        async def go():
            return await self._my_id(await self._client())
        return self._run(go(), 20, login=True)

    def username_free(self, username):
        """True if nobody uses this @username (a bot about to be created needs a free one)."""
        async def go():
            c = await self._client()
            try:
                await c.get_entity(username)
                return False
            except (ValueError, TypeError):
                return True
        return self._run(go(), 20, login=True)

    def create_bot(self, name, username):
        """The fixed /newbot dialogue with @BotFather; returns the token. Nothing else is ever sent to BotFather
        (no /deletebot, /revoke, /setname ...). On any unexpected answer the dialogue is cancelled."""
        async def go():
            c = await self._client()
            async with c.conversation('BotFather', timeout=30, exclusive=False) as conv:
                async def say(text):
                    await conv.send_message(text)
                    return (await conv.get_response()).raw_text or ''
                r = await say('/newbot')
                if 'name' not in r.lower():
                    raise TGError('botfather', 'BotFather 没有接受新建：' + r[:200])
                r = await say(name)
                if 'username' not in r.lower():
                    await conv.send_message('/cancel')
                    raise TGError('botfather', 'BotFather 没有接受这个名字：' + r[:200])
                r = await say(username)
                m = re.search(r'\b(\d{6,12}:[A-Za-z0-9_-]{30,})\b', r)
                if not m:
                    await conv.send_message('/cancel')
                    raise TGError('botfather', 'BotFather 没有接受这个用户名：' + r[:200])
                return m.group(1)
        return self._run(go(), 120, login=True)

    def create_group(self, title, about, bot_username):
        """A private supergroup with topics, with our bot added as an admin that may only manage topics.
        Returns the channel id."""
        async def go():
            from telethon.tl import functions, types
            c = await self._client()
            r = await c(functions.channels.CreateChannelRequest(title=title, about=about, megagroup=True, forum=True))
            ch = r.chats[0]
            bot = await c.get_input_entity(bot_username)
            await c(functions.channels.InviteToChannelRequest(ch, [bot]))
            await c(functions.channels.EditAdminRequest(ch, bot, types.ChatAdminRights(manage_topics=True), rank='agent'))
            return ch.id
        return self._run(go(), 60, login=True)

    def start_bot(self, username):
        """/start to our own new bot, so that it may write to us."""
        async def go():
            c = await self._client()
            await c.send_message(username, '/start')
        return self._run(go(), 30, login=True)

    async def _channel(self, c, channel_id):
        from telethon.tl import types
        try:
            return await c.get_input_entity(types.PeerChannel(channel_id))
        except ValueError:          # not in the session's cache yet: the dialog list fills it
            await c.get_dialogs()
            return await c.get_input_entity(types.PeerChannel(channel_id))

    def create_topic(self, channel_id, title):
        """Create a forum topic as the user (so it counts as one he made). Returns its id."""
        async def go():
            from telethon.tl import functions, types
            c = await self._client()
            peer = await self._channel(c, channel_id)
            r = await c(functions.messages.CreateForumTopicRequest(peer=peer, title=title, random_id=int.from_bytes(os.urandom(8), 'big', signed=True)))
            for u in getattr(r, 'updates', []):
                m = getattr(u, 'message', None)
                if m is not None and isinstance(getattr(m, 'action', None), types.MessageActionTopicCreate):
                    return m.id
            raise TGError('no_topic', 'Telegram 没有返回新话题的编号')
        return self._run(go(), 30, login=True)

    def pin_topic(self, channel_id, topic_id):
        async def go():
            from telethon.tl import functions
            c = await self._client()
            peer = await self._channel(c, channel_id)
            try:
                await c(functions.messages.UpdatePinnedForumTopicRequest(peer=peer, topic_id=topic_id, pinned=True))
            except Exception as e:
                if 'PINNED_TOPIC_NOT_MODIFIED' not in str(e):     # already pinned is what we want
                    raise
            return True
        return self._run(go(), 30, login=True)

    def send(self, ref, text):
        async def go():
            c = await self._client()
            m = await c.send_message(self._peer(ref), text)
            return m.id, int(m.date.timestamp())
        return self._run(go(), 30, login=True)

    def send_file(self, ref, name, data, caption, kind, video=None):
        """kind: 'photo' (shown as a picture), 'video' (plays inline; video = (duration, w, h, thumb jpeg or None))
        or 'file'."""
        async def go():
            from telethon.tl import types
            c = await self._client()
            f = io.BytesIO(data); f.name = name
            kw = {}
            if kind == 'video':
                dur, w, h, thumb = video or (0, 0, 0, None)
                kw['supports_streaming'] = True
                if w and h:
                    kw['attributes'] = [types.DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)]
                if thumb:
                    t = io.BytesIO(thumb); t.name = 'thumb.jpg'
                    kw['thumb'] = t
            if kind != 'photo' and len(data) > 1024 * 1024:
                f = await self._upload_parallel(c, data, name)
                if kind == 'file':
                    kw['attributes'] = [types.DocumentAttributeFilename(name)]
                    kw['mime_type'] = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                else:
                    kw['mime_type'] = 'video/mp4'
                    kw.setdefault('attributes', []).append(types.DocumentAttributeFilename(name))
            m = await c.send_file(self._peer(ref), f, caption=caption or None, force_document=(kind == 'file'), **kw)
            return m.id, int(m.date.timestamp())
        try:
            return self._run(go(), 600, login=True)
        except TGError as e:
            if e.code in ('telegram_error', 'network', 'timeout'):    # a half-finished upload can leave the connection stuck
                try:
                    self._run(self._drop(), 10)
                except Exception:
                    pass
            raise

    @staticmethod
    async def _upload_parallel(c, data, name, part=512 * 1024, width=8):
        """Telethon uploads one 512 KB part at a time, so a video crawls along at one part per round trip through
        the proxy; Telegram takes the parts in any order, so keep several in flight."""
        import hashlib, random
        from telethon.tl import functions, types
        fid, n = random.getrandbits(63), (len(data) + part - 1) // part
        big = len(data) > 10 * 1024 * 1024
        gate = asyncio.Semaphore(width)

        async def one(i):
            chunk = data[i * part:(i + 1) * part]
            req = (functions.upload.SaveBigFilePartRequest(fid, i, n, chunk) if big
                   else functions.upload.SaveFilePartRequest(fid, i, chunk))
            async with gate:
                for attempt in range(3):
                    try:
                        if await c(req):
                            return
                    except (ConnectionError, OSError, asyncio.TimeoutError):
                        if attempt == 2:
                            raise
                raise TGError('telegram_error', f'Telegram 没有收下第 {i + 1}/{n} 块')
        await asyncio.gather(*(one(i) for i in range(n)))
        if big:
            return types.InputFileBig(fid, n, name)
        return types.InputFile(fid, n, name, hashlib.md5(data).hexdigest())

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
    """'socks5://host:port' (also socks4, http, user:pass@) -> Telethon proxy tuple, or None.
    The setting may list several proxies (comma or space separated): all are checked, the user account uses
    the first; the bot rotates through them (tgbot.proxies)."""
    items = [p for p in re.split(r'[\s,]+', (s or '').strip()) if p]
    if not items:
        return None
    out = None
    for item in items:
        m = re.match(r'^(socks5|socks4|http)://(?:([^:@/]+):([^@/]*)@)?([^:/]+):(\d+)/?$', item)
        if not m:
            raise TGError('bad_proxy', '代理格式应为 socks5://地址:端口，多个用逗号隔开')
        scheme, user, pw, host, port = m.groups()
        t = (scheme, host, int(port))
        out = out or (t + (True, user, pw) if user else t)
    return out


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
        self._live = {}
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

    # -- live search (Telegram's own search over the whole history; results are returned, never stored)
    def search_live(self, q, chat=None, limit=50):
        q = (q or '').strip()
        if not q:
            raise TGError('empty', '缺少搜索关键词')
        limit = max(1, min(int(limit), 100))
        key = (q.lower(), chat or '', limit)
        hit = self._live.get(key)
        if hit and time.time() - hit[0] < TTL:       # Telegram limits global search hard: repeats are answered from memory
            return hit[1]
        c = self.db()
        try:
            ref = self._ref(c, chat) if chat else None
            got = self._call(self.adapter.search, ref, q, limit)
            names = {r[0]: r[1] for r in c.execute('select chat, name from tg_dialogs')}
        finally:
            c.close()
        out = []
        for m in got:
            cid = chat or m['chat']
            text = m['text']
            if m['media']:
                ph = {'img': '[图片]', 'vid': '[视频]', 'voice': '[语音]', 'file': '[文件] ' + m['name']}[m['media']]
                text = (text + ' ' + ph).strip() if text else ph
            out.append({'source': 'telegram', 'live': True, 'chat': cid, 'chat_name': names.get(cid, cid),
                        'message_id': m['id'], 'ts': m['ts'], 'sender': m['sender'], 'text': text})
        out.sort(key=lambda r: -r['ts'])
        self._live[key] = (time.time(), out)
        if len(self._live) > 50:
            self._live.pop(next(iter(self._live)))
        return out

    # -- contacts
    def contacts(self, force=False):
        """Cache the Telegram address book (10 minutes). Names and @usernames only, never phone numbers."""
        c = self.db()
        try:
            have = c.execute("select 1 from contacts where source='telegram' limit 1").fetchone()
            if not force and have and time.time() - float(self._kv(c, 'tg:contacts_at', 0)) < 600:
                return
            rows = self._call(self.adapter.contacts)
            c.execute("delete from contacts where source='telegram'")
            c.executemany("insert or replace into contacts(source,id,name,remark,nickname,alias,username,kind) "
                          "values('telegram',?,?,NULL,?,NULL,?,'person')", [(r[0], r[1], r[1], r[2]) for r in rows])
            c.execute('insert or replace into kv values(?,?)', ('tg:contacts_at', str(time.time())))
            c.commit()
        finally:
            c.close()

    # -- messages
    def _store(self, c, chat, got, backfill):
        """backfill=1 marks history filled in later; incremental pulls (after_id) skip those rows."""
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
            rows.append((chat, m['id'], m['ts'], 1, m['sender'], text, res, backfill))
        rows.sort(key=lambda r: (r[2], r[1]))      # Telegram returns newest first; insert oldest first
        c.executemany('insert or ignore into msgs(chat, local_id, ts, type, sender, text, res, backfill) values(?,?,?,?,?,?,?,?)', rows)
        return len(rows)

    def fetch(self, chat, days=None, limit=None, force=False):
        """Bring the cache of one chat up to date; returns how many new rows were stored.

        - Always: everything newer than the newest cached message (in rounds, so a busy chat leaves no gap).
        - A chat read for the first time (or with fewer cached messages than wanted): its newest `limit` messages
          (default FIRST, at most FETCH_MAX). Older history comes only when asked: scrolling up (fetch_older) or days=.
        - days=N: also the complete last N days; `tg:cover:<chat>` remembers how far back the cache is complete.
          Stops after BUDGET seconds and returns what it has; the next call continues.
        - The 60 s TTL applies when nothing is missing.
        """
        want = FIRST if not limit else max(1, min(int(limit), FETCH_MAX))
        c = self.db()
        try:
            now = time.time()
            deadline = now + BUDGET
            at = float(self._kv(c, 'tg:fetch:' + chat, 0))
            cover = float(self._kv(c, 'tg:cover:' + chat, 0))
            end = self._kv(c, 'tg:end:' + chat, '0') == '1'
            cnt, hi, lo = c.execute('select count(*), coalesce(max(local_id),0), coalesce(min(local_id),0) from msgs where chat=?',
                                    (chat,)).fetchone()
            since = now - max(1, int(days)) * 86400 if days else None
            need_back = since is not None and not end and (cover == 0 or cover > since + 120)
            need_more = cnt < want and not end
            if not force and now - at < TTL and not need_back and not need_more:
                return 0
            ref = self._ref(c, chat)
            new = 0

            def mark(got, asked):
                nonlocal cover, end
                if len(got) < asked:
                    end, cover = True, 1
                    c.execute('insert or replace into kv values(?,?)', ('tg:end:' + chat, '1'))
                elif got:
                    cover = min(cover or 1e18, min(m['ts'] for m in got))
                c.execute('insert or replace into kv values(?,?)', ('tg:cover:' + chat, str(cover)))

            if hi:
                off = 0
                for _ in range(4):
                    got = self._call(self.adapter.messages, ref, 0, hi, FETCH_MAX, off)
                    new += self._store(c, chat, got, 0); c.commit()   # newer than the cache: really new. Commit: no write lock during network calls
                    if len(got) < FETCH_MAX or time.time() > deadline:
                        break
                    off = min(m['id'] for m in got)
            if need_more:
                ask = want - cnt
                got = self._call(self.adapter.messages, ref, 0, 0, ask, lo)        # the newest ones (lo=0) or just below the cache
                new += self._store(c, chat, got, 1); mark(got, ask); c.commit()
                if got:
                    lo = min(m['id'] for m in got) if not lo else min(lo, min(m['id'] for m in got))
            if need_back and not end:
                while time.time() < deadline and cover > since + 120:
                    got = self._call(self.adapter.messages, ref, since, 0, FETCH_MAX, lo)
                    new += self._store(c, chat, got, 1); c.commit()   # release the write lock before the next network call
                    if len(got) < FETCH_MAX:
                        cover = since                     # reached the start of the window (or of the chat)
                        c.execute('insert or replace into kv values(?,?)', ('tg:cover:' + chat, str(cover)))
                        break
                    lo = min(m['id'] for m in got)
                    mark(got, FETCH_MAX); c.commit()   # mark writes too: commit before the next network call
            top = c.execute('select coalesce(max(local_id),0) from msgs where chat=?', (chat,)).fetchone()[0]
            c.execute('insert into chats(chat, name, last_local) values(?,?,?) on conflict(chat) do update set '
                      'last_local=max(last_local, excluded.last_local)',
                      (chat, (c.execute('select name from tg_dialogs where chat=?', (chat,)).fetchone() or [chat])[0], top))
            c.execute('insert or replace into kv values(?,?)', ('tg:fetch:' + chat, str(now)))
            c.commit()
            return new
        finally:
            c.close()

    def at_start(self, chat):
        """True once the very first message of the chat is cached."""
        c = self.db()
        try:
            return self._kv(c, 'tg:end:' + chat, '0') == '1'
        finally:
            c.close()

    def fetch_older(self, chat, n=100):
        """Read `n` messages older than the oldest cached one (no 30 day limit); marks the start of the chat."""
        c = self.db()
        try:
            lo = c.execute('select coalesce(min(local_id),0) from msgs where chat=?', (chat,)).fetchone()[0]
            if not lo or self._kv(c, 'tg:end:' + chat, '0') == '1':
                return 0
            got = self._call(self.adapter.messages, self._ref(c, chat), 0, 0, n, lo)
            self._store(c, chat, got, 1)
            cover = float(self._kv(c, 'tg:cover:' + chat, 0))
            if len(got) < n:                                   # reached the first message
                c.execute('insert or replace into kv values(?,?)', ('tg:end:' + chat, '1'))
                cover = 1
            elif got:
                cover = min(cover or 1e18, min(m['ts'] for m in got))
            c.execute('insert or replace into kv values(?,?)', ('tg:cover:' + chat, str(cover)))
            c.commit()
            return len(got)
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

    # -- bot pool support (tgbot.Bots); the only BotFather access in the app
    def botfather_create(self, name, username):
        return self._call(self.adapter.create_bot, name, username)

    def owner_id(self):
        return self._call(self.adapter.me_id)

    def username_free(self, username):
        return self._call(self.adapter.username_free, username)

    def start_bot(self, username):
        return self._call(self.adapter.start_bot, username)

    def create_group(self, title, about, bot_username):
        return self._call(self.adapter.create_group, title, about, bot_username)

    def create_topic(self, channel_id, title):
        return self._call(self.adapter.create_topic, channel_id, title)

    def pin_topic(self, channel_id, topic_id):
        return self._call(self.adapter.pin_topic, channel_id, topic_id)

    # -- sending
    def send_direct(self, chat, text):
        """Saved Messages and the chats on the user's allowlist. Everything else must be approved on the page."""
        if chat != 'tg:me' and chat not in self.allowed():
            raise TGError('needs_approval', '只有发给收藏夹或白名单里的会话才能直接发送')
        return self._send(chat, text, 'direct' if chat == 'tg:me' else 'allowlist')

    def allowed(self):
        """Chats the user lets agents send to without approval (managed on the settings page)."""
        return [c for c in self.config().get('allow', []) if is_tg(c) and c != 'tg:me']

    def set_allowed(self, chats):
        c = self.config()
        c['allow'] = list(dict.fromkeys(x for x in chats if is_tg(x) and x != 'tg:me'))
        _write(self.cfg_path, json.dumps(c))

    def send_file(self, chat, name, data, text=''):
        """A picture, an mp4 (plays inline) or any file, to Saved Messages or an allowlisted chat only."""
        if chat != 'tg:me' and chat not in self.allowed():
            raise TGError('needs_approval', '只有收藏夹和白名单里的会话能直接发文件')
        name = re.sub(r'[\\/\x00-\x1f]', '_', os.path.basename((name or '').strip())) or 'file'
        if not data:
            raise TGError('empty', '文件是空的')
        if len(data) > SEND_FILE_MAX:
            raise TGError('too_long', f'文件最大 {SEND_FILE_MAX >> 20}MB')
        text = (text or '').strip()[:1024]
        kind = file_kind(name, len(data))
        video = probe_video(data) if kind == 'video' else None
        c = self.db()
        try:
            ref = self._ref(c, chat)
            cname = (c.execute('select name from tg_dialogs where chat=?', (chat,)).fetchone() or ['Saved Messages'])[0]
        finally:
            c.close()
        t0 = time.time()
        try:
            mid, ts = self._call(self.adapter.send_file, ref, name, data, text, kind, video)
        except TGError as e:
            self.log('telegram: send file failed', kind, len(data), 'to', chat, f'after {time.time() - t0:.0f}s:', e.code, e.msg)
            raise
        body = (text + ' ' if text else '') + {'photo': '[图片]', 'video': '[视频]'}.get(kind, '[文件]') + ' ' + name
        c = self.db()
        try:
            c.execute('insert into tg_sent(ts, chat, chat_name, body, via) values(?,?,?,?,?)',
                      (ts, chat, cname, body, 'direct' if chat == 'tg:me' else 'allowlist'))
            c.commit()
        finally:
            c.close()
        self.log('telegram: sent file', kind, len(data), 'to', chat, f'in {time.time() - t0:.0f}s')
        return {'status': 'sent', 'chat': chat, 'message_id': mid, 'ts': ts, 'kind': kind}

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


def file_kind(name, size):
    """How a file is shown in Telegram: pictures as photos (Telegram takes up to 10 MB), mp4 as a playable video."""
    low = name.lower()
    if low.endswith(('.jpg', '.jpeg', '.png', '.webp')) and size <= 10 * 1024 * 1024:
        return 'photo'
    if low.endswith(('.mp4', '.m4v')):
        return 'video'
    return 'file'


def probe_video(data):
    """(duration, width, height, thumbnail jpeg) via ffprobe/ffmpeg; zeros/None for what can't be read.
    Without width/height Telegram shows the video as a 1x1 box."""
    dur, w, h, thumb = 0, 0, 0, None
    d = tempfile.mkdtemp(prefix='tgsend')
    try:
        src = os.path.join(d, 'v.mp4')
        with open(src, 'wb') as f:
            f.write(data)
        try:
            r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                'stream=width,height:format=duration', '-of', 'json', src],
                               capture_output=True, timeout=30)
            j = json.loads(r.stdout or b'{}')
            st = (j.get('streams') or [{}])[0]
            w, h = int(st.get('width') or 0), int(st.get('height') or 0)
            dur = float((j.get('format') or {}).get('duration') or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        try:
            out = os.path.join(d, 't.jpg')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(min(1.0, dur / 2)), '-i', src, '-frames:v', '1',
                            '-vf', 'scale=320:320:force_original_aspect_ratio=decrease', '-q:v', '4', out],
                           capture_output=True, timeout=30)
            if os.path.isfile(out) and os.path.getsize(out) < 200 * 1024:
                thumb = open(out, 'rb').read()
        except (OSError, subprocess.SubprocessError):
            pass
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return dur, w, h, thumb


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
