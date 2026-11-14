"""Read-only access to WeChat 4.x SQLCipher4 databases: decrypt db + WAL into a plain SQLite copy."""
import hashlib, hmac, os, struct
from crypto import AES

PS, RES = 4096, 80

def derive(passphrase, salt):
    return hashlib.pbkdf2_hmac('sha512', passphrase, salt, 256000, 32)

def key_ok(key, page1):
    s = page1[:16]
    mk = hashlib.pbkdf2_hmac('sha512', key, bytes(b ^ 0x3a for b in s), 2, 32)
    return hmac.new(mk, page1[16:PS - 64] + (1).to_bytes(4, 'little'), hashlib.sha512).digest() == page1[PS - 64:]

def _dec_page(key, pg, pgno):
    off = 16 if pgno == 1 else 0
    plain = AES.cbc(key, pg[PS - RES:PS - RES + 16], pg[off:PS - RES])
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
_ZERR = ''
def _zstd():
    """Return (mode, obj). stdlib compression.zstd (3.14+) > pip zstandard > libzstd via ctypes."""
    global _Z, _ZERR
    if _Z is not None:
        return _Z
    try:
        from compression import zstd
        _Z = ('std', zstd)
        return _Z
    except ImportError:
        pass
    try:
        import zstandard
        _Z = ('pip', zstandard.ZstdDecompressor())
        return _Z
    except ImportError:
        pass
    import ctypes, ctypes.util, sys
    names = []
    if sys.platform == 'darwin':
        names += ['/opt/homebrew/opt/zstd/lib/libzstd.dylib', '/usr/local/opt/zstd/lib/libzstd.dylib']
    elif sys.platform == 'win32':
        base = getattr(sys, 'base_prefix', sys.prefix)
        names += [os.path.join(base, 'DLLs', 'libzstd.dll'), 'libzstd.dll']
    else:
        found = ctypes.util.find_library('zstd')
        if found:
            names.append(found)
        names += ['libzstd.so.1', 'libzstd']
    for n in names:
        try:
            L = ctypes.CDLL(n)
            L.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
            L.ZSTD_getFrameContentSize.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
            L.ZSTD_decompress.restype = ctypes.c_size_t
            L.ZSTD_decompress.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
            _Z = ('ctypes', L)
            return _Z
        except OSError:
            continue
    _ZERR = 'zstd 不可用：请 pip install zstandard'
    _Z = ('none', None)
    return _Z

def zstd_ok():
    return _zstd()[0] != 'none'

def unzstd(b):
    if not isinstance(b, (bytes, bytearray)) or b[:4] != b'\x28\xb5\x2f\xfd':
        return b
    mode, z = _zstd()
    if mode == 'none':
        return b
    if mode == 'std':
        try:
            return z.ZstdDecompressor().decompress(b)
        except Exception:
            return b
    if mode == 'pip':
        try:
            return z.decompress(b)
        except Exception:
            return b
    size = z.ZSTD_getFrameContentSize(bytes(b), len(b))
    if size >= (1 << 62):
        size = len(b) * 20
    out = ctypes.create_string_buffer(size)
    r = z.ZSTD_decompress(out, size, bytes(b), len(b))
    return out.raw[:r] if r < (1 << 62) else b
