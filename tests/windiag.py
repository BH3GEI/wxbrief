import ctypes, os, sys
lib = ctypes.CDLL(os.path.join(sys.base_prefix, 'DLLs', 'sqlite3.dll'))
lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
lib.sqlite3_close.argtypes = [ctypes.c_void_p]
db = ctypes.c_void_p()
pp = ctypes.pointer(db)
print('pointer(db) addr:', ctypes.addressof(db), 'pp:', pp)
rc = lib.sqlite3_open(b':memory:', pp)
print('pointer open rc:', rc, 'handle:', db.value)
lib.sqlite3_close(db)
