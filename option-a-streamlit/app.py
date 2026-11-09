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
LEVEL_HEX = {"Critical": "#D9483B", "High": "#E58A35", "Moderate": "#5C7380", "Low": "#238C78"}  # level colours for charts
LEVEL_RGB = {"Critical": [217, 72, 59], "High": [229, 138, 53], "Moderate": [92, 115, 128], "Low": [35, 140, 120]}  # same, for the map
ROAD_RGB = {"OPEN": [117, 129, 138], "SLOW": [229, 138, 53], "BLOCKED": [217, 72, 59]}  # grey, orange, red
SOS_RGB = {"OPEN": [217, 72, 59], "IN_TRANSIT": [229, 138, 53], "RESOLVED": [35, 140, 120]}  # SOS dot colours
# Emergency-operations palette. Names stay so the existing layout CSS still resolves.
NAVY = "#142B3B"
BLUE = "#5C7380"
GREEN = "#238C78"
CRIMSON = "#D9483B"
AMBER = "#E58A35"
IVORY = "#F3F5F6"
GOLD = "#E1E6E9"
GOLD_LIGHT = "#C5D0D6"
EYEBROW = "#75818A"
CREAM = "#F3F5F6"
NAVY_MUTED = "#C5D0D6"
CANVAS = IVORY
RAISED = "#FFFFFF"
INSET = "#E7ECEE"
SIDEBAR = NAVY
LINE = "#E1E6E9"
INPUT_LINE = "#D5DDE1"
TEXT = "#263238"
MUTED = "#75818A"
WARNING = "#E58A35"  # pending attention; not a severity level
WARNING_BORDER = "#E8C49A"
WARNING_BG = "#FBF3EA"
EVENT_ICON = {"ROAD_BLOCKED": "⛔", "ROAD_SLOW": "🐢", "ROAD_REOPENED": "✅", "HOSPITAL_CAPACITY_CHANGED": "🏥",
              "NEW_PATIENTS": "🚑", "RESOURCE_RECEIVED": "📦", "RESOURCE_DEPLETED": "⚠️", "REOPTIMIZED": "🔁",
              "RISK_UPDATED": "🌀", "TAMPER": "🚨", "INTAKE_SUBMITTED": "📥", "INTAKE_APPROVED": "✅",
              "INTAKE_REJECTED": "🚫"}  # small symbol per event type in the feed

