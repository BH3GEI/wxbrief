import ctypes, os, sys, subprocess
full = """import ctypes,os,sys,sqlite3
sqlite3.connect(':memory:')
lib=ctypes.CDLL(os.path.join(sys.base_prefix,'DLLs','sqlite3.dll'))
lib.sqlite3_open.argtypes=[ctypes.c_char_p,ctypes.POINTER(ctypes.c_void_p)]
db=ctypes.c_void_p()
rc=lib.sqlite3_open(b':memory:', ctypes.byref(db))
print('OK rc', rc, 'handle', db.value)
"""
r = subprocess.run([sys.executable, '-c', full], capture_output=True, text=True)
print('with sqlite3 imported first:', r.stdout.strip(), r.stderr.strip().splitlines()[-1:] )
full2 = """import ctypes,os,sys
lib=ctypes.CDLL(os.path.join(sys.base_prefix,'DLLs','sqlite3.dll'))
lib.sqlite3_initialize()
lib.sqlite3_open.argtypes=[ctypes.c_char_p,ctypes.POINTER(ctypes.c_void_p)]
db=ctypes.c_void_p()
rc=lib.sqlite3_open(b':memory:', ctypes.byref(db))
print('OK rc', rc, 'handle', db.value)
"""
r = subprocess.run([sys.executable, '-c', full2], capture_output=True, text=True)
print('with sqlite3_initialize:', r.stdout.strip(), r.stderr.strip().splitlines()[-1:])
