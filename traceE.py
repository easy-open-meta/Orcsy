import ast, io, re
py = io.open('orcsy/dashboard_html.py', encoding='utf-8').read()
tree = ast.parse(py)
for node in tree.body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == 'PAGE':
        html = ast.literal_eval(node.value)
        break
# 找 ProjectsView render 里 E 的来源：搜 setup 返回或 import 解构中 E=...
# E.t —— E 是某个导入的压缩名。lang 模块 tNow 压缩名前面查到是 h（模块内）。
# 但跨模块 import 的名字不同。搜索 lang 导出别名段：
m = re.search(r'export\{([^}]{0,400})\}', html[html.index('ta-lang'):html.index('ta-lang')+20000])
print('export seg:', m.group(1)[:400] if m else 'not found')
