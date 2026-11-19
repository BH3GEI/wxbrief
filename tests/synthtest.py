#!/usr/bin/env python3
"""Synthetic end-to-end test for wxbrief (no real WeChat data).

Builds a fake xwechat_files tree with two SQLCipher-4-encrypted databases (contact + message)
with 80 reserved bytes/page, pre-seeds the key cache, runs app.py, checks the JSON API incl.
the suggested-reply drafts. Key values are never printed.

Run from the repo root:  python tests/synthtest.py
"""
import hashlib, hmac, json, os, re, shutil, subprocess, sys, time
import urllib.request, urllib.parse, urllib.error
import ctypes

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONTENT = os.path.join(HERE, '..', 'content')
sys.path.insert(0, CONTENT)

import wxdb  # noqa: E402
from crypto import AES  # noqa: E402

ROOT = os.path.join(HERE, '.synth')
XW = os.path.join(ROOT, 'xwechat_files')
ACC = os.path.join(XW, 'wxid_test0001_ab12')
VAR = os.path.join(ROOT, 'var')
WORK = os.path.join(ROOT, 'work')
PORT = int(os.environ.get('TEST_PORT', '3999'))
PS, RES = 4096, 80


def sqlite_lib():
    if sys.platform == 'darwin':
        return ctypes.CDLL('/usr/lib/libsqlite3.dylib')
    if sys.platform == 'win32':
        for n in (os.path.join(sys.base_prefix, 'DLLs', 'sqlite3.dll'), 'sqlite3.dll'):
            try:
                return ctypes.CDLL(n)
            except OSError:
                continue
        raise RuntimeError('sqlite3.dll not found')
    return ctypes.CDLL('libsqlite3.so.0')


def raw_db(path, sql):
    """Create a db with 80 reserved bytes/page using the C API (python sqlite3 can't do it).

    Note: on Windows, python's sqlite3.dll crashes in sqlite3_open unless the library has been
    initialized first (importing the sqlite3 module does that)."""
    import sqlite3
    sqlite3.connect(':memory:').close()   # ensure sqlite3_initialize on every platform
    lib = sqlite_lib()
    db = ctypes.c_void_p()
    lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p]
    lib.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.sqlite3_close.argtypes = [ctypes.c_void_p]
    assert lib.sqlite3_open(path.encode(), ctypes.byref(db)) == 0
    n = ctypes.c_int(RES)
    assert lib.sqlite3_file_control(db, b'main', 38, ctypes.byref(n)) == 0   # RESERVE_BYTES
    for s in sql:
        assert lib.sqlite3_exec(db, s.encode(), None, None, None) == 0, s
    assert lib.sqlite3_exec(db, b'vacuum', None, None, None) == 0
    lib.sqlite3_close(db)
    with open(path, 'rb') as f:
        assert f.read(100)[20] == 80, 'reserved_bytes not applied'


def aes_cbc_encrypt(key, iv, data):
    """EVP AES-256-CBC encrypt, no padding (test-side only; the app itself only decrypts)."""
    if sys.platform == 'darwin':
        # CommonCrypto encrypt: CCCryptorCreateWithMode op=0
        lib = ctypes.CDLL('/usr/lib/system/libcommonCrypto.dylib')
        lib.CCCryptorCreateWithMode.argtypes = [ctypes.c_int] * 4 + \
            [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
             ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        lib.CCCryptorUpdate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                        ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        c = ctypes.c_void_p()
        assert lib.CCCryptorCreateWithMode(0, 2, 0, 0, iv, key, len(key), None, 0, 0, 0,
                                           ctypes.byref(c)) == 0
        try:
            out = ctypes.create_string_buffer(len(data) + 32)
            n = ctypes.c_size_t()
            assert lib.CCCryptorUpdate(c, data, len(data), out, len(out), ctypes.byref(n)) == 0
            return out.raw[:n.value]
        finally:
            lib.CCCryptorRelease(c)
    L = AES.lib
    L.EVP_EncryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_char_p, ctypes.c_char_p]
    L.EVP_EncryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
                                    ctypes.c_char_p, ctypes.c_int]
    c = L.EVP_CIPHER_CTX_new()
    try:
        L.EVP_EncryptInit_ex(c, L.EVP_aes_256_cbc(), None, key, iv)
        L.EVP_CIPHER_CTX_set_padding(c, 0)
        out = ctypes.create_string_buffer(len(data) + 32)
        n = ctypes.c_int()
        L.EVP_EncryptUpdate(c, out, ctypes.byref(n), data, len(data))
        return out.raw[:n.value]
    finally:
        L.EVP_CIPHER_CTX_free(c)


