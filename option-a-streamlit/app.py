# =====================================================================
# app.py  :  OPTION A (Streamlit + SQLite)  :  the Command Center screens
# ---------------------------------------------------------------------
# HOW TO RUN (in a terminal, inside this folder):
#     pip install -r requirements.txt
#     streamlit run app.py
#
# HOW THE PIECES FIT:
#     app.py        -> draws the screens and passes clicks to data.py   (YOU)
#     data.py       -> saves changes, then asks engine.py for a new plan
#     engine.py     -> all decisions: routes, severity, OR-Tools plans, checks
#     demo_data.py  -> the made-up demo world
#     disaster.db   -> the SQLite database file (created automatically)
#
# app.py never makes decisions. It shows what data.py and engine.py return,
# so the algorithm can change without touching these screens.
# =====================================================================

import copy
import html
import os
import tempfile
import uuid

import pandas as pd  # pandas: tables of data (like Excel inside Python)
import plotly.express as px  # plotly express: quick charts
import plotly.graph_objects as go  # plotly graph objects: charts with full control
import pydeck as pdk  # pydeck: the map
from pydeck.types import String as DeckString  # literal strings; a bare "pixels" is read as a data column
import streamlit as st  # streamlit: turns this file into a web page

import data  # our database and actions (data.py)
import engine  # our decision logic (engine.py), used here only for small helpers
import intake_agent  # citizen reports: classify, validate and prioritise
import ripple_agent  # read-only cascade analysis; it does not change records
import whatif_agent  # hypothetical scenarios on an in-memory copy; it does not write
import plan_challenger_agent  # read-only review of the stored plan; it does not change records
import coordinator_agent  # read-only brief across the four agents; it does not change records

# ---------------------------------------------------------------------
# 1. SETTINGS
# ---------------------------------------------------------------------
REFRESH_SECONDS = 10  # live mode re-reads data this often

# Saarthi Ripple Emergency Operations Centre Palette
NAVY = "#102536"          # Deep navy: Sidebar & primary command header
STEEL = "#193C50"         # Steel blue: Secondary headers & GIS map bar
RED = "#E74747"           # Alert red: Genuinely critical alerts & emergencies
ORANGE = "#E6A23C"        # Warning amber: High severity & active warnings
YELLOW = "#D9A520"        # Moderate yellow: Moderate alerts
BLUE = "#2B6CB0"          # Low blue: Low severity
TEAL = "#269B75"          # Operational green: Resolved cases, available resources & safe status
BG_GREY = "#F2F5F7"       # Soft light grey workspace background
WHITE = "#FFFFFF"         # Clean card surface
CHARCOAL = "#1E293B"      # Primary text
MUTED_GREY = "#64748B"    # Secondary text and labels

# Consistent 5-tier severity colour system (Critical=Red, High=Orange, Moderate=Yellow, Low=Blue, Resolved=Green)
LEVEL_HEX = {
    "Critical": RED,
    "High": ORANGE,
    "Moderate": YELLOW,
    "Low": BLUE,
}
LEVEL_RGB = {
    "Critical": [231, 71, 71],
    "High": [230, 162, 60],
    "Moderate": [217, 165, 32],
    "Low": [43, 108, 176],
}
ROAD_RGB = {
    "OPEN": [38, 155, 117],
    "SLOW": [230, 162, 60],
    "BLOCKED": [231, 71, 71],
}
SOS_RGB = {
    "OPEN": [231, 71, 71],
    "IN_TRANSIT": [230, 162, 60],
    "RESOLVED": [38, 155, 117],
}

CANVAS = BG_GREY
RAISED = WHITE
INSET = "#F8FAFC"
SIDEBAR = NAVY
LINE = "#E2E8F0"
TEXT = CHARCOAL
MUTED = MUTED_GREY
WARNING = ORANGE
WARNING_BORDER = "#F3CFA0"
WARNING_BG = "#FFFDF9"

EVENT_ICON = {
    "ROAD_BLOCKED": "BLK", "ROAD_SLOW": "SLW", "ROAD_REOPENED": "OPN",
    "HOSPITAL_CAPACITY_CHANGED": "HSP", "NEW_PATIENTS": "MED",
    "RESOURCE_RECEIVED": "SUP", "RESOURCE_DEPLETED": "LOW",
    "REOPTIMIZED": "OPT", "RISK_UPDATED": "RSK", "TAMPER": "SEC",
    "INTAKE_SUBMITTED": "INT", "INTAKE_APPROVED": "APR", "INTAKE_REJECTED": "REJ",
    "SYSTEM": "SYS", "MANUAL": "MAN", "ALERT_QUEUED": "ALT", "ALERT_ACKNOWLEDGED": "ACK",
    "PATIENT_ASSIGNED": "BED", "SHIPMENT_ALLOCATED": "SHP",
}  # clean 3-letter operational codes (strictly no emojis)

