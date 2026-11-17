"""从已校验的报告生成下载文件，不运行模型文本或依赖用户路径。"""

import io
import re
from pathlib import Path
from typing import Any, Literal
from xml.sax.saxutils import escape

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

FileFormat = Literal["docx", "pdf", "md"]
MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "md": "text/markdown; charset=utf-8",
}


def pdf_font() -> str:
    """优先嵌入可用中文字体，容器提供文泉驿；无字体时采用标准 CJK 字体。"""
    name = "ResearchCJK"
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    for path in (
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    ):
        if path.is_file():
            pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=0))
            return name
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def filename(title: str, format: FileFormat | Literal["zip"]) -> str:
    """去除路径及控制字符，文件名只用于下载提示。"""
    return (
        (re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:100] or "研究报告")
        + "."
        + format
    )


def paragraphs(markdown: str) -> list[tuple[int, str]]:
    """将报告的标题、段落和脚注转换为文档段落，保留全部引用文本。"""
    result = []
    for line in markdown.splitlines():
        if not line.strip():
            continue
        heading = re.match(r"^(#{1,3})\s+", line)
        level = len(heading[1]) if heading else 0
        text = line[heading.end() :] if heading else line
        text = re.sub(r"\\([\\`*_{}\[\]()<>#+.!|~-])", r"\1", text)
        text = re.sub(r"\[\^(\d+)\]", r"[\1]", text).replace("**", "")
        result.append((level, text))
    return result


def export_report(markdown: str, format: FileFormat) -> bytes:
    """生成 UTF-8 Markdown、可编辑 Word 或中文 PDF。"""
    if format == "md":
        return markdown.encode("utf-8")
    output = io.BytesIO()
    content = paragraphs(markdown)
    if format == "docx":
        document = Document()
        section = document.sections[0]
        section.page_width, section.page_height = Inches(8.5), Inches(11)
        section.top_margin = section.bottom_margin = Inches(0.8)
        section.left_margin = section.right_margin = Inches(0.85)
        for name in ["Normal", "Title", "Heading 1", "Heading 2", "Heading 3"]:
            style = document.styles[name]
            style.font.name = "Arial"
            style.font.color.rgb = RGBColor(0, 0, 0)
            style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "宋体")
        document.styles["Normal"].font.size = Pt(11)
        document.styles["Normal"].paragraph_format.space_after = Pt(8)
        for level, text in content:
            if level:
                document.add_heading(text, level=0 if level == 1 else min(level - 1, 3))
            else:
                document.add_paragraph(text)
        footer = section.footer.paragraphs[0]
        footer.alignment = 1
        field = OxmlElement("w:fldSimple")
        field.set(qn("w:instr"), "PAGE")
        footer._p.append(field)
        document.save(output)
    else:
        font = pdf_font()
        styles = {
            level: ParagraphStyle(
                f"research-{level}",
                fontName=font,
                fontSize={0: 11, 1: 20, 2: 15, 3: 12}[level],
                leading={0: 17, 1: 28, 2: 23, 3: 19}[level],
                spaceAfter=10,
                wordWrap="CJK",
                keepWithNext=bool(level),
            )
            for level in range(4)
        }
        story = []
        for level, text in content:
            story.append(Paragraph(escape(text), styles[level]))
            story.append(Spacer(1, 3))

        def page_number(canvas: Any, document: Any) -> None:
            """在页脚绘制页码，正文分页由排版器处理。"""
            canvas.setFont(font, 9)
            canvas.drawCentredString(306, 30, str(document.page))

        SimpleDocTemplate(
            output,
            pagesize=(612, 792),
            leftMargin=60,
            rightMargin=60,
            topMargin=52,
            bottomMargin=52,
        ).build(story, onFirstPage=page_number, onLaterPages=page_number)
    return output.getvalue()
