import streamlit as st
import pandas as pd
import io
import re

st.set_page_config(page_title="拣货单 & 发货单生成器", layout="wide")
st.title("拣货单 & 发货单生成器")


# ── Parsing helpers ──────────────────────────────────────────────────────────

def extract_location(loc_field: str) -> str:
    """Pull 'A-01-01' out of 'Product ｜ 库位：A-01-01'."""
    m = re.search(r"库位：(\S+)", str(loc_field))
    return m.group(1).strip() if m else str(loc_field).strip()


def parse_customer_rows(df: pd.DataFrame) -> list[dict]:
    """Expand each customer order row into one dict per product."""
    rows = []
    for _, r in df.iterrows():
        product_raw = str(r.get("Product Name", "")).strip()
        size        = str(r.get("Size'", "")).strip()
        loc_raw     = str(r.get("库位", "")).strip()
        tracking    = str(r.get("查物流 Tracking No.", "")).strip()
        note        = str(r.get("備註", "")).strip()

        products  = [p.strip() for p in product_raw.split(",") if p.strip()]
        loc_parts = [l.strip() for l in loc_raw.split(",")     if l.strip()]

        for i, product in enumerate(products):
            loc = extract_location(loc_parts[i]) if i < len(loc_parts) else ""
            rows.append(dict(tracking=tracking, product=product,
                             location=loc, size=size, note=note,
                             address=str(r.get("Shipping Info", "")).strip()))
    return rows


def parse_influencer_rows(df: pd.DataFrame) -> list[dict]:
    """Expand each influencer row into one dict per product (from 款式+库位 column)."""
    rows = []
    for _, r in df.iterrows():
        size     = str(r.get("Size", "")).strip()
        loc_raw  = str(r.get("款式 + 库位", "")).strip()
        tracking = str(r.get("查物流 Tracking No.", "")).strip()
        note     = str(r.get("備註", "")).strip()

        entries = [e.strip() for e in loc_raw.split(",") if e.strip()]
        for entry in entries:
            m = re.match(r"(.+?)\s*｜\s*库位：(\S+)", entry)
            if m:
                product, loc = m.group(1).strip(), m.group(2).strip()
            else:
                product, loc = entry, ""
            rows.append(dict(tracking=tracking, product=product,
                             location=loc, size=size, note=note,
                             address=str(r.get("地址", "")).strip()))
    return rows


# ── Report generators ────────────────────────────────────────────────────────

def make_pick_list(rows: list[dict]) -> pd.DataFrame:
    """Aggregate by 库位 / 款式, pivot sizes to columns (S / M / L)."""
    if not rows:
        return pd.DataFrame(columns=["库位", "款式", "S", "M", "L", "合计"])

    df = pd.DataFrame(rows)
    df["qty"] = 1

    pivot = (
        df.groupby(["location", "product", "size"])["qty"]
        .sum()
        .unstack(fill_value=0)
        .reset_index()
    )
    pivot.columns.name = None

    for col in ["S", "M", "L"]:
        if col not in pivot.columns:
            pivot[col] = 0

    pivot["合计"] = pivot[["S", "M", "L"]].sum(axis=1)
    pivot = pivot[["location", "product", "S", "M", "L", "合计"]]
    pivot.columns = ["库位", "款式", "S", "M", "L", "合计"]
    return pivot.sort_values(["库位", "款式"]).reset_index(drop=True)


def make_shipping_list(rows: list[dict]) -> pd.DataFrame:
    """One row per product line with tracking, address, details and note."""
    if not rows:
        return pd.DataFrame(columns=["Tracking No.", "地址", "款式", "库位", "尺码", "数量", "备注"])

    df = pd.DataFrame(rows)
    df["qty"] = 1

    ship = (
        df.groupby(["tracking", "product", "location", "size", "note", "address"])["qty"]
        .sum()
        .reset_index()
    )
    ship.columns = ["Tracking No.", "款式", "库位", "尺码", "备注", "地址", "数量"]
    ship = ship[["Tracking No.", "地址", "款式", "库位", "尺码", "数量", "备注"]]
    return ship.sort_values(["Tracking No.", "库位"]).reset_index(drop=True)


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue()


def show_section(label: str, rows: list[dict], key_prefix: str):
    pick = make_pick_list(rows)
    ship = make_shipping_list(rows)

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
    st.dataframe(ship, use_container_width=True, hide_index=True)
    st.download_button(
        f"⬇ 下载{label}发货单 CSV",
        to_csv_bytes(ship),
        f"{label}发货单.csv",
        "text/csv",
        key=f"{key_prefix}_ship",
    )


# ── UI ───────────────────────────────────────────────────────────────────────

col_c, col_i = st.columns(2)

with col_c:
    customer_file = st.file_uploader(
        "上传客人水单 CSV", type=["csv"], key="customer_upload"
    )

with col_i:
    influencer_file = st.file_uploader(
        "上传深度达人单 CSV", type=["csv"], key="influencer_upload"
    )

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