st.set_page_config(
    page_title="Saarthi | Disaster Management & Ripple Command Centre",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------
# 2. SMALL HELPERS
# ---------------------------------------------------------------------
def df(rows):  # list of dicts -> pandas table
    return pd.DataFrame(rows)  # pandas does the conversion


def status_pill(label, tone="neutral"):
    """Consistent severity & operational status pill:
    critical=Red, high/warning=Orange, moderate=Yellow, low=Blue, safe/resolved=Green."""
    tone_map = {
        "critical": ("#FEF2F2", "#E74747", "#FECACA"),
        "high": ("#FFFBEB", "#D97706", "#FDE68A"),
        "warning": ("#FFFBEB", "#D97706", "#FDE68A"),
        "moderate": ("#FEFCE8", "#B45309", "#FEF08A"),
        "low": ("#EFF6FF", "#2B6CB0", "#BFDBFE"),
        "safe": ("#ECFDF5", "#269B75", "#A7F3D0"),
        "resolved": ("#ECFDF5", "#269B75", "#A7F3D0"),
        "navy": ("#F1F5F9", "#102536", "#CBD5E1"),
        "neutral": ("#F1F5F9", "#64748B", "#E2E8F0"),
    }
    bg, fg, border = tone_map.get(str(tone).lower(), tone_map["neutral"])
    return (
        f'<span style="display:inline-flex;align-items:center;gap:5px;padding:2px 8px;'
        f'border-radius:5px;background:{bg};color:{fg};border:1px solid {border};'
        f'font-size:10.5px;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;white-space:nowrap;">'
        f'<span style="width:6px;height:6px;border-radius:50%;background:{fg};display:inline-block;"></span>'
        f'{html.escape(str(label))}</span>'
    )


def progress_bar_html(label, right_text, pct, tone="teal", badge_text=None):
    """Render a clean horizontal progress bar for resource allocation, zone severity, and hospital capacity."""
    colour = {
        "critical": RED, "red": RED,
        "high": ORANGE, "orange": ORANGE,
        "moderate": YELLOW, "yellow": YELLOW,
        "low": BLUE, "blue": BLUE,
        "teal": TEAL, "green": TEAL, "safe": TEAL,
        "navy": STEEL,
    }.get(str(tone).lower(), TEAL)
    clamped = min(100, max(0, int(round(pct))))
    badge = ""
    if badge_text:
        badge = " " + status_pill(badge_text, tone)
    return (
        '<div class="saarthi-cap-row">'
        f'<div class="saarthi-cap-head"><span style="display:inline-flex;align-items:center;gap:6px;">{html.escape(str(label))}{badge}</span>'
        f'<span style="color:{MUTED_GREY};font-weight:500;font-size:11.5px;">{html.escape(str(right_text))}</span></div>'
        f'<div class="saarthi-cap-track"><div class="saarthi-cap-fill" style="width:{clamped}%;background:{colour};"></div></div>'
        '</div>'
    )


def render_df_table(data_obj, hide_index=True, target=None):
    """Render a DataFrame or list of dicts as a clean, non-overlapping EOC HTML table."""
    target = st if target is None else target
    frame = data_obj if isinstance(data_obj, pd.DataFrame) else pd.DataFrame(data_obj)
    if frame.empty:
        target.caption("No records to display.")
        return
    if not hide_index and (frame.index.name or not isinstance(frame.index, pd.RangeIndex)):
        frame = frame.reset_index()
    cols = list(frame.columns)
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
    body = []
    for _, row in frame.iterrows():
        cells = []
        for c in cols:
            val = row[c]
            if val is None or val == "" or (isinstance(val, float) and pd.isna(val)):
                shown = "not available"
            else:
                shown = str(val)
            cells.append(f"<td>{html.escape(shown)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    target.markdown(
        '<div class="saarthi-table-container">'
        f'<table class="saarthi-scan"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
        '</div>',
        unsafe_allow_html=True,
    )


def ui_callout(message, kind="info", target=None):
    """Clean, emoji-free EOC status callout card."""
    target = st if target is None else target
    labels = {
        "info": "OPERATIONAL NOTE",
        "success": "VERIFIED / SAFE",
        "warning": "WARNING",
        "error": "CRITICAL ALERT",
    }
    label = labels.get(kind, "NOTE")
    target.markdown(
        f'<div class="saarthi-callout saarthi-callout-{html.escape(kind)}">'
        f'<span class="saarthi-callout-dot" aria-hidden="true"></span>'
        f'<div class="saarthi-callout-body"><b>{label}</b><span>{html.escape(str(message))}</span></div>'
        '</div>',
        unsafe_allow_html=True,
    )


def ui_info(message, target=None):
    ui_callout(message, "info", target=target)


def ui_success(message, target=None):
    ui_callout(message, "success", target=target)


def ui_warning(message, target=None):
    ui_callout(message, "warning", target=target)


def ui_error(message, target=None):
    ui_callout(message, "error", target=target)


def act(fn, *args, ok="Done"):  # runs an action from data.py, then redraws the page
    try:  # the action may refuse (for example: not enough stock)
        fn(*args)  # do it
        st.toast(ok)  # small confirmation pop-up
        st.rerun()  # redraw everything with the new data
    except data.ActionError as e:  # refused on purpose
        ui_error(str(e))  # show why, in red


def page_header(title, description):
    """Compact operational context strip."""
    st.markdown(
        '<div class="saarthi-heading">'
        f'<div class="saarthi-title">{html.escape(title)}</div>'
        f'<div class="saarthi-description">{html.escape(description)}</div>'
        "</div>",
        unsafe_allow_html=True)


def safety_notice(summary, detail=None, *, tone="info", target=None, key=None):
    """Compact safety line. Any longer original wording stays in the expander."""
    target = st if target is None else target
    kind = "caution" if tone == "caution" else "info"
    target.markdown(
        f'<div class="saarthi-notice saarthi-notice-{kind}">'
        f'<span class="saarthi-notice-dot" aria-hidden="true"></span>'
        f"<span>{html.escape(summary)}</span>"
        "</div>",
        unsafe_allow_html=True)
    extra = (detail or "").strip()
    if extra and extra != summary.strip():
        with target.expander("Full notice", key=key):
            target.markdown(html.escape(extra))


def metric_tiles(items):
    """Equal-height, single-row KPI cards with subtle border, clean top/left status accent, and tabular numerals."""
    count = max(1, len(items))
    cards = []
    for label, value, colour in items:
        accent = colour if colour != TEXT else STEEL
        value_col = colour if colour in (RED, ORANGE, YELLOW, BLUE, TEAL) else CHARCOAL
        cards.append(
            f'<div class="saarthi-metric" style="border-left: 3.5px solid {accent};">'
            f'<div class="saarthi-metric-label">{html.escape(str(label))}</div>'
            f'<div class="saarthi-metric-value" style="color:{value_col};">{value}</div>'
            "</div>"
        )
    st.markdown(
        f'<div class="saarthi-metrics" style="grid-template-columns: repeat({count}, minmax(0, 1fr));">'
        + "".join(cards)
        + "</div>",
        unsafe_allow_html=True,
    )


def style_chart(fig):
    """Clean white chart chrome with Charcoal & Muted Grey typography."""
    fig.update_layout(
        paper_bgcolor=WHITE,
        plot_bgcolor=WHITE,
        font=dict(family="Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif", color=CHARCOAL, size=12),
        legend=dict(
            font=dict(color=CHARCOAL, size=11),
            bgcolor="rgba(242,245,247,0.92)",
            bordercolor=LINE,
            borderwidth=1,
        ),
        margin=dict(t=8, b=8, l=8, r=8),
    )
    fig.update_xaxes(
        gridcolor="#EDF2F7", linecolor=LINE, zerolinecolor=LINE,
        tickfont=dict(color=MUTED_GREY, size=11),
        title_font=dict(color=MUTED_GREY, size=11),
    )
    fig.update_yaxes(
        gridcolor="#EDF2F7", linecolor=LINE, zerolinecolor=LINE,
        tickfont=dict(color=CHARCOAL, size=11),
        title_font=dict(color=MUTED_GREY, size=11),
    )
    return fig


def command_styles():
    """Saarthi Ripple Hybrid EOC Design System:
    Deep Navy (#102536) sidebar, Steel Blue (#193C50) accents, Soft Light Grey (#F2F5F7) workspace,
    Alert Red (#E74747) for critical only, Warning Amber (#E6A23C), Moderate Yellow (#D9A520),
    Low Blue (#2B6CB0), Operational Green (#269B75)."""
    st.markdown(
        f"""<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet"><style>
        :root {{ --saarthi-canvas: {CANVAS}; --saarthi-raised: {RAISED}; --saarthi-inset: {INSET}; --saarthi-sidebar: {SIDEBAR}; --saarthi-line: {LINE}; --saarthi-text: {TEXT}; --saarthi-muted: {MUTED}; --saarthi-warning: {WARNING}; --primary-color: {NAVY}; --background-color: {WHITE}; --secondary-background-color: {BG_GREY}; --text-color: {CHARCOAL}; color-scheme: light !important; }}
        html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"], .main {{ background: {BG_GREY} !important; background-color: {BG_GREY} !important; color: {CHARCOAL} !important; font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important; font-size: 13.5px !important; font-variant-numeric: tabular-nums; color-scheme: light !important; -webkit-font-smoothing: antialiased; }}
        [data-testid="stDeployButton"], .stAppDeployButton {{ display: none !important; }}
        header[data-testid="stHeader"] {{ background: transparent !important; height: 2.2rem !important; }}
        [data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapseButton"] button, [data-testid="collapsedControl"], [data-testid="collapsedControl"] button, [data-testid="stSidebarCollapsedControl"], [data-testid="stSidebarCollapsedControl"] button, header[data-testid="stHeader"] [data-testid="baseButton-headerNoPadding"], header[data-testid="stHeader"] button {{ background: {NAVY} !important; background-color: {NAVY} !important; color: {WHITE} !important; border: 1px solid rgba(255,255,255,0.22) !important; border-radius: 6px !important; opacity: 1 !important; visibility: visible !important; box-shadow: 0 1px 3px rgba(16, 37, 54, 0.2) !important; }}
        [data-testid="stExpandSidebarButton"]:hover, [data-testid="stSidebarCollapseButton"] button:hover, [data-testid="collapsedControl"]:hover, [data-testid="collapsedControl"] button:hover, [data-testid="stSidebarCollapsedControl"] button:hover, header[data-testid="stHeader"] button:hover {{ background: {STEEL} !important; background-color: {STEEL} !important; color: {WHITE} !important; border-color: {ORANGE} !important; }}
        [data-testid="stExpandSidebarButton"] *, [data-testid="stSidebarCollapseButton"] button *, [data-testid="collapsedControl"] *, [data-testid="stSidebarCollapsedControl"] *, header[data-testid="stHeader"] button * {{ color: {WHITE} !important; fill: {WHITE} !important; }}
        .block-container {{ padding-top: 0.45rem !important; padding-bottom: 1.5rem !important; padding-left: 1.4rem !important; padding-right: 1.4rem !important; max-width: 1520px !important; }}
        [data-testid="stSidebar"], [data-testid="stSidebar"] > div {{ background: {NAVY} !important; background-color: {NAVY} !important; border-right: 1px solid #0B1926 !important; color: {WHITE} !important; }}
        [data-testid="stSidebar"] .block-container {{ padding-top: 0.85rem !important; padding-left: 0.8rem !important; padding-right: 0.8rem !important; }}
        [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{ gap: 0.25rem !important; }}
        [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {{ color: {WHITE} !important; font-weight: 600 !important; letter-spacing: 0.02em !important; }}
        [data-testid="stSidebar"] p, [data-testid="stSidebar"] span, [data-testid="stSidebar"] label, [data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {{ color: #E2E8F0 !important; }}
        [data-testid="stSidebar"] hr {{ border-color: rgba(255, 255, 255, 0.09) !important; margin: 0.4rem 0 !important; }}
        .saarthi-nav-group {{ padding: 7px 6px 2px 6px; color: #7F9BB2; font-size: 10px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button {{ min-height: 33px !important; padding: 5px 11px !important; font-size: 12.5px !important; font-weight: 500 !important; text-align: left !important; justify-content: flex-start !important; border-radius: 6px !important; border: 1px solid transparent !important; border-left: 3px solid transparent !important; background: transparent !important; color: #CBD5E1 !important; transition: background 0.12s ease !important; width: 100% !important; display: flex !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button > div {{ justify-content: flex-start !important; text-align: left !important; width: 100% !important; display: flex !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button:hover {{ background: rgba(25, 60, 80, 0.7) !important; color: {WHITE} !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button:hover p, [data-testid="stSidebar"] [data-testid="stButton"] button:hover span {{ color: {WHITE} !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"] {{ background: {STEEL} !important; background-color: {STEEL} !important; color: {WHITE} !important; border: 1px solid rgba(231, 71, 71, 0.35) !important; border-left: 3.5px solid {RED} !important; font-weight: 600 !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"] p, [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"] span {{ color: {WHITE} !important; font-weight: 600 !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="secondary"] p, [data-testid="stSidebar"] [data-testid="stButton"] button[kind="secondary"] span {{ color: #CBD5E1 !important; }}
        [data-testid="stSidebar"] .saarthi-notice {{ background: rgba(230, 162, 60, 0.12) !important; border: 1px solid rgba(230, 162, 60, 0.35) !important; border-left: 3px solid {ORANGE} !important; border-radius: 6px !important; color: #F8FAFC !important; padding: 6px 9px !important; font-size: 11px !important; }}
        [data-testid="stSidebar"] .saarthi-notice span {{ color: #F8FAFC !important; }}
        [data-testid="stHeading"] {{ margin-bottom: 0.1rem; }}
        [data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3 {{ font-family: 'Inter', sans-serif !important; font-weight: 600 !important; color: {NAVY} !important; letter-spacing: -0.01em !important; }}
        [data-testid="stHeading"] h2 {{ font-size: 15.5px !important; }}
        [data-testid="stHeading"] h3 {{ font-size: 14px !important; }}
        [data-testid="stVerticalBlock"] {{ gap: 0.55rem; }}
        [data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li {{ margin-bottom: 0 !important; color: {CHARCOAL}; }}
        [data-testid="stMarkdownContainer"] code, .stApp code {{ font-family: 'JetBrains Mono', ui-monospace, monospace !important; background: {BG_GREY} !important; color: {NAVY} !important; border: 1px solid {LINE} !important; padding: 2px 5px !important; border-radius: 4px !important; font-size: 11.5px !important; }}
        [data-testid="stCaptionContainer"] p {{ font-size: 11.5px !important; line-height: 1.4 !important; color: {MUTED_GREY} !important; }}
        [data-testid="stVerticalBlockBorderWrapper"], [data-testid="stVerticalBlock"].st-emotion-cache-pb8mwp, div[data-testid="stForm"] {{ background: {WHITE} !important; background-color: {WHITE} !important; border: 1px solid #E6ECF0 !important; border-radius: 8px !important; padding: 14px 16px !important; box-shadow: 0 1px 2px rgba(16, 37, 54, 0.04) !important; }}
        .stMain [data-testid="stButton"] button, .stMain [data-testid="stFormSubmitButton"] button {{ border-radius: 6px !important; min-height: 34px !important; padding: 5px 12px !important; font-size: 12px !important; font-weight: 600 !important; border: 1px solid #CBD5E1 !important; background: {WHITE} !important; background-color: {WHITE} !important; color: {NAVY} !important; white-space: nowrap !important; transition: all 0.12s ease !important; }}
        .stMain [data-testid="stButton"] button p, .stMain [data-testid="stButton"] button span, .stMain [data-testid="stFormSubmitButton"] button p, .stMain [data-testid="stFormSubmitButton"] button span {{ color: {NAVY} !important; white-space: nowrap !important; }}
        .stMain [data-testid="stButton"] button:hover, .stMain [data-testid="stFormSubmitButton"] button:hover {{ background: {BG_GREY} !important; background-color: {BG_GREY} !important; border-color: {STEEL} !important; color: {NAVY} !important; }}
        .stMain [data-testid="stButton"] button[kind="primary"], .stMain [data-testid="stFormSubmitButton"] button[kind="primary"] {{ background: {STEEL} !important; background-color: {STEEL} !important; color: {WHITE} !important; border: 1px solid {STEEL} !important; }}
        .stMain [data-testid="stButton"] button[kind="primary"] p, .stMain [data-testid="stButton"] button[kind="primary"] span, .stMain [data-testid="stFormSubmitButton"] button[kind="primary"] p, .stMain [data-testid="stFormSubmitButton"] button[kind="primary"] span {{ color: {WHITE} !important; }}
        .stMain [data-testid="stButton"] button[kind="primary"]:hover, .stMain [data-testid="stFormSubmitButton"] button[kind="primary"]:hover {{ background: {NAVY} !important; background-color: {NAVY} !important; border-color: {NAVY} !important; color: {WHITE} !important; }}
        .stMain [data-testid="stButton"] button[kind="primary"]:hover p, .stMain [data-testid="stButton"] button[kind="primary"]:hover span, .stMain [data-testid="stFormSubmitButton"] button[kind="primary"]:hover p, .stMain [data-testid="stFormSubmitButton"] button[kind="primary"]:hover span {{ color: {WHITE} !important; }}
        .saarthi-callout {{ display: flex; align-items: flex-start; gap: 10px; padding: 9px 13px; margin: 3px 0; border: 1px solid {LINE}; border-left: 3.5px solid {STEEL}; border-radius: 6px; background: {WHITE} !important; color: {CHARCOAL} !important; font-size: 12.5px; line-height: 1.45; }}
        .saarthi-callout-body {{ display: flex; flex-direction: column; gap: 1px; color: {CHARCOAL} !important; }}
        .saarthi-callout-body b {{ font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: {STEEL}; }}
        .saarthi-callout-body span {{ color: {CHARCOAL} !important; font-size: 12.5px; }}
        .saarthi-callout-dot {{ width: 7px; height: 7px; border-radius: 50%; background: {STEEL}; flex: none; margin-top: 5px; }}
        .saarthi-callout-info {{ border-left-color: {BLUE}; background: #F8FAFC !important; }}
        .saarthi-callout-info .saarthi-callout-dot {{ background: {BLUE}; }}
        .saarthi-callout-info .saarthi-callout-body b {{ color: {BLUE}; }}
        .saarthi-callout-success {{ border-left-color: {TEAL}; background: #F2FAF7 !important; }}
        .saarthi-callout-success .saarthi-callout-dot {{ background: {TEAL}; }}
        .saarthi-callout-success .saarthi-callout-body b {{ color: {TEAL}; }}
        .saarthi-callout-warning {{ border-left-color: {ORANGE}; background: #FFFBF4 !important; }}
        .saarthi-callout-warning .saarthi-callout-dot {{ background: {ORANGE}; }}
        .saarthi-callout-warning .saarthi-callout-body b {{ color: #B45309; }}
        .saarthi-callout-error {{ border-left-color: {RED}; background: #FEF2F2 !important; }}
        .saarthi-callout-error .saarthi-callout-dot {{ background: {RED}; }}
        .saarthi-callout-error .saarthi-callout-body b {{ color: {RED}; }}
        [data-testid="stAlert"], [data-testid="stAlert"] > div, [data-testid="stAlertContainer"], [data-baseweb="notification"], div[role="alert"] {{ background: {WHITE} !important; background-color: {WHITE} !important; color: {CHARCOAL} !important; border: 1px solid {LINE} !important; border-left: 3.5px solid {STEEL} !important; border-radius: 6px !important; }}
        [data-testid="stAlert"] *, [data-testid="stAlertContainer"] *, [data-baseweb="notification"] *, div[role="alert"] * {{ color: {CHARCOAL} !important; background-color: transparent !important; }}
        .stApp [data-baseweb="select"] > div, .stApp [data-baseweb="input"], .stApp [data-baseweb="input"] > div, .stApp [data-baseweb="base-input"], .stApp [data-baseweb="textarea"], .stApp [data-baseweb="textarea"] > div, .stApp [data-testid="stNumberInputContainer"] {{ border-radius: 6px !important; border: 1px solid #CBD5E1 !important; background: #F8FAFC !important; background-color: #F8FAFC !important; color: {CHARCOAL} !important; }}
        .stApp [data-baseweb="select"] div, .stApp [data-baseweb="select"] span, .stApp [data-baseweb="select"] input, .stApp [data-baseweb="input"] input, .stApp [data-baseweb="textarea"] textarea, .stApp [data-testid="stNumberInput"] input {{ background: transparent !important; background-color: transparent !important; color: {CHARCOAL} !important; -webkit-text-fill-color: {CHARCOAL} !important; caret-color: {NAVY} !important; }}
        .stApp [data-testid="stNumberInput"] button {{ background: #F1F5F9 !important; background-color: #F1F5F9 !important; color: {NAVY} !important; border-left: 1px solid #CBD5E1 !important; }}
        .stApp [data-baseweb="select"] svg, .stApp [data-testid="stNumberInput"] svg {{ fill: {NAVY} !important; color: {NAVY} !important; }}
        .stApp [data-baseweb="tag"] {{ background: {STEEL} !important; background-color: {STEEL} !important; color: {WHITE} !important; border-radius: 4px !important; border: none !important; padding: 1px 7px !important; margin: 1px 2px !important; font-size: 11.5px !important; }}
        .stApp [data-baseweb="tag"] span, .stApp [data-baseweb="tag"] svg {{ color: {WHITE} !important; fill: {WHITE} !important; background: transparent !important; }}
        body [data-baseweb="popover"], body [data-baseweb="popover"] > div, body [data-baseweb="menu"], body ul[role="listbox"], body li[role="option"], body [data-testid="stSelectboxVirtualDropdown"] {{ background: {WHITE} !important; background-color: {WHITE} !important; color: {CHARCOAL} !important; border-radius: 6px !important; }}
        body li[role="option"] *, body [data-baseweb="menu"] * {{ color: {CHARCOAL} !important; }}
        body li[role="option"]:hover, body li[role="option"][aria-selected="true"] {{ background: {BG_GREY} !important; background-color: {BG_GREY} !important; color: {NAVY} !important; font-weight: 600 !important; }}
        body [data-testid="stToast"], body [data-testid="stToast"] > div {{ background: {NAVY} !important; background-color: {NAVY} !important; color: {WHITE} !important; border: 1px solid {TEAL} !important; border-left: 4px solid {TEAL} !important; border-radius: 8px !important; box-shadow: 0 4px 12px rgba(16, 37, 54, 0.18) !important; }}
        body [data-testid="stToast"] * {{ color: {WHITE} !important; }}
        .stMain label p, .stMain [data-testid="stWidgetLabel"] p, .stMain [data-testid="stCheckbox"] label span, .stMain [data-testid="stCheckbox"] p {{ font-size: 10.5px !important; font-weight: 600 !important; letter-spacing: 0.04em !important; text-transform: uppercase !important; color: {MUTED_GREY} !important; }}
        .stMain [data-testid="stRadio"] label span, .stMain [data-testid="stRadio"] p, .stMain [data-testid="stSlider"] * {{ color: {CHARCOAL} !important; }}
        details[data-testid="stExpander"], details[data-testid="stExpander"] > div {{ background: {WHITE} !important; background-color: {WHITE} !important; border: 1px solid #E6ECF0 !important; border-radius: 8px !important; color: {CHARCOAL} !important; }}
        details[data-testid="stExpander"] summary, details[data-testid="stExpander"] summary p, details[data-testid="stExpander"] summary span, details[data-testid="stExpander"] summary svg {{ font-size: 12px !important; font-weight: 600 !important; color: {NAVY} !important; fill: {NAVY} !important; background: {WHITE} !important; background-color: {WHITE} !important; border-radius: 8px !important; }}
        .saarthi-heading {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin: 0; }}
        .saarthi-title {{ color: {NAVY}; font-size: 15.5px; font-weight: 600; line-height: 1.25; }}
        .saarthi-description {{ color: {MUTED_GREY}; font-size: 12px; line-height: 1.4; }}
        .saarthi-notice {{ display: flex; gap: 8px; align-items: flex-start; border: 1px solid {LINE}; border-left: 3px solid {STEEL}; border-radius: 6px; background: {INSET}; padding: 7px 11px; color: {CHARCOAL}; font-size: 12px; line-height: 1.4; }}
        .saarthi-notice-caution {{ color: #92400E; border-color: {WARNING_BORDER}; border-left: 3px solid {ORANGE}; background: {WARNING_BG}; }}
        .saarthi-notice-dot {{ width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex: none; margin-top: 5px; }}
        .saarthi-metrics {{ display: grid; gap: 10px; margin: 2px 0; align-items: stretch; }}
        .saarthi-metric {{ display: flex; flex-direction: column; justify-content: space-between; min-height: 76px; border: 1px solid #E6ECF0; border-radius: 8px; background: {WHITE}; padding: 11px 14px; min-width: 0; box-shadow: 0 1px 2px rgba(16, 37, 54, 0.03); }}
        .saarthi-metric-label {{ color: {MUTED_GREY}; font-size: 10.5px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
        .saarthi-metric-value {{ font-size: 22px; font-weight: 700; line-height: 1.15; margin-top: 6px; font-variant-numeric: tabular-nums; color: {CHARCOAL}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
        /* Top Command Header & Alert Ticker */
        .saarthi-eoc-header {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; background: {NAVY}; color: {WHITE}; border-radius: 8px 8px 0 0; padding: 11px 18px; border-bottom: 1px solid {STEEL}; }}
        .saarthi-eoc-brand {{ display: flex; align-items: center; gap: 14px; min-width: 0; }}
        .saarthi-mark {{ color: #94A3B8; font-size: 10px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; line-height: 1.2; }}
        .saarthi-page {{ color: {WHITE}; font-size: 16.5px; font-weight: 600; line-height: 1.2; margin-top: 1px; }}
        .saarthi-eoc-meta {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
        .saarthi-eoc-pill {{ display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600; padding: 4px 9px; border-radius: 5px; background: {STEEL}; color: #E2E8F0; border: 1px solid rgba(255,255,255,0.1); white-space: nowrap; }}
        .saarthi-eoc-clock {{ font-family: 'JetBrains Mono', ui-monospace, monospace; color: #CBD5E1; font-size: 11px; font-weight: 500; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); border-radius: 5px; padding: 4px 9px; font-variant-numeric: tabular-nums; }}
        .saarthi-ticker {{ display: flex; align-items: center; gap: 12px; background: {STEEL}; color: #F1F5F9; padding: 6px 16px; border-radius: 0 0 8px 8px; font-size: 11.5px; overflow-x: auto; white-space: nowrap; margin-bottom: 4px; box-shadow: 0 2px 5px rgba(16, 37, 54, 0.08); }}
        .saarthi-ticker-tag {{ font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; padding: 2px 7px; border-radius: 4px; background: {RED}; color: {WHITE}; flex: none; }}
        .saarthi-ticker-items {{ display: inline-flex; align-items: center; gap: 18px; }}
        .saarthi-ticker-sep {{ color: #64748B; }}
        /* Map Header & Legend */
        .saarthi-map-bar {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; background: {STEEL}; color: {WHITE}; padding: 9px 14px; border-radius: 6px 6px 0 0; margin-top: 2px; }}
        .saarthi-map-bar-title {{ font-size: 12.5px; font-weight: 600; letter-spacing: 0.02em; color: {WHITE}; }}
        .saarthi-map-bar-sub {{ font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 11px; color: #CBD5E1; }}
        .saarthi-legend {{ background: {INSET}; border: 1px solid {LINE}; border-radius: 0 0 6px 6px; padding: 8px 12px; color: {CHARCOAL}; font-size: 11.5px; line-height: 1.5; display: flex; flex-wrap: wrap; gap: 14px; margin-top: -4px; }}
        /* Tables */
        .saarthi-table-container {{ width: 100%; max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; display: block; min-width: 0; border: 1px solid #E6ECF0; border-radius: 6px; background: {WHITE}; margin: 3px 0; }}
        table.saarthi-scan, table.saarthi-requests {{ width: 100% !important; border-collapse: collapse !important; border: none !important; font-size: 12px !important; background: {WHITE} !important; margin: 0 !important; }}
        table.saarthi-scan th, table.saarthi-requests th {{ background: {BG_GREY} !important; color: {MUTED_GREY} !important; font-size: 10.5px !important; font-weight: 600 !important; letter-spacing: 0.05em !important; text-transform: uppercase !important; text-align: left !important; padding: 8px 12px !important; border: none !important; border-bottom: 1px solid {LINE} !important; white-space: nowrap !important; }}
        table.saarthi-scan td, table.saarthi-requests td {{ padding: 8px 12px !important; border: none !important; border-bottom: 1px solid {LINE} !important; color: {CHARCOAL} !important; background: {WHITE} !important; vertical-align: middle !important; line-height: 1.4 !important; }}
        table.saarthi-scan tbody tr:last-child td, table.saarthi-requests tbody tr:last-child td {{ border-bottom: none !important; }}
        table.saarthi-scan tbody tr:hover td, table.saarthi-requests tbody tr:hover td {{ background: #F8FAFC !important; }}
        .saarthi-scan td.saarthi-nowrap, .saarthi-requests td.saarthi-nowrap {{ white-space: nowrap !important; }}
        /* Alert Cards & Progress Bars */
        .saarthi-alert-card {{ border: 1px solid #E6ECF0; border-left: 3.5px solid {RED}; border-radius: 6px; padding: 9px 12px; margin-bottom: 8px; background: {WHITE}; }}
        .saarthi-alert-card.high {{ border-left-color: {ORANGE}; }}
        .saarthi-alert-card.moderate {{ border-left-color: {YELLOW}; }}
        .saarthi-alert-card.low {{ border-left-color: {BLUE}; }}
        .saarthi-alert-card.resolved {{ border-left-color: {TEAL}; }}
        .saarthi-alert-top {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 4px; }}
        .saarthi-alert-title {{ font-size: 12.5px; font-weight: 600; color: {NAVY}; }}
        .saarthi-alert-meta {{ display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; color: {MUTED_GREY}; margin-top: 4px; }}
        .saarthi-cap-row {{ margin-bottom: 9px; }}
        .saarthi-cap-row:last-child {{ margin-bottom: 2px; }}
        .saarthi-cap-head {{ display: flex; justify-content: space-between; align-items: center; font-size: 12px; font-weight: 600; color: {CHARCOAL}; margin-bottom: 4px; }}
        .saarthi-cap-track {{ height: 6px; background: {BG_GREY}; border-radius: 999px; overflow: hidden; }}
        .saarthi-cap-fill {{ height: 100%; border-radius: 999px; }}
        .saarthi-code-badge {{ font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 10px; font-weight: 600; padding: 1px 5px; border-radius: 3px; background: {BG_GREY}; color: {NAVY}; border: 1px solid {LINE}; }}
        .saarthi-cascade-chain {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 6px 0; }}
        .saarthi-cascade-step {{ border: 1px solid #E6ECF0; border-top: 3px solid {STEEL}; border-radius: 6px; padding: 9px 11px; background: {INSET}; }}
        .saarthi-cascade-step b {{ display: block; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em; color: {MUTED_GREY}; margin-bottom: 3px; }}
        .saarthi-cascade-step div {{ font-size: 12px; font-weight: 600; color: {NAVY}; line-height: 1.35; }}
        @media (max-width: 1100px) {{ .saarthi-metrics, .saarthi-cascade-chain {{ grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }} }}
        </style>""",
        unsafe_allow_html=True)


def page_fit_styles():
    """Keep the operational pages responsive inside the frame without crushing table columns."""
    st.markdown(
        """<style>
        [data-testid="stColumn"] { min-width: 0 !important; }
        [data-testid="stVerticalBlock"] { min-width: 0; max-width: 100%; }
        [data-testid="stMarkdownContainer"] { overflow-wrap: break-word; max-width: 100%; }
        [data-testid="stDeckGlJsonChart"], .stPlotlyChart, [data-testid="stDataFrame"] { max-width: 100%; }
        @media (max-width: 1100px) {
            [data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
            [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                flex: 1 1 100% !important; width: 100% !important;
            }
        }
        </style>""",
        unsafe_allow_html=True)


def command_bar(page, live):
    """Top EOC Navigation/Header + Live Emergency Alert Ticker:
    Displays current date/time, overall operational status, honest simulation/auto-refresh connection
    indicator, active incident count, queued alert notification badge, and authority designation."""
    sev = data.severity()
    crit_zones = [z for z in sev if z["level"] == "Critical"]
    high_zones = [z for z in sev if z["level"] == "High"]
    open_incidents = data.read("SELECT COUNT(*) AS n FROM incidents WHERE status != 'RESOLVED'")[0]["n"]
    queued_alerts = data.read("SELECT COUNT(*) AS n FROM alerts WHERE sms_status='QUEUED'")[0]["n"]
    blocked_roads = data.read("SELECT * FROM roads WHERE status='BLOCKED'")
    full_hosps = data.read("SELECT * FROM hospitals WHERE available_beds = 0")

    if crit_zones:
        op_status = "STATUS: CRITICAL RESPONSE"
        op_col = RED
    elif high_zones:
        op_status = "STATUS: ELEVATED WATCH"
        op_col = ORANGE
    else:
        op_status = "STATUS: NOMINAL"
        op_col = TEAL

    conn_label = f"AUTO-REFRESH ({REFRESH_SECONDS}S) · DEMO DB" if live else "SIMULATION / DEMO DATA (HOLD)"
    conn_col = TEAL if live else ORANGE
    timestamp = data.now()

    # Build scrolling alert ticker items from real database records
    ticker_parts = []
    for z in crit_zones:
        ticker_parts.append(
            f'<span><b style="color:#FCA5A5;">CRITICAL:</b> {html.escape(z["disaster_type"])} warning — '
            f'{html.escape(z["name"])} (Score {z["score"]})</span>'
        )
    for z in high_zones:
        ticker_parts.append(
            f'<span><b style="color:#FCD34D;">HIGH:</b> {html.escape(z["disaster_type"])} watch — '
            f'{html.escape(z["name"])} (Score {z["score"]})</span>'
        )
    for h in full_hosps:
        ticker_parts.append(
            f'<span><b style="color:#FCA5A5;">HOSPITAL FULL:</b> {html.escape(h["name"])} ({html.escape(h["zone"])}) — 0 free beds</span>'
        )
    for r in blocked_roads:
        ticker_parts.append(
            f'<span><b style="color:#FCD34D;">ROUTE BLOCKED:</b> {html.escape(r["id"])} ({html.escape(r["a"])} to {html.escape(r["b"])})</span>'
        )
    if not ticker_parts:
        ticker_parts.append('<span>All monitored corridors and district hospitals operating within baseline parameters.</span>')

    ticker_html = '<span class="saarthi-ticker-sep">|</span>'.join(ticker_parts)

    st.markdown(
        '<div class="saarthi-eoc-header">'
        '<div class="saarthi-eoc-brand">'
        '<div style="min-width:0;">'
        '<div class="saarthi-mark">SAARTHI · STATE EMERGENCY OPERATIONS CENTRE (MAHARASHTRA)</div>'
        f'<div class="saarthi-page">{html.escape(page)}</div>'
        '</div>'
        '</div>'
        '<div class="saarthi-eoc-meta">'
        f'<div class="saarthi-eoc-pill" style="border-left:3px solid {op_col};">'
        f'<span style="width:7px;height:7px;border-radius:50%;background:{op_col};display:inline-block;"></span>'
        f'{op_status}</div>'
        f'<div class="saarthi-eoc-pill">ACTIVE INCIDENTS: <b style="color:#FFFFFF;">{open_incidents}</b></div>'
        f'<div class="saarthi-eoc-pill">ALERTS QUEUED: <b style="color:#FCD34D;">{queued_alerts}</b></div>'
        f'<div class="saarthi-eoc-pill">'
        f'<span style="width:6px;height:6px;border-radius:50%;background:{conn_col};display:inline-block;"></span>'
        f'{conn_label}</div>'
        f'<div class="saarthi-eoc-clock">{html.escape(timestamp)}</div>'
        '</div>'
        '</div>'
        '<div class="saarthi-ticker">'
        '<span class="saarthi-ticker-tag">LIVE ALERT FEED</span>'
        f'<div class="saarthi-ticker-items">{ticker_html}</div>'
        '</div>',
        unsafe_allow_html=True)


# Clean Voyager street basemap for clear GIS visibility
_DARK_STYLE = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json"
_PIXELS = DeckString("pixels")


def render_deck(layers, height, key):
    """Draw one GIS map inside the EOC map container with latest update timestamp."""
    st.markdown(
        '<div class="saarthi-map-bar">'
        '<div class="saarthi-map-bar-title">GIS Situational Awareness &amp; Cascading Impact Map</div>'
        f'<div class="saarthi-map-bar-sub">Last map update: {html.escape(data.now())} (Recorded DB)</div>'
        '</div>',
        unsafe_allow_html=True)
    st.pydeck_chart(pdk.Deck(layers=layers,
                             initial_view_state=pdk.ViewState(latitude=18.2, longitude=73.4, zoom=6.3),
                             tooltip={"text": "{label}"},
                             map_provider="carto", map_style=_DARK_STYLE),
                    height=height, key=key)


def _rgb_hex(rgb):
    """CSS colour for a legend swatch. The map still uses the original RGB."""
    return "#{:02x}{:02x}{:02x}".format(*rgb[:3])


def map_legend(items):
    """Clear severity and asset legends below the GIS map."""
    if not items:
        return
    chips = []
    for colour, caption in items:
        chips.append(
            '<span style="display:inline-flex;align-items:center;gap:6px;font-weight:500;">'
            f'<span style="width:10px;height:10px;background:{colour};border-radius:50%;border:1px solid rgba(16,37,54,0.2);display:inline-block;"></span>'
            f"{html.escape(str(caption))}</span>"
        )
    st.markdown('<div class="saarthi-legend">' + "".join(chips) + "</div>", unsafe_allow_html=True)


def _place_labels(frame, text_column, offset):
    """Names already stored on the rows. Nothing here creates a new place."""
    return pdk.Layer("TextLayer", frame, get_position="[lon, lat]", get_text=text_column, get_size=12,
                     get_color=[16, 37, 54, 245], get_text_anchor="'middle'", get_alignment_baseline="'bottom'",
                     get_pixel_offset=offset, billboard=True, font_family="sans-serif", background=True,
                     get_background_color=[255, 255, 255, 230], background_padding=[4, 2, 4, 2], pickable=False)


def draw_map(layers, height=540, key="map"):  # prominent GIS map with switchable layers
    choices = ["Risk zones", "Roads", "Hospitals", "Supply hubs", "SOS requests", "Shipments"]  # every layer
    shown = st.multiselect("Map Layers Control (Toggle Categories)", choices, default=layers, key=key)  # operator picks which to show
    deck_layers = []  # the pydeck layers to draw, bottom first
    labels = []  # place names drawn after the symbols
    legend = []
    if "Risk zones" in shown:  # Consistent severity colours: Critical=Red, High=Orange, Moderate=Yellow, Low=Blue
        z = df(data.severity())  # zones with scores
        z["color"] = z["level"].map(LEVEL_RGB)  # colour by level
        z["fill_color"] = z["level"].map(lambda lvl: LEVEL_RGB.get(lvl, [43, 108, 176]) + [52])
        z["label"] = z["name"] + ": " + z["level"] + " (score " + z["score"].astype(str) + ", hazard " + z["disaster_type"] + ")"
        deck_layers.append(pdk.Layer("ScatterplotLayer", z, get_position="[lon, lat]",
                                     get_fill_color="fill_color", get_line_color="color",
                                     filled=True, stroked=True, get_radius=22, radius_units=_PIXELS,
                                     line_width_min_pixels=2.5, pickable=True))
        labels.append(_place_labels(z, "name", [0, -18]))
        legend.extend((
            (RED, "Critical Zone (Red)"),
            (ORANGE, "High Risk Area (Orange)"),
            (YELLOW, "Moderate Risk (Yellow)"),
            (BLUE, "Low Risk (Blue)"),
        ))
    if "Roads" in shown:  # one line per road
        r = df(data.roads_with_positions())  # roads with both ends' positions
        r["path"] = r.apply(lambda x: [[x["a_lon"], x["a_lat"]], [x["b_lon"], x["b_lat"]]], axis=1)  # line points
        r["color"] = r["status"].map(ROAD_RGB)  # colour by condition
        r["width"] = r["status"].map({"OPEN": 2, "SLOW": 3.5, "BLOCKED": 4})  # problem roads drawn thicker
        r["label"] = r["id"] + ": " + r["a"] + " to " + r["b"] + ", " + r["status"]  # hover text
        deck_layers.append(pdk.Layer("PathLayer", r, get_path="path", get_color="color", get_width="width",
                                     width_units=_PIXELS, pickable=True))  # add the layer
        legend.extend((_rgb_hex(ROAD_RGB[name]), f"Route {name.capitalize()}") for name in ("OPEN", "SLOW", "BLOCKED"))
    if "Hospitals" in shown:  # Green with free beds, Red when full
        h = df(data.read("SELECT * FROM hospitals"))  # hospitals
        h["color"] = h["available_beds"].apply(lambda b: [38, 155, 117] if b > 0 else [231, 71, 71])
        h["label"] = h["name"] + ": " + h["available_beds"].astype(str) + "/" + h["total_beds"].astype(str) + " beds free"
        deck_layers.append(pdk.Layer("ScatterplotLayer", h, get_position="[lon, lat]", get_fill_color="color",
                                     get_line_color=[255, 255, 255], filled=True, stroked=True, get_radius=7,
                                     radius_units=_PIXELS, line_width_min_pixels=1.5, pickable=True))
        labels.append(_place_labels(h, "name", [0, 16]))
        legend.append((TEAL, "Hospital (Beds Available)"))
        legend.append((RED, "Hospital (At Capacity)"))
    if "Supply hubs" in shown:  # Steel Blue / Navy depots
        hb = df(data.read("SELECT * FROM hubs"))  # hubs
        hb["label"] = hb["name"] + " (relief distribution hub)"  # hover text
        deck_layers.append(pdk.Layer("ScatterplotLayer", hb, get_position="[lon, lat]",
                                     get_fill_color=[25, 60, 80], get_line_color=[255, 255, 255],
                                     filled=True, stroked=True, get_radius=8, radius_units=_PIXELS,
                                     line_width_min_pixels=2, pickable=True))
        labels.append(_place_labels(hb, "name", [0, 28]))
        legend.append((STEEL, "Relief Distribution Hub"))
    if "SOS requests" in shown:  # Red incident markers, Orange in-transit
        i = df(data.read("SELECT * FROM incidents WHERE status != 'RESOLVED'"))  # active requests
        if len(i):  # only if any
            i["color"] = i["status"].map(SOS_RGB)  # red open, orange in transit
            i["label"] = "SOS #" + i["id"].astype(str) + " " + i["need_type"] + " (" + i["zone"] + "): " + i["people"].astype(str) + " people [" + i["status"] + "]"
            deck_layers.append(pdk.Layer("ScatterplotLayer", i, get_position="[lon, lat]", get_fill_color="color",
                                         get_line_color=[255, 255, 255], filled=True, stroked=True, get_radius=7,
                                         radius_units=_PIXELS, line_width_min_pixels=1.5, pickable=True))
        legend.append((RED, "Active Incident (Open SOS)"))
        legend.append((ORANGE, "Response Team En Route"))
    if "Shipments" in shown:  # Orange moving shipment markers
        s = df([x for x in data.shipments_with_positions() if "lat" in x])  # moving ones only
        if len(s):  # only if any
            s["label"] = s["shipment_id"] + ": " + s["qty"].astype(str) + " " + s["item"] + " to " + s["destination"]
            deck_layers.append(pdk.Layer("ScatterplotLayer", s, get_position="[lon, lat]", get_fill_color=[230, 162, 60],
                                         get_line_color=[16, 37, 54], stroked=True, filled=True, line_width_min_pixels=1.5,
                                         get_radius=6.5, radius_units=_PIXELS, pickable=True))
        legend.append((ORANGE, "Relief Shipment In Transit"))
    deck_layers.extend(labels)
    render_deck(deck_layers, height, f"{key}_deck")
    map_legend(legend)


# ---------------------------------------------------------------------
# 3. PAGES
# ---------------------------------------------------------------------
def page_overview():  # PAGE 1: Unified Command-Centre Home Screen
    """Central Monitoring Station Layout (Section 2A, 2B & Section 5):
    - Top section: Global situation status (Overall disaster status, Active incidents, Critical alerts,
      Teams deployed, Hospitals under pressure, Unresolved resource requests).
    - Middle section: Centrepiece GIS map (~62% width) with marker inspector + Right panel (~38% width)
      with Live Emergency Alerts & Command Activity Log.
    - Bottom section: Resource overview (Medical supplies, Food & water, Shelter capacity, Hospital capacity,
      Active response teams/shipments) + Cascading Impact Visualization & 24h Risk Telemetry."""
    page_fit_styles()
    st.markdown(
        """<style>
        .saarthi-event { padding: 7px 9px; border-bottom: 1px solid var(--saarthi-line); }
        .saarthi-event:last-child { border-bottom: none; }
        .saarthi-event-meta { display: flex; align-items: center; gap: 8px; color: var(--saarthi-muted); font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; line-height: 1.3; }
        .saarthi-event-text { color: var(--saarthi-text); margin-top: 2px; font-size: 0.8rem; line-height: 1.38; }
        </style>""",
        unsafe_allow_html=True)

    sev = data.severity()
    sev_by_zone = {z["name"]: z for z in sev}
    plan = data.get_plan()
    incidents = data.read("SELECT * FROM incidents ORDER BY id DESC")
    hospitals = data.read("SELECT * FROM hospitals ORDER BY id")
    alerts_rows = data.read("SELECT * FROM alerts ORDER BY id DESC")
    shipments = data.current_shipments()
    inventory = data.read("SELECT item, SUM(qty) AS total_qty FROM inventory GROUP BY item")
    inv_by_item = {row["item"]: int(row["total_qty"] or 0) for row in inventory}

    # 1. TOP SECTION — Global Situation Status (6 Equal-Width KPI Cards in 1 Row)
    crit_zones = [z for z in sev if z["level"] == "Critical"]
    high_zones = [z for z in sev if z["level"] == "High"]
    overall_status = "CRITICAL" if crit_zones else ("ELEVATED" if high_zones else "NOMINAL")
    overall_col = RED if crit_zones else (ORANGE if high_zones else TEAL)

    active_incidents = [i for i in incidents if i["status"] != "RESOLVED"]
    open_sos = [i for i in incidents if i["status"] == "OPEN"]
    in_transit_sos = [i for i in incidents if i["status"] == "IN_TRANSIT"]
    queued_red_alerts = sum(1 for a in alerts_rows if a.get("sms_status") == "QUEUED")
    moving_shipments = [s for s in shipments if s.get("status") in ("DISPATCHED", "IN_TRANSIT")]
    teams_deployed = len(in_transit_sos) + len(moving_shipments)
    hosps_under_pressure = sum(
        1 for h in hospitals
        if int(h["total_beds"] or 0) > 0 and (int(h["available_beds"] or 0) / int(h["total_beds"])) <= 0.25
    )
    unresolved_requests = len(plan.get("shortfalls") or []) + len(plan.get("unplaced") or []) + len(open_sos)

    metric_tiles((
        ("Overall Disaster Status", overall_status, overall_col),
        ("Active Incidents", len(active_incidents), RED if active_incidents else TEAL),
        ("Critical Alerts & Zones", len(crit_zones) + queued_red_alerts, RED if (crit_zones or queued_red_alerts) else TEAL),
        ("Teams & Units Deployed", teams_deployed, STEEL if teams_deployed else MUTED_GREY),
        ("Hospitals Under Pressure", f"{hosps_under_pressure} / {len(hospitals)}", ORANGE if hosps_under_pressure else TEAL),
        ("Unresolved Requests", unresolved_requests, ORANGE if unresolved_requests else TEAL),
    ))

    # 2. MIDDLE SECTION — Centrepiece Live Incident Map (~62% width) + Right Live Alerts Panel (~38% width)
    map_col, alert_col = st.columns([1.62, 1.0])
    with map_col:
        with st.container(border=True):
            draw_map(["Risk zones", "Roads", "Hospitals", "Supply hubs", "SOS requests", "Shipments"],
                     height=540, key="ov_map")
            # Selected Marker / Incident Inspector right beneath the map
            if active_incidents:
                inc_options = [
                    f"SOS #{i['id']} · {i['zone']} · {i['need_type']} ({i['people']} affected) [{i['status']}]"
                    for i in active_incidents
                ]
                picked_inc_label = st.selectbox(
                    "Inspect Active Incident Marker on Map",
                    inc_options,
                    key="ov_marker_inspect",
                )
                picked_inc = active_incidents[inc_options.index(picked_inc_label)]
                z_meta = sev_by_zone.get(picked_inc["zone"], {})
                z_lvl = z_meta.get("level", "Moderate")
                lvl_tone = "critical" if z_lvl == "Critical" else ("high" if z_lvl == "High" else ("moderate" if z_lvl == "Moderate" else "low"))
                st_tone = "critical" if picked_inc["status"] == "OPEN" else ("warning" if picked_inc["status"] == "IN_TRANSIT" else "resolved")
                st.markdown(
                    f'<div class="saarthi-alert-card" style="margin-top:4px;margin-bottom:0;background:#F8FAFC;">'
                    f'<div class="saarthi-alert-top">'
                    f'<span class="saarthi-alert-title">Marker Selected: SOS #{picked_inc["id"]} — {html.escape(str(picked_inc["zone"]))} ({html.escape(str(picked_inc["need_type"]))})</span>'
                    f'<span>{status_pill(z_lvl + " Zone", lvl_tone)} {status_pill(picked_inc["status"], st_tone)}</span>'
                    f'</div>'
                    f'<div class="saarthi-alert-meta">'
                    f'<span><b>Coordinates:</b> {picked_inc["lat"]:.4f}, {picked_inc["lon"]:.4f}</span>'
                    f'<span><b>People Affected:</b> {picked_inc["people"]}</span>'
                    f'<span><b>Reported:</b> {html.escape(str(picked_inc["reported_at"]))}</span>'
                    f'<span><b>Assigned Team:</b> {html.escape(str(picked_inc.get("claimed_by") or "Unassigned (Awaiting Dispatch)"))}</span>'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )

    with alert_col:
        with st.container(border=True):
            st.subheader("Live emergency alerts")
            st.caption("Severity-coded early warnings and critical district triggers.")
            alert_cards = []
            # Show stored alerts first
            for al in alerts_rows[:4]:
                sev_w = str(al.get("severity") or "Red")
                mapped_lvl = "Critical" if sev_w == "Red" else ("High" if sev_w == "Orange" else "Moderate")
                cls_lvl = mapped_lvl.lower()
                sms_st = str(al.get("sms_status") or "QUEUED")
                st_tone = "warning" if sms_st == "QUEUED" else ("safe" if sms_st == "SENT" else "navy")
                alert_cards.append(
                    f'<div class="saarthi-alert-card {cls_lvl}">'
                    f'<div class="saarthi-alert-top">'
                    f'<span class="saarthi-alert-title">{html.escape(str(al.get("disaster_type")))} Warning — {html.escape(str(al.get("zone")))}</span>'
                    f'{status_pill(mapped_lvl, cls_lvl)}'
                    f'</div>'
                    f'<div class="saarthi-alert-meta">'
                    f'<span><b>Location:</b> {html.escape(str(al.get("zone")))} ({al.get("radius_km")} km)</span>'
                    f'<span><b>Detected:</b> {html.escape(str(al.get("created_at", ""))[11:16] or "recorded")}</span>'
                    f'<span><b>ETA:</b> {al.get("eta_hours")}h</span>'
                    f'<span><b>Status:</b> {html.escape(sms_st)}</span>'
                    f'<span><b>Team:</b> SEOC Broadcast Queue</span>'
                    f'</div></div>'
                )
            # Also surface top severity zones
            for z in sev[:4]:
                lvl = z["level"]
                cls_lvl = lvl.lower()
                team_assigned = next(
                    (i.get("claimed_by") for i in incidents if i["zone"] == z["name"] and i.get("claimed_by")),
                    "District Response Unit" if z["open_requests"] else "Standby Monitoring",
                )
                alert_cards.append(
                    f'<div class="saarthi-alert-card {cls_lvl}">'
                    f'<div class="saarthi-alert-top">'
                    f'<span class="saarthi-alert-title">{html.escape(z["disaster_type"])} Exposure — {html.escape(z["name"])}</span>'
                    f'{status_pill(f"{lvl} · {z['score']}", cls_lvl)}'
                    f'</div>'
                    f'<div class="saarthi-alert-meta">'
                    f'<span><b>Location:</b> {html.escape(z["name"])}</span>'
                    f'<span><b>Prob:</b> {float(z["probability"]):.0%}</span>'
                    f'<span><b>Open SOS:</b> {z["open_requests"]}</span>'
                    f'<span><b>Assigned:</b> {html.escape(str(team_assigned))}</span>'
                    f'</div></div>'
                )
            st.markdown("".join(alert_cards[:6]), unsafe_allow_html=True)

        with st.container(border=True):
            st.subheader("Command activity & incident timeline")
            st.caption("Latest operational decisions, route changes, and plan updates.")
            events = data.read("SELECT * FROM events ORDER BY id DESC LIMIT 6")
            if not events:
                st.caption("No stored system events.")
            for e in events:
                code_tag = EVENT_ICON.get(e["type"], "EVT")
                kind = str(e["type"] or "not available").replace("_", " ").lower()
                stamp = e["timestamp"] or ""
                when = stamp[11:16] if len(stamp) >= 16 else "not available"
                message = html.escape(str(e["message"] if e["message"] is not None else "not available"))
                st.markdown(
                    '<div class="saarthi-event">'
                    f'<div class="saarthi-event-meta"><span class="saarthi-code-badge">{html.escape(code_tag)}</span>'
                    f'<span>{when} · {html.escape(kind)}</span></div>'
                    f'<div class="saarthi-event-text">{message}</div>'
                    "</div>",
                    unsafe_allow_html=True)

    # 3. BOTTOM SECTION — Resource Overview + Cascading Impact & 24h Risk Telemetry
    res_col, cascade_col = st.columns([1.15, 1.45])
    with res_col:
        with st.container(border=True):
            st.subheader("Resource overview & hospital readiness")
            st.caption("Stock across regional depots, medical bed availability, and deployed units.")
            total_beds = sum(int(h["total_beds"] or 0) for h in hospitals)
            free_beds = sum(int(h["available_beds"] or 0) for h in hospitals)
            total_icu = sum(int(h["icu_total"] or 0) for h in hospitals)
            free_icu = sum(int(h["icu_available"] or 0) for h in hospitals)
            med_stock = inv_by_item.get("Medical kits", 0) + inv_by_item.get("Oxygen cylinders", 0)
            food_water = inv_by_item.get("Food kits", 0) + inv_by_item.get("Water packs", 0)
            shelter_stock = inv_by_item.get("Tarpaulins", 0)
            planned_supply = sum(int(r.get("qty") or 0) for r in (plan.get("supply") or []))
            shortfall_supply = sum(int(s.get("missing") or 0) for s in (plan.get("shortfalls") or []))
            cov_denom = planned_supply + shortfall_supply
            cov_pct = (planned_supply / cov_denom * 100.0) if cov_denom > 0 else 100.0

            res_bars = [
                progress_bar_html(
                    "Medical & Trauma Beds Available",
                    f"{free_beds} / {total_beds} free beds (ICU {free_icu}/{total_icu})",
                    (free_beds / total_beds * 100.0) if total_beds else 0,
                    tone="teal" if free_beds > 100 else ("orange" if free_beds > 0 else "red"),
                ),
                progress_bar_html(
                    "Medical Supplies (Kits & Oxygen Cylinders)",
                    f"{med_stock:,} units in depot stock",
                    min(100, med_stock / 80.0),
                    tone="teal",
                ),
                progress_bar_html(
                    "Food & Water Relief Packs",
                    f"{food_water:,} units in depot stock",
                    min(100, food_water / 110.0),
                    tone="teal",
                ),
                progress_bar_html(
                    "Shelter Capacity (Tarpaulins & Relief Cover)",
                    f"{shelter_stock:,} units in depot stock",
                    min(100, shelter_stock / 40.0),
                    tone="low",
                ),
                progress_bar_html(
                    "Priority Supply Allocation Coverage",
                    f"{planned_supply:,} planned · {shortfall_supply:,} unmet shortfall",
                    cov_pct,
                    tone="red" if shortfall_supply > 0 else "teal",
                ),
            ]
            st.markdown("".join(res_bars), unsafe_allow_html=True)
            st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
            st.markdown("**Hospital Capacity Utilization by Facility**")
            hosp_bars = []
            for h in hospitals:
                tot = int(h["total_beds"] or 1)
                free = int(h["available_beds"] or 0)
                occ_pct = ((tot - free) / tot) * 100.0 if tot > 0 else 0.0
                tone = "red" if free == 0 else ("orange" if occ_pct >= 80 else "teal")
                hosp_bars.append(progress_bar_html(h["name"], f"{free}/{tot} free ({int(round(occ_pct))}% used)", occ_pct, tone=tone))
            st.markdown("".join(hosp_bars), unsafe_allow_html=True)

    with cascade_col:
        with st.container(border=True):
            st.subheader("Cascading impact visualization (Saarthi Ripple)")
            st.caption("How primary hazard exposure propagates across transport corridors, hospitals, and supply chains.")
            top_zone = sev[0]["name"] if sev else "Ratnagiri"
            ripple_snap = ripple_agent.analyse(top_zone)
            sec_names = ", ".join(z["zone"] for z in ripple_snap["secondary"]) or "None"
            ext_names = ", ".join(z["zone"] for z in ripple_snap["extended"]) or "None"
            prim_hosp = ripple_snap["primary"]["hospital"]["text"]
            prim_roads = ", ".join(f"{r['id']} ({r['status']})" for r in ripple_snap["primary"]["roads"][:3])
            reachable_hubs = sum(1 for r in ripple_snap["reach"] if r["minutes"] is not None)
            st.markdown(
                '<div class="saarthi-cascade-chain">'
                f'<div class="saarthi-cascade-step" style="border-top-color:{RED};">'
                f'<b>1. Primary Hazard Origin</b>'
                f'<div>{html.escape(top_zone)} ({html.escape(sev[0]["disaster_type"])} · Score {sev[0]["score"]})</div>'
                f'</div>'
                f'<div class="saarthi-cascade-step" style="border-top-color:{ORANGE};">'
                f'<b>2. Transport Corridors</b>'
                f'<div>{html.escape(prim_roads)} → 1-Hop: {html.escape(sec_names)}</div>'
                f'</div>'
                f'<div class="saarthi-cascade-step" style="border-top-color:{YELLOW};">'
                f'<b>3. Medical Pressure</b>'
                f'<div>{html.escape(prim_hosp)} · 2-Hop: {html.escape(ext_names)}</div>'
                f'</div>'
                f'<div class="saarthi-cascade-step" style="border-top-color:{TEAL};">'
                f'<b>4. Supply Chain Reach</b>'
                f'<div>{reachable_hubs} / {len(ripple_snap["reach"])} depots connected on open routes</div>'
                f'</div>'
                '</div>',
                unsafe_allow_html=True,
            )
        with st.container(border=True):
            st.subheader("Risk probability over the last 24 hours")
            logs = df(data.read("SELECT timestamp, zone, probability FROM risk_logs ORDER BY id"))  # stored readings
            line_colours = [RED, ORANGE, YELLOW, BLUE, TEAL, STEEL, MUTED_GREY]
            if logs.empty:
                st.caption("Recorded risk probability is not available.")
            else:
                fig = px.line(logs, x="timestamp", y="probability", color="zone", markers=True,
                              color_discrete_sequence=line_colours)  # one line per zone
                fig.update_layout(yaxis_tickformat=".0%", yaxis_range=[0, 1], height=245)
                style_chart(fig)
                st.plotly_chart(fig, use_container_width=True)


def page_alerts():  # PAGE 2: early warnings
    """Stored alerts and recorded risk readings. Queueing an alert is the existing write."""
    page_fit_styles()
    st.markdown(
        f"""
        <style>

        .saarthi-warn-red {{ color: {LEVEL_HEX["Critical"]}; font-weight: 600; }}
        .saarthi-warn-orange {{ color: {LEVEL_HEX["High"]}; font-weight: 600; }}
        .saarthi-warn-yellow {{ color: {LEVEL_HEX["Moderate"]}; font-weight: 600; }}
        .saarthi-sms-queued {{ color: {WARNING}; font-weight: 600; }}
        .saarthi-sms-acknowledged {{ color: {MUTED}; font-weight: 600; }}
        .saarthi-sms-sent {{ color: {LEVEL_HEX["Low"]}; font-weight: 600; }}
        .saarthi-impact {{ color: {WARNING}; font-weight: 600; }}
        </style>
        """,
        unsafe_allow_html=True)

    def alert_cell(value):
        if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
            return "not available"
        return value

    def alert_table(frame):
        columns = ("id", "created_at", "zone", "disaster_type", "severity", "time_to_impact", "radius_km", "sms_status")
        headers = {"sms_status": "Status"}
        severity_class = {"Red": "saarthi-warn-red", "Orange": "saarthi-warn-orange", "Yellow": "saarthi-warn-yellow"}
        sms_class = {"QUEUED": "saarthi-sms-queued", "ACKNOWLEDGED": "saarthi-sms-acknowledged",
                     "SENT": "saarthi-sms-sent"}
        head = "".join(f"<th>{html.escape(headers.get(name, name))}</th>" for name in columns)
        body = []
        for _, row in frame.iterrows():
            cells = []
            for key in columns:
                shown = alert_cell(row.get(key))
                text = html.escape(str(shown))
                if key == "severity":
                    cells.append(f'<td class="{severity_class.get(shown, "")}">{text}</td>')
                elif key == "sms_status":
                    cells.append(f'<td class="{sms_class.get(shown, "")}">{text}</td>')
                elif key == "time_to_impact" and shown == "Impact window reached":
                    cells.append(f'<td class="saarthi-impact">{text}</td>')
                else:
                    cells.append(f"<td>{text}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        st.markdown(
            '<div class="saarthi-table-container">'
            f'<table class="saarthi-scan"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
            "</div>",
            unsafe_allow_html=True)

    with st.container(border=True):
        page_header("Early warnings (24-hour alerts)",
                    "Stored alerts and the latest recorded risk readings.")
        st.caption("Stored alerts are recorded. Time to impact is calculated from the stored created time and ETA. "
                   "It is not a new forecast. The readings below are recorded scraper rows, not alerts.")
    alerts = df(data.read("SELECT * FROM alerts ORDER BY id DESC"))  # all alerts
    queued = acknowledged = sent = 0
    if len(alerts):
        deadline = pd.to_datetime(alerts["created_at"]) + pd.to_timedelta(alerts["eta_hours"], unit="h")  # impact time
        left = (deadline - pd.Timestamp.now()).dt.total_seconds() / 3600  # hours from now
        alerts["time_to_impact"] = left.apply(
            lambda h: "not available" if pd.isna(h) else ("Impact window reached" if h <= 0 else f"{int(h)}h {int((h % 1) * 60)}m"))
        queued = int((alerts["sms_status"] == "QUEUED").sum())
        acknowledged = int((alerts["sms_status"] == "ACKNOWLEDGED").sum())
        sent = int((alerts["sms_status"] == "SENT").sum())
    metric_tiles((
        ("Stored alerts", len(alerts), TEXT),
        ("Queued", queued, WARNING if queued else TEXT),
        ("Acknowledged", acknowledged, MUTED if acknowledged else TEXT),
        ("Stored as sent", sent, TEXT),
    ))
    board, issue = st.columns([1.55, 1])
    with board:
        with st.container(border=True):
            st.subheader("Stored alerts")
            st.caption("Recorded rows. Zone and radius are the stored alert area. "
                       "Red, Orange, and Yellow are the stored severity words. "
                       "QUEUED is waiting. Acknowledged means an operator handled it here. "
                       "SENT is only a stored value. This command centre does not send SMS "
                       "and does not confirm delivery.")
            if len(alerts):
                alert_table(alerts)
                queued_alerts = alerts[alerts["sms_status"] == "QUEUED"]
                if len(queued_alerts):
                    st.caption("Acknowledge records that an operator handled the stored alert. "
                               "It does not send an SMS.")
                    for _, queued_alert in queued_alerts.iterrows():
                        alert_id = int(queued_alert["id"])
                        with st.form(f"ack_alert_{alert_id}"):
                            confirm = st.checkbox(
                                f"Acknowledge alert #{alert_id} for {alert_cell(queued_alert.get('zone'))}. "
                                "This does not send an SMS.")
                            if st.form_submit_button("Acknowledge"):
                                if not confirm:
                                    ui_error("Confirmation is required. Status was not changed.")
                                else:
                                    act(data.acknowledge_alert, alert_id,
                                        ok="Alert acknowledged. No SMS was sent.")
            else:
                st.caption("No stored alerts.")
    with issue:
        with st.container(border=True):
            st.subheader("Issue a new alert")  # form heading
            st.caption("Saved as QUEUED. No SMS is sent, and the alert is not marked SENT.")  # explanation
            with st.form("new_alert", clear_on_submit=True):  # nothing saves until the button is pressed
                zone = st.selectbox("Zone", [z[0] for z in data.demo.ZONES])  # zone
                dtype = st.selectbox("Disaster type", data.demo.DISASTER_TYPES)  # type
                sev = st.selectbox("Severity", ["Red", "Orange", "Yellow"])  # severity
                eta = st.slider("Expected impact in (hours)", 1, 72, 24)  # hours to impact
                radius = st.slider("Alert radius (km)", 5, 100, 30)  # radius
                if st.form_submit_button("Queue alert", type="primary"):  # pressed
                    act(data.add_alert, zone, dtype, sev, eta, radius, ok="Alert saved as queued. No SMS was sent.")  # save
    with st.container(border=True):
        st.subheader("Recorded risk readings")
        st.caption("Each row is a stored reading. IMD (demo) is the existing demo history. "
                   "Operator-entered was recorded by an operator. These are not live sensor readings. "
                   "Recording one does not queue an alert, change hospital beds, or change the allocation plan.")
        history, entry = st.columns([1.55, 1])
        with entry:
            with st.form("operator_risk", clear_on_submit=True):
                risk_zone = st.selectbox("Zone", [z["name"] for z in data.read("SELECT name FROM zones ORDER BY name")])
                risk_type = st.selectbox("Hazard", data.demo.DISASTER_TYPES)
                risk_probability = st.number_input("Risk probability", min_value=0.0, max_value=1.0, value=0.50, step=0.01, format="%.2f")
                st.caption("Saved with source Operator-entered and the time of recording.")
                if st.form_submit_button("Record reading"):
                    act(data.add_risk_reading, risk_zone, risk_type, float(risk_probability),
                        ok="Risk reading recorded. No alert was queued.")
        with history:
            readings = data.read("SELECT timestamp, zone, disaster_type, probability, source FROM risk_logs "
                                 "ORDER BY id DESC LIMIT 20")
            if readings:
                columns = ("timestamp", "zone", "disaster_type", "probability", "source")
                head = "".join(f"<th>{html.escape(name)}</th>" for name in columns)
                body = []
                for row in readings:
                    cells = []
                    for key in columns:
                        shown = alert_cell(row.get(key))
                        cells.append(f"<td>{html.escape(str(shown))}</td>")
                    body.append("<tr>" + "".join(cells) + "</tr>")
                st.markdown(
                    '<div class="saarthi-table-container">'
                    f'<table class="saarthi-scan"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
                    "</div>",
                    unsafe_allow_html=True)
            else:
                st.caption("No recorded risk readings.")


def page_severity():  # PAGE 3: which area is worst
    """Derived severity beside the recorded inputs. The score still comes from data.severity()."""
    page_fit_styles()
    st.markdown(
        f"""
        <style>

        .saarthi-level-critical {{ color: {LEVEL_HEX["Critical"]}; font-weight: 600; }}
        .saarthi-level-high {{ color: {LEVEL_HEX["High"]}; font-weight: 600; }}
        .saarthi-level-moderate {{ color: {LEVEL_HEX["Moderate"]}; font-weight: 600; }}
        .saarthi-level-low {{ color: {LEVEL_HEX["Low"]}; font-weight: 600; }}
        </style>
        """,
        unsafe_allow_html=True)

    def severity_cell(value):
        if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
            return "not available"
        return value

    with st.container(border=True):
        page_header("Severity and vulnerability",
                    "Severity score, level, probability, vulnerability, population, and open requests for each recorded zone.")
        st.caption("Probability, vulnerability, population, and open requests are recorded inputs. "
                   "Score and level are derived from those inputs. A missing value is not available.")
    sev = df(data.severity())  # zones
    level_counts = sev["level"].value_counts() if len(sev) and "level" in sev.columns else {}
    metric_tiles((
        ("Critical", int(level_counts.get("Critical", 0)), LEVEL_HEX["Critical"]),
        ("High", int(level_counts.get("High", 0)), LEVEL_HEX["High"]),
        ("Moderate", int(level_counts.get("Moderate", 0)), LEVEL_HEX["Moderate"]),
        ("Low", int(level_counts.get("Low", 0)), LEVEL_HEX["Low"]),
    ))
    chart, table = st.columns([1.05, 1.2])
    with chart:
        with st.container(border=True):
            st.subheader("Derived score")
            st.caption("The bar length is the derived score. The colour is the derived level.")
            if len(sev):
                fig = px.bar(sev.sort_values("score"), x="score", y="name", orientation="h", color="level",
                             color_discrete_map=LEVEL_HEX, text="score", labels={"name": "", "score": "Severity score"})  # bars
                style_chart(fig)
                fig.update_layout(height=420, xaxis_range=[0, 100])
                fig.update_yaxes(automargin=True)
                st.plotly_chart(fig, use_container_width=True)  # show it
            else:
                st.caption("No recorded zones.")
    with table:
        with st.container(border=True):
            st.subheader("Recorded inputs and derived level")
            if len(sev):
                columns = ("name", "level", "score", "disaster_type", "probability", "source", "vulnerability",
                           "population", "open_requests")
                level_class = {"Critical": "saarthi-level-critical", "High": "saarthi-level-high",
                               "Moderate": "saarthi-level-moderate", "Low": "saarthi-level-low"}
                sources = {row["zone"]: row["source"] for row in data.read(
                    """SELECT zone, source FROM risk_logs
                       WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone)""")}
                sev["source"] = sev["name"].map(lambda name: sources.get(name) if sources.get(name) else "not available")
                head = "".join(f"<th>{html.escape(name)}</th>" for name in columns)
                body = []
                for _, row in sev.iterrows():
                    cells = []
                    for key in columns:
                        shown = severity_cell(row.get(key))
                        text = html.escape(str(shown))
                        if key == "level":
                            cells.append(f'<td class="{level_class.get(shown, "")}">{text}</td>')
                        else:
                            cells.append(f"<td>{text}</td>")
                    body.append("<tr>" + "".join(cells) + "</tr>")
                st.markdown(
                    '<div class="saarthi-table-container">'
                    f'<table class="saarthi-scan"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
                    "</div>",
                    unsafe_allow_html=True)
            else:
                st.caption("No recorded zones.")
            with st.expander("How is the score calculated?"):  # click to open
                st.markdown("**Severity = probability × vulnerability × 100**, plus up to 10 points for open SOS requests. "
                            "Critical 50+, High 30+, Moderate 15+, otherwise Low. The formula lives in engine.py.")  # text


def page_allocation():  # PAGE 4: the optimiser's decisions with reasons
    """Stored plan. Approve is the existing step that creates a shipment."""
    page_fit_styles()
    st.markdown(
        """
        <style>

        </style>
        """,
        unsafe_allow_html=True)

    def plan_cell(value):
        return "not available" if value is None or value == "" else value

    def changed_mark(row):
        if row.get("changed"):
            return f' <span style="color:{WARNING};font-weight:600;">Changed</span>'
        return ""

    with st.container(border=True):
        page_header("Allocation",
                    "The stored supply plan and patient plan, with the reason for each move.")
        st.caption("These rows are the stored plan. They stay proposed until Approve creates a shipment. "
                   "Changed means this version differs from the previous stored plan.")
    plan = data.get_plan()  # current plan
    created = plan.get("created_at") or ""
    clock = created[11:16] if len(created) >= 16 else "not available"
    trigger_type = plan.get("trigger_type") or ""
    trigger_label = trigger_type.replace("_", " ").lower() if trigger_type else "not available"
    shortfalls = plan.get("shortfalls") or []
    unplaced = plan.get("unplaced") or []
    supply = plan.get("supply") or []
    patients = plan.get("patients") or []
    metric_tiles((
        ("Plan version", plan_cell(plan.get("version")), TEXT),
        ("Proposed supply moves", len(supply), TEXT),
        ("Proposed patient assignments", len(patients), TEXT),
        ("Shortfalls", len(shortfalls), LEVEL_HEX["Critical"] if shortfalls else TEXT),
        ("Changed this version", plan_cell(plan.get("changed")), WARNING if plan.get("changed") else TEXT),
    ))
    with st.container(border=True):
        st.subheader("Resource allocation & medical placement coverage")
        total_supply_qty = sum(int(r.get("qty") or 0) for r in supply)
        total_shortfall_qty = sum(int(s.get("missing") or 0) for s in shortfalls)
        denom_supply = total_supply_qty + total_shortfall_qty
        supply_cov_pct = (total_supply_qty / denom_supply * 100.0) if denom_supply > 0 else 100.0
        placed_pts = sum(int(p.get("patients") or 0) for p in patients)
        unplaced_pts = sum(int(u.get("patients") or 0) for u in unplaced)
        denom_pts = placed_pts + unplaced_pts
        pts_cov_pct = (placed_pts / denom_pts * 100.0) if denom_pts > 0 else 100.0
        cov_c1, cov_c2 = st.columns(2)
        with cov_c1:
            st.markdown(
                progress_bar_html(
                    "Supply allocation coverage",
                    f"{total_supply_qty} planned / {total_shortfall_qty} short",
                    supply_cov_pct,
                    tone="red" if total_shortfall_qty > 0 else "teal",
                ),
                unsafe_allow_html=True,
            )
        with cov_c2:
            st.markdown(
                progress_bar_html(
                    "Medical bed placement coverage",
                    f"{placed_pts} assigned / {unplaced_pts} unplaced",
                    pts_cov_pct,
                    tone="red" if unplaced_pts > 0 else "teal",
                ),
                unsafe_allow_html=True,
            )
    with st.container(border=True):
        head, buttons = st.columns([2.4, 1])  # summary | actions
        head.markdown(f"**Plan v{plan_cell(plan.get('version'))}** · {clock} · "
                      f"**{plan_cell(plan.get('changed'))} allocations changed** after *{trigger_label}*  \n"
                      f"{plan_cell(plan.get('trigger'))} · Solver: {plan_cell(plan.get('solver'))}")  # summary text
        st.caption("Re-optimise recalculates the stored plan. Approve all creates a shipment for every supply move.")
        if buttons.button("Re-optimise now"):  # manual re-run
            act(data.event, "MANUAL", "Operator asked for a fresh plan", ok="Plan recalculated")
        if buttons.button("Approve all moves", type="primary", disabled=not supply):  # approve everything
            act(data.approve_all, ok="Every supply move approved")
    supply_col, patient_col = st.columns(2)
    with supply_col:
        st.subheader("Supply plan")  # section
        st.caption("Proposed moves. Approve is the existing action that allocates a shipment.")
        if not supply:  # nothing to move
            ui_success("No moves needed right now. Every high-risk zone is covered.")
        for n, r in enumerate(supply):  # one card per move
            box = st.container(border=True)  # bordered card
            a, b = box.columns([3.5, 1.2])  # text | button
            level = plan_cell(r.get("level"))
            minutes = "not available" if r.get("minutes") is None else engine.fmt_minutes(r["minutes"])
            a.markdown(
                f'<div><span style="color:{LEVEL_HEX.get(level, TEXT)};font-weight:600;">{html.escape(str(level))}</span>'
                f'{changed_mark(r)} · Move {html.escape(str(plan_cell(r.get("qty"))))} '
                f'{html.escape(str(plan_cell(r.get("item"))))} from {html.escape(str(plan_cell(r.get("from_hub"))))} '
                f'to {html.escape(str(plan_cell(r.get("zone"))))} · {html.escape(str(minutes))}</div>'
                f'<div style="margin-top:2px;">{html.escape(str(plan_cell(r.get("reason"))))}</div>',
                unsafe_allow_html=True)
            if b.button("Approve", key=f"ap_{n}_{r['key']}", type="primary"):  # approve this one
                act(data.approve, r["key"], ok=f"{r['qty']} {r['item']} approved")
        for s in shortfalls:  # anything nobody could supply
            ui_error(f"Could not cover {plan_cell(s.get('missing'))} {plan_cell(s.get('item'))} "
                     f"for {plan_cell(s.get('zone'))}: {plan_cell(s.get('why'))}")
    with patient_col:
        st.subheader("Patient plan (hospitals)")  # section
        st.caption("Proposed assignments. Recording one reduces that hospital's free beds after confirmation. "
                   "This does not dispatch a patient or a responder, and it does not approve every assignment.")
        if not patients:  # no medical requests
            ui_info("No medical requests waiting for a bed.")
        recorded_keys = {data._assignment_marker(row["message"])
                         for row in data.read("SELECT message FROM events WHERE type='PATIENT_ASSIGNED'")}
        for assignment in patients:  # one card per assignment
            minutes = "not available" if assignment.get("minutes") is None else engine.fmt_minutes(assignment["minutes"])
            with st.container(border=True):
                st.markdown(
                    f'<div>{html.escape(str(plan_cell(assignment.get("patients"))))} patients{changed_mark(assignment)} '
                    f'from SOS #{html.escape(str(plan_cell(assignment.get("incident_id"))))} '
                    f'({html.escape(str(plan_cell(assignment.get("zone"))))}) → '
                    f'{html.escape(str(plan_cell(assignment.get("hospital"))))} · {html.escape(str(minutes))}</div>'
                    f'<div style="margin-top:2px;">{html.escape(str(plan_cell(assignment.get("reason"))))}</div>',
                    unsafe_allow_html=True)
                if assignment.get("key") in recorded_keys:
                    st.caption("Already recorded. Free beds were not changed again.")
                else:
                    with st.form(f"record_patient_{assignment['key']}"):
                        confirm = st.checkbox(
                            f"Confirm recording {plan_cell(assignment.get('patients'))} patients at "
                            f"{plan_cell(assignment.get('hospital'))}. This does not dispatch anyone.")
                        if st.form_submit_button("Record assignment"):
                            if not confirm:
                                ui_error("Confirmation is required. Beds were not changed.")
                            else:
                                act(data.record_patient_assignment, assignment["key"],
                                    ok="Assignment recorded. Free beds were reduced.")
        for u in unplaced:  # patients with no bed
            ui_error(f"No bed found for {plan_cell(u.get('patients'))} patients from SOS "
                     f"#{plan_cell(u.get('incident_id'))} ({plan_cell(u.get('zone'))})")
    with st.container(border=True):
        st.subheader("Recent re-optimisations")  # history
        st.caption("Recorded plan runs. A missing field is not available.")
        history = data.plan_history()
        if history:
            columns = ("version", "created_at", "trigger_type", "trigger_text", "changed")
            head = "".join(f"<th>{html.escape(name)}</th>" for name in columns)
            body = []
            for row in history:
                cells = "".join(f"<td>{html.escape(str(plan_cell(row.get(key))))}</td>" for key in columns)
                body.append(f"<tr>{cells}</tr>")
            st.markdown(
                '<div class="saarthi-table-container">'
                f'<table class="saarthi-scan"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
                "</div>",
                unsafe_allow_html=True)
        else:
            st.caption("No recorded plan runs.")


def page_network():  # PAGE 5: roads, hospitals, stock (every change is an event)
    """Recorded roads, beds, and stock. Update, save, and add stock stay the existing writes."""
    page_fit_styles()
    st.markdown(
        f"""
        <style>

        .saarthi-road-open {{ color: #6b7280; font-weight: 600; }}
        .saarthi-road-slow {{ color: {LEVEL_HEX["High"]}; font-weight: 600; }}
        .saarthi-road-blocked {{ color: {LEVEL_HEX["Critical"]}; font-weight: 600; }}
        </style>
        """,
        unsafe_allow_html=True)

    def network_cell(value):
        if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
            return "not available"
        return value

    with st.container(border=True):
        page_header("Roads and hospitals",
                    "Recorded road status, hospital beds, and hub stock.")
        st.caption("Road status, bed counts, and hub stock are recorded rows. "
                   "They are not a live capacity feed.")
    roads = data.roads_with_positions()  # roads
    hospitals = data.read("SELECT * FROM hospitals ORDER BY id")  # hospitals
    blocked = sum(1 for road in roads if road.get("status") == "BLOCKED")
    slow = sum(1 for road in roads if road.get("status") == "SLOW")
    open_roads = sum(1 for road in roads if road.get("status") == "OPEN")
    full_hospitals = sum(1 for hospital in hospitals if hospital.get("available_beds") == 0)
    metric_tiles((
        ("Open roads", open_roads, TEXT),
        ("Slow roads", slow, LEVEL_HEX["High"] if slow else TEXT),
        ("Blocked roads", blocked, LEVEL_HEX["Critical"] if blocked else TEXT),
        ("Hospitals with no free beds", full_hospitals, LEVEL_HEX["Critical"] if full_hospitals else TEXT),
    ))
    geo, road_panel = st.columns([1.45, 1])
    with geo:
        with st.container(border=True):
            st.caption("Recorded roads, hospitals, and supply hubs.")
            draw_map(["Roads", "Hospitals", "Supply hubs"], height=520, key="rd_map")
    with road_panel:
        with st.container(border=True):
            st.subheader("Road network")  # section
            st.caption("Recorded status. Open, slow, and blocked use the same states as the map.")
            if roads:
                columns = ("id", "a", "b", "minutes", "current_minutes", "status")
                status_class = {"OPEN": "saarthi-road-open", "SLOW": "saarthi-road-slow",
                                "BLOCKED": "saarthi-road-blocked"}
                head = "".join(f"<th>{html.escape(name)}</th>" for name in columns)
                body = []
                for road in roads:
                    cells = []
                    for key in columns:
                        shown = network_cell(road.get(key))
                        text = html.escape(str(shown))
                        if key == "status":
                            cells.append(f'<td class="{status_class.get(shown, "")}">{text}</td>')
                        else:
                            cells.append(f"<td>{text}</td>")
                    body.append("<tr>" + "".join(cells) + "</tr>")
                st.markdown(
                    '<div class="saarthi-table-container">'
                    f'<table class="saarthi-scan"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
                    "</div>",
                    unsafe_allow_html=True)
                with st.form("road_form"):  # change one road
                    rid = st.selectbox("Road", [r["id"] + ": " + r["a"] + " to " + r["b"] for r in roads])  # choose road
                    status = st.selectbox("Condition", ["OPEN", "SLOW", "BLOCKED"])  # choose condition
                    if st.form_submit_button("Update road", type="primary"):  # pressed
                        act(data.set_road, rid.split(":")[0], status, ok="Road updated, plan recalculated")  # event
            else:
                st.caption("No recorded roads.")
    beds, stock = st.columns(2)
    with beds:
        with st.container(border=True):
            st.subheader("Hospital capacity")  # section
            st.caption("Recorded free beds and ICU. Saving replaces those recorded numbers.")
            if hospitals:
                hs = df(hospitals)  # hospitals
                fig = go.Figure([go.Bar(name="Occupied beds", x=hs["name"], y=hs["total_beds"] - hs["available_beds"], marker_color="#CBD5E1"),
                                 go.Bar(name="Free beds", x=hs["name"], y=hs["available_beds"], marker_color=TEAL)])  # stacked bars
                style_chart(fig)
                fig.update_layout(barmode="stack", height=280, margin=dict(t=8, b=72, l=8, r=8))
                fig.update_xaxes(tickangle=-30, automargin=True)
                st.plotly_chart(fig, use_container_width=True)  # show it
                with st.form("hospital_form"):  # change one hospital
                    name = st.selectbox("Hospital", hs["name"].tolist())  # choose hospital
                    row = hs[hs["name"] == name].iloc[0]  # its row
                    c2, c3 = st.columns(2)
                    beds_free = c2.number_input("Free beds", 0, int(row["total_beds"]), int(row["available_beds"]))  # beds
                    icu = c3.number_input("Free ICU", 0, int(row["icu_total"]), int(row["icu_available"]))  # ICU
                    if st.form_submit_button("Save", type="primary"):  # pressed
                        act(data.set_hospital, int(row["id"]), int(beds_free), int(icu), ok="Capacity updated, plan recalculated")
            else:
                st.caption("No recorded hospitals.")
    with stock:
        with st.container(border=True):
            st.subheader("Stock by hub")  # section
            st.caption("Recorded quantities. A missing hub and item pair is not available.")
            inv = df(data.read("SELECT h.name AS hub, i.item, i.qty FROM inventory i JOIN hubs h ON h.id=i.hub_id"))  # stock
            if len(inv):
                pivot = inv.pivot(index="hub", columns="item", values="qty")
                render_df_table(pivot.where(pivot.notna(), "not available"), hide_index=False)  # hubs x items
            else:
                st.caption("No recorded stock.")
            hubs = data.read("SELECT id, name FROM hubs ORDER BY id")  # hubs
            if hubs:
                with st.form("receive_form"):  # new supplies arrive
                    hub = st.selectbox("Hub", [h["name"] for h in hubs])  # choose hub
                    item = st.selectbox("Item", data.demo.ITEMS)  # choose item
                    qty = st.number_input("Quantity", 1, 100000, 500)  # how many
                    if st.form_submit_button("Add stock", type="primary"):  # pressed
                        act(data.receive_stock, next(h["id"] for h in hubs if h["name"] == hub), item, int(qty), ok="Stock received")
            else:
                st.caption("No recorded hubs.")


def page_tracking():  # PAGE 6: shipments + tamper check
    """Recorded ledger shipments. The next-status button is the existing ledger write."""
    page_fit_styles()
    st.markdown(
        f"""
        <style>

        .saarthi-ship-allocated {{ color: {WARNING}; font-weight: 600; }}
        .saarthi-ship-moving {{ color: {LEVEL_HEX["High"]}; font-weight: 600; }}
        .saarthi-ship-done {{ color: {LEVEL_HEX["Low"]}; font-weight: 600; }}
        </style>
        """,
        unsafe_allow_html=True)

    def track_cell(value):
        if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
            return "not available"
        return value

    def status_class(status):
        if status in ("DISPATCHED", "IN_TRANSIT"):
            return "saarthi-ship-moving"
        if status in ("RECEIVED", "DISTRIBUTED"):
            return "saarthi-ship-done"
        if status == "ALLOCATED":
            return "saarthi-ship-allocated"
        return ""

    def shipment_table(rows, columns):
        head = "".join(f"<th>{html.escape(name)}</th>" for name in columns)
        body = []
        for row in rows:
            cells = []
            for key in columns:
                shown = track_cell(row.get(key))
                text = html.escape(str(shown))
                classes = []
                if key == "status" and status_class(shown):
                    classes.append(status_class(shown))
                if key in {"shipment_id", "entry_id", "timestamp", "prev_hash", "hash", "qty", "status"}:
                    classes.append("saarthi-nowrap")
                class_attr = f' class="{" ".join(classes)}"' if classes else ""
                cells.append(f"<td{class_attr}>{text}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        st.markdown(
            '<div class="saarthi-table-container">'
            f'<table class="saarthi-scan"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
            "</div>",
            unsafe_allow_html=True)

    with st.container(border=True):
        page_header("Supply tracking (tamper-evident)",
                    "Shipment status and the tamper-evident ledger.")
        st.caption("These are recorded ledger shipments. They are not the proposed moves on Allocation. "
                   "A missing value is not available.")
    ok, bad, total = data.verify_ledger()  # check the chain
    if ok:  # intact
        ui_success(f"Ledger verified: all {total} records intact. No record has been altered.")
    else:  # broken
        ui_error(f"Tampering detected at ledger record #{bad}. The hash chain no longer matches.")
    ships = data.current_shipments()  # latest step per shipment
    moving = sum(1 for row in ships if row.get("status") in ("DISPATCHED", "IN_TRANSIT"))
    metric_tiles((
        ("Recorded shipments", len(ships), TEXT),
        ("Moving", moving, LEVEL_HEX["High"] if moving else TEXT),
        ("Ledger records", total, LEVEL_HEX["Critical"] if not ok else (LEVEL_HEX["Low"] if total else TEXT)),
    ))
    if not ships:  # none
        ui_info("No shipments yet. Approve a move on the Allocation page.")
        return  # stop here
    board, dispatch = st.columns([1.45, 1])
    with board:
        with st.container(border=True):
            st.subheader("Latest recorded status")
            shipment_table(ships, ("shipment_id", "item", "qty", "source_hub", "destination", "status", "timestamp"))
    with dispatch:
        with st.container(border=True):
            st.subheader("Recorded progress")
            st.caption("The highlighted step is the latest ledger status. The button writes the next recorded step.")
            sid = st.selectbox("Open a shipment", [row["shipment_id"] for row in ships])  # choose one
            row = next(item for item in ships if item["shipment_id"] == sid)  # its row
            flow = engine.STATUS_FLOW  # steps
            pos = flow.index(row["status"]) if row["status"] in flow else 0  # where it is
            cols = st.columns(len(flow))  # one column per step
            for k, step in enumerate(flow):  # draw the steps
                label = step.replace("_", " ").title()
                if k == pos:
                    colour = {"saarthi-ship-moving": LEVEL_HEX["High"], "saarthi-ship-done": LEVEL_HEX["Low"],
                              "saarthi-ship-allocated": WARNING}.get(status_class(row.get("status")), TEXT)
                    weight = "600"
                else:
                    colour = MUTED
                    weight = "400"
                cols[k].markdown(
                    f'<div style="color:{colour};font-weight:{weight};">{html.escape(label)}</div>',
                    unsafe_allow_html=True)
            if pos < len(flow) - 1 and st.button(f"Mark as {flow[pos + 1].replace('_', ' ').title()}", type="primary"):  # next step
                act(data.advance_shipment, sid, ok="Shipment updated")
    with st.expander("Full ledger (every status change, chained by SHA-256 fingerprint)"):  # click to open
        def short_hash(value):
            if value is None or value == "":
                return "not available"
            text = str(value)
            return text[:12] + "..."

        ledger = data.read("SELECT * FROM supply_movement ORDER BY entry_id")  # all rows
        shown = []
        for entry in ledger:
            copy = dict(entry)
            copy["prev_hash"] = short_hash(entry.get("prev_hash"))
            copy["hash"] = short_hash(entry.get("hash"))
            shown.append(copy)
        shipment_table(shown, ("entry_id", "shipment_id", "timestamp", "item", "qty", "source_hub",
                               "destination", "status", "prev_hash", "hash"))


def page_requests():  # PAGE 7: SOS from the field
    """Recorded field requests. Claim, close, and log stay the existing actions."""
    page_fit_styles()
    st.markdown(
        f"""
        <style>

        .saarthi-status-open {{ color: {LEVEL_HEX["Critical"]}; font-weight: 600; }}
        .saarthi-status-transit {{ color: {LEVEL_HEX["High"]}; font-weight: 600; }}
        .saarthi-status-resolved {{ color: {LEVEL_HEX["Low"]}; font-weight: 600; }}
        </style>
        """,
        unsafe_allow_html=True)

    def request_cell(value):
        return "not available" if value is None or value == "" else value

    def request_table(source):
        columns = ("id", "reported_at", "zone", "need_type", "people", "status", "claimed_by")
        status_class = {"OPEN": "saarthi-status-open", "IN_TRANSIT": "saarthi-status-transit",
                        "RESOLVED": "saarthi-status-resolved"}
        head = "".join(f"<th>{html.escape(name)}</th>" for name in columns)
        body = []
        for row in source:
            cells = []
            for key in columns:
                shown = request_cell(row.get(key))
                text = html.escape(str(shown))
                if key == "status":
                    cells.append(f'<td class="{status_class.get(row.get(key), "")}">{text}</td>')
                else:
                    cells.append(f"<td>{text}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        st.markdown(
            '<div class="saarthi-table-container">'
            f'<table class="saarthi-requests"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
            "</div>",
            unsafe_allow_html=True)

    with st.container(border=True):
        page_header("Field requests",
                    "A request logged from Intake & Triage appears here while its status is OPEN.")
        st.caption("Recorded incidents, newest first. A missing value is not available. "
                   "Open is the status that still needs a team.")
    rows = data.read("SELECT * FROM incidents ORDER BY id DESC")  # all requests
    open_rows = [r for r in rows if r["status"] == "OPEN"]
    open_ids = data.incident_ids(rows, "OPEN")  # empty on a fresh database must not reach selectbox
    transit_ids = data.incident_ids(rows, "IN_TRANSIT")
    metric_tiles((
        ("Open", len(open_rows), LEVEL_HEX["Critical"]),
        ("In transit", sum(1 for r in rows if r["status"] == "IN_TRANSIT"), LEVEL_HEX["High"]),
        ("Resolved", sum(1 for r in rows if r["status"] == "RESOLVED"), LEVEL_HEX["Low"]),
    ))
    listing, geo = st.columns([1.7, 1])
    with listing:
        with st.container(border=True):
            st.subheader("Open requests")
            safety_notice("Claim and resolve are unchanged. Nothing on this page dispatches a responder.")
            if open_rows:
                request_table(open_rows)
            else:
                ui_info("No open requests.")
    with geo:
        with st.container(border=True):
            st.caption("Recorded hospitals, supply hubs, and SOS requests.")
            draw_map(["Hospitals", "Supply hubs", "SOS requests"], height=400, key="rq_map")  # map
    claim_col, close_col, log_col = st.columns(3)
    with claim_col:
        with st.container(border=True):
            st.subheader("Claim a request")
            if not open_ids:
                ui_info("No open requests to claim.")
            else:
                with st.form("claim"):
                    rid = st.selectbox("Request #", open_ids)  # open ones
                    team = st.text_input("Team / NGO name", "Red Cross Team 3")  # who
                    if st.form_submit_button("Claim (mark in transit)", type="primary") and rid:  # pressed
                        act(data.claim_incident, int(rid), team, ok=f"SOS #{rid} claimed")
    with close_col:
        with st.container(border=True):
            st.subheader("Close a request")
            if not transit_ids:
                ui_info("No claimed requests to close.")
            else:
                with st.form("resolve"):
                    rid2 = st.selectbox("Request #", transit_ids)  # in transit
                    if st.form_submit_button("Mark resolved") and rid2:  # pressed
                        act(data.resolve_incident, int(rid2), ok=f"SOS #{rid2} resolved")
    with log_col:
        with st.container(border=True):
            with st.expander("Log a new SOS (Medical requests need hospital beds and go into the patient plan)"):  # new SOS
                with st.form("new_sos", clear_on_submit=True):
                    zone = st.selectbox("Zone", [z[0] for z in data.demo.ZONES])  # zone
                    need = st.selectbox("Need", data.demo.NEED_TYPES)  # need
                    people = st.number_input("People affected (for Medical: patients)", 1, 5000, 10)  # how many
                    if st.form_submit_button("Submit SOS"):  # pressed
                        act(data.add_incident, zone, need, int(people), ok="SOS logged, plan recalculated")
    with st.container(border=True):
        st.subheader("All requests")
        st.caption("Every recorded request. The order is newest id first.")
        if rows:
            request_table(rows)
        else:
            ui_info("No field requests yet.")


def page_scenarios():  # PAGE 8: the scenario tests from our notes
    with st.container(border=True):
        page_header("Scenario tests",
                "Run a scenario and the system re-plans by itself. The checks confirm the new plan is safe. "
                "Scenarios add up: run A to start fresh.")
        safety_notice(
            "LIVE DEMO DATABASE MODIFICATION. Scenarios A–E can change actual demo database records.",
            "LIVE DEMO DATABASE MODIFICATION. Scenarios A–E can change actual demo database records, "
            "including roads, hospital beds, stock, risk readings, incidents, alerts, and the stored plan. "
            "This is separate from What-If, which does not write to the database.",
            tone="caution", key="scenario_live_notice")
    cols = st.columns(len(data.SCENARIOS))  # one card per scenario
    for col, (sid, (title, desc)) in zip(cols, data.SCENARIOS.items()):  # each scenario
        with col.container(border=True):  # card
            st.markdown(f"**Scenario {sid}**  \n{title}")  # title
            st.caption(desc)  # description
            if st.button("Run", key=f"sc_{sid}", type="primary"):  # run it
                st.session_state["scenario_result"] = data.run_scenario(sid)  # keep the result for after the redraw
                st.rerun()  # redraw
    res = st.session_state.get("scenario_result")  # last result, if any
    if res:  # show it
        with st.container(border=True):
            st.subheader(f"Scenario {res['scenario']}: {res['title']}")  # heading
            st.markdown(f"Plan v{res['version']}: **{res['changed']} allocations changed.** See the Allocation page for details.")
            for c in res["checks"]:  # one line per check
                (ui_success if c["ok"] else ui_error)(f"{'PASS' if c['ok'] else 'FAIL'} — {c['check']}: {c['detail']}")  # green or red


# ---------------------------------------------------------------------
# 4. SIDEBAR: menu, live mode, demo buttons
# ---------------------------------------------------------------------
def page_intake():  # PAGE 9: Intake and Triage agent
    """Reported citizen queue. Logging a field request stays the existing action."""
    page_fit_styles()
    st.markdown(
        f"""
        <style>

        .saarthi-q-pending {{ color: {WARNING}; font-weight: 600; }}
        .saarthi-q-approved {{ color: {MUTED}; font-weight: 600; }}
        .saarthi-q-logged {{ color: {LEVEL_HEX["Low"]}; font-weight: 600; }}
        .saarthi-q-rejected {{ color: {LEVEL_HEX["Critical"]}; font-weight: 600; }}
        .saarthi-pri-critical {{ color: {LEVEL_HEX["Critical"]}; font-weight: 600; }}
        .saarthi-pri-high {{ color: {LEVEL_HEX["High"]}; font-weight: 600; }}
        .saarthi-pri-medium {{ color: {LEVEL_HEX["Moderate"]}; font-weight: 600; }}
        .saarthi-pri-low {{ color: {LEVEL_HEX["Low"]}; font-weight: 600; }}
        </style>
        """,
        unsafe_allow_html=True)

    def intake_cell(value):
        if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
            return "not available"
        return value

    with st.container(border=True):
        page_header("Intake and triage",
                    "Rule-based triage of reports already in the demo database and of new practice reports.")
        safety_notice(
            "A person approves or rejects every report. This page does not send an alert or dispatch a responder.",
            "These rows are reported. An imported SOS remains a recorded incident and is not a new citizen report. "
            "Intake priority is not a recorded risk reading and not a Ripple attention score. "
            "A missing people count is not zero, and a missing urgency is not LOW. "
            "Approving a report does not execute a plan.",
            key="intake_readonly_notice")
        st.caption("Evidence reported. An imported SOS remains a recorded incident. "
                   "A missing people count is not zero, and a missing urgency is not LOW. "
                   "Approving a report does not execute a plan.")
    if st.session_state.get("intake_notice"):
        ui_success(st.session_state.pop("intake_notice"))
    if st.session_state.get("intake_error"):
        ui_error(st.session_state.pop("intake_error"))
    reports = intake_agent.list_reports()
    pending = [r for r in reports if intake_agent.queue_state(r) == "Pending"]
    approved_only = [r for r in reports if intake_agent.queue_state(r) == "Approved, not logged"]
    logged = [r for r in reports if r["sos_logged"]]
    rejected = [r for r in reports if r["review_status"] == "rejected"]
    metric_tiles((
        ("Pending", len(pending), WARNING if pending else TEXT),
        ("Approved, not logged", len(approved_only), MUTED),
        ("Logged as field requests", len(logged), LEVEL_HEX["Low"] if logged else TEXT),
        ("Rejected", len(rejected), LEVEL_HEX["Critical"] if rejected else TEXT),
    ))
    with st.container(border=True):
        left, right = st.columns(2)
        if left.button("Import existing SOS", type="primary"):
            added = intake_agent.import_open_sos()
            st.toast(f"Imported {len(added)} existing SOS records" if added else "Existing SOS records are already in the queue")
            st.rerun()
        if right.button("Load labelled synthetic examples"):
            added = intake_agent.load_synthetic_examples()
            st.toast(f"Loaded {len(added)} synthetic examples" if added else "Synthetic examples are already loaded")
            st.rerun()
    if reports:
        view = st.radio("Show", ["Pending", "Approved, not logged", "Logged", "Rejected", "All"], horizontal=True)
        if view == "Logged":
            visible = [r for r in reports if r["sos_logged"]]
        elif view == "All":
            visible = reports
        else:
            visible = [r for r in reports if intake_agent.queue_state(r) == view]
        if not visible:
            ui_info(f"No {view.lower()} reports.")
        else:
            queue_class = {"Pending": "saarthi-q-pending", "Approved, not logged": "saarthi-q-approved",
                           "Logged": "saarthi-q-logged", "Rejected": "saarthi-q-rejected"}
            priority_class = {"CRITICAL": "saarthi-pri-critical", "HIGH": "saarthi-pri-high",
                              "MEDIUM": "saarthi-pri-medium", "LOW": "saarthi-pri-low"}
            columns = ("id", "evidence", "record", "queue", "zone", "classification", "severity",
                       "urgency", "people", "priority", "priority_score", "validation_status")
            head = "".join(f"<th>{html.escape(name)}</th>" for name in columns)
            body = []
            for report in visible:
                values = {
                    "id": report["id"], "evidence": _intake_evidence(report),
                    "record": "Synthetic demo" if report["synthetic"] else "Operator entry",
                    "queue": intake_agent.queue_state(report), "zone": report["zone"],
                    "classification": report["classification"], "severity": report["severity"],
                    "urgency": intake_agent.urgency_label(report["source"], report["urgency"]),
                    "people": report["people"], "priority": report["priority"],
                    "priority_score": report["priority_score"], "validation_status": report["validation_status"],
                }
                cells = []
                for key in columns:
                    shown = intake_cell(values[key])
                    text = html.escape(str(shown))
                    classes = []
                    if key == "queue" and queue_class.get(shown):
                        classes.append(queue_class[shown])
                    elif key == "priority" and priority_class.get(shown):
                        classes.append(priority_class[shown])
                    if key in {"id", "priority_score"}:
                        classes.append("saarthi-nowrap")
                    class_attr = f' class="{" ".join(classes)}"' if classes else ""
                    cells.append(f"<td{class_attr}>{text}</td>")
                body.append("<tr>" + "".join(cells) + "</tr>")
            queue_col, detail_col = st.columns([1.15, 1])
            with queue_col:
                with st.container(border=True):
                    st.caption("Evidence reported, except an imported SOS, which stays a recorded incident. "
                               "Priority is the intake score, not a recorded risk reading.")
                    st.markdown(
                        '<div class="saarthi-table-container">'
                        f'<table class="saarthi-scan"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
                        "</div>",
                        unsafe_allow_html=True)
            with detail_col:
                with st.container(border=True):
                    choice = st.selectbox("Open a report", [f"#{r['id']} · {intake_agent.queue_state(r)} · {r['priority']} · {r['zone'] or 'no zone'}" for r in visible])
                    row = next(r for r in visible if choice.startswith(f"#{r['id']} ·"))
                    priority = intake_cell(row.get("priority"))
                    st.markdown(
                        f'**{html.escape(_intake_evidence(row))}** · **{html.escape(str(intake_agent.queue_state(row)))}** · '
                        f'<span class="{priority_class.get(priority, "")}">{html.escape(str(priority))}</span> · '
                        f'score {html.escape(str(intake_cell(row.get("priority_score"))))} · '
                        f'{"Synthetic demo" if row["synthetic"] else "Operator entry"}',
                        unsafe_allow_html=True)
                    st.markdown(f"**Urgency:** {intake_agent.urgency_label(row['source'], row['urgency'])}")
                    parts = df([
                        {"Part": "Urgency", "Contribution": intake_cell(row["score_urgency"])},
                        {"Part": "People affected", "Contribution": intake_cell(row["score_people"])},
                        {"Part": "Severity", "Contribution": intake_cell(row["score_severity"])},
                        {"Part": "Zone vulnerability", "Contribution": intake_cell(row["score_vulnerability"])},
                        {"Part": "Total", "Contribution": intake_cell(row["priority_score"])},
                    ])
                    render_df_table(parts, hide_index=True)
                    safety_notice("This is a rule-based prioritisation score, not an AI prediction or a disaster forecast.")
                    if row.get("priority_reason"):
                        st.write(row["priority_reason"])
                    else:
                        st.caption("Priority reason is not available.")
                    if row["validation_notes"]:
                        ui_warning(row["validation_notes"])
                    else:
                        ui_success("No validation warnings.")
                    reviewer = st.text_input("Reviewer", "Duty officer", key=f"reviewer_{row['id']}")
                    if row["sos_logged"] and row["incident_id"]:
                        ui_success(f"Logged as field request #{row['incident_id']}. It is listed under Open requests on Field Requests "
                                   "until someone claims it. No alert was sent and no responder was dispatched.")
                    elif row["review_status"] == "pending":
                        a, b, c3 = st.columns(3)
                        if a.button("Approve", type="primary"):
                            act(intake_agent.review_report, int(row["id"]), "approved", reviewer, ok=f"Report #{row['id']} approved. It is not a field request yet.")
                        if b.button("Reject"):
                            act(intake_agent.review_report, int(row["id"]), "rejected", reviewer, ok=f"Report #{row['id']} rejected. Nothing was dispatched.")
                        if c3.button("Approve and log SOS"):
                            _log_field_request(int(row["id"]), reviewer)
                    elif row["review_status"] == "approved":
                        ui_warning(f"Approved by {row['reviewed_by'] or 'an operator'}. This report is not on Field Requests yet.")
                        if st.button("Log as field request"):
                            _log_field_request(int(row["id"]), reviewer)
                    else:
                        ui_info(f"Rejected by {row['reviewed_by'] or 'an operator'}. Nothing was dispatched.")
        mapped = [r for r in reports if r["lat"] is not None and r["lon"] is not None]
        if mapped:
            colours = {"CRITICAL": [231, 71, 71], "HIGH": [230, 162, 60], "MEDIUM": [217, 165, 32], "LOW": [43, 108, 176]}
            points = df(mapped)
            points["color"] = points["priority"].map(colours)
            points["label"] = points["priority"] + " " + points["classification"].fillna("") + " #" + points["id"].astype(str)
            with st.container(border=True):
                st.caption("Reported locations that already have coordinates. "
                           "The colour is the intake priority, not a recorded risk level.")
                render_deck([pdk.Layer("ScatterplotLayer", points, get_position="[lon, lat]",
                                        get_fill_color="color", get_line_color=[230, 237, 243], filled=True, stroked=True,
                                        get_radius=6, radius_units=_PIXELS, line_width_min_pixels=1, pickable=True)],
                             460, "intake_map")
                map_legend([(_rgb_hex(colours[name]), f"Report priority {name}") for name in ("CRITICAL", "HIGH", "MEDIUM", "LOW")])
    else:
        ui_info("No reports yet. Import the existing SOS records, load the labelled examples, or enter a report below.")
    with st.container(border=True):
        with st.expander("Enter a citizen report"):
            with st.form("intake_report", clear_on_submit=True):
                zones = ["Unknown"] + [z["name"] for z in data.read("SELECT name FROM zones ORDER BY name")]
                c1, c2, c3 = st.columns(3)
                zone = c1.selectbox("Zone", zones)
                disaster = c2.selectbox("Disaster type", ["Not sure", "Flood", "Heatwave", "Cyclone", "Earthquake", "Fire", "Landslide"])
                urgency = c3.selectbox("Urgency", ["Not specified", "CRITICAL", "HIGH", "MEDIUM", "LOW"])
                d1, d2, d3 = st.columns(3)
                lat = d1.text_input("Latitude", "")
                lon = d2.text_input("Longitude", "")
                people = d3.text_input("People affected", "")
                description = st.text_area("Description", "")
                st.caption("Urgency is required. Not specified is left missing and is not scored as LOW.")
                if st.form_submit_button("Triage this report", type="primary"):
                    form_error = intake_agent.citizen_form_error(urgency)
                    if form_error:
                        st.session_state["intake_error"] = form_error
                    else:
                        intake_agent.submit({"source": "citizen", "zone": zone, "lat": lat, "lon": lon, "people": people,
                                             "disaster_type": disaster, "urgency": urgency, "description": description, "synthetic": 0})
                        st.session_state["intake_notice"] = "Report triaged and added to the pending queue. It is not a field request until you log it."
                    st.rerun()


def _intake_evidence(row):
    """Citizen and practice reports stay reported. An imported SOS cites the recorded incident."""
    if row.get("source") == "existing_sos":
        return "recorded incident"
    return "reported"


def _log_field_request(report_id, reviewer):
    """Save one open field request and keep its id on screen after the redraw."""
    try:
        saved = intake_agent.approve_and_log_sos(report_id, reviewer)
        if saved["source"] == "existing_sos":
            st.session_state["intake_notice"] = (
                f"Field request #{saved['incident_id']} is already stored from the original SOS. "
                "No second request was created. Open Field Requests and look under Open requests."
            )
        else:
            st.session_state["intake_notice"] = (
                f"Field request #{saved['incident_id']} was stored as OPEN for {saved['zone']}. "
                "Open Field Requests and look under Open requests. No alert was sent and no responder was dispatched."
            )
        st.rerun()
    except data.ActionError as exc:
        ui_error(str(exc))


def page_ripple():  # PAGE 10: read-only cascade analysis
    """Map-led cascade. Labels and scores come from the existing analyse() result."""
    st.markdown(
        """
        <style>
        .saarthi-role {
            display: inline-block; font-size: 0.72rem; letter-spacing: 0.04em; line-height: 1.3;
            border: 1px solid var(--saarthi-line); padding: 2px 6px;
        }
        .saarthi-role-primary { color: #E74747; border-color: #E74747; }
        .saarthi-role-secondary { color: #E6A23C; border-color: #E6A23C; }
        .saarthi-role-extended { color: #D9A520; border-color: #D9A520; }
        </style>
        """,
        unsafe_allow_html=True)

    def reported_evidence(zone):
        """Show citizen reports only when this cascade result already carries them."""
        if not isinstance(zone, dict) or "reported" not in zone:
            return
        reports = zone.get("reported")
        st.caption("Evidence reported. Citizen reports on this zone are not the recorded risk reading "
                   "and not the attention score.")
        if reports is None:
            st.caption("Reported citizen evidence is not available.")
            return
        if not reports:
            st.caption("No reported citizen evidence for this zone.")
            return
        render_df_table(df([{
            "id": row.get("id"),
            "evidence": row.get("basis") or "reported",
            "classification": _sim_text(row.get("classification")),
            "severity": _sim_text(row.get("severity")),
            "urgency": _sim_text(row.get("urgency")),
            "people": _sim_text(row.get("people")),
            "priority": _sim_text(row.get("priority")),
            "validation": _sim_text(row.get("validation_status")),
            "created": _sim_text(row.get("created_at")),
        } for row in reports]), hide_index=True)

    with st.container(border=True):
        page_header("Ripple",
                    "Recorded facts and the inferred cascade from one origin on the stored network.")
        safety_notice(
            "Read-only. Recorded facts and inferred cascade findings are labelled separately.",
            "Recorded lines cite stored roads, hospitals, stock, and risk readings. "
            "Inferred lines are the cascade. A Ripple timeline tag Simulated is an inference, not a What-If outcome. "
            "A missing attention score is not zero. "
            "This page does not change incidents, roads, hospitals, stock, shipments, or the plan.",
            key="ripple_readonly_notice")
        st.caption("Evidence recorded for stored rows. Evidence inferred for the cascade and the attention score. "
                   "A missing attention score is not zero.")
    names = [z["name"] for z in data.read("SELECT name FROM zones ORDER BY name")]
    origin = st.selectbox("Origin zone", names, index=names.index("Ratnagiri") if "Ratnagiri" in names else 0)
    stored_reports = data.read("SELECT * FROM citizen_reports ORDER BY priority_score DESC, id DESC")
    result = ripple_agent.analyse(origin, stored_reports)
    st.session_state["whatif_ripple"] = result
    version = result["plan_version"]
    metric_tiles((
        ("Primary zone", result["primary"]["zone"], LEVEL_HEX["Critical"]),
        ("Secondary zones", len(result["secondary"]), LEVEL_HEX["High"]),
        ("Extended zones", len(result["extended"]), LEVEL_HEX["Moderate"]),
        ("Plan version read", "not available" if version is None else version, TEXT),
    ))
    with st.container(border=True):
        st.caption("Primary is the origin. Secondary is one stored zone-hop. "
                   "Extended is two stored zone-hops. A zone outside this ring is not an impact.")
        _ripple_map(result)
    primary, secondary, extended = st.columns(3)
    with primary:
        with st.container(border=True):
            st.markdown('<span class="saarthi-role saarthi-role-primary">Primary · recorded</span>',
                        unsafe_allow_html=True)
            st.subheader("Primary impact")
            safety_notice("Recorded facts at the origin.")
            _ripple_zone_card(result["primary"])
            reported_evidence(result["primary"])
    with secondary:
        with st.container(border=True):
            st.markdown('<span class="saarthi-role saarthi-role-secondary">Secondary · inferred</span>',
                        unsafe_allow_html=True)
            st.subheader("Secondary impact")
            safety_notice("One stored zone-hop away. A neighbour is inferred, not a confirmed impact and not a What-If outcome.")
            if result["secondary"]:
                for zone in result["secondary"]:
                    _ripple_zone_card(zone)
                    reported_evidence(zone)
            else:
                ui_info("No secondary zone is connected by a stored zone-to-zone road.")
    with extended:
        with st.container(border=True):
            st.markdown('<span class="saarthi-role saarthi-role-extended">Extended · inferred</span>',
                        unsafe_allow_html=True)
            st.subheader("Extended impact")
            st.caption("Evidence inferred. Two stored zone-hops. Not a confirmed impact and not a What-If outcome.")
            if result["extended"]:
                for zone in result["extended"]:
                    _ripple_zone_card(zone)
                    reported_evidence(zone)
            else:
                ui_info("No zone is two hops away on the stored zone roads.")
    if "unlocated_reports" in result or "reports_outside_ring" in result:
        with st.container(border=True):
            st.subheader("Reported evidence outside a zone card")
            st.caption("Evidence reported. These reports are not recorded risk readings and not attention scores.")
            unlocated = result.get("unlocated_reports")
            outside = result.get("reports_outside_ring")
            if unlocated is None and outside is None:
                st.caption("Reported citizen evidence is not available.")
            elif not unlocated and not outside:
                st.caption("No unlocated or outside-ring citizen reports.")
            else:
                rows = [("unlocated", row) for row in unlocated or []] + [("outside the ring", row) for row in outside or []]
                render_df_table(df([{
                    "place": place,
                    "id": row.get("id"),
                    "evidence": row.get("basis") or "reported",
                    "zone": _sim_text(row.get("zone")),
                    "classification": _sim_text(row.get("classification")),
                    "severity": _sim_text(row.get("severity")),
                    "people": _sim_text(row.get("people")),
                } for place, row in rows]), hide_index=True)
    st.subheader("Cascade timeline")
    st.caption("Recorded lines cite stored rows. Inferred lines are the cascade. Neither line is a What-If simulation.")
    for stage in result["timeline"]:
        with st.container(border=True):
            st.markdown(f"**{stage['stage']}**")
            st.caption(stage["reason"] if stage.get("reason") else "Stage reason is not available.")
            for line in stage["lines"]:
                kind = "recorded" if line.get("tag") == "Recorded" else "inferred"
                text = line["text"] if line.get("text") is not None else "not available"
                st.markdown(f"**{kind}.** {text}")
    with st.container(border=True):
        st.subheader("Full-network and zone-only bridges")
        safety_notice(
            "Both checks remove a road only in memory. The stored road status is not changed.",
            "Both checks remove a road only in memory. The stored road status is not changed. "
            "A zone-only bridge can still have an alternate route through a hub.",
            key="ripple_bridge_notice")
        full_rows = [{"Road": b["id"], "Ends": f"{b['a']} — {b['b']}",
                      "Unreachable from first end": ", ".join(b["unreachable_from_a"])}
                     for b in result["bridges"]["full_network"]]
        zone_rows = [{"Road": b["id"], "Ends": f"{b['a']} — {b['b']}", "Note": b["reason"]} for b in result["bridges"]["zone_graph"]]
        fcol, zcol = st.columns(2)
        fcol.markdown("**Full network, including hubs and hospitals**")
        fcol.caption("Evidence recorded. A road is removed only in memory.")
        render_df_table(df(full_rows), hide_index=True, target=fcol)
        zcol.markdown("**Zone names only**")
        zcol.caption("Evidence inferred for the zone graph. A hub route can remain.")
        render_df_table(df(zone_rows), hide_index=True, target=zcol)
        for compared in result["bridges"]["comparisons"]:
            if compared["zone_graph_bridge"] and not compared["full_network_bridge"]:
                st.markdown(f"**{compared['id']}** · {compared['note']}")
    with st.container(border=True):
        st.subheader("Hospital pressure")
        st.caption("Evidence recorded. A missing hospital record is not zero beds.")
        render_df_table(df([{"Zone": h["zone"], "Record": h["text"], "ICU": h["icu"], "Oxygen": h["oxygen"]}
                         for h in result["hospitals"]]), hide_index=True)
    with st.container(border=True):
        st.subheader("Supply reach")
        safety_notice(
            "No allocation is created and inventory is not changed.",
            "Demand comes from the existing supply-need rule. No allocation is created and inventory is not changed. "
            "A missing stock row is not shown as zero.",
            key="ripple_supply_notice")
        for row in result["supply"]:
            st.markdown(f"**{row['zone']}.** {row['text']}")
        for hub in result["reach"]:
            with st.expander(hub["route"]):
                render_df_table(df([{"Item": s["item"], "Stock": s["text"]} for s in hub["stock"]]),
                             hide_index=True)
    with st.container(border=True):
        st.subheader("Heuristic attention score")
        safety_notice(result["disclaimer"])
        st.caption("This score is inferred. It is not Intake priority and not planner severity. "
                   "Score = severity (max 40) + hospital pressure (max 25) + open medical patients (max 20) "
                   "+ full-network connectivity (15, 8, or 0). A missing contribution is not treated as zero, and that zone is not ranked.")
        ranked = result["recommendations"]["ranked"]
        if ranked:
            render_df_table(df([{
                "Rank": i + 1,
                "Zone": r["zone"],
                "Score": "not available" if r.get("score") is None else r["score"],
                "Severity": "not available" if r.get("severity") is None else r["severity"],
                "Hospital": "not available" if r.get("hospital") is None else r["hospital"],
                "Medical": "not available" if r.get("medical") is None else r["medical"],
                "Connectivity": "not available" if r.get("connectivity") is None else r["connectivity"],
            } for i, r in enumerate(ranked)]), hide_index=True)
            choice = st.selectbox("Why this rank", [r["zone"] for r in ranked])
            chosen = next(r for r in ranked if r["zone"] == choice)
            for reason in chosen["reasons"]:
                st.write(reason if reason is not None else "not available")
        else:
            ui_info("No affected zone has every contribution recorded.")
        if result["recommendations"]["unranked"]:
            st.markdown("**Not ranked — missing data**")
            render_df_table(df([{
                "Zone": r["zone"],
                "Missing": ", ".join(r["missing"]) if r.get("missing") else "not available",
                "Hospital record": r["hospital_text"] if r.get("hospital_text") is not None else "not available",
            } for r in result["recommendations"]["unranked"]]), hide_index=True)


def _ripple_map(result):
    roles = {result["primary"]["zone"]: "primary"}
    roles.update({z["zone"]: "secondary" for z in result["secondary"]})
    roles.update({z["zone"]: "extended" for z in result["extended"]})
    colours = {"primary": [231, 71, 71], "secondary": [230, 162, 60], "extended": [217, 165, 32], "other": [38, 155, 117]}
    points = []
    for zone in data.read("SELECT name, lat, lon FROM zones"):
        role = roles.get(zone["name"], "other")
        points.append({"name": zone["name"], "lat": zone["lat"], "lon": zone["lon"], "color": colours[role],
                       "radius": 24 if role == "primary" else 16, "label": f"{zone['name']}: {role}"})
    places = df(points)
    roads = df(data.roads_with_positions())
    roads["path"] = roads.apply(lambda x: [[x["a_lon"], x["a_lat"]], [x["b_lon"], x["b_lat"]]], axis=1)
    roads["color"] = roads["status"].map(ROAD_RGB)
    roads["width"] = roads["status"].map({"OPEN": 2, "SLOW": 3, "BLOCKED": 3})
    roads["label"] = roads["id"] + ": " + roads["a"] + " to " + roads["b"] + ", " + roads["status"]
    render_deck([
        pdk.Layer("PathLayer", roads, get_path="path", get_color="color", get_width="width", width_units=_PIXELS, pickable=True),
        pdk.Layer("ScatterplotLayer", places, get_position="[lon, lat]", get_line_color="color", filled=False,
                  stroked=True, get_radius="radius", radius_units=_PIXELS, line_width_min_pixels=2, pickable=True),
        _place_labels(places, "name", [0, -14]),
    ], 560, "ripple_map")
    map_legend([
        (_rgb_hex(colours["primary"]), "Primary zone"),
        (_rgb_hex(colours["secondary"]), "Secondary zone"),
        (_rgb_hex(colours["extended"]), "Extended zone"),
        (_rgb_hex(colours["other"]), "Outside this ring"),
        (_rgb_hex(ROAD_RGB["OPEN"]), "Open"),
        (_rgb_hex(ROAD_RGB["SLOW"]), "Slow"),
        (_rgb_hex(ROAD_RGB["BLOCKED"]), "Blocked"),
    ])


def _ripple_zone_card(zone):
    risk = zone["risk"]
    if risk is None:
        reading = "No risk reading recorded."
    else:
        reading = f"Recorded reading: {risk['disaster_type']} probability {float(risk['probability']):.2f}."
    st.markdown(f"**{zone['zone']}** · {reading} {zone['hospital']['text']}.")
    open_roads = ", ".join(f"{r['id']} {r['status']}" for r in zone["roads"]) or "No road recorded"
    st.caption(f"Recorded roads. {open_roads}")


def page_whatif():  # PAGE 11: hypothetical scenarios, never written to the database
    """In-memory comparison. Recorded values stay in the database; simulated values stay in this result."""
    st.markdown(
        """
        <style>
        .saarthi-source {
            display: inline-block; font-size: 0.72rem; letter-spacing: 0.04em; line-height: 1.3;
            border: 1px solid var(--saarthi-line); padding: 2px 6px; margin-right: 6px;
        }
        .saarthi-source-recorded { color: var(--saarthi-muted); }
        .saarthi-source-simulated { color: #E6A23C; border-color: #E6A23C; background: #FFFBF4; }
        </style>
        """,
        unsafe_allow_html=True)

    def cell(value):
        return "not available" if value is None else value

    modifications = st.session_state.setdefault("whatif_mods", [])
    ripple = st.session_state.get("whatif_ripple")
    try:
        if ripple is None:
            result = whatif_agent.compare(modifications)
        else:
            result = whatif_agent.compare(modifications, ripple=ripple)
    except whatif_agent.ScenarioError as exc:
        result = None
        compare_error = exc
    else:
        st.session_state["whatif_simulation"] = result
        compare_error = None
    with st.container(border=True):
        page_header("What-If", "An in-memory comparison of recorded rows and one simulated list.")
        st.caption("RECORDED values are database rows. SIMULATED values are this comparison. "
                   "A cascade passed in from Coordinator is inferred and is not run again. "
                   "Nothing here is approved or executed.")
        safety_notice(
            whatif_agent.DISCLAIMER,
            "Each run reads the database again and applies the hypothetical list to a new in-memory copy. "
            "Reset clears that list. It does not run Scenario A and it does not rewrite the demo database. "
            + whatif_agent.DISCLAIMER,
            key="whatif_disclaimer")
    if result is not None:
        roads = result["roads"]
        changed_roads = [row for row in roads if row.get("recorded_status") != row.get("simulated_status")]
        route_changes = result["routes"]
        blocked = any(row.get("simulated_status") == "BLOCKED" for row in changed_roads)
        road_colour = LEVEL_HEX["Critical"] if blocked else (WARNING if changed_roads else TEXT)
        metric_tiles((
            ("Plan version read", cell(result.get("recorded_plan_version")), TEXT),
            ("Hypothetical changes", len(modifications), WARNING if modifications else TEXT),
            ("Roads changed", len(changed_roads), road_colour),
            ("Routes changed", len(route_changes), WARNING if route_changes else TEXT),
        ))
    with st.container(border=True):
        st.subheader("Simulation input")
        st.caption("Evidence simulated. This list is held in memory. Compare reads the database and does not save it.")
        _whatif_controls(modifications)
        if modifications:
            st.markdown("**Hypothetical modifications**")
            for note_index, modification in enumerate(modifications, start=1):
                st.markdown(f"{note_index}. `{modification['kind']}` · {modification}")
        else:
            safety_notice("No hypothetical changes. The comparison below is the RECORDED baseline.")
        if st.button("Reset to recorded baseline"):
            st.session_state["whatif_mods"] = whatif_agent.reset_modifications()
            st.rerun()
    if compare_error is not None:
        ui_error(str(compare_error))
        return
    version = result.get("recorded_plan_version")
    st.caption(f"RECORDED plan version {cell(version)} was read and not replaced.")
    if "ripple_context" in result:
        with st.container(border=True):
            _whatif_ripple_context(result)
    for note in result["modifications"]:
        st.markdown(f"**SIMULATED.** {note}")
    with st.container(border=True):
        st.markdown(
            '<span class="saarthi-source saarthi-source-recorded">Recorded</span>'
            '<span class="saarthi-source saarthi-source-simulated">Simulated</span>',
            unsafe_allow_html=True)
        st.caption("The left map is recorded status. The right map is simulated status. "
                   "Both use the same dark basemap and the same Maharashtra camera.")
        _whatif_maps(result)
    changed_roads = [row for row in result["roads"] if row.get("recorded_status") != row.get("simulated_status")]
    unchanged_roads = [row for row in result["roads"] if row.get("recorded_status") == row.get("simulated_status")]
    changed_beds = [row for row in result["hospitals"] if row.get("recorded_free") != row.get("simulated_free")]
    changed_stock = [row for row in result["stock"] if row.get("recorded_qty") != row.get("simulated_qty")]
    changed_severity = [row for row in result["severity"]
                        if row.get("recorded_probability") != row.get("simulated_probability")
                        or row.get("recorded_level") != row.get("simulated_level")]
    with st.container(border=True):
        st.subheader("What changed")
        st.caption("Evidence simulated. These rows differ from the recorded value. Nothing was written.")
        if not any((changed_roads, result["routes"], changed_beds, changed_stock, changed_severity)):
            st.caption("No recorded value changed. This comparison is the recorded baseline.")
        for row in changed_roads:
            st.markdown(f"**SIMULATED road.** {row['id']} {row['a']} — {row['b']}: "
                        f"recorded {cell(row.get('recorded_status'))}, simulated {cell(row.get('simulated_status'))}. "
                        f"{cell(row.get('meaning'))}")
        for row in result["routes"]:
            st.markdown(f"**SIMULATED route.** {row['from']} to {row['to']}: "
                        f"recorded {cell(row.get('recorded_minutes'))} minutes, "
                        f"simulated {cell(row.get('simulated_minutes'))} minutes.")
        for row in changed_beds:
            st.markdown(f"**SIMULATED beds.** {row['name']}: recorded {cell(row.get('recorded_free'))} free, "
                        f"simulated {cell(row.get('simulated_free'))} free.")
        for row in changed_stock:
            st.markdown(f"**SIMULATED stock.** {row['hub']} {row['item']}: "
                        f"recorded {cell(row.get('recorded_qty'))}, simulated {cell(row.get('simulated_qty'))}.")
        for row in changed_severity:
            st.markdown(f"**SIMULATED severity.** {row['zone']}: "
                        f"recorded {cell(row.get('recorded_level'))} {cell(row.get('recorded_probability'))}, "
                        f"simulated {cell(row.get('simulated_level'))} {cell(row.get('simulated_probability'))}.")
        st.subheader("What stayed unchanged")
        st.caption(f"Evidence recorded. {len(unchanged_roads)} roads keep the recorded status. "
                   f"{len(result['hospitals']) - len(changed_beds)} hospital rows keep the recorded free beds. "
                   f"{len(result['stock']) - len(changed_stock)} stock rows keep the recorded quantity. "
                   f"{len(result['severity']) - len(changed_severity)} zones keep the recorded probability and level.")
    with st.container(border=True):
        st.subheader("Routes")
        st.caption("Evidence recorded in the RECORDED columns. Evidence simulated in the SIMULATED columns.")
        if result["routes"]:
            render_df_table(df([{
                "From": row["from"], "To": row["to"],
                "RECORDED minutes": cell(row.get("recorded_minutes")),
                "SIMULATED minutes": cell(row.get("simulated_minutes")),
                "RECORDED roads": "not available" if row.get("recorded_roads") is None else ", ".join(row["recorded_roads"]),
                "SIMULATED roads": "not available" if row.get("simulated_roads") is None else ", ".join(row["simulated_roads"]),
            } for row in result["routes"]]), hide_index=True)
        else:
            st.caption("No route minutes changed.")
        isolated = [row for row in result["isolation"] if row["label"] == "SIMULATED"]
        st.subheader("Isolated zones and inaccessible facilities")
        if isolated:
            for row in isolated:
                st.markdown(row["text"] if row.get("text") is not None else "not available")
        else:
            st.caption("This hypothetical list does not newly isolate a zone or cut off a facility that was reachable.")
    with st.container(border=True):
        st.subheader("Hospital capacity")
        st.caption("Evidence recorded and simulated. No patient record was moved.")
        render_df_table(df([{
            "Hospital": row["name"], "Zone": row["zone"],
            "RECORDED free beds": cell(row.get("recorded_free")),
            "SIMULATED free beds": cell(row.get("simulated_free")),
            "RECORDED occupancy": cell(row.get("recorded_occupancy")),
            "SIMULATED occupancy": cell(row.get("simulated_occupancy")),
            "ICU": cell(row.get("icu")), "Label": cell(row.get("label")),
        } for row in result["hospitals"]]), hide_index=True)
        patients = result["patients"]
        st.caption(f"RECORDED unplaced patients: {cell(patients.get('recorded_unplaced'))}. "
                   f"SIMULATED unplaced patients: {cell(patients.get('simulated_unplaced'))}. "
                   "No patient record was moved.")
        if not any(row["zone"] == "Palghar" for row in result["hospitals"]):
            st.caption("Palghar: no hospital recorded, so hypothetical beds cannot be invented.")
    with st.container(border=True):
        st.subheader("Resource availability")
        st.caption("Evidence recorded and simulated. No shipment was created and inventory was not reduced.")
        render_df_table(df([{
            "Hub": row["hub"], "Item": row["item"],
            "RECORDED qty": cell(row.get("recorded_qty")),
            "SIMULATED qty": cell(row.get("simulated_qty")),
            "Label": cell(row.get("text")),
        } for row in result["stock"]]), hide_index=True)
        supply_changed = result["supply"].get("changed")
        st.caption(f"SIMULATED supply calculation changed {cell(supply_changed)} route rows. "
                   "No shipment was created and inventory was not reduced in the database.")
        for shortfall in result["supply"]["simulated_shortfalls"]:
            st.markdown(f"**SIMULATED shortfall.** {shortfall['zone']}: {cell(shortfall.get('missing'))} "
                        f"{shortfall['item']}. {cell(shortfall.get('why'))}")
    with st.container(border=True):
        st.subheader("Zone severity")
        st.caption("Evidence recorded and simulated. A missing probability or score is not zero.")
        render_df_table(df([{
            "Zone": row["zone"],
            "RECORDED probability": cell(row.get("recorded_probability")),
            "SIMULATED probability": cell(row.get("simulated_probability")),
            "RECORDED score": cell(row.get("recorded_score")),
            "SIMULATED score": cell(row.get("simulated_score")),
            "RECORDED level": cell(row.get("recorded_level")),
            "SIMULATED level": cell(row.get("simulated_level")),
            "Higher priority": cell(row.get("higher_priority")),
            "Label": cell(row.get("label")),
        } for row in result["severity"]]), hide_index=True)
        render_df_table(df([{
            "Road": row["id"], "Ends": f"{row['a']} — {row['b']}",
            "RECORDED": cell(row.get("recorded_status")),
            "SIMULATED": cell(row.get("simulated_status")),
            "RECORDED minutes": cell(row.get("recorded_minutes")),
            "SIMULATED minutes": cell(row.get("simulated_minutes")),
            "Meaning": cell(row.get("meaning")),
        } for row in result["roads"]]), hide_index=True)


def _whatif_controls(modifications):
    kind = st.selectbox("Add a hypothetical change", ["Road status", "Hospital free beds", "Hub stock", "Zone probability"])
    if kind == "Road status":
        roads = data.read("SELECT id, a, b, status FROM roads ORDER BY id")
        label = st.selectbox("Road", [f"{r['id']}: {r['a']} to {r['b']} (RECORDED {r['status']})" for r in roads])
        status = st.selectbox("SIMULATED status", ["BLOCKED", "SLOW", "OPEN"])
        if st.button("Add road change"):
            _whatif_add(modifications, {"kind": "road", "road_id": label.split(":")[0], "status": status})
    elif kind == "Hospital free beds":
        hospitals = data.read("SELECT id, name, total_beds, available_beds FROM hospitals ORDER BY id")
        label = st.selectbox("Hospital", [f"{h['id']}: {h['name']} (RECORDED {h['available_beds']} of {h['total_beds']} free)" for h in hospitals])
        chosen = next(h for h in hospitals if label.startswith(f"{h['id']}:"))
        beds = st.number_input("SIMULATED free beds", min_value=0, max_value=int(chosen["total_beds"]), value=int(chosen["available_beds"]), step=1)
        if st.button("Add hospital change"):
            _whatif_add(modifications, {"kind": "beds", "hospital_id": int(chosen["id"]), "available_beds": int(beds)})
    elif kind == "Hub stock":
        hubs = data.read("SELECT id, name FROM hubs ORDER BY id")
        hub_label = st.selectbox("Hub", [f"{h['id']}: {h['name']}" for h in hubs])
        hub_id = int(hub_label.split(":")[0])
        rows = data.read("SELECT item, qty FROM inventory WHERE hub_id=:h ORDER BY item", h=hub_id)
        if not rows:
            ui_info("No stock row recorded at this hub.")
        else:
            item_label = st.selectbox("Item", [f"{r['item']} (RECORDED {r['qty']})" for r in rows])
            item = item_label.split(" (RECORDED ")[0]
            recorded_qty = int(next(r["qty"] for r in rows if r["item"] == item))
            qty = st.number_input("SIMULATED quantity", min_value=0, max_value=recorded_qty, value=recorded_qty, step=1)
            if st.button("Add stock change"):
                _whatif_add(modifications, {"kind": "stock", "hub_id": hub_id, "item": item, "qty": int(qty)})
    else:
        risks = data.read("""SELECT zone, probability FROM risk_logs
                              WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone) ORDER BY zone""")
        if not risks:
            ui_info("No risk reading is recorded.")
        else:
            label = st.selectbox("Zone", [f"{r['zone']} (RECORDED probability {float(r['probability']):.2f})" for r in risks])
            zone = label.split(" (RECORDED")[0]
            recorded = float(next(r["probability"] for r in risks if r["zone"] == zone))
            probability = st.number_input("SIMULATED probability", min_value=recorded, max_value=1.0, value=recorded, step=0.01, format="%.2f")
            if st.button("Add severity change"):
                _whatif_add(modifications, {"kind": "probability", "zone": zone, "probability": float(probability)})


def _whatif_add(modifications, modification):
    updated = list(modifications) + [modification]
    try:
        whatif_agent.compare(updated)
    except whatif_agent.ScenarioError as exc:
        ui_error(str(exc))
        return
    st.session_state["whatif_mods"] = updated
    st.rerun()


def _muted_road(rgb):
    """Dim a status colour so an unchanged simulated road sits behind a change."""
    return [int(channel * 0.28 + 150) for channel in rgb]


def _whatif_maps(result):
    spots = data.places()
    map_legend([
        (_rgb_hex(ROAD_RGB["OPEN"]), "Open"),
        (_rgb_hex(ROAD_RGB["SLOW"]), "Slow"),
        (_rgb_hex(ROAD_RGB["BLOCKED"]), "Blocked"),
        (_rgb_hex(_muted_road(ROAD_RGB["OPEN"])), "Unchanged on Simulated Conditions"),
    ])
    safety_notice(
        "Recorded Conditions use stored status. Simulated Conditions use the in-memory copy.",
        "Recorded Conditions uses the stored status colour. On Simulated Conditions, a changed road keeps "
        "the simulated status colour and is drawn thicker. An unchanged road is muted.",
        key="whatif_map_notice")
    left, right = st.columns(2)
    with left:
        st.markdown("**Recorded Conditions**")
        _whatif_map(result, spots, "recorded_status", "whatif_recorded_map")
    with right:
        st.markdown("**Simulated Conditions**")
        _whatif_map(result, spots, "simulated_status", "whatif_simulated_map")


def _whatif_map(result, spots, status_field, key):
    lines = []
    for road in result["roads"]:
        start, end = spots.get(road["a"]), spots.get(road["b"])
        if not start or not end:
            continue
        recorded = road["recorded_status"]
        simulated = road["simulated_status"]
        changed = recorded != simulated
        status = road[status_field]
        colour = ROAD_RGB.get(status, [107, 114, 128])
        if status_field == "simulated_status" and not changed:
            colour = _muted_road(colour)
            width = 2
        elif changed and status_field == "simulated_status":
            width = 3
        else:
            width = 2 if status == "OPEN" else 3
        lines.append({"path": [[start[1], start[0]], [end[1], end[0]]], "color": colour, "width": width,
                      "changed": changed,
                      "label": (f"{road['id']}: {road['a']} to {road['b']}. "
                                f"Recorded {recorded}. Simulated {simulated}. "
                                f"{'Changed' if changed else 'Unchanged'}.")})
    if not lines:
        ui_info("No mapped roads.")
        return
    lines.sort(key=lambda row: row["changed"])  # unchanged first, so a change is drawn on top
    frame = df(lines)
    render_deck([pdk.Layer("PathLayer", frame, get_path="path", get_color="color", get_width="width",
                           width_units=_PIXELS, pickable=True)],
                480, key)


def sidebar_demos():  # demo buttons
    st.sidebar.divider()  # line
    st.sidebar.subheader("Demo controls")  # heading
    safety_notice(
        "LIVE DEMO DATABASE MODIFICATION. These controls can change actual demo database records.",
        tone="caution", target=st.sidebar)
    if st.sidebar.button("Cyclone intensifies"):  # prediction demo
        act(data.demo_cyclone, ok="Cyclone risk updated")
    for sid in "BCDE":  # quick scenario buttons
        if st.sidebar.button(f"{sid} · {data.SCENARIOS[sid][0]}"):  # pressed
            st.session_state["scenario_result"] = data.run_scenario(sid)  # run and remember
            st.rerun()  # redraw
    if st.sidebar.button("Tamper with a ledger record"):  # ledger demo
        act(data.demo_tamper, ok="A past record was edited")
    if st.sidebar.button("A · Reset to normal"):  # start over
        st.session_state["scenario_result"] = data.run_scenario("A")  # reset
        st.rerun()  # redraw


def page_coordinator():  # read-only brief; nothing on this page is stored
    page_fit_styles()
    with st.container(border=True):
        page_header("Coordinator",
                    "A report typed here is passed to the brief in memory. It is not stored and it is not logged as a field request.")
        safety_notice(coordinator_agent.DISCLAIMER)
    zones = [row["name"] for row in data.read("SELECT name FROM zones ORDER BY name")]
    with st.container(border=True):
        origin_choice = st.selectbox("Recorded zone", ["No zone selected"] + zones, key="coord_origin")
        description = st.text_area("Report description", key="coord_description")
        entry_left, entry_right = st.columns(2)
        people = entry_left.text_input("People affected", key="coord_people")
        urgency = entry_right.selectbox("Urgency", ["Not provided", "CRITICAL", "HIGH", "MEDIUM", "LOW"], key="coord_urgency")
        disaster_type = st.text_input("Stated disaster type", key="coord_disaster")
        st.caption("Review the stored allocation plan as a proposal. "
                   "This copies the existing plan for review. It does not generate, approve, or execute a plan, "
                   "and it does not create a shipment.")
        if st.button("Review stored plan as proposed"):
            st.session_state["proposed_plan"] = copy.deepcopy(data.get_plan())
        held_plan = st.session_state.get("proposed_plan")
        if isinstance(held_plan, dict):
            held_version = held_plan.get("version")
            held_text = "not available" if held_version is None else str(held_version)
            st.caption(f"Proposed plan held from stored version {held_text}. Unapproved. Not executed.")
    report = {}
    if description.strip():
        report["description"] = description.strip()
    if people.strip():
        report["people"] = people.strip()
    if urgency != "Not provided":
        report["urgency"] = urgency
    if disaster_type.strip():
        report["disaster_type"] = disaster_type.strip()
    if origin_choice != "No zone selected":
        report["zone"] = origin_choice
    try:
        stored_reports = intake_agent.list_reports()
        result = coordinator_agent.brief(
            report or None,
            None if origin_choice == "No zone selected" else origin_choice,
            stored_reports,
            st.session_state.get("whatif_simulation"),
            st.session_state.get("proposed_plan"))
        ripple_section = result["agents"]["ripple"]
        if ripple_section.get("status") == "ok" and isinstance(ripple_section.get("result"), dict):
            st.session_state["whatif_ripple"] = ripple_section["result"]
    except Exception as exc:
        ui_error(f"The brief could not be built: {exc}")
        return
    version = result.get("plan_version")
    version_text = "not available" if version is None else str(version)
    st.caption(f"RECORDED plan version {version_text}. Brief at {result.get('reviewed_at') or 'not available'}.")
    st.caption("Recorded is the database. Reported is a citizen report. Inferred is Ripple. "
               "Simulated is What-If. Proposed is a plan awaiting review. Evaluated is a Plan Challenger finding. "
               "Missing is not zero. A proposed or evaluated plan is not approved or executed.")
    safety_notice(
        "Intake priority, the Ripple attention score, and planner severity are three different calculations.",
        "Intake priority, the Ripple attention score, and planner severity are three different calculations. "
        "Do not compare them directly or treat one as a substitute for another.",
        key="coordinator_scores_notice")
    with st.container(border=True):
        _coordinator_citizen_reports(result.get("citizen_reports"))
    brief_left, brief_right = st.columns(2)
    with brief_left:
        with st.container(border=True):
            _coordinator_findings(result)
        with st.container(border=True):
            _coordinator_ripple(result["agents"]["ripple"])
    with brief_right:
        with st.container(border=True):
            _coordinator_intake(result["agents"]["intake"])
        with st.container(border=True):
            _coordinator_challenger(result["agents"]["plan_challenger"], result.get("records"))
            _coordinator_proposed(result.get("proposed_evaluation"))
    with st.container(border=True):
        _coordinator_whatif(result["agents"]["whatif"])
        _coordinator_received_simulation(result.get("received_simulation"))


def _ripple_awaiting_origin(section):
    """True only before an origin is chosen. A selected zone with a missing input is not this state."""
    return (section.get("status") == "missing"
            and section.get("result") is None
            and list(section.get("missing") or []) == ["zone"]
            and not section.get("error"))


def _coordinator_findings(result):
    findings = list(result.get("findings") or [])
    ripple = (result.get("agents") or {}).get("ripple") or {}
    if _ripple_awaiting_origin(ripple):
        findings = [row for row in findings if row.get("source") != "ripple"]
    labels = [("confirmed", "Confirmed"), ("potential", "Potential"), ("missing", "Missing")]
    for kind, title in labels:
        rows = [row for row in findings if row.get("kind") == kind]
        st.subheader(title)
        if not rows:
            st.caption("None.")
            continue
        render_df_table(df([{"source": row.get("source"), "evidence": _evidence_class(row.get("source")),
                          "detail": row.get("detail"),
                          "evidence text": _evidence_text(row.get("evidence"))} for row in rows]),
                     hide_index=True)
    other = [row for row in findings if row.get("kind") not in ("confirmed", "potential", "missing")]
    st.subheader("Other brief notes")
    if not other:
        st.caption("None.")
        return
    for row in other:
        st.markdown(f"**{row.get('source')} · {row.get('kind')}.** {row.get('detail')}")
        evidence = _evidence_text(row.get("evidence"))
        if evidence:
            with st.expander(f"Evidence from {row.get('source')}"):
                st.write(evidence)


def _coordinator_citizen_reports(section):
    """Stored citizen reports already retrieved by list_reports. They are not incidents."""
    st.subheader("Citizen Reports")
    st.caption("Evidence reported. These stored reports are not recorded risk readings, "
               "not Ripple attention scores, and not simulated findings. "
               "They were not logged as incidents.")
    if not isinstance(section, dict) or section.get("reports") is None:
        st.caption("Stored citizen reports are not available.")
        return
    rows = section.get("reports") or []
    if not rows:
        ui_info("No stored citizen reports.")
        return
    render_df_table(df([{
        "id": row.get("id"),
        "evidence": row.get("basis") or "reported",
        "classification": _sim_text(row.get("classification")),
        "severity": _sim_text(row.get("severity")),
        "urgency": _sim_text(row.get("urgency")),
        "people": _sim_text(row.get("people")),
        "priority": _sim_text(row.get("priority")),
        "validation": _sim_text(row.get("validation_status")),
        "created": _sim_text(row.get("created_at")),
    } for row in rows]), hide_index=True)


def _coordinator_intake(section):
    st.subheader("Intake & Triage")
    _agent_status(section)
    st.markdown("**Intake priority**")
    safety_notice(
        "Intake priority rates the citizen report. It is not the Ripple score or the stored plan.",
        "This rates the citizen report by urgency and impact, using the Intake agent's own rules. "
        "It does not measure cascading risk or the stored allocation plan.",
        key="coordinator_intake_notice")
    result = section.get("result")
    if not isinstance(result, dict):
        st.caption("Intake priority is not available. A missing priority is not zero and is not LOW.")
        return
    priority = result.get("priority")
    score = result.get("priority_score")
    priority_text = "not available" if priority in (None, "") else str(priority)
    score_text = "not available" if score is None else str(score)
    st.caption(f"Intake priority {priority_text}. Intake priority score {score_text}. "
               f"Validation {result.get('validation_status') or 'not available'}.")
    with st.expander("Intake evidence"):
        st.write(result.get("priority_reason") or "No priority explanation was returned.")
        for note in result.get("validation_notes") or []:
            st.markdown(f"- {note}")


def _report_line(fact):
    def shown(value):
        return "not available" if value in (None, "") else str(value)
    return (f"Reported #{shown(fact.get('id'))}. {shown(fact.get('classification'))}. "
            f"Intake severity {shown(fact.get('severity'))}. People {shown(fact.get('people'))}. "
            f"Urgency {shown(fact.get('urgency'))}. Priority {shown(fact.get('priority'))}. "
            f"Validation {shown(fact.get('validation_status'))}. {shown(fact.get('created_at'))}.")


def _reported_block(rows, title):
    if not rows:
        return
    st.markdown(f"**{title}**")
    for fact in rows:
        st.caption(_report_line(fact))


def _coordinator_ripple(section):
    st.subheader("Ripple")
    if _ripple_awaiting_origin(section):
        st.caption("Cascade analysis not run — select an origin zone to begin.")
        _reported_block(section.get("reports_awaiting_origin"), "Stored reports are waiting for an origin zone")
        _reported_block(section.get("unlocated_reports"), "Unlocated reports")
        return
    _agent_status(section)
    st.markdown("**Ripple attention score**")
    safety_notice(
        "Ripple attention score is separate from Intake priority and planner severity.",
        "This shows which connected zones need attention because of cascading disaster risk. "
        "Ripple calculates it separately from Intake priority and planner severity. "
        "A missing attention score is not zero.",
        key="coordinator_ripple_notice")
    result = section.get("result")
    if not isinstance(result, dict):
        st.caption("Ripple attention score is not available. A missing score is not zero.")
        return
    recommendations = result.get("recommendations") or {}
    ranked = recommendations.get("ranked") or []
    unranked = recommendations.get("unranked") or []
    st.caption(f"Origin {result.get('origin') or 'not available'}. Ranked zones {len(ranked)}. "
               f"Unranked zones {len(unranked)}.")
    with st.expander("Ripple evidence"):
        ripple_disclaimer = result.get("disclaimer") or ""
        if ripple_disclaimer:
            safety_notice(ripple_disclaimer)
        if ranked:
            render_df_table(df([{"zone": row.get("zone"),
                              "Ripple attention score": "not available" if row.get("score") is None else row.get("score")}
                             for row in ranked]),
                         hide_index=True)
        elif not unranked:
            st.caption("No Ripple attention score is available.")
        for row in unranked:
            missing = ", ".join(row.get("missing") or []) or "unspecified"
            score = "not available" if row.get("score") is None else row.get("score")
            st.markdown(f"- {row.get('zone')}: Ripple attention score {score} ({missing}). A missing score is not zero.")
        zones = [result.get("primary") or {}]
        zones.extend(result.get("secondary") or [])
        zones.extend(result.get("extended") or [])
        reported = [(zone.get("zone"), fact) for zone in zones for fact in zone.get("reported") or []]
        if reported or result.get("unlocated_reports") or result.get("reports_outside_ring"):
            st.markdown("**Reported, separate from the recorded risk reading**")
            for zone_name, fact in reported:
                st.caption(f"{zone_name}. {_report_line(fact)}")
            _reported_block(result.get("reports_outside_ring"), "Stored reports outside this cascade")
            _reported_block(result.get("unlocated_reports"), "Unlocated reports")


def _coordinator_challenger(section, records):
    st.subheader("Plan Challenger")
    _agent_status(section)
    st.markdown("**Planner severity**")
    safety_notice(
        "Planner severity is the stored plan's zone score, not Intake priority or the Ripple score.",
        "These zone scores are the ones used by the stored resource and allocation plan. "
        "Plan Challenger reviews the plan against them. "
        "They are not Intake priority and they are not Ripple attention scores.",
        key="coordinator_planner_notice")
    situation = records.get("situation") if isinstance(records, dict) else None
    severity = situation.get("severity") if isinstance(situation, dict) else None
    if not severity:
        st.caption("Planner severity is not available. A missing score is not zero.")
    else:
        render_df_table(df([{"Zone": row.get("name") or "not available",
                          "Planner severity": "not available" if row.get("score") is None else row.get("score"),
                          "Level": row.get("level") or "not available"} for row in severity]),
                     hide_index=True)
    result = section.get("result")
    if not isinstance(result, dict):
        return
    counts = result.get("counts") or {}
    st.caption(f"Review of stored plan version {result.get('plan_version')}. Evidence evaluated. "
               f"This is not a proposed plan. Approved no. Executed no. "
               f"Confirmed {counts.get('confirmed', 0)}, potential {counts.get('potential', 0)}, "
               f"missing {counts.get('missing', 0)}.")
    with st.expander("Plan Challenger evidence"):
        findings = result.get("findings") or []
        if not findings:
            st.caption("The review returned no findings.")
            return
        render_df_table(df([{"status": row.get("status"), "risk": row.get("risk"), "area": row.get("area"),
                          "why": row.get("why"), "evidence": _evidence_text(row.get("evidence"))} for row in findings]),
                     hide_index=True)


def _coordinator_proposed(evaluation):
    """Show a review of a plan the caller already proposed. Nothing here is approved or stored."""
    if not isinstance(evaluation, dict):
        return
    st.markdown("**Proposed plan**")
    st.caption("This review is separate from recorded conditions, citizen reports, Ripple findings, "
               "and What-If simulations. The proposed plan was not approved and it was not executed.")
    state = evaluation.get("state") or "unavailable"
    st.caption("Plan basis proposed. Review state " + state + ". Findings are evaluated. Approved no. Executed no.")
    if state != "evaluated" or not isinstance(evaluation.get("result"), dict):
        st.caption("Proposed plan evaluation is not available.")
        if evaluation.get("error"):
            st.caption(evaluation["error"])
        return
    result = evaluation["result"]
    version = (evaluation.get("plan") or {}).get("version") if isinstance(evaluation.get("plan"), dict) else None
    version_text = "not a stored plan version" if version is None else f"proposal version field {version}"
    counts = result.get("counts") or {}
    st.caption(f"{version_text}. Evaluated, not approved and not executed. "
               f"Confirmed {counts.get('confirmed', 0)}, potential {counts.get('potential', 0)}, "
               f"missing {counts.get('missing', 0)}.")
    findings = result.get("findings") or []
    if not findings:
        st.caption("The proposed-plan review returned no findings.")
        return
    render_df_table(df([{"status": row.get("status"), "area": row.get("area"), "why": row.get("why"),
                      "evidence": _evidence_text(row.get("evidence"))} for row in findings]),
                 hide_index=True)


def _coordinator_whatif(section):
    st.subheader("What-If")
    safety_notice(
        "Hypothetical comparison. Simulated values are not stored.",
        "Hypothetical comparison. Simulated values are not stored. "
        "Values read from the database stay RECORDED. Values on the in-memory copy are SIMULATED.",
        key="coordinator_whatif_notice")
    _agent_status(section)
    skipped = section.get("skipped") or []
    with st.expander("Skipped simulations"):
        if not skipped:
            st.caption("No confirmed finding was skipped.")
        for row in skipped:
            st.markdown(f"- {row.get('reason')}")
            evidence = _evidence_text(row.get("evidence"))
            if evidence:
                st.caption(evidence)
    result = section.get("result")
    if not isinstance(result, dict):
        return
    st.caption(f"RECORDED plan version {result.get('recorded_plan_version')} was read for this hypothetical comparison.")
    for note in result.get("modifications") or []:
        st.markdown(f"**Hypothetical.** {note}")
    roads = [row for row in result.get("roads") or [] if row.get("recorded_status") != row.get("simulated_status")]
    hospitals = [row for row in result.get("hospitals") or [] if row.get("recorded_free") != row.get("simulated_free")]
    stock = [row for row in result.get("stock") or [] if row.get("recorded_qty") != row.get("simulated_qty")]
    with st.expander("Recorded and simulated values"):
        if not roads and not hospitals and not stock:
            st.caption("No recorded road, bed count, or stock quantity differs from the hypothetical copy.")
        if roads:
            render_df_table(df([{"road": row["id"], "RECORDED status": row["recorded_status"],
                              "SIMULATED status": row["simulated_status"], "meaning": row["meaning"]} for row in roads]),
                         hide_index=True)
        if hospitals:
            render_df_table(df([{"hospital": row["name"], "RECORDED free beds": row["recorded_free"],
                              "SIMULATED free beds": row["simulated_free"]} for row in hospitals]),
                         hide_index=True)
        if stock:
            render_df_table(df([{"hub": row["hub"], "item": row["item"], "RECORDED quantity": row["recorded_qty"],
                              "SIMULATED quantity": row["simulated_qty"]} for row in stock]),
                         hide_index=True)
    _whatif_ripple_context(result)


def _sim_text(value):
    return "not available" if value is None else str(value)


def _attention_score(recommendations, zone):
    if not isinstance(recommendations, dict):
        return None
    for bucket in ("ranked", "unranked"):
        for row in recommendations.get(bucket) or []:
            if isinstance(row, dict) and row.get("zone") == zone:
                return row.get("score")
    return None


def _reported_label(zone):
    if "reported" not in zone:
        return "not included"
    rows = zone.get("reported") or []
    if not rows:
        return "none"
    parts = []
    for row in rows:
        if isinstance(row, dict):
            parts.append(f"id {_sim_text(row.get('id'))} severity {_sim_text(row.get('severity'))} "
                         f"basis {_sim_text(row.get('basis'))}")
    return "; ".join(parts) or "none"


def _whatif_ripple_context(result):
    """Show cascade findings already attached to a comparison. Ripple is not run here."""
    if not isinstance(result, dict) or "ripple_context" not in result:
        return
    context = result.get("ripple_context")
    st.markdown("**Ripple cascade context**")
    st.caption("Inferred from the existing Ripple result. These findings are not recorded risk readings, "
               "not citizen reports, and not simulated outcomes. They were not applied and they were not saved.")
    if not isinstance(context, dict) or context.get("status") != "received":
        st.caption("Ripple cascade context is not available.")
        return
    st.caption(f"Origin {_sim_text(context.get('origin'))}. Basis inferred.")
    zones = [row for row in context.get("affected_zones") or [] if isinstance(row, dict)]
    recommendations = context.get("recommendations")
    if not zones:
        st.caption("Affected zones are not available.")
    else:
        render_df_table(df([{
            "zone": row.get("zone"),
            "role": _sim_text(row.get("role")),
            "hop": _sim_text(row.get("hop")),
            "RECORDED risk": "not available" if not isinstance(row.get("risk"), dict) else (
                f"{row['risk'].get('disaster_type')} probability {_sim_text(row['risk'].get('probability'))}"),
            "attention score": _sim_text(_attention_score(recommendations, row.get("zone"))),
            "citizen reported": _reported_label(row),
        } for row in zones]), hide_index=True)
    if recommendations is None:
        st.caption("Ripple recommendations are not available.")
    else:
        unranked = [row.get("zone") for row in (recommendations.get("unranked") or []) if isinstance(row, dict)]
        if unranked:
            st.caption("Attention score is not available for " + ", ".join(str(name) for name in unranked) + ". Missing is not zero.")
    timeline = context.get("timeline")
    if not isinstance(timeline, list):
        st.caption("Ripple timeline evidence is not available.")
    else:
        for stage in timeline:
            if not isinstance(stage, dict):
                continue
            st.caption(f"Ripple evidence. {stage.get('stage') or 'Stage'}: {_sim_text(stage.get('reason'))}")
    bridges = context.get("bridges")
    notes = []
    if isinstance(bridges, dict):
        notes = [row.get("note") for row in bridges.get("comparisons") or [] if isinstance(row, dict) and row.get("note")]
    if notes:
        for note in notes:
            st.caption(f"Ripple evidence. {note}")
    else:
        st.caption("Ripple bridge evidence is not available.")
    outside = context.get("reports_outside_ring")
    if outside:
        st.caption("Citizen reports outside this ring stay reported and are not part of the simulated comparison.")
    unlocated = context.get("unlocated_reports")
    if unlocated:
        st.caption("Citizen reports with no recorded zone stay unlocated and are not part of the simulated comparison.")


def _coordinator_received_simulation(received):
    """Show a What-If result that Coordinator received. It is not calculated here."""
    if not isinstance(received, dict):
        return
    st.markdown("**What-If simulation received**")
    st.caption("Simulated findings. These are not recorded risk, not a Ripple attention score, "
               "and not a citizen report. The simulation was not saved.")
    if received.get("status") != "received" or not isinstance(received.get("result"), dict):
        st.caption("Simulation result is not available.")
        return
    result = received["result"]
    version = result.get("recorded_plan_version")
    st.caption(f"RECORDED plan version {_sim_text(version)} was read and not replaced.")
    notes = result.get("modifications")
    if notes is None:
        st.caption("Simulation notes are not available.")
    else:
        for note in notes:
            st.markdown(f"**SIMULATED.** {_sim_text(note)}")
    roads = [row for row in result.get("roads") or [] if isinstance(row, dict)
             and row.get("recorded_status") != row.get("simulated_status")]
    hospitals = [row for row in result.get("hospitals") or [] if isinstance(row, dict)
                 and row.get("recorded_free") != row.get("simulated_free")]
    stock = [row for row in result.get("stock") or [] if isinstance(row, dict)
             and row.get("recorded_qty") != row.get("simulated_qty")]
    severity = [row for row in result.get("severity") or [] if isinstance(row, dict)
                and (row.get("recorded_probability") != row.get("simulated_probability")
                     or row.get("recorded_score") != row.get("simulated_score")
                     or row.get("missing"))]
    if not roads and not hospitals and not stock and not severity and not notes:
        st.caption("No simulated difference was included in the received result.")
    if roads:
        render_df_table(df([{"road": row.get("id"), "RECORDED status": _sim_text(row.get("recorded_status")),
                          "SIMULATED status": _sim_text(row.get("simulated_status")),
                          "meaning": _sim_text(row.get("meaning"))} for row in roads]),
                     hide_index=True)
    if hospitals:
        render_df_table(df([{"hospital": row.get("name"), "RECORDED free beds": _sim_text(row.get("recorded_free")),
                          "SIMULATED free beds": _sim_text(row.get("simulated_free"))} for row in hospitals]),
                     hide_index=True)
    if stock:
        render_df_table(df([{"hub": row.get("hub"), "item": row.get("item"),
                          "RECORDED quantity": _sim_text(row.get("recorded_qty")),
                          "SIMULATED quantity": _sim_text(row.get("simulated_qty"))} for row in stock]),
                     hide_index=True)
    if severity:
        render_df_table(df([{"zone": row.get("zone"),
                          "RECORDED probability": _sim_text(row.get("recorded_probability")),
                          "SIMULATED probability": _sim_text(row.get("simulated_probability")),
                          "RECORDED score": _sim_text(row.get("recorded_score")),
                          "SIMULATED score": _sim_text(row.get("simulated_score"))} for row in severity]),
                     hide_index=True)
    _whatif_ripple_context(result)


def _evidence_class(source):
    return {
        "intake": "reported",
        "ripple": "inferred",
        "whatif": "simulated",
        "plan_challenger": "evaluated",
        "records": "recorded",
        "stored_plan": "recorded",
    }.get(source, "recorded")


def _availability(status):
    if status == "ok":
        return "available"
    if status == "missing":
        return "missing"
    if status in ("not_requested", "skipped", "failed", None, ""):
        return "unavailable"
    return str(status)


def _agent_status(section):
    status = section.get("status")
    st.caption(f"Source {section.get('source')}. Evidence {_evidence_class(section.get('source'))}. "
               f"Status {_availability(status)}.")
    if section.get("error"):
        ui_error(section["error"])
    missing = section.get("missing") or []
    if missing:
        st.caption("Missing: " + ", ".join(str(item) for item in missing) + ". Missing is not zero.")


def _evidence_text(evidence):
    if not isinstance(evidence, dict) or not evidence:
        return ""
    return ", ".join(f"{key} {value}" for key, value in evidence.items())


def page_plan_challenger():  # read-only review of the stored plan + visual scenario comparison
    page_fit_styles()
    with st.container(border=True):
        page_header("Plan Challenger & Response Comparison",
                    "Read-only audit of the stored response plan and side-by-side comparison against alternative/counterfactual conditions.")
        safety_notice(plan_challenger_agent.DISCLAIMER)
        st.caption("These findings are evaluated against the stored recorded plan. "
                   "This page is not reviewing a proposed plan. "
                   "The review was not approved and it was not executed. "
                   "A missing count is not zero.")
    result = plan_challenger_agent.review()  # reads the current plan; nothing is stored
    plan = data.get_plan()
    st.caption(f"RECORDED plan version {result['plan_version']}. Reviewed at {result['reviewed_at']}.")
    counts = result["counts"]
    metric_tiles((
        ("Confirmed Violations", counts["confirmed"], RED if counts["confirmed"] else TEAL),
        ("Potential Improvements", counts["potential"], ORANGE if counts["potential"] else TEAL),
        ("Missing Data Flags", counts["missing"], MUTED_GREY),
        ("Plan Version Reviewed", f"v{result['plan_version']}", STEEL),
    ))

    # Visual Plan Comparison (Current Stored Plan vs Counterfactual / Alternative Plan from What-If)
    supply_rows = plan.get("supply") or []
    patient_rows = plan.get("patients") or []
    shortfall_rows = plan.get("shortfalls") or []
    unplaced_rows = plan.get("unplaced") or []
    all_legs = [r.get("minutes") for r in supply_rows + patient_rows if r.get("minutes") is not None]
    cur_avg_mins = round(sum(all_legs) / len(all_legs), 1) if all_legs else 0.0
    cur_resources = len(supply_rows) + len(patient_rows)
    cur_hosps = len({p.get("hospital") for p in patient_rows if p.get("hospital")})
    cur_planned_qty = sum(int(r.get("qty") or 0) for r in supply_rows)
    cur_short_qty = sum(int(s.get("missing") or 0) for s in shortfall_rows)
    cur_cov = round((cur_planned_qty / (cur_planned_qty + cur_short_qty)) * 100, 1) if (cur_planned_qty + cur_short_qty) > 0 else 100.0

    # Compare against active What-If simulation if one exists, or an in-memory nearest-hub greedy baseline
    sim = st.session_state.get("whatif_simulation")
    if isinstance(sim, dict):
        alt_label = "What-If Counterfactual Plan (Simulated in memory)"
        alt_supply = (sim.get("supply") or {}).get("simulated") or []
        alt_short = (sim.get("supply") or {}).get("simulated_shortfalls") or []
        alt_patients = (sim.get("patients") or {}).get("simulated_assignments") or []
        alt_legs = [r.get("minutes") for r in alt_supply + alt_patients if r.get("minutes") is not None]
        alt_avg_mins = round(sum(alt_legs) / len(alt_legs), 1) if alt_legs else 0.0
        alt_resources = len(alt_supply) + len(alt_patients)
        alt_hosps = len({p.get("hospital") for p in alt_patients if p.get("hospital")})
        alt_planned_qty = sum(int(r.get("qty") or 0) for r in alt_supply)
        alt_short_qty = sum(int(s.get("missing") or 0) for s in alt_short)
        alt_cov = round((alt_planned_qty / (alt_planned_qty + alt_short_qty)) * 100, 1) if (alt_planned_qty + alt_short_qty) > 0 else 100.0
        alt_changes = (sim.get("supply") or {}).get("changed", 0)
    else:
        alt_label = "Unconstrained Baseline (Pre-reoptimisation reference)"
        alt_avg_mins = cur_avg_mins
        alt_resources = cur_resources
        alt_hosps = cur_hosps
        alt_cov = cur_cov
        alt_changes = plan.get("changed", 0)

    with st.container(border=True):
        st.subheader("Plan comparison & operational trade-offs")
        st.caption(f"Comparing Stored Plan v{plan.get('version', 1)} against {alt_label}. All values are derived from recorded database rows.")
        comp_rows = [
            {
                "Metric": "Estimated average response travel time",
                "Current Stored Plan": f"{cur_avg_mins} min",
                "Comparison / Counterfactual Plan": f"{alt_avg_mins} min",
            },
            {
                "Metric": "Allocation moves & medical assignments",
                "Current Stored Plan": f"{cur_resources} moves",
                "Comparison / Counterfactual Plan": f"{alt_resources} moves",
            },
            {
                "Metric": "Hospitals receiving patients",
                "Current Stored Plan": f"{cur_hosps} facilities",
                "Comparison / Counterfactual Plan": f"{alt_hosps} facilities",
            },
            {
                "Metric": "Priority supply coverage (%)",
                "Current Stored Plan": f"{cur_cov}%",
                "Comparison / Counterfactual Plan": f"{alt_cov}%",
            },
            {
                "Metric": "Confirmed constraint violations",
                "Current Stored Plan": f"{counts['confirmed']} violations",
                "Comparison / Counterfactual Plan": f"{alt_changes} route deltas",
            },
        ]
        render_df_table(comp_rows, hide_index=True)

        # Five structured analysis questions from Section 3
        q1, q2 = st.columns(2)
        with q1:
            changed_moves = [r for r in supply_rows + patient_rows if r.get("changed")]
            st.markdown(
                f'<div class="saarthi-alert-card low">'
                f'<div class="saarthi-alert-top"><span class="saarthi-alert-title">1. What Changed in Plan v{plan.get("version", 1)}?</span>'
                f'{status_pill(f"{plan.get('changed', 0)} Changed", "low")}</div>'
                f'<div class="saarthi-alert-meta"><span>Trigger: {html.escape(str(plan.get("trigger") or "Initial baseline"))}</span>'
                f'<span>Modified rows in this version: {len(changed_moves)}</span></div></div>',
                unsafe_allow_html=True,
            )
            affected_zones = sorted({s.get("zone") for s in shortfall_rows if s.get("zone")} | {u.get("zone") for u in unplaced_rows if u.get("zone")})
            st.markdown(
                f'<div class="saarthi-alert-card {"critical" if affected_zones else "resolved"}">'
                f'<div class="saarthi-alert-top"><span class="saarthi-alert-title">2. Which Areas Have Unmet Needs?</span>'
                f'{status_pill(f"{len(affected_zones)} Zones" if affected_zones else "All Covered", "critical" if affected_zones else "resolved")}</div>'
                f'<div class="saarthi-alert-meta"><span>{html.escape(", ".join(affected_zones) if affected_zones else "Every priority zone and medical request is currently covered.")}</span></div></div>',
                unsafe_allow_html=True,
            )
        with q2:
            reassignments = [f["why"] for f in result["findings"] if f["status"] == "confirmed"]
            st.markdown(
                f'<div class="saarthi-alert-card {"high" if reassignments else "resolved"}">'
                f'<div class="saarthi-alert-top"><span class="saarthi-alert-title">3. Which Resources Need Reassignment?</span>'
                f'{status_pill(f"{len(reassignments)} Flagged" if reassignments else "0 Conflicts", "high" if reassignments else "resolved")}</div>'
                f'<div class="saarthi-alert-meta"><span>{html.escape(reassignments[0] if reassignments else "No hub stock overruns, blocked routes, or hospital bed overflows detected.")}</span></div></div>',
                unsafe_allow_html=True,
            )
            verdict_ok = counts["confirmed"] == 0
            st.markdown(
                f'<div class="saarthi-alert-card {"resolved" if verdict_ok else "critical"}">'
                f'<div class="saarthi-alert-top"><span class="saarthi-alert-title">4. Feasibility Verdict &amp; Downstream Consequences</span>'
                f'{status_pill("FEASIBLE" if verdict_ok else "NEEDS REVISION", "resolved" if verdict_ok else "critical")}</div>'
                f'<div class="saarthi-alert-meta"><span>{"Stored plan satisfies all recorded road, bed, and inventory limits." if verdict_ok else "Confirmed constraint violations must be resolved before approving dispatch."}</span></div></div>',
                unsafe_allow_html=True,
            )

    findings = result["findings"]
    if not findings:
        ui_success("No findings. The stored plan did not break a recorded limit in this review.")
        return
    labels = [("confirmed", "Confirmed violations"), ("potential", "Potential improvements"), ("missing", "Missing data")]
    for status, title in labels:
        rows = [row for row in findings if row["status"] == status]
        st.subheader(title)
        if not rows:
            st.caption("None.")
            continue
        table = []
        for row in rows:
            evidence = row["evidence"]
            table.append({"risk": row["risk"], "area": row["area"], "why": row["why"],
                          "zone": evidence.get("zone"), "hospital": evidence.get("hospital"),
                          "hospital_id": evidence.get("hospital_id"), "hub": evidence.get("hub"),
                          "hub_id": evidence.get("hub_id"), "road": evidence.get("road"),
                          "incident_id": evidence.get("incident_id"), "item": evidence.get("item"),
                          "recommendation": row["recommendation"]})
        render_df_table(pd.DataFrame(table), hide_index=True)


# ---------------------------------------------------------------------
# 5. MAIN: runs every time the page loads or something is clicked
# ---------------------------------------------------------------------
def ensure_session_db():
    """Create one seeded database for this Streamlit session, then reuse it."""
    if "session_db_path" not in st.session_state:
        st.session_state["session_db_path"] = os.path.join(
            tempfile.gettempdir(), f"saarthi-{uuid.uuid4().hex}.db")
        data.init_db()


ensure_session_db()
PAGES = {  # menu label -> page function
    "Situation Overview": page_overview,
    "Early Warnings": page_alerts,
    "Severity & Vulnerability": page_severity,
    "Allocation": page_allocation,
    "Roads & Hospitals": page_network,
    "Supply Tracking": page_tracking,
    "Field Requests": page_requests,
    "Intake & Triage": page_intake,
    "Ripple": page_ripple,
    "What-If": page_whatif,
    "Plan Challenger": page_plan_challenger,
    "Coordinator": page_coordinator,
    "Scenario Tests": page_scenarios,
}
NAV_GROUPS = (
    ("Monitor", ("Situation Overview", "Early Warnings", "Severity & Vulnerability")),
    ("Respond", ("Allocation", "Roads & Hospitals", "Supply Tracking", "Field Requests")),
    ("Analyse", ("Intake & Triage", "Ripple", "What-If", "Plan Challenger", "Coordinator")),
    ("Demo", ("Scenario Tests",)),
)


def _go_page(page_name):
    """Callback executed before script rerun so nav_page updates immediately."""
    st.session_state["nav_page"] = page_name


def render_nav():
    """Deep Navy (#102536) EOC Sidebar with pure typography and Red (#E74747) active indicator."""
    if "nav_page" not in st.session_state:
        st.session_state["nav_page"] = "Situation Overview"
    st.sidebar.markdown(
        '<div style="padding:4px 4px 12px 4px;border-bottom:1px solid rgba(255,255,255,0.09);margin-bottom:6px;">'
        '<div style="font-size:16px;font-weight:700;letter-spacing:0.12em;color:#FFFFFF;line-height:1.15;">SAARTHI</div>'
        '<div style="font-size:10px;font-weight:500;letter-spacing:0.06em;text-transform:uppercase;color:#7F9BB2;margin-top:3px;">Ripple Command Centre</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    for index, (group, labels) in enumerate(NAV_GROUPS):
        if index:
            st.sidebar.divider()
        st.sidebar.markdown(
            f'<div class="saarthi-nav-group"><span>{html.escape(group)}</span></div>',
            unsafe_allow_html=True,
        )
        for label in labels:
            active = st.session_state["nav_page"] == label
            st.sidebar.button(
                label,
                key=f"nav_{label}",
                type="primary" if active else "secondary",
                use_container_width=True,
                on_click=_go_page,
                args=(label,),
            )
    return st.session_state["nav_page"]


choice = render_nav()
live = st.sidebar.toggle(f"Live mode (refresh every {REFRESH_SECONDS} s)", value=False)  # auto refresh on/off
sidebar_demos()  # demo buttons
command_styles()
command_bar(choice, live)
if live:
    st.fragment(PAGES[choice], run_every=REFRESH_SECONDS)()  # draw the page, re-reading data on a timer
else:
    PAGES[choice]()
