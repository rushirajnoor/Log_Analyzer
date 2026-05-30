#!/usr/bin/env python3
"""
Log_Analyzer Hackathon Presentation Generator
Generates a professional, dark-themed 16:9 PowerPoint presentation.
"""

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn, nsmap
import copy

# ── Colour Palette ──────────────────────────────────────────────────────────
BG       = RGBColor(0x1F, 0x1F, 0x23)
RED      = RGBColor(0xE6, 0x39, 0x46)
ORANGE   = RGBColor(0xF7, 0x7F, 0x00)
BLUE     = RGBColor(0x45, 0x7B, 0x9D)
GREEN    = RGBColor(0x06, 0xA7, 0x7D)
PURPLE   = RGBColor(0x72, 0x09, 0xB7)
WHITE    = RGBColor(0xFF, 0xFF, 0xFF)
GRAY     = RGBColor(0xA0, 0xA0, 0xA0)
DARK_CARD = RGBColor(0x2A, 0x2A, 0x2F)
DARKER   = RGBColor(0x17, 0x17, 0x1B)
ACCENT_GRAY = RGBColor(0x3A, 0x3A, 0x40)
LIGHT_GRAY  = RGBColor(0xD0, 0xD0, 0xD0)
SUBTLE_LINE = RGBColor(0x44, 0x44, 0x4A)

# ── Helpers ─────────────────────────────────────────────────────────────────
def set_slide_bg(slide, color):
    """Set solid background colour for a slide."""
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color


def add_textbox(slide, left, top, width, height, text, font_size=16,
                color=WHITE, bold=False, alignment=PP_ALIGN.LEFT,
                font_name="Calibri", anchor=MSO_ANCHOR.TOP, line_spacing=1.15):
    """Add a text box with styled text."""
    txBox = slide.shapes.add_textbox(Inches(left), Inches(top),
                                     Inches(width), Inches(height))
    tf = txBox.text_frame
    tf.word_wrap = True
    from pptx.enum.text import MSO_AUTO_SIZE
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.paragraphs[0].text = text
    tf.paragraphs[0].font.size = Pt(font_size)
    tf.paragraphs[0].font.color.rgb = color
    tf.paragraphs[0].font.bold = bold
    tf.paragraphs[0].font.name = font_name
    tf.paragraphs[0].alignment = alignment
    tf.paragraphs[0].space_after = Pt(0)
    tf.paragraphs[0].space_before = Pt(0)
    # line spacing
    pPr = tf.paragraphs[0]._pPr
    if pPr is None:
        pPr = tf.paragraphs[0]._p.get_or_add_pPr()
    lnSpc = pPr.find(qn('a:lnSpc'))
    if lnSpc is not None:
        pPr.remove(lnSpc)
    return txBox


def add_multiline_textbox(slide, left, top, width, height, lines,
                          font_name="Calibri", anchor=MSO_ANCHOR.TOP):
    """Add text box with multiple styled paragraphs.
    lines: list of (text, font_size, color, bold, alignment)
    """
    txBox = slide.shapes.add_textbox(Inches(left), Inches(top),
                                     Inches(width), Inches(height))
    tf = txBox.text_frame
    tf.word_wrap = True
    from pptx.enum.text import MSO_AUTO_SIZE
    tf.auto_size = MSO_AUTO_SIZE.NONE

    for i, (text, font_size, color, bold, alignment) in enumerate(lines):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.text = text
        p.font.size = Pt(font_size)
        p.font.color.rgb = color
        p.font.bold = bold
        p.font.name = font_name
        p.alignment = alignment
        p.space_after = Pt(4)
        p.space_before = Pt(2)
    return txBox


def add_rounded_rect(slide, left, top, width, height, fill_color,
                     border_color=None, border_width=Pt(1)):
    """Add a rounded rectangle shape."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE,
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    if border_color:
        shape.line.color.rgb = border_color
        shape.line.width = border_width
    else:
        shape.line.fill.background()
    return shape


def add_rect(slide, left, top, width, height, fill_color,
             border_color=None, border_width=Pt(1)):
    """Add a rectangle shape."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    if border_color:
        shape.line.color.rgb = border_color
        shape.line.width = border_width
    else:
        shape.line.fill.background()
    return shape


def add_arrow_shape(slide, left, top, width, height, fill_color):
    """Add a right arrow shape."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RIGHT_ARROW,
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    shape.line.fill.background()
    return shape


def add_chevron(slide, left, top, width, height, fill_color):
    """Add a chevron shape."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.CHEVRON,
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    shape.line.fill.background()
    return shape


def add_oval(slide, left, top, width, height, fill_color, border_color=None):
    """Add an oval shape."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.OVAL,
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    if border_color:
        shape.line.color.rgb = border_color
        shape.line.width = Pt(2)
    else:
        shape.line.fill.background()
    return shape


def add_line(slide, start_x, start_y, end_x, end_y, color, width=Pt(2)):
    """Add a line connector."""
    connector = slide.shapes.add_connector(
        1,  # straight
        Inches(start_x), Inches(start_y),
        Inches(end_x), Inches(end_y)
    )
    connector.line.color.rgb = color
    connector.line.width = width
    return connector


def set_shape_text(shape, text, font_size=14, color=WHITE, bold=False,
                   alignment=PP_ALIGN.CENTER, font_name="Calibri"):
    """Set text within a shape."""
    tf = shape.text_frame
    tf.word_wrap = True
    from pptx.enum.text import MSO_AUTO_SIZE
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.paragraphs[0].text = text
    tf.paragraphs[0].font.size = Pt(font_size)
    tf.paragraphs[0].font.color.rgb = color
    tf.paragraphs[0].font.bold = bold
    tf.paragraphs[0].font.name = font_name
    tf.paragraphs[0].alignment = alignment


def set_shape_multiline(shape, lines, font_name="Calibri"):
    """Set multi-paragraph text in a shape.
    lines: list of (text, font_size, color, bold, alignment)
    """
    tf = shape.text_frame
    tf.word_wrap = True
    from pptx.enum.text import MSO_AUTO_SIZE
    tf.auto_size = MSO_AUTO_SIZE.NONE
    for i, (text, font_size, color, bold, alignment) in enumerate(lines):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.text = text
        p.font.size = Pt(font_size)
        p.font.color.rgb = color
        p.font.bold = bold
        p.font.name = font_name
        p.alignment = alignment
        p.space_after = Pt(2)
        p.space_before = Pt(1)


def add_speaker_notes(slide, notes_text):
    """Add speaker notes to a slide."""
    notes_slide = slide.notes_slide
    notes_slide.notes_text_frame.text = notes_text


def draw_grid_pattern(slide, cols=20, rows=12, line_color=None):
    """Draw a subtle grid pattern on the slide background."""
    if line_color is None:
        line_color = RGBColor(0x28, 0x28, 0x2E)
    w = 13.333
    h = 7.5
    step_x = w / cols
    step_y = h / rows
    for i in range(1, cols):
        add_line(slide, step_x * i, 0, step_x * i, h, line_color, Pt(0.5))
    for j in range(1, rows):
        add_line(slide, 0, step_y * j, w, step_y * j, line_color, Pt(0.5))


def add_slide_number(slide, number, total=15):
    """Add slide number in bottom right."""
    add_textbox(slide, 11.8, 7.0, 1.3, 0.4,
                f"{number} / {total}", font_size=10, color=GRAY,
                alignment=PP_ALIGN.RIGHT)


def add_top_accent_line(slide, color=RED, y=0.0):
    """Add a thin accent line at the top of the slide."""
    rect = add_rect(slide, 0, y, 13.333, 0.06, color)
    return rect


def add_section_title(slide, title, subtitle=None, y_start=0.3):
    """Add a standardised section title with accent bar."""
    add_rect(slide, 0.7, y_start, 0.06, 0.5, RED)
    add_textbox(slide, 1.0, y_start - 0.05, 11, 0.6, title,
                font_size=32, color=WHITE, bold=True)
    if subtitle:
        add_textbox(slide, 1.0, y_start + 0.55, 11, 0.4, subtitle,
                    font_size=16, color=GRAY)


# ── Slide Builders ──────────────────────────────────────────────────────────

def build_slide_01(prs):
    """Title + Hook"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    set_slide_bg(slide, BG)

    # Subtle grid
    draw_grid_pattern(slide, 24, 14, RGBColor(0x26, 0x26, 0x2C))

    # Top accent bar
    add_rect(slide, 0, 0, 13.333, 0.08, RED)

    # Bottom accent bar
    add_rect(slide, 0, 7.42, 13.333, 0.08, BLUE)

    # Center decorative box
    add_rounded_rect(slide, 1.5, 1.8, 10.333, 4.2, RGBColor(0x24, 0x24, 0x2A),
                     border_color=ACCENT_GRAY, border_width=Pt(1.5))

    # Small label
    label = add_rounded_rect(slide, 5.2, 1.5, 2.9, 0.45, RED)
    set_shape_text(label, "HACKATHON 2026", font_size=12, color=WHITE, bold=True)

    # Title
    add_textbox(slide, 2.0, 2.4, 9.333, 1.2,
                "Toward Reasoning-Driven\nAutonomous Infrastructure",
                font_size=42, color=WHITE, bold=True,
                alignment=PP_ALIGN.CENTER, font_name="Calibri")

    # Divider line
    add_line(slide, 4.5, 3.85, 8.833, 3.85, ACCENT_GRAY, Pt(1))

    # Subtitle
    add_textbox(slide, 2.0, 4.0, 9.333, 0.8,
                "When Kubernetes Orchestrates, But Operations Still Fails",
                font_size=20, color=GRAY, alignment=PP_ALIGN.CENTER)

    # Project name badge
    badge = add_rounded_rect(slide, 5.0, 5.0, 3.333, 0.55, DARKER,
                             border_color=BLUE, border_width=Pt(1.5))
    set_shape_text(badge, "Log_Analyzer", font_size=16, color=BLUE, bold=True)

    # Bottom tagline
    add_textbox(slide, 2.0, 6.4, 9.333, 0.5,
                "Autonomous AIOps  ·  Root Cause Analysis  ·  Self-Healing  ·  Adaptive Learning",
                font_size=12, color=GRAY, alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 1)

    add_speaker_notes(slide,
        "Kubernetes has solved infrastructure orchestration. It hasn't solved operational "
        "intelligence. When a service fails deep in your dependency chain—hidden behind three "
        "layers of microservices—Kubernetes doesn't diagnose the root cause. It doesn't "
        "understand your topology. It doesn't learn from outcomes. Log_Analyzer is the missing "
        "layer between orchestration and autonomous operations.")


