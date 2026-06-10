import io
import re
from collections import OrderedDict

import pandas as pd
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)
from reportlab.lib.styles import ParagraphStyle

# Register CJK font (bundled with reportlab, no external file needed)
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
CJK = "STSong-Light"

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
        product_raw  = str(r.get("Product Name", "")).strip()
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

        products  = [p.strip() for p in product_raw.split(",")
                     if p.strip() and not is_blank(p)]
        loc_parts = [l.strip() for l in loc_raw.split(",") if l.strip()]

        for i, product in enumerate(products):
            loc = extract_location(loc_parts[i]) if i < len(loc_parts) else ""
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
                            leftMargin=0.75*inch, rightMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)

    h1    = ParagraphStyle("h1",    fontName=CJK, fontSize=13, spaceAfter=4, leading=18)
    body  = ParagraphStyle("body",  fontName=CJK, fontSize=10, spaceAfter=3, leading=14)
    label = ParagraphStyle("label", fontName=CJK, fontSize=10, spaceAfter=3,
                           leading=14, textColor=colors.HexColor("#555555"))
    note  = ParagraphStyle("note",  fontName=CJK, fontSize=10, spaceAfter=3,
                           leading=14, textColor=colors.red)

    # Group rows preserving insertion order (= upload order)
    groups: dict[str, list[dict]] = OrderedDict()
    for row in rows:
        groups.setdefault(row["tracking"], []).append(row)

    story = []
    first_page = True

    for tracking, items in groups.items():
        if not first_page:
            story.append(PageBreak())
        first_page = False

        # ── Header ──
        story.append(Paragraph(f"Tracking No.: {tracking}", h1))
        story.append(Spacer(1, 0.08*inch))

        name    = items[0]["name"]
        address = items[0]["address"]

        story.append(Paragraph(f"收件人：{name}", body))
        for line in address.split("\n"):
            if line.strip():
                story.append(Paragraph(line.strip(), body))
        story.append(Spacer(1, 0.15*inch))

        # ── Product table (aggregate qty) ──
        merged: dict[tuple, int] = {}
        for item in items:
            key = (item["product"], item["location"], item["size"])
            merged[key] = merged.get(key, 0) + 1

        table_data = [["款式", "库位", "尺码", "数量"]]
        for (product, loc, sz), qty in merged.items():
            table_data.append([product, loc, sz, str(qty)])

        col_widths = [3.0*inch, 1.2*inch, 0.7*inch, 0.7*inch]
        tbl = Table(table_data, colWidths=col_widths)
        tbl.setStyle(TableStyle([
            ("FONTNAME",       (0, 0), (-1, -1), CJK),
            ("FONTSIZE",       (0, 0), (-1, -1), 10),
            ("BACKGROUND",     (0, 0), (-1,  0), colors.HexColor("#4A90D9")),
            ("TEXTCOLOR",      (0, 0), (-1,  0), colors.white),
            ("ALIGN",          (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",          (0, 1), (0,  -1), "LEFT"),
            ("GRID",           (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#F0F4F8")]),
            ("TOPPADDING",     (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING",  (0, 0), (-1, -1), 6),
            ("LEFTPADDING",    (0, 0), (-1, -1), 6),
        ]))
        story.append(tbl)

        # ── Notes ──
        all_notes = list(dict.fromkeys(
            item["note"] for item in items if item["note"]
        ))
        if all_notes:
            story.append(Spacer(1, 0.12*inch))
            for n in all_notes:
                story.append(Paragraph(f"备注：{n}", note))

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

col_c, col_i = st.columns(2)

with col_c:
    customer_file = st.file_uploader("上传客人水单 CSV", type=["csv"], key="customer_upload")

with col_i:
    influencer_file = st.file_uploader("上传深度达人单 CSV", type=["csv"], key="influencer_upload")

st.divider()

if customer_file:
    df_c = pd.read_csv(customer_file)
    rows_c = parse_customer_rows(df_c)
    st.markdown("## 客人水单")
    show_section("客人", rows_c, "c")
    st.divider()

if influencer_file:
    df_i = pd.read_csv(influencer_file)
    rows_i = parse_influencer_rows(df_i)
    st.markdown("## 深度达人单")
    show_section("达人", rows_i, "i")

if not customer_file and not influencer_file:
    st.info("请上传客人水单或深度达人单 CSV 文件以生成报表。")
