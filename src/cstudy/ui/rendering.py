"""把题目原文安全地显示出来。

题目里全是代码，**不能当 Markdown 渲染**——踩过这个坑：

- `#include` 会被当成 Markdown 的标题，渲染成一个巨大的 "include"
- Markdown 里**单个换行等于空格**，代码的每一行会被拼成一长条
  （本来整齐的 `for 循环` 挤成一坨）

所以题干、参考答案、解析一律走这个函数：先转义，再放进 `<pre>`——
保住换行和空格，还自带等宽字体（代码看着对得齐）。`pre-wrap` 让太长的行照样折行。
"""

from __future__ import annotations

from html import escape


def material_html(text: str) -> str:
    """题目原文 → HTML 片段。空内容返回空字符串。"""
    if not text or not text.strip():
        return ""
    return (
        "<pre style='white-space:pre-wrap; margin:0; font-size:10.5pt;'>"
        f"{escape(text.strip())}"
        "</pre>"
    )
