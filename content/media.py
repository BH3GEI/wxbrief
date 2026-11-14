"""Find and decode the original media behind a stored message (images, videos, files). Read-only.

WeChat 4.x keeps media next to the databases (all platforms share the xwechat_files layout):
  images  msg/attach/<md5(chat)>/<YYYY-MM>/Img/<hash>{,_h,_t}.dat   (encrypted .dat, see decode_dat)
  videos  msg/video/<YYYY-MM>/<hash>.mp4 and <hash>_thumb.jpg      (plain)
  files   msg/file/<YYYY-MM>/<title>                               (plain)
<hash> comes from the message's packed_info_data; the month folder may differ from our local time zone,
so lookups glob over all months.

Image .dat V2 layout: 6-byte signature, aes_len and xor_len (uint32 LE), 1 byte, then
AES-128-ECB(PKCS7) of the first aes_len bytes, the middle in clear, and the last xor_len bytes XORed
with one byte. On this WeChat build the AES key is md5(str(uin) + wxid_without_suffix)[:16] and the XOR
byte is uin & 0xff; the low byte is recovered from a JPEG tail and the other 24 bits are brute-forced
against a thumbnail's padding block (an all-0x10 block, so a match is exact).
"""
import ctypes, ctypes.util, glob, hashlib, json, os, re, struct, subprocess, threading, time
import multiprocessing as mp
from collections import Counter

from crypto import AES, OpenSsl
import runenv

VAR = runenv.var_dir()
CACHE = runenv.cache_dir(VAR)
IMGKEY = VAR + '/imgkey.json'
SIG_V1, SIG_V2 = b'\x07\x08V1\x08\x07', b'\x07\x08V2\x08\x07'
V1KEY = b'cfcd208495d565ef'
PAD = b'\x10' * 16

def aes_ecb(key, data):
    return AES.ecb(key, data)


# ---------------- message -> resource reference ----------------
_HASH = re.compile(rb'[0-9a-f]{32}')

def packed_hash(pk):
    if not isinstance(pk, (bytes, bytearray)):
        return None
    m = _HASH.search(pk)
    return m.group().decode() if m else None


# ---------------- image key ----------------
def _enc_len(al):
    return al + (16 - al % 16)

def _probe(acc):
    """(padding block, xor byte) from the account's V2 thumbnails, or None."""
    blocks, xors = Counter(), Counter()
    for f in glob.glob(acc + '/msg/attach/*/*/Img/*.dat')[:400]:
        with open(f, 'rb') as fh:
            b = fh.read()
        if b[:6] != SIG_V2 or len(b) < 64:
            continue
        al, xl = struct.unpack_from('<II', b, 6)
        if al % 16 == 0:
            e = _enc_len(al)
            blocks[b[15 + e - 16:15 + e]] += 1
        if xl >= 2 and b[-2] ^ 0xff == b[-1] ^ 0xd9:   # JPEG ends with FF D9
            xors[b[-1] ^ 0xd9] += 1
    if not blocks or not xors:
        return None
    return blocks.most_common(1)[0][0], xors.most_common(1)[0][0]

def _scan(args):
    lo, hi, xor, wx, block = args
    lib = getattr(AES, 'lib', None) if isinstance(AES, OpenSsl) else None
    if lib is None:   # CommonCrypto (macOS): no reusable context, go through the wrapper
        for h in range(lo, hi):
            k = hashlib.md5((str((h << 8) | xor) + wx).encode()).hexdigest()[:16].encode()
            if AES.ecb(k, block) == PAD:
                return k.decode()
        return None
    c = lib.EVP_CIPHER_CTX_new(); E = lib.EVP_aes_128_ecb()
    out = ctypes.create_string_buffer(32); n = ctypes.c_int()
    try:
        for h in range(lo, hi):
            k = hashlib.md5((str((h << 8) | xor) + wx).encode()).hexdigest()[:16].encode()
            lib.EVP_DecryptInit_ex(c, E, None, k, None); lib.EVP_CIPHER_CTX_set_padding(c, 0)
            lib.EVP_DecryptUpdate(c, out, ctypes.byref(n), block, 16)
            if out.raw[:16] == PAD:
                return k.decode()
    finally:
        lib.EVP_CIPHER_CTX_free(c)
    return None

def key_matches(key, block):
    return aes_ecb(key, block) == PAD


