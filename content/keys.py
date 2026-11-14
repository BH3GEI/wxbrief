"""WeChat 4.x database keys: verify saved keys, and capture new ones when they stop working.

Two capture paths, picked by runtime:
- Lazycat container: gdb breakpoint on the WCDB cipher-config function (located by ELF static
  analysis), which yields the account passphrase; per-DB keys are derived from it (PBKDF2).
  Follows TANGandXUE/wcdb-key-tool (MIT).
- Windows / macOS: scan the running WeChat process's memory for WCDB's cached per-DB keys
  (see memscan.py). Windows needs no admin rights; macOS re-execs this file under sudo.

Saved state (VAR dir, mode 600): pass.bin (passphrase, gdb path only) and keys.json
(salt -> [raw key hexes], each verified against the db's first page before use).
"""
import glob, hashlib, json, os, re, struct, subprocess, sys, threading, time
import runenv, wxdb

VAR = runenv.var_dir()
PASS = VAR + '/pass.bin'
KEYS = VAR + '/keys.json'
WECHAT_EXE = '/lzcapp/var/apps/wechat/opt/wechat/wechat'  # path as seen inside the cloud WeChat container
USE_GDB = runenv.LAZYCAT

ANCHOR = b'com.Tencent.WCDB.Config.Cipher'
LEA_RSI, LEA_RDI, FUNC_HEAD = b'\x48\x8d\x35', b'\x48\x8d\x3d', b'\x55\x41\x57'


