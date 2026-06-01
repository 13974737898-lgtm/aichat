"""
面试报告 PDF 生成模块 - 将已生成的面试评估报告排版成接近前端弹窗的彩色 PDF。
与 backend/app.py 的面试报告下载接口配合使用。
"""

from datetime import datetime
from html import escape
from io import BytesIO

from reportlab.graphics.shapes import Drawing, Rect
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


FONT_NAME = "STSong-Light"
BG_PRIMARY = colors.HexColor("#0F0A1F")
BG_SECONDARY = colors.HexColor("#171033")
BG_CARD = colors.HexColor("#21164A")
BG_CARD_LIGHT = colors.HexColor("#2A1D5E")
PRIMARY_COLOR = colors.HexColor("#8B5CF6")
PRIMARY_DARK = colors.HexColor("#6D28D9")
PRIMARY_LIGHT = colors.HexColor("#DDD6FE")
ACCENT_COLOR = colors.HexColor("#EC4899")
SUCCESS_COLOR = colors.HexColor("#22C55E")
WARNING_COLOR = colors.HexColor("#C084FC")
DANGER_COLOR = colors.HexColor("#EF4444")
TEXT_PRIMARY = colors.HexColor("#F7F3FF")
TEXT_SECONDARY = colors.HexColor("#CEC2F5")
TEXT_MUTED = colors.HexColor("#9F93CC")
BORDER_COLOR = colors.HexColor("#4C3B86")
CONTENT_WIDTH = 174 * mm


def register_chinese_font():
    """注册中文字体，保证报告中的中文能正常显示。"""
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))


def safe_text(value, default="-"):
    """把任意值转换成适合写入 PDF 的安全文本。"""
    if value is None or value == "":
        return default
    return escape(str(value))


def format_report_time(value):
    """把报告中的时间转换成易读格式。"""
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def normalize_score(value, default=75):
    """把评分整理成 0 到 100 之间的整数。"""
    try:
        score = int(float(value))
        return max(0, min(100, score))
    except (TypeError, ValueError):
        return default


def score_color(score):
    """根据分数返回前端一致的反馈颜色。"""
    if score >= 80:
        return SUCCESS_COLOR
    if score >= 60:
        return WARNING_COLOR
    return DANGER_COLOR


def build_styles():
    """创建彩色报告使用的文字样式。"""
    base = {
        "fontName": FONT_NAME,
        "leading": 15,
        "spaceAfter": 4,
        "textColor": TEXT_SECONDARY,
    }

    def make_style(name, **kwargs):
        """基于通用样式创建具体段落样式。"""
        params = base.copy()
        params.update(kwargs)
        return ParagraphStyle(name, **params)

    return {
        "title": make_style("Title", fontSize=21, leading=27, alignment=TA_CENTER, textColor=TEXT_PRIMARY),
        "subtitle": make_style("Subtitle", fontSize=10.5, leading=15, alignment=TA_CENTER, textColor=TEXT_MUTED),
        "name": make_style("Name", fontSize=15, leading=20, textColor=TEXT_PRIMARY),
        "body": make_style("Body", fontSize=10.2, leading=15, textColor=TEXT_SECONDARY),
        "muted": make_style("Muted", fontSize=9.2, leading=13, textColor=TEXT_MUTED),
        "section": make_style("Section", fontSize=12.5, leading=17, textColor=TEXT_PRIMARY),
        "card_title": make_style("CardTitle", fontSize=11, leading=15, textColor=TEXT_PRIMARY),
        "score": make_style("Score", fontSize=24, leading=28, alignment=TA_CENTER, textColor=TEXT_PRIMARY),
        "score_label": make_style("ScoreLabel", fontSize=8.5, leading=11, alignment=TA_CENTER, textColor=TEXT_MUTED),
        "tag": make_style("Tag", fontSize=8.8, leading=12, textColor=PRIMARY_LIGHT),
        "success_tag": make_style("SuccessTag", fontSize=8.8, leading=12, textColor=SUCCESS_COLOR),
        "warning_tag": make_style("WarningTag", fontSize=8.8, leading=12, textColor=WARNING_COLOR),
    }