def build_slide_02(prs):
    """The Infrastructure Crisis"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, RED)
    add_section_title(slide, "The Hidden Cost of Cloud-Native Complexity")

    # ─ Panel 1: Cascading Failure ─
    p1_left = 0.7
    card1 = add_rounded_rect(slide, p1_left, 1.4, 3.8, 5.2, DARK_CARD,
                             border_color=RED, border_width=Pt(1.5))
    # Header
    h1 = add_rounded_rect(slide, p1_left, 1.4, 3.8, 0.55, RED)
    set_shape_text(h1, "⚡ CASCADING FAILURE", font_size=14, color=WHITE, bold=True)

    # Chain boxes
    boxes = [
        ("Frontend", "T+8s", 2.3),
        ("CartService", "T+5s", 3.3),
        ("Redis-Cart", "T0 (Root)", 4.3),
    ]
    for name, timing, y in boxes:
        bx = add_rounded_rect(slide, 1.1, y, 2.6, 0.6, DARKER,
                               border_color=ACCENT_GRAY)
        set_shape_multiline(bx, [
            (name, 13, WHITE, True, PP_ALIGN.CENTER),
            (timing, 11, RED, False, PP_ALIGN.CENTER),
        ])
    # Arrows between boxes
    for y in [2.95, 3.95]:
        arr = add_arrow_shape(slide, 2.15, y, 0.5, 0.25, RED)

    add_textbox(slide, 1.0, 5.2, 3.2, 0.8,
                "One root cause triggers\nthree service failures",
                font_size=12, color=GRAY, alignment=PP_ALIGN.CENTER)

    # ─ Panel 2: Alert Fatigue ─
    p2_left = 4.8
    card2 = add_rounded_rect(slide, p2_left, 1.4, 3.7, 5.2, DARK_CARD,
                             border_color=ORANGE, border_width=Pt(1.5))
    h2 = add_rounded_rect(slide, p2_left, 1.4, 3.7, 0.55, ORANGE)
    set_shape_text(h2, "🔔 ALERT FATIGUE", font_size=14, color=WHITE, bold=True)

    # Big number
    add_textbox(slide, p2_left + 0.2, 2.3, 3.3, 1.0,
                "92%", font_size=64, color=ORANGE, bold=True,
                alignment=PP_ALIGN.CENTER)
    add_textbox(slide, p2_left + 0.2, 3.4, 3.3, 0.5,
                "of alerts are symptoms,\nnot root causes",
                font_size=16, color=LIGHT_GRAY, alignment=PP_ALIGN.CENTER)

    # Alert icon bars
    for i in range(6):
        c = ORANGE if i < 5 else GREEN
        w = 2.8 - i * 0.3
        add_rounded_rect(slide, p2_left + 0.45, 4.3 + i * 0.32, w, 0.22, c,
                         border_color=None)

    # ─ Panel 3: Reactive Ops ─
    p3_left = 8.8
    card3 = add_rounded_rect(slide, p3_left, 1.4, 3.8, 5.2, DARK_CARD,
                             border_color=BLUE, border_width=Pt(1.5))
    h3 = add_rounded_rect(slide, p3_left, 1.4, 3.8, 0.55, BLUE)
    set_shape_text(h3, "⏱ REACTIVE OPS", font_size=14, color=WHITE, bold=True)

    add_textbox(slide, p3_left + 0.2, 2.3, 3.4, 0.6,
                "Average MTTR", font_size=14, color=GRAY,
                alignment=PP_ALIGN.CENTER)
    add_textbox(slide, p3_left + 0.2, 2.8, 3.4, 1.2,
                "45 min", font_size=56, color=RED, bold=True,
                alignment=PP_ALIGN.CENTER)
    add_textbox(slide, p3_left + 0.2, 3.9, 3.4, 0.5,
                "manual incident response",
                font_size=14, color=GRAY, alignment=PP_ALIGN.CENTER)

    # Timeline bars
    add_textbox(slide, p3_left + 0.3, 4.6, 3.2, 0.3, "Typical Incident Timeline",
                font_size=11, color=GRAY, alignment=PP_ALIGN.LEFT, bold=True)
    labels = ["Detect", "Triage", "Diagnose", "Fix", "Verify"]
    colors_t = [RED, ORANGE, ORANGE, BLUE, GREEN]
    widths = [0.4, 0.6, 1.0, 0.7, 0.5]
    x = p3_left + 0.3
    for lbl, clr, w in zip(labels, colors_t, widths):
        bar = add_rounded_rect(slide, x, 4.95, w, 0.35, clr)
        set_shape_text(bar, lbl, font_size=8, color=WHITE, bold=True)
        x += w + 0.05

    add_textbox(slide, p3_left + 0.3, 5.45, 3.2, 0.3,
                "Most time: diagnosing root cause",
                font_size=10, color=GRAY, alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 2)
    add_speaker_notes(slide,
        "Modern systems don't fail through isolated events. They fail through dependency "
        "chains. One root cause. Three services affected. Dozens of alerts firing. Without "
        "intelligence, you're flying blind. 45 minutes to recover.")


def build_slide_03(prs):
    """Why Traditional Monitoring Fails"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, BLUE)
    add_section_title(slide, "The Gap Between Orchestration and Intelligence")

    # ─ Left column: What K8s Does Well ─
    left_card = add_rounded_rect(slide, 0.7, 1.4, 5.3, 4.4, DARK_CARD,
                                 border_color=GREEN, border_width=Pt(1.5))
    lh = add_rounded_rect(slide, 0.7, 1.4, 5.3, 0.55, GREEN)
    set_shape_text(lh, "✓  WHAT KUBERNETES DOES WELL", font_size=14,
                   color=WHITE, bold=True)

    k8s_good = [
        "Container Scheduling & Placement",
        "Resource Allocation & Limits",
        "Liveness & Readiness Checks",
        "Rolling Updates & Rollbacks",
        "Load Balancing & Service Discovery",
        "Automatic Pod Restarts",
    ]
    for i, item in enumerate(k8s_good):
        y = 2.2 + i * 0.55
        check = add_rounded_rect(slide, 1.1, y, 0.35, 0.35, GREEN)
        set_shape_text(check, "✓", font_size=14, color=WHITE, bold=True)
        add_textbox(slide, 1.55, y, 4.2, 0.4, item,
                    font_size=14, color=WHITE)

    # ─ Right column: What It Doesn't Do ─
    right_card = add_rounded_rect(slide, 7.3, 1.4, 5.3, 4.4, DARK_CARD,
                                  border_color=RED, border_width=Pt(1.5))
    rh = add_rounded_rect(slide, 7.3, 1.4, 5.3, 0.55, RED)
    set_shape_text(rh, "✗  WHAT IT DOESN'T DO", font_size=14,
                   color=WHITE, bold=True)

    k8s_bad = [
        "Correlate Multi-Service Failures",
        "Understand Dependency Topology",
        "Diagnose Root Causes",
        "Make Context-Aware Decisions",
        "Learn from Past Outcomes",
        "Adapt Remediation Strategy",
    ]
    for i, item in enumerate(k8s_bad):
        y = 2.2 + i * 0.55
        cross = add_rounded_rect(slide, 7.7, y, 0.35, 0.35, RED)
        set_shape_text(cross, "✗", font_size=14, color=WHITE, bold=True)
        add_textbox(slide, 8.15, y, 4.2, 0.4, item,
                    font_size=14, color=WHITE)

    # Center VS divider
    vs_circle = add_oval(slide, 6.25, 3.2, 0.8, 0.8, DARKER, border_color=ACCENT_GRAY)
    set_shape_text(vs_circle, "VS", font_size=16, color=WHITE, bold=True)

    # Bottom quote box
    quote_box = add_rounded_rect(slide, 1.5, 6.1, 10.333, 0.85, DARKER,
                                 border_color=BLUE, border_width=Pt(1.5))
    set_shape_text(quote_box,
        '"Kubernetes automates infrastructure. Operational intelligence requires reasoning."',
        font_size=16, color=BLUE, bold=False, alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 3)
    add_speaker_notes(slide,
        "Kubernetes is brilliant at what it does. But it's fundamentally reactive. "
        "There's a fundamental gap between orchestration and autonomous operations. "
        "That gap is where most incidents live.")


