"""A fake Telegram adapter shared by the tests (no network)."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'content'))
import tg


class Fake:
    """Implements the adapter interface used by tg.Service."""
    def __init__(self):
        self.on_session = None; self.sent = []; self.files = []; self.calls = []; self.flood = 0; self.st = 'ready'
        now = int(time.time())
        self.msgs = {('u', 7): [dict(id=i, ts=now - 3600 * i, sender='小明', text=f'msg{i}', media=None, name='') for i in (3, 2, 1)]}
        self.msgs[('u', 7)].insert(0, dict(id=4, ts=now - 60, sender='小明', text='看这张', media='img', name=''))
        self.msgs[('u', 7)].insert(0, dict(id=5, ts=now - 30, sender='小明', text='', media='file', name='报价.pdf'))
        self.msgs[('me', 0)] = []
        self.taken, self.refuse, self.groups, self.started = set(), set(), [], []
    def configure(self, *a): pass
    def status(self): return {'state': self.st, 'qr_url': '', 'error': ''}
    def check(self): return self.st
    def me(self): return '测试用户'
    def qr_begin(self): self.st = 'qr'
    def logout(self): self.st = 'logged_out'
    def dialogs(self):
        self.calls.append('dialogs')
        return [('tg:u7', '小明', 'u', 111, 100, 'xiaoming'), ('tg:g9', '项目群', 'g', None, 200, None), ('tg:me', 'Saved Messages（收藏夹）', 'me', None, 50, None)]
    def messages(self, ref, since, min_id, limit, offset_id=0):
        self.calls.append(('messages', ref, min_id))
        if self.flood: raise tg.TGError('rate_limited', 'x', self.flood)
        ms = [m for m in self.msgs.get((ref[0], ref[1]), []) if m['id'] > min_id and (not offset_id or m['id'] < offset_id) and m['ts'] >= since]
        ms.sort(key=lambda m: -m['id'])            # newest first, like Telegram
        return ms[:limit]
    def search(self, ref, q, limit):
        self.calls.append(('search', ref, q))
        if self.flood: raise tg.TGError('rate_limited', 'x', self.flood)
        ids = {('u', 7): 'tg:u7', ('g', 9): 'tg:g9', ('me', 0): 'tg:me'}
        out = []
        for peer, ms in self.msgs.items():
            if ref and (ref[0], ref[1]) != peer: continue
            for m in ms:
                if q.lower() in m['text'].lower(): out.append(dict(m, chat=ids.get(peer, 'tg:u1'), media=m.get('media')))
        out += [dict(id=900 + i, chat='tg:u7', ts=int(time.time()) - 900 * 86400, sender='小明', text=f'很久以前的{q}{i}', media=None, name='') for i in range(2)] if q == '很久以前' and (not ref or ref[0] == 'u') else []
        return out[:limit]
    # bot pool support
    owner = 4242
    created = 0
    serial = 0
    botfather_error = None          # set to a TGError to make the next create fail
    taken = set()
    started = []
    def me_id(self): return self.owner
    def username_free(self, u): return u not in self.taken
    def create_bot(self, name, username):
        self.calls.append(('create_bot', name, username))
        if self.botfather_error:
            e, self.botfather_error = self.botfather_error, None
            raise e
        if username in self.refuse:
            raise tg.TGError('botfather', 'BotFather 没有接受这个用户名：Sorry, this username is already taken.')
        self.created += 1
        self.taken.add(username)
        Fake.serial += 1          # unique across all fake accounts in one test run
        return f'{7000 + Fake.serial}:' + 'T' * 34 + str(Fake.serial)
    def start_bot(self, username): self.started.append(username)
    groups = []
    def create_group(self, title, about, bot_username):
        self.groups.append((title, bot_username)); return 5550001
    topics_made = []
    def create_topic(self, channel_id, title):
        self.topics_made.append((channel_id, title)); return 777
    pinned = []
    def pin_topic(self, channel_id, topic_id): self.pinned.append((channel_id, topic_id)); return True
    renamed = []
    def rename_topic(self, channel_id, topic_id, title): self.renamed.append((channel_id, topic_id, title)); return True
    def contacts(self):
        self.calls.append('contacts')
        return [('tg:u7', '小明', 'xiaoming', 111), ('tg:u50', '没聊过的人', 'nochat', 222)]
    def send(self, ref, text): self.sent.append((ref, text)); return 99, int(time.time())
    def send_file(self, ref, name, data, caption, kind, video=None):
        self.files.append((ref, name, len(data), caption, kind, video)); return 98, int(time.time())
    def download(self, ref, mid, destdir):
        self.calls.append(('download', mid))
        p = os.path.join(destdir, 'photo_2026.jpg'); open(p, 'wb').write(b'x' * 10); return p
