#!/usr/bin/env python3
"""tgmd: Markdown → Telegram text + entities.  Run from the repo root:  python tests/tgmd_test.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'content'))
from tgmd import render


def spans(md):
    t, ents = render(md)
    b = t.encode('utf-16-le')
    return t, [(e['type'], b[e['offset'] * 2:(e['offset'] + e['length']) * 2].decode('utf-16-le')) for e in ents]


def check(md, text, want):
    t, got = spans(md)
    assert t == text, (md, t)
    assert got == want, (md, got)


check('**粗** 和 `码`', '粗 和 码', [('bold', '粗'), ('code', '码')])
check('- **位置：** 本机 `a/b`', '• 位置： 本机 a/b', [('bold', '位置：'), ('code', 'a/b')])
check('看 [这里](https://x.y/z)。', '看 这里。', [('text_link', '这里')])
check('*斜* a*b*c snake_case 2*3*4', '斜 a*b*c snake_case 2*3*4', [('italic', '斜')])
check('# 标题', '标题', [('bold', '标题')])
check('> 一\n> 二\n正文', '一\n二\n正文', [('blockquote', '一\n二')])
check('```rust\nlet x = **y**;\n```', 'let x = **y**;', [('pre', 'let x = **y**;')])
check('😀 **粗**', '😀 粗', [('bold', '粗')])            # UTF-16 offsets after an emoji
check('未闭合 **粗 和 `码', '未闭合 **粗 和 `码', [])        # broken markup stays as written
check('---\n| a | b |', '---\n| a | b |', [])
check('\\*不是斜体\\*', '*不是斜体*', [])
check('| 文件 | 行数 | 内容 |\n|---|---|---|\n| a.md | 242 | 红线 |', '• a.md：行数 242；内容 红线', [('code', 'a.md'), ('bold', 'a.md')])
check('| 名 | 值 |\n|:--|--:|\n| a | 1 |', '• a：1', [('bold', 'a')])
check('价格 $5 和 $10', '价格 $5 和 $10', [])               # dollars are not math
check('公式 $E=mc^2$', '公式 E=mc²', [('code', 'E=mc²')])
check('$$\n\\frac{1}{1+e^{-x}} \\le \\sqrt{2}\n$$', '1/(1+e⁻ˣ) ≤ √2', [('pre', '1/(1+e⁻ˣ) ≤ √2')])
check('```\n| a | b |\n|---|---|\n```', '| a | b |\n|---|---|', [('pre', '| a | b |\n|---|---|')])   # untouched in code
check('看 README.md 和 knowledge/ops.md。', '看 README.md 和 knowledge/ops.md。', [('code', 'README.md'), ('code', 'knowledge/ops.md')])
check('见 https://x.com/a/README.md 与 foo.mdx', '见 https://x.com/a/README.md 与 foo.mdx', [])   # URLs and other names untouched
print('tgmd ok')