class Keys:
    def __init__(self, log, account_dir=None):
        self.log = log
        self.account_dir = account_dir or (lambda: None)
        self.lock = threading.Lock()
        self.cache = {}
        try:
            raw = json.load(open(KEYS))
            self.cache = {s: [k] if isinstance(k, str) else k for s, k in raw.items()}
        except Exception:
            pass
        self.passphrase = open(PASS, 'rb').read() if os.path.exists(PASS) else None
        self.state = 'ok' if (self.passphrase or self.cache) else 'need_login'
        self.capturing = False
        self.capture_note = ''
        self.gdb = None
        self.scan_proc = None

    def key_for(self, path):
        """Return the AES key for a DB file, or None (and start capture) if it can't be opened."""
        with open(path, 'rb') as f:
            page = f.read(wxdb.PS)
        if len(page) < wxdb.PS:
            return None
        salt = page[:16].hex()
        with self.lock:
            for k in self.cache.get(salt, []):
                if wxdb.key_ok(bytes.fromhex(k), page):
                    return bytes.fromhex(k)
            if self.passphrase:
                k = wxdb.derive(self.passphrase, page[:16])
                if wxdb.key_ok(k, page):
                    self.cache[salt] = [k.hex()]
                    _write(KEYS, json.dumps(self.cache))
                    self.state = 'ok'
                    return k
        self.state = 'need_login'
        self.start_capture(path)
        return None

    def release(self):
        """Detach gdb cleanly: SIGINT stops the 'continue', the script then detaches and removes the
        breakpoint. Never SIGKILL gdb while attached, or the int3 stays in WeChat's code."""
        pr = self.gdb
        if not pr or pr.poll() is not None:
            return
        import signal
        pr.send_signal(signal.SIGINT)
        try:
            pr.wait(15)
        except Exception:
            pr.send_signal(signal.SIGINT)
            try:
                pr.wait(15)
            except Exception:
                pass
        self.gdb = None

    # ---------- capture ----------
    def start_capture(self, probe_db):
        with self.lock:
            if self.capturing:
                return
            self.capturing = True
        threading.Thread(target=self._capture_loop, args=(probe_db,), daemon=True).start()

    def _capture_loop(self, probe_db):
        try:
            if USE_GDB:
                self._gdb_loop(probe_db)
            else:
                self._memscan_loop(probe_db)
        finally:
            self.capturing = False

    # ----- memory scan (Windows / macOS) -----
    def _wanted_salts(self):
        """salt -> first page, for the account's contact/message/media databases."""
        acc = self.account_dir()
        if not acc:
            return {}
        out = {}
        for pat in ('db_storage/contact/contact.db', 'db_storage/message/message_[0-9]*.db',
                    'db_storage/message/media_[0-9]*.db'):
            for p in glob.glob(acc + '/' + pat):
                try:
                    page = open(p, 'rb').read(wxdb.PS)
                except OSError:
                    continue
                if len(page) == wxdb.PS:
                    out[page[:16].hex()] = page
        return out

    def _memscan_loop(self, probe_db):
        import memscan
        self.log('key: no usable saved key; will scan WeChat process memory for the db keys')
        page = open(probe_db, 'rb').read(wxdb.PS)
        want_salt = page[:16].hex()
        try:
            while True:
                salt_pages = self._wanted_salts()
                pids = memscan.find_pids()
                if not pids:
                    self.capture_note = '微信没有在运行：请打开微信并登录，会自动完成取密钥'
                    time.sleep(15)
                    continue
                self.capture_note = f'正在扫描微信进程（pid {" ".join(map(str, pids))}）的内存'
                try:
                    found = self._scan_pids(pids, salt_pages)
                except Exception as e:
                    self.capture_note = f'内存扫描失败：{e}'
                    self.log('key:', self.capture_note)
                    time.sleep(30)
                    continue
                keys = found.get(want_salt)
                if not keys:
                    self.capture_note = '微信在运行但还没找到密钥（可能未登录完成），继续重试'
                    time.sleep(15)
                    continue
                with self.lock:
                    for s, ks in found.items():
                        ok = [k for k in ks if s not in salt_pages or
                              wxdb.key_ok(bytes.fromhex(k), salt_pages[s])]
                        if ok:
                            self.cache[s] = ok
                    _write(KEYS, json.dumps(self.cache))
                    self.state = 'ok'
                self.capture_note = 'captured'
                self.log('key: db keys captured from memory and verified')
                return
        finally:
            self._kill_scan()

    def _kill_scan(self):
        pr = self.scan_proc
        if pr and pr.poll() is None:
            try:
                pr.terminate()
            except Exception:
                pass
        self.scan_proc = None

    def _scan_pids(self, pids, salt_pages):
        import memscan
        if sys.platform == 'darwin':
            return self._scan_sudo(pids, salt_pages)
        hexes = []
        for pid in pids:
            self.log('key: scanning memory of pid', pid)
            try:
                hexes += list(memscan.scan_pid(pid))
            except OSError as e:
                self.log('key: pid', pid, e)
        return memscan.extract(hexes, salt_pages)[0]

    def _scan_sudo(self, pids, salt_pages):
        """macOS: root is required for task_for_pid; run this file under sudo as a helper."""
        import memscan
        found = {}
        for pid in pids:
            cmd = ['sudo', sys.executable, os.path.abspath(__file__), '--scan-mem', str(pid)]
            self.log('key: sudo memory scan for pid', pid)
            pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.scan_proc = pr
            hexes = []
            try:
                for line in pr.stdout:
                    p = line.split(maxsplit=1)
                    if len(p) == 2 and p[0] == 'M':
                        hexes.append(p[1].strip())
                    elif p and p[0] == 'E':
                        self.log('key: helper:', line.strip())
                pr.wait()
            finally:
                if pr.poll() is None:
                    pr.terminate()
            if pr.returncode != 0 and not hexes:
                err = (pr.stderr.read() if pr.stderr else '')[-200:]
                if 'a terminal is required' in err or 'askpass' in err:
                    raise RuntimeError('需要 sudo 输密码：请在「终端」里启动本程序后再试一次')
                if pr.returncode:
                    raise RuntimeError(f'sudo 扫描失败（exit {pr.returncode}）：{err}'.strip())
            found.update(memscan.extract(hexes, salt_pages)[0])
        return found

    # ----- gdb hook (Lazycat cloud WeChat) -----
    def _gdb_loop(self, probe_db):
        self.log('key: saved passphrase does not open the databases; waiting for a WeChat re-login to capture a new one')
        page = open(probe_db, 'rb').read(wxdb.PS)
        tried = set()
        while True:
            pid = find_wechat_pid()
            if not pid:
                self.capture_note = 'WeChat is not running'
                time.sleep(5)
                continue
            try:
                addr = hook_address(pid)
            except Exception as e:
                self.capture_note = f'cannot locate hook: {e}'
                self.log('key:', self.capture_note)
                time.sleep(60)
                continue
            self.capture_note = f'attached to WeChat pid {pid}, waiting for login'
            self.log('key:', self.capture_note, hex(addr))
            script = GDB_SCRIPT.replace('ADDR', hex(addr))
            sp = '/tmp/wx_cap.gdb'
            open(sp, 'w').write(script)
            pr = subprocess.Popen(['gdb', '-q', '--nx', '-batch', '-p', str(pid), '-x', sp],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.gdb = pr
            for line in pr.stdout:
                m = re.match(r'CAND=([0-9a-f]{64})', line)
                if not m or m.group(1) in tried:
                    continue
                tried.add(m.group(1))
                cand = bytes.fromhex(m.group(1))
                if wxdb.key_ok(wxdb.derive(cand, page[:16]), page):
                    self.release()
                    with self.lock:
                        self.passphrase = cand
                        self.cache = {}
                        _write(PASS, cand)
                        _write(KEYS, b'{}')
                        self.state = 'ok'
                    self.capture_note = 'captured'
                    self.log('key: new passphrase captured and verified')
                    return
            pr.wait()
            time.sleep(2)


GDB_SCRIPT = '''python
import gdb
class B(gdb.Breakpoint):
    def stop(self):
        try:
            inf = gdb.selected_inferior()
            rsi = int(gdb.parse_and_eval("$rsi")); rdx = int(gdb.parse_and_eval("$rdx"))
            if rsi and rdx == 32:
                print("CAND=" + inf.read_memory(rsi, 32).tobytes().hex())
            if rsi:
                sz = int(gdb.parse_and_eval("*(unsigned long long*)(%d+16)" % rsi))
                kp = int(gdb.parse_and_eval("*(unsigned long long*)(%d+8)" % rsi))
                if sz == 32 and kp:
                    print("CAND=" + inf.read_memory(kp, 32).tobytes().hex())
        except Exception as e:
            print("err", e)
        return False
B("*ADDR")
end
continue
detach
'''


def _write(path, data):
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data.encode() if isinstance(data, str) else data)
    os.replace(tmp, path)


