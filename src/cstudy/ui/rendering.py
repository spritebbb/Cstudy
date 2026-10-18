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


# 长文阅读的排版：**只管字号、间距、行高，不管颜色**。
# 颜色一律留给主题（QSS 的 `color` 会继承进文档），写死一个深色在亮色主题里就瞎了。
HEADING_PT = {1: 15.0, 2: 12.5, 3: 11.0, 4: 10.5}
BODY_LINE_HEIGHT = 160          # 百分比
BODY_BOTTOM_MARGIN = 6          # 段落之间留一点气口


def _restyle(document) -> None:
    """把 Markdown 渲染出来的文档重新排一遍版。

    **为什么不用 `setDefaultStyleSheet` 了**：`QTextDocument.setMarkdown()`
    走的是 Qt 自己的 markdown 导入器，它**直接构造成 block/char format**，
    不生成 HTML —— 所以 `h1 { font-size: ... }` 那种 CSS 对它完全无效。
    第一版就是这么白忙的（标题还是那么大）。这里改成逐个区块改格式。

    改两件事：
    1. 行距 160%、段间距 6px —— 默认 100% 行距读长文很挤；
    2. 标题字号按级别压下来 —— Qt 默认 h1 是正文的 2 倍（正文 10.5pt 时就是 21pt），
       一个标题占半屏，视觉层级反而糊。
    """
    from PyQt6.QtGui import QFont, QTextBlockFormat, QTextCursor

    block = document.begin()
    while block.isValid():
        block_format = block.blockFormat()

        line = block_format.lineHeight()
        # 已经有自定义行距的块（比如代码块）不覆盖
        if not line or block_format.lineHeightType() == 0:
            block_format.setLineHeight(
                BODY_LINE_HEIGHT,
                QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
            )
            block_format.setBottomMargin(BODY_BOTTOM_MARGIN)
            cursor = QTextCursor(block)
            cursor.setBlockFormat(block_format)

        size = HEADING_PT.get(block_format.headingLevel())
        if size:
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    char_format = fragment.charFormat()
                    char_format.setFontPointSize(size)
                    char_format.setFontWeight(QFont.Weight.DemiBold.value)
                    span = QTextCursor(block)
                    span.setPosition(fragment.position())
                    span.setPosition(
                        fragment.position() + fragment.length(),
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                    span.mergeCharFormat(char_format)
                iterator += 1

        block = block.next()


def render_markdown(view, text: str) -> None:
    """Markdown 正文 → 排好版的文档。显示笔记、题干都走这一个入口。"""
    view.setMarkdown(text or "")
    document = view.document()
    if document is not None:
        _restyle(document)


def render_html(view, html: str) -> None:
    """现成的 HTML → 排好版的文档（判分卡片、运行输出走这个）。

    和 `render_markdown` 一样要重排一遍，否则判分卡片里的行距跟笔记页对不上，
    同一块屏幕上两种密度，眼睛会累。
    """
    view.setHtml(html or "")
    document = view.document()
    if document is not None:
        _restyle(document)


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


def style_document(view, margin: int = 20) -> None:
    """给长文视图留够边距。

    **文档内部的排版（字号、行距、标题层级）不在这儿**——那些得在
    Markdown 渲染完之后由 `_restyle()` 逐个区块改（原因见 `_restyle` 的注释）。
    这里只设"文字离边框多远"，设一次就够。
    """
    document = view.document()
    if document is not None:
        document.setDocumentMargin(margin)
