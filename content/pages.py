"""The phone-sized web pages. Static CSS / JS live in web/."""
import html, json, os, threading, time
import urllib.parse
from urllib.parse import parse_qs

import ctx, tg, runenv, tgbot
from api import agent_line, api, draft_list, rows_json
from ctx import INGEST_NOW, STATE, db


WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

def _asset(name):
    with open(os.path.join(WEB, name), encoding='utf-8') as f:
        return f.read()

def drafts_html(drafts):
    if not drafts:
        return ''
    out = ['<div class="drafts"><h4 class="dh">建议回复 <span class="sub">agent 起草，复制、发送或忽略</span></h4>']
    for d in drafts:
        out.append(f'<div class="draft"><div class="sub">给 {html.escape(d["chat_name"])}</div>'
                   f'<div class="db">{html.escape(d["body"])}</div>'
                   + (f'<div class="sub">{html.escape(d["note"])}</div>' if d['note'] else '') +
                   f'<div class="da"><button class="pri" type="button" onclick="cpdraft(this)" data-t="{html.escape(d["body"], quote=True)}">复制</button>'
                   + (f'<form class="inline two" method="post" action="/tg/send"><input type="hidden" name="id" value="{d["id"]}">'
                      f'<button class="pri" data-ask="再点一次：发给 {html.escape(d["chat_name"])}">发送</button></form>' if tg.is_tg(d['chat']) else '') +
                   f'<form class="inline two" method="post" action="/drafts/dismiss">'
                   f'<input type="hidden" name="id" value="{d["id"]}"><button data-ask="确认不再显示">忽略</button></form>'
                   f'<span class="sub" style="margin-left:8px">{fmt(d["created"])}</span></div></div>')
    return ''.join(out) + '</div>' + DRAFT_JS + TWO_JS

DRAFT_JS = '<script>' + _asset('drafts.js') + '</script>'

# ---------------- web ----------------
CSS = _asset('style.css')

AUTO_JS = '<script>' + _asset('auto.js') + '</script>'

def auto_toggle():
    return '<button type="button" class="tab2" id="autot" style="margin:0">自动刷新</button>'

LOAD_HTML = _asset('loading.html')

def page(title, body, auto=''):
    js = AUTO_JS.replace('__QUERY__', auto) if auto else ''
    return (f'<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}{js}</main>{LOAD_HTML}</body></html>')

def fmt(ts):
    return time.strftime('%m-%d %H:%M', time.localtime(ts)) if ts else '-'

IK_TEXT = {'ok': '正常', 'deriving': '正在获取', 'none': '尚未获取',
           'no_images': '暂时无法获取：需要有一张已下载的图片来校验，收到并点开一张图后会自动完成'}

TWO_JS = '<script>' + _asset('two.js') + '</script>'

RELOAD_JS = '<script>setTimeout(function(){location.reload()},5000)</script>'

def _key_body(K):
    wx = runenv.wx_label()
    ik = IK_TEXT.get(ctx.IK.state, ctx.IK.state)
    svc = ''
    if runenv.LAZYCAT:
        svc = ' · 抓取服务：' + ('运行中' if K.capturer_alive() else '<b class="err">没有运行，请在懒猫里重启本应用</b>')
    status = f'{wx}密钥：<b>{"正常" if K.state == "ok" else "失效"}</b> · 图片密钥：{html.escape(ik)}{svc}'
    note = html.escape(K.capture_note or '等待中')
    if K.pending():
        if runenv.LAZYCAT:
            how = '请在云微信里退出登录，再重新扫码登录。登录的瞬间会自动取得密钥，这一页会自动刷新。'
        else:
            how = f'保持{wx}运行并登录，应用正在扫描进程取得密钥。'
        return (f'<div class="card"><p>{status}</p><p><b>正在抓取</b>：{how}</p><p class="na">进度：{note}</p>'
                '<form method="post" action="/capture/cancel"><button>取消</button></form></div>' + RELOAD_JS)
    if K.state != 'ok':
        if runenv.LAZYCAT:
            steps = '<li>打开云微信，退出登录。</li><li>重新扫码登录。</li><li>登录的瞬间自动取得新密钥，不需要做别的。</li>'
        else:
            steps = f'<li>保持{wx}运行并登录。</li><li>点下面的按钮，应用会扫描{wx}进程取得密钥。</li>'
        return (f'<div class="card err"><p>{status}</p><p><b>暂时读不到新消息</b></p><ol>{steps}</ol>'
                f'<p class="na">进度：{note}</p>'
                '<form method="post" action="/capture/start"><button class="pri">开始抓取</button></form></div>' + RELOAD_JS)
    return (f'<div class="card"><p>{status}</p>'
            '<p class="na">一般不需要操作。只有云微信的数据被重建、密钥变了才用得上；平时密钥失效时，页面会自动提示并自动抓取。</p>'
            '<form class="two" method="post" action="/capture/start"><input type="hidden" name="force" value="1">'
            '<button data-ask="再点一次：开始等待，然后到云微信重新登录">重新获取密钥</button></form></div>' + TWO_JS)