def draw_page_background(canvas, doc):
    """绘制每页背景和页脚。"""
    canvas.saveState()
    canvas.setFillColor(BG_PRIMARY)
    canvas.rect(0, 0, A4[0], A4[1], stroke=0, fill=1)
    canvas.setFillColor(TEXT_MUTED)
    canvas.setFont(FONT_NAME, 8)
    canvas.drawCentredString(A4[0] / 2, 9 * mm, f"面试评估报告 · 第 {doc.page} 页")
    canvas.restoreState()


def paragraph(text, style):
    """创建安全文本段落。"""
    return Paragraph(safe_text(text), style)


def score_badge(text, styles, color=PRIMARY_COLOR):
    """创建彩色胶囊标签。"""
    table = Table([[paragraph(text, styles["card_title"])]], colWidths=[34 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("BOX", (0, 0), (-1, -1), 0, color),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    return table


def make_header(report_data, styles):
    """创建顶部候选人和综合评分区域。"""
    score = normalize_score(report_data.get("overallScore"))
    recommendation = report_data.get("recommendation") or "待定"
    candidate_info = [
        paragraph(report_data.get("candidateName") or "候选人", styles["name"]),
        paragraph(f"应聘：{safe_text(report_data.get('position'))}", styles["body"]),
        paragraph(f"面试时间：{format_report_time(report_data.get('interviewDate'))}", styles["muted"]),
    ]
    score_panel = Table([
        [paragraph(str(score), styles["score"])],
        [paragraph("综合评分", styles["score_label"])],
        [score_badge(recommendation, styles, score_color(score))],
    ], colWidths=[42 * mm])
    score_panel.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    header = Table([[candidate_info, score_panel]], colWidths=[126 * mm, 42 * mm])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BG_SECONDARY),
        ("BOX", (0, 0), (-1, -1), 0.8, BORDER_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return header


def make_section(title, styles):
    """创建紫色章节标题条。"""
    table = Table([[paragraph(title, styles["section"])]], colWidths=[CONTENT_WIDTH])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BG_CARD),
        ("LINEBELOW", (0, 0), (-1, -1), 1.2, PRIMARY_COLOR),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return table


def make_text_panel(text, styles):
    """创建彩色正文内容块。"""
    table = Table([[paragraph(text, styles["body"])]], colWidths=[CONTENT_WIDTH])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BG_SECONDARY),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER_COLOR),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    return table


def make_tags(items, styles, tag_style="tag", bg_color=BG_CARD_LIGHT, text_color=None):
    """创建接近前端标签样式的彩色标签组。"""
    values = items if isinstance(items, list) and items else ["-"]
    rows = []
    row = []
    for item in values:
        row.append(paragraph(item, styles[tag_style]))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        row.extend([""] * (3 - len(row)))
        rows.append(row)

    table = Table(rows, colWidths=[54 * mm, 54 * mm, 54 * mm], hAlign="LEFT")
    cell_bg = bg_color
    table_style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for row_index, row_items in enumerate(rows):
        for col_index, value in enumerate(row_items):
            if value:
                table_style.append(("BACKGROUND", (col_index, row_index), (col_index, row_index), cell_bg))
                table_style.append(("BOX", (col_index, row_index), (col_index, row_index), 0.4, BORDER_COLOR))
            if text_color:
                table_style.append(("TEXTCOLOR", (col_index, row_index), (col_index, row_index), text_color))
    table.setStyle(TableStyle(table_style))
    return table


def make_score_bar(score, width=48 * mm):
    """创建和前端技能条类似的评分条。"""
    normalized = normalize_score(score)
    drawing = Drawing(width, 5 * mm)
    drawing.add(Rect(0, 1 * mm, width, 2.8 * mm, fillColor=BG_PRIMARY, strokeColor=None))
    drawing.add(Rect(0, 1 * mm, width * normalized / 100, 2.8 * mm, fillColor=PRIMARY_COLOR, strokeColor=None))
    return drawing


