"""浏览器页面注入脚本：DOM 遍历快照树构建与元素定位坐标计算。"""

from __future__ import annotations

# 快照构建脚本：注入页面执行，给交互元素分配 data-orcsy-ref 并返回结构化文本树
BUILD_SNAPSHOT_SCRIPT = r"""
function(options) {
    options = options || {};
    const rootRef = options.rootRef;
    const maxChars = options.maxChars || 6000;

    let rootNode = document.body;
    if (rootRef) {
        rootNode = document.querySelector(`[data-orcsy-ref="${rootRef}"]`) || document.body;
    }

    if (!rootNode) {
        return {
            title: document.title || '',
            url: window.location.href,
            text: '（页面空白或无内容）'
        };
    }

    // 元素计数器基准
    let counter = 1;

    function isVisible(el) {
        if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    function getDirectText(el) {
        let text = '';
        for (let child of el.childNodes) {
            if (child.nodeType === Node.TEXT_NODE) {
                text += child.textContent;
            }
        }
        return text.trim().replace(/\s+/g, ' ');
    }

    const lines = [];
    lines.push(`Title: ${document.title || '（无标题）'}`);
    lines.push(`URL: ${window.location.href}`);
    lines.push('');

    // 递归遍历可见元素
    function walk(node, depth) {
        if (lines.join('\n').length >= maxChars) return;
        if (!node || !isVisible(node)) return;

        const tag = node.tagName.toLowerCase();
        const role = (node.getAttribute('role') || '').toLowerCase();
        let ref = node.getAttribute('data-orcsy-ref');
        
        let isInteractive = false;
        let desc = '';

        if (tag === 'button' || role === 'button') {
            isInteractive = true;
            const btnText = (node.innerText || node.value || '').trim().replace(/\s+/g, ' ');
            desc = `button "${btnText || '按钮'}"`;
        } else if (tag === 'a' || role === 'link') {
            const linkText = (node.innerText || '').trim().replace(/\s+/g, ' ');
            const href = node.getAttribute('href') || '';
            if (linkText || href) {
                isInteractive = true;
                desc = `link "${linkText || '链接'}"${href ? ' -> ' + href : ''}`;
            }
        } else if (tag === 'input') {
            isInteractive = true;
            const type = (node.getAttribute('type') || 'text').toLowerCase();
            const placeholder = node.getAttribute('placeholder') || '';
            const val = type === 'password' ? '***' : (node.value || '');
            const name = node.getAttribute('name') || node.getAttribute('aria-label') || placeholder || '';
            
            if (type === 'checkbox' || type === 'radio') {
                desc = `${type} "${name}" (${node.checked ? 'checked' : 'unchecked'})`;
            } else if (type === 'submit' || type === 'button') {
                desc = `button "${node.value || name || '提交'}"`;
            } else {
                desc = `textbox "${name}" (type=${type}, value="${val}")`;
            }
        } else if (tag === 'textarea') {
            isInteractive = true;
            const placeholder = node.getAttribute('placeholder') || '';
            const val = node.value || '';
            const name = node.getAttribute('name') || node.getAttribute('aria-label') || placeholder || '';
            desc = `textarea "${name}" (value="${val.slice(0, 30)}")`;
        } else if (tag === 'select') {
            isInteractive = true;
            const selectedOpt = node.options[node.selectedIndex];
            const optText = selectedOpt ? selectedOpt.text : '';
            desc = `select "${node.name || ''}" (selected="${optText}")`;
        } else if (/^h[1-6]$/.test(tag)) {
            const hText = (node.innerText || '').trim().replace(/\s+/g, ' ');
            if (hText) {
                desc = `heading level ${tag[1]} "${hText}"`;
            }
        }

        if (desc) {
            if (!ref) {
                ref = 'e' + counter++;
                node.setAttribute('data-orcsy-ref', ref);
            }
            const indent = '  '.repeat(Math.min(depth, 4));
            lines.push(`${indent}[${ref}] ${desc}`);
        }

        for (let child of node.children) {
            walk(child, desc ? depth + 1 : depth);
        }
    }

    walk(rootNode, 0);

    let output = lines.join('\n');
    if (output.length > maxChars) {
        output = output.slice(0, maxChars) + '\n…（页面内容过长已截断）';
    }

    return {
        title: document.title || '',
        url: window.location.href,
        text: output
    };
}
"""

# 定位元素并获取视口中心坐标脚本
GET_ELEMENT_BOUNDS_SCRIPT = r"""
function(refOrSelector) {
    if (!refOrSelector) return null;
    let el = null;
    if (refOrSelector.startsWith('e') && /^e\d+$/.test(refOrSelector)) {
        el = document.querySelector(`[data-orcsy-ref="${refOrSelector}"]`);
    }
    if (!el) {
        try {
            el = document.querySelector(refOrSelector);
        } catch(e) {}
    }
    if (!el) return null;

    el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
    const rect = el.getBoundingClientRect();
    return {
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
        width: rect.width,
        height: rect.height,
        left: rect.left,
        top: rect.top,
        tagName: el.tagName.toLowerCase(),
        isVisible: rect.width > 0 && rect.height > 0
    };
}
"""

# 聚焦或清空元素输入框脚本
FOCUS_AND_CLEAR_SCRIPT = r"""
function(refOrSelector, clear) {
    let el = null;
    if (refOrSelector.startsWith('e') && /^e\d+$/.test(refOrSelector)) {
        el = document.querySelector(`[data-orcsy-ref="${refOrSelector}"]`);
    }
    if (!el) {
        try {
            el = document.querySelector(refOrSelector);
        } catch(e) {}
    }
    if (!el) return false;

    el.focus();
    if (clear) {
        if ('value' in el) {
            el.value = '';
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        } else if (el.isContentEditable) {
            el.innerText = '';
        }
    }
    return true;
}
"""
