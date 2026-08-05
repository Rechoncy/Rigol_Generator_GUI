from __future__ import annotations

import html
import re
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.utils import ImageReader


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "USER_MANUAL_CN.md"
SCREENSHOTS = ROOT / "tmp" / "pdfs" / "screenshots"
OUTPUT = ROOT / "output" / "pdf" / "DG4000_NI_Control_User_Manual_CN.pdf"

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_X = 17 * mm
MARGIN_TOP = 18 * mm
MARGIN_BOTTOM = 17 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN_X

NAVY = colors.HexColor("#102235")
TEAL = colors.HexColor("#00AFA2")
CYAN = colors.HexColor("#DFF8F5")
TEXT = colors.HexColor("#223044")
MUTED = colors.HexColor("#617086")
LIGHT = colors.HexColor("#F2F5F8")
LINE = colors.HexColor("#CAD4DF")
WARNING = colors.HexColor("#FFF3D6")


def register_fonts() -> None:
    candidates = [
        ("CN", Path(r"C:\Windows\Fonts\Deng.ttf")),
        ("CN", Path(r"C:\Windows\Fonts\simhei.ttf")),
    ]
    bold_candidates = [
        ("CNBold", Path(r"C:\Windows\Fonts\Dengb.ttf")),
        ("CNBold", Path(r"C:\Windows\Fonts\simhei.ttf")),
    ]
    for name, path in candidates:
        if path.exists():
            pdfmetrics.registerFont(TTFont(name, str(path)))
            break
    else:
        raise RuntimeError("未找到可用于 PDF 的中文字体")
    for name, path in bold_candidates:
        if path.exists():
            pdfmetrics.registerFont(TTFont(name, str(path)))
            break
    else:
        pdfmetrics.registerFont(TTFont("CNBold", str(candidates[0][1])))
    console = Path(r"C:\Windows\Fonts\consola.ttf")
    if console.exists():
        pdfmetrics.registerFont(TTFont("Code", str(console)))


def styles():
    base = getSampleStyleSheet()
    return {
        "body": ParagraphStyle(
            "BodyCN",
            parent=base["BodyText"],
            fontName="CN",
            fontSize=9.3,
            leading=15,
            textColor=TEXT,
            spaceAfter=4,
            wordWrap="CJK",
        ),
        "h1": ParagraphStyle(
            "H1CN",
            parent=base["Heading1"],
            fontName="CNBold",
            fontSize=21,
            leading=27,
            textColor=NAVY,
            spaceBefore=6,
            spaceAfter=10,
            keepWithNext=True,
            wordWrap="CJK",
        ),
        "h2": ParagraphStyle(
            "H2CN",
            parent=base["Heading2"],
            fontName="CNBold",
            fontSize=15,
            leading=21,
            textColor=TEAL,
            spaceBefore=12,
            spaceAfter=7,
            keepWithNext=True,
            wordWrap="CJK",
        ),
        "h3": ParagraphStyle(
            "H3CN",
            parent=base["Heading3"],
            fontName="CNBold",
            fontSize=11.5,
            leading=17,
            textColor=NAVY,
            spaceBefore=8,
            spaceAfter=5,
            keepWithNext=True,
            wordWrap="CJK",
        ),
        "caption": ParagraphStyle(
            "CaptionCN",
            parent=base["BodyText"],
            fontName="CN",
            fontSize=8,
            leading=12,
            alignment=TA_CENTER,
            textColor=MUTED,
            spaceBefore=4,
            spaceAfter=8,
            wordWrap="CJK",
        ),
        "table": ParagraphStyle(
            "TableCN",
            parent=base["BodyText"],
            fontName="CN",
            fontSize=8,
            leading=11,
            textColor=TEXT,
            wordWrap="CJK",
        ),
        "table_head": ParagraphStyle(
            "TableHeadCN",
            parent=base["BodyText"],
            fontName="CNBold",
            fontSize=8,
            leading=11,
            textColor=colors.white,
            wordWrap="CJK",
        ),
        "code": ParagraphStyle(
            "CodeCN",
            parent=base["Code"],
            fontName="Code" if "Code" in pdfmetrics.getRegisteredFontNames() else "Courier",
            fontSize=7.7,
            leading=11,
            textColor=NAVY,
            backColor=LIGHT,
            borderColor=LINE,
            borderWidth=0.6,
            borderPadding=(7, 8, 7, 8),
            borderRadius=4,
            spaceBefore=4,
            spaceAfter=8,
        ),
    }