def refresh_btn(to):
    return (f'<form class="inline" method="post" action="/refresh"><input type="hidden" name="to" value="{html.escape(to, quote=True)}">'
            '<button class="btn" type="submit" style="margin:0">刷新</button></form>')

OLDER_JS = '<script>' + _asset('older.js') + '</script>'
FOLD_JS = '<script>' + _asset('fold.js') + '</script>'

def snippet(text, q, width=70):
    """The text around the first match, with the match highlighted."""
    t = text or ''
    i = t.lower().find(q.lower()) if q else -1
    if i < 0:
        return html.escape(t[:120])
    a, b = max(0, i - width // 3), min(len(t), i + len(q) + width)
    return (('…' if a > 0 else '') + html.escape(t[a:i]) + '<mark>' + html.escape(t[i:i + len(q)]) + '</mark>'
            + html.escape(t[i + len(q):b]) + ('…' if b < len(t) else ''))

def tgsearch_page(q):
    out = [topbar('tg'), f'<form action="/tgsearch"><input name="q" value="{html.escape(q)}" placeholder="在 Telegram 里搜全部历史"></form>']
    if q.strip():
        try:
            hits = ctx.TGS.search_live(q, None, 50)
        except tg.TGError as e:
            out.append(f'<div class="err">{html.escape(e.msg)}' + (f'（{e.retry_after} 秒后再试）' if e.retry_after else '') + '</div>')
            hits = []
        else:
            out.append('<p class="sub">Telegram 全部历史里的命中（现查，没有存进库里）</p>')
        for r in hits:
            out.append(f'<a class="row" href="/chat?c={urllib.parse.quote(r["chat"])}"><div>{snippet(r["text"], q)}</div>'
                       f'<div class="sub">{html.escape(r["chat_name"])} · {fmt(r["ts"])}' + (f' · {html.escape(r["sender"])}' if r['sender'] else '') + '</div></a>')
        if not hits and 'err' not in ''.join(out):
            out.append('<p class="sub">没有找到。</p>')
        elif len(hits) >= 50:
            out.append('<p class="sub">只显示最新的 50 条命中；关键词再具体一点会更准。</p>')
    return page('搜 Telegram', ''.join(out))

def search_html(q, source):
    """Chats whose name matches, then messages whose text matches."""
    out = []
    chats = api('/api/chats', {'q': [q], 'source': [source], 'limit': ['8']}) if source == 'wechat' else []
    for r in chats:
        out.append(f'<a class="row" href="/chat?c={urllib.parse.quote(r["chat"])}"><div>{html.escape(r["name"])} <span class="sub">会话</span></div>'
                   f'<div class="sub">{fmt(r["last_ts"])} · {r["n"]} 条</div></a>')
    hits = api('/api/search', {'q': [q], 'source': [source], 'limit': ['50']})
    for r in hits:
        out.append(f'<a class="row" href="/chat?c={urllib.parse.quote(r["chat"])}&around={r["id"]}#m{r["id"]}"><div>{snippet(r["text"], q)}</div>'
                   f'<div class="sub">{html.escape(r["chat_name"])} · {fmt(r["ts"])}</div></a>')
    if not out:
        out.append('<p class="sub">没有找到。</p>')
    elif len(hits) >= 50:
        out.append('<p class="sub">只显示最新的 50 条命中。</p>')
    return ''.join(out)

PINNED = ('tg:me', 'filehelper')    # Saved Messages, 文件传输助手: shown first in the page lists only

def pin_first(rows):
    """Display order only: nothing is stored and the API keeps its time order. sorted() is stable."""
    return sorted(rows, key=lambda r: 0 if r['chat'] in PINNED else 1)

def pin_tag(r):
    return ' <span class="sub">置顶</span>' if r['chat'] in PINNED else ''

def topbar(active):
    on = lambda k: ' class="on"' if active == k else ''
    return (f'<div class="top"><b>消息简报</b><span>{refresh_btn("/" if active == "wx" else "/tg")} <a class="btn" href="/settings">设置</a> <a class="btn" href="/keys">API key</a></span></div>'
            f'<div class="tabs"><a{on("wx")} href="/">微信</a><a{on("tg")} href="/tg">Telegram</a></div>')

def home(K, q):
    s = q.get('q', [''])[0]
    out = [topbar('wx'), f'<form><input name="q" value="{html.escape(s)}" placeholder="搜会话或消息"></form>']
    if K.state != 'ok' or K.pending():
        out.append('<a class="err" style="display:block" href="/settings">' + runenv.wx_label() +
                   '密钥有问题，读不到新消息 · 去设置处理 →</a>')
    if not s:
        out.append(drafts_html([d for d in draft_list() if not tg.is_tg(d['chat'])]))
    if K.state == 'ok' and STATE['error']:
        out.append(f'<div class="err">{html.escape(STATE["error"])}</div>')
    if s:
        out.append(search_html(s, 'wechat'))
    else:
        rows = api('/api/chats', {'source': ['wechat'], 'limit': ['200']})
        if not any(r['chat'] == 'filehelper' for r in rows):          # may be older than the 200 most recent chats
            rows += [r for r in api('/api/chats', {'q': ['filehelper'], 'source': ['wechat'], 'limit': ['5']}) if r['chat'] == 'filehelper']
        for r in pin_first(rows):
            out.append(f'<a class="row" href="/chat?c={html.escape(r["chat"])}"><div>{html.escape(r["name"])}{pin_tag(r)}</div>'
                       f'<div class="sub">{fmt(r["last_ts"])} · {r["n"]} 条</div></a>')
    out.append(f'<p class="sub">读取于 {fmt(STATE["last_ingest"])} · {auto_toggle()}</p>')
    return page('微信', ''.join(out), 'tab=wx')

TG_BG = {'running': False, 'error': ''}

RELOAD3_JS = '<script>setTimeout(function(){location.reload()},3000)</script>'

def _tg_refresh_bg():
    if TG_BG['running']:
        return
    TG_BG['running'] = True
    def go():
        try:
            ctx.TGS.dialogs(force=True); TG_BG['error'] = ''
        except tg.TGError as e:
            TG_BG['error'] = e.msg
        finally:
            TG_BG['running'] = False
    threading.Thread(target=go, daemon=True).start()

def bots_fold():
    """The bot pool as one folded row at the top of the Telegram tab (display only)."""
    bots = ctx.BOTS.list() if ctx.BOTS else []
    if not bots:
        return ''
    c = db()
    rows, latest = [], (0, '')
    try:
        for b in bots:
            m = c.execute('select ts, sender, text from msgs where chat=? order by ts desc, id desc limit 1', (b['chat'],)).fetchone()
            last_in = c.execute("select coalesce(max(id),0) from msgs where chat=? and sender != '我'", (b['chat'],)).fetchone()[0]
            if m and m['ts'] > latest[0]:
                latest = (m['ts'], f"{b['name']}：{(m['text'] or '')[:30]}")
            state = (f"使用中 · {html.escape(b['holder'] or '')} · 最后活动 {fmt(b['last_activity'])}" if b['status'] == 'busy' else '空闲')
            rows.append(f'<a class="row" href="/chat?c={urllib.parse.quote(b["chat"])}" data-chat="{html.escape(b["chat"])}" data-last="{last_in}">'
                        f'<div>{html.escape(b["name"])}</div><div class="sub">{state}' +
                        (f' · {html.escape((m["text"] or "")[:40])}' if m else '') + '</div></a>')
    finally:
        c.close()
    busy = sum(1 for b in bots if b['status'] == 'busy')
    return (f'<div class="fold" id="botfold"><div><span class="arrow">▶</span>exterior <span class="sub">{len(bots)} 个 bot · {busy} 个在用</span>'
            f'<span class="badge" id="botnew" hidden></span><div class="sub">{html.escape(latest[1]) if latest[0] else "还没有消息"}</div></div>'
            f'<span class="sub">{fmt(latest[0]) if latest[0] else ""}</span></div><div id="botlist" hidden>{"".join(rows)}</div>' + FOLD_JS)

def tg_home(q):
    s = q.get('q', [''])[0].lower()
    out = [topbar('tg')]
    st = ctx.TGS.status()
    if not st['configured']:
        return page('Telegram', ''.join(out) + '<div class="card"><p>还没有设置 Telegram。</p>'
                    '<p><a class="btn" href="/settings">去设置</a></p></div>')
    if not st['has_session'] and st['state'] != 'ready':
        return page('Telegram', ''.join(out) + '<div class="card"><p>还没有登录 Telegram。</p>'
                    '<p><a class="btn" href="/settings">去设置，扫码登录</a></p></div>')
    rows, at = ctx.TGS.cached_dialogs()
    if not rows or time.time() - at > 300:
        _tg_refresh_bg()
    out.append(f'<form><input name="q" value="{html.escape(s)}" placeholder="搜会话"></form>')
    if TG_BG['error']:
        out.append(f'<div class="err">{html.escape(TG_BG["error"])}</div>')
    out.append(drafts_html([d for d in draft_list() if tg.is_tg(d['chat'])]))
    s = s.lstrip('@')
    dup = ctx.BOTS.user_side_chats() if ctx.BOTS else set()      # the pool bots are shown in the folded row instead
    shown = [r for r in rows if r['chat'] not in dup and (not s or s in (r['name'] or '').lower() or s in (r['username'] or '').lower())]
    raw_q = q.get('q', [''])[0].strip()
    pinned = [r for r in pin_first(shown) if r['chat'] in PINNED]
    for i, r in enumerate(pinned + [r for r in shown if r['chat'] not in PINNED] + [None]):
        if i == len(pinned) and not raw_q:
            out.append(bots_fold())
        if r is None:
            break
        mark = {'u': '', 'g': '群 · ', 'c': '频道 · ', 'me': ''}.get(r['kind'], '')
        out.append(f'<a class="row" href="/chat?c={html.escape(r["chat"])}"><div>{html.escape(r["name"] or r["chat"])}{pin_tag(r)}</div>'
                   f'<div class="sub">{mark}{fmt(r["last_ts"])}' + (f' · 已读取 {r["n"]} 条' if r['n'] else '') + '</div></a>')
    if raw_q and shown:
        out.append('<p class="sub" style="margin-top:10px">会话</p>')
    if not shown and not raw_q:
        out.append('<p class="sub">' + ('正在读取会话列表…' if TG_BG['running'] or not rows else '没有匹配的会话') + '</p>')
    if raw_q:
        out.append('<p class="sub" style="margin-top:14px">消息（只搜已经读取过的会话）</p>' + search_html(raw_q, 'telegram'))
        out.append(f'<p><a class="btn" href="/tgsearch?q={urllib.parse.quote(raw_q)}">在 Telegram 里搜全部历史 →</a></p>')
    sent = ctx.TGS.sent_log()
    if sent:
        out.append('<details style="margin-top:18px"><summary class="na" style="cursor:pointer">已发送（最近 30 条）</summary>' +
                   ''.join(f'<div class="m"><span class="sub">{fmt(x["ts"])} · {html.escape(x["chat_name"] or x["chat"])} · '
                           f'{"直接发送" if x["via"] == "direct" else "你确认后发送"}</span><br>{html.escape(x["body"])}</div>' for x in sent) +
                   '</details>')
    if TG_BG['running']:
        out.append(RELOAD3_JS)
    out.append(f'<p class="sub">{auto_toggle()}</p>')
    return page('Telegram', ''.join(out), 'tab=tg')

def chat(q):
    cid = q.get('c', [''])[0]
    c = db()
    name = (c.execute('select name from chats where chat=?', (cid,)).fetchone() or [cid])[0]
    c.close()
    isbot = tgbot.is_bot(cid)
    back = '/tg' if tg.is_tg(cid) or isbot else '/'
    here = '/chat?c=' + urllib.parse.quote(cid)
    out = [f'<div class="top"><a class="sub" href="{back}">← 返回</a><span>{auto_toggle()} {refresh_btn(here)}</span></div><h3>{html.escape(name)}</h3>']
    if isbot and ctx.BOTS:
        b = ctx.BOTS.get(cid) or {}
        out.append('<p class="sub">' + (f"{html.escape(b.get('holder_name') or '')} 在用 · 用途：{html.escape(b.get('purpose') or '')} · "
                                        f"最后活动 {fmt(b.get('last_activity'))}" if b.get('holder') else '空闲，还没有 agent 在用') +
                   f" · @{html.escape(cid[len(tgbot.PREFIX):])}</p>")
    out.append(drafts_html(draft_list(chat=cid)))
    params = {'chat': [cid], 'limit': ['300']}
    around = q.get('around', [''])[0]
    if around.isdigit():
        # the window around a search hit, by time (ids do not follow time for history filled in later)
        c = db()
        t = c.execute('select ts, id from msgs where id=?', (int(around),)).fetchone()
        lo = c.execute('select min(ts) from (select ts from msgs where chat=? and (ts < ? or (ts = ? and id < ?)) '
                       'order by ts desc, id desc limit 100)', (cid, t['ts'], t['ts'], t['id'])).fetchone()[0] if t else None
        c.close()
        if t:
            params = {'chat': [cid], 'since': [str((lo if lo is not None else t['ts']) - 1)], 'limit': ['300']}
        out.append(f'<p class="sub"><a class="file" href="/chat?c={urllib.parse.quote(cid)}">跳到最新消息 ↓</a></p>')
    if tg.is_tg(cid):
        try:
            ctx.TGS.fetch(cid)
        except tg.TGError as e:
            out.append(f'<div class="err">现在读不到 Telegram 的新消息：{html.escape(e.msg)}'
                       + (f'（{e.retry_after} 秒后再试）' if e.retry_after else '') + '。下面是已缓存的。</div>')
        params['live'] = ['0']
    out.append(f'<div id="olderbar" class="sub" style="text-align:center;height:22px;line-height:22px"></div><div id="msgs" data-chat="{html.escape(cid, quote=True)}">')
    for r in sorted(api('/api/messages', params), key=lambda r: (r['ts'], r['id'])):
        out.append(msg_html(r))
    out.append('</div>' + OLDER_JS)
    if isbot:      # remember what has been read, for the unread badge on the folded bot row
        c = db(); top = c.execute('select coalesce(max(id),0) from msgs where chat=?', (cid,)).fetchone()[0]; c.close()
        out.append(f"<script>try{{localStorage.setItem({json.dumps('wxb_seen_' + cid)},'{top}')}}catch(e){{}}</script>")
    return page(name, ''.join(out), 'tab=chat&c=' + urllib.parse.quote(cid))

def msg_html(r):
    return (f'<div class="m" id="m{r["id"]}" data-ts="{r["ts"]}" data-id="{r["id"]}"><span class="sub">{fmt(r["ts"])}</span> '
            f'<span class="w">{html.escape(r["sender"] or "")}</span>：{media_html(r)}</div>')

def older_page(cid, ts, mid, n=100):
    """The `n` messages just before (ts, id), oldest first, as HTML. Telegram chats read further back on demand."""
    def rows_before():
        c = db()
        try:
            return c.execute('select m.*, coalesce(ch.name, m.chat) name from msgs m left join chats ch on ch.chat=m.chat '
                             'where m.chat=? and (m.ts < ? or (m.ts = ? and m.id < ?)) order by m.ts desc, m.id desc limit ?',
                             (cid, ts, ts, mid, n + 1)).fetchall()
        finally:
            c.close()
    rows = rows_before()
    istg = tg.is_tg(cid)
    if istg and len(rows) <= n and not ctx.TGS.at_start(cid):
        ctx.TGS.fetch_older(cid, n)                   # may raise TGError (rate limit etc.)
        rows = rows_before()
    more = len(rows) > n or (istg and not ctx.TGS.at_start(cid) and bool(rows))
    rows = rows[:n][::-1]
    return {'html': ''.join(msg_html(r) for r in rows_json(rows)), 'more': more, 'count': len(rows)}

def media_html(r):
    text = html.escape(r['text'] or '')
    mj = r.get('media')
    if not mj:
        return text
    u = f"/media/{r['id']}"
    if not mj['available'] and tg.is_tg(r['chat']):
        return f'{text} <a class="file" href="{u}">下载</a>'
    if not mj['available']:
        return f'{text} <span class="na">（原件未下载）</span>'
    if mj['kind'] == 'file':
        return f'<a class="file" href="{u}">{text}</a>'
    if mj['kind'] == 'voice':
        return f'{text}<audio class="pic" controls preload="none" src="{u}"></audio>'
    img = f'<img loading="lazy" src="{u}?thumb=1" alt="{text}">'
    if mj['kind'] == 'video':
        if mj['available'] == 'full':
            return f'<a class="file" href="{u}">{text}</a><a class="pic" href="{u}">{img}</a>'
        return f'{text} <span class="na">（只有封面）</span><span class="pic">{img}</span>'
    return f'<a class="pic" href="{u}">{img}</a>'

def base_url(handler):
    host = handler.headers.get('X-Forwarded-Host') or handler.headers.get('Host') or 'brief'
    proto = handler.headers.get('X-Forwarded-Proto') or ('https' if runenv.LAZYCAT else 'http')
    return proto + '://' + host.split(',')[0].strip()

# No confirm()/alert(): the Lazycat client's webview silently suppresses native dialogs.
KEYS_JS = '<script>' + _asset('keys.js') + '</script>'

def keys_page(base, new_id=None):
    e = html.escape
    out = ['<p><a class="sub" href="/">← 返回</a></p><h3>API key 管理</h3>',
           '<p class="sub">每个 agent 用一个 key。泄露了就“重置”（换新 key，名字不变），不用了就“吊销”。每个 key 下面是一行给 agent 的说明（已含 key），复制给 agent 即可。'
           '每个 key 有三份说明：<b>全部</b>、<b>微信</b>、<b>Telegram</b>，按这个 agent 要用的范围选一份复制（只是告诉它怎么用，key 本身的权限不变）。完整用法见 <a class="file" href="/api/help">/api/help</a>。</p>']
    keys = ctx.APIKEYS.list()
    for k in keys:
        new = k['id'] == new_id
        out.append(
            f'<div class="key" id="k{k["id"]}"><h4>{e(k["name"])}{" <span class=sub>（刚生成，复制下面的说明给 agent）</span>" if new else ""}</h4>'
            f'<div class="sub">创建于 {fmt(k["created"])} · 最后使用 {fmt(k["last_used"])} · <code>{e(k["key"][:8])}…</code></div>'
            f'<div class="tabbar"><button type="button" class="tab2 on" onclick="pick(this,\'all\')">全部</button>'
            f'<button type="button" class="tab2" onclick="pick(this,\'wechat\')">微信</button>'
            f'<button type="button" class="tab2" onclick="pick(this,\'telegram\')">Telegram</button></div>'
            + ''.join(f'<textarea rows="8" readonly data-w="{w}"{" autofocus" if new and w == "all" else ""}'
                      f'{"" if w == "all" else " hidden"}>{e(agent_line(base, k["key"], w))}</textarea>'
                      for w in ('all', 'wechat', 'telegram')) +
            f'<button class="pri" type="button" onclick="cp(this,0)">复制给 agent 的说明</button>'
            f'<button type="button" onclick="cp(this,1)" data-k="{e(k["key"])}">只复制 key</button>'
            f'<form class="inline two" method="post" action="/keys/reset">'
            f'<input type="hidden" name="id" value="{k["id"]}"><button data-ask="再点一次：换新 key，旧的立刻失效">重置</button></form>'
            f'<form class="inline two" method="post" action="/keys/revoke">'
            f'<input type="hidden" name="id" value="{k["id"]}"><button class="del" data-ask="再点一次：确认吊销">吊销</button></form></div>')
    if not keys:
        out.append('<p class="sub">还没有 key。</p>')
    out.append('<h4 style="margin-top:24px">新建 key</h4><form method="post" action="/keys/create">'
               '<input name="name" maxlength="40" placeholder="名字，比如 Claude Code、日报机器人" required>'
               '<button class="pri">新建</button></form>' + KEYS_JS)
    return page('API key', ''.join(out))

def ping_version(K, q):
    """A short token that changes when the page the user is looking at has something new."""
    tab = q.get('tab', [''])[0]; cid = q.get('c', [''])[0]
    c = db()
    try:
        d = c.execute('select count(*), coalesce(max(id),0) from drafts where hidden=0').fetchone()
        if tab == 'chat' and cid:
            if tg.is_tg(cid):
                try:
                    ctx.TGS.fetch(cid)            # at most once a minute (cache TTL); nothing runs when no page is open
                except tg.TGError:
                    pass
            m = c.execute('select coalesce(max(id),0) from msgs where chat=?', (cid,)).fetchone()[0]
        elif tab == 'tg':
            try:
                ctx.TGS.dialogs()
            except tg.TGError:
                pass
            bots_top = c.execute("select coalesce(max(id),0) from msgs where chat like 'tgbot:%'").fetchone()[0]
            m = f"{ctx.TGS._kv(c, 'tg:dialogs_at', 0)}:{c.execute('select coalesce(max(id),0) from tg_sent').fetchone()[0]}:{bots_top}"
        else:
            m = c.execute("select coalesce(max(id),0) from msgs where chat not like 'tg:%' and chat not like 'tgbot:%'").fetchone()[0]
        return f'{m}:{d[0]}-{d[1]}:{K.state}'
    finally:
        c.close()

def safe_back(to):
    """Only same-site paths; never an absolute or protocol-relative URL."""
    to = (to or '').strip()
    return to if to.startswith('/') and not to.startswith('//') and '\\' not in to else '/'

def do_refresh(to):
    """The page's refresh button: read again now, then show the same page."""
    to = safe_back(to)
    path, _, qs = to.partition('?')
    if path == '/chat':
        cid = parse_qs(qs).get('c', [''])[0]
        if tg.is_tg(cid):
            try:
                ctx.TGS.fetch(cid, force=True)
            except tg.TGError:
                pass                       # the chat page itself shows why it could not refresh
    elif path == '/tg':
        try:
            ctx.TGS.dialogs(force=True)
        except tg.TGError:
            pass
    else:
        INGEST_NOW.set(); time.sleep(2)    # let the WeChat reader run once
    return to

TG_CHECK = {'running': False, 'at': 0, 'error': ''}

def settings_page(K):
    st = ctx.TGS.status()
    e = html.escape
    out = ['<p><a class="sub" href="/">← 返回</a></p><h3>设置</h3>']
    out.append('<div class="sec"><h3>' + runenv.wx_label() + '密钥</h3>' + _key_body(K) + '</div>')
    t = ['<div class="sec"><h3>Telegram</h3>']
    state = st['state']
    if st['configured'] and st['has_session'] and state == 'logged_out':
        if not TG_CHECK['running'] and time.time() - TG_CHECK['at'] > 60:
            TG_CHECK['running'] = True; TG_CHECK['at'] = time.time()
            def chk():
                try:
                    ctx.TGS.refresh_state()
                except tg.TGError as ex:
                    TG_CHECK['error'] = ex.msg
                finally:
                    TG_CHECK['running'] = False
            threading.Thread(target=chk, daemon=True).start()
        t.append('<p class="sub">正在确认登录状态…</p>' + RELOAD3_JS)
    elif state == 'ready':
        t.append(f'<p>已登录：<b>{e(st["me"] or "")}</b></p>'
                 '<form class="two" method="post" action="/tg/logout"><button class="del" data-ask="再点一次：退出并让这个登录失效">退出 Telegram</button></form>'
                 '<p class="sub">退出会在 Telegram 服务器上注销这个登录；你也可以在手机 Telegram 的 设置 → 设备 里移除它。</p>')
    elif state == 'qr':
        try:
            svg = tg.qr_svg(st['qr_url'])
        except Exception:
            svg = ''
        t.append('<p><b>用手机扫码登录</b></p><ol><li>打开手机上的 Telegram。</li><li>设置 → 设备 → 链接桌面设备。</li><li>扫描下面的二维码。</li></ol>'
                 f'<div class="qr">{svg}</div><p class="sub">二维码约 30 秒换一次，页面会自动刷新。开了两步验证的话，扫完这里会让你输密码。</p>'
                 '<form method="post" action="/tg/logout"><button>取消</button></form>' + RELOAD3_JS)
        ctx.TGS.poll_login()
    elif state == 'password':
        t.append('<p><b>这个账号开了两步验证</b>，请输入密码：</p><form method="post" action="/tg/password">'
                 '<input type="password" name="pw" autocomplete="off" required><button class="pri">确认</button></form>')
    else:
        if st['configured']:
            t.append('<p>还没有登录。</p><form method="post" action="/tg/login"><button class="pri">用二维码登录</button></form>')
            if TG_CHECK.get('error'):
                t.append(f'<p class="err">{e(TG_CHECK["error"])}</p>')
            if st['error']:
                t.append(f'<p class="err">{e(st["error"])}</p>')
        else:
            t.append('<p class="sub">先填下面三项，保存后就能登录。</p>')
    cfg_open = '' if st['configured'] else ' open'
    t.append(f'<details{cfg_open} style="margin-top:14px"><summary class="na" style="cursor:pointer">连接设置</summary>'
             '<form method="post" action="/tg/config">'
             f'<label class="sub">api_id（在 my.telegram.org 申请）</label><input name="api_id" inputmode="numeric" value="{e(st["api_id"])}">'
             f'<label class="sub">api_hash {"（已设置，不改就留空）" if st["api_hash_set"] else ""}</label><input name="api_hash" type="password" autocomplete="off">'
             f'<label class="sub">代理（盒子连不上 Telegram 时需要）</label><input name="proxy" placeholder="socks5://地址:端口" value="{e(st["proxy"])}">'
             '<button class="pri">保存</button></form></details></div>')
    out.append(''.join(t))
    out.append(bots_settings())
    return page('设置', ''.join(out) + TWO_JS)

def bots_settings():
    if not ctx.BOTS:
        return ''
    e = html.escape
    sm = ctx.BOTS.summary()
    out = ['<div class="sec" id="bots"><h3>和 agent 对话的 bot</h3>',
           f'<p>已建 <b>{sm["total"]}</b> / 上限 {sm["size"]} · {sm["busy"]} 个在用</p>',
           '<p class="sub">agent 领一个 bot，通过它在 Telegram 上和你对话，消息会推到你手机上。bot 只认你一个人，'
           '闲置超时后自动回收。bot 用你的 Telegram 账号经 BotFather 创建；应用不会删除或修改任何 bot 的 token。</p>']
    if sm['note']:
        out.append(f'<p class="sub">进度：{e(sm["note"])}</p>')
    if sm['fill']:
        out.append('<form method="post" action="/bots/fill"><input type="hidden" name="on" value="0"><button>暂停自动建 bot</button></form>')
    elif sm['total'] < sm['size']:
        out.append(f'<form class="two" method="post" action="/bots/fill"><input type="hidden" name="on" value="1">'
                   f'<button class="pri" data-ask="再点一次：用你的 Telegram 账号建 {sm["size"] - sm["total"]} 个 bot">开始建立 bot 池</button></form>')
    for b in ctx.BOTS.list():
        state = f'{e(b["holder"] or "")} 在用 · 最后活动 {fmt(b["last_activity"])}' if b['status'] == 'busy' else '空闲'
        out.append(f'<div class="row"><a href="/chat?c={urllib.parse.quote(b["chat"])}">{e(b["name"])}</a>'
                   f'<div class="sub">@{e(b["username"])} · {state}</div></div>')
    out.append('<form method="post" action="/bots/config" style="margin-top:12px">'
               f'<label class="sub">上限（个，最多 {tgbot.MAX_SIZE}；Telegram 普通账号最多能有 20 个 bot，Premium 40 个，包括你在别处建的）</label><input name="size" inputmode="numeric" value="{sm["size"]}">'
               f'<label class="sub">闲置多久自动回收（小时）</label><input name="idle_hours" inputmode="numeric" value="{sm["idle_hours"]}">'
               '<button>保存</button></form></div>')
    return ''.join(out)