def enc_page(key, salt, plain, pgno):
    iv = os.urandom(16)
    body = plain[16:PS - RES] if pgno == 1 else plain[:PS - RES]
    enc = aes_cbc_encrypt(key, iv, body)
    page = bytearray(PS)
    if pgno == 1:
        page[:16] = salt
        page[16:PS - RES] = enc
    else:
        page[:PS - RES] = enc
    page[PS - RES:PS - RES + 16] = iv
    mk = hashlib.pbkdf2_hmac('sha512', key, bytes(b ^ 0x3a for b in salt), 2, 32)
    mac_data = bytes(page[16:PS - 64]) + pgno.to_bytes(4, 'little')
    page[PS - 64:] = hmac.new(mk, mac_data, hashlib.sha512).digest()
    return bytes(page)


def encrypt_db(src_plain, key, dst):
    salt = os.urandom(16)
    with open(src_plain, 'rb') as f:
        data = f.read()
    with open(dst, 'wb') as f:
        for i in range(len(data) // PS):
            f.write(enc_page(key, salt, data[i * PS:(i + 1) * PS], i + 1))
    return salt.hex()


def main():
    shutil.rmtree(ROOT, ignore_errors=True)
    for d in (os.path.join(ACC, 'db_storage', 'message'), os.path.join(ACC, 'db_storage', 'contact'),
              VAR, WORK):
        os.makedirs(d, exist_ok=True)

    cp = os.path.join(ROOT, 'contact_plain.db')
    raw_db(cp, ['create table contact(username text, remark text, nick_name text, alias text, local_type int);',
                'create table stranger(username text, nick_name text);',
                'insert into contact values ("wxid_friend1", "老王", "王哥", "wang01", 1), ("chat1@chatroom", "", "群聊", NULL, 2),'
                ' ("gh_news", "", "某公众号", NULL, 4);',
                'insert into stranger values ("wxid_stranger1", "路人");'])
    key1 = os.urandom(32)
    salt1 = encrypt_db(cp, key1, os.path.join(ACC, 'db_storage', 'contact', 'contact.db'))

    mp = os.path.join(ROOT, 'msg_plain.db')
    chat = 'chat1@chatroom'
    t = 'Msg_' + hashlib.md5(chat.encode()).hexdigest()
    now = int(time.time())
    raw_db(mp, ['create table Name2Id(rowid integer primary key, user_name text);',
                'insert into Name2Id values (10, "chat1@chatroom"), (11, "wxid_friend1"), (12, "wxid_test0001");',
                f'create table {t}(local_id integer primary key, server_id int, local_type int, '
                'real_sender_id int, create_time int, message_content blob, packed_info_data blob);',
                f'insert into {t} values (1,101,1,12,{now - 300},"wxid_friend1:\n在吗",null);',
                f'insert into {t} values (2,102,1,12,{now - 240},"wxid_test0001:\n在的",null);',
                f'insert into {t} values (3,103,1,11,{now - 180},"看看这个文件",null);',
                f'insert into {t} values (4,104,49,11,{now - 120},'
                '"<msg><appmsg><title>报告.pdf</title><type>6</type></appmsg></msg>",null);'])
    key2 = os.urandom(32)
    salt2 = encrypt_db(mp, key2, os.path.join(ACC, 'db_storage', 'message', 'message_0.db'))
    json.dump({salt1: key1.hex(), salt2: key2.hex()}, open(os.path.join(VAR, 'keys.json'), 'w'))

    env = dict(os.environ, VAR=VAR, WORK=WORK, WECHAT_DIR=XW, PORT=str(PORT), PYTHONDONTWRITEBYTECODE='1')
    pr = subprocess.Popen([sys.executable, '-u', os.path.join(CONTENT, 'app.py')], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        base = f'http://127.0.0.1:{PORT}'
        up = False
        for _ in range(60):
            time.sleep(1)
            try:
                up = urllib.request.urlopen(base + '/', timeout=3).status == 200
                break
            except Exception:
                pass
        assert up, 'server never came up'
        req = urllib.request.Request(base + '/keys/create', data=urllib.parse.urlencode({'name': 'test'}).encode(),
                                     headers={'Origin': base})
        key_page = urllib.request.urlopen(req).read().decode()
        m = re.search(r'Bearer ([0-9a-zA-Z_\-]+)', key_page)
        assert m, 'no api key on keys page'
        ak = m.group(1)
        H = {'Authorization': 'Bearer ' + ak}

        def get(p):
            return json.load(urllib.request.urlopen(urllib.request.Request(base + p, headers=H)))

        chats = get('/api/chats')
        assert any(c['chat'] == chat for c in chats), 'chat not ingested'
        msgs = get('/api/messages?chat=' + urllib.parse.quote(chat))
        for msg in msgs:
            print('msg:', {k: msg[k] for k in ('sender', 'text')})
        texts = [m2['text'] for m2 in msgs]
        assert any('在吗' in x for x in texts), 'text message missing'
        assert any('文件' in x and '报告.pdf' in x for x in texts), 'file message missing'
        assert get('/api/search?q=' + urllib.parse.quote('文件')), 'search empty'
        print('messages/search OK')
        cs = {c['id']: c for c in get('/api/contacts?source=wechat')}
        assert cs['wxid_friend1']['remark'] == '老王' and cs['wxid_friend1']['alias'] == 'wang01' and cs['wxid_friend1']['type'] == 1, cs
        assert cs['chat1@chatroom']['kind'] == 'group' and cs['chat1@chatroom']['has_chat'] is True, cs
        assert 'gh_news' not in cs, 'official account shown by default'
        assert [c['id'] for c in get('/api/contacts?source=wechat&q=wang01')] == ['wxid_friend1']
        print('contacts OK (read from the encrypted contact database)')

        payload = json.dumps({'chat': chat, 'body': '在的，稍等我看一下', 'note': '由测试写入'}).encode()
        req = urllib.request.Request(base + '/api/drafts', data=payload, method='POST',
                                     headers={**H, 'Content-Type': 'application/json'})
        d = json.load(urllib.request.urlopen(req))
        assert d.get('id'), f'draft create failed: {d}'
        dup = json.load(urllib.request.urlopen(req))
        assert dup.get('duplicate'), 'duplicate not detected'
        listed = get('/api/drafts')
        assert any(x['id'] == d['id'] for x in listed), 'draft not listed'
        home = urllib.request.urlopen(base + '/').read().decode()
        assert '在的，稍等我看一下' in home and '建议回复' in home, 'draft not pinned on home'
        chatpage = urllib.request.urlopen(base + '/chat?c=' + urllib.parse.quote(chat)).read().decode()
        assert '在的，稍等我看一下' in chatpage, 'draft not shown in chat'
        req = urllib.request.Request(base + f"/api/drafts/{d['id']}", method='DELETE', headers=H)
        assert json.load(urllib.request.urlopen(req)) == {'ok': True}
        assert not any(x['id'] == d['id'] for x in get('/api/drafts')), 'draft not dismissed'
        assert '在的，稍等我看一下' not in urllib.request.urlopen(base + '/').read().decode()
        print('drafts OK (create/dedupe/pin/dismiss)')
        big = json.dumps({'chat': chat, 'note': '长' * 30000, 'body': '大请求体的最后一个字段'}).encode()   # \uXXXX escapes: ~180 KB
        assert len(big) > 64 * 1024
        d = json.load(urllib.request.urlopen(urllib.request.Request(base + '/api/drafts', data=big, method='POST',
                                                                    headers={**H, 'Content-Type': 'application/json'})))
        assert d.get('id') and d['body'] == '大请求体的最后一个字段', f'JSON body over 64 KB not read whole: {d}'
        urllib.request.urlopen(urllib.request.Request(base + f"/api/drafts/{d['id']}", method='DELETE', headers=H))
        print('JSON body over 64 KB OK')
        try:
            urllib.request.urlopen(urllib.request.Request(base + '/api/drafts', data=payload, method='POST',
                                                          headers={'Content-Type': 'application/json'}))
            raise AssertionError('unauthorized draft accepted')
        except urllib.error.HTTPError as e:
            assert e.code == 401
        print('ALL SYNTHETIC CHECKS PASSED')
    finally:
        pr.terminate()
        try:
            print(pr.stdout.read()[-1500:])
        except Exception:
            pass
        shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == '__main__':
    main()
