"""A fake Telegram adapter shared by the tests (no network)."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'content'))
import tg


class Fake:
    """Implements the adapter interface used by tg.Service."""
    def __init__(self):
        self.on_session = None; self.sent = []; self.calls = []; self.flood = 0; self.st = 'ready'
        now = int(time.time())
        self.msgs = {('u', 7): [dict(id=i, ts=now - 3600 * i, sender='小明', text=f'msg{i}', media=None, name='') for i in (3, 2, 1)]}
        self.msgs[('u', 7)].insert(0, dict(id=4, ts=now - 60, sender='小明', text='看这张', media='img', name=''))
        self.msgs[('u', 7)].insert(0, dict(id=5, ts=now - 30, sender='小明', text='', media='file', name='报价.pdf'))
        self.msgs[('me', 0)] = []
    def configure(self, *a): pass
    def status(self): return {'state': self.st, 'qr_url': '', 'error': ''}
    def check(self): return self.st
    def me(self): return '测试用户'
    def qr_begin(self): self.st = 'qr'
    def logout(self): self.st = 'logged_out'
    def dialogs(self):
        self.calls.append('dialogs')
        return [('tg:u7', '小明', 'u', 111, 100, 'xiaoming'), ('tg:g9', '项目群', 'g', None, 200, None), ('tg:me', 'Saved Messages（收藏夹）', 'me', None, 50, None)]
    def messages(self, ref, since, min_id, limit):
        self.calls.append(('messages', ref, min_id))
        if self.flood: raise tg.TGError('rate_limited', 'x', self.flood)
        return [m for m in self.msgs.get((ref[0], ref[1]), []) if m['id'] > min_id and m['ts'] >= since][:limit]
    def send(self, ref, text): self.sent.append((ref, text)); return 99, int(time.time())
    def download(self, ref, mid, destdir):
        self.calls.append(('download', mid))
        p = os.path.join(destdir, 'photo_2026.jpg'); open(p, 'wb').write(b'x' * 10); return p
