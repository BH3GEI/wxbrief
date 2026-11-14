"""Read-only access to WeChat 4.x (Linux) SQLCipher4 databases: decrypt db + WAL into a plain SQLite copy."""
import ctypes, ctypes.util, hashlib, hmac, os, struct

PS, RES = 4096, 80
_L = ctypes.CDLL(ctypes.util.find_library('crypto') or 'libcrypto.so.3')
_L.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
_L.EVP_aes_256_cbc.restype = ctypes.c_void_p
_L.EVP_DecryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
_L.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
_L.EVP_DecryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_int]
_L.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]

def _aes(key, iv, data):
    c = _L.EVP_CIPHER_CTX_new()
    try:
        _L.EVP_DecryptInit_ex(c, _L.EVP_aes_256_cbc(), None, key, iv)
        _L.EVP_CIPHER_CTX_set_padding(c, 0)
        out = ctypes.create_string_buffer(len(data) + 32); n = ctypes.c_int()
        _L.EVP_DecryptUpdate(c, out, ctypes.byref(n), data, len(data))
        return out.raw[:n.value]
    finally:
        _L.EVP_CIPHER_CTX_free(c)

def derive(passphrase, salt):
    return hashlib.pbkdf2_hmac('sha512', passphrase, salt, 256000, 32)

def key_ok(key, page1):
    s = page1[:16]
    mk = hashlib.pbkdf2_hmac('sha512', key, bytes(b ^ 0x3a for b in s), 2, 32)
    return hmac.new(mk, page1[16:PS - 64] + (1).to_bytes(4, 'little'), hashlib.sha512).digest() == page1[PS - 64:]

def _dec_page(key, pg, pgno):
    off = 16 if pgno == 1 else 0
    plain = _aes(key, pg[PS - RES:PS - RES + 16], pg[off:PS - RES])
    return (b'SQLite format 3\x00' if pgno == 1 else b'') + plain + b'\x00' * RES

def decrypt_to(src, key, dst):
    """Decrypt src (+ src-wal committed frames) into plain SQLite file dst. Returns page count."""
    pages = {}
    with open(src, 'rb') as f:
        data = f.read()
    n = len(data) // PS
    for i in range(n):
        pages[i + 1] = data[i * PS:(i + 1) * PS]
    wal = src + '-wal'
    if os.path.exists(wal):
        with open(wal, 'rb') as f:
            w = f.read()
        if len(w) >= 32:
            s1, s2 = struct.unpack_from('>II', w, 16)
            fs = 24 + PS; pending = {}; off = 32
            while off + fs <= len(w):
                pgno, commit, a, b = struct.unpack_from('>IIII', w, off)
                if (a, b) != (s1, s2):
                    break
                pending[pgno] = w[off + 24:off + fs]
                if commit:
                    pages.update(pending); pending = {}; n = max(n, commit)
                off += fs
    tmp = dst + '.part'
    with open(tmp, 'wb') as f:
        for p in range(1, n + 1):
            pg = pages.get(p)
            f.write(_dec_page(key, pg, p) if pg else b'\x00' * PS)
    os.replace(tmp, dst)
    return n

_Z = None
def unzstd(b):
    global _Z
    if not isinstance(b, (bytes, bytearray)) or b[:4] != b'\x28\xb5\x2f\xfd':
        return b
    if _Z is None:
        _Z = ctypes.CDLL(ctypes.util.find_library('zstd') or 'libzstd.so.1')
        _Z.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        _Z.ZSTD_getFrameContentSize.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        _Z.ZSTD_decompress.restype = ctypes.c_size_t
        _Z.ZSTD_decompress.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
    size = _Z.ZSTD_getFrameContentSize(bytes(b), len(b))
    if size >= (1 << 62):
        size = len(b) * 20
    out = ctypes.create_string_buffer(size)
    r = _Z.ZSTD_decompress(out, size, bytes(b), len(b))
    return out.raw[:r] if r < (1 << 62) else b
