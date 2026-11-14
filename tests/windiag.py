import ctypes, os, sys, struct, subprocess
VARIANTS = {
 'buffer_noargs': "db=ctypes.create_string_buffer(8); lib.sqlite3_open(b':memory:', db)",
 'buffer_argtypes': "lib.sqlite3_open.argtypes=[ctypes.c_char_p,ctypes.c_void_p]; db=ctypes.create_string_buffer(8); lib.sqlite3_open(b':memory:', db)",
 'byref_noargs': "db=ctypes.c_void_p(); lib.sqlite3_open(b':memory:', ctypes.byref(db))",
 'byref_argtypes': "lib.sqlite3_open.argtypes=[ctypes.c_char_p,ctypes.POINTER(ctypes.c_void_p)]; db=ctypes.c_void_p(); lib.sqlite3_open(b':memory:', ctypes.byref(db))",
}
for name, code in VARIANTS.items():
    full = f"import ctypes,os,sys\nlib=ctypes.CDLL(os.path.join(sys.base_prefix,'DLLs','sqlite3.dll'))\n{code}\nprint('OK', db)"
    r = subprocess.run([sys.executable, '-c', full], capture_output=True, text=True)
    print(name, '->', r.stdout.strip()[-60:] or r.stderr.strip().splitlines()[-1])