class ImageKey:
    def __init__(self, log):
        self.log = log
        self.key = None; self.xor = None
        self.state = 'none'
        self.lock = threading.Lock()
        try:
            d = json.load(open(IMGKEY))
            self.key, self.xor, self.wxid = d['key'].encode(), d['xor'], d['wxid']
            self.state = 'ok'
        except Exception:
            self.wxid = ''

    def ensure(self, acc):
        """Called from the ingest loop. Verifies the cached key, derives a new one if needed."""
        with self.lock:
            if self.state == 'deriving':
                return
            wx = os.path.basename(acc)
            pr = _probe(acc)
            if pr is None:
                self.state = 'no_images'
                return
            block, xor = pr
            if self.key and self.wxid == wx and key_matches(self.key, block):
                self.state = 'ok'
                return
            self.state = 'deriving'
        threading.Thread(target=self._derive, args=(wx, block, xor), daemon=True).start()

    def _derive(self, wx, block, xor):
        base = re.sub(r'_[0-9a-f]{4}$', '', wx)
        t0 = time.time(); found = None
        step = 1 << 16
        jobs = [(i, i + step, xor, w, block) for w in (base, wx) for i in range(0, 1 << 24, step)]
        try:
            with mp.get_context('spawn').Pool(max(1, (os.cpu_count() or 2) - 1)) as pool:
                for r in pool.imap_unordered(_scan, jobs):
                    if r:
                        found = r; pool.terminate(); break
        except Exception as e:
            self.log('imgkey: derive error', e)
        with self.lock:
            if found:
                self.key, self.xor, self.wxid, self.state = found.encode(), xor, wx, 'ok'
                tmp = IMGKEY + '.tmp'
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as f:
                    json.dump({'key': found, 'xor': xor, 'wxid': wx}, f)
                os.replace(tmp, IMGKEY)
                self.log(f'imgkey: derived in {time.time() - t0:.0f}s')
            else:
                self.state = 'failed'
                self.log('imgkey: no key found; images stay unavailable')


# ---------------- decoding ----------------
_MAGIC = [(b'\xff\xd8\xff', 'image/jpeg'), (b'\x89PNG', 'image/png'), (b'GIF8', 'image/gif'),
          (b'RIFF', 'image/webp'), (b'wxgf', 'image/wxgf'), (b'BM', 'image/bmp')]

def sniff(d):
    for m, t in _MAGIC:
        if d[:len(m)] == m:
            return t
    return None

def decode_dat(b, ik):
    """Encrypted .dat bytes -> plain image bytes, or None."""
    if b[:6] in (SIG_V1, SIG_V2):
        key = V1KEY if b[:6] == SIG_V1 else ik.key
        if key is None:
            return None
        al, xl = struct.unpack_from('<II', b, 6)
        e = _enc_len(al)
        p = aes_ecb(key, b[15:15 + e])
        if not p or not 0 < p[-1] <= 16:
            return None
        p = p[:-p[-1]]
        x = ik.xor if ik.xor is not None else 0
        return p + b[15 + e:len(b) - xl] + bytes(c ^ x for c in b[len(b) - xl:])
    for m, _ in _MAGIC:   # pre-4.0 format: the whole file XORed with one byte
        x = b[0] ^ m[0]
        if all(b[i] ^ x == m[i] for i in range(len(m))):
            return bytes(c ^ x for c in b)
    return None

def wxgf_to_jpeg(d, cache_name):
    """WeChat's HEVC wrapper -> JPEG via ffmpeg (optional), cached."""
    path = f'{CACHE}/{cache_name}.jpg'
    if os.path.exists(path):
        return open(path, 'rb').read()
    i = d.find(b'\x00\x00\x00\x01')
    if i < 0:
        return None
    try:
        r = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'hevc', '-i', '-', '-frames:v', '1', '-q:v', '3',
                            '-f', 'image2', '-c:v', 'mjpeg', '-'], input=d[i:], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None   # ffmpeg not installed
    if r.returncode != 0 or r.stdout[:3] != b'\xff\xd8\xff':
        return None
    os.makedirs(CACHE, exist_ok=True)
    tmp = path + '.part'
    with open(tmp, 'wb') as f:
        f.write(r.stdout)
    os.replace(tmp, path)
    return r.stdout


# ---------------- lookup ----------------
def _first(pattern):
    g = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return g[0] if g else None

def _img_files(acc, chat, h, thumb):
    base = f'{acc}/msg/attach/{hashlib.md5(chat.encode()).hexdigest()}/*/Img/{h}'
    order = ('_t', '', '_h') if thumb else ('', '_h', '_t')
    return [(s, p) for s in order for p in [_first(base + s + '.dat')] if p]

def _safe_name(title):
    n = os.path.basename((title or '').replace('\\', '/')).strip()
    return n if n and n not in ('.', '..') else None