def build_slide_04(prs):
    """Introducing the Intelligence Layer — Architecture Diagram"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, PURPLE)
    add_section_title(slide, "An Operational Reasoning Engine for Kubernetes")

    # Top layer: Decision & Learning
    top = add_rounded_rect(slide, 2.5, 1.5, 8.333, 0.7, PURPLE,
                           border_color=None)
    set_shape_text(top, "DECISION & LEARNING LAYER", font_size=15,
                   color=WHITE, bold=True)

    # Arrow down
    add_arrow_shape(slide, 6.4, 2.25, 0.5, 0.3, PURPLE)
    # rotate not easy, so use a simple line
    add_line(slide, 6.666, 2.25, 6.666, 2.55, PURPLE, Pt(3))

    # Middle row: 5 engine blocks
    engines = [
        ("RCA\nEngine", RED, 1.5),
        ("Metrics\nMonitor", ORANGE, 3.9),
        ("Dependency\nGraph", BLUE, 6.3),
        ("Incident\nTracker", GREEN, 8.7),
        ("LLM\n(Ollama)", PURPLE, 11.1),
    ]
    for label, color, x in engines:
        bx = add_rounded_rect(slide, x, 2.7, 2.0, 1.1, DARK_CARD,
                               border_color=color, border_width=Pt(2))
        set_shape_multiline(bx, [
            (label.split('\n')[0], 13, color, True, PP_ALIGN.CENTER),
            (label.split('\n')[1] if '\n' in label else '', 11, GRAY, False, PP_ALIGN.CENTER),
        ])

    # Connecting line across middle
    add_line(slide, 2.5, 3.25, 12.1, 3.25, ACCENT_GRAY, Pt(1))

    # Arrows down to remediation
    add_line(slide, 6.666, 3.85, 6.666, 4.3, ACCENT_GRAY, Pt(2))

    # Auto-Remediation Layer
    rem = add_rounded_rect(slide, 3.0, 4.4, 7.333, 0.7, GREEN,
                           border_color=None)
    set_shape_text(rem, "AUTO-REMEDIATION LAYER", font_size=15,
                   color=WHITE, bold=True)

    # Arrow down to K8s
    add_line(slide, 6.666, 5.15, 6.666, 5.55, GREEN, Pt(3))

    # K8s Cluster
    k8s = add_rounded_rect(slide, 2.0, 5.6, 9.333, 1.0, DARKER,
                           border_color=BLUE, border_width=Pt(2))
    set_shape_multiline(k8s, [
        ("KUBERNETES CLUSTER", 14, BLUE, True, PP_ALIGN.CENTER),
        ("12 Microservices  ·  Online Boutique", 12, GRAY, False, PP_ALIGN.CENTER),
    ])

    # Side labels
    add_textbox(slide, 0.3, 2.8, 1.0, 0.5, "REASON", font_size=11,
                color=GRAY, bold=True, alignment=PP_ALIGN.CENTER)
    add_textbox(slide, 0.3, 4.5, 1.0, 0.5, "ACT", font_size=11,
                color=GRAY, bold=True, alignment=PP_ALIGN.CENTER)
    add_textbox(slide, 0.3, 5.8, 1.0, 0.5, "INFRA", font_size=11,
                color=GRAY, bold=True, alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 4)
    add_speaker_notes(slide,
        "This is not a dashboard. This is a reasoning engine. Four pillars: RCA Engine, "
        "Metrics Monitor, Dependency Graph, Incident Tracker. All feeding into a Decision "
        "Engine. Which triggers Auto-Remediation.")


def build_slide_05(prs):
    """How It Reasons — Dependency-Aware RCA"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, RED)
    add_section_title(slide, "From Logs to Root Cause")

    # Four stages as a horizontal pipeline
    stages = [
        ("1", "RAW LOGS", RED, [
            "Error: connection refused",
            "HTTP 503 Service Unavail.",
            "SIGTERM received",
        ]),
        ("2", "ANALYSIS", ORANGE, [
            "Signal classification",
            "Pattern matching",
            "Severity scoring",
        ]),
        ("3", "DEPENDENCY\nTRAVERSAL", BLUE, [
            "Frontend →",
            "  CartService →",
            "    Redis-Cart ✦",
        ]),
        ("4", "ROOT CAUSE", GREEN, [
            "redis-cart",
            "Confidence: 6/6",
            "Rating: HIGH ●",
        ]),
    ]

    x_start = 0.7
    stage_w = 2.7
    gap = 0.55

    for idx, (num, title, color, items) in enumerate(stages):
        x = x_start + idx * (stage_w + gap)
        # Stage card
        card = add_rounded_rect(slide, x, 1.6, stage_w, 4.5, DARK_CARD,
                                border_color=color, border_width=Pt(2))

        # Stage number circle
        circ = add_oval(slide, x + stage_w / 2 - 0.25, 1.35, 0.5, 0.5, color)
        set_shape_text(circ, num, font_size=16, color=WHITE, bold=True)

        # Stage title
        add_textbox(slide, x + 0.15, 2.0, stage_w - 0.3, 0.7,
                    title, font_size=16, color=color, bold=True,
                    alignment=PP_ALIGN.CENTER)

        # Divider
        add_line(slide, x + 0.3, 2.75, x + stage_w - 0.3, 2.75,
                 ACCENT_GRAY, Pt(1))

        # Items
        for i, item in enumerate(items):
            add_textbox(slide, x + 0.25, 2.9 + i * 0.55, stage_w - 0.5, 0.5,
                        item, font_size=13, color=LIGHT_GRAY,
                        font_name="Consolas" if idx in (0, 2) else "Calibri")

        # Arrow between stages
        if idx < 3:
            ax = x + stage_w + 0.05
            arr = add_arrow_shape(slide, ax, 3.5, 0.45, 0.3, color)

    # Bottom label
    label_box = add_rounded_rect(slide, 3.0, 6.5, 7.333, 0.6, DARKER,
                                 border_color=ACCENT_GRAY)
    set_shape_text(label_box,
        "Hybrid Reasoning: Rule-Based Pattern Matching + LLM Analysis",
        font_size=14, color=GRAY, alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 5)
    add_speaker_notes(slide,
        "The system does signal extraction, then dependency resolution. It traces backward "
        "through the dependency chain. This is hybrid reasoning—rule-based pattern matching "
        "combined with LLM analysis.")


