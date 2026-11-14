import ctypes, os, sys, sqlite3
c = sqlite3.connect(':memory:')
print('python sqlite3 works:', c.execute('select sqlite_version()').fetchone())
lib = ctypes.CDLL(os.path.join(sys.base_prefix, 'DLLs', 'sqlite3.dll'))
print('libversion_number:', lib.sqlite3_libversion_number())
print('threadsafe:', lib.sqlite3_threadsafe())
db = ctypes.c_void_p(12345)
buf = (ctypes.c_char * 16)()
print('buffer addr:', ctypes.addressof(buf))
# use a preallocated pointer-sized buffer as ppDb
import struct
ptrbuf = ctypes.create_string_buffer(8)
try:
    rc = lib.sqlite3_open(b':memory:', ptrbuf)
    print('open with char buffer rc:', rc, 'handle:', struct.unpack('P', ptrbuf.raw)[0])
except OSError as e:
    print('char buffer failed:', e)
