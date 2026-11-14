"""AES (CBC/ECB decrypt, no padding) via the platform's crypto library.

- Linux / Windows: OpenSSL's EVP through ctypes. On Windows the DLL ships with python.org
  installs (DLLs/libcrypto-3.dll).
- macOS: CommonCrypto. /usr/lib/libcrypto.dylib must NOT be dlopen'ed directly — modern macOS
  aborts the process ("loading libcrypto in an unsafe way") because it has no stable ABI.
"""
import ctypes
import ctypes.util
import os
import sys


class OpenSsl:
    def __init__(self, lib):
        self.lib = lib
        lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
        lib.EVP_aes_128_ecb.restype = ctypes.c_void_p
        lib.EVP_aes_256_cbc.restype = ctypes.c_void_p
        lib.EVP_DecryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_char_p, ctypes.c_char_p]
        lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.EVP_DecryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_int]
        lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]

    def _dec(self, cipher, key, iv, data):
        c = self.lib.EVP_CIPHER_CTX_new()
        try:
            self.lib.EVP_DecryptInit_ex(c, cipher, None, key, iv)
            self.lib.EVP_CIPHER_CTX_set_padding(c, 0)
            out = ctypes.create_string_buffer(len(data) + 32)
            n = ctypes.c_int()
            self.lib.EVP_DecryptUpdate(c, out, ctypes.byref(n), data, len(data))
            return out.raw[:n.value]
        finally:
            self.lib.EVP_CIPHER_CTX_free(c)

    def cbc(self, key, iv, data):
        return self._dec(self.lib.EVP_aes_256_cbc(), key, iv, data)

    def ecb(self, key, data):
        return self._dec(self.lib.EVP_aes_128_ecb(), key, None, data)


class CommonCrypto:
    """macOS only. Public API, stable ABI, part of libSystem."""
    KCC_DECRYPT, KCCMODE_CBC, KCCMODE_ECB = 1, 2, 1
    KCCALG_AES, CCNOPADDING = 0, 0

    def __init__(self):
        self.lib = ctypes.CDLL('/usr/lib/system/libcommonCrypto.dylib')
        for f in ('CCCryptorCreateWithMode', 'CCCryptorUpdate', 'CCCryptorRelease'):
            getattr(self.lib, f)
        self.lib.CCCryptorCreateWithMode.argtypes = [ctypes.c_int] * 4 + \
            [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
             ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        self.lib.CCCryptorUpdate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                             ctypes.c_char_p, ctypes.c_size_t,
                                             ctypes.POINTER(ctypes.c_size_t)]

    def _dec(self, mode, key, iv, data):
        cryptor = ctypes.c_void_p()
        rc = self.lib.CCCryptorCreateWithMode(self.KCC_DECRYPT, mode, self.KCCALG_AES,
                                              self.CCNOPADDING, iv, key, len(key),
                                              None, 0, 0, 0, ctypes.byref(cryptor))
        if rc != 0:
            raise OSError(f'CCCryptorCreateWithMode rc={rc}')
        try:
            out = ctypes.create_string_buffer(len(data) + 32)
            n = ctypes.c_size_t()
            rc = self.lib.CCCryptorUpdate(cryptor, data, len(data), out, len(out), ctypes.byref(n))
            if rc != 0:
                raise OSError(f'CCCryptorUpdate rc={rc}')
            return out.raw[:n.value]
        finally:
            self.lib.CCCryptorRelease(cryptor)

    def cbc(self, key, iv, data):
        return self._dec(self.KCCMODE_CBC, key, iv, data)

    def ecb(self, key, data):
        return self._dec(self.KCCMODE_ECB, key, None, data)


def _load():
    if sys.platform == 'darwin':
        return CommonCrypto()
    names = []
    if sys.platform == 'win32':
        base = getattr(sys, 'base_prefix', sys.prefix)
        for dll in ('libcrypto-3.dll', 'libcrypto-1_1.dll'):
            names += [os.path.join(base, 'DLLs', dll), dll]
    else:
        found = ctypes.util.find_library('crypto')
        if found:
            names.append(found)
        names += ['libcrypto.so.3', 'libcrypto.so.1.1']
    errs = []
    for n in names:
        try:
            return OpenSsl(ctypes.CDLL(n))
        except OSError as e:
            errs.append(f'{n}: {e}')
    hint = 'Windows 请从 python.org 安装 Python（自带 OpenSSL）。' if sys.platform == 'win32' \
        else '安装 openssl 后重试。'
    raise RuntimeError('找不到可用的 AES 加密库（OpenSSL）。' + hint + ' 细节：' + '; '.join(errs))


AES = _load()