def build_slide_06(prs):
    """Confidence Scoring & Decision Quality"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, ORANGE)
    add_section_title(slide, "Certainty Gates: Act Only When Confident")

    # ─ Top: Scoring Rubric Table ─
    add_textbox(slide, 0.8, 1.3, 5, 0.4, "CONFIDENCE SCORING RUBRIC",
                font_size=13, color=GRAY, bold=True)

    rubric = [
        ("Service Down", "+3", RED),
        ("Redis Issue Detected", "+4", RED),
        ("Request Failures", "+3", ORANGE),
        ("Resource Anomaly", "+2", ORANGE),
        ("Transient Error", "+1", BLUE),
    ]

    # Table header
    hdr = add_rounded_rect(slide, 0.8, 1.7, 5.5, 0.45, ACCENT_GRAY)
    set_shape_multiline(hdr, [
        ("Signal                                              Points", 12, WHITE, True, PP_ALIGN.LEFT),
    ])

    for i, (signal, pts, color) in enumerate(rubric):
        y = 2.2 + i * 0.5
        row_bg = DARK_CARD if i % 2 == 0 else DARKER
        row = add_rounded_rect(slide, 0.8, y, 5.5, 0.45, row_bg)
        # Signal name
        add_textbox(slide, 1.0, y + 0.02, 3.5, 0.4, signal,
                    font_size=13, color=LIGHT_GRAY)
        # Points badge
        badge = add_rounded_rect(slide, 5.2, y + 0.05, 0.8, 0.35, color)
        set_shape_text(badge, pts, font_size=13, color=WHITE, bold=True)

    # ─ Right: Decision Gates ─
    add_textbox(slide, 7.0, 1.3, 5, 0.4, "DECISION GATES",
                font_size=13, color=GRAY, bold=True)

    gates = [
        ("HIGH", "≥ 6 points", "Act Immediately", GREEN, "Auto-remediate"),
        ("MEDIUM", "≥ 3 points", "Verify First", ORANGE, "Confirm & remediate"),
        ("LOW", "< 3 points", "Human Review", RED, "Escalate to operator"),
    ]

    for i, (level, threshold, action, color, desc) in enumerate(gates):
        y = 1.8 + i * 1.6
        gate_card = add_rounded_rect(slide, 7.0, y, 5.5, 1.3, DARK_CARD,
                                     border_color=color, border_width=Pt(2))
        # Level badge
        lvl_badge = add_rounded_rect(slide, 7.3, y + 0.15, 1.2, 0.45, color)
        set_shape_text(lvl_badge, level, font_size=14, color=WHITE, bold=True)
        # Threshold
        add_textbox(slide, 8.7, y + 0.15, 1.8, 0.4, threshold,
                    font_size=13, color=LIGHT_GRAY, bold=True)
        # Action
        add_textbox(slide, 10.6, y + 0.15, 1.7, 0.4, action,
                    font_size=13, color=color, bold=True)
        # Description
        add_textbox(slide, 7.3, y + 0.7, 4.8, 0.4, desc,
                    font_size=12, color=GRAY)

    # Bottom callout
    callout = add_rounded_rect(slide, 2.5, 6.4, 8.333, 0.65, DARKER,
                               border_color=GREEN, border_width=Pt(2))
    set_shape_text(callout,
        "Result: 4% false positive rate  ·  Prevents action storms  ·  Safe autonomy",
        font_size=15, color=GREEN, bold=True, alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 6)
    add_speaker_notes(slide,
        "We don't just flag problems. We assign certainty. HIGH confidence: act immediately. "
        "MEDIUM: wait for verification. LOW: human review. This prevents action storms. "
        "That's why our false-positive rate is only 4%.")


def build_slide_07(prs):
    """Adaptive Learning"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, GREEN)
    add_section_title(slide, "Systems That Improve Themselves")

    add_textbox(slide, 0.8, 1.1, 11, 0.4, "ADAPTIVE FEEDBACK LOOP",
                font_size=13, color=GRAY, bold=True)

    # Circular loop: 6 steps arranged in a rounded path
    steps = [
        ("Incident\nOccurs", RED, 2.0, 1.7),
        ("Evaluate\nOptions", ORANGE, 5.5, 1.7),
        ("ML\nSelector", PURPLE, 9.0, 1.7),
        ("Execute\nAction", BLUE, 9.0, 4.2),
        ("Log\nOutcome", GREEN, 5.5, 4.2),
        ("Update\nSuccess Rates", ORANGE, 2.0, 4.2),
    ]

    for label, color, x, y in steps:
        card = add_rounded_rect(slide, x, y, 2.2, 1.2, DARK_CARD,
                                border_color=color, border_width=Pt(2))
        lines_text = label.split('\n')
        set_shape_multiline(card, [
            (lines_text[0], 14, color, True, PP_ALIGN.CENTER),
            (lines_text[1] if len(lines_text) > 1 else '', 12, LIGHT_GRAY, False, PP_ALIGN.CENTER),
        ])

    # Forward arrows (top row: left→right)
    for sx, ex, y in [(4.25, 5.45, 2.15), (7.75, 8.95, 2.15)]:
        add_arrow_shape(slide, sx, y, 0.5, 0.3, ACCENT_GRAY)

    # Down arrow right
    add_line(slide, 10.1, 2.95, 10.1, 4.15, ACCENT_GRAY, Pt(2))
    # Reverse arrows (bottom row: right→left)
    # We'll use text arrows since MSO doesn't have left arrows easily
    for sx, ex, y in [(7.75, 5.45, 4.65), (4.25, 2.0, 4.65)]:
        # just draw a line
        add_line(slide, ex + 2.2, y, sx, y, ACCENT_GRAY, Pt(2))
    # Up arrow left
    add_line(slide, 3.1, 3.0, 3.1, 4.15, ACCENT_GRAY, Pt(2))

    # Center: real metrics card
    center = add_rounded_rect(slide, 3.8, 3.15, 5.6, 0.8, DARKER,
                              border_color=GREEN, border_width=Pt(1.5))
    set_shape_multiline(center, [
        ("Live Success Rates", 13, GREEN, True, PP_ALIGN.CENTER),
        ("Restart: 60% (5 incidents)  ·  Scale-up: 100% (43 incidents)", 12, LIGHT_GRAY, False, PP_ALIGN.CENTER),
    ])

    # Bottom insight
    add_textbox(slide, 1.5, 5.8, 10.333, 0.8,
                "Different services respond differently to different remediation strategies.\n"
                "The system learns these patterns and selects the highest-probability action.",
                font_size=14, color=GRAY, alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 7)
    add_speaker_notes(slide,
        "After each incident, the system records outcomes. For redis-cart, restart works "
        "60% of the time. Scale-up works 100%. Next time, the system picks the best action. "
        "Different services respond differently. The system learns these patterns.")