def inline_markup(text: str) -> str:
    parts = re.split(r"(`[^`]+`)", text)
    output = []
    for part in parts:
        if part.startswith("`") and part.endswith("`"):
            code = html.escape(part[1:-1])
            output.append(f'<font name="Code" color="#007E75">{code}</font>')
        else:
            output.append(html.escape(part))
    return "".join(output).replace("  ", " ")


def cover_story(style_map):
    title = ParagraphStyle(
        "CoverTitle",
        fontName="CNBold",
        fontSize=29,
        leading=39,
        textColor=NAVY,
        alignment=TA_LEFT,
        wordWrap="CJK",
    )
    subtitle = ParagraphStyle(
        "CoverSubtitle",
        fontName="CN",
        fontSize=13,
        leading=21,
        textColor=MUTED,
    )
    small = ParagraphStyle(
        "CoverSmall",
        fontName="CN",
        fontSize=9.5,
        leading=16,
        textColor=TEXT,
    )
    features = [
        [Paragraph("多设备", style_map["table_head"]), Paragraph("首页统一管理、共享连接、独立设置窗口", style_map["table_head"])],
        [Paragraph("触发步进", style_map["table"]), Paragraph("频率、幅度、相位、PSK 相位、FSK 频率", style_map["table"])],
        [Paragraph("虚拟实验室", style_map["table"]), Paragraph("无硬件体验单次、批量、连续触发和积压追平", style_map["table"])],
        [Paragraph("实机接口", style_map["table"]), Paragraph("PyVISA + NI-DAQmx，NI 只进行硬件累计边沿计数", style_map["table"])],
    ]
    feature_table = Table(features, colWidths=[37 * mm, 123 * mm])
    feature_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("BACKGROUND", (0, 1), (-1, -1), LIGHT),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return [
        Spacer(1, 23 * mm),
        Paragraph("DG4000 + NI", title),
        Paragraph("多设备联动控制软件使用说明", title),
        Spacer(1, 7 * mm),
        Table([[""]], colWidths=[54 * mm], rowHeights=[2.2 * mm], style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), TEAL)])),
        Spacer(1, 8 * mm),
        Paragraph("从虚拟实验室到实体硬件联调的完整操作、时序与故障排查指南", subtitle),
        Spacer(1, 12 * mm),
        feature_table,
        Spacer(1, 13 * mm),
        Table(
            [[Paragraph("版本 0.2.1", small), Paragraph("2026-08-05", small)]],
            colWidths=[80 * mm, 80 * mm],
            style=TableStyle([("ALIGN", (1, 0), (1, 0), "RIGHT")]),
        ),
        Spacer(1, 10 * mm),
        Table(
            [[Paragraph("安全提示：实体设备输出前，请核对幅度、偏置、负载、PFI 电平和实验电路耐压。虚拟实验室不产生物理输出。", small)]],
            colWidths=[160 * mm],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), WARNING),
                    ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#E5B84E")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ]
            ),
        ),
        PageBreak(),
    ]