def wxid():
    return os.environ.get('WXID', '')


def find_wechat_pid():
    """Host PID of the cloud WeChat main process that has our account's databases open."""
    want = '/xwechat_files/' + wxid() if wxid() else '/xwechat_files/'
    for p in os.listdir('/proc'):
        if not p.isdigit():
            continue
        try:
            if os.readlink(f'/proc/{p}/exe') != WECHAT_EXE:
                continue
            for fd in os.listdir(f'/proc/{p}/fd'):
                try:
                    if want in os.readlink(f'/proc/{p}/fd/{fd}'):
                        return int(p)
                except OSError:
                    pass
        except OSError:
            pass
    return None


def _sections(data):
    shoff = struct.unpack_from('<Q', data, 40)[0]
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', data, 58)
    raw = []
    for i in range(shnum):
        name, _t, _f, addr, off, size = struct.unpack_from('<IIQQQQ', data, shoff + i * shentsize)
        raw.append((name, addr, off, size))
    so = raw[shstrndx][2]
    out = {}
    for name, addr, off, size in raw:
        n = data[so + name:data.index(b'\0', so + name)].decode()
        out[n] = (addr, data[off:off + size])
    return out


def _refs(text_addr, text, opcode, target):
    hits = []
    for m in re.finditer(re.escape(opcode), text):
        o = m.start()
        if o + 7 > len(text):
            continue
        if text_addr + o + 7 + struct.unpack_from('<i', text, o + 3)[0] == target:
            hits.append(o)
    return hits


def hook_va(data):
    s = _sections(data)
    ro_addr, ro = s['.rodata']
    tx_addr, tx = s['.text']
    cands = []
    i = ro.find(ANCHOR)
    while i != -1:
        anchor = ro_addr + i
        for r1 in _refs(tx_addr, tx, LEA_RSI, anchor):
            if r1 < 7 or tx[r1 - 7:r1 - 4] != LEA_RDI:
                continue
            unk = tx_addr + r1 + struct.unpack_from('<i', tx, r1 - 4)[0]
            for r2 in _refs(tx_addr, tx, LEA_RSI, unk):
                for c in range(r2, max(0, r2 - 0x500) - 1, -1):
                    if tx[c:c + 3] == FUNC_HEAD:
                        cands.append(tx_addr + c)
                        break
        i = ro.find(ANCHOR, i + 1)
    if not cands:
        raise RuntimeError('hook not found in this WeChat build')
    return min(cands)


def hook_address(pid):
    data = open(f'/proc/{pid}/exe', 'rb').read()
    va = hook_va(data)
    phoff = struct.unpack_from('<Q', data, 32)[0]
    phent, phnum = struct.unpack_from('<HH', data, 54)
    foff = None
    for i in range(phnum):
        t, _fl, off, vaddr, _pa, _fsz, msz, _al = struct.unpack_from('<IIQQQQQQ', data, phoff + i * phent)
        if t == 1 and vaddr <= va < vaddr + msz:
            foff = va - vaddr + off
    for line in open(f'/proc/{pid}/maps'):
        p = line.split()
        if len(p) > 5 and p[5] == WECHAT_EXE:
            a, b = (int(x, 16) for x in p[0].split('-'))
            o = int(p[2], 16)
            if o <= foff < o + (b - a):
                addr = a + foff - o
                with open(f'/proc/{pid}/mem', 'rb', 0) as m:
                    m.seek(addr)
                    if m.read(3) != FUNC_HEAD:
                        raise RuntimeError('hook bytes mismatch')
                return addr
    raise RuntimeError('hook not mapped')


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--scan-mem':
        import memscan
        memscan.helper_main(sys.argv[2:])
