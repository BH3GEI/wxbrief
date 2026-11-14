import ctypes, os, sys
print(sys.base_prefix)
p = os.path.join(sys.base_prefix, 'DLLs', 'sqlite3.dll')
print('dll path:', p, os.path.exists(p))
lib = ctypes.CDLL(p)
lib.sqlite3_libversion.restype = ctypes.c_char_p
print('version:', lib.sqlite3_libversion())
lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
lib.sqlite3_close.argtypes = [ctypes.c_void_p]
db = ctypes.c_void_p()
rc = lib.sqlite3_open(b':memory:', ctypes.byref(db))
print('open rc:', rc, 'db:', db.value)
lib.sqlite3_close(db)
