# 同名文件：按消息时间取对应那一份（brief 0.9.37）
import os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'content'))
import media
d = tempfile.mkdtemp()
acc = d
os.makedirs(f'{acc}/msg/file/2026-11')
t0 = 1795400000
for i, (n, t) in enumerate([('合同.docx', t0), ('合同(1).docx', t0 + 3600), ('合同(2).docx', t0 + 7200), ('合同(x).docx', t0 + 9000), ('别的.docx', t0)]):
    p = f'{acc}/msg/file/2026-11/{n}'
    open(p, 'w').write(n); os.utime(p, (t, t))
c = media._same_name(acc, '合同.docx')
assert sorted(os.path.basename(x) for x in c) == ['合同(1).docx', '合同(2).docx', '合同.docx'], c
assert os.path.basename(media.pick_file(c, t0 + 3590)[0]) == '合同(1).docx'
assert media.pick_file(c, t0 + 3590)[1] is True
assert os.path.basename(media.pick_file(c, t0 - 30)[0]) == '合同.docx'
p, sure = media.pick_file(c, t0 + 20000)
assert os.path.basename(p) == '合同(2).docx' and not sure, 'nothing after: newest, not sure'
p, sure = media.pick_file(c, None)
assert os.path.basename(p) == '合同(2).docx' and not sure
assert media.pick_file([c[0]], t0)[1] is True
how, path, mime, name = media.fetch(acc, 'x', 'file:合同.docx', None, False, t0 + 3590)
assert os.path.basename(path) == '合同(1).docx' and name == '合同.docx', (path, name)
how, path, mime, name = media.fetch(acc, 'x', 'file:合同.docx', None, False, None)
assert name.endswith(media.UNSURE)
print('ALL PASS')