def availability(acc, chat, res):
    """'full', 'thumb' or None (original not downloaded by this WeChat)."""
    if not acc or not res:
        return None
    kind, _, ref = res.partition(':')
    if kind == 'img':
        got = {s for s, _ in _img_files(acc, chat, ref, False)}
        return 'full' if got & {'', '_h'} else ('thumb' if got else None)
    if kind == 'vid':
        if _first(f'{acc}/msg/video/*/{ref}.mp4'):
            return 'full'
        return 'thumb' if _first(f'{acc}/msg/video/*/{ref}_thumb.jpg') else None
    if kind == 'file':
        n = _safe_name(ref)
        return 'full' if n and _first(f'{acc}/msg/file/*/{glob.escape(n)}') else None
    return None

def fetch(acc, chat, res, ik, thumb=False):
    """-> ('bytes', data, mime, filename) | ('path', path, mime, filename) | (None, reason, None, None)."""
    if not acc or not res:
        return None, 'no media', None, None
    kind, _, ref = res.partition(':')
    if kind == 'img':
        files = _img_files(acc, chat, ref, thumb)
        if not files:
            return None, '原图未下载到微信', None, None
        if ik.state != 'ok':
            return None, f'图片密钥未就绪（{ik.state}）', None, None
        for s, p in files:
            with open(p, 'rb') as f:
                d = decode_dat(f.read(), ik)
            if not d:
                continue
            mime = sniff(d)
            if mime == 'image/wxgf':
                d = wxgf_to_jpeg(d, ref + s)
                if not d:
                    continue
                mime = 'image/jpeg'
            if mime:
                return 'bytes', d, mime, f'{ref}{s}.{mime.split("/")[1].replace("jpeg", "jpg")}'
        return None, '图片解码失败', None, None
    if kind == 'vid':
        if not thumb:
            p = _first(f'{acc}/msg/video/*/{ref}.mp4')
            if p:
                return 'path', p, 'video/mp4', ref + '.mp4'
            return None, '视频未下载到微信（只有封面，加 ?thumb=1 取封面）', None, None
        p = _first(f'{acc}/msg/video/*/{ref}_thumb.jpg')
        if p:
            with open(p, 'rb') as f:
                d = f.read()
            return 'bytes', d, sniff(d) or 'image/jpeg', ref + '_thumb.jpg'
        return None, '视频未下载到微信', None, None
    if kind == 'file':
        n = _safe_name(ref)
        p = n and _first(f'{acc}/msg/file/*/{glob.escape(n)}')
        if not p or not os.path.realpath(p).startswith(os.path.realpath(acc + '/msg/file') + '/'):
            return None, '文件未下载到微信', None, None
        import mimetypes
        return 'path', p, mimetypes.guess_type(n)[0] or 'application/octet-stream', n
    return None, 'no media', None, None


# ---------------- voice ----------------
def _varint(b, i):
    v = s = 0
    while i < len(b):
        c = b[i]; i += 1
        v |= (c & 0x7f) << s; s += 7
        if not c & 0x80:
            return v, i
    raise ValueError('truncated varint')

def _fields(b):
    """Top-level protobuf fields as (field_no, wire_type, value)."""
    i = 0; out = []
    while i < len(b):
        key, i = _varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 2:
            n, i = _varint(b, i); v = b[i:i + n]; i += n
        elif w == 1:
            v = b[i:i + 8]; i += 8
        elif w == 5:
            v = b[i:i + 4]; i += 4
        else:
            raise ValueError('wire type')
        out.append((f, w, v))
    return out

def voice_transcript(pk):
    """WeChat's own speech-to-text (present once someone tapped 转文字), from packed_info_data field 5."""
    if not isinstance(pk, (bytes, bytearray)):
        return None
    try:
        for f, w, v in _fields(bytes(pk)):
            if f == 5 and w == 2:
                best = None
                for f2, w2, v2 in _fields(v):
                    if w2 == 2:
                        try:
                            s = v2.decode('utf-8').strip()
                        except UnicodeDecodeError:
                            continue
                        if s and (best is None or len(s) > len(best)):
                            best = s
                return best
    except (ValueError, IndexError):
        return None
    return None

_silk = None
def silk_to_wav(data):
    """WeChat voice (0x02 + SILK v3) -> 24 kHz mono WAV bytes, or None if the decoder is unavailable."""
    global _silk
    import io, sys, wave
    if _silk is None:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor'))
        try:
            import pysilk
            _silk = pysilk
        except Exception:
            _silk = False
    if not _silk:
        return None
    src = data[1:] if data[:1] == b'\x02' else data
    pcm = io.BytesIO()
    _silk.decode(io.BytesIO(src), pcm, 24000)
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm.getvalue())
    return out.getvalue()
