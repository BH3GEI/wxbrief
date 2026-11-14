"""API keys for agents: several named keys, each revocable. Stored in VAR/apikeys.json (0600).
The pre-0.5 single token (VAR/token) is migrated as a key named 默认 so existing agents keep working."""
import json, os, secrets, threading, time

VAR = os.environ.get('VAR', '/lzcapp/var')
PATH = VAR + '/apikeys.json'
LEGACY = VAR + '/token'


def _write(path, data):
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


class ApiKeys:
    def __init__(self):
        self.lock = threading.Lock()
        self.flushed = 0
        try:
            self.keys = json.load(open(PATH))['keys']
        except Exception:
            self.keys = []
            try:
                old = open(LEGACY).read().strip()
            except OSError:
                old = ''
            if old:
                self.keys.append({'id': secrets.token_hex(4), 'name': '默认', 'key': old,
                                  'created': int(os.path.getmtime(LEGACY)), 'last_used': 0})
            self._save()

    def _save(self):
        _write(PATH, {'keys': self.keys})
        self.flushed = time.time()

    def list(self):
        with self.lock:
            return [dict(k) for k in self.keys]

    def get(self, kid):
        with self.lock:
            return next((dict(k) for k in self.keys if k['id'] == kid), None)

    def create(self, name):
        name = (name or '').strip()[:40] or 'agent'
        k = {'id': secrets.token_hex(4), 'name': name, 'key': 'wxb_' + secrets.token_urlsafe(32),
             'created': int(time.time()), 'last_used': 0}
        with self.lock:
            self.keys.append(k)
            self._save()
        return dict(k)

    def reset(self, kid):
        """New secret for an existing key (same name); the old secret stops working at once."""
        with self.lock:
            for k in self.keys:
                if k['id'] == kid:
                    k['key'] = 'wxb_' + secrets.token_urlsafe(32)
                    k['last_used'] = 0
                    self._save()
                    return True
        return False

    def revoke(self, kid):
        with self.lock:
            n = len(self.keys)
            self.keys = [k for k in self.keys if k['id'] != kid]
            if len(self.keys) != n:
                self._save()
                return True
        return False

    def check(self, header):
        """Authorization header -> key record or None. Records last use (flushed at most once a minute)."""
        if not header.startswith('Bearer '):
            return None
        given = header[7:].strip()
        with self.lock:
            for k in self.keys:
                if secrets.compare_digest(given.encode(), k['key'].encode()):
                    k['last_used'] = int(time.time())
                    if time.time() - self.flushed > 60:
                        self._save()
                    return dict(k)
        return None
