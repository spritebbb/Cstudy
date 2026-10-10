"""把题目原文安全地显示出来，顺便管字体。

题目里全是代码，**不能当 Markdown 渲染**——踩过这个坑：

- `#include` 会被当成 Markdown 的标题，渲染成一个巨大的 "include"
- Markdown 里**单个换行等于空格**，代码的每一行会被拼成一长条

所以题干、参考答案、解析一律走 `material_html()`：先转义，再放进 `<pre>`——
保住换行和空格，还自带等宽字体（代码看着对得齐）。`pre-wrap` 让太长的行照样折行。

字体由 `configure()` 设置，启动时从 `config.yaml` 的 `ui:` 段读。
**不设的话 Qt 默认拿 Courier New 显示 `<pre>`**——那字体细、发虚，看久了很伤眼，
所以默认值也选好了（Consolas + 微软雅黑兜中文）。
"""

from __future__ import annotations

from html import escape

# 等宽字体：先 Consolas 显代码，中文落到雅黑上（这两个 Windows 上都自带）。
# 用户想在 config.yaml 里换成别的，configure() 会覆盖。
DEFAULT_MONO_FAMILY = "'Consolas', 'Microsoft YaHei', 'Noto Sans SC', monospace"
DEFAULT_MONO_SIZE = 12

_mono_family = DEFAULT_MONO_FAMILY
_mono_size = DEFAULT_MONO_SIZE


def configure(mono_family: str = "", mono_size: int | None = None) -> None:
    """设置题干/代码用的字体。启动时调一次就行。

    `mono_family` 是字体名（比如 Consolas）；这里自动补上中文字体和 monospace 兜底，
    免得用户填了个纯英文等宽字体，中文变成方框。
    """
    global _mono_family, _mono_size

    name = (mono_family or "").strip()
    if name:
        _mono_family = (
            f"'{name}', 'Microsoft YaHei', 'Noto Sans SC', monospace"
            if "'" not in name and "," not in name
            else f"{name}, 'Microsoft YaHei', monospace"
        )
    if mono_size:
        try:
            _mono_size = max(8, int(mono_size))
        except (TypeError, ValueError):
            pass


def material_html(text: str) -> str:
    """题目原文 → HTML 片段。空内容返回空字符串。

    **style 属性用双引号，字体名用单引号**——反过来会变成 `style='...'Consolas'...'`，
    单引号套单引号，属性当场被截断，整套样式全失效（踩过）。
    """
    if not text or not text.strip():
        return ""
    return (
        '<pre style="white-space:pre-wrap; margin:0; '
        f'font-family:{_mono_family}; font-size:{_mono_size}pt; line-height:150%;">'
        f"{escape(text.strip())}"
        "</pre>"
    )
