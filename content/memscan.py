"""Find WeChat's per-database keys by scanning the running WeChat process's memory.

WCDB caches every open database's raw key as an ASCII hex string in SQL syntax:
    x'<64 hex chars: enc key><32 hex chars: the db's salt>'
One pass over the readable memory therefore yields the keys for ALL databases at once; callers
verify each key against the database's first page before using it (wxdb.key_ok).

Platform notes:
- Windows: WeChat 4 runs as Weixin.exe; same-user processes can be read without admin rights.
- macOS: the binary is WeChat; reading another process's memory needs root (task_for_pid), so
  keys.py re-execs this file under sudo and reads the matches from its stdout.
- Lazycat keeps its proven gdb hook instead (see keys.py); generic Linux uses /proc/pid/mem.
"""
import ctypes
import os
import re
import subprocess
import sys

HEX_RE = re.compile(rb"x'([0-9a-fA-F]{64,512})'")
CHUNK = 8 << 20
OVERLAP = 512          # a hex string can straddle chunk boundaries
MAX_REGION = 2 << 30   # skip gigantic shared-cache-style regions


def find_pids():
    """PIDs of WeChat main processes (largest memory first on Windows)."""
    if sys.platform == 'win32':
        r = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True)
        procs = []
        for line in r.stdout.splitlines():
            m = re.match(r'^"(.+?)","(\d+)",.*?"([\d,]+)\s*K"', line)
            if m and m.group(1).lower() in ('weixin.exe', 'wechat.exe'):
                procs.append((int(m.group(2)), int(m.group(3).replace(',', '') or 0)))
        return [p for p, _ in sorted(procs, key=lambda x: -x[1])]
    if sys.platform == 'darwin':
        pids = []
        for name in ('WeChat', 'Weixin', 'wechat'):
            r = subprocess.run(['pgrep', '-x', name], capture_output=True, text=True)
            pids += [int(x) for x in r.stdout.split()]
        return sorted(set(pids))
    out = []
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        try:
            exe = os.readlink(f'/proc/{d}/exe')
        except OSError:
            continue
        if os.path.basename(exe).lower() in ('wechat', 'weixin'):
            out.append(int(d))
    return out


# ---------------- reading memory ----------------
class _WinReader:
    def __init__(self, pid):
        import ctypes.wintypes as wt

        class MBI(ctypes.Structure):
            _fields_ = [('BaseAddress', ctypes.c_uint64), ('AllocationBase', ctypes.c_uint64),
                        ('AllocationProtect', wt.DWORD), ('_pad1', wt.DWORD),
                        ('RegionSize', ctypes.c_uint64), ('State', wt.DWORD),
                        ('Protect', wt.DWORD), ('Type', wt.DWORD), ('_pad2', wt.DWORD)]

        self.MBI = MBI
        self.k32 = ctypes.windll.kernel32
        self.h = self.k32.OpenProcess(0x0410, False, pid)   # PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
        if not self.h:
            raise OSError('无法打开微信进程（OpenProcess 失败）')
        self.mbi = MBI()

    def regions(self):
        READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
        addr = 0
        while addr < 0x7FFFFFFFFFFF:
            if not self.k32.VirtualQueryEx(self.h, ctypes.c_uint64(addr),
                                           ctypes.byref(self.mbi), ctypes.sizeof(self.MBI)):
                break
            m = self.mbi
            if m.State == 0x1000 and m.Protect in READABLE and 0 < m.RegionSize <= MAX_REGION:
                yield m.BaseAddress, m.RegionSize
            nxt = m.BaseAddress + m.RegionSize
            if nxt <= addr:
                break
            addr = nxt

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t()
        if self.k32.ReadProcessMemory(self.h, ctypes.c_uint64(addr), buf, size, ctypes.byref(n)):
            return buf.raw[:n.value]
        return None

    def close(self):
        self.k32.CloseHandle(self.h)


class _ProcReader:
    """Generic Linux: /proc/<pid>/mem over readable mappings. Needs ptrace permission."""
    def __init__(self, pid):
        self.mem = open(f'/proc/{pid}/mem', 'rb', 0)
        self.maps = []
        with open(f'/proc/{pid}/maps') as f:
            for line in f:
                p = line.split()
                if 'r' not in p[1]:
                    continue
                a, b = (int(x, 16) for x in p[0].split('-'))
                if 0 < b - a <= MAX_REGION and not (a >= 0x7fff0000000000):
                    self.maps.append((a, b - a))

    def regions(self):
        return iter(self.maps)

    def read(self, addr, size):
        try:
            self.mem.seek(addr)
            return self.mem.read(size)
        except OSError:
            return None

    def close(self):
        self.mem.close()


