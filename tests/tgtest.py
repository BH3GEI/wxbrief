#!/usr/bin/env python3
"""Telegram service rules, against a fake adapter (no network, no Telethon calls)."""
import os, shutil, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'content'))
sys.path.insert(0, HERE)
import tg  # noqa: E402

ok = True
def check(name, cond):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name)
    ok = ok and bool(cond)


from tgfake import Fake  # noqa: E402


def make(tmp):
    def db():
        c = sqlite3.connect(tmp + '/brief.db'); c.row_factory = sqlite3.Row; return c
    c = db()
    c.executescript('''create table msgs(id integer primary key, chat text, local_id int, server_id int, ts int, type int,
        sender text, text text, res text, backfill int default 0, unique(chat, local_id));
      create table chats(chat text primary key, name text, last_local int default 0);
      create table kv(k text primary key, v text);
      create table tg_dialogs(chat text primary key, name text, kind text, hash int, last_ts int, username text);
      create table tg_sent(id integer primary key autoincrement, ts int, chat text, chat_name text, body text, via text);''')
    c.commit(); c.close()
    f = Fake(); s = tg.Service(tmp, db, lambda *a: None, adapter=f)
    s.set_config(api_id='123', api_hash='abc', proxy='socks5://127.0.0.1:1080')
    open(tmp + '/tg.session', 'w').write('s')
    return s, f, db


