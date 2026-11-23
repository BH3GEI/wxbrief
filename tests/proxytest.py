#!/usr/bin/env python3
"""Proxy failover for the Bot API (tgbot): several proxies in the setting, used in the listed order; a failing one
is skipped within the same call and paused, the background probe brings it back; a call that breaks before the
request went out moves on at once, after it went out only read-only calls are repeated.
Also tg.parse_proxy with several proxies. No network: the _Conn methods are replaced.

Run from the repo root:  python3 tests/proxytest.py
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'content', 'vendor'))
sys.path.insert(0, os.path.join(HERE, '..', 'content'))
import tgbot, tg  # noqa: E402

import json, time  # noqa: E402

tried = []
dead = set()          # handshake fails
send_dead = set()     # connects, then the request fails while sending
read_dead = set()     # request goes out, the response never comes
sent_to = []          # (proxy, path) of every request that went out


class FakeResp:
    status = 200
    def __init__(self, body): self.body = body
    def read(self): return self.body


def fake_connect(self):
    tried.append(self._proxy)
    if self._proxy in dead:
        raise ConnectionRefusedError('dead')


def fake_request(self, verb, path, body=None, headers=None):
    if self._proxy in send_dead:
        raise BrokenPipeError('send')
    sent_to.append((self._proxy, path.rsplit('/', 1)[-1]))


def fake_getresponse(self):
    if self._proxy in read_dead:
        raise TimeoutError('read')
    return FakeResp(json.dumps({'ok': True, 'result': self._proxy}).encode())


tgbot._Conn.connect = fake_connect
tgbot._Conn.request = fake_request
tgbot._Conn.getresponse = fake_getresponse
tgbot._Conn.close = lambda self: None
S = 'socks5://a:1, socks5://b:2 socks5://c:3'
A, B, C = 'socks5://a:1', 'socks5://b:2', 'socks5://c:3'
ok = fail = 0


def check(name, cond):
    global ok, fail
    print(('PASS ' if cond else 'FAIL ') + name)
    ok, fail = ok + bool(cond), fail + (not cond)


def reset():
    tgbot._HEALTH.clear()
    tried.clear(); sent_to.clear(); dead.clear(); send_dead.clear(); read_dead.clear()


def call(method='sendMessage'):
    return tgbot.bot_call('T', method, {}, S)


check('split', tgbot.proxies(S) == [A, B, C])
check('empty = direct', tgbot.proxies('') == [''])
check('label hides the password', tgbot.label('socks5://u:secret@h:1') == 'socks5://h:1' and tgbot.label('') == '直连')

reset()
check('all fine: the first is used', call() == A and tried == [A])

reset(); dead.add(A)
r = call()
check('dead first proxy is skipped within the same call', r == B and tried == [A, B])
tried.clear()
r = call()
check('a failing proxy is paused: the next call does not wait for it', r == B and tried == [B])
h = tgbot.health(S)
check('health: first bad with a pause, second ok', h[0]['state'] == 'bad' and h[0]['retry_in'] > 0 and h[1]['state'] == 'ok'
      and h[2]['state'] == 'unknown')

dead.clear(); tried.clear()
tgbot._probe_once(S)
check('probe waits for the pause to end', tried == [] and tgbot.health(S)[0]['state'] == 'bad')
tgbot._HEALTH[A]['until'] = time.time() - 1
tgbot._probe_once(S)
check('probe after the pause: recovered', tried == [A] and tgbot.health(S)[0]['state'] == 'ok')
tried.clear()
check('recovered preferred proxy is used again', call() == A and tried == [A])

reset(); dead.add(A); call()
tgbot._HEALTH[A]['until'] = time.time() - 1
tgbot._probe_once(S)
check('probe failing again: pause grows', tgbot._HEALTH[A]['fails'] == 2 and tgbot._HEALTH[A]['until'] - time.time() > 50)

reset(); send_dead.add(A)
check('broken while sending: next proxy in the same call', call() == B and sent_to == [(B, 'sendMessage')])

reset(); read_dead.add(A)
try:
    call('sendMessage')
    check('sent but no answer: a send is not repeated', False)
except tg.TGError as e:
    check('sent but no answer: a send is not repeated', e.code == 'network' and sent_to == [(A, 'sendMessage')])
sent_to.clear()
check('...and the next send goes through the next proxy', call('sendMessage') == B and sent_to == [(B, 'sendMessage')])

reset(); read_dead.add(A)
check('sent but no answer: a read-only call is repeated on the next proxy', call('getUpdates') == B
      and sent_to == [(A, 'getUpdates'), (B, 'getUpdates')])
reset(); read_dead.add(A)
check('getFile download is repeated too', tgbot.bot_get_file('T', 'x', S) and sent_to[-1][0] == B)

reset(); dead.update([A, B, C])
try:
    call()
    check('all dead raises network, each tried once', False)
except tg.TGError as e:
    check('all dead raises network, each tried once', e.code == 'network' and sorted(tried) == [A, B, C] and len(tried) == 3)
tried.clear(); dead.discard(A)
tgbot._HEALTH[B]['until'] += 100; tgbot._HEALTH[C]['until'] += 200
check('all paused: still tried, soonest-back first', call() == A and tried == [A])

reset(); dead.update([A, B]); send_dead.add(C)
try:
    call()
    check('every proxy fails somewhere: one error, no proxy tried twice', False)
except tg.TGError:
    check('every proxy fails somewhere: one error, no proxy tried twice', sorted(tried) == [A, B, C])

logged = []
tgbot._LOG[0] = lambda tag, msg: logged.append(msg)
reset(); dead.add('socks5://u:pw@a:1')
tgbot.bot_call('T', 'getMe', {}, 'socks5://u:pw@a:1, socks5://b:2')
check('logs the switch once, without the password', len(logged) == 1 and 'pw' not in logged[0] and 'a:1' in logged[0])
dead.clear(); tgbot._HEALTH['socks5://u:pw@a:1']['until'] = 0
tgbot._probe_once('socks5://u:pw@a:1, socks5://b:2')
check('logs the recovery', len(logged) == 2 and '恢复' in logged[1])
tgbot._LOG[0] = None

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
