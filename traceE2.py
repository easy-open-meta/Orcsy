import ast, io
py = io.open('orcsy/dashboard_html.py', encoding='utf-8').read()
tree = ast.parse(py)
html = None
for node in tree.body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == 'PAGE':
        html = ast.literal_eval(node.value)
        break
# 单文件打包无 export；E 在 ProjectsView 模块作用域。找 ProjectsView render 前
# 的 import 解构：搜 '简体中文' (LANG_LABEL) 的使用位置之后，第一个
# 'projpage' 之前的 2000 字符里 E= 出现的形态
i = html.index('projpage')
seg = html[max(0, i-6000):i]
import re
for m in re.finditer(r'[,;{(](\w{1,2})=([A-Za-z_$]{1,3})[.,;]', seg):
    pass
# 更直接：搜 E=St( 或 E=xx 形式 + 搜 ProjectsView 模板中 it( 压缩：
# it 在源里与 tFmt 同导入；压缩器给 it 一个名，给 tFmt 另一个名。
# 找 tFmt 特征调用（不存在的）：搜 '_0(' 不好。直接搜 ProjectsView 里
# 'E=' 的定义行
for m in re.finditer(r'\bE=', seg):
    print('E= at seg offset', m.start(), ':', seg[max(0,m.start()-60):m.start()+60].replace('\n',' ')[-110:])
    break