def build_slide_08(prs):
    """Full Autonomous Remediation Flow"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, GREEN)
    add_section_title(slide, "From Detection to Recovery: Fully Autonomous")

    # Vertical timeline on the left
    timeline_steps = [
        ("T0:00", "DETECT", "Anomaly detected in logs", RED),
        ("T0:02", "ANALYZE", "Root cause analysis (RCA)", ORANGE),
        ("T0:05", "DECIDE", "ML strategy selection", PURPLE),
        ("T0:06", "REMEDIATE", "Execute auto-fix", BLUE),
        ("T0:10", "VERIFY", "Confirm service health", GREEN),
        ("T0:15", "LEARN", "Log outcome & update model", GREEN),
    ]

    for i, (time, stage, desc, color) in enumerate(timeline_steps):
        y = 1.5 + i * 0.9
        # Time label
        add_textbox(slide, 0.8, y, 1.0, 0.4, time,
                    font_size=14, color=color, bold=True,
                    alignment=PP_ALIGN.RIGHT, font_name="Consolas")
        # Dot on timeline
        dot = add_oval(slide, 2.05, y + 0.05, 0.3, 0.3, color)
        # Vertical line
        if i < 5:
            add_line(slide, 2.2, y + 0.38, 2.2, y + 0.87, ACCENT_GRAY, Pt(2))
        # Stage card
        card = add_rounded_rect(slide, 2.6, y - 0.05, 4.8, 0.55, DARK_CARD,
                                border_color=color, border_width=Pt(1.5))
        set_shape_multiline(card, [
            (f"{stage}  —  {desc}", 13, WHITE, False, PP_ALIGN.LEFT),
        ])

    # Right side: MTTR callout
    mttr_card = add_rounded_rect(slide, 8.0, 1.5, 4.8, 3.5, DARK_CARD,
                                 border_color=GREEN, border_width=Pt(2))
    add_textbox(slide, 8.2, 1.7, 4.4, 0.4, "MTTR COMPARISON",
                font_size=13, color=GRAY, bold=True, alignment=PP_ALIGN.CENTER)

    # Log_Analyzer bar
    add_textbox(slide, 8.4, 2.3, 4.0, 0.3, "Log_Analyzer",
                font_size=12, color=GREEN, bold=True)
    bar1 = add_rounded_rect(slide, 8.4, 2.65, 1.0, 0.45, GREEN)
    set_shape_text(bar1, "104.2s", font_size=14, color=WHITE, bold=True)

    # Traditional bar
    add_textbox(slide, 8.4, 3.3, 4.0, 0.3, "Traditional",
                font_size=12, color=RED, bold=True)
    bar2 = add_rounded_rect(slide, 8.4, 3.65, 4.0, 0.45, RED)
    set_shape_text(bar2, "45 min", font_size=14, color=WHITE, bold=True)

    # Reduction badge
    reduce = add_rounded_rect(slide, 8.8, 4.3, 3.5, 0.55, DARKER,
                              border_color=GREEN, border_width=Pt(2))
    set_shape_text(reduce, "96% Reduction", font_size=18, color=GREEN, bold=True)

    # Right bottom quote
    quote = add_rounded_rect(slide, 8.0, 5.5, 4.8, 1.2, DARKER,
                             border_color=ACCENT_GRAY)
    set_shape_multiline(quote, [
        ("Fully autonomous.", 14, WHITE, True, PP_ALIGN.CENTER),
        ("No human in the loop.", 13, GRAY, False, PP_ALIGN.CENTER),
        ("Detection to recovery in seconds.", 13, GREEN, False, PP_ALIGN.CENTER),
    ])

    add_slide_number(slide, 8)
    add_speaker_notes(slide,
        "This is the full incident lifecycle. Completely autonomous. No human involved. "
        "104 seconds vs 45 minutes. That's not a percentage improvement. That's a "
        "fundamental shift.")


def build_slide_09(prs):
    """Multi-Service Orchestration"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, BLUE)
    add_section_title(slide, "When Multiple Services Fail: Dependency Reasoning")

    # ─ Scenario A: Cascading ─
    a_card = add_rounded_rect(slide, 0.7, 1.5, 5.8, 5.0, DARK_CARD,
                              border_color=GREEN, border_width=Pt(2))
    a_hdr = add_rounded_rect(slide, 0.7, 1.5, 5.8, 0.55, GREEN)
    set_shape_text(a_hdr, "SCENARIO A: CASCADING FAILURE", font_size=14,
                   color=WHITE, bold=True)

    add_textbox(slide, 1.0, 2.2, 5.2, 0.4, "Correlated Failures",
                font_size=15, color=GREEN, bold=True)
    add_textbox(slide, 1.0, 2.6, 5.2, 0.8,
                "System detects that Frontend, CartService, and Redis\nfailures share a common root cause.",
                font_size=12, color=GRAY)

    # Chain visualization
    services_a = [("Frontend", RED), ("CartService", ORANGE), ("Redis-Cart", RED)]
    for i, (svc, clr) in enumerate(services_a):
        y = 3.5 + i * 0.7
        bx = add_rounded_rect(slide, 1.5, y, 2.5, 0.5, DARKER,
                               border_color=clr, border_width=Pt(1.5))
        set_shape_text(bx, svc, font_size=12, color=clr, bold=True)
        if i < 2:
            add_line(slide, 2.75, y + 0.55, 2.75, y + 0.65, clr, Pt(2))

    # Result box
    result_a = add_rounded_rect(slide, 4.3, 3.7, 1.9, 1.5, DARKER,
                                border_color=GREEN, border_width=Pt(2))
    set_shape_multiline(result_a, [
        ("FIX 1", 14, GREEN, True, PP_ALIGN.CENTER),
        ("root cause", 11, GRAY, False, PP_ALIGN.CENTER),
        ("Recover 3", 14, GREEN, True, PP_ALIGN.CENTER),
        ("services", 11, GRAY, False, PP_ALIGN.CENTER),
    ])

    add_arrow_shape(slide, 4.0, 4.2, 0.3, 0.25, GREEN)

    # ─ Scenario B: Independent ─
    b_card = add_rounded_rect(slide, 6.8, 1.5, 5.8, 5.0, DARK_CARD,
                              border_color=BLUE, border_width=Pt(2))
    b_hdr = add_rounded_rect(slide, 6.8, 1.5, 5.8, 0.55, BLUE)
    set_shape_text(b_hdr, "SCENARIO B: INDEPENDENT FAILURES", font_size=14,
                   color=WHITE, bold=True)

    add_textbox(slide, 7.1, 2.2, 5.2, 0.4, "Parallel Remediation",
                font_size=15, color=BLUE, bold=True)
    add_textbox(slide, 7.1, 2.6, 5.2, 0.8,
                "Independent failures get different strategies\napplied in parallel for fastest recovery.",
                font_size=12, color=GRAY)

    services_b = [
        ("Service A", "Scale-up", GREEN),
        ("Service B", "Restart", ORANGE),
        ("Service C", "Rollback", BLUE),
    ]
    for i, (svc, strategy, clr) in enumerate(services_b):
        y = 3.5 + i * 0.7
        bx = add_rounded_rect(slide, 7.3, y, 2.0, 0.5, DARKER,
                               border_color=clr, border_width=Pt(1.5))
        set_shape_text(bx, svc, font_size=12, color=clr, bold=True)
        add_arrow_shape(slide, 9.4, y + 0.1, 0.35, 0.25, clr)
        strat = add_rounded_rect(slide, 9.9, y, 2.3, 0.5, clr)
        set_shape_text(strat, strategy, font_size=12, color=WHITE, bold=True)

    # Bottom insight
    insight = add_rounded_rect(slide, 2.5, 6.7, 8.333, 0.55, DARKER,
                               border_color=ACCENT_GRAY)
    set_shape_text(insight,
        "Dependency graph determines correlation → chooses optimal strategy per incident",
        font_size=13, color=GRAY, alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 9)
    add_speaker_notes(slide,
        "The system gets really powerful handling cascades. Fix redis. CartService "
        "automatically recovers. Frontend automatically recovers. One action. Three "
        "services recovered.")