def make_skill_rows(soft, styles):
    """创建软技能评分条区域。"""
    items = [
        ("沟通表达", soft.get("communication") or {}),
        ("问题解决", soft.get("problemSolving") or {}),
        ("学习能力", soft.get("learning") or {}),
        ("团队协作", soft.get("teamwork") or {}),
    ]
    rows = []
    for name, data in items:
        score = normalize_score(data.get("score"))
        comment = data.get("comment") or "-"
        rows.append([
            paragraph(name, styles["body"]),
            make_score_bar(score),
            paragraph(f"{score} 分", styles["muted"]),
            paragraph(comment, styles["muted"]),
        ])
    table = Table(rows, colWidths=[22 * mm, 50 * mm, 16 * mm, 72 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BG_SECONDARY),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, BORDER_COLOR),
    ]))
    return table


def make_card(title, score, body_items, styles):
    """创建类似前端 report-card 的彩色能力卡片。"""
    header = Table([[
        paragraph(title, styles["card_title"]),
        score_badge(f"{normalize_score(score)} 分", styles, PRIMARY_COLOR),
    ]], colWidths=[52 * mm, 34 * mm])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BG_CARD_LIGHT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))

    body = [[header]]
    for item in body_items:
        body.append([paragraph(item, styles["body"])])
    table = Table(body, colWidths=[86 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 1), (-1, -1), BG_SECONDARY),
        ("BOX", (0, 0), (-1, -1), 0.7, BORDER_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 1), (-1, -1), 9),
        ("RIGHTPADDING", (0, 1), (-1, -1), 9),
        ("TOPPADDING", (0, 1), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 8),
    ]))
    return table


def generate_interview_report_pdf(report_data):
    """根据面试报告数据生成彩色 PDF 字节流。"""
    register_chinese_font()
    styles = build_styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="面试评估报告",
    )

    tech = report_data.get("technicalAssessment") or {}
    soft = report_data.get("softSkillsAssessment") or {}
    project = report_data.get("projectExperience") or {}
    culture = report_data.get("cultureFit") or {}

    story = [
        paragraph("面试评估报告", styles["title"]),
        paragraph("AI 面试表现分析 · 能力评估 · 改进建议", styles["subtitle"]),
        Spacer(1, 5 * mm),
        make_header(report_data, styles),
        Spacer(1, 6 * mm),
        make_section("总体评价", styles),
        make_text_panel(report_data.get("summary"), styles),
        Spacer(1, 5 * mm),
        make_section("评估详情", styles),
    ]

    story.append(Table([[
        make_card("技术能力", tech.get("score"), [
            f"等级：{tech.get('level') or '-'}",
            f"详细评价：{tech.get('details') or '-'}",
        ], styles),
        make_card("项目经验", project.get("score"), [
            f"项目深度：{project.get('depth') or '-'}",
        ], styles),
    ]], colWidths=[86 * mm, 86 * mm], hAlign="LEFT", spaceBefore=0, spaceAfter=0))
    story.append(Spacer(1, 4 * mm))
    story.append(Table([[
        make_card("文化匹配度", culture.get("score"), [
            f"求职动机：{culture.get('motivation') or '-'}",
            f"职业规划：{culture.get('careerPlan') or '-'}",
        ], styles),
        make_card("软技能总分", soft.get("score"), [
            "下方按沟通、解决问题、学习和协作维度展示。",
        ], styles),
    ]], colWidths=[86 * mm, 86 * mm], hAlign="LEFT"))
    story.append(Spacer(1, 5 * mm))

    story.extend([
        make_section("软技能评估", styles),
        make_skill_rows(soft, styles),
        Spacer(1, 5 * mm),
        make_section("技术亮点", styles),
        make_tags(tech.get("highlights"), styles, "tag", BG_CARD_LIGHT),
        Spacer(1, 5 * mm),
        make_section("项目亮点", styles),
        make_tags(project.get("highlights"), styles, "tag", BG_CARD_LIGHT),
        Spacer(1, 5 * mm),
        make_section("核心优势", styles),
        make_tags(report_data.get("strengths"), styles, "success_tag", colors.HexColor("#143923")),
        Spacer(1, 5 * mm),
        make_section("改进建议", styles),
        make_tags(report_data.get("areasForImprovement"), styles, "warning_tag", colors.HexColor("#35264D")),
        Spacer(1, 5 * mm),
        make_section("面试亮点", styles),
        make_tags(report_data.get("interviewHighlights"), styles, "tag", BG_CARD_LIGHT),
    ])

    doc.build(story, onFirstPage=draw_page_background, onLaterPages=draw_page_background)
    buffer.seek(0)
    return buffer
