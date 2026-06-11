import io
import os
import re
from collections import OrderedDict

import pandas as pd
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)
from reportlab.lib.styles import ParagraphStyle


def _register_cjk_font() -> str:
    """Try to register a TTF font that covers all CJK (incl. traditional).
    Falls back to the built-in CID font if none found."""
    candidates = [
        # Linux / Streamlit Cloud (install via packages.txt: fonts-noto-cjk)
        "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        # macOS
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Songti.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("CJKFont", path))
                return "CJKFont"
            except Exception:
                continue
    # Built-in CID fallback (covers GB2312 simplified CJK)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"

CJK = _register_cjk_font()

st.set_page_config(page_title="拣货单 & 发货单生成器", layout="wide")
st.title("拣货单 & 发货单生成器")


# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_location(loc_field: str) -> str:
    m = re.search(r"库位：(\S+)", str(loc_field))
    return m.group(1).strip() if m else str(loc_field).strip()


def split_shipping_info(raw: str) -> tuple[str, str]:
    """Return (name, address) from multi-line Shipping Info.
    Format: Name / (+1)Phone / Street / City,State,Country / Zip
    """
    lines = [l.strip() for l in str(raw).split("\n") if l.strip()]
    if not lines:
        return "", ""
    name = lines[0]
    # Drop the phone line (starts with parenthesis)
    addr_lines = [l for l in lines[1:] if not re.match(r"^\(", l)]
    return name, "\n".join(addr_lines)


def is_blank(val: str) -> bool:
    return val.strip().lower() in ("", "nan")


# ── Parsers ──────────────────────────────────────────────────────────────────

def parse_customer_rows(df: pd.DataFrame) -> list[dict]:
    rows = []
    prev_tracking = prev_name = prev_address = prev_note = ""

    for _, r in df.iterrows():
        size         = str(r.get("Size'", "")).strip()
        loc_raw      = str(r.get("库位", "")).strip()
        tracking_raw = str(r.get("打包 Tracking No.", "")).strip()
        note         = str(r.get("備註", "")).strip()
        shipping_raw = str(r.get("Shipping Info", "")).strip()

        # 同上 or blank tracking → inherit from previous row
        if is_blank(tracking_raw) or tracking_raw == "同上":
            tracking = prev_tracking
        else:
            tracking = tracking_raw
            prev_tracking = tracking

        # 同上 or blank shipping info → inherit name/address
        if is_blank(shipping_raw) or shipping_raw == "同上":
            name, address = prev_name, prev_address
        else:
            name, address = split_shipping_info(shipping_raw)
            prev_name, prev_address = name, address

        if is_blank(note):
            note = ""

        # Parse product name + location directly from 库位 column
        # Each entry format: "Product Name ｜ 库位：A-01-01"
        for part in loc_raw.split(","):
            part = part.strip()
            m = re.match(r"(.+?)\s*｜\s*库位：(\S+)", part)
            if m:
                product = m.group(1).strip()
                loc     = m.group(2).strip()
            else:
                continue  # skip malformed entries
            rows.append(dict(tracking=tracking, name=name, address=address,
                             product=product, location=loc, size=size, note=note))
    return rows


def parse_influencer_rows(df: pd.DataFrame) -> list[dict]:
    rows = []
    for _, r in df.iterrows():
        size         = str(r.get("Size", "")).strip()
        loc_raw      = str(r.get("款式 + 库位", "")).strip()
        tracking     = str(r.get("打包 Tracking No.", "")).strip()
        note         = str(r.get("備註", "")).strip()
        name         = str(r.get("达人Name", "")).strip()
        address      = str(r.get("地址", "")).strip()

        if is_blank(note):
            note = ""

        entries = [e.strip() for e in loc_raw.split(",") if e.strip()]
        for entry in entries:
            m = re.match(r"(.+?)\s*｜\s*库位：(\S+)", entry)
            if m:
                product, loc = m.group(1).strip(), m.group(2).strip()
            else:
                product, loc = entry, ""
            rows.append(dict(tracking=tracking, name=name, address=address,
                             product=product, location=loc, size=size, note=note))
    return rows


# ── Pick list (CSV) ──────────────────────────────────────────────────────────

def make_pick_list(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["库位", "款式", "S", "M", "L", "合计"])
    df = pd.DataFrame(rows)
    df["qty"] = 1
    pivot = (df.groupby(["location", "product", "size"])["qty"]
               .sum().unstack(fill_value=0).reset_index())
    pivot.columns.name = None
    for col in ["S", "M", "L"]:
        if col not in pivot.columns:
            pivot[col] = 0
    pivot["合计"] = pivot[["S", "M", "L"]].sum(axis=1)
    pivot = pivot[["location", "product", "S", "M", "L", "合计"]]
    pivot.columns = ["库位", "款式", "S", "M", "L", "合计"]
    return pivot.sort_values(["库位", "款式"]).reset_index(drop=True)


# ── Shipping list (PDF) ──────────────────────────────────────────────────────