def build_slide_10(prs):
    """Results & Operational Impact — LIVE DATA"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, GREEN)
    add_section_title(slide, "Measured Improvement in Autonomous Operations")

    # ─ Panel 1: MTTR ─
    p1 = add_rounded_rect(slide, 0.5, 1.4, 3.9, 3.5, DARK_CARD,
                          border_color=GREEN, border_width=Pt(2))
    p1h = add_rounded_rect(slide, 0.5, 1.4, 3.9, 0.5, GREEN)
    set_shape_text(p1h, "MTTR REDUCTION", font_size=13, color=WHITE, bold=True)

    add_textbox(slide, 0.7, 2.1, 3.5, 1.0,
                "104.2s", font_size=52, color=GREEN, bold=True,
                alignment=PP_ALIGN.CENTER)
    add_textbox(slide, 0.7, 3.0, 3.5, 0.4,
                "vs 45 min manual", font_size=14, color=GRAY,
                alignment=PP_ALIGN.CENTER)
    # Comparison bars
    bar_la = add_rounded_rect(slide, 1.0, 3.5, 0.8, 0.35, GREEN)
    set_shape_text(bar_la, "104s", font_size=10, color=WHITE, bold=True)
    bar_trad = add_rounded_rect(slide, 1.0, 3.95, 3.1, 0.35, RED)
    set_shape_text(bar_trad, "2700s (45 min)", font_size=10, color=WHITE, bold=True)

    # ─ Panel 2: Success Rates ─
    p2 = add_rounded_rect(slide, 4.65, 1.4, 4.0, 3.5, DARK_CARD,
                          border_color=BLUE, border_width=Pt(2))
    p2h = add_rounded_rect(slide, 4.65, 1.4, 4.0, 0.5, BLUE)
    set_shape_text(p2h, "SUCCESS RATES", font_size=13, color=WHITE, bold=True)

    # Bar chart
    rates = [
        ("Scale-up", 100, 43, GREEN),
        ("Restart", 60, 5, ORANGE),
        ("Overall", 94.1, None, BLUE),
    ]
    for i, (label, rate, count, color) in enumerate(rates):
        y = 2.15 + i * 0.85
        add_textbox(slide, 4.85, y, 1.5, 0.3, label,
                    font_size=11, color=LIGHT_GRAY, bold=True)
        bar_w = rate / 100 * 2.2
        bar = add_rounded_rect(slide, 6.3, y + 0.3, bar_w, 0.3, color)
        extra = f" ({count})" if count else ""
        add_textbox(slide, 6.3 + bar_w + 0.1, y + 0.28, 1.0, 0.3,
                    f"{rate}%{extra}", font_size=11, color=color, bold=True)

    # ─ Panel 3: Operational Impact ─
    p3 = add_rounded_rect(slide, 8.9, 1.4, 3.9, 3.5, DARK_CARD,
                          border_color=PURPLE, border_width=Pt(2))
    p3h = add_rounded_rect(slide, 8.9, 1.4, 3.9, 0.5, PURPLE)
    set_shape_text(p3h, "OPERATIONAL IMPACT", font_size=13, color=WHITE, bold=True)

    metrics = [
        ("100%", "Autonomous Resolution", "(17/17 incidents)"),
        ("48", "Total Evaluations", ""),
        ("10.1s", "Avg Recovery Time", "(successful)"),
    ]
    for i, (val, label, sub) in enumerate(metrics):
        y = 2.1 + i * 0.95
        add_textbox(slide, 9.1, y, 1.2, 0.5, val,
                    font_size=22, color=PURPLE, bold=True)
        add_textbox(slide, 10.3, y, 2.3, 0.3, label,
                    font_size=12, color=WHITE, bold=True)
        if sub:
            add_textbox(slide, 10.3, y + 0.3, 2.3, 0.3, sub,
                        font_size=10, color=GRAY)

    # ─ Bottom callout boxes ─
    callouts = [
        ("Dependency\ncorrelation: 92%", BLUE),
        ("Confidence gate\nprecision: 88%", GREEN),
        ("False positive\nrate: 4%", ORANGE),
        ("Avg recovery\ntime: 10.1s", PURPLE),
    ]
    for i, (text, color) in enumerate(callouts):
        x = 0.5 + i * 3.15
        box = add_rounded_rect(slide, x, 5.3, 2.95, 0.9, DARKER,
                               border_color=color, border_width=Pt(1.5))
        set_shape_multiline(box, [
            (text.split('\n')[0], 12, color, True, PP_ALIGN.CENTER),
            (text.split('\n')[1], 13, WHITE, True, PP_ALIGN.CENTER),
        ])

    # Data source note
    add_textbox(slide, 0.5, 6.5, 12, 0.4,
                "All metrics from live incident data  ·  17 total incidents  ·  48 evaluations  ·  PostgreSQL-backed",
                font_size=11, color=GRAY, alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 10)
    add_speaker_notes(slide,
        "Let's talk impact. MTTR: 104 seconds fully autonomous vs 45 minutes manual. "
        "94.1% overall remediation success rate. 100% of incidents resolved autonomously. "
        "These are real numbers from real incident data.")


def build_slide_11(prs):
    """Real Incident Examples"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, RED)
    add_section_title(slide, "Systems in Action: Three Scenarios")

    cards = [
        {
            "title": "Redis Failure",
            "color": RED,
            "icon": "⚡",
            "steps": [
                ("Detection", "Pod crash detected"),
                ("RCA", "redis-cart identified"),
                ("Action", "Restart pod"),
                ("Verify", "Health check passed"),
                ("Result", "Resolved in 15s"),
            ],
        },
        {
            "title": "Cascading Frontend",
            "color": ORANGE,
            "icon": "🔗",
            "steps": [
                ("Symptom", "Frontend 500 errors"),
                ("Trace", "Frontend → CartService"),
                ("Root", "Redis-Cart failure"),
                ("Fix", "Fixed root cause"),
                ("Result", "3 services recovered"),
            ],
        },
        {
            "title": "Resource Contention",
            "color": BLUE,
            "icon": "📊",
            "steps": [
                ("Detect", "Memory pressure alert"),
                ("History", "Check past outcomes"),
                ("Decision", "Scale-up > Restart"),
                ("Action", "Scale replicas"),
                ("Result", "Load distributed"),
            ],
        },
    ]

    for idx, card_data in enumerate(cards):
        x = 0.5 + idx * 4.2
        color = card_data["color"]

        # Card background
        card = add_rounded_rect(slide, x, 1.5, 3.9, 5.2, DARK_CARD,
                                border_color=color, border_width=Pt(2))

        # Header
        hdr = add_rounded_rect(slide, x, 1.5, 3.9, 0.6, color)
        set_shape_text(hdr, f'{card_data["icon"]}  {card_data["title"]}',
                       font_size=15, color=WHITE, bold=True)

        # Steps
        for i, (step, desc) in enumerate(card_data["steps"]):
            y = 2.3 + i * 0.85
            # Step number dot
            dot = add_oval(slide, x + 0.25, y + 0.05, 0.35, 0.35, color)
            set_shape_text(dot, str(i + 1), font_size=11, color=WHITE, bold=True)
            # Step label
            add_textbox(slide, x + 0.7, y - 0.05, 1.3, 0.3, step,
                        font_size=12, color=color, bold=True)
            # Description
            add_textbox(slide, x + 0.7, y + 0.25, 2.9, 0.35, desc,
                        font_size=12, color=LIGHT_GRAY)
            # Connector line
            if i < 4:
                add_line(slide, x + 0.42, y + 0.45, x + 0.42, y + 0.82,
                         ACCENT_GRAY, Pt(1))

    add_slide_number(slide, 11)
    add_speaker_notes(slide,
        "Three real incidents. First: Redis pod crashes. 15 seconds total. Second: "
        "Frontend 500 errors traced to Redis root cause. Third: Memory contention, "
        "system chose scale-up over restart based on history.")


