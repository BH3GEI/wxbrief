#!/usr/bin/env python3
"""Unit checks for code paths the synthetic e2e test does not reach (no real WeChat data).

- wxdb.unzstd on every zstd backend available here, including the libzstd/ctypes fallback
- keys.Keys.key_for deriving a per-db key from a saved passphrase and persisting keys.json
- media._scan brute-forcing the image key over a small range

Run from the repo root:  python tests/unit_test.py
"""
import json, os, shutil, sys, tempfile

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'content'))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix='wxbrief-unit-')
os.environ['VAR'] = TMP

import wxdb  # noqa: E402

# zstd frame of ('wxid_a:\n你好，zstd 测试' * 3), as stored in message_content
ZST = bytes.fromhex('28b52ffd20541d0100e0777869645f613a0ae4bda0e5a5bdefbc8c7a73746420e6b58be8af95'
                    '0100f4cfe904')
PLAIN = 'wxid_a:\n你好，zstd 测试'.encode() * 3


def test_unzstd():
    tried = []
    first = wxdb._zstd()
    if first[0] != 'none':
        assert wxdb.unzstd(ZST) == PLAIN, first[0]
        tried.append(first[0])
    # force the ctypes fallback too, when a libzstd is around
    saved = wxdb._Z
    try:
        blocked = {m: sys.modules.pop(m, None) for m in ('compression.zstd', 'zstandard')}
        sys.modules['compression.zstd'] = None
        sys.modules['zstandard'] = None
        pkg = sys.modules.get('compression')
        pkg_zstd = pkg.__dict__.pop('zstd', None) if pkg else None
        wxdb._Z = None
        mode = wxdb._zstd()[0]
        if mode == 'ctypes':
            assert wxdb.unzstd(ZST) == PLAIN, 'ctypes zstd'
            tried.append('ctypes')
        for m, v in blocked.items():
            if v is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = v
        if pkg_zstd is not None:
            pkg.zstd = pkg_zstd
    finally:
        wxdb._Z = saved
    assert wxdb.unzstd(b'plain text') == b'plain text'
    print('unzstd OK via', ', '.join(tried) or 'nothing (no zstd backend here)')


def test_key_derive():
    import keys
    from synthtest import enc_page, PS
    passphrase = os.urandom(32)
    salt = os.urandom(16)
    k = wxdb.derive(passphrase, salt)
    page1 = bytearray(PS)
    page1[:16] = b'SQLite format 3\x00'
    db = os.path.join(TMP, 'message_0.db')
    with open(db, 'wb') as f:
        f.write(enc_page(k, salt, bytes(page1), 1))
    with open(os.path.join(TMP, 'pass.bin'), 'wb') as f:
        f.write(passphrase)
    K = keys.Keys(lambda *a: None)
    assert K.key_for(db) == k, 'derived key does not open the db'
    saved = json.load(open(os.path.join(TMP, 'keys.json')))
    assert saved == {salt.hex(): [k.hex()]}, 'keys.json not written'
    assert keys.Keys(lambda *a: None).key_for(db) == k, 'cached key not reused'
    print('key derive + keys.json OK')


def test_image_scan():
    import media
    # key = md5(str((0x1234 << 8) | 0x56) + 'wxid_test')[:16]; block = AES-128-ECB(key, 16 * 0x10)
    block = bytes.fromhex('92eba5216b8318568d1de455ef3905ac')
    assert media._scan((0x1200, 0x1240, 0x56, 'wxid_test', block)) == 'e18be4f83bfd72a3'
    assert media._scan((0x1300, 0x1310, 0x56, 'wxid_test', block)) is None
    assert media.key_matches(b'e18be4f83bfd72a3', block)
    print('image key scan OK')


if __name__ == '__main__':
    try:
        test_unzstd()
        test_key_derive()
        test_image_scan()
        print('ALL UNIT CHECKS PASSED')
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
