"""Markdown as agents write it → Telegram plain text + message entities.

Entities (not parse_mode) so that a malformed message can never be rejected: whatever does not parse stays as
written. Offsets and lengths are in UTF-16 code units, as Telegram counts them.

Handled: ``` fenced blocks ```, `code`, **bold**, *italic*, ~~strike~~, [text](url), # headings (bold),
> quotes, - / * / + list bullets (shown as •), backslash escapes. Anything else (tables…) is left as is.
"""
import re

_LINK = re.compile(r'\[([^\]\n]+)\]\(((?:https?|tg|mailto):[^)\s]+)\)')
_ITALIC = re.compile(r'\*(?=\S)([^*\n]+?)(?<=\S)\*')
_FENCE = re.compile(r'^\s*```\s*([\w+-]*)\s*$')
_HEAD = re.compile(r'^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$')
_QUOTE = re.compile(r'^\s{0,3}>\s?(.*)$')
_BULLET = re.compile(r'^(\s*)[-*+]\s+(.*)$')
_PUNCT = set('\\`*_{}[]()#+-.!~>|')


def _u16(s):
    return len(s.encode('utf-16-le')) // 2


class _Out:
    def __init__(self):
        self.parts, self.n, self.ents = [], 0, []

    def put(self, s):
        if s:
            self.parts.append(s)
            self.n += _u16(s)

    def ent(self, kind, start, **kw):
        if self.n > start:
            self.ents.append(dict(type=kind, offset=start, length=self.n - start, **kw))

    def text(self):
        return ''.join(self.parts)


def _inline(s, o):
    i, lit = 0, []

    def flush():
        if lit:
            o.put(''.join(lit))
            lit.clear()

    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s) and s[i + 1] in _PUNCT:
            lit.append(s[i + 1]); i += 2; continue
        if c == '`':
            j = s.find('`', i + 1)
            if j > i + 1:
                flush(); st = o.n; o.put(s[i + 1:j]); o.ent('code', st); i = j + 1; continue
        if s.startswith('**', i) or s.startswith('~~', i):
            mark = s[i:i + 2]
            j = s.find(mark, i + 2)
            if j > i + 2:
                flush(); st = o.n; _inline(s[i + 2:j], o); o.ent('bold' if mark == '**' else 'strikethrough', st); i = j + 2; continue
        if c == '[':
            m = _LINK.match(s, i)
            if m:
                flush(); st = o.n; _inline(m.group(1), o); o.ent('text_link', st, url=m.group(2)); i = m.end(); continue
        if c == '*' and (i == 0 or not s[i - 1].isalnum()):
            m = _ITALIC.match(s, i)
            if m:
                flush(); st = o.n; _inline(m.group(1), o); o.ent('italic', st); i = m.end(); continue
        lit.append(c); i += 1
    flush()


def render(md):
    """Return (text, entities). Entities is a list of dicts ready for the Bot API."""
    o = _Out()
    lines = (md or '').split('\n')
    i, quote_start = 0, None
    while i < len(lines):
        line = lines[i]
        if i:
            o.put('\n')
        q = _QUOTE.match(line)
        if q is None and quote_start is not None:
            # the newline just written belongs after the quote
            o.n -= 1; o.ent('blockquote', quote_start); o.n += 1
            quote_start = None
        f = _FENCE.match(line)
        if f:
            end = next((k for k in range(i + 1, len(lines)) if lines[k].strip() == '```'), None)
            if end is not None:
                st = o.n
                o.put('\n'.join(lines[i + 1:end]))
                o.ent('pre', st, **({'language': f.group(1)} if f.group(1) else {}))
                i = end + 1
                continue
        h = _HEAD.match(line)
        if h:
            st = o.n; _inline(h.group(1), o); o.ent('bold', st)
        elif q:
            if quote_start is None:
                quote_start = o.n
            _inline(q.group(1), o)
        else:
            b = _BULLET.match(line)
            if b and not re.match(r'^\s*([-*_])(\s*\1){2,}\s*$', line):
                o.put(b.group(1) + '• '); _inline(b.group(2), o)
            else:
                _inline(line, o)
        i += 1
    if quote_start is not None:
        o.ent('blockquote', quote_start)
    o.ents.sort(key=lambda e: (e['offset'], -e['length']))
    return o.text(), o.ents