def build_slide_12(prs):
    """System Architecture — Full Stack"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, BLUE)
    add_section_title(slide, "From Telemetry to Autonomy: Full Stack")

    # 7-layer vertical stack
    layers = [
        ("DASHBOARD", "Streamlit Real-time UI", BLUE, 1.5),
        ("DECISION & LEARNING LAYER", "Strategy Selection + Outcome Tracking", PURPLE, 2.3),
        ("AUTO-REMEDIATION", "kubectl exec, scale, rollout", GREEN, 3.1),
    ]

    for label, desc, color, y in layers:
        bx = add_rounded_rect(slide, 1.5, y, 7.5, 0.65, DARK_CARD,
                               border_color=color, border_width=Pt(2))
        set_shape_multiline(bx, [
            (label, 13, color, True, PP_ALIGN.LEFT),
            (desc, 10, GRAY, False, PP_ALIGN.LEFT),
        ])
        if y < 3.1:
            add_line(slide, 5.25, y + 0.7, 5.25, y + 0.75, ACCENT_GRAY, Pt(2))

    # Middle engines row
    mid_engines = [
        ("RCA Engine", RED),
        ("LLM\nOllama", PURPLE),
        ("Metrics\nMonitor", ORANGE),
        ("Dependency\nGraph", BLUE),
    ]
    for i, (label, color) in enumerate(mid_engines):
        x = 1.5 + i * 1.95
        bx = add_rounded_rect(slide, x, 4.0, 1.8, 0.9, DARK_CARD,
                               border_color=color, border_width=Pt(1.5))
        set_shape_text(bx, label, font_size=11, color=color, bold=True)

    add_line(slide, 5.25, 3.8, 5.25, 3.95, ACCENT_GRAY, Pt(2))

    # Log Collection
    log_layer = add_rounded_rect(slide, 1.5, 5.1, 7.5, 0.55, DARK_CARD,
                                 border_color=ORANGE, border_width=Pt(2))
    set_shape_text(log_layer, "LOG COLLECTION  ·  Structured Parsing  ·  Signal Extraction",
                   font_size=12, color=ORANGE, bold=True)

    add_line(slide, 5.25, 4.95, 5.25, 5.05, ACCENT_GRAY, Pt(2))

    # K8s Cluster
    k8s = add_rounded_rect(slide, 1.5, 5.85, 7.5, 0.65, DARKER,
                           border_color=BLUE, border_width=Pt(2))
    set_shape_multiline(k8s, [
        ("KUBERNETES CLUSTER", 13, BLUE, True, PP_ALIGN.LEFT),
        ("12 Microservices  ·  Online Boutique Demo App", 10, GRAY, False, PP_ALIGN.LEFT),
    ])

    add_line(slide, 5.25, 5.7, 5.25, 5.8, ACCENT_GRAY, Pt(2))

    # Tech Stack sidebar
    tech_card = add_rounded_rect(slide, 9.5, 1.5, 3.3, 5.0, DARK_CARD,
                                 border_color=ACCENT_GRAY, border_width=Pt(1.5))
    add_textbox(slide, 9.7, 1.6, 2.9, 0.4, "TECH STACK",
                font_size=13, color=WHITE, bold=True)
    add_line(slide, 9.8, 2.0, 12.4, 2.0, ACCENT_GRAY, Pt(1))

    techs = [
        ("FastAPI", "REST API Layer", BLUE),
        ("PostgreSQL", "Incident Database", GREEN),
        ("Ollama", "qwen2.5:3b LLM", PURPLE),
        ("Scikit-learn", "ML Strategy Select", ORANGE),
        ("kubectl", "K8s API Interface", BLUE),
        ("Streamlit", "Dashboard UI", RED),
        ("Docker", "Containerization", BLUE),
    ]
    for i, (tech, desc, color) in enumerate(techs):
        y = 2.15 + i * 0.6
        dot = add_rounded_rect(slide, 9.8, y + 0.08, 0.2, 0.2, color)
        add_textbox(slide, 10.1, y - 0.02, 1.4, 0.3, tech,
                    font_size=12, color=WHITE, bold=True)
        add_textbox(slide, 10.1, y + 0.25, 2.4, 0.3, desc,
                    font_size=10, color=GRAY)

    add_slide_number(slide, 12)
    add_speaker_notes(slide,
        "Complete system. Each layer has a purpose. It's not one monolithic script. "
        "It's a reasoning platform built from the ground up.")


def build_slide_13(prs):
    """Key Differentiators"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, PURPLE)
    add_section_title(slide, "What Makes This Different")

    pillars = [
        ("Dependency\nAware", "Traces topology\ngraph, not just\nisolated alerts",
         BLUE, "🔗"),
        ("Adaptive\nLearning", "Improves over\ntime from real\noutcome data",
         GREEN, "🧠"),
        ("Confidence\nGated", "4% false positive\nrate via certainty\nscoring",
         ORANGE, "🎯"),
        ("Fully\nAutonomous", "100% resolution\nrate with zero\nhuman intervention",
         PURPLE, "⚡"),
    ]

    for i, (title, desc, color, icon) in enumerate(pillars):
        x = 0.6 + i * 3.15
        # Card
        card = add_rounded_rect(slide, x, 1.5, 2.9, 3.5, DARK_CARD,
                                border_color=color, border_width=Pt(2))
        # Icon circle
        ic = add_oval(slide, x + 1.05, 1.25, 0.8, 0.8, color)
        set_shape_text(ic, icon, font_size=20, color=WHITE, bold=True)

        # Title
        add_textbox(slide, x + 0.15, 2.2, 2.6, 0.7,
                    title, font_size=17, color=color, bold=True,
                    alignment=PP_ALIGN.CENTER)

        # Divider
        add_line(slide, x + 0.4, 3.0, x + 2.5, 3.0, ACCENT_GRAY, Pt(1))

        # Description
        add_textbox(slide, x + 0.2, 3.1, 2.5, 1.2,
                    desc, font_size=13, color=LIGHT_GRAY,
                    alignment=PP_ALIGN.CENTER)

    # Competitive comparison row
    add_textbox(slide, 0.6, 5.3, 12, 0.4, "COMPETITIVE COMPARISON",
                font_size=13, color=GRAY, bold=True)

    comparisons = [
        ("vs Alert-Based Systems", "We reason. They fire alarms.", RED),
        ("vs Pure ML Solutions", "We're interpretable and explainable.", ORANGE),
        ("vs Manual Operations", "Seconds vs minutes. Always on.", GREEN),
    ]
    for i, (versus, desc, color) in enumerate(comparisons):
        x = 0.6 + i * 4.1
        comp = add_rounded_rect(slide, x, 5.7, 3.9, 0.9, DARKER,
                                border_color=color, border_width=Pt(1.5))
        set_shape_multiline(comp, [
            (versus, 12, color, True, PP_ALIGN.CENTER),
            (desc, 12, LIGHT_GRAY, False, PP_ALIGN.CENTER),
        ])

    add_slide_number(slide, 13)
    add_speaker_notes(slide,
        "Four differentiators: dependency awareness, adaptive learning, confidence "
        "gating, full autonomy. Vs alert-based systems (we reason, they fire alarms). "
        "Vs pure ML (we're interpretable). Vs manual ops (seconds vs minutes).")


