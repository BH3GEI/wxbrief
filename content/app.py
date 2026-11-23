"""wxbrief: read WeChat 4.x's local databases (read-only) and Telegram (on demand), keep every message, and expose a
small JSON API for agents plus a minimal phone-sized page. Runs on Windows, macOS and Lazycat.

Modules: ctx (state, store), wechat (reading WeChat), api (agent API), pages (web pages), app (HTTP + main)."""
import html, json, os, re, sys, threading, traceback
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor'))
import base64
import runenv, ctx, wxdb, media, apikeys, keys as keymod, tg, tgbot, proxyctl
from api import BOTFATHER, api, api_send, draft_create, draft_dismiss, draft_get, draft_list, help_text, tg_reply, wait_new
from ctx import STATE, VAR, WECHAT, db, init_store, log
from pages import base_url, chat, do_refresh, home, keys_page, older_page, page, ping_version, settings_page, sync_part, tg_home, tgsearch_page
from wechat import VOICES, account_dir, ingest_loop


# Request body limits. Bodies are always read whole; anything bigger is refused with 413, never cut short.
SEND_FILE_MAX = 70 * 1024 * 1024    # POST /api/send_file: base64 of a file up to 50 MB (bot topics stay at 20 MB)
JSON_MAX = 4 * 1024 * 1024          # other JSON endpoints (100 menu commands or a 32768-char message as \uXXXX fit easily)
FORM_MAX = 1024 * 1024              # web page forms (/tg/send text etc.)
DRAIN_MAX = 96 * 1024 * 1024        # oversized bodies up to this are drained before answering 413


class BodyError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg); self.code = code


