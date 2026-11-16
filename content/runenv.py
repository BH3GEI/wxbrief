"""Runtime environment: default paths and labels for Lazycat (linux container), Windows and macOS.

Every value can be overridden with an env var (VAR / WORK / CACHE / WECHAT_DIR / BIND / PORT),
which is what the Lazycat container's start.sh does. Desktop defaults keep everything under
~/.wxbrief and bind to 127.0.0.1 only.
"""
import os
import sys

WINDOWS = sys.platform == 'win32'
MACOS = sys.platform == 'darwin'
LAZYCAT = not WINDOWS and not MACOS and os.path.isdir('/lzcapp')


def _home():
    return os.path.expanduser('~')


def var_dir():
    if os.environ.get('VAR'):
        return os.environ['VAR']
    if LAZYCAT:
        return '/lzcapp/var'
    return os.path.join(_home(), '.wxbrief')


def work_dir(var):
    if os.environ.get('WORK'):
        return os.environ['WORK']
    if os.path.isdir('/work') and os.access('/work', os.W_OK):   # tmpfs in the Lazycat container
        return '/work'
    return os.path.join(var, 'work')


def cache_dir(var):
    if os.environ.get('CACHE'):
        return os.environ['CACHE'] + '/media'
    if os.path.isdir('/lzcapp/cache'):
        return '/lzcapp/cache/media'
    return os.path.join(var, 'cache', 'media')


def wechat_dir_candidates():
    if os.environ.get('WECHAT_DIR'):
        yield os.environ['WECHAT_DIR']
    home = _home()
    if MACOS:
        yield os.path.join(home, 'Library', 'Containers', 'com.tencent.xinWeChat',
                           'Data', 'Documents', 'xwechat_files')
    elif WINDOWS:
        for sub in ('Documents', 'OneDrive\\Documents', 'OneDrive\\文档'):
            yield os.path.join(home, sub, 'xwechat_files')
    else:
        yield '/wechat/xwechat_files'   # cloud WeChat mount inside the Lazycat container


def find_wechat_dir():
    """WECHAT_DIR if set (even when it does not exist yet: never fall back to another WeChat on this machine),
    else the first existing default location, else the first one (for the error message)."""
    if os.environ.get('WECHAT_DIR'):
        return os.environ['WECHAT_DIR']
    first = None
    for c in wechat_dir_candidates():
        if first is None:
            first = c
        if os.path.isdir(c):
            return c
    return first


def bind_host():
    return os.environ.get('BIND') or ('0.0.0.0' if LAZYCAT else '127.0.0.1')


def wx_label():
    """What we call WeChat in user-visible strings."""
    return os.environ.get('WX_LABEL') or ('云微信' if LAZYCAT else '微信')
