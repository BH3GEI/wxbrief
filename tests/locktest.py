#!/usr/bin/env python3
"""Regression: slow steps (decrypting the next WeChat database, waiting for Telegram) must not hold the
write lock on the store, or a page request that writes at the same time fails with "database is locked"."""
import hashlib, os, shutil, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(dir=os.path.join(HERE, '..'))
XW = TMP + '/xw'; ACC = XW + '/wxid_lock_ab12'
os.environ.update(VAR=TMP + '/var', WORK=TMP + '/work', WECHAT_DIR=XW)
sys.path.insert(0, os.path.join(HERE, '..', 'content')); sys.path.insert(0, HERE)
import ctx, wechat, wxdb, tg  # noqa: E402
from tgfake import Fake  # noqa: E402

ok = True
def check(name, cond, extra=''):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name + (f'  [{extra}]' if not cond and extra else ''))
    ok = ok and bool(cond)

def can_write_now():
    """Another writer (a page request) trying to write right now, without waiting."""
    c = sqlite3.connect(ctx.STORE, timeout=0.2)
    try:
        c.execute('begin immediate'); c.rollback(); return True
    except sqlite3.OperationalError:
        return False
    finally:
        c.close()

def plain_db(path, sql):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path); c.executescript(sql); c.commit(); c.close()

try:
    os.makedirs(TMP + '/var'); now = int(time.time())
    plain_db(ACC + '/db_storage/contact/contact.db',
             'create table contact(username text, remark text, nick_name text, alias text, local_type int);'
             "insert into contact values('wxid_a','','甲',null,1),('wxid_b','','乙',null,1),('wxid_c','','丙',null,1);")
    for n, chats in ((0, ('wxid_a', 'wxid_b')), (1, ('wxid_c',))):
        sql = 'create table Name2Id(rowid integer primary key, user_name text);'
        for i, ch in enumerate(chats):
            t = 'Msg_' + hashlib.md5(ch.encode()).hexdigest()
            sql += (f"insert into Name2Id values({i + 1}, '{ch}');"
                    f'create table {t}(local_id integer primary key, server_id int, local_type int, real_sender_id int, '
                    'create_time int, message_content blob, packed_info_data blob);'
                    + ''.join(f"insert into {t} values({j},{j},1,{i + 1},{now - 100 + j},'消息{j}',null);" for j in range(1, 4)))
        plain_db(ACC + f'/db_storage/message/message_{n}.db', sql)

    ctx.init_store()
    probes = []
    def fake_decrypt(src, key, dst):          # the slow step in real life
        assert os.path.abspath(src).startswith(TMP), 'test touched a WeChat directory outside its temp dir: ' + src
        probes.append((os.path.basename(src), can_write_now()))
        shutil.copy(src, dst)
    wxdb.decrypt_to = fake_decrypt
    class K:
        def key_for(self, path): return b'k' * 32
    n = wechat.ingest(K())
    c = ctx.db(); stored = c.execute('select count(*) from msgs').fetchone()[0]; c.close()
    check('WeChat ingest stored every message', n == 9 and stored == 9, str((n, stored)))
    later = [ok_ for name, ok_ in probes if name.startswith('message_1')]
    check('while the next WeChat database is decrypted, others can write', later == [True], str(probes))

    # Telegram: several rounds of network calls with stores in between
    tg.FETCH_MAX = 2
    f = Fake(); s = tg.Service(TMP + '/var', ctx.db, lambda *a: None, adapter=f)
    s.set_config(api_id='1', api_hash='h'); open(TMP + '/var/tg.session', 'w').write('s')
    s.dialogs()
    f.msgs[('u', 7)] = [dict(id=i, ts=now - 3600 + i, sender='小明', text=f't{i}', media=None, name='') for i in range(7, 0, -1)]
    seen = []
    orig = f.messages
    def probing(*a, **k):
        seen.append(can_write_now())
        return orig(*a, **k)
    f.messages = probing
    s.fetch('tg:u7', days=30)
    c = ctx.db(); got = c.execute("select count(*) from msgs where chat='tg:u7'").fetchone()[0]; c.close()
    check('Telegram fetch in small rounds stored everything', got == 7, str(got))
    check('while waiting for Telegram, others can write', len(seen) >= 3 and all(seen), str(seen))
    f.msgs[('u', 7)] += [dict(id=i, ts=now - 10 + i, sender='小明', text=f'n{i}', media=None, name='') for i in range(10, 16)]
    seen.clear(); s.fetch('tg:u7', force=True)
    check('also when reading newer messages in rounds', len(seen) >= 2 and all(seen), str(seen))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print('ALL PASS' if ok else 'FAILURES')
sys.exit(0 if ok else 1)