def make_handler(K):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def send(self, body, code=200, ctype='text/html; charset=utf-8'):
            b = body.encode()
            self.send_response(code); self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
        def body(self, limit):
            """The whole request body, exactly Content-Length bytes. Too big or cut short raises BodyError; never truncates."""
            if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
                self.close_connection = True
                raise BodyError(411, 'Content-Length required (chunked bodies are not supported)')
            try:
                n = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                n = -1
            if n < 0:
                self.close_connection = True
                raise BodyError(400, 'bad Content-Length')
            if n > limit:
                self.close_connection = True
                left = n if n <= DRAIN_MAX else 0   # read and drop it so the client gets the 413 instead of a broken pipe
                while left > 0:
                    got = self.rfile.read(min(left, 1 << 16))
                    if not got:
                        break
                    left -= len(got)
                raise BodyError(413, f'body too large: {n} bytes, the limit for {urlparse(self.path).path} is {limit} bytes')
            data = self.rfile.read(n)           # buffered: loops until n bytes or EOF
            if len(data) < n:
                self.close_connection = True
                raise BodyError(400, f'body cut short: got {len(data)} of {n} bytes')
            return data
        def json_body(self, path):
            """(dict, None) or (None, error already sent)."""
            try:
                f = json.loads(self.body(SEND_FILE_MAX if path == '/api/send_file' else JSON_MAX).decode('utf-8'))
                if not isinstance(f, dict):
                    raise ValueError('JSON object required')
                return f, None
            except BodyError as e:
                return None, self.send(json.dumps({'error': str(e)}), e.code, 'application/json')
            except Exception as e:
                return None, self.send(json.dumps({'error': f'invalid json: {e}'}), 400, 'application/json')
        def sec_headers(self):
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', 'sandbox')
        def voice(self, r, mid):
            raw = VOICES.get(K, r['chat'], int(r['res'][6:]), r['ts'])
            if not raw:
                return None, '语音未下载到微信', None, None
            fmt = parse_qs(urlparse(self.path).query).get('format', [''])[0]
            if fmt != 'silk':
                wav = media.silk_to_wav(raw)
                if wav:
                    return 'bytes', wav, 'audio/wav', f'voice_{mid}.wav'
            return 'bytes', raw[1:] if raw[:1] == b'\x02' else raw, 'audio/silk', f'voice_{mid}.silk'
        def send_media(self, mid, thumb):
            c = db()
            r = c.execute('select chat, res, ts from msgs where id=?', (mid,)).fetchone()
            c.close()
            if not r or not r['res']:
                return self.send('{"error":"no media"}', 404, 'application/json')
            if r['res'].startswith('voice:'):
                how, data, mime, name = self.voice(r, mid)
            elif r['res'].startswith('bot:'):
                try:
                    data, mime, name = ctx.BOTS.media_file(mid)
                except tg.TGError as e:
                    return self.send(json.dumps({'error': e.msg}, ensure_ascii=False), 404, 'application/json; charset=utf-8')
                how = 'file'
            elif r['res'].startswith('tg:'):
                data, mime, name = ctx.TGS.media_file(mid)      # downloads from Telegram on first request
                how = 'file'
            else:
                how, data, mime, name = media.fetch(STATE['acc'], r['chat'], r['res'], ctx.IK, thumb, r['ts'] if 'ts' in r.keys() else None)
            # several files share this name and the copy could not be tied to this message for certain
            unsure = bool(name) and name.endswith(media.UNSURE)
            if unsure:
                name = name[:-len(media.UNSURE)]
            if how is None:
                return self.send(json.dumps({'error': data}, ensure_ascii=False), 404, 'application/json; charset=utf-8')
            from urllib.parse import quote
            # Files come from chat partners: never let them run in this origin (it serves /token).
            risky = mime.startswith('application/') or any(x in mime for x in ('html', 'xml', 'svg', 'javascript'))
            disp = f"{'attachment' if risky else 'inline'}; filename*=UTF-8''{quote(name)}"
            if how == 'bytes':
                self.send_response(200); self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data))); self.send_header('Content-Disposition', disp); self.sec_headers()
                self.send_header('Cache-Control', 'private, max-age=86400'); self.end_headers()
                return self.wfile.write(data)
            size = os.path.getsize(data); start, end = 0, size - 1
            m = re.match(r'bytes=(\d*)-(\d*)$', self.headers.get('Range', ''))
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1)); end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416); self.send_header('Content-Range', f'bytes */{size}'); self.end_headers(); return
                self.send_response(206); self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            else:
                self.send_response(200)
            self.send_header('Content-Type', mime); self.send_header('Accept-Ranges', 'bytes')
            if unsure:
                self.send_header('X-Brief-Warning', 'same-name-file-uncertain')
            self.send_header('Content-Length', str(end - start + 1)); self.send_header('Content-Disposition', disp); self.sec_headers()
            self.end_headers()
            with open(data, 'rb') as f:
                f.seek(start); left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk); left -= len(chunk)
        def do_GET(self):
            u = urlparse(self.path); q = parse_qs(u.query)
            thumb = q.get('thumb', [''])[0] in ('1', 'true')
            try:
                mm = re.match(r'^/(api/)?media/(\d+)$', u.path)
                if mm and not mm.group(1):
                    return self.send_media(int(mm.group(2)), thumb)
                if u.path == '/api/help':
                    return self.send(help_text(base_url(self), q.get('for', ['all'])[0]), ctype='text/plain; charset=utf-8')
                if u.path.startswith('/api/'):
                    key = ctx.APIKEYS.check(self.headers.get('Authorization', ''))
                    if not key:
                        return self.send('{"error":"unauthorized"}', 401, 'application/json')
                    if mm and mm.group(1):
                        return self.send_media(int(mm.group(2)), thumb)
                    if u.path == '/api/drafts':
                        res = draft_list(q.get('chat', [None])[0])
                    elif u.path == '/api/status':
                        res = {'key': K.state, 'capture': K.capture_note, 'image_key': ctx.IK.state, 'capturer': K.capturer_alive(),
                               'telegram': {k: v for k, v in ctx.TGS.status().items() if k in ('configured', 'state', 'me')},
                               'bots': ctx.BOTS.summary() if ctx.BOTS else None,
                               'platform': sys.platform, 'zstd': 'ok' if wxdb.zstd_ok() else 'missing',
                               **{k: v for k, v in STATE.items() if k != 'acc'}}
                    elif u.path == '/api/bots':
                        res = ctx.BOTS.list(key['id'])
                    elif u.path == '/api/wait':
                        res = wait_new(q, key)
                    else:
                        res = api(u.path, q, agent=True)
                    if res is None:
                        return self.send('{"error":"not found"}', 404, 'application/json')
                    return self.send(json.dumps(res, ensure_ascii=False), ctype='application/json; charset=utf-8')
                if u.path == '/': self.send(home(K, q))
                elif u.path == '/chat': self.send(chat(q))
                elif u.path == '/tg': self.send(tg_home(q))
                elif u.path == '/tgsearch': self.send(tgsearch_page(q.get('q', [''])[0]))
                elif u.path == '/ping': self.send(json.dumps({'v': ping_version(K, q)}), ctype='application/json')
                elif u.path == '/sync': self.send(json.dumps(sync_part(q), ensure_ascii=False), ctype='application/json; charset=utf-8')
                elif u.path == '/older':
                    try:
                        res = older_page(q.get('c', [''])[0], int(q.get('ts', ['0'])[0]), int(q.get('id', ['0'])[0]))
                    except tg.TGError as e:
                        res = e.as_json()
                    self.send(json.dumps(res, ensure_ascii=False), ctype='application/json; charset=utf-8')
                elif u.path == '/settings': self.send(settings_page(K))
                elif u.path in ('/keys', '/token'):
                    self.send(keys_page(base_url(self), q.get('new', [''])[0]))
                elif u.path == '/healthz': self.send('ok', ctype='text/plain')
                else: self.send('not found', 404, 'text/plain')
            except (BrokenPipeError, ConnectionResetError):
                pass
            except tg.TGError as e:
                tg_reply(self, e)
            except Exception as e:
                log('http', traceback.format_exc()); self.send(f'error: {html.escape(str(e))}', 500)
        def redirect(self, to):
            self.send_response(303); self.send_header('Location', to); self.send_header('Content-Length', '0'); self.end_headers()
        def do_DELETE(self):
            try:
                if not ctx.APIKEYS.check(self.headers.get('Authorization', '')):
                    return self.send('{"error":"unauthorized"}', 401, 'application/json')
                m = re.match(r'^/api/drafts/(\d+)$', urlparse(self.path).path)
                if m:
                    ok = draft_dismiss(int(m.group(1)))
                    return self.send(json.dumps({'ok': ok}), 200 if ok else 404, 'application/json')
                self.send('{"error":"not found"}', 404, 'application/json')
            except Exception as e:
                log('http', traceback.format_exc()); self.send(json.dumps({'error': str(e)}), 500, 'application/json')
        def do_POST(self):
            u = urlparse(self.path)
            try:
                if u.path == '/api/drafts':
                    if not ctx.APIKEYS.check(self.headers.get('Authorization', '')):
                        return self.send('{"error":"unauthorized"}', 401, 'application/json')
                    f, err = self.json_body(u.path)
                    if f is None:
                        return err
                    d = draft_create(f.get('chat', ''), f.get('body', ''), f.get('note', ''))
                    if d is None:
                        return self.send('{"error":"chat and body required"}', 400, 'application/json')
                    return self.send(json.dumps(d, ensure_ascii=False), 201 if 'id' in d else 200,
                                     'application/json; charset=utf-8')
                if u.path in ('/api/send', '/api/send_file', '/api/edit', '/api/react', '/api/bots', '/api/bots/release', '/api/bots/adopt', '/api/bots/commands', '/api/bots/user_topic', '/api/bots/rename'):
                    key = ctx.APIKEYS.check(self.headers.get('Authorization', ''))
                    if not key:
                        return self.send('{"error":"unauthorized"}', 401, 'application/json')
                    f, err = self.json_body(u.path)
                    if f is None:
                        return err
                    if u.path == '/api/send_file':
                        to = (f.get('chat') or '').strip()
                        bot = bool(ctx.BOTS and tgbot.is_bot(to))
                        if not bot and not (tg.is_tg(to) and to != BOTFATHER and (to == 'tg:me' or to in ctx.TGS.allowed())):
                            raise tg.TGError('unknown_chat', '只能发到你用 POST /api/bots 开的话题、收藏夹 tg:me，或设置页直接发送白名单里的 Telegram 会话')
                        try:
                            raw = base64.b64decode(f.get('data') or '', validate=True)
                        except Exception:
                            raise tg.TGError('bad_file', 'data 要是 base64')
                        if bot:
                            res = ctx.BOTS.send_file(key, to, f.get('name', ''), raw, f.get('text', ''))
                        else:
                            res = ctx.TGS.send_file(to, f.get('name', ''), raw, f.get('text', ''))
                    elif u.path == '/api/send':
                        res = api_send(f.get('chat', ''), f.get('text', ''), f.get('note', ''), key, f.get('buttons'))
                    elif u.path in ('/api/edit', '/api/react'):
                        if not (ctx.BOTS and tgbot.is_bot(f.get('chat', ''))):
                            raise tg.TGError('unknown_chat', '只能在你用 POST /api/bots 开的话题里用')
                        if u.path == '/api/edit':
                            res = ctx.BOTS.edit(key, f.get('chat', ''), f.get('id'), f.get('text', ''), f.get('buttons'))
                        else:
                            res = ctx.BOTS.react(key, f.get('chat', ''), f.get('id'), f.get('emoji', '👀'))
                    elif u.path == '/api/bots/adopt':
                        res = ctx.BOTS.adopt(key)
                    elif u.path == '/api/bots/user_topic':
                        res = ctx.BOTS.user_topic(key, f.get('name', ''), f.get('pin') is True)
                    elif u.path == '/api/bots/rename':
                        res = ctx.BOTS.rename(key, f.get('chat', ''), f.get('name', ''))
                    elif u.path == '/api/bots/commands':
                        res = ctx.BOTS.commands(key, f.get('commands'))
                    elif u.path == '/api/bots':
                        res = ctx.BOTS.claim(key, f.get('purpose', ''))
                    else:
                        res = ctx.BOTS.release(key, f.get('chat', ''))
                    return self.send(json.dumps(res, ensure_ascii=False), ctype='application/json; charset=utf-8')
                # Key management is web-only (on Lazycat behind its login; on desktop only reachable
                # because we bind to 127.0.0.1); /api/ is the public path and stays read-only.
                if u.path not in ('/keys/create', '/keys/revoke', '/keys/reset', '/drafts/dismiss', '/capture/start', '/capture/cancel',
                                  '/tg/config', '/tg/login', '/tg/password', '/tg/logout', '/tg/send', '/refresh',
                                  '/bots/config', '/bots/setup', '/proxy/config', '/tg/allow'):
                    return self.send('not found', 404, 'text/plain')
                # CSRF guard. Some webviews send neither header; the Lazycat login cookie is what really gates this.
                origin = self.headers.get('Origin') or self.headers.get('Referer') or ''
                if origin and origin != 'null' and urlparse(origin).netloc != urlparse(base_url(self)).netloc:
                    log('apikey: rejected POST from', origin[:80])
                    return self.send(page('出错了', '<p>请求来源不对，请从本页面操作。</p><p><a class="file" href="/keys">← 回到 key 管理</a></p>'), 403)
                try:
                    f = parse_qs(self.body(FORM_MAX).decode('utf-8', 'replace'))
                except BodyError as e:
                    return self.send(page('出错了', f'<p>{html.escape(str(e))}</p>'), e.code)
                if u.path == '/bots/config':
                    ctx.BOTS.set_config(idle_hours=int(f.get('idle_hours', ['72'])[0] or 72))
                    log('bots: settings saved')
                    return self.redirect('/settings#bots')
                if u.path == '/bots/setup':
                    ctx.BOTS.set_config(setup=True)
                    log('bots: setup requested from the page')
                    return self.redirect('/settings#bots')
                if u.path == '/refresh':
                    return self.redirect(do_refresh(f.get('to', ['/'])[0]))
                if u.path == '/tg/config':
                    ctx.TGS.set_config(f.get('api_id', [''])[0], f.get('api_hash', [''])[0], f.get('proxy', [''])[0])
                    log('telegram: settings saved')
                    return self.redirect('/settings')
                if u.path == '/proxy/config':
                    cur = ctx.TGS.config().get('proxy', '')
                    if f.get('off'):
                        proxyctl.turn_off()
                        ctx.TGS.set_config(proxy=proxyctl.with_local(cur, False))
                        log('proxy: turned off')
                    else:
                        sub = f.get('sub', [''])[0].strip()
                        if sub:
                            try:
                                proxyctl.set_sub(sub)
                            except ValueError as ex:
                                return self.send(page('出错了', f'<p>{html.escape(str(ex))}</p>'), 400)
                        if sub or proxyctl.enabled():
                            ctx.TGS.set_config(proxy=proxyctl.with_local(cur, True))
                            log('proxy: subscription saved')
                    return self.redirect('/settings#proxy')
                if u.path == '/tg/allow':
                    cur = ctx.TGS.allowed()
                    add, rm = f.get('add', [''])[0].strip(), f.get('del', [''])[0].strip()
                    if add and add != BOTFATHER:
                        cur.append(add); log('allowlist: added', add)
                    if rm:
                        cur = [x for x in cur if x != rm]; log('allowlist: removed', rm)
                    ctx.TGS.set_allowed(cur)
                    return self.redirect('/settings#allow')
                if u.path == '/tg/login':
                    ctx.TGS.login_qr(); return self.redirect('/settings')
                if u.path == '/tg/password':
                    ctx.TGS.password(f.get('pw', [''])[0]); log('telegram: logged in'); return self.redirect('/settings')
                if u.path == '/tg/logout':
                    ctx.TGS.logout(); log('telegram: logged out'); return self.redirect('/settings')
                if u.path == '/tg/send':
                    d = draft_get(int(f.get('id', ['0'])[0] or 0))
                    if not d or d['hidden']:
                        return self.redirect('/tg')
                    ctx.TGS.send_approved(d['chat'], d['body'])
                    draft_dismiss(d['id'])
                    return self.redirect('/tg')
                if u.path == '/capture/start':
                    acc = account_dir()
                    if acc:
                        K.request_capture(acc + '/db_storage/contact/contact.db', force=f.get('force', [''])[0] == '1')
                        log('key: capture requested from the page')
                    return self.redirect('/')
                if u.path == '/capture/cancel':
                    K.cancel_capture()
                    log('key: capture cancelled from the page')
                    return self.redirect('/')
                if u.path == '/keys/create':
                    k = ctx.APIKEYS.create(f.get('name', [''])[0])
                    log('apikey: created', k['id'])
                    return self.redirect(f"/keys?new={k['id']}#k{k['id']}")
                kid = f.get('id', [''])[0]
                if u.path == '/keys/reset':
                    if ctx.APIKEYS.reset(kid):
                        log('apikey: reset', kid)
                    return self.redirect(f"/keys?new={kid}#k{kid}")
                if u.path == '/drafts/dismiss':
                    if kid.isdigit():
                        draft_dismiss(int(kid))
                    return self.redirect(self.headers.get('Referer') or '/')
                if ctx.APIKEYS.revoke(kid):
                    log('apikey: revoked', kid)
                return self.redirect('/keys')
            except tg.TGError as e:
                if self.path.startswith('/api/'):
                    tg_reply(self, e)
                else:
                    self.send(page('出错了', f'<p class="err">{html.escape(e.msg)}</p><p><a class="file" href="/settings">← 回到设置</a></p>'), 400)
            except Exception as e:
                log('http', traceback.format_exc()); self.send(f'error: {html.escape(str(e))}', 500)
    return H