class _MachReader:
    """macOS; needs root. Walks the task's VM regions with mach_vm_region/mach_vm_read_overwrite.

    Note: with SIP enabled, WeChat (hardened, no get-task-allow) denies even root's
    task_for_pid / task_read_for_pid unless the caller is a declared read-only debugger.
    Users may need `csrutil enable --without debug` for this to work."""
    def __init__(self, pid):
        self.lib = ctypes.CDLL('/usr/lib/system/libsystem_kernel.dylib', use_errno=True)
        L = self.lib
        L.mach_task_self.restype = ctypes.c_uint32
        L.task_for_pid.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32)]
        L.mach_vm_read_overwrite.argtypes = [ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint64,
                                             ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64)]
        L.mach_vm_region.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64),
                                     ctypes.POINTER(ctypes.c_uint64), ctypes.c_int,
                                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                     ctypes.POINTER(ctypes.c_uint32)]
        self.task = ctypes.c_uint32()
        kr = L.task_for_pid(L.mach_task_self(), pid, ctypes.byref(self.task))
        if kr != 0:
            raise OSError(kr, '无法附加到微信进程。macOS 的 SIP 会阻止读取微信（hardened runtime）内存：'
                              '需要在恢复模式执行 csrutil enable --without debug 后重启，或改用 keys.json 手动导入。')
        self.cur = ctypes.c_uint64(0)

    def regions(self):
        VM_REGION_BASIC_INFO_64, COUNT = 9, 8
        info = (ctypes.c_uint32 * COUNT)()
        count = ctypes.c_uint32(COUNT)
        name = ctypes.c_uint32(0)
        while True:
            size = ctypes.c_uint64(0)
            kr = self.lib.mach_vm_region(self.task, ctypes.byref(self.cur), ctypes.byref(size),
                                         VM_REGION_BASIC_INFO_64, info, ctypes.byref(count),
                                         ctypes.byref(name))
            if kr != 0:
                break
            addr = self.cur.value
            prot = info[0]
            if prot & 1 and 0 < size.value <= MAX_REGION:   # VM_PROT_READ
                yield addr, size.value
            self.cur = ctypes.c_uint64(addr + size.value)

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        out = ctypes.c_uint64(0)
        kr = self.lib.mach_vm_read_overwrite(self.task, addr, size, ctypes.addressof(buf),
                                             ctypes.byref(out))
        return buf.raw[:out.value] if kr == 0 else None

    def close(self):
        pass


def reader_for(pid):
    if sys.platform == 'win32':
        return _WinReader(pid)
    if sys.platform == 'darwin':
        return _MachReader(pid)
    return _ProcReader(pid)


def scan_pid(pid):
    """Yield every hex-string match from one process's memory."""
    r = reader_for(pid)
    try:
        for base, size in r.regions():
            off = 0
            while off < size:
                n = min(CHUNK, size - off)
                data = r.read(base + off, n)
                if data:
                    for m in HEX_RE.finditer(data):
                        yield m.group(1).decode()
                if n <= OVERLAP:
                    break
                off += n - OVERLAP
    finally:
        r.close()


def extract(hexes, salt_pages=None):
    """Hex strings -> ({salt_hex: [key_hex...]}, [bare 64-hex keys]).

    salt_pages (optional) maps a db's salt_hex to its first page; bare 64-hex keys (no salt
    attached in memory) are validated against those pages and merged in.
    """
    out = {}
    bare = []
    for h in hexes:
        h = h.lower()
        if len(h) >= 96 and len(h) % 2 == 0:
            out.setdefault(h[-32:], []).append(h[:64])
        elif len(h) == 64:
            bare.append(h)
    if salt_pages:
        import wxdb
        for k in bare:
            kb = bytes.fromhex(k)
            for salt, page in salt_pages.items():
                if salt not in out and wxdb.key_ok(kb, page):
                    out.setdefault(salt, []).append(k)
    return out, bare


def helper_main(argv):
    """Run as root under sudo: `app.py --scan-mem PID...` -> prints M <hex> lines."""
    for pid in (int(a) for a in argv):
        print(f'S pid={pid}', flush=True)
        try:
            for h in scan_pid(pid):
                print('M ' + h, flush=True)
        except OSError as e:
            print(f'E pid={pid} {e}', flush=True)
    print('D done', flush=True)
