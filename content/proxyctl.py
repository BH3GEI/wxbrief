"""The optional built-in proxy (the `proxy` service, content/proxy.sh) as seen from the settings page:
the subscription URL (VAR/mihomo/sub_url, 0600, never shown again) and the live status from mihomo's API.
Saving a subscription also puts socks5://proxy:7890 first in the Telegram proxy list; turning it off takes it out."""
import json, os, re, urllib.request

LOCAL = 'socks5://proxy:7890'
API = 'http://proxy:9090'


def _dir():
    return os.path.join(os.environ.get('VAR', '/lzcapp/var'), 'mihomo')


def _write(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)


def enabled():
    try:
        return bool(open(os.path.join(_dir(), 'sub_url')).read().strip())
    except OSError:
        return False


def set_sub(url):
    url = (url or '').strip()
    if not re.match(r'^https?://\S+$', url):
        raise ValueError('订阅链接应以 http:// 或 https:// 开头')
    _write(os.path.join(_dir(), 'sub_url'), url + '\n')


def turn_off():
    _write(os.path.join(_dir(), 'sub_url'), '')    # emptied, not deleted: the service stops mihomo within seconds


def with_local(setting, on):
    """The Telegram proxy setting with the built-in proxy first (on) or removed (off); other proxies stay in order."""
    items = [p for p in re.split(r'[\s,]+', setting or '') if p and p.rstrip('/') != LOCAL]
    return ', '.join(([LOCAL] if on else []) + items)


def _get(path, timeout=2):
    try:
        secret = open(os.path.join(_dir(), 'secret')).read().strip()
    except OSError:
        secret = ''
    req = urllib.request.Request(API + path, headers={'Authorization': 'Bearer ' + secret})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def status():
    """{'enabled', 'running', 'node', 'delay', 'alive', 'total', 'error'}"""
    s = {'enabled': enabled(), 'running': False, 'node': '', 'delay': 0, 'alive': 0, 'total': 0, 'error': ''}
    if not s['enabled']:
        return s
    try:
        g = _get('/proxies/fastest')
        nodes = _get('/providers/proxies/sub').get('proxies', [])
    except Exception as e:
        s['error'] = type(e).__name__
        return s
    delays = {}
    for n in nodes:
        h = n.get('history') or []
        delays[n.get('name')] = h[-1].get('delay', 0) if h else 0
    s.update(running=True, node=g.get('now', ''), delay=delays.get(g.get('now'), 0),
             alive=sum(1 for d in delays.values() if d > 0), total=len(nodes))
    return s