tmp = tempfile.mkdtemp(dir=os.path.join(HERE, '..'))
try:
    s, f, db = make(tmp)
    check('config file is 0600', oct(os.stat(tmp + '/tg.json').st_mode & 0o777) == '0o600')
    try: s.set_config(api_id='abc'); check('bad api_id rejected', False)
    except tg.TGError as e: check('bad api_id rejected', e.code == 'bad_config')
    try: s.set_config(proxy='ftp://x'); check('bad proxy rejected', False)
    except tg.TGError as e: check('bad proxy rejected', e.code == 'bad_proxy')

    d = s.dialogs()
    check('dialogs lists all three, newest first', [x['chat'] for x in d] == ['tg:g9', 'tg:u7', 'tg:me'])
    check('usernames are kept', {x['chat']: x['username'] for x in d}['tg:u7'] == 'xiaoming')
    check('own display name is filled in on first use', s.me_name == '测试用户')
    s.dialogs(); check('dialog list is cached within TTL', f.calls.count('dialogs') == 1)
    s.dialogs(force=True); check('force refreshes dialogs', f.calls.count('dialogs') == 2)

    n = s.fetch('tg:u7', days=30)
    check('first fetch stores all 5 messages', n == 5)
    c = db(); rows = {r['local_id']: r for r in c.execute('select * from msgs where chat="tg:u7"')}
    byid = [r['ts'] for r in c.execute('select ts from msgs where chat="tg:u7" order by id')]; c.close()
    check('row ids follow time (oldest first) even though Telegram returns newest first', byid == sorted(byid))
    check('photo gets a placeholder and a res marker', rows[4]['text'] == '看这张 [图片]' and rows[4]['res'] == 'tg:img')
    check('file with no caption gets name placeholder', rows[5]['text'] == '[文件] 报价.pdf' and rows[5]['res'] == 'tg:file:报价.pdf')
    before = sum(1 for x in f.calls if isinstance(x, tuple) and x[0] == 'messages')
    s.fetch('tg:u7'); check('fetch within TTL is served from cache', sum(1 for x in f.calls if isinstance(x, tuple) and x[0] == 'messages') == before)
    now = int(time.time()); f.msgs[('u', 7)].insert(0, dict(id=6, ts=now, sender='小明', text='新的', media=None, name=''))
    n = s.fetch('tg:u7', force=True)
    check('incremental fetch reads only newer messages (min_id)', n == 1 and f.calls[-1][2] == 5)

    s.fetch('tg:u7', days=1, force=True)
    c = db(); check('days window respected (old ones not fetched on a fresh chat)', c.execute('select count(*) from msgs where chat="tg:g9"').fetchone()[0] == 0); c.close()

    try: s.fetch('tg:u404', force=True); check('unknown chat rejected', False)
    except tg.TGError as e: check('unknown chat rejected', e.code == 'unknown_chat')

    # rate limit: error carries retry_after and later calls fail fast without touching Telegram
    f.flood = 30
    try: s.fetch('tg:u7', force=True); check('rate limit surfaced', False)
    except tg.TGError as e: check('rate limit surfaced with retry_after', e.code == 'rate_limited' and e.retry_after == 30)
    cnt = len(f.calls)
    try: s.fetch('tg:u7', force=True)
    except tg.TGError as e: check('while limited, Telegram is not called again', e.code == 'rate_limited' and len(f.calls) == cnt)
    f.flood = 0; s.flood_until = 0

    # media: lazy, cached, no second download
    c = db(); mid = c.execute('select id from msgs where chat="tg:u7" and local_id=4').fetchone()[0]; c.close()
    check('media not ready before request', not s.media_ready(mid))
    p, mime, name = s.media_file(mid)
    check('media downloaded on request, named by Telethon, with extension', os.path.exists(p) and s.media_ready(mid) and name == 'photo_2026.jpg' and mime == 'image/jpeg')
    check('no temp download dirs left behind', not [x for x in os.listdir(s.media_dir) if x.startswith('.dl')])
    n1 = sum(1 for x in f.calls if isinstance(x, tuple) and x[0] == 'download')
    s.media_file(mid); check('second request uses the cache', sum(1 for x in f.calls if isinstance(x, tuple) and x[0] == 'download') == n1)
    try: s.media_file(999999); check('message without media rejected', False)
    except tg.TGError as e: check('message without media rejected', e.code == 'no_media')

    # cleanup: age and size
    old = os.path.join(s.media_dir, '1_old.bin'); open(old, 'wb').write(b'x' * 100)
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    check('cleanup removes media older than 30 days', s.cleanup() == 1 and not os.path.exists(old))
    for i, age in ((10, 300), (11, 200), (12, 100)):
        q = os.path.join(s.media_dir, f'{i}_f.bin'); open(q, 'wb').write(b'x' * 1000); os.utime(q, (time.time() - age,) * 2)
    removed = s.cleanup(cap=2500)
    left = sorted(x for x in os.listdir(s.media_dir) if x[0] in '12' and x.startswith(('10_', '11_', '12_')))
    check('size cap removes the oldest first', removed >= 1 and '10_f.bin' not in left and '12_f.bin' in left)

    # sending rules
    r = s.send_direct('tg:me', '提醒：发票')
    check('Saved Messages can be sent directly', r['status'] == 'sent' and f.sent[-1] == (('me', 0, None), '提醒：发票'))
    try: s.send_direct('tg:u7', 'hi'); check('direct send to anyone else is refused', False)
    except tg.TGError as e: check('direct send to anyone else is refused', e.code == 'needs_approval' and len(f.sent) == 1)
    r = s.send_approved('tg:u7', '好的，今晚交')
    check('approved send goes through', r['status'] == 'sent' and f.sent[-1][0] == ('u', 7, 111))
    try: s.send_approved('wxid_x', 'hi'); check('WeChat chats cannot be sent', False)
    except tg.TGError as e: check('WeChat chats cannot be sent', e.code == 'bad_chat')
    try: s.send_direct('tg:me', '   '); check('empty text refused', False)
    except tg.TGError as e: check('empty text refused', e.code == 'empty')
    try: s.send_direct('tg:me', 'x' * 5000); check('over-long text refused', False)
    except tg.TGError as e: check('over-long text refused', e.code == 'too_long')
    log = s.sent_log()
    check('every send is logged with how it was sent', [x['via'] for x in log] == ['approved', 'direct'] and log[1]['body'] == '提醒：发票')

    # not logged in
    os.remove(tmp + '/tg.session'); f.st = 'logged_out'
    try: s.fetch('tg:u7', force=True); check('not logged in is reported clearly', False)
    except tg.TGError as e: check('not logged in is reported clearly', e.code == 'not_logged_in')
    os.makedirs(tmp + '/x'); s2 = tg.Service(tmp + '/x', db, lambda *a: None, adapter=Fake())
    c = db(); c.execute('delete from tg_dialogs'); c.commit(); c.close()
    try: s2.dialogs(); check('not configured is reported clearly', False)
    except tg.TGError as e: check('not configured is reported clearly', e.code == 'not_configured')
    # ---- the reported bug: a small early read must never leave a hole in the history
    open(tmp + '/tg.session', 'w').write('s'); f.st = 'ready'; s.dialogs(force=True)    # earlier tests logged out and emptied the list
    now = int(time.time()); DAY = 86400
    f.msgs[('g', 9)] = [dict(id=i, ts=now - (6 * DAY if i <= 2 else 3600 * (10 - i)), sender='甲', text=f'g{i}', media=None, name='') for i in range(6, 0, -1)]
    def ids(chat):
        c = db(); r = [x[0] for x in c.execute('select local_id from msgs where chat=? order by local_id', (chat,))]; c.close(); return r
    def n_calls(): return sum(1 for x in f.calls if isinstance(x, tuple) and x[0] == 'messages')
    # first read: only the newest few (limit), not a whole window
    s.fetch('tg:g9', limit=3)
    check('a first read takes only the newest `limit` messages', ids('tg:g9') == [4, 5, 6])
    s.fetch('tg:g9', days=1, limit=3, force=True)
    check('days=1 fills the last day', ids('tg:g9') == [3, 4, 5, 6])
    s.fetch('tg:g9', days=30)
    check('a later 30 day read backfills the older ones (Saved Messages bug)', ids('tg:g9') == [1, 2, 3, 4, 5, 6])
    c = db(); by_time = [x[0] for x in c.execute('select local_id from msgs where chat="tg:g9" order by ts, id')]
    by_id = [x[0] for x in c.execute('select local_id from msgs where chat="tg:g9" order by id')]; c.close()
    check('backfilled rows get later db ids, so display must sort by time', by_time == [1, 2, 3, 4, 5, 6] and by_id != by_time)
    k = n_calls(); s.fetch('tg:g9', days=30); s.fetch('tg:g9', days=1); s.fetch('tg:g9')
    check('once nothing is missing, repeat reads inside the TTL cost nothing', n_calls() == k)
    c = db(); c.execute("delete from kv where k in ('tg:cover:tg:g9', 'tg:end:tg:g9')"); c.execute('delete from msgs where chat="tg:g9" and local_id<6'); c.commit(); c.close()
    s.fetch('tg:g9')
    check('a cache left half-filled by an older version heals on the next read', ids('tg:g9') == [1, 2, 3, 4, 5, 6])

    # tiny read size: paging back takes several rounds but ends complete
    tg.FETCH_MAX = 2
    try:
        f.msgs[('me', 0)] = [dict(id=i, ts=now - 3600 * (20 - i), sender='我', text=f'n{i}', media=None, name='') for i in range(7, 0, -1)]
        s.fetch('tg:me', days=30, limit=2)
        check('paging back in small rounds still loads the whole window', ids('tg:me') == list(range(1, 8)))
        f.msgs[('me', 0)] = [dict(id=i, ts=now - 600 + i, sender='我', text=f'n{i}', media=None, name='') for i in range(14, 0, -1)]
        s.fetch('tg:me', force=True)
        check('a busy chat (many new messages since last time) leaves no gap', ids('tg:me') == list(range(1, 15)))
    finally:
        tg.FETCH_MAX = 500

    # a days= window has a time budget: it returns what it has, the next call continues
    c = db(); c.execute('delete from msgs where chat="tg:me"'); c.execute("delete from kv where k like 'tg:%:tg:me'"); c.commit(); c.close()
    f.msgs[('me', 0)] = [dict(id=i, ts=now - 20 * DAY + i * 3600, sender='我', text=f'w{i}', media=None, name='') for i in range(400, 0, -1)]
    budget, tg.BUDGET = tg.BUDGET, 0
    try:
        s.fetch('tg:me', days=30, limit=50)
        check('out of time: only what fit (the newest 50) is there, no hang', ids('tg:me') == list(range(351, 401)))
    finally:
        tg.BUDGET = budget
    s.fetch('tg:me', days=30, limit=50)
    check('the next call continues and completes the window', ids('tg:me') == list(range(1, 401)))

    # ---- scrolling up: history older than any window, down to the first message
    c = db(); c.execute('delete from msgs where chat="tg:me"'); c.execute("delete from kv where k like 'tg:%:tg:me'"); c.commit(); c.close()
    f.msgs[('me', 0)] = [dict(id=i, ts=now - 90 * DAY + i, sender='我', text=f'旧{i}', media=None, name='') for i in range(450, 0, -1)]
    s.fetch('tg:me')
    check('a chat quiet for 90 days opens with its newest 200 (not empty, not everything)', ids('tg:me') == list(range(251, 451)))
    check('start of chat is not reported yet', not s.at_start('tg:me'))
    got = s.fetch_older('tg:me', 100)
    check('scrolling up loads the next 100 older messages', got == 100 and ids('tg:me') == list(range(151, 451)))
    s.fetch_older('tg:me', 100)
    check('and the next 100', ids('tg:me') == list(range(51, 451)))
    check('a full batch does not prove the start was reached', not s.at_start('tg:me'))
    s.fetch_older('tg:me', 100)
    check('a short batch marks the start of the chat', s.at_start('tg:me') and ids('tg:me') == list(range(1, 451)))
    k = n_calls(); s.fetch_older('tg:me', 100)
    check('after the start is known, no more requests to Telegram', n_calls() == k)
    check('days=365 no longer re-reads the old history', (s.fetch('tg:me', days=365, force=False), n_calls())[1] == k)
    c = db(); c.execute('delete from msgs where chat="tg:me" and local_id<251'); c.execute("delete from kv where k in ('tg:end:tg:me')"); c.commit(); c.close()
    f.flood = 30; s.flood_until = 0
    try: s.fetch_older('tg:me', 100); check('rate limit while scrolling is reported', False)
    except tg.TGError as e: check('rate limit while scrolling is reported, cache untouched', e.code == 'rate_limited' and ids('tg:me') == list(range(251, 451)))
    f.flood = 0; s.flood_until = 0
    f.msgs[('u', 8)] = []
    c = db(); c.execute("insert into tg_dialogs(chat,name,kind,hash,last_ts,username) values('tg:u8','空会话','u',1,0,null)"); c.commit(); c.close()
    s.fetch('tg:u8')
    check('a chat with no messages at all is marked as fully read', s.at_start('tg:u8') and ids('tg:u8') == [])

    # ---- live search: Telegram's own search over the whole history, returned but never stored
    f.msgs[('u', 7)].insert(0, dict(id=8, ts=int(time.time()) - 5, sender='小明', text='', media='img', name=''))
    c = db(); before = c.execute('select count(*) from msgs').fetchone()[0]; c.close()
    r = s.search_live('很久以前')
    check('live search finds old messages that were never cached', len(r) == 2 and all('很久以前' in x['text'] for x in r))
    check('hits carry message_id, chat, chat name, time, sender and are marked live', r[0]['message_id'] and r[0]['chat'] == 'tg:u7' and r[0]['chat_name'] == '小明' and r[0]['live'] is True and r[0]['ts'] and r[0]['sender'] == '小明')
    c = db(); after = c.execute('select count(*) from msgs').fetchone()[0]; c.close()
    check('live search stores nothing (ids and the cache stay as they were)', before == after)
    k = n_calls(); s.search_live('很久以前')
    check('the same search within a minute is answered from memory', sum(1 for x in f.calls if isinstance(x, tuple) and x[0] == 'search') == 1)
    r2 = s.search_live('很久以前', chat='tg:u7')
    check('a search can be limited to one chat', len(r2) == 2 and all(x['chat'] == 'tg:u7' for x in r2) and f.calls[-1][1][:2] == ('u', 7))
    check('limit is respected', len(s.search_live('msg', limit=1)) == 1)
    try: s.search_live('   '); check('empty keyword refused', False)
    except tg.TGError as e: check('empty keyword refused', e.code == 'empty')
    f.flood = 30; s.flood_until = 0
    try: s.search_live('另一个词'); check('rate limit on search is reported', False)
    except tg.TGError as e: check('rate limit on search is reported with retry_after', e.code == 'rate_limited' and e.retry_after == 30)
    f.flood = 0; s.flood_until = 0
    r3 = s.search_live('不存在的词xyz')
    check('no hits is an empty list, not an error', r3 == [])

    # adapter state: a successful read proves the login, housekeeping calls do not
    import asyncio
    live = tg.Live(lambda *a: None)
    async def one(): return 1
    check('state stays logged_out after a call that does not need a login', (live._run(one(), 5), live.state)[1] == 'logged_out')
    check('state becomes ready after a call that needs a login (reads/sends)', (live._run(one(), 5, login=True), live.state)[1] == 'ready')
    live.state = 'qr'; live._run(one(), 5, login=True)
    check('a QR login in progress is not overwritten', live.state == 'qr')

    # how files are shown when sent from the user account
    check('pictures go as photos, big ones and svg as files', tg.file_kind('a.PNG', 100) == 'photo' and tg.file_kind('a.jpg', 11 << 20) == 'file' and tg.file_kind('a.svg', 10) == 'file')
    check('mp4 goes as a video, others as files', tg.file_kind('v.mp4', 10) == 'video' and tg.file_kind('a.pdf', 10) == 'file')
    check('unreadable video: zeros, no crash', tg.probe_video(b'junk')[:3] == (0, 0, 0))
    if shutil.which('ffmpeg') and shutil.which('ffprobe'):
        import subprocess
        vt = tempfile.mkdtemp()
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=10:duration=2',
                        '-pix_fmt', 'yuv420p', vt + '/t.mp4'], check=True)
        dur, w, h, thumb = tg.probe_video(open(vt + '/t.mp4', 'rb').read())
        shutil.rmtree(vt)
        check('ffprobe reads size and duration, ffmpeg makes a thumbnail', (w, h) == (320, 240) and 1.5 < dur < 2.5 and thumb and thumb[:2] == b'\xff\xd8')
finally:
    import shutil; shutil.rmtree(tmp, ignore_errors=True)

print('ALL PASS' if ok else 'FAILURES')
sys.exit(0 if ok else 1)
