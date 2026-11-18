import fs from 'fs'
// 当前构建（3c9c180 后）2497 行 col 5413 的内容：
const py = fs.readFileSync('../orcsy/dashboard_html.py', 'utf8')
import { execSync } from 'child_process'
// 用 venv python 的 ast 提取
const out = execSync('../.venv/Scripts/python.exe - <<' + String.fromCharCode(39) + 'PYEOF' + String.fromCharCode(39) + `
import ast, io
py = io.open('orcsy/dashboard_html.py', encoding='utf-8').read()
tree = ast.parse(py)
for node in tree.body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == 'PAGE':
        html = ast.literal_eval(node.value)
        lines = html.split('\n')
        line = lines[2496] if len(lines) > 2496 else ''
        print(line[5340:5480])
        break
`, {encoding: 'utf8', cwd: '..'})
console.log('current col 5340-5480:', out.trim())