st.set_page_config(
    page_title="Saarthi – Emergency Command Center",
    page_icon="🚨",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------
# 2. SMALL HELPERS
# ---------------------------------------------------------------------
def df(rows):  # list of dicts -> pandas table
    return pd.DataFrame(rows)  # pandas does the conversion


def render_df_table(data_obj, hide_index=True, target=None):
    """Render a DataFrame or list of dicts as a crisp, high-contrast .saarthi-scan HTML table."""
    target = st if target is None else target
    frame = data_obj if isinstance(data_obj, pd.DataFrame) else pd.DataFrame(data_obj)
    if frame.empty:
        target.caption("No records to display.")
        return
    if not hide_index and frame.index.name:
        frame = frame.reset_index()
    elif not hide_index and not isinstance(frame.index, pd.RangeIndex):
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
    """Render a high-contrast light callout box so Streamlit dark-mode stAlert never hides text."""
    target = st if target is None else target
    icons = {"info": "i", "success": "✓", "warning": "!", "error": "✕"}
    labels = {"info": "Notice", "success": "Verified", "warning": "Attention", "error": "Alert"}
    mark = icons.get(kind, "i")
    label = labels.get(kind, "Notice")
    target.markdown(
        f'<div class="saarthi-callout saarthi-callout-{html.escape(kind)}">'
        f'<span class="saarthi-callout-mark" aria-hidden="true">{mark}</span>'
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
    """Shared page heading: gold eyebrow, Playfair Display title, and one description line."""
    st.markdown(
        '<div class="saarthi-heading">'
        '<div class="eb">SAARTHI · COMMAND CENTER MODULE</div>'
        f'<div class="saarthi-title">{title}</div>'
        f'<div class="saarthi-description">{html.escape(description)}</div>'
        "</div>",
        unsafe_allow_html=True)


def safety_notice(summary, detail=None, *, tone="info", target=None, key=None):
    """Compact safety line. Any longer original wording stays in the expander."""
    target = st if target is None else target
    kind = "caution" if tone == "caution" else "info"
    mark = "!" if tone == "caution" else "i"
    target.markdown(
        f'<div class="saarthi-notice saarthi-notice-{kind}">'
        f'<span class="saarthi-notice-mark" aria-hidden="true">{mark}</span>'
        f"<span>{html.escape(summary)}</span>"
        "</div>",
        unsafe_allow_html=True)
    extra = (detail or "").strip()
    if extra and extra != summary.strip():
        with target.expander("Full notice", key=key):
            target.markdown(html.escape(extra))


def metric_tiles(items):
    """Compact status tiles. items are (label, value, value colour)."""
    cards = []
    for label, value, colour in items:
        tone = colour if colour != TEXT else NAVY
        cards.append(
            '<div class="saarthi-metric">'
            f'<div class="saarthi-metric-value" style="color:{tone};">{value}</div>'
            f'<div class="saarthi-metric-label">{label}</div>'
            "</div>"
        )
    st.markdown('<div class="saarthi-metrics">' + "".join(cards) + "</div>", unsafe_allow_html=True)


def style_chart(fig):
    """Editorial white & ivory chart chrome with Playfair/Inter typography."""
    fig.update_layout(
        paper_bgcolor=RAISED,
        plot_bgcolor=RAISED,
        font=dict(family="Inter, sans-serif", color=TEXT, size=12),
        legend=dict(
            font=dict(family="Inter, sans-serif", color=TEXT, size=11),
            bgcolor="rgba(250,247,240,0.92)",
            bordercolor=LINE,
            borderwidth=1,
        ),
        margin=dict(t=10, b=10, l=10, r=10),
    )
    fig.update_xaxes(
        gridcolor=LINE, linecolor=LINE, zerolinecolor=LINE,
        tickfont=dict(family="Inter, sans-serif", color=MUTED, size=11),
        title_font=dict(family="Inter, sans-serif", color=MUTED, size=11),
    )
    fig.update_yaxes(
        gridcolor=LINE, linecolor=LINE, zerolinecolor=LINE,
        tickfont=dict(family="Inter, sans-serif", color=TEXT, size=11),
        title_font=dict(family="Inter, sans-serif", color=MUTED, size=11),
    )
    return fig


def command_styles():
    """Exact visual styling from Saarthi – Emergency Command Center.html with complete light-surface overrides."""
    st.markdown(
        f"""<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Playfair+Display:wght@500;600;700&family=Noto+Sans+Devanagari:wght@400;600&display=swap" rel="stylesheet"><style>
        :root {{ --nv: {NAVY}; --iv: {IVORY}; --gd: {GOLD}; --ink: {TEXT}; --mu: {MUTED}; --hl: {LINE}; --cr: #D9483B; --am: #E58A35; --bl: #5C7380; --gn: #238C78; --primary-color: {NAVY}; --background-color: #FFFFFF; --secondary-background-color: {IVORY}; --text-color: {TEXT}; --saarthi-canvas: {CANVAS}; --saarthi-raised: {RAISED}; --saarthi-inset: {INSET}; --saarthi-sidebar: {SIDEBAR}; --saarthi-line: {LINE}; --saarthi-text: {TEXT}; --saarthi-muted: {MUTED}; --saarthi-warning: {WARNING}; color-scheme: light !important; }}
        html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"], .main {{ background: {IVORY} !important; background-color: {IVORY} !important; color: {TEXT} !important; font: 14px/1.45 'Inter', sans-serif !important; font-variant-numeric: tabular-nums; color-scheme: light !important; }}
        [data-testid="stDeployButton"], .stAppDeployButton {{ display: none !important; }}
        header[data-testid="stHeader"] {{ background: transparent !important; }}
        [data-testid="collapsedControl"], [data-testid="stSidebarCollapsedControl"] button {{ background: {NAVY} !important; color: {CREAM} !important; border: 1px solid {LINE} !important; border-radius: 0px !important; }}
        [data-testid="collapsedControl"] svg, [data-testid="stSidebarCollapsedControl"] svg {{ fill: {CREAM} !important; color: {CREAM} !important; }}
        .block-container {{ padding-top: 0.5rem !important; padding-bottom: 2rem !important; max-width: 1440px !important; }}
        [data-testid="stSidebar"], [data-testid="stSidebar"] > div {{ background: {NAVY} !important; background-color: {NAVY} !important; border-right: none !important; color: {CREAM} !important; }}
        [data-testid="stSidebar"] .block-container {{ padding-top: 1rem !important; }}
        [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {{ font-family: 'Playfair Display', serif !important; color: {CREAM} !important; letter-spacing: 0.14em !important; }}
        [data-testid="stSidebar"] p, [data-testid="stSidebar"] span, [data-testid="stSidebar"] label, [data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {{ color: {CREAM} !important; }}
        [data-testid="stSidebar"] hr {{ border-color: rgba(255, 255, 255, 0.16) !important; }}
        [data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {{ font: 600 10.5px 'Inter', sans-serif !important; letter-spacing: 0.16em !important; text-transform: uppercase !important; color: {GOLD_LIGHT} !important; line-height: 1.3 !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button {{ min-height: 38px !important; padding: 8px 14px !important; font: 600 11.5px 'Inter', sans-serif !important; letter-spacing: 0.06em !important; text-align: left !important; border-radius: 0px !important; border: 1px solid rgba(255, 255, 255, 0.18) !important; background: rgba(255, 255, 255, 0.04) !important; color: {CREAM} !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button:hover {{ background: {WARNING} !important; color: {NAVY} !important; border-color: {WARNING} !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button:hover p, [data-testid="stSidebar"] [data-testid="stButton"] button:hover span {{ color: {NAVY} !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"] {{ background: {WARNING} !important; color: {NAVY} !important; border: 1px solid {WARNING} !important; border-left: 4px solid {CRIMSON} !important; font-weight: 600 !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"] p, [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"] span {{ color: {NAVY} !important; font-weight: 600 !important; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="secondary"] p, [data-testid="stSidebar"] [data-testid="stButton"] button[kind="secondary"] span {{ color: {CREAM} !important; }}
        [data-testid="stSidebar"] .saarthi-notice {{ background: #16294D !important; border-color: {WARNING} !important; border-left: 3px solid {WARNING} !important; color: {CREAM} !important; }}
        [data-testid="stSidebar"] .saarthi-notice span {{ color: {CREAM} !important; }}
        [data-testid="stHeading"] {{ margin-bottom: 0.25rem; }}
        [data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3 {{ font-family: 'Playfair Display', serif !important; font-weight: 600 !important; color: {NAVY} !important; letter-spacing: 0 !important; }}
        [data-testid="stHeading"] h2 {{ font-size: 22px !important; }}
        [data-testid="stHeading"] h3 {{ font-size: 18px !important; }}
        [data-testid="stVerticalBlock"] {{ gap: 0.65rem; }}
        [data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li {{ margin-bottom: 0 !important; color: {TEXT}; }}
        [data-testid="stMarkdownContainer"] code, .stApp code {{ background: #F3F5F6 !important; color: {NAVY} !important; border: 1px solid {LINE} !important; padding: 1px 5px !important; border-radius: 0px !important; font-size: 12px !important; }}
        [data-testid="stCaptionContainer"] p {{ font-size: 12px !important; line-height: 1.45 !important; color: {MUTED} !important; }}
        [data-testid="stVerticalBlockBorderWrapper"], [data-testid="stVerticalBlock"].st-emotion-cache-pb8mwp, div[data-testid="stForm"] {{ background: #FFFFFF !important; background-color: #FFFFFF !important; border: 1px solid {LINE} !important; border-top: 3px solid {LINE} !important; border-radius: 0px !important; padding: 18px 20px !important; }}
        .stMain [data-testid="stButton"] button, .stMain [data-testid="stFormSubmitButton"] button {{ border-radius: 0px !important; min-height: 38px !important; padding: 6px 14px !important; font: 600 11.5px 'Inter', sans-serif !important; letter-spacing: 0.05em !important; border: 1px solid {LINE} !important; background: #FFFFFF !important; background-color: #FFFFFF !important; color: {NAVY} !important; white-space: nowrap !important; }}
        .stMain [data-testid="stButton"] button p, .stMain [data-testid="stButton"] button span, .stMain [data-testid="stFormSubmitButton"] button p, .stMain [data-testid="stFormSubmitButton"] button span {{ color: {NAVY} !important; white-space: nowrap !important; }}
        .stMain [data-testid="stButton"] button:hover, .stMain [data-testid="stFormSubmitButton"] button:hover {{ background: {WARNING} !important; background-color: {WARNING} !important; color: {NAVY} !important; }}
        .stMain [data-testid="stButton"] button[kind="primary"], .stMain [data-testid="stFormSubmitButton"] button[kind="primary"] {{ background: {NAVY} !important; background-color: {NAVY} !important; color: {CREAM} !important; border: 1px solid {NAVY} !important; border-top: 2px solid {WARNING} !important; }}
        .stMain [data-testid="stButton"] button[kind="primary"] p, .stMain [data-testid="stButton"] button[kind="primary"] span, .stMain [data-testid="stFormSubmitButton"] button[kind="primary"] p, .stMain [data-testid="stFormSubmitButton"] button[kind="primary"] span {{ color: {CREAM} !important; }}
        .stMain [data-testid="stButton"] button[kind="primary"]:hover, .stMain [data-testid="stFormSubmitButton"] button[kind="primary"]:hover {{ background: {WARNING} !important; background-color: {WARNING} !important; color: {NAVY} !important; border-color: {WARNING} !important; }}
        .stMain [data-testid="stButton"] button[kind="primary"]:hover p, .stMain [data-testid="stButton"] button[kind="primary"]:hover span, .stMain [data-testid="stFormSubmitButton"] button[kind="primary"]:hover p, .stMain [data-testid="stFormSubmitButton"] button[kind="primary"]:hover span {{ color: {NAVY} !important; }}
        .saarthi-callout {{ display: flex; align-items: flex-start; gap: 10px; padding: 11px 14px; margin: 6px 0; border: 1px solid {LINE}; border-left: 4px solid {BLUE}; background: #F3F5F6 !important; color: {TEXT} !important; font-size: 12.5px; line-height: 1.45; }}
        .saarthi-callout-body {{ display: flex; flex-direction: column; gap: 2px; color: {TEXT} !important; }}
        .saarthi-callout-body b {{ font: 600 10.5px 'Inter', sans-serif; letter-spacing: 0.12em; text-transform: uppercase; color: {NAVY}; }}
        .saarthi-callout-body span {{ color: {TEXT} !important; font-size: 12.5px; }}
        .saarthi-callout-mark {{ font: 700 10px 'Inter', sans-serif; line-height: 1.5; border: 1px solid currentColor; border-radius: 50%; width: 1.2rem; height: 1.2rem; text-align: center; flex: none; margin-top: 1px; }}
        .saarthi-callout-info {{ border-left-color: #5C7380; background: #F2F6FA !important; }}
        .saarthi-callout-info .saarthi-callout-mark, .saarthi-callout-info .saarthi-callout-body b {{ color: #5C7380; }}
        .saarthi-callout-success {{ border-left-color: #238C78; background: #F0F7F4 !important; }}
        .saarthi-callout-success .saarthi-callout-mark, .saarthi-callout-success .saarthi-callout-body b {{ color: #238C78; }}
        .saarthi-callout-warning {{ border-left-color: {WARNING}; background: {WARNING_BG} !important; }}
        .saarthi-callout-warning .saarthi-callout-mark, .saarthi-callout-warning .saarthi-callout-body b {{ color: #E58A35; }}
        .saarthi-callout-error {{ border-left-color: #D9483B; background: #FDF2F3 !important; }}
        .saarthi-callout-error .saarthi-callout-mark, .saarthi-callout-error .saarthi-callout-body b {{ color: #D9483B; }}
        [data-testid="stAlert"], [data-testid="stAlert"] > div, [data-testid="stAlertContainer"], [data-baseweb="notification"], div[role="alert"] {{ background: #F3F5F6 !important; background-color: #F3F5F6 !important; color: {TEXT} !important; border: 1px solid {LINE} !important; border-left: 4px solid {CRIMSON} !important; border-radius: 0px !important; }}
        [data-testid="stAlert"] *, [data-testid="stAlertContainer"] *, [data-baseweb="notification"] *, div[role="alert"] * {{ color: {TEXT} !important; background-color: transparent !important; }}
        .stApp [data-baseweb="select"], .stApp [data-baseweb="select"] > div, .stApp [data-baseweb="input"], .stApp [data-baseweb="input"] > div, .stApp [data-baseweb="base-input"], .stApp [data-baseweb="textarea"], .stApp [data-baseweb="textarea"] > div, .stApp [data-testid="stNumberInputContainer"], .stApp input, .stApp textarea {{ border-radius: 0px !important; border-color: {INPUT_LINE} !important; background: #FFFFFF !important; background-color: #FFFFFF !important; color: {TEXT} !important; -webkit-text-fill-color: {TEXT} !important; caret-color: {NAVY} !important; }}
        .stApp [data-baseweb="select"] span, .stApp [data-baseweb="select"] div, .stApp [data-testid="stNumberInput"] input, .stApp [data-testid="stNumberInput"] button {{ color: {TEXT} !important; background: #FFFFFF !important; background-color: #FFFFFF !important; }}
        .stApp [data-baseweb="select"] svg, .stApp [data-testid="stNumberInput"] svg {{ fill: {NAVY} !important; color: {NAVY} !important; }}
        body [data-baseweb="popover"], body [data-baseweb="popover"] > div, body [data-baseweb="menu"], body ul[role="listbox"], body li[role="option"], body [data-testid="stSelectboxVirtualDropdown"] {{ background: #FFFFFF !important; background-color: #FFFFFF !important; color: {TEXT} !important; border-radius: 0px !important; }}
        body li[role="option"] *, body [data-baseweb="menu"] * {{ color: {TEXT} !important; }}
        body li[role="option"]:hover, body li[role="option"][aria-selected="true"] {{ background: {IVORY} !important; background-color: {IVORY} !important; color: {NAVY} !important; font-weight: 600 !important; }}
        body [data-testid="stToast"], body [data-testid="stToast"] > div {{ background: {NAVY} !important; background-color: {NAVY} !important; color: {CREAM} !important; border: 1px solid {LINE} !important; border-top: 3px solid {LINE} !important; border-radius: 0px !important; }}
        body [data-testid="stToast"] * {{ color: {CREAM} !important; }}
        .stApp [data-baseweb="tag"] {{ background: {NAVY} !important; background-color: {NAVY} !important; color: {CREAM} !important; border-radius: 0px !important; border: 1px solid {LINE} !important; }}
        .stApp [data-baseweb="tag"] span, .stApp [data-baseweb="tag"] svg {{ color: {CREAM} !important; fill: {CREAM} !important; background: transparent !important; }}
        .stMain label p, .stMain [data-testid="stWidgetLabel"] p, .stMain [data-testid="stCheckbox"] label span, .stMain [data-testid="stCheckbox"] p {{ font: 600 10.5px 'Inter', sans-serif !important; letter-spacing: 0.12em !important; text-transform: uppercase !important; color: {TEXT} !important; }}
        .stMain [data-testid="stRadio"] label span, .stMain [data-testid="stRadio"] p, .stMain [data-testid="stSlider"] * {{ color: {TEXT} !important; }}
        [data-testid="stTabs"] [data-baseweb="tab-list"] {{ gap: 2px !important; border-bottom: 1px solid {LINE} !important; background: transparent !important; }}
        [data-testid="stTabs"] [data-baseweb="tab"] {{ background: transparent !important; border: 0 !important; border-bottom: 3px solid transparent !important; font: 600 12px 'Inter', sans-serif !important; letter-spacing: 0.06em !important; text-transform: uppercase !important; color: {MUTED} !important; padding: 10px 14px !important; }}
        [data-testid="stTabs"] [data-baseweb="tab"] p, [data-testid="stTabs"] [data-baseweb="tab"] span {{ color: {MUTED} !important; }}
        [data-testid="stTabs"] [aria-selected="true"], [data-testid="stTabs"] [aria-selected="true"] p, [data-testid="stTabs"] [aria-selected="true"] span {{ color: {NAVY} !important; border-bottom-color: {WARNING} !important; font-weight: 600 !important; }}
        details[data-testid="stExpander"], details[data-testid="stExpander"] > div {{ background: #FFFFFF !important; background-color: #FFFFFF !important; border: 1px solid {LINE} !important; border-top: 2px solid {WARNING} !important; border-radius: 0px !important; color: {TEXT} !important; }}
        details[data-testid="stExpander"] summary, details[data-testid="stExpander"] summary p, details[data-testid="stExpander"] summary span, details[data-testid="stExpander"] summary svg {{ font: 600 12px 'Inter', sans-serif !important; letter-spacing: 0.05em !important; color: {NAVY} !important; fill: {NAVY} !important; background: #FFFFFF !important; background-color: #FFFFFF !important; }}
        .royal {{ position: relative; background: {NAVY}; color: {CREAM}; overflow: hidden; border-bottom: 1px solid {LINE}; margin-bottom: 8px; }}
        .royal .pat {{ position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }}
        .royal .hd {{ position: relative; max-width: 1400px; margin: 0 auto; padding: 20px 28px 16px; }}
        .royal .row {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }}
        .royal .brand {{ display: flex; align-items: center; gap: 14px; }}
        .royal .bn {{ font: 600 22px 'Playfair Display', serif; letter-spacing: .28em; color: {CREAM}; }}
        .royal .bt {{ font: 500 11px 'Inter', sans-serif; letter-spacing: .14em; text-transform: uppercase; color: {NAVY_MUTED}; margin-top: 3px; }}
        .royal .pills {{ display: flex; border: 1px solid #3A4C70; flex-wrap: wrap; }}
        .royal .pills span {{ background: none; border: 0; color: {NAVY_MUTED}; font: 500 12px 'Inter', sans-serif; letter-spacing: .06em; padding: 9px 15px; }}
        .royal .pills span.on {{ background: {CREAM}; color: {NAVY}; font-weight: 600; }}
        .royal h1 {{ font: 600 36px/1.1 'Playfair Display', serif !important; margin: 18px 0 0 !important; color: {CREAM} !important; }}
        .royal .chip {{ font: 600 10.5px 'Inter', sans-serif; letter-spacing: .16em; text-transform: uppercase; color: {GOLD_LIGHT}; border: 1px solid #E1E6E988; padding: 6px 10px; margin-left: 12px; vertical-align: middle; display: inline-block; }}
        .royal .kp {{ display: grid; grid-template-columns: repeat(5, minmax(100px, 1fr)); border-top: 1px solid {GOLD}; min-width: min(560px, 100%); margin-top: 10px; }}
        .royal .kp div {{ padding: 10px 12px 0 0; margin-right: 12px; border-right: 1px solid #E1E6E955; }}
        .royal .kp div:last-child {{ border-right: 0; margin-right: 0; }}
        .royal .kp .n {{ font: 600 30px 'Playfair Display', serif; line-height: 1; color: {CREAM}; }}
        .royal .kp .l {{ font: 500 10px 'Inter', sans-serif; letter-spacing: .12em; text-transform: uppercase; color: {NAVY_MUTED}; margin-top: 6px; }}
        .royal .demo-strip {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 16px; padding-top: 12px; border-top: 1px solid #E1E6E955; }}
        .royal .demo-badge {{ border: 1px solid #E1E6E9aa; color: {CREAM}; font: 600 11px 'Inter', sans-serif; letter-spacing: .06em; padding: 6px 12px; text-transform: uppercase; }}
        .royal .demo-badge.on {{ background: {WARNING}; color: {NAVY}; }}
        .royal .ph {{ margin-left: auto; font: 500 11px ui-monospace, Menlo, monospace; letter-spacing: .06em; color: {NAVY_MUTED}; }}
        .royal .ph b {{ color: {GOLD_LIGHT}; }}
        .eb {{ font: 600 11px 'Inter', sans-serif; letter-spacing: .18em; text-transform: uppercase; color: {EYEBROW}; }}
        .eb.live {{ color: #238C78; }}
        .saarthi-heading {{ margin: 0; }}
        .saarthi-title {{ font: 600 22px/1.2 'Playfair Display', serif; color: {NAVY}; margin-top: 4px; }}
        .saarthi-description {{ color: {MUTED}; font-size: 12.5px; line-height: 1.45; margin-top: 4px; }}
        .saarthi-notice {{ display: flex; gap: 10px; align-items: flex-start; border: 1px solid {LINE}; border-left: 3px solid {WARNING}; background: {IVORY}; padding: 9px 12px; color: {TEXT}; font-size: 12.5px; line-height: 1.45; }}
        .saarthi-notice-caution {{ color: #D9483B; border-color: #D9483B66; border-left: 3px solid #D9483B; background: #FDF2F3; }}
        .saarthi-notice-mark {{ font: 600 10px 'Inter', sans-serif; line-height: 1.45; border: 1px solid currentColor; border-radius: 50%; width: 1.15rem; height: 1.15rem; text-align: center; flex: none; color: {EYEBROW}; }}
        .saarthi-notice-caution .saarthi-notice-mark {{ color: #D9483B; }}
        .saarthi-metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(10.5rem, 1fr)); gap: 12px; }}
        .saarthi-metric {{ border: 1px solid {LINE}; border-top: 3px solid {LINE}; background: #FFFFFF; padding: 14px 16px; min-width: 0; }}
        .saarthi-metric-value {{ font: 600 30px/1 'Playfair Display', serif; font-variant-numeric: tabular-nums; color: {NAVY}; }}
        .saarthi-metric-label {{ color: {MUTED}; font: 600 10.5px 'Inter', sans-serif; letter-spacing: 0.12em; text-transform: uppercase; line-height: 1.35; margin-top: 7px; }}
        .saarthi-map-header {{ background: {NAVY}; border-top: 3px solid {LINE}; padding: 14px 18px 10px; display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; flex-wrap: wrap; }}
        .saarthi-map-header h3 {{ font: 600 20px 'Playfair Display', serif !important; color: {CREAM} !important; margin: 4px 0 0 !important; }}
        .saarthi-map-header .sub {{ font-size: 12px; color: {NAVY_MUTED}; }}
        .saarthi-legend {{ background: {NAVY}; color: {NAVY_MUTED}; font-size: 11.5px; line-height: 1.7; padding: 10px 18px 14px; display: flex; flex-wrap: wrap; gap: 16px; border-bottom: 2px solid {GOLD}; }}
        .saarthi-table-container {{ width: 100%; max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; display: block; min-width: 0; margin: 6px 0; background: #FFFFFF; border: 1px solid {LINE}; }}
        table.saarthi-scan, table.saarthi-requests {{ width: 100% !important; border-collapse: collapse !important; border: none !important; font-size: 12.5px !important; background: #FFFFFF !important; margin: 0 !important; }}
        table.saarthi-scan thead tr, table.saarthi-requests thead tr {{ background: {IVORY} !important; border-bottom: 2px solid {LINE} !important; }}
        table.saarthi-scan th, table.saarthi-requests th {{ font: 600 10.5px 'Inter', sans-serif !important; letter-spacing: 0.1em !important; text-transform: uppercase !important; color: {MUTED} !important; text-align: left !important; padding: 10px 14px !important; border: none !important; border-bottom: 2px solid {LINE} !important; white-space: nowrap !important; background: {IVORY} !important; }}
        table.saarthi-scan td, table.saarthi-requests td {{ padding: 10px 14px !important; border: none !important; border-bottom: 1px solid {LINE} !important; color: {TEXT} !important; background: #FFFFFF !important; vertical-align: middle !important; line-height: 1.45 !important; }}
        table.saarthi-scan tbody tr:hover td, table.saarthi-requests tbody tr:hover td {{ background: #F3F5F6 !important; }}
        .saarthi-scan td.saarthi-nowrap, .saarthi-requests td.saarthi-nowrap {{ white-space: nowrap !important; }}
        .inc {{ border: 1px solid {LINE}; border-top: 3px solid #D9483B; background: #FFFFFF; padding: 14px 16px; margin-top: 12px; }}
        .inc.HIGH {{ border-top-color: #E58A35; }}
        .inc.MEDIUM, .inc.MODERATE {{ border-top-color: #75818A; }}
        .inc.LOW {{ border-top-color: #238C78; }}
        .ih {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; font: 600 11px ui-monospace, Menlo, monospace; color: {MUTED}; margin-bottom: 6px; }}
        .ih-left {{ display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
        .tg {{ display: inline-block !important; white-space: nowrap !important; line-height: 1.2 !important; font: 600 10px 'Inter', sans-serif !important; letter-spacing: .1em !important; text-transform: uppercase !important; padding: 3px 8px !important; border: 1px solid currentColor !important; background: #FFFFFF !important; }}
        .tg.CRITICAL {{ color: #D9483B !important; border-color: #D9483B !important; background: #FDF2F3 !important; }}
        .tg.HIGH {{ color: #E58A35 !important; border-color: #E58A35 !important; background: #FBF3EA !important; }}
        .tg.MEDIUM, .tg.MODERATE {{ color: #75818A !important; border-color: #75818A !important; }}
        .tg.LOW, .tg.RESOLVED {{ color: #238C78 !important; border-color: #238C78 !important; background: #F0F7F4 !important; }}
        .tg.FLOOD, .tg.WATER, .tg.MEDICAL {{ color: #5C7380 !important; border-color: #5C7380 !important; background: #F2F6FA !important; }}
        .tg.HEATWAVE, .tg.CYCLONE, .tg.RESCUE, .tg.SHELTER, .tg.FOOD {{ color: #E58A35 !important; border-color: #E58A35 !important; background: #FBF3EA !important; }}
        .inc h3 {{ font: 600 17px 'Playfair Display', serif !important; margin: 6px 0 4px !important; color: {NAVY} !important; }}
        .inc p {{ margin: 0 0 10px; font-size: 12.5px; color: #263238; line-height: 1.45; }}
        .ch {{ display: flex; gap: 6px; flex-wrap: wrap; }}
        .ch span {{ display: inline-block; font: 500 11px 'Inter', sans-serif; border: 1px solid {LINE}; background: {IVORY}; padding: 4px 8px; color: {TEXT}; white-space: nowrap; }}
        .ch .ai {{ color: #238C78; border-color: #238C7866; background: #F0F7F4; font-weight: 600; }}
        .if {{ display: flex; justify-content: space-between; font-size: 11px; color: {MUTED}; margin-top: 10px; flex-wrap: wrap; gap: 6px; }}
        .bar {{ height: 6px; background: #E1E6E9; margin: 6px 0 14px; }}
        .bar i {{ display: block; height: 100%; background: #5C7380; }}
        .bar i.hot {{ background: #D9483B; }}
        .bh {{ display: flex; justify-content: space-between; font-size: 12.5px; gap: 10px; color: {TEXT}; }}
        .cf {{ border: 1px solid {LINE}; padding: 12px 14px; margin-bottom: 10px; background: #FFFFFF; }}
        .cf b {{ display: block; font: 600 11px 'Inter', sans-serif; letter-spacing: .1em; text-transform: uppercase; margin-bottom: 5px; }}
        .cf p {{ margin: 0 0 4px; font-size: 13px; color: {TEXT}; }}
        .cf small {{ color: {MUTED}; font-size: 11.5px; }}
        .cf.bad {{ border-color: #D9483B66; background: #FDF2F3; border-left: 4px solid #D9483B; }}
        .cf.bad b {{ color: #D9483B; }}
        .cf.good {{ border-color: #238C7866; background: #F0F7F4; border-left: 4px solid #238C78; }}
        .cf.good b {{ color: #238C78; }}
        .bc {{ border: 1px solid {LINE}; border-top: 3px solid {LINE}; padding: 14px 16px; margin-bottom: 12px; background: #FFFFFF; }}
        .bc .bt2 {{ display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; font: 600 10.5px 'Inter', sans-serif; letter-spacing: .1em; text-transform: uppercase; color: {EYEBROW}; margin-bottom: 8px; }}
        .bc p {{ margin: 5px 0; font: 400 13.5px/1.65 'Noto Sans Devanagari', 'Inter', sans-serif; color: {TEXT}; }}
        .bc b {{ color: {NAVY}; }}
        .bc .en {{ color: {MUTED}; font-size: 12.5px; }}
        .saarthi-foot {{ max-width: 1400px; margin: 18px auto 0; padding: 16px 32px 12px; border-top: 1px solid {LINE}; text-align: center; font-size: 12px; color: {MUTED}; }}
        </style>""",
        unsafe_allow_html=True)


def page_fit_styles():
    """Keep the operational pages inside the frame without crushing table cells."""
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


def _go_page(page_name):
    """Callback executed before script rerun so nav_page updates in a single pass."""
    st.session_state["nav_page"] = page_name


def command_bar(page, live):
    """Royal Navy & Gold header banner with embedded live KPIs, matching Saarthi – Emergency Command Center.html."""
    state = "LIVE" if live else "HOLD"
    group_map = {
        "Monitor": ("Situation Overview", "Early Warnings", "Severity & Vulnerability"),
        "Respond": ("Allocation", "Roads & Hospitals", "Supply Tracking", "Field Requests"),
        "Analyse": ("Intake & Triage", "Ripple", "What-If", "Plan Challenger", "Coordinator"),
        "Demo": ("Scenario Tests",),
    }
    pills_html = []
    for grp, pages in group_map.items():
        cls = "on" if page in pages else ""
        pills_html.append(f'<span class="{cls}">{html.escape(grp)}</span>')

    sev = data.severity()
    plan = data.get_plan()
    crit_zones = sum(1 for z in sev if z["level"] == "Critical")
    open_sos = data.read("SELECT COUNT(*) AS n FROM incidents WHERE status='OPEN'")[0]["n"]
    free_beds = sum(int(h["available_beds"] or 0) for h in data.read("SELECT available_beds FROM hospitals"))
    blocked_rds = data.read("SELECT COUNT(*) AS n FROM roads WHERE status='BLOCKED'")[0]["n"]
    ok_ledger, _, _ = data.verify_ledger()

    kpis = [
        (str(open_sos), "Active SOS calls"),
        (f"{crit_zones} / {len(sev)}", "Critical zones"),
        (str(free_beds), "Free hospital beds"),
        (str(blocked_rds), "Roads blocked"),
        (f"v{plan.get('version', 1)} ({plan.get('changed', 0)} Δ)", "Active plan (" + ("Ledger OK" if ok_ledger else "TAMPERED") + ")"),
    ]
    kpi_html = "".join(f'<div><div class="n">{html.escape(v)}</div><div class="l">{html.escape(lbl)}</div></div>' for v, lbl in kpis)
    pills_str = "".join(pills_html)
    page_esc = html.escape(page)
    page_up = html.escape(page.upper())

    st.markdown(
        f"""<div class="royal"><svg class="pat" viewBox="0 0 1440 360" preserveAspectRatio="xMidYMid slice" aria-hidden="true"><defs><pattern id="jl" width="64" height="64" patternUnits="userSpaceOnUse"><path d="M32 2L62 32L32 62L2 32Z" fill="none" stroke="#E1E6E9" stroke-opacity=".2"/><circle cx="32" cy="32" r="9" fill="none" stroke="#E1E6E9" stroke-opacity=".2"/><path d="M0 0L64 64M64 0L0 64" stroke="#E1E6E9" stroke-opacity=".06"/></pattern><line id="sp" x1="1250" y1="-90" x2="1250" y2="450"/></defs><rect width="1440" height="360" fill="url(#jl)"/><g fill="none" stroke="#E1E6E9" stroke-opacity=".34"><circle cx="1250" cy="180" r="270" stroke-width="2"/><circle cx="1250" cy="180" r="245"/><circle cx="1250" cy="180" r="48" stroke-width="2"/><use href="#sp"/><use href="#sp" transform="rotate(15 1250 180)"/><use href="#sp" transform="rotate(30 1250 180)"/><use href="#sp" transform="rotate(45 1250 180)"/><use href="#sp" transform="rotate(60 1250 180)"/><use href="#sp" transform="rotate(75 1250 180)"/><use href="#sp" transform="rotate(90 1250 180)"/><use href="#sp" transform="rotate(105 1250 180)"/><use href="#sp" transform="rotate(120 1250 180)"/><use href="#sp" transform="rotate(135 1250 180)"/><use href="#sp" transform="rotate(150 1250 180)"/><use href="#sp" transform="rotate(165 1250 180)"/></g></svg><header class="hd"><div class="row"><div class="brand"><svg width="44" height="44" viewBox="0 0 48 48" aria-hidden="true" fill="none" stroke="#E1E6E9" stroke-width="1.6"><circle cx="24" cy="24" r="21"/><circle cx="24" cy="24" r="5"/><line x1="24" y1="3" x2="24" y2="45"/><line x1="3" y1="24" x2="45" y2="24"/><line x1="9.2" y1="9.2" x2="38.8" y2="38.8"/><line x1="38.8" y1="9.2" x2="9.2" y2="38.8"/><line x1="13.5" y1="4.5" x2="34.5" y2="43.5"/><line x1="34.5" y1="4.5" x2="13.5" y2="43.5"/></svg><div><div class="bn">SAARTHI</div><div class="bt">ECC · Multi-agent coordinator (PS-03)</div></div></div><div class="pills">{pills_str}</div></div><div class="row" style="align-items:flex-end"><h1>{page_esc}<span class="chip">All agents online</span></h1><div class="kp">{kpi_html}</div></div><div class="demo-strip"><span class="demo-badge on">{page_esc}</span><span class="demo-badge">Plan v{plan.get('version', 1)} · {plan.get('solver', 'Active')}</span><span class="ph">DEMO CONTROLS · Mode: <b>{state}</b> | Active View: <b>{page_up}</b></span></div></header></div>""",
        unsafe_allow_html=True)


def render_top_controls():
    """Clickable top navigation bar and demo scenario buttons right below the Royal banner.
    Uses on_click callbacks so every button switches pages or runs scenarios on the very first click."""
    current = st.session_state.get("nav_page", "Situation Overview")
    with st.container(border=True):
        st.markdown('<div class="eb">Command Center Navigation — Click any module to switch views</div>', unsafe_allow_html=True)
        all_pages = list(PAGES.keys())
        row1 = st.columns(7)
        for col, label in zip(row1, all_pages[:7]):
            is_cur = current == label
            col.button(
                label,
                key=f"top_btn_{label}",
                type="primary" if is_cur else "secondary",
                use_container_width=True,
                on_click=_go_page,
                args=(label,),
            )
        row2 = st.columns(6)
        for col, label in zip(row2, all_pages[7:]):
            is_cur = current == label
            col.button(
                label,
                key=f"top_btn_{label}",
                type="primary" if is_cur else "secondary",
                use_container_width=True,
                on_click=_go_page,
                args=(label,),
            )

        st.markdown('<div class="eb" style="margin-top:6px;">Live Demo Controls &amp; Stress Scenarios (A–E)</div>', unsafe_allow_html=True)
        b1, b2, b3, b4, b5, b6, b7 = st.columns(7)
        if b1.button("🌀 Cyclone", key="top_cyclone", use_container_width=True, help="Cyclone intensifies near Ratnagiri & Sindhudurg"):
            data.demo_cyclone()
            plan = data.get_plan()
            st.session_state["last_demo_banner"] = (
                f"🌀 Cyclone intensifies executed: Ratnagiri (95%) & Sindhudurg (85%) risk jumped; "
                f"Red alert queued for Sindhudurg. Plan v{plan['version']} recalculated ({plan['changed']} allocations changed)."
            )
            st.rerun()
        if b2.button("B · Hospital", key="top_sc_b", use_container_width=True, help=data.SCENARIOS["B"][1]):
            res = data.run_scenario("B")
            st.session_state["scenario_result"] = res
            st.session_state["last_demo_banner"] = (
                f"🏥 Scenario B ({res['title']}): Ratnagiri Civil Hospital beds set to 0. "
                f"Plan v{res['version']} recalculated ({res['changed']} allocations changed)."
            )
            st.rerun()
        if b3.button("C · Road Cut", key="top_sc_c", use_container_width=True, help=data.SCENARIOS["C"][1]):
            res = data.run_scenario("C")
            st.session_state["scenario_result"] = res
            st.session_state["last_demo_banner"] = (
                f"⛔ Scenario C ({res['title']}): Roads R8 and R9 blocked. "
                f"Plan v{res['version']} recalculated ({res['changed']} allocations changed)."
            )
            st.rerun()
        if b4.button("D · Supplies", key="top_sc_d", use_container_width=True, help=data.SCENARIOS["D"][1]):
            res = data.run_scenario("D")
            st.session_state["scenario_result"] = res
            st.session_state["last_demo_banner"] = (
                f"📦 Scenario D ({res['title']}): +1,500 Food kits & +600 Medical kits added at Kolhapur Depot. "
                f"Plan v{res['version']} recalculated ({res['changed']} allocations changed)."
            )
            st.rerun()
        if b5.button("E · Multi-Hit", key="top_sc_e", use_container_width=True, help=data.SCENARIOS["E"][1]):
            res = data.run_scenario("E")
            st.session_state["scenario_result"] = res
            st.session_state["last_demo_banner"] = (
                f"⚡ Scenario E ({res['title']}): Cyclone worsens, Road R6 blocked, Thane Civil full, +40 patients in Raigad. "
                f"Plan v{res['version']} recalculated ({res['changed']} allocations changed)."
            )
            st.rerun()
        if b6.button("🚨 Tamper", key="top_tamper", use_container_width=True, help="Tamper with a past supply ledger record"):
            data.demo_tamper()
            ok_l, bad_id, _ = data.verify_ledger()
            st.session_state["last_demo_banner"] = (
                f"🚨 Ledger Tamper executed: Record #{bad_id} was altered directly in SQLite! "
                "SHA-256 verification now flags the broken hash chain (see Supply Tracking)."
            )
            st.rerun()
        if b7.button("↺ Reset (A)", key="top_sc_a", type="primary", use_container_width=True, help="Scenario A: Reset to normal baseline"):
            res = data.run_scenario("A")
            st.session_state["scenario_result"] = res
            st.session_state["last_demo_banner"] = (
                f"✅ Scenario A ({res['title']}): Database reset to normal starting conditions (Plan v{res['version']})."
            )
            st.rerun()

        if st.session_state.get("last_demo_banner"):
            msg = st.session_state["last_demo_banner"]
            b_msg, b_go1, b_go2, b_close = st.columns([4.2, 1.1, 1.1, 0.6])
            ui_success(msg, target=b_msg)
            b_go1.button("View Allocation", key="banner_go_alloc", use_container_width=True, on_click=_go_page, args=("Allocation",))
            b_go2.button("Scenario Checks", key="banner_go_scen", use_container_width=True, on_click=_go_page, args=("Scenario Tests",))
            if b_close.button("✕", key="banner_dismiss", use_container_width=True):
                st.session_state.pop("last_demo_banner", None)
                st.rerun()


_DARK_STYLE = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json"
_PIXELS = DeckString("pixels")


def render_deck(layers, height, key):
    """Draw one map inside the Royal Navy map frame."""
    st.markdown(
        '<div class="saarthi-map-header">'
        '<div><div class="eb" style="color:#C5D0D6">Coastal Maharashtra · MMR &amp; Konkan</div>'
        '<h3>Live operations map</h3></div>'
        '<div class="sub">Interactive geospatial layer · hover markers for recorded details</div>'
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
    """Circular colour dots inside the dark Royal Navy map footer (.lg), matching the HTML reference."""
    if not items:
        return
    chips = []
    for colour, caption in items:
        chips.append(
            '<span style="display:inline-flex;align-items:center;margin-right:6px;">'
            f'<i style="width:10px;height:10px;background:{colour};border-radius:50%;display:inline-block;margin-right:6px;"></i>'
            f"{html.escape(str(caption))}</span>"
        )
    st.markdown('<div class="saarthi-legend">' + "".join(chips) + "</div>", unsafe_allow_html=True)


def _place_labels(frame, text_column, offset):
    """Names already stored on the rows. Nothing here creates a new place."""
    return pdk.Layer("TextLayer", frame, get_position="[lon, lat]", get_text=text_column, get_size=12,
                     get_color=[38, 50, 56, 235], get_text_anchor="'middle'", get_alignment_baseline="'bottom'",
                     get_pixel_offset=offset, billboard=True, font_family="sans-serif", background=True,
                     get_background_color=[255, 255, 255, 220], background_padding=[4, 2, 4, 2], pickable=False)


def draw_map(layers, height=480, key="map"):  # map with switchable layers
    choices = ["Risk zones", "Roads", "Hospitals", "Supply hubs", "SOS requests", "Shipments"]  # every layer
    shown = st.multiselect("Map layers", choices, default=layers, key=key)  # operator picks which to show
    deck_layers = []  # the pydeck layers to draw, bottom first
    labels = []  # place names drawn after the symbols
    legend = []
    if "Risk zones" in shown:  # outline only, so roads and facilities stay visible
        z = df(data.severity())  # zones with scores
        z["color"] = z["level"].map(LEVEL_RGB)  # colour by level
        z["label"] = z["name"] + ": " + z["level"] + ", score " + z["score"].astype(str)  # hover text
        deck_layers.append(pdk.Layer("ScatterplotLayer", z, get_position="[lon, lat]", get_line_color="color",
                                     filled=False, stroked=True, get_radius=18, radius_units=_PIXELS,
                                     line_width_min_pixels=2, pickable=True))
        labels.append(_place_labels(z, "name", [0, -14]))
        legend.extend((_rgb_hex(LEVEL_RGB[name]), name) for name in ("Critical", "High", "Moderate", "Low"))
    if "Roads" in shown:  # one line per road
        r = df(data.roads_with_positions())  # roads with both ends' positions
        r["path"] = r.apply(lambda x: [[x["a_lon"], x["a_lat"]], [x["b_lon"], x["b_lat"]]], axis=1)  # line points
        r["color"] = r["status"].map(ROAD_RGB)  # colour by condition
        r["width"] = r["status"].map({"OPEN": 2, "SLOW": 3, "BLOCKED": 3})  # screen pixels; problem roads drawn thicker
        r["label"] = r["id"] + ": " + r["a"] + " to " + r["b"] + ", " + r["status"]  # hover text
        deck_layers.append(pdk.Layer("PathLayer", r, get_path="path", get_color="color", get_width="width",
                                     width_units=_PIXELS, pickable=True))  # add the layer
        legend.extend((_rgb_hex(ROAD_RGB[name]), name.capitalize()) for name in ("OPEN", "SLOW", "BLOCKED"))
    if "Hospitals" in shown:  # small dots: purple with free beds, grey when full
        h = df(data.read("SELECT * FROM hospitals"))  # hospitals
        h["color"] = h["available_beds"].apply(lambda b: [35, 140, 120] if b > 0 else [117, 129, 138])  # grey when full
        h["label"] = h["name"] + ": " + h["available_beds"].astype(str) + "/" + h["total_beds"].astype(str) + " beds free"
        deck_layers.append(pdk.Layer("ScatterplotLayer", h, get_position="[lon, lat]", get_fill_color="color",
                                     get_line_color=[255, 255, 255], filled=True, stroked=True, get_radius=7,
                                     radius_units=_PIXELS, line_width_min_pixels=1.5, pickable=True))
        labels.append(_place_labels(h, "name", [0, 16]))
        legend.append(("#238C78", "Hospital with free beds"))
        legend.append(("#75818A", "Hospital full"))
    if "Supply hubs" in shown:  # navy markers, distinct from orange shipments
        hb = df(data.read("SELECT * FROM hubs"))  # hubs
        hb["label"] = hb["name"] + " (supply hub)"  # hover text
        deck_layers.append(pdk.Layer("ScatterplotLayer", hb, get_position="[lon, lat]", get_fill_color=[20, 43, 59],
                                     get_line_color=[255, 255, 255], filled=True, stroked=True, get_radius=8,
                                     radius_units=_PIXELS, line_width_min_pixels=2, pickable=True))
        labels.append(_place_labels(hb, "name", [0, 28]))
        legend.append(("#142B3B", "Supply hub"))
    if "SOS requests" in shown:  # small SOS dots
        i = df(data.read("SELECT * FROM incidents WHERE status != 'RESOLVED'"))  # active requests
        if len(i):  # only if any
            i["color"] = i["status"].map(SOS_RGB)  # red open, orange in transit
            i["label"] = "SOS #" + i["id"].astype(str) + " " + i["need_type"] + ": " + i["people"].astype(str) + " people"
            deck_layers.append(pdk.Layer("ScatterplotLayer", i, get_position="[lon, lat]", get_fill_color="color",
                                         get_line_color=[255, 255, 255], filled=True, stroked=True, get_radius=6,
                                         radius_units=_PIXELS, line_width_min_pixels=1.5, pickable=True))
        legend.append((_rgb_hex(SOS_RGB["OPEN"]), "SOS open"))
        legend.append((_rgb_hex(SOS_RGB["IN_TRANSIT"]), "SOS in transit"))
    if "Shipments" in shown:  # gold dots halfway along moving shipments
        s = df([x for x in data.shipments_with_positions() if "lat" in x])  # moving ones only
        if len(s):  # only if any
            s["label"] = s["shipment_id"] + ": " + s["qty"].astype(str) + " " + s["item"] + " to " + s["destination"]
            deck_layers.append(pdk.Layer("ScatterplotLayer", s, get_position="[lon, lat]", get_fill_color=[229, 138, 53],
                                         get_line_color=[255, 255, 255], stroked=True, filled=True, line_width_min_pixels=1.5,
                                         get_radius=6, radius_units=_PIXELS, pickable=True))
        legend.append(("#E58A35", "Shipment on the move"))
    deck_layers.extend(labels)
    render_deck(deck_layers, height, f"{key}_deck")
    map_legend(legend)


# ---------------------------------------------------------------------
# 3. PAGES
# ---------------------------------------------------------------------
def _render_ripple_svg(origin_zone="Ratnagiri"):
    """Dynamic SVG Ripple cascade network diagram matching Saarthi – Emergency Command Center.html,
    driven by live database rows from ripple_agent.analyse()."""
    try:
        res = ripple_agent.analyse(origin_zone)
    except Exception:
        return
    p_risk = res["primary"].get("risk") or {}
    p_prob = int(round(float(p_risk.get("probability") or 0) * 100))
    p_type = p_risk.get("disaster_type") or "Hazard"
    sec_zones = res.get("secondary") or []
    ext_zones = res.get("extended") or []
    roads = res["primary"].get("roads") or []
    hospitals = res.get("hospitals") or []

    def col_for(pct):
        if pct >= 70:
            return "#D63B45"
        if pct >= 50:
            return "#E58A35"
        if pct >= 35:
            return "#8FA37E"
        return "#3B8FD0"

    mid_nodes = []
    for idx_r, r in enumerate(roads[:4]):
        st_word = r.get("status", "OPEN")
        pct = 90 if st_word == "BLOCKED" else (65 if st_word == "SLOW" else max(25, p_prob - 15 * idx_r))
        mid_nodes.append((f"{r['id']} ({r['a']}–{r['b']}) · {st_word}", pct, 340, 45 + idx_r * 68))
    if not mid_nodes:
        for idx_s, z in enumerate(sec_zones[:4]):
            zr = z.get("risk") or {}
            pct = int(round(float(zr.get("probability") or 0.4) * 100))
            mid_nodes.append((f"{z['zone']} (1-hop)", pct, 340, 55 + idx_s * 70))

    right_nodes = []
    for idx_h, h in enumerate(hospitals[:4]):
        occ = h.get("occupancy")
        pct = int(round(float(occ) * 100)) if occ is not None else 35
        right_nodes.append((f"{h['zone']}: {h.get('name') or 'No hospital'}", pct, 625, 50 + idx_h * 68))
    if not right_nodes:
        for idx_e, z in enumerate(ext_zones[:3]):
            zr = z.get("risk") or {}
            pct = int(round(float(zr.get("probability") or 0.3) * 100))
            right_nodes.append((f"{z['zone']} (2-hop)", pct, 625, 65 + idx_e * 80))

    lines_svg = []
    for mn in mid_nodes:
        lines_svg.append(f'<line x1="95" y1="150" x2="{mn[2]}" y2="{mn[3]}"/>')
        for rn in right_nodes:
            lines_svg.append(f'<line x1="{mn[2]}" y1="{mn[3]}" x2="{rn[2]}" y2="{rn[3]}"/>')

    circles_svg = [
        f'<circle cx="95" cy="150" r="12" fill="{col_for(p_prob)}" stroke="#fff" stroke-width="2"/>'
        f'<text x="95" y="130" text-anchor="middle" font-family="Inter" font-size="11.5" font-weight="600" fill="#142B3B">'
        f'{html.escape(origin_zone)} {html.escape(p_type)} ({p_prob}%)</text>'
    ]
    for label, pct, cx_n, cy_n in mid_nodes + right_nodes:
        circles_svg.append(
            f'<circle cx="{cx_n}" cy="{cy_n}" r="11" fill="{col_for(pct)}" stroke="#fff" stroke-width="2"/>'
            f'<text x="{cx_n}" y="{cy_n - 16}" text-anchor="middle" font-family="Inter" font-size="11" font-weight="600" fill="#142B3B">'
            f'{html.escape(label)} ({pct}%)</text>'
        )

    st.markdown(
        f'<svg viewBox="0 0 760 295" width="100%" role="img" aria-label="Ripple cascade network">'
        f'<g stroke="#E1E6E9" stroke-opacity=".6" stroke-width="1.5">{"".join(lines_svg)}</g>'
        f'{"".join(circles_svg)}</svg>'
        '<div class="saarthi-description">Cascade exposure by node · primary origin on the left, connecting corridors in the middle, downstream zone hospitals on the right.</div>',
        unsafe_allow_html=True,
    )


def page_overview():  # PAGE 1: whole situation — matches Saarthi – Emergency Command Center.html layout
    """Two-column Command Center matching Saarthi – Emergency Command Center.html:
    Left column: Incident Queue with 2x2 filters, .inc cards, and quick SOS actions.
    Right column: Live Operations Map (.mp) + 5 interactive Command Center tabs below."""
    page_fit_styles()
    st.markdown(
        """<style>
        .saarthi-event { padding: 8px 10px; border-top: 1px solid var(--hl); }
        .saarthi-event-meta { color: var(--mu); font: 500 11px ui-monospace, Menlo, monospace; line-height: 1.3; }
        .saarthi-event-text { color: var(--ink); margin-top: 2px; font-size: 12.5px; line-height: 1.35; }
        </style>""",
        unsafe_allow_html=True)

    sev = data.severity()
    sev_by_zone = {z["name"]: z for z in sev}
    plan = data.get_plan()
    incidents = data.read("SELECT * FROM incidents ORDER BY id DESC")

    left_col, right_col = st.columns([1.05, 2.35])

    # LEFT COLUMN: Incident Queue (matches <aside class="panel"> in Saarthi – Emergency Command Center.html)
    with left_col:
        with st.container(border=True):
            st.markdown('<div class="eb">Incident queue</div>', unsafe_allow_html=True)
            f1, f2 = st.columns(2)
            urg_filter = f1.selectbox("Severity Level", ["All levels", "Critical", "High", "Moderate", "Low"], key="ov_flt_lvl")
            need_filter = f2.selectbox("Need / Mode", ["All needs"] + list(data.demo.NEED_TYPES), key="ov_flt_need")
            f3, f4 = st.columns(2)
            status_filter = f3.selectbox("Status", ["All status", "OPEN", "IN_TRANSIT", "RESOLVED"], index=1, key="ov_flt_st")
            zone_filter = f4.selectbox("Zone", ["All zones"] + [z[0] for z in data.demo.ZONES], key="ov_flt_zn")

            filtered_inc = []
            for inc in incidents:
                z_info = sev_by_zone.get(inc["zone"], {"level": "Moderate", "score": 0, "disaster_type": "None"})
                if urg_filter != "All levels" and z_info["level"] != urg_filter:
                    continue
                if need_filter != "All needs" and inc["need_type"] != need_filter:
                    continue
                if status_filter != "All status" and inc["status"] != status_filter:
                    continue
                if zone_filter != "All zones" and inc["zone"] != zone_filter:
                    continue
                filtered_inc.append((inc, z_info))

            st.subheader(f"{len(filtered_inc)} shown")
            st.caption(f"{len(incidents)} total recorded SOS requests · click actions below to claim, resolve, or log")

            with st.expander("⚡ Quick SOS Actions (Claim / Resolve / Log New SOS)"):
                open_ids = data.incident_ids(incidents, "OPEN")
                transit_ids = data.incident_ids(incidents, "IN_TRANSIT")
                qa1, qa2 = st.tabs(["Claim / Resolve", "Log New SOS"])
                with qa1:
                    if open_ids:
                        with st.form("ov_quick_claim"):
                            cid = st.selectbox("Open SOS #", open_ids, key="ov_cid")
                            cteam = st.text_input("Responder Team", "NDRF Rescue Unit Alpha", key="ov_cteam")
                            if st.form_submit_button("Claim (mark in transit)", type="primary"):
                                act(data.claim_incident, int(cid), cteam, ok=f"SOS #{cid} claimed")
                    else:
                        st.caption("No OPEN requests waiting to be claimed.")
                    if transit_ids:
                        with st.form("ov_quick_resolve"):
                            rid = st.selectbox("In-Transit SOS #", transit_ids, key="ov_rid")
                            if st.form_submit_button("Mark resolved"):
                                act(data.resolve_incident, int(rid), ok=f"SOS #{rid} resolved")
                with qa2:
                    with st.form("ov_quick_new_sos", clear_on_submit=True):
                        nz = st.selectbox("Zone", [z[0] for z in data.demo.ZONES], key="ov_nz")
                        nn = st.selectbox("Need", data.demo.NEED_TYPES, key="ov_nn")
                        np_cnt = st.number_input("People affected", 1, 5000, 15, key="ov_np")
                        if st.form_submit_button("Submit SOS", type="primary"):
                            act(data.add_incident, nz, nn, int(np_cnt), ok="SOS logged, plan recalculated")

            # Render rich .inc cards matching Saarthi – Emergency Command Center.html
            patient_by_inc = {a["incident_id"]: a for a in plan.get("patients") or []}
            for inc, z_info in filtered_inc[:8]:
                lvl_up = str(z_info.get("level") or "Moderate").upper()
                need_up = str(inc.get("need_type") or "SOS").upper()
                dtype_up = str(z_info.get("disaster_type") or "HAZARD").upper()
                assigned = patient_by_inc.get(inc["id"])
                if assigned:
                    route_txt = f"Via {', '.join(assigned.get('route') or ['local'])} ({engine.fmt_minutes(assigned.get('minutes'))})"
                    unit_txt = f"Hospital: {assigned.get('hospital')}"
                else:
                    unit_txt = f"Assigned: {inc.get('claimed_by') or 'Awaiting team'}"
                    route_txt = f"Status: {inc.get('status')}"
                st.markdown(
                    f'<article class="inc {html.escape(lvl_up)}">'
                    f'<div class="ih"><span class="ih-left"><span>SOS-{inc["id"]}</span><span class="tg {html.escape(need_up)}">{html.escape(dtype_up)} · {html.escape(need_up)}</span></span>'
                    f'<span class="tg {html.escape(lvl_up)}">{html.escape(lvl_up)}</span></div>'
                    f'<h3>{html.escape(str(inc["zone"]))} · {html.escape(str(inc["need_type"]))} Request</h3>'
                    f'<p>{html.escape(str(inc["people"]))} citizens affected in {html.escape(str(inc["zone"]))} ({html.escape(dtype_up)} zone probability {float(z_info.get("probability") or 0):.0%}).</p>'
                    f'<div class="ch"><span>{html.escape(unit_txt)}</span><span>{html.escape(route_txt)}</span>'
                    f'<span>Score {html.escape(str(z_info.get("score")))}</span><span class="ai">OR-Tools active</span></div>'
                    f'<div class="if"><span>{html.escape(str(inc["zone"]))} · {html.escape(str(inc["status"]))}</span>'
                    f'<span>{html.escape(str(inc.get("reported_at") or ""))}</span></div>'
                    f'</article>',
                    unsafe_allow_html=True,
                )
            if not filtered_inc:
                st.caption("No incidents match these filters.")

    # RIGHT COLUMN: Map (.mp) + 5 Interactive Tabs below (matches <div class="rc"> in HTML reference)
    with right_col:
        with st.container(border=True):
            draw_map(["Risk zones", "Roads", "Hospitals", "Supply hubs", "SOS requests", "Shipments"],
                     height=460, key="ov_map")

        with st.container(border=True):
            t1, t2, t3, t4, t5 = st.tabs([
                "Operational Overview",
                "Plan Challenger & What-If",
                "Ripple Cascade Network",
                "Local Broadcasts (MR / HI)",
                "SQLite Audit (All Tables)",
            ])

            # TAB 1: Operational Overview (Hospital capacity bars + Active dispatch corridors + 24h Risk + Events)
            with t1:
                g_left, g_right = st.columns([1, 1.25])
                with g_left:
                    st.markdown(
                        '<div class="bh"><span class="eb">Resource &amp; hospital capacity</span>'
                        '<span class="eb live">Live telemetry</span></div><div style="height:10px"></div>',
                        unsafe_allow_html=True,
                    )
                    hospitals = data.read("SELECT * FROM hospitals ORDER BY id")
                    bars_html = []
                    for h in hospitals:
                        tot = int(h["total_beds"] or 1)
                        free = int(h["available_beds"] or 0)
                        occ_pct = int(round(((tot - free) / tot) * 100)) if tot > 0 else 0
                        hot_cls = "hot" if occ_pct >= 85 else ""
                        bars_html.append(
                            f'<div class="bh"><span>{html.escape(h["name"])}</span>'
                            f'<span>{free} beds free ({occ_pct}% occupied)</span></div>'
                            f'<div class="bar"><i class="{hot_cls}" style="width:{min(100, max(0, occ_pct))}%"></i></div>'
                        )
                    st.markdown("".join(bars_html), unsafe_allow_html=True)
                with g_right:
                    supply_moves = plan.get("supply") or []
                    st.markdown(
                        f'<div class="bh"><span class="eb">Active dispatch corridors (Plan v{plan.get("version", 1)})</span>'
                        f'<span class="eb live">{len(plan.get("shortfalls") or [])} shortfalls</span></div><div style="height:10px"></div>',
                        unsafe_allow_html=True,
                    )
                    if supply_moves:
                        head_cells = "".join(f"<th>{c}</th>" for c in ("zone", "item", "qty", "from_hub", "eta"))
                        body_rows = []
                        for m in supply_moves[:6]:
                            eta_s = engine.fmt_minutes(m.get("minutes"))
                            body_rows.append(
                                f"<tr><td>{html.escape(str(m.get('zone')))}</td>"
                                f"<td>{html.escape(str(m.get('item')))}</td>"
                                f"<td>{html.escape(str(m.get('qty')))}</td>"
                                f"<td>{html.escape(str(m.get('from_hub')))}</td>"
                                f"<td>{html.escape(str(eta_s))}</td></tr>"
                            )
                        st.markdown(
                            f'<div class="saarthi-table-container"><table class="saarthi-scan">'
                            f'<thead><tr>{head_cells}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></div>',
                            unsafe_allow_html=True,
                        )
                        ab1, ab2 = st.columns(2)
                        if ab1.button("Approve all supply moves", key="ov_approve_all", type="primary"):
                            act(data.approve_all, ok="Every supply move approved")
                        if ab2.button("Re-optimise plan now", key="ov_reopt"):
                            act(data.event, "MANUAL", "Operator asked for a fresh plan", ok="Plan recalculated")
                    else:
                        ui_success("All high-risk zone supply needs are covered.")

                st.divider()
                ch_col, ev_col = st.columns([1.35, 1])
                with ch_col:
                    st.markdown('<div class="eb">24-Hour Risk Telemetry</div>', unsafe_allow_html=True)
                    st.subheader("Risk probability over the last 24 hours")
                    logs = df(data.read("SELECT timestamp, zone, probability FROM risk_logs ORDER BY id"))
                    line_colours = ["#142B3B", "#D9483B", "#E1E6E9", "#5C7380", "#238C78", "#E58A35", "#238C78"]
                    if logs.empty:
                        st.caption("Recorded risk probability is not available.")
                    else:
                        fig = px.line(logs, x="timestamp", y="probability", color="zone", markers=True,
                                      color_discrete_sequence=line_colours)
                        fig.update_layout(yaxis_tickformat=".0%", yaxis_range=[0, 1], height=220)
                        style_chart(fig)
                        st.plotly_chart(fig, use_container_width=True)
                with ev_col:
                    st.markdown('<div class="eb">Live System Events</div>', unsafe_allow_html=True)
                    st.subheader("System events")
                    events = data.read("SELECT * FROM events ORDER BY id DESC LIMIT 6")
                    for e in events:
                        icon = EVENT_ICON.get(e["type"], "•")
                        kind = str(e["type"] or "not available").replace("_", " ").lower()
                        stamp = e["timestamp"] or ""
                        when = stamp[11:16] if len(stamp) >= 16 else "not available"
                        message = html.escape(str(e["message"] if e["message"] is not None else "not available"))
                        st.markdown(
                            '<div class="saarthi-event">'
                            f'<div class="saarthi-event-meta">{icon} {when} · {html.escape(kind)}</div>'
                            f'<div class="saarthi-event-text">{message}</div>'
                            "</div>",
                            unsafe_allow_html=True)

            # TAB 2: Plan Challenger & What-If (matches Tab 1 in Saarthi – Emergency Command Center.html)
            with t2:
                review = plan_challenger_agent.review()
                findings = review.get("findings") or []
                c_bad, c_good = st.columns(2)
                with c_bad:
                    st.markdown('<div class="eb" style="margin-bottom:10px">Plan conflicts &amp; warnings flagged by Challenger</div>', unsafe_allow_html=True)
                    if not findings:
                        st.markdown(
                            '<div class="cf good"><b>0 CONSTRAINT VIOLATIONS</b>'
                            '<p>All hospital bed limits, hub stock limits, and road statuses are respected.</p>'
                            f'<small>Verified on Plan v{review.get("plan_version")}</small></div>',
                            unsafe_allow_html=True,
                        )
                    for f_row in findings[:6]:
                        st.markdown(
                            f'<div class="cf bad"><b>{html.escape(str(f_row.get("area", "")).upper())} · {html.escape(str(f_row.get("status", "")).upper())} ({html.escape(str(f_row.get("risk", "")).upper())})</b>'
                            f'<p>{html.escape(str(f_row.get("why", "")))}</p>'
                            f'<small>{html.escape(str(f_row.get("recommendation", "")))}</small></div>',
                            unsafe_allow_html=True,
                        )
                with c_good:
                    st.markdown('<div class="eb" style="margin-bottom:10px">Coordinator &amp; OR-Tools verified allocations</div>', unsafe_allow_html=True)
                    for p_row in (plan.get("patients") or [])[:4]:
                        st.markdown(
                            f'<div class="cf good"><b>SOS #{p_row.get("incident_id")} · {p_row.get("patients")} PATIENTS ({engine.fmt_minutes(p_row.get("minutes"))} ETA)</b>'
                            f'<p>Destination: {html.escape(str(p_row.get("hospital")))} ({html.escape(str(p_row.get("zone")))})</p>'
                            f'<small>Corridor: {", ".join(p_row.get("route") or ["local"])}</small></div>',
                            unsafe_allow_html=True,
                        )
                    for s_row in (plan.get("supply") or [])[:4]:
                        st.markdown(
                            f'<div class="cf good"><b>{html.escape(str(s_row.get("zone")).upper())} · {s_row.get("qty")} {html.escape(str(s_row.get("item")))} ({engine.fmt_minutes(s_row.get("minutes"))} ETA)</b>'
                            f'<p>Source Hub: {html.escape(str(s_row.get("from_hub")))}</p>'
                            f'<small>Corridor: {", ".join(s_row.get("route") or ["local"])}</small></div>',
                            unsafe_allow_html=True,
                        )

            # TAB 3: Ripple Cascade Network (matches Tab 2 in Saarthi – Emergency Command Center.html)
            with t3:
                z_names = [z["name"] for z in data.read("SELECT name FROM zones ORDER BY name")]
                ov_origin = st.selectbox("Select origin zone for Ripple cascade diagram", z_names,
                                         index=z_names.index("Ratnagiri") if "Ratnagiri" in z_names else 0,
                                         key="ov_ripple_origin")
                _render_ripple_svg(ov_origin)

            # TAB 4: Local Broadcasts (MR / HI / EN) (matches Tab 3 in Saarthi – Emergency Command Center.html)
            with t4:
                st.markdown('<div class="eb" style="margin-bottom:8px">Multilingual Emergency Dispatch &amp; Early Warning Broadcasts (Marathi / Hindi / English)</div>', unsafe_allow_html=True)
                alerts_list = data.read("SELECT * FROM alerts ORDER BY id DESC LIMIT 4")
                for al in alerts_list:
                    z_n = al.get("zone") or "Zone"
                    dt_n = al.get("disaster_type") or "Hazard"
                    sv_n = al.get("severity") or "Red"
                    eta_n = al.get("eta_hours") or 24
                    rad_n = al.get("radius_km") or 30
                    st_n = al.get("sms_status") or "QUEUED"
                    st.markdown(
                        f'<div class="bc"><div class="bt2"><span>ALERT #{al["id"]} · {html.escape(str(z_n).upper())} ({html.escape(str(dt_n).upper())} · {html.escape(str(sv_n).upper())})</span>'
                        f'<span class="live">STATUS: {html.escape(str(st_n))} · MARATHI / HINDI / ENGLISH</span></div>'
                        f'<p><b>Marathi:</b> [SAARTHI सतर्कता] {html.escape(str(z_n))} परिमंडळ ({rad_n} किमी): {html.escape(str(dt_n))} ({html.escape(str(sv_n))}) इशारा नोंदवला आहे. अंदाजे वेळ: {eta_n} तास. सुरक्षित स्थळी राहा.</p>'
                        f'<p><b>Hindi:</b> [SAARTHI अलर्ट] {html.escape(str(z_n))} क्षेत्र ({rad_n} किमी): {html.escape(str(dt_n))} ({html.escape(str(sv_n))}) चेतावनी दर्ज की गई है। अनुमानित समय: {eta_n} घंटे। सुरक्षित स्थान पर रहें।</p>'
                        f'<p class="en"><b>English:</b> [SAARTHI EARLY WARNING] {html.escape(str(z_n))} ({rad_n} km radius) - {html.escape(str(sv_n))} {html.escape(str(dt_n))} alert recorded (ETA {eta_n}h). Status: {html.escape(str(st_n))}.</p></div>',
                        unsafe_allow_html=True,
                    )
                for p_row in (plan.get("patients") or [])[:3]:
                    z_n = p_row.get("zone") or "Zone"
                    hosp_n = p_row.get("hospital") or "Hospital"
                    pts_n = p_row.get("patients") or 0
                    inc_id = p_row.get("incident_id") or 0
                    eta_m = engine.fmt_minutes(p_row.get("minutes"))
                    via_r = ", ".join(p_row.get("route") or ["local"])
                    st.markdown(
                        f'<div class="bc"><div class="bt2"><span>SOS #{inc_id} · {html.escape(str(z_n).upper())} MEDICAL TRIAGE</span>'
                        f'<span class="live">PROPOSED PLAN · MARATHI / HINDI</span></div>'
                        f'<p><b>Marathi:</b> [SAARTHI सतर्कता] {html.escape(str(z_n))}: वैद्यकीय विनंती (SOS-{inc_id}) — {pts_n} रुग्णांसाठी ‘{html.escape(str(hosp_n))}’ येथे ‘{html.escape(via_r)}’ मार्गे ({html.escape(eta_m)}) नियोजन.</p>'
                        f'<p><b>Hindi:</b> [SAARTHI अलर्ट] {html.escape(str(z_n))}: चिकित्सा अनुरोध (SOS-{inc_id}) — {pts_n} मरीजों के लिए ‘{html.escape(str(hosp_n))}’ में ‘{html.escape(via_r)}’ मार्ग से ({html.escape(eta_m)}) योजना।</p>'
                        f'<p class="en"><b>English:</b> [SAARTHI MEDICAL PLAN] {html.escape(str(z_n))} - {pts_n} patients from SOS #{inc_id} routed via {html.escape(via_r)} (ETA {html.escape(eta_m)}) → {html.escape(str(hosp_n))}.</p></div>',
                        unsafe_allow_html=True,
                    )

            # TAB 5: SQLite Audit (All Tables) (matches Tab 4 in Saarthi – Emergency Command Center.html)
            with t5:
                st.markdown('<div class="eb">Inspect SQLite table</div>', unsafe_allow_html=True)
                tables = [
                    "zones", "roads", "hospitals", "hubs", "inventory",
                    "incidents", "alerts", "risk_logs", "supply_movement",
                    "plans", "plan_runs", "events", "citizen_reports",
                ]
                tb_col1, tb_col2 = st.columns([1, 2])
                chosen_tb = tb_col1.selectbox("SQLite Table", tables, key="ov_sqlite_tb")
                tb_col2.caption(f"Live query: SELECT * FROM {chosen_tb} (SQLite session state store)")
                tb_rows = data.read(f"SELECT * FROM {chosen_tb} LIMIT 100")
                if tb_rows:
                    render_df_table(df(tb_rows), hide_index=True)
                else:
                    st.caption(f"Table `{chosen_tb}` has no rows yet.")


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
            a, b = box.columns([3.4, 1.2])  # text | button
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

        .saarthi-road-open {{ color: #75818A; font-weight: 600; }}
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
                fig = go.Figure([go.Bar(name="Occupied beds", x=hs["name"], y=hs["total_beds"] - hs["available_beds"], marker_color="#C5CED3"),
                                 go.Bar(name="Free beds", x=hs["name"], y=hs["available_beds"], marker_color="#238C78")])  # stacked bars
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
                cls = "good" if c["ok"] else "bad"
                mark = "✓ PASSED" if c["ok"] else "✗ FAILED"
                st.markdown(
                    f'<div class="cf {cls}"><b>{mark} · {html.escape(str(c["check"]))}</b>'
                    f'<p>{html.escape(str(c["detail"]))}</p></div>',
                    unsafe_allow_html=True,
                )


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
            colours = {"CRITICAL": [185, 28, 28], "HIGH": [217, 88, 13], "MEDIUM": [201, 154, 6], "LOW": [47, 125, 79]}
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
        .saarthi-role-primary { color: #D9483B; border-color: #D9483B; }
        .saarthi-role-secondary { color: #E58A35; border-color: #E58A35; }
        .saarthi-role-extended { color: #E58A35; border-color: #E58A35; }
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
    colours = {"primary": [185, 28, 28], "secondary": [217, 88, 13], "extended": [201, 154, 6], "other": [156, 163, 175]}
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
        .saarthi-source-simulated { color: #E58A35; border-color: #E8C49A; background: #FBF3EA; }
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
    return [int(channel * 0.28 + 145) for channel in rgb]


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


def page_plan_challenger():  # read-only review of the stored plan
    with st.container(border=True):
        page_header("Plan Challenger", "Read-only review of the stored plan.")
        safety_notice(plan_challenger_agent.DISCLAIMER)
        st.caption("These findings are evaluated against the stored recorded plan. "
                   "This page is not reviewing a proposed plan. "
                   "The review was not approved and it was not executed. "
                   "A missing count is not zero.")
    result = plan_challenger_agent.review()  # reads the current plan; nothing is stored
    st.caption(f"RECORDED plan version {result['plan_version']}. Reviewed at {result['reviewed_at']}.")
    counts = result["counts"]
    metric_tiles((
        ("Confirmed", counts["confirmed"], "#D9483B"),
        ("Potential", counts["potential"], "#E58A35"),
        ("Missing data", counts["missing"], "#75818A"),
    ))
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


def render_nav():
    """One grouped menu. The current page uses the primary button style."""
    if "nav_page" not in st.session_state:
        st.session_state["nav_page"] = "Situation Overview"
    st.sidebar.markdown(
        '<div style="display:flex;align-items:center;gap:10px;padding-bottom:6px;">'
        '<svg width="32" height="32" viewBox="0 0 48 48" aria-hidden="true" fill="none" stroke="#E1E6E9" stroke-width="1.8">'
        '<circle cx="24" cy="24" r="21"/><circle cx="24" cy="24" r="5"/>'
        '<line x1="24" y1="3" x2="24" y2="45"/><line x1="3" y1="24" x2="45" y2="24"/>'
        '<line x1="9.2" y1="9.2" x2="38.8" y2="38.8"/><line x1="38.8" y1="9.2" x2="9.2" y2="38.8"/>'
        '<line x1="13.5" y1="4.5" x2="34.5" y2="43.5"/><line x1="34.5" y1="4.5" x2="13.5" y2="43.5"/>'
        '</svg>'
        '<div>'
        '<div style="font:600 18px Georgia,serif;letter-spacing:.24em;color:#F3F5F6;">SAARTHI</div>'
        '<div style="font:500 9.5px sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#C5D0D6;">ECC · PS-03</div>'
        '</div></div>',
        unsafe_allow_html=True)
    for index, (group, labels) in enumerate(NAV_GROUPS):
        if index:
            st.sidebar.divider()
        st.sidebar.caption(group)
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
live = st.sidebar.toggle("Live status", value=False, key="live_mode")
sidebar_demos()  # demo buttons
command_styles()
command_bar(choice, live)
render_top_controls()
# Draw once. A run_every fragment rebuilt the map, charts and every tab on
# each tick, so the running indicator never stayed off after the redesign.
PAGES[choice]()
st.markdown(
    '<div class="saarthi-foot">Prototype view · simulated data · Team Peaky Coders · AgentX Hackathon</div>',
    unsafe_allow_html=True)
