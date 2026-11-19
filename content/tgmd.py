"""Markdown as agents write it → Telegram plain text + message entities.

Entities (not parse_mode) so that a malformed message can never be rejected: whatever does not parse stays as
written. Offsets and lengths are in UTF-16 code units, as Telegram counts them.

Handled: ``` fenced blocks ```, `code`, **bold**, *italic*, ~~strike~~, [text](url), # headings (bold),
> quotes, - / * / + list bullets (shown as •), backslash escapes. Telegram has no tables and no formulas, so
tables become one bullet per row and $…$ / $$…$$ LaTeX becomes Unicode (x², ≤, √, α…) in monospace.
"""
import re

_LINK = re.compile(r'\[([^\]\n]+)\]\(((?:https?|tg|mailto):[^)\s]+)\)')
_ITALIC = re.compile(r'\*(?=\S)([^*\n]+?)(?<=\S)\*')
_FENCE = re.compile(r'^\s*```\s*([\w+-]*)\s*$')
_HEAD = re.compile(r'^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$')
_QUOTE = re.compile(r'^\s{0,3}>\s?(.*)$')
_BULLET = re.compile(r'^(\s*)[-*+]\s+(.*)$')
_PUNCT = set('\\`*_{}[]()#+-.!~>|$')
_ROW = re.compile(r'^\s*\|(.+)\|\s*$')
_RULE = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')
# $…$ as pandoc reads it: no space inside the dollars, no digit right after the closing one (so "$5 and $10" stays)
_MATH = re.compile(r'\$(?=\S)([^$\n]+?)(?<=\S)\$(?!\d)')
_SYM = {
    'times': '×', 'cdot': '·', 'div': '÷', 'pm': '±', 'mp': '∓', 'le': '≤', 'leq': '≤', 'ge': '≥', 'geq': '≥',
    'ne': '≠', 'neq': '≠', 'approx': '≈', 'sim': '∼', 'equiv': '≡', 'propto': '∝', 'infty': '∞', 'to': '→',
    'rightarrow': '→', 'leftarrow': '←', 'Rightarrow': '⇒', 'Leftrightarrow': '⇔', 'leftrightarrow': '↔',
    'in': '∈', 'notin': '∉', 'subset': '⊂', 'subseteq': '⊆', 'cup': '∪', 'cap': '∩', 'forall': '∀', 'exists': '∃',
    'partial': '∂', 'nabla': '∇', 'sum': '∑', 'prod': '∏', 'int': '∫', 'sqrt': '√', 'ldots': '…', 'cdots': '⋯',
    'dots': '…', 'degree': '°', 'circ': '∘', 'land': '∧', 'lor': '∨', 'neg': '¬', 'log': 'log', 'ln': 'ln',
    'exp': 'exp', 'sin': 'sin', 'cos': 'cos', 'tan': 'tan', 'max': 'max', 'min': 'min', 'lim': 'lim',
    'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'delta': 'δ', 'epsilon': 'ε', 'varepsilon': 'ε', 'zeta': 'ζ',
    'eta': 'η', 'theta': 'θ', 'lambda': 'λ', 'mu': 'μ', 'nu': 'ν', 'xi': 'ξ', 'pi': 'π', 'rho': 'ρ',
    'sigma': 'σ', 'tau': 'τ', 'phi': 'φ', 'varphi': 'φ', 'chi': 'χ', 'psi': 'ψ', 'omega': 'ω',
    'Gamma': 'Γ', 'Delta': 'Δ', 'Theta': 'Θ', 'Lambda': 'Λ', 'Pi': 'Π', 'Sigma': 'Σ', 'Phi': 'Φ', 'Omega': 'Ω',
    'quad': ' ', 'qquad': '  ', ',': ' ', ';': ' ', '!': '', 'left': '', 'right': '', '%': '%', '{': '{', '}': '}',
}
_SUP = dict(zip('0123456789+-=()nijkx', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱʲᵏˣ'))
_SUB = dict(zip('0123456789+-=()aeijknmortx', '₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢⱼₖₙₘₒᵣₜₓ'))


def _group(s, i):
    """The {…} group or single character at s[i]: (content, end)."""
    if i < len(s) and s[i] == '{':
        depth, j = 0, i
        while j < len(s):
            depth += {'{': 1, '}': -1}.get(s[j], 0)
            if depth == 0:
                return s[i + 1:j], j + 1
            j += 1
        return s[i + 1:], len(s)
    return s[i:i + 1], i + 1


def _script(body, table, mark):
    t = _latex(body)
    if all(c in table for c in t):
        return ''.join(table[c] for c in t)
    return mark + (t if len(t) == 1 else '(' + t + ')')


def latex(s):
    """LaTeX math → readable Unicode. Unknown commands keep their name."""
    return _latex(s).strip()


def _latex(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == '\\':
            m = re.match(r'\\([A-Za-z]+|.)', s[i:])
            name = m.group(1) if m else ''
            i += len(m.group(0)) if m else 1
            if name in ('frac', 'dfrac', 'tfrac'):
                a, i = _group(s, i); b, i = _group(s, i)
                a, b = _latex(a), _latex(b)
                wrap = lambda x: x if re.fullmatch(r'[\w.²³⁰¹⁴-⁹]+', x) else '(' + x + ')'
                out.append(wrap(a) + '/' + wrap(b))
            elif name == 'sqrt':
                a, i = _group(s, i); a = _latex(a)
                out.append('√' + (a if len(a) == 1 else '(' + a + ')'))
            elif name in ('text', 'mathrm', 'mathbf', 'mathit', 'operatorname', 'mathbb', 'boldsymbol'):
                a, i = _group(s, i); out.append(_latex(a))
            else:
                out.append(_SYM.get(name, name))
        elif c in '^_':
            a, i = _group(s, i + 1)
            out.append(_script(a, _SUP if c == '^' else _SUB, c))
        elif c in '{}':
            i += 1
        else:
            out.append(c); i += 1
    return re.sub(r' {2,}', ' ', ''.join(out))


def _cells(line):
    return [c.strip() for c in _ROW.match(line).group(1).split('|')]


def _table(rows):
    """Header + rows → one bullet line per row (Telegram has no tables)."""
    head, body = rows[0], rows[1:]
    lines = []
    for r in body:
        r = r + [''] * (len(head) - len(r))
        if len(head) == 2:
            lines.append(f'- **{r[0]}**：{r[1]}')
        else:
            rest = '；'.join(f'{h}：{v}' if h else v for h, v in zip(head[1:], r[1:]) if v)
            lines.append(f'- **{r[0]}**' + (f' — {rest}' if rest else ''))
    return lines or ['- ' + ' | '.join(head)]


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
        if c == '$':
            m = _MATH.match(s, i)
            if m:
                flush(); st = o.n; o.put(latex(m.group(1))); o.ent('code', st); i = m.end(); continue
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
    lines, src = [], (md or '').split('\n')
    k, fenced = 0, False
    while k < len(src):
        line = src[k]
        if line.strip().startswith('```'):
            fenced = not fenced
        if not fenced and _ROW.match(line) and k + 1 < len(src) and _RULE.match(src[k + 1]):
            rows = [_cells(line)]
            k += 2
            while k < len(src) and _ROW.match(src[k]):
                rows.append(_cells(src[k])); k += 1
            lines += _table(rows)
            continue
        if not fenced and line.strip().startswith('$$'):
            end = next((j for j in range(k, len(src)) if src[j].strip().endswith('$$') and (j > k or len(src[j].strip()) > 4)), None)
            if end is not None:
                body = '\n'.join(src[k:end + 1]).strip()[2:-2]
                lines += ['```', latex(body.replace('\n', ' ')), '```']
                k = end + 1
                continue
        lines.append(line); k += 1
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
