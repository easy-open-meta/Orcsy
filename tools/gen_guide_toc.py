#!/usr/bin/env python3
"""用户指南网页版目录再生成：从 docs/用户指南.md 与 docs/user-guide.md 解析
h2/h3 标题，重建 website/guide.html 与 guide-en.html 侧栏的**嵌套二级目录**。

历史痛点：pandoc 生成的侧栏只有一级目录（h2），10.x 等二级小节要滚到正文里
一个个找——本脚本把 h3 挂到所属 h2 下，层级缩进 + 折叠展开。

改动 Markdown 后重新生成网页版时：先用 pandoc 重建整页（保持既有流程），
再跑本脚本刷新目录；或单独跑本脚本只刷新目录。
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# GitHub 风格 slug：小写、空白→连字符、去标点（与既有锚点生成规则一致）
def gh_slug(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", s)  # 保留 CJK/字母数字/连字符/空格
    s = s.replace(" ", "-")
    return s


def parse_toc(md_path: Path) -> list[dict]:
    """解析 markdown，返回 [{level, text, slug, children: [...]}]（h2 顶层，h3 挂子级）。"""
    items: list[dict] = []
    current_h2 = None
    in_code = False
    for line in md_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^(#{2,3})\s+(.+?)\s*#*$", line)
        if not m:
            continue
        level, text = len(m.group(1)), m.group(2).strip()
        node = {"level": level, "text": text, "slug": gh_slug(text), "children": []}
        if level == 2:
            items.append(node)
            current_h2 = node
        elif level == 3 and current_h2:
            current_h2["children"].append(node)
    return items


def render_toc(items: list[dict]) -> str:
    """渲染嵌套目录 HTML：<ul><li>h2 <ul><li>h3...</ul></li></ul>，h3 缩进。"""
    out = ['<nav id="TOC" aria-label="目录"><ul>']
    for h2 in items:
        out.append(f'<li class="toc-h2"><a href="#{html.escape(h2["slug"], quote=True)}">'
                   f'{html.escape(h2["text"])}</a>')
        if h2["children"]:
            out.append('<ul class="toc-sub">')
            for h3 in h2["children"]:
                out.append(f'<li class="toc-h3"><a href="#{html.escape(h3["slug"], quote=True)}">'
                           f'{html.escape(h3["text"])}</a></li>')
            out.append('</ul>')
        out.append('</li>')
    out.append('</ul></nav>')
    return "".join(out)


# 侧栏样式：二级缩进 + 折叠交互（由页面 JS 配合 .toc-sub 显隐）
TOC_CSS = '''
.toc-h2 > a { font-weight: 600; }
.toc-sub { margin: 2px 0 6px 10px; padding-left: 10px; border-left: 2px solid var(--line); }
.toc-sub a { font-size: 12px; color: var(--dim); display: block; padding: 2px 0; }
.toc-sub a:hover { color: var(--acc); }
.toc-h2 { margin-top: 4px; }
'''

FOLD_JS = '''
// 目录二级折叠：点 h2 链接展开其子目录（再次点击收起）
document.querySelectorAll('#TOC .toc-h2').forEach(function (li) {
  var a = li.querySelector(':scope > a');
  var sub = li.querySelector(':scope > ul.toc-sub');
  if (!sub) return;
  a.addEventListener('click', function () { li.classList.toggle('open'); });
});
// 当前阅读位置自动展开所在章节
var cur = location.hash;
if (cur) {
  var link = document.querySelector('#TOC a[href="' + cur + '"]');
  if (link) {
    var li = link.closest('.toc-h3') ? link.closest('.toc-h2') : null;
    if (li) li.classList.add('open');
  }
}
'''

def apply(page: Path, md: Path, toc_title_check: str) -> None:
    t = page.read_text(encoding="utf-8")
    items = parse_toc(md)
    new_nav = render_toc(items)
    # 替换既有 <nav id="TOC" ...>...</nav>
    m = re.search(r'<nav id="TOC"[^>]*>.*?</nav>', t, re.DOTALL)
    assert m, f'{page.name}: TOC nav not found'
    t = t[:m.start()] + new_nav + t[m.end():]
    # 样式注入（只注入一次）
    if ".toc-sub {" not in t:
        t = t.replace("</style>", TOC_CSS + "</style>", 1)
    # 折叠 JS 注入（body 尾部；只注入一次）
    if "toc-h2').forEach" not in t and "toc-h2\').forEach" not in t:
        t = t.rstrip()
        assert t.endswith("</html>")
        t = t[:-len("</html>")] + "<script>" + FOLD_JS + "</script>\n</html>"
    page.write_text(t, encoding="utf-8")
    n2 = len(items)
    n3 = sum(len(h["children"]) for h in items)
    print(f"✓ {page.name}: 一级 {n2} / 二级 {n3} 目录项")

def main() -> int:
    apply(ROOT / "website" / "guide.html", ROOT / "docs" / "用户指南.md", "本页目录")
    apply(ROOT / "website" / "guide-en.html", ROOT / "docs" / "user-guide.md", "TOC")
    return 0

if __name__ == "__main__":
    sys.exit(main())
