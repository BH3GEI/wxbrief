"""Shared state: paths, the log, the store, and the long-lived services (set up by app.main)."""
import os, sqlite3, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runenv

TGS = None       # tg.Service
APIKEYS = None   # apikeys.ApiKeys
IK = None        # media.ImageKey
KEYS = None      # keys.Keys (WeChat database keys)


VAR = runenv.var_dir()

WECHAT = runenv.find_wechat_dir()

WORK = runenv.work_dir(VAR)

STORE = VAR + '/brief.db'

LOGF = VAR + '/brief.log'

LABEL = runenv.wx_label()

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
    create table if not exists drafts(id integer primary key autoincrement, chat text not null,
        body text not null, note text, created int, hidden int default 0);
    create index if not exists drafts_list on drafts(hidden, created desc);
    create index if not exists drafts_chat on drafts(chat, hidden);
    create table if not exists tg_dialogs(chat text primary key, name text, kind text, hash int, last_ts int, username text);
    create table if not exists contacts(source text, id text, name text, remark text, nickname text, alias text, username text,
        kind text, type int, primary key(source, id));
    create index if not exists contacts_name on contacts(source, kind, name);
    create table if not exists tg_sent(id integer primary key autoincrement, ts int, chat text, chat_name text,
        body text, via text);
    ''')
    if 'type' not in [r[1] for r in c.execute('pragma table_info(contacts)')]:
        c.execute('alter table contacts add column type int')
    if 'username' not in [r[1] for r in c.execute('pragma table_info(tg_dialogs)')]:
        c.execute('alter table tg_dialogs add column username text')
        c.execute("delete from kv where k='tg:dialogs_at'")      # refetch once so usernames get filled in
    if 'backfill' not in [r[1] for r in c.execute('pragma table_info(msgs)')]:
        c.execute('alter table msgs add column backfill int default 0')
        # Telegram rows cached so far may include history filled in after newer rows: keep them out of after_id pulls
        c.execute("update msgs set backfill=1 where chat like 'tg:%'")
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

INGEST_NOW = threading.Event()    # set by the refresh button to skip the 30 s wait