def build_slide_14(prs):
    """Evolution & Iterations"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)
    add_top_accent_line(slide, ORANGE)
    add_section_title(slide, "From Prototype to Production: 11 Iterations")

    # Horizontal timeline
    # Main timeline line
    add_line(slide, 0.8, 3.8, 12.5, 3.8, ACCENT_GRAY, Pt(3))

    milestones = [
        ("v1.0", "Basic\nPipeline", "Log parsing &\nalert generation", RED, True),
        ("v2.0", "Redis\nHandling", "Service-specific\nremediation", ORANGE, False),
        ("v4.0", "Multi-Service\n+ Deps", "Dependency graph\n& correlation", BLUE, True),
        ("v8.0", "Confidence\nGates", "Certainty scoring\n& safety", GREEN, False),
        ("v9.0", "LLM\nIntegration", "Ollama qwen2.5:3b\nhybrid reasoning", PURPLE, True),
        ("v11", "Learning +\nDashboard", "Adaptive ML &\nStreamlit UI", GREEN, False),
    ]

    for i, (version, title, desc, color, above) in enumerate(milestones):
        x = 1.0 + i * 2.0
        dot_y = 3.6

        # Dot on timeline
        dot = add_oval(slide, x + 0.4, dot_y, 0.4, 0.4, color)
        set_shape_text(dot, "", font_size=1, color=color)

        if above:
            card_y = 1.5
            line_y1 = 2.6
            line_y2 = 3.75
        else:
            card_y = 4.5
            line_y1 = 4.05
            line_y2 = 4.45

        # Vertical line from dot to card
        add_line(slide, x + 0.6, min(line_y1, line_y2),
                 x + 0.6, max(line_y1, line_y2), color, Pt(1.5))

        # Card
        card = add_rounded_rect(slide, x - 0.15, card_y, 1.9, 1.0, DARK_CARD,
                                border_color=color, border_width=Pt(1.5))
        set_shape_multiline(card, [
            (version, 12, color, True, PP_ALIGN.CENTER),
            (title.replace('\n', ' '), 11, WHITE, True, PP_ALIGN.CENTER),
        ])

        # Description below/above card
        desc_y = card_y + 1.05 if above else card_y - 0.65
        if not above:
            desc_y = card_y + 1.05
        add_textbox(slide, x - 0.15, desc_y, 1.9, 0.6,
                    desc.replace('\n', ' '), font_size=9, color=GRAY,
                    alignment=PP_ALIGN.CENTER)

    # Bottom note
    note = add_rounded_rect(slide, 2.5, 6.5, 8.333, 0.55, DARKER,
                            border_color=ACCENT_GRAY)
    set_shape_text(note,
        "Each iteration validated against real incident data before progressing",
        font_size=13, color=GRAY, alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 14)
    add_speaker_notes(slide,
        "This evolved through 11 iterations. Rules first. Then correlation. Then "
        "safety gates. Then reasoning. Then learning. That's the foundation of a "
        "system you can trust.")


def build_slide_15(prs):
    """Future Vision + Closing"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG)

    # Grid background
    draw_grid_pattern(slide, 24, 14, RGBColor(0x26, 0x26, 0x2C))

    add_top_accent_line(slide, PURPLE)
    add_section_title(slide, "Next Frontiers in Cloud Operations")

    # Three vision cards
    visions = [
        ("🔮", "Predictive\nAutonomy", "Prevent failures before\nthey happen. Anomaly\nforecasting with temporal\npattern analysis.",
         PURPLE),
        ("🌐", "Multi-Cluster\nReasoning", "Coordinate remediation\nacross regions and\nclusters. Global\noperational intelligence.",
         BLUE),
        ("💬", "Natural Language\nOperations", "Describe intent in\nEnglish. The system\ntranslates to\ninfrastructure actions.",
         GREEN),
    ]

    for i, (icon, title, desc, color) in enumerate(visions):
        x = 0.8 + i * 4.1
        card = add_rounded_rect(slide, x, 1.5, 3.7, 3.5, DARK_CARD,
                                border_color=color, border_width=Pt(2))

        # Icon
        ic = add_oval(slide, x + 1.45, 1.25, 0.8, 0.8, color)
        set_shape_text(ic, icon, font_size=22, color=WHITE, bold=True)

        # Title
        add_textbox(slide, x + 0.2, 2.2, 3.3, 0.7,
                    title, font_size=18, color=color, bold=True,
                    alignment=PP_ALIGN.CENTER)

        add_line(slide, x + 0.5, 3.0, x + 3.2, 3.0, ACCENT_GRAY, Pt(1))

        # Description
        add_textbox(slide, x + 0.3, 3.1, 3.1, 1.3,
                    desc, font_size=13, color=LIGHT_GRAY,
                    alignment=PP_ALIGN.CENTER)

    # Center quote
    quote_box = add_rounded_rect(slide, 1.5, 5.3, 10.333, 0.85, DARKER,
                                 border_color=PURPLE, border_width=Pt(2))
    set_shape_text(quote_box,
        '"Kubernetes automated orchestration. The next frontier is automating operational intelligence."',
        font_size=16, color=PURPLE, bold=False, alignment=PP_ALIGN.CENTER)

    # Project name and closing
    add_textbox(slide, 2.0, 6.4, 9.333, 0.5,
                "Log_Analyzer  ·  Autonomous AIOps for Kubernetes",
                font_size=18, color=WHITE, bold=True,
                alignment=PP_ALIGN.CENTER)

    add_textbox(slide, 2.0, 6.9, 9.333, 0.4,
                "Thank you  ·  Questions?",
                font_size=14, color=GRAY,
                alignment=PP_ALIGN.CENTER)

    add_slide_number(slide, 15)
    add_speaker_notes(slide,
        "Three paths forward. Predictive autonomy. Multi-cluster reasoning. "
        "Natural language operations. These are logical extensions of the reasoning "
        "layer we've built. Kubernetes solved orchestration. Operations requires reasoning.")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    prs = Presentation()

    # Set 16:9 widescreen
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    print("Building slides...")
    builders = [
        build_slide_01,
        build_slide_02,
        build_slide_03,
        build_slide_04,
        build_slide_05,
        build_slide_06,
        build_slide_07,
        build_slide_08,
        build_slide_09,
        build_slide_10,
        build_slide_11,
        build_slide_12,
        build_slide_13,
        build_slide_14,
        build_slide_15,
    ]

    for i, builder in enumerate(builders, 1):
        print(f"  Slide {i:2d}/15 ... ", end="")
        builder(prs)
        print("done")

    output_path = "Log_Analyzer_Hackathon.pptx"
    prs.save(output_path)
    print(f"\n✅ Presentation saved to: {output_path}")
    print(f"   Slides: {len(prs.slides)}")
    print(f"   Size:   16:9 widescreen")


if __name__ == "__main__":
    main()
