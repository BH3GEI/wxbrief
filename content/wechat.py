"""Reading the WeChat databases: messages, contacts, voice."""
import glob, hashlib, os, re, sqlite3, threading, time, traceback
import xml.etree.ElementTree as ET

import ctx, wxdb, media
from ctx import INGEST_NOW, LABEL, STATE, WECHAT, WORK, db, kv_get, log


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
    t = lt & 0xFFFFFFFF
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

def contact_kind(u):
    """person, group, or other (official accounts, enterprise and system entries)."""
    if u.endswith('@chatroom'):
        return 'group'
    if u.startswith('gh_') or u.endswith('@openim') or SKIP.match(u):
        return 'other'
    return 'person'

def save_contacts(out, crows):
    """Replace the stored WeChat address book."""
    rows = [('wechat', u, rem or nick or u, rem or None, nick or None, alias or None, None, contact_kind(u), lt)
            for u, rem, nick, alias, lt in crows if u]
    out.execute("delete from contacts where source='wechat'")
    out.executemany('insert or replace into contacts(source,id,name,remark,nickname,alias,username,kind,type) values(?,?,?,?,?,?,?,?,?)', rows)
    out.commit()

def ingest(K):
    acc = account_dir()
    if not acc:
        STATE['error'] = f'找不到{LABEL}的数据目录（{WECHAT}）。微信登录过一次后才会生成。'
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
        STATE['error'] = '密钥未就绪或已失效，等待自动获取（保持微信登录状态）'
        return 0
    wxdb.decrypt_to(store + '/contact/contact.db', ck, WORK + '/contact.db')
    cc = sqlite3.connect(WORK + '/contact.db')
    try:
        crows = cc.execute('select username, remark, nick_name, alias, local_type from contact').fetchall()
    except sqlite3.Error:      # older layouts have no alias / local_type
        crows = [(u, rem, nick, None, None) for u, rem, nick in cc.execute('select username, remark, nick_name from contact')]
    for u, rem, nick, alias, lt in crows:
        names[u] = rem or nick or u
    try:
        for u, nick in cc.execute('select username, nick_name from stranger'):
            names.setdefault(u, nick or u)
    except sqlite3.Error:
        pass
    cc.close(); os.remove(WORK + '/contact.db')
    names[me] = '我'

    out = db(); new = 0
    save_contacts(out, crows)
    backfill = kv_get('res_version') != RES_VERSION   # rows stored before media refs existed
    last = {r['chat']: r['last_local'] for r in out.execute('select chat, last_local from chats')}
    for path in sorted(glob.glob(store + '/message/message_[0-9]*.db')):
        k = K.key_for(path)
        if k is None:
            STATE['error'] = '密钥未就绪或已失效，等待自动获取（保持微信登录状态）'
            out.commit(); out.close()
            return new
        plain = WORK + '/' + os.path.basename(path)
        wxdb.decrypt_to(path, k, plain)
        m = sqlite3.connect(plain)
        try:
            ids = {r[0]: r[1] for r in m.execute('select rowid, user_name from Name2Id')}
            tables = {r[0] for r in m.execute("select name from sqlite_master where type='table' and name like 'Msg_%'")}
            for chat in ids.values():
                out.commit()    # never hold the write lock across chats or while the next database is decrypted
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
            out.commit()
        finally:
            m.close(); os.remove(plain)
    if backfill:
        out.execute("insert or replace into kv values('res_version', ?)", (RES_VERSION,))
    out.commit(); out.close()
    STATE['error'] = ''
    return new

def ingest_loop(K):
    seen = None; ik_checked = 0
    while True:
        try:
            acc = account_dir()
            if acc and K.state == 'ok' and ctx.IK.state != 'deriving' and (not ik_checked or (
                    ctx.IK.state != 'ok' and time.time() - ik_checked > 600)):
                ik_checked = time.time()
                ctx.IK.ensure(acc)
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
                STATE['error'] = f'找不到{LABEL}的数据目录'
        except Exception as e:
            STATE['error'] = f'读取出错：{e}'
            log('ingest error', traceback.format_exc())
        INGEST_NOW.wait(30)
        INGEST_NOW.clear()

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
