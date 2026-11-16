"""Privileged side of the Lazycat app: the only process with ptrace access to the cloud WeChat.

It does nothing until the worker leaves VAR/capture.req (a path to a database the saved passphrase
cannot open). Then it attaches gdb to WeChat, waits for the next login, verifies the captured
passphrase against that database, saves pass.bin and removes the request. It has no network
listener and reads no messages.
"""
import os, re, signal, sys, threading, time
os.environ['ROLE'] = 'capture'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keys as K, wxdb


def log(*a):
    print(time.strftime('%m-%d %H:%M:%S'), *a, flush=True)


def still_needed(probe):
    """True if the saved passphrase still fails on this database."""
    try:
        page = open(probe, 'rb').read(wxdb.PS)
        pw = open(K.PASS, 'rb').read() if os.path.exists(K.PASS) else None
    except OSError:
        return False
    return len(page) == wxdb.PS and not (pw and wxdb.key_ok(wxdb.derive(pw, page[:16]), page))


def watch_cancel(keys, done):
    """The page's cancel button removes the request file: stop waiting and detach gdb cleanly."""
    while not done.is_set() and os.path.exists(K.REQ):
        time.sleep(1)
    if not done.is_set():
        keys.cancel = True
        keys.release()


def main():
    os.makedirs(K.VAR, exist_ok=True)
    keys = K.Keys(log)

    def stop(sig, frame):
        log('stopping; releasing gdb if attached')
        keys.release()
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log('capturer ready, waiting for a request')
    while True:
        K._write(K.ALIVE, str(time.time()))
        if os.path.exists(K.REQ):
            req = open(K.REQ).read().split()
            probe = req[0] if req else ''
            force = 'force' in req[1:]
            m = re.search(r'/xwechat_files/(wxid_[^/]+)/', probe)
            if not m:
                # Never attach without knowing exactly which account's WeChat to target.
                log('request has no account directory; ignoring', probe[:120])
                os.remove(K.REQ)
                continue
            os.environ['WXID'] = m.group(1)
            if force or still_needed(probe):
                keys.capture_note = '等待微信重新登录以获取新口令'
                keys.cancel = False
                done = threading.Event()
                threading.Thread(target=watch_cancel, args=(keys, done), daemon=True).start()
                try:
                    keys._gdb_loop(probe)
                except Exception as e:
                    keys.capture_note = f'抓取出错：{e}'
                    log('capture error', e)
                    keys.release()
                    time.sleep(30)
                    continue
                finally:
                    done.set()
            try:
                os.remove(K.REQ)
            except OSError:
                pass
        time.sleep(5)


if __name__ == '__main__':
    main()
