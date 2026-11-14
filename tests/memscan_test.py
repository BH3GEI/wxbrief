#!/usr/bin/env python3
"""Memory-scan test: spawn a child process holding a fake WCDB cached-key string, then scan it
with the same platform code wxbrief uses against WeChat. Windows/Linux run without extra setup;
macOS needs root + SIP debugging relaxed, so it is skipped there unless MEMSCAN_TEST=1.
"""
import os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'content'))
import memscan  # noqa: E402

KEY = 'ab' * 32
SALT = 'cd' * 16
MARK = "x'" + KEY + SALT + "'"


def main():
    if sys.platform == 'darwin' and not os.environ.get('MEMSCAN_TEST'):
        print('SKIP: macOS memory scan needs root and SIP debugging relaxed (MEMSCAN_TEST=1 to force)')
        return
    child = subprocess.Popen([sys.executable, '-c',
                              f'import time\ns = {MARK!r}.encode()\nprint(len(s), flush=True)\ntime.sleep(120)'],
                             stdout=subprocess.PIPE, text=True)
    try:
        child.stdout.readline()
        time.sleep(0.5)
        found = []
        for h in memscan.scan_pid(child.pid):
            found.append(h)
        keys, bare = memscan.extract(found)
        assert SALT in keys and KEY in keys[SALT], f'cached key not found (matches={len(found)})'
        print(f'OK: found cached db key via {type(memscan.reader_for(child.pid)).__name__} '
              f'({len(found)} hex matches)')
    finally:
        child.terminate()


if __name__ == '__main__':
    main()