def parse_markdown(style_map):
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    first_break = next(i for i, line in enumerate(lines) if line.strip() == "<!-- PAGEBREAK -->")
    lines = lines[first_break + 1 :]
    story = []
    paragraph_lines: list[str] = []
    bullet_lines: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        if paragraph_lines:
            story.append(Paragraph(inline_markup(" ".join(paragraph_lines)), style_map["body"]))
            paragraph_lines.clear()

    def flush_bullets() -> None:
        if bullet_lines:
            items = [
                ListItem(Paragraph(inline_markup(item), style_map["body"]), leftIndent=8)
                for item in bullet_lines
            ]
            story.append(
                ListFlowable(
                    items,
                    bulletType="bullet",
                    start="circle",
                    leftIndent=16,
                    bulletFontName="CN",
                    bulletFontSize=7,
                    spaceAfter=5,
                )
            )
            bullet_lines.clear()

    index = 0
    while index < len(lines):
        raw = lines[index]
        line = raw.strip()
        if line.startswith("```"):
            flush_paragraph()
            flush_bullets()
            if in_code:
                story.append(Preformatted("\n".join(code_lines), style_map["code"]))
                code_lines.clear()
                in_code = False
            else:
                in_code = True
            index += 1
            continue
        if in_code:
            code_lines.append(raw)
            index += 1
            continue
        if line == "<!-- PAGEBREAK -->":
            flush_paragraph()
            flush_bullets()
            story.append(PageBreak())
            index += 1
            continue
        screenshot = re.fullmatch(r"<!-- SCREENSHOT: ([^|]+) \| (.+) -->", line)
        if screenshot:
            flush_paragraph()
            flush_bullets()
            filename, caption = screenshot.group(1).strip(), screenshot.group(2).strip()
            path = SCREENSHOTS / filename
            if not path.exists():
                raise FileNotFoundError(f"缺少说明书截图：{path}")
            width, height = ImageReader(str(path)).getSize()
            scale = min(CONTENT_WIDTH / width, 108 * mm / height)
            image = Image(str(path), width=width * scale, height=height * scale)
            image.hAlign = "CENTER"
            story.append(
                KeepTogether(
                    [
                        image,
                        Paragraph(inline_markup(caption), style_map["caption"]),
                    ]
                )
            )
            index += 1
            continue
        if line.startswith("|") and line.endswith("|"):
            flush_paragraph()
            flush_bullets()
            table_lines = []
            while index < len(lines):
                candidate = lines[index].strip()
                if not (candidate.startswith("|") and candidate.endswith("|")):
                    break
                table_lines.append(candidate)
                index += 1
            rows = []
            for row_index, table_line in enumerate(table_lines):
                cells = [cell.strip() for cell in table_line.strip("|").split("|")]
                if row_index == 1 and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                    continue
                style = style_map["table_head"] if not rows else style_map["table"]
                rows.append([Paragraph(inline_markup(cell), style) for cell in cells])
            if rows:
                column_count = len(rows[0])
                widths = [CONTENT_WIDTH / column_count] * column_count
                table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
                table.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
                            ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 5),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                            ("TOPPADDING", (0, 0), (-1, -1), 5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                        ]
                    )
                )
                story.extend([table, Spacer(1, 7)])
            continue
        if not line:
            flush_paragraph()
            flush_bullets()
            index += 1
            continue
        if line.startswith("### "):
            flush_paragraph()
            flush_bullets()
            story.append(Paragraph(inline_markup(line[4:]), style_map["h3"]))
        elif line.startswith("## "):
            flush_paragraph()
            flush_bullets()
            story.append(Paragraph(inline_markup(line[3:]), style_map["h2"]))
        elif line.startswith("# "):
            flush_paragraph()
            flush_bullets()
            story.append(Paragraph(inline_markup(line[2:]), style_map["h1"]))
        elif re.match(r"^\d+\. ", line):
            flush_paragraph()
            flush_bullets()
            match = re.match(r"^(\d+)\. (.+)", line)
            number_style = ParagraphStyle(
                f"Number{index}",
                parent=style_map["body"],
                fontName="CNBold",
                textColor=TEAL,
                alignment=TA_CENTER,
            )
            numbered = Table(
                [[
                    Paragraph(match.group(1), number_style),
                    Paragraph(inline_markup(match.group(2)), style_map["body"]),
                ]],
                colWidths=[8 * mm, CONTENT_WIDTH - 8 * mm],
                hAlign="LEFT",
            )
            numbered.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    ]
                )
            )
            story.append(numbered)
        elif line.startswith("- "):
            flush_paragraph()
            bullet_lines.append(line[2:])
        else:
            flush_bullets()
            paragraph_lines.append(line)
        index += 1
    flush_paragraph()
    flush_bullets()
    return story


def draw_header_footer(canvas, document) -> None:
    canvas.saveState()
    page = canvas.getPageNumber()
    if page > 1:
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.4)
        canvas.line(MARGIN_X, PAGE_HEIGHT - 12 * mm, PAGE_WIDTH - MARGIN_X, PAGE_HEIGHT - 12 * mm)
        canvas.setFont("CN", 7.7)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN_X, PAGE_HEIGHT - 9.3 * mm, "DG4000 + NI 多设备联动控制软件使用说明")
    canvas.setFont("CN", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN_X, 8 * mm, "版本 0.2.1")
    canvas.drawRightString(PAGE_WIDTH - MARGIN_X, 8 * mm, f"第 {page} 页")
    canvas.restoreState()


def build() -> Path:
    register_fonts()
    style_map = styles()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    story = cover_story(style_map) + parse_markdown(style_map)
    document = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        rightMargin=MARGIN_X,
        leftMargin=MARGIN_X,
        topMargin=MARGIN_TOP,
        bottomMargin=MARGIN_BOTTOM,
        title="DG4000 + NI 多设备联动控制软件使用说明",
        author="Rigol Generator GUI Project",
        subject="设备管理、虚拟实验室、触发步进和实机联调",
    )
    document.build(story, onFirstPage=draw_header_footer, onLaterPages=draw_header_footer)
    reader = PdfReader(str(OUTPUT))
    if len(reader.pages) < 10:
        raise RuntimeError(f"说明书页数异常：{len(reader.pages)}")
    print(f"已生成 {len(reader.pages)} 页 PDF：{OUTPUT}")
    return OUTPUT


if __name__ == "__main__":
    build()