def main():
    if len(sys.argv) >= 3 and sys.argv[1] == '--scan-mem':
        import memscan
        memscan.helper_main(sys.argv[2:])
        return
    if sys.platform == 'win32':
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    os.makedirs(VAR, exist_ok=True)
    init_store()
    ctx.APIKEYS = apikeys.ApiKeys()
    ctx.TGS = tg.Service(VAR, db, log)
    ctx.BOTS = tgbot.Bots(VAR, db, log, ctx.TGS, key_name=lambda kid: (ctx.APIKEYS.get(kid) or {}).get('name'),
                          proxy=lambda: ctx.TGS.config().get('proxy', ''))
    K = keymod.Keys(log, account_dir)
    ctx.KEYS = K
    ctx.IK = media.ImageKey(log)
    log('start on', sys.platform, '; key state', K.state, '; image key', ctx.IK.state,
        '; wechat dir', WECHAT, '; data', VAR)
    import signal
    def stop(sig, frame):
        log('stopping; releasing gdb if attached')
        K.release()
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=ingest_loop, args=(K,), daemon=True).start()
    ctx.BOTS.start()
    bind = runenv.bind_host()
    port = int(os.environ.get('PORT', 3000))
    log(f'listening on http://{bind}:{port}' + ('' if bind == "0.0.0.0" else ''))
    ThreadingHTTPServer((bind, port), make_handler(K)).serve_forever()

if __name__ == '__main__':
    main()
