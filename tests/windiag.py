import ctypes, os, sys, struct
lib = ctypes.CDLL(os.path.join(sys.base_prefix, 'DLLs', 'sqlite3.dll'))
SZ = ctypes.sizeof(ctypes.c_void_p)
lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
lib.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
lib.sqlite3_close.argtypes = [ctypes.c_void_p]
dbbuf = ctypes.create_string_buffer(SZ)
rc = lib.sqlite3_open(b':memory:', dbbuf)
db = struct.unpack('P', dbbuf.raw)[0]
print('open rc:', rc, 'handle:', db)
nbuf = ctypes.create_string_buffer(4)
struct.pack_into('i', nbuf, 0, 80)
rc = lib.sqlite3_file_control(db, b'main', 38, nbuf)
print('file_control rc:', rc)
rc = lib.sqlite3_exec(db, b'create table t(x); insert into t values (1);', None, None, None)
print('exec rc:', rc)
print('close rc:', lib.sqlite3_close(db))
