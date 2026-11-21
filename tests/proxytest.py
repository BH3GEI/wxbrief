#!/usr/bin/env python3
"""Proxy rotation for the Bot API (tgbot._open): several proxies in the setting, a dead one is skipped,
the one that worked is tried first next time, a call that breaks after connecting moves on to the next proxy.
Also tg.parse_proxy with several proxies. No network: _Conn.connect is replaced.

Run from the repo root:  python3 tests/proxytest.py
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'content', 'vendor'))
sys.path.insert(0, os.path.join(HERE, '..', 'content'))
import tgbot, tg  # noqa: E402

tried = []
dead = set()


def fake_connect(self):
    tried.append(self._proxy)
    if self._proxy in dead:
        raise ConnectionRefusedError('dead')


tgbot._Conn.connect = fake_connect
S = 'socks5://a:1, socks5://b:2 socks5://c:3'
ok = fail = 0


def check(name, cond):
    global ok, fail
    print(('PASS ' if cond else 'FAIL ') + name)
    ok, fail = ok + bool(cond), fail + (not cond)


check('split', tgbot.proxies(S) == ['socks5://a:1', 'socks5://b:2', 'socks5://c:3'])
check('empty = direct', tgbot.proxies('') == [''])

dead = {'socks5://a:1'}
c, k = tgbot._open(S, 30)
check('dead first proxy is skipped', k == 1 and tried == ['socks5://a:1', 'socks5://b:2'])

tried.clear()
c, k = tgbot._open(S, 30)
check('the proxy that worked goes first', k == 1 and tried == ['socks5://b:2'])

tried.clear()
tgbot._failed(S, k)
c, k = tgbot._open(S, 30)
check('after a broken call the next proxy goes first', k == 2 and tried == ['socks5://c:3'])

tried.clear()
dead = {'socks5://a:1', 'socks5://b:2', 'socks5://c:3'}
try:
    tgbot._open(S, 30)
    check('all dead raises network', False)
except tg.TGError as e:
    check('all dead raises network', e.code == 'network' and len(tried) == 3)

tried.clear()
dead = {'socks5://c:3'}
c, k = tgbot._open(S, 30)
check('wraps around to the first', k == 0 and tried == ['socks5://c:3', 'socks5://a:1'])

check('parse_proxy: first of several', tg.parse_proxy(S) == ('socks5', 'a', 1))
check('parse_proxy: single', tg.parse_proxy('socks5://u:p@h:9') == ('socks5', 'h', 9, True, 'u', 'p'))
check('parse_proxy: empty', tg.parse_proxy('  ') is None)
try:
    tg.parse_proxy('socks5://a:1, nonsense')
    check('parse_proxy: rejects a bad item', False)
except tg.TGError:
    check('parse_proxy: rejects a bad item', True)

# built-in proxy settings (proxyctl): subscription file and the Telegram proxy list
import tempfile, proxyctl  # noqa: E402
os.environ['VAR'] = tempfile.mkdtemp(prefix='wxbrief-proxy-')
L = proxyctl.LOCAL
check('with_local on: first, others kept', proxyctl.with_local('socks5://r:1', True) == f'{L}, socks5://r:1')
check('with_local on: no duplicate', proxyctl.with_local(f'socks5://r:1, {L}', True) == f'{L}, socks5://r:1')
check('with_local off: removed, others kept', proxyctl.with_local(f'{L}, socks5://r:1', False) == 'socks5://r:1')
check('with_local on empty', proxyctl.with_local('', True) == L)
check('off by default', not proxyctl.enabled() and proxyctl.status()['enabled'] is False)
try:
    proxyctl.set_sub('ftp://x'); check('bad subscription rejected', False)
except ValueError:
    check('bad subscription rejected', True)
proxyctl.set_sub('https://example.invalid/sub')
f = os.path.join(os.environ['VAR'], 'mihomo', 'sub_url')
check('subscription saved 0600', proxyctl.enabled() and oct(os.stat(f).st_mode & 0o777) == '0o600')
st = proxyctl.status()
check('enabled but service unreachable: not running, error given', st['enabled'] and not st['running'] and st['error'])
proxyctl.turn_off()
check('turned off: emptied, not deleted', os.path.exists(f) and not proxyctl.enabled())

print(f'{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
