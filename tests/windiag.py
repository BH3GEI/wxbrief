import ctypes, os, sys
p = os.path.join(sys.base_prefix, 'DLLs', 'sqlite3.dll')
lib = ctypes.CDLL(p)
lib.sqlite3_libversion.restype = ctypes.c_char_p
print('version:', lib.sqlite3_libversion())
# no argtypes at all
db = ctypes.c_void_p()
try:
    rc = lib.sqlite3_open(b':memory:', ctypes.byref(db))
    print('no-argtypes open rc:', rc, db.value)
except OSError as e:
    print('no-argtypes failed:', e)
# try open_v2 with explicit types
lib.sqlite3_open_v2.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_char_p]
db2 = ctypes.c_void_p()
try:
    rc = lib.sqlite3_open_v2(b':memory:', ctypes.byref(db2), 2, None)
    print('open_v2 rc:', rc, db2.value)
except OSError as e:
    print('open_v2 failed:', e)
