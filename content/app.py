"""wxbrief: read the cloud WeChat's databases (read-only), keep every message, and expose a small
JSON API for agents plus a minimal phone-sized page."""
import glob, hashlib, html, json, os, re, sqlite3, sys, threading, time, traceback
import secrets
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxdb, media, apikeys, keys as keymod

VAR = os.environ.get('VAR', '/lzcapp/var')
WECHAT = os.environ.get('WECHAT_DIR', '/wechat/xwechat_files')
WORK = os.environ.get('WORK', '/work')
STORE = VAR + '/brief.db'
LOGF = VAR + '/brief.log'

_loglock = threading.Lock()
def log(*a):
    line = time.strftime('%m-%d %H:%M:%S ') + ' '.join(str(x) for x in a)
    print(line, flush=True)
    with _loglock:
        try:
            if os.path.exists(LOGF) and os.path.getsize(LOGF) > 2_000_000:
                os.replace(LOGF, LOGF + '.1')
            with open(LOGF, 'a') as f:
                f.write(line + '\n')
        except Exception:
            pass

def db():
    c = sqlite3.connect(STORE, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init_store():
    c = db()
    c.executescript('''
    pragma journal_mode=wal;
    create table if not exists msgs(id integer primary key, chat text, local_id int, server_id int,
        ts int, type int, sender text, text text, unique(chat, local_id));
    create index if not exists msgs_ts on msgs(ts);
    create index if not exists msgs_chat on msgs(chat, ts);
    create table if not exists chats(chat text primary key, name text, last_local int default 0);
    create table if not exists kv(k text primary key, v text);
    ''')
    if 'res' not in [r[1] for r in c.execute('pragma table_info(msgs)')]:
        c.execute('alter table msgs add column res text')   # img:<hash> | vid:<hash> | file:<title>
    c.commit(); c.close()

def kv_get(k, d=None):
    c = db(); r = c.execute('select v from kv where k=?', (k,)).fetchone(); c.close()
    return r[0] if r else d

def kv_set(k, v):
    c = db(); c.execute('insert or replace into kv values(?,?)', (k, str(v))); c.commit(); c.close()

# ---------------- reading WeChat ----------------
STATE = {'last_ingest': 0, 'last_new': 0, 'error': '', 'wxid': '', 'acc': ''}

def account_dir():
    ds = [d for d in glob.glob(WECHAT + '/wxid_*') if os.path.exists(d + '/db_storage/message/message_0.db')]
    if not ds:
        return None
    return max(ds, key=lambda d: os.path.getmtime(d + '/db_storage/message/message_0.db'))

SKIP = re.compile(r'^(gh_|@|brandsessionholder|notifymessage|fmessage|medianote|floatbottle|qmessage|qqmail|tmessage|weixin$|newsapp)')
PREFIX = re.compile(r'^([A-Za-z0-9_\-@.]{3,64}):\n')

def render(lt, raw, is_group, packed=None):
    """Message content -> (sender_from_prefix, readable text, media ref or None)."""
    who, text = _render(lt, raw, is_group)
    t = lt & 0xFFFFFFFF; sub = lt >> 32
    res = None
    if t == 34:
        m = re.search(r'voicelength="(\d+)"', _text(raw))
        secs = f' {round(int(m.group(1)) / 1000)}秒' if m else ''
        tr = media.voice_transcript(packed)
        return who, f'[语音]{secs}' + (f' {tr}' if tr else ''), 'voice'
    if t in (3, 43):
        h = media.packed_hash(packed)
        if h:
            res = ('img:' if t == 3 else 'vid:') + h
    elif t == 49 and text.startswith('[文件] '):
        res = 'file:' + text[len('[文件] '):]
    return who, text, res

def _text(raw):
    b = wxdb.unzstd(raw) if raw is not None else b''
    return b.decode('utf-8', 'replace') if isinstance(b, (bytes, bytearray)) else str(b)

def _render(lt, raw, is_group):
    t = lt & 0xFFFFFFFF; sub = lt >> 32
    b = wxdb.unzstd(raw) if raw is not None else b''
    s = b.decode('utf-8', 'replace') if isinstance(b, (bytes, bytearray)) else str(b)
    who = None
    if is_group:
        m = PREFIX.match(s)
        if m:
            who, s = m.group(1), s[m.end():]
    if t == 1: return who, s
    if t == 3: return who, '[图片]'
    if t == 34: return who, '[语音]'
    if t == 43: return who, '[视频]'
    if t == 47: return who, '[表情]'
    if t == 48: return who, '[位置]'
    if t == 42: return who, '[名片]'
    if t == 50: return who, '[通话]'
    if t in (10000, 10002):
        return who, '[系统] ' + re.sub(r'<[^>]+>', '', s)[:300]
    if t == 49:
        try:
            x = ET.fromstring(s[s.find('<'):]) if '<' in s else None
        except ET.ParseError:
            x = None
        if x is not None:
            title = (x.findtext('.//appmsg/title') or '').strip()
            typ = (x.findtext('.//appmsg/type') or '').strip()
            if typ == '57':
                ref = (x.findtext('.//refermsg/content') or '').strip()
                refwho = (x.findtext('.//refermsg/displayname') or '').strip()
                ref = PREFIX.sub('', ref)
                if ref.startswith('<'):
                    ref = '…'
                return who, f'{title}（回复 {refwho}：{ref[:80]}）'
            if typ == '6': return who, f'[文件] {title}'
            if typ == '5':
                url = (x.findtext('.//appmsg/url') or '').strip()
                return who, f'[链接] {title} {url[:200]}'
            if typ == '19': return who, f'[聊天记录] {title}'
            if typ == '2000': return who, f'[转账] {title}'
            if typ == '2001': return who, '[红包]'
            if typ in ('33', '36'): return who, f'[小程序] {title}'
            return who, f'[{typ or sub}] {title}'
        return who, '[卡片消息]'
    return who, f'[类型{t}]'

RES_VERSION = '1'

def ingest(K):
    acc = account_dir()
    if not acc:
        STATE['error'] = '找不到云微信的数据目录'
        return 0
    wx = os.path.basename(acc)
    STATE['wxid'] = wx; STATE['acc'] = acc
    os.environ['WXID'] = wx
    me = re.sub(r'_[0-9a-f]{4}$', '', wx)
    store = acc + '/db_storage'
    os.makedirs(WORK, exist_ok=True)
    # contacts
    names = {}
    ck = K.key_for(store + '/contact/contact.db')
    if ck is None:
        STATE['error'] = '密钥失效，等待重新登录微信'
        return 0
    wxdb.decrypt_to(store + '/contact/contact.db', ck, WORK + '/contact.db')
    cc = sqlite3.connect(WORK + '/contact.db')
    for u, rem, nick in cc.execute('select username, remark, nick_name from contact'):
        names[u] = rem or nick or u
    try:
        for u, nick in cc.execute('select username, nick_name from stranger'):
            names.setdefault(u, nick or u)
    except sqlite3.Error:
        pass
    cc.close(); os.remove(WORK + '/contact.db')
    names[me] = '我'

    out = db(); new = 0
    backfill = kv_get('res_version') != RES_VERSION   # rows stored before media refs existed
    last = {r['chat']: r['last_local'] for r in out.execute('select chat, last_local from chats')}
    for path in sorted(glob.glob(store + '/message/message_[0-9]*.db')):
        k = K.key_for(path)
        if k is None:
            STATE['error'] = '密钥失效，等待重新登录微信'
            return new
        plain = WORK + '/' + os.path.basename(path)
        wxdb.decrypt_to(path, k, plain)
        m = sqlite3.connect(plain)
        try:
            ids = {r[0]: r[1] for r in m.execute('select rowid, user_name from Name2Id')}
            tables = {r[0] for r in m.execute("select name from sqlite_master where type='table' and name like 'Msg_%'")}
            for chat in ids.values():
                t = 'Msg_' + hashlib.md5(chat.encode()).hexdigest()
                if t not in tables or SKIP.match(chat):
                    continue
                grp = chat.endswith('@chatroom')
                lo = last.get(chat, 0)
                if backfill and lo:
                    old = m.execute(f'select local_id, local_type, message_content, packed_info_data from {t} '
                                    f'where local_id <= ? and (local_type & 4294967295) in (3, 43, 49)', (lo,)).fetchall()
                    fill = [(r, chat, lid) for lid, lt, content, pk in old
                            for r in [render(lt or 0, content, grp, pk)[2]] if r and r != 'voice']
                    out.executemany('update msgs set res=? where chat=? and local_id=? and res is null', fill)
                if lo:
                    vs = m.execute(f'select local_id, local_type, message_content, packed_info_data from {t} '
                                   f'where local_id <= ? and (local_type & 4294967295) = 34', (lo,)).fetchall()
                    out.executemany('update msgs set text=?, res=? where chat=? and local_id=? and '
                                    '(text is not ? or res is not ?)',
                                    [(tx, f'voice:{lid}', chat, lid, tx, f'voice:{lid}')
                                     for lid, lt, content, pk in vs for tx in [render(lt or 0, content, grp, pk)[1]]])
                rows = m.execute(f'select local_id, server_id, local_type, real_sender_id, create_time, message_content, '
                                 f'packed_info_data from {t} where local_id > ? order by local_id', (lo,)).fetchall()
                if not rows:
                    continue
                batch = []
                for lid, sid, lt, rs, ts, content, pk in rows:
                    who, text, res = render(lt or 0, content, grp, pk)
                    if res == 'voice':
                        res = f'voice:{lid}'
                    sender = ids.get(rs) or who or (chat if not grp else '?')
                    batch.append((chat, lid, sid, ts, (lt or 0) & 0xFFFFFFFF, names.get(sender, sender), text, res))
                out.executemany('insert or ignore into msgs(chat, local_id, server_id, ts, type, sender, text, res) '
                                'values(?,?,?,?,?,?,?,?)', batch)
                hi = rows[-1][0]
                out.execute('insert into chats(chat, name, last_local) values(?,?,?) on conflict(chat) do update set '
                            'name=excluded.name, last_local=max(last_local, excluded.last_local)',
                            (chat, names.get(chat, chat), hi))
                last[chat] = hi
                new += len(batch)
            # refresh chat display names
            for chat in ids.values():
                if chat in names:
                    out.execute('update chats set name=? where chat=?', (names[chat], chat))
        finally:
            m.close(); os.remove(plain)
    if backfill:
        out.execute("insert or replace into kv values('res_version', ?)", (RES_VERSION,))
    out.commit(); out.close()
    STATE['error'] = ''
    return new

def ingest_loop(K, IK):
    seen = None; ik_checked = 0
    while True:
        try:
            acc = account_dir()
            if acc and K.state == 'ok' and IK.state != 'deriving' and (not ik_checked or (
                    IK.state != 'ok' and time.time() - ik_checked > 600)):
                ik_checked = time.time()
                IK.ensure(acc)
            sig = None
            if acc:
                sig = tuple(sorted((f, os.path.getmtime(f), os.path.getsize(f))
                                   for f in glob.glob(acc + '/db_storage/message/message_[0-9]*.db*')))
            if sig and sig != seen:
                n = ingest(K)
                if not STATE['error']:
                    seen = sig
                STATE['last_ingest'] = time.time()
                if n:
                    STATE['last_new'] = time.time()
                    log('ingest: new messages', n)
            elif not acc:
                STATE['error'] = '找不到云微信的数据目录'
        except Exception as e:
            STATE['error'] = f'读取出错：{e}'
            log('ingest error', traceback.format_exc())
        time.sleep(30)


# ---------------- voice ----------------
class Voices:
    """Voice audio lives in media_*.db (VoiceInfo). Keep one decrypted copy, refreshed when the source changes."""
    def __init__(self):
        self.lock = threading.Lock(); self.sig = None; self.index = {}

    def _refresh(self, K):
        acc = STATE['acc']
        if not acc:
            return False
        srcs = sorted(glob.glob(acc + '/db_storage/message/media_[0-9]*.db'))
        sig = tuple((f, os.path.getmtime(f), os.path.getsize(f)) for p in srcs for f in (p, p + '-wal') if os.path.exists(f))
        if sig == self.sig:
            return True
        os.makedirs(WORK, exist_ok=True)
        index = {}
        for i, p in enumerate(srcs):
            k = K.key_for(p)
            if k is None:
                return False
            plain = f'{WORK}/voice_{i}.db'
            wxdb.decrypt_to(p, k, plain)
            c = sqlite3.connect(plain)
            for chat, lid, ts, n, rid in c.execute('select n.user_name, v.local_id, v.create_time, length(v.voice_data), v.rowid '
                                                   'from VoiceInfo v join Name2Id n on n.rowid = v.chat_name_id'):
                index.setdefault((chat, lid), []).append((ts, n or 0, plain, rid))
            c.close()
        self.index, self.sig = index, sig
        return True

    def _find(self, chat, lid, ts):
        c = self.index.get((chat, lid)) or []
        c = sorted(c, key=lambda x: (abs((x[0] or 0) - (ts or 0)), -x[1]))
        return c[0] if c and c[0][1] > 0 else None

    def available(self, K, chat, lid, ts):
        with self.lock:
            return 'full' if self._refresh(K) and self._find(chat, lid, ts) else None

    def get(self, K, chat, lid, ts):
        with self.lock:
            if not self._refresh(K):
                return None
            hit = self._find(chat, lid, ts)
            if not hit:
                return None
            c = sqlite3.connect(hit[2])
            try:
                return c.execute('select voice_data from VoiceInfo where rowid=?', (hit[3],)).fetchone()[0]
            finally:
                c.close()

VOICES = Voices()
KEYS = None

# ---------------- api ----------------
APIKEYS = None

MEDIA_KIND = {'img': 'image', 'vid': 'video', 'file': 'file', 'voice': 'voice'}

def media_json(r):
    res = r['res']
    if not res:
        return None
    kind = res.split(':', 1)[0]
    if kind == 'voice':
        avail = VOICES.available(KEYS, r['chat'], int(res[6:]), r['ts'])
    else:
        avail = media.availability(STATE['acc'], r['chat'], res)
    return {'kind': MEDIA_KIND.get(kind), 'url': f"/api/media/{r['id']}",
            'thumb_url': f"/api/media/{r['id']}?thumb=1" if kind in ('img', 'vid') else None,
            'available': avail}

def rows_json(rows):
    out = []
    for r in rows:
        d = {'id': r['id'], 'chat': r['chat'], 'chat_name': r['name'], 'ts': r['ts'], 'type': r['type'],
             'sender': r['sender'], 'text': r['text']}
        mj = media_json(r)
        if mj:
            d['media'] = mj
        out.append(d)
    return out

KIND_SQL = {'group': " and m.chat like '%@chatroom'", 'private': " and m.chat not like '%@chatroom'"}

def api(path, q):
    g = lambda k, d='': q.get(k, [d])[0]
    lim = min(int(g('limit', '200') or 200), 2000)
    c = db()
    try:
        if path == '/api/chats':
            like = '%' + g('q') + '%'
            rs = c.execute('select ch.chat, ch.name, max(m.ts) last_ts, count(m.id) n from chats ch join msgs m on m.chat=ch.chat '
                           'where (ch.name like ? or ch.chat like ?)' + KIND_SQL.get(g('kind'), '').replace('m.chat', 'ch.chat') +
                           ' group by ch.chat order by last_ts desc limit ?', (like, like, lim)).fetchall()
            return [dict(r) for r in rs]
        if path in ('/api/messages', '/api/search'):
            w, a = [], []
            if g('kind') in KIND_SQL: w.append(KIND_SQL[g('kind')][5:])
            if g('chat'): w.append('m.chat=?'); a.append(g('chat'))
            if g('since'): w.append('m.ts>?'); a.append(int(g('since')))
            if g('until'): w.append('m.ts<=?'); a.append(int(g('until')))
            if g('after_id'): w.append('m.id>?'); a.append(int(g('after_id')))
            if g('q'): w.append('m.text like ?'); a.append('%' + g('q') + '%')
            order = 'asc' if g('after_id') or g('since') else 'desc'
            sql = ('select m.*, coalesce(ch.name, m.chat) name from msgs m left join chats ch on ch.chat=m.chat '
                   + ('where ' + ' and '.join(w) if w else '') + f' order by m.id {order} limit ?')
            rs = c.execute(sql, a + [lim]).fetchall()
            return rows_json(rs if order == 'asc' else rs[::-1])
    finally:
        c.close()
    return None

# ---------------- web ----------------
CSS = """
:root{--bg:#fff;--fg:#111;--mute:#777;--line:#e6e6e6;--acc:#1f6f4a;--warn:#b54708}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mute:#999;--line:#2a2a2a;--acc:#5cc493;--warn:#f0a35e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,"PingFang SC",sans-serif}
main{max-width:640px;margin:0 auto;padding:12px 16px 40px}a{color:inherit;text-decoration:none}
.row{display:block;padding:12px 0;border-bottom:1px solid var(--line)}.sub{color:var(--mute);font-size:13px}
.m{padding:8px 0;border-bottom:1px solid var(--line);word-break:break-word;white-space:pre-wrap}.w{color:var(--acc);font-weight:600}
input{width:100%;font:inherit;color:inherit;background:none;border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0}
.err{color:var(--warn);padding:8px 0}.pic{display:block;margin-top:4px}.pic img{max-width:60%;max-height:240px;border-radius:6px}
.file{color:var(--acc)}
.key{padding:14px 0;border-bottom:1px solid var(--line)}.key h4{margin:0 0 2px}
textarea{width:100%;font:13px/1.45 ui-monospace,Menlo,monospace;color:inherit;background:none;border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:6px;resize:vertical}
button{font:inherit;font-size:14px;color:var(--fg);background:none;border:1px solid var(--line);border-radius:8px;padding:6px 12px;margin:4px 6px 0 0;cursor:pointer}
button.pri{background:var(--acc);border-color:var(--acc);color:var(--bg)}button.del{color:var(--warn)}.inline{display:inline}.na{color:var(--mute);font-size:13px}code{word-break:break-all;font-size:13px}
"""

def page(title, body):
    return (f'<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>')

def fmt(ts):
    return time.strftime('%m-%d %H:%M', time.localtime(ts)) if ts else '-'

def home(K, q):
    s = q.get('q', [''])[0]
    out = [f'<form><input name="q" value="{html.escape(s)}" placeholder="搜会话或消息"></form>']
    if K.state != 'ok':
        out.append('<div class="err">微信密钥失效：请在云微信里退出再登录一次，会自动重新获取。</div>')
    elif STATE['error']:
        out.append(f'<div class="err">{html.escape(STATE["error"])}</div>')
    if s:
        for r in api('/api/search', {'q': [s], 'limit': ['50']}):
            out.append(f'<a class="row" href="/chat?c={html.escape(r["chat"])}#m{r["id"]}"><div>{html.escape(r["text"][:120])}</div>'
                       f'<div class="sub">{html.escape(r["chat_name"])} · {fmt(r["ts"])}</div></a>')
    else:
        for r in api('/api/chats', {'limit': ['200']}):
            out.append(f'<a class="row" href="/chat?c={html.escape(r["chat"])}"><div>{html.escape(r["name"])}</div>'
                       f'<div class="sub">{fmt(r["last_ts"])} · {r["n"]} 条</div></a>')
    out.append(f'<p class="sub">读取于 {fmt(STATE["last_ingest"])} · <a href="/keys">API key 管理</a></p>')
    return page('微信', ''.join(out))

def chat(q):
    cid = q.get('c', [''])[0]
    c = db()
    name = (c.execute('select name from chats where chat=?', (cid,)).fetchone() or [cid])[0]
    c.close()
    out = [f'<p><a class="sub" href="/">← 返回</a></p><h3>{html.escape(name)}</h3>']
    for r in api('/api/messages', {'chat': [cid], 'limit': ['300']}):
        out.append(f'<div class="m" id="m{r["id"]}"><span class="sub">{fmt(r["ts"])}</span> '
                   f'<span class="w">{html.escape(r["sender"] or "")}</span>：{media_html(r)}</div>')
    return page(name, ''.join(out))

def media_html(r):
    text = html.escape(r['text'] or '')
    mj = r.get('media')
    if not mj:
        return text
    u = f"/media/{r['id']}"
    if not mj['available']:
        return f'{text} <span class="na">（原件未下载）</span>'
    if mj['kind'] == 'file':
        return f'<a class="file" href="{u}">{text}</a>'
    if mj['kind'] == 'voice':
        return f'{text}<audio class="pic" controls preload="none" src="{u}"></audio>'
    img = f'<img loading="lazy" src="{u}?thumb=1" alt="{text}">'
    if mj['kind'] == 'video':
        if mj['available'] == 'full':
            return f'<a class="file" href="{u}">{text}</a><a class="pic" href="{u}">{img}</a>'
        return f'{text} <span class="na">（只有封面）</span><span class="pic">{img}</span>'
    return f'<a class="pic" href="{u}">{img}</a>'

def agent_line(base, key):
    """The one line you paste to an agent."""
    return (f'消息简报 API（我的微信聊天记录，只读）：地址 {base} ，每个请求加请求头 Authorization: Bearer {key} ；'
            f'先 GET {base}/api/help 看完整用法。常用：GET /api/chats 列会话，GET /api/messages?chat=会话ID&since=秒级时间戳 取消息，'
            f'GET /api/search?q=关键词 搜索，消息里有 media.url 的用 GET /api/media/消息ID 下载图片/视频/文件/语音原件。')

HELP = """消息简报 API —— 云微信聊天记录的只读接口

地址：{base}
认证：每个请求带请求头 Authorization: Bearer <你的 key>（/api/help 本身不需要）。key 在 {base}/keys 管理。
返回：JSON（UTF-8）。时间 ts 都是 Unix 秒。

GET /api/chats?q=&kind=group|private&limit=200
    会话列表，按最近消息倒序：chat（会话ID）、name、last_ts、n（消息数）。
GET /api/messages?chat=&kind=group|private&since=&until=&after_id=&limit=200（最多 2000）
    消息：id、chat、chat_name、ts、type、sender、text，带原件的还有 media。
    给了 since 或 after_id 时从旧到新取，否则取最新的 limit 条；返回都按时间正序。
    增量拉取：记住最大的 id，下次用 after_id=它。
GET /api/search?q=关键词&chat=&limit=
    在消息文本里搜索（包括微信自带的语音转文字）。
GET /api/media/<消息id>[?thumb=1][&format=silk]
    原件。media.kind：image（jpg/png）、video（mp4，thumb=1 取封面）、file（原文件）、voice（WAV，format=silk 取原始 SILK）。
    media.available：full=原件在，thumb=只有缩略图/封面，null=云微信没下载（请求会 404）。
GET /api/status
    微信数据库密钥、图片密钥和最近一次同步的状态。

文本约定：[图片] [视频] [语音] N秒 [文件] 文件名 [链接] 标题 URL [聊天记录] [转账] [红包] [小程序] [系统] ……
语音在微信里点过“转文字”的，转写跟在“[语音] N秒”后面。
"""

def help_text(base):
    return HELP.replace('{base}', base)

def base_url(handler):
    host = handler.headers.get('X-Forwarded-Host') or handler.headers.get('Host') or 'brief'
    return 'https://' + host.split(',')[0].strip()

KEYS_JS = """<script>function cp(b,only){var d=b.parentNode,t=only?b.dataset.k:d.querySelector('textarea').value,o=b.textContent;
function done(){b.textContent='已复制';setTimeout(function(){b.textContent=o},1500)}
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(done)}
else{var a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();done()}}</script>"""

def keys_page(base, new_id=None):
    e = html.escape
    out = ['<p><a class="sub" href="/">← 返回</a></p><h3>API key 管理</h3>',
           '<p class="sub">每个 agent 用一个 key，不用了就吊销。每个 key 下面是一行给 agent 的说明（已含 key），复制给 agent 即可。'
           '完整用法见 <a class="file" href="/api/help">/api/help</a>。</p>']
    keys = APIKEYS.list()
    for k in keys:
        new = k['id'] == new_id
        out.append(
            f'<div class="key" id="k{k["id"]}"><h4>{e(k["name"])}{" <span class=sub>（新建）</span>" if new else ""}</h4>'
            f'<div class="sub">创建于 {fmt(k["created"])} · 最后使用 {fmt(k["last_used"])} · <code>{e(k["key"][:8])}…</code></div>'
            f'<textarea rows="4" readonly{" autofocus" if new else ""}>{e(agent_line(base, k["key"]))}</textarea>'
            f'<button class="pri" type="button" onclick="cp(this,0)">复制给 agent 的说明</button>'
            f'<button type="button" onclick="cp(this,1)" data-k="{e(k["key"])}">只复制 key</button>'
            f'<form class="inline" method="post" action="/keys/revoke" '
            f'onsubmit="return confirm(&quot;吊销「{e(k["name"])}」？用它的 agent 会立刻失去访问。&quot;)">'
            f'<input type="hidden" name="id" value="{k["id"]}"><button class="del">吊销</button></form></div>')
    if not keys:
        out.append('<p class="sub">还没有 key。</p>')
    out.append('<h4 style="margin-top:24px">新建 key</h4><form method="post" action="/keys/create">'
               '<input name="name" maxlength="40" placeholder="名字，比如 Claude Code、日报机器人" required>'
               '<button class="pri">新建</button></form>' + KEYS_JS)
    return page('API key', ''.join(out))

IK = None

def make_handler(K):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def send(self, body, code=200, ctype='text/html; charset=utf-8'):
            b = body.encode()
            self.send_response(code); self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
        def sec_headers(self):
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', 'sandbox')
        def voice(self, r, mid):
            raw = VOICES.get(K, r['chat'], int(r['res'][6:]), r['ts'])
            if not raw:
                return None, '语音未下载到云微信', None, None
            fmt = parse_qs(urlparse(self.path).query).get('format', [''])[0]
            if fmt != 'silk':
                wav = media.silk_to_wav(raw)
                if wav:
                    return 'bytes', wav, 'audio/wav', f'voice_{mid}.wav'
            return 'bytes', raw[1:] if raw[:1] == b'\x02' else raw, 'audio/silk', f'voice_{mid}.silk'
        def send_media(self, mid, thumb):
            c = db()
            r = c.execute('select chat, res, ts from msgs where id=?', (mid,)).fetchone()
            c.close()
            if not r or not r['res']:
                return self.send('{"error":"no media"}', 404, 'application/json')
            if r['res'].startswith('voice:'):
                how, data, mime, name = self.voice(r, mid)
            else:
                how, data, mime, name = media.fetch(STATE['acc'], r['chat'], r['res'], IK, thumb)
            if how is None:
                return self.send(json.dumps({'error': data}, ensure_ascii=False), 404, 'application/json; charset=utf-8')
            from urllib.parse import quote
            # Files come from chat partners: never let them run in this origin (it serves /token).
            risky = mime.startswith('application/') or any(x in mime for x in ('html', 'xml', 'svg', 'javascript'))
            disp = f"{'attachment' if risky else 'inline'}; filename*=UTF-8''{quote(name)}"
            if how == 'bytes':
                self.send_response(200); self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data))); self.send_header('Content-Disposition', disp); self.sec_headers()
                self.send_header('Cache-Control', 'private, max-age=86400'); self.end_headers()
                return self.wfile.write(data)
            size = os.path.getsize(data); start, end = 0, size - 1
            m = re.match(r'bytes=(\d*)-(\d*)$', self.headers.get('Range', ''))
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1)); end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416); self.send_header('Content-Range', f'bytes */{size}'); self.end_headers(); return
                self.send_response(206); self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            else:
                self.send_response(200)
            self.send_header('Content-Type', mime); self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1)); self.send_header('Content-Disposition', disp); self.sec_headers()
            self.end_headers()
            with open(data, 'rb') as f:
                f.seek(start); left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk); left -= len(chunk)
        def do_GET(self):
            u = urlparse(self.path); q = parse_qs(u.query)
            thumb = q.get('thumb', [''])[0] in ('1', 'true')
            try:
                mm = re.match(r'^/(api/)?media/(\d+)$', u.path)
                if mm and not mm.group(1):
                    return self.send_media(int(mm.group(2)), thumb)
                if u.path == '/api/help':
                    return self.send(help_text(base_url(self)), ctype='text/plain; charset=utf-8')
                if u.path.startswith('/api/'):
                    if not APIKEYS.check(self.headers.get('Authorization', '')):
                        return self.send('{"error":"unauthorized"}', 401, 'application/json')
                    if mm:
                        return self.send_media(int(mm.group(2)), thumb)
                    if u.path == '/api/status':
                        res = {'key': K.state, 'capture': K.capture_note, 'image_key': IK.state,
                               **{k: v for k, v in STATE.items() if k != 'acc'}}
                    else:
                        res = api(u.path, q)
                    if res is None:
                        return self.send('{"error":"not found"}', 404, 'application/json')
                    return self.send(json.dumps(res, ensure_ascii=False), ctype='application/json; charset=utf-8')
                if u.path == '/': self.send(home(K, q))
                elif u.path == '/chat': self.send(chat(q))
                elif u.path in ('/keys', '/token'):
                    self.send(keys_page(base_url(self), q.get('new', [''])[0]))
                elif u.path == '/healthz': self.send('ok', ctype='text/plain')
                else: self.send('not found', 404, 'text/plain')
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log('http', traceback.format_exc()); self.send(f'error: {html.escape(str(e))}', 500)
        def redirect(self, to):
            self.send_response(303); self.send_header('Location', to); self.send_header('Content-Length', '0'); self.end_headers()
        def do_POST(self):
            u = urlparse(self.path)
            try:
                # Key management is web-only (behind the Lazycat login); /api/ is the public path and stays read-only.
                if u.path not in ('/keys/create', '/keys/revoke'):
                    return self.send('not found', 404, 'text/plain')
                origin = self.headers.get('Origin') or self.headers.get('Referer') or ''
                if urlparse(origin).netloc != urlparse(base_url(self)).netloc:
                    return self.send('bad origin', 403, 'text/plain')
                n = min(int(self.headers.get('Content-Length') or 0), 4096)
                f = parse_qs(self.rfile.read(n).decode('utf-8', 'replace'))
                if u.path == '/keys/create':
                    k = APIKEYS.create(f.get('name', [''])[0])
                    log('apikey: created', k['id'])
                    return self.redirect(f"/keys?new={k['id']}#k{k['id']}")
                kid = f.get('id', [''])[0]
                if APIKEYS.revoke(kid):
                    log('apikey: revoked', kid)
                return self.redirect('/keys')
            except Exception as e:
                log('http', traceback.format_exc()); self.send(f'error: {html.escape(str(e))}', 500)
    return H

def main():
    os.makedirs(VAR, exist_ok=True)
    init_store()
    global IK, KEYS, APIKEYS
    APIKEYS = apikeys.ApiKeys()
    K = keymod.Keys(log)
    KEYS = K
    IK = media.ImageKey(log)
    log('start; key state', K.state, '; image key', IK.state)
    import signal
    def stop(sig, frame):
        log('stopping; releasing gdb if attached')
        K.release()
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=ingest_loop, args=(K, IK), daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', 3000))), make_handler(K)).serve_forever()

if __name__ == '__main__':
    main()