def make_shipping_pdf(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            leftMargin=0.6*inch, rightMargin=0.6*inch,
                            topMargin=0.6*inch, bottomMargin=0.6*inch)

    YELLOW     = colors.HexColor("#FFE600")
    PINK       = colors.HexColor("#FFD6E0")
    HEADER_BG  = colors.HexColor("#4A90D9")

    tracking_style = ParagraphStyle(
        "tracking", fontName=CJK, fontSize=22, leading=28,
        spaceAfter=10, backColor=YELLOW,
        leftIndent=6, rightIndent=6,
    )
    name_style = ParagraphStyle(
        "name", fontName=CJK, fontSize=16, leading=22,
        spaceAfter=4, backColor=PINK,
        leftIndent=6, rightIndent=6,
    )
    addr_style = ParagraphStyle(
        "addr", fontName=CJK, fontSize=14, leading=20,
        spaceAfter=3, leftIndent=6,
    )
    note_style = ParagraphStyle(
        "note", fontName=CJK, fontSize=14, leading=20,
        spaceAfter=3, textColor=colors.red, leftIndent=6,
    )

    # Group rows preserving insertion order
    groups: dict[str, list[dict]] = OrderedDict()
    for row in rows:
        groups.setdefault(row["tracking"], []).append(row)

    story = []
    first_page = True

    for tracking, items in groups.items():
        if not first_page:
            story.append(PageBreak())
        first_page = False

        # ── Tracking number (yellow highlight) ──
        story.append(Paragraph(f"Tracking No.: {tracking}", tracking_style))
        story.append(Spacer(1, 0.12*inch))

        # ── Recipient name (pink highlight) + address ──
        name    = items[0]["name"]
        address = items[0]["address"]

        story.append(Paragraph(name, name_style))
        for line in address.split("\n"):
            if line.strip():
                story.append(Paragraph(line.strip(), addr_style))
        story.append(Spacer(1, 0.2*inch))

        # ── Product table (aggregate qty) ──
        merged: dict[tuple, int] = {}
        for item in items:
            key = (item["product"], item["location"], item["size"])
            merged[key] = merged.get(key, 0) + 1

        table_data = [["款式", "库位", "尺码", "数量"]]
        for (product, loc, sz), qty in merged.items():
            table_data.append([product, loc, sz, str(qty)])

        usable = 7.3 * inch  # letter width minus margins
        col_widths = [usable * 0.50, usable * 0.25, usable * 0.13, usable * 0.12]
        tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("FONTNAME",       (0, 0), (-1, -1), CJK),
            ("FONTSIZE",       (0, 0), (-1,  0), 16),   # header row
            ("FONTSIZE",       (0, 1), (-1, -1), 15),   # data rows
            ("BACKGROUND",     (0, 0), (-1,  0), HEADER_BG),
            ("TEXTCOLOR",      (0, 0), (-1,  0), colors.white),
            ("ALIGN",          (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",          (0, 1), (0,  -1), "LEFT"),
            ("GRID",           (0, 0), (-1, -1), 0.8, colors.HexColor("#AAAAAA")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#F0F4F8")]),
            ("TOPPADDING",     (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING",  (0, 0), (-1, -1), 10),
            ("LEFTPADDING",    (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",   (0, 0), (-1, -1), 8),
        ]))
        story.append(tbl)

        # ── Notes ──
        all_notes = list(dict.fromkeys(
            item["note"] for item in items if item["note"]
        ))
        if all_notes:
            story.append(Spacer(1, 0.15*inch))
            for n in all_notes:
                story.append(Paragraph(f"备注：{n}", note_style))

    doc.build(story)
    return buf.getvalue()


# ── Utilities ────────────────────────────────────────────────────────────────

def to_csv_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue()


def show_section(label: str, rows: list[dict], key_prefix: str):
    pick = make_pick_list(rows)

    st.subheader(f"{label} — 拣货单")
    st.dataframe(pick, use_container_width=True, hide_index=True)
    st.download_button(
        f"⬇ 下载{label}拣货单 CSV",
        to_csv_bytes(pick),
        f"{label}拣货单.csv",
        "text/csv",
        key=f"{key_prefix}_pick",
    )

    st.divider()
    st.subheader(f"{label} — 发货单")
    pdf_bytes = make_shipping_pdf(rows)
    st.download_button(
        f"⬇ 下载{label}发货单 PDF",
        pdf_bytes,
        f"{label}发货单.pdf",
        "application/pdf",
        key=f"{key_prefix}_ship",
    )


# ── UI ───────────────────────────────────────────────────────────────────────

def read_file(f) -> pd.DataFrame:
    name = f.name.lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        return pd.read_excel(f)
    return pd.read_csv(f)


col_c, col_i = st.columns(2)

with col_c:
    customer_file = st.file_uploader(
        "上传客人水单 CSV / Excel", type=["csv", "xlsx", "xls"], key="customer_upload"
    )

with col_i:
    influencer_file = st.file_uploader(
        "上传深度达人单 CSV / Excel", type=["csv", "xlsx", "xls"], key="influencer_upload"
    )

st.divider()

if customer_file:
    df_c = read_file(customer_file)
    rows_c = parse_customer_rows(df_c)
    st.markdown("## 客人水单")
    show_section("客人", rows_c, "c")
    st.divider()

if influencer_file:
    df_i = read_file(influencer_file)
    rows_i = parse_influencer_rows(df_i)
    st.markdown("## 深度达人单")
    show_section("达人", rows_i, "i")

if not customer_file and not influencer_file:
    st.info("请上传客人水单或深度达人单 CSV / Excel 文件以生成报表。")
