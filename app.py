"""Rally — per-pillar submissions and leadership rollup."""

from __future__ import annotations

import base64
import copy
import html
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from pillars import PILLARS, Pillar, get_pillar
from storage import MeetingStorage, NoteEntry, RollupRecord, SummaryRecord, read_uploaded_text
from summarizer import MeetingSummarizer

LEADERSHIP_CHAT_PROMPT = (ROOT / "prompts" / "leadership_chat.md").read_text(encoding="utf-8")

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _fmt_pacific(iso_utc: str) -> str:
    """Convert a UTC ISO-8601 string to a 'Jun 12, 2026 8:58 AM PT' display string."""
    if not iso_utc:
        return ""
    try:
        dt = datetime.fromisoformat(iso_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_PACIFIC).strftime("%b %d, %Y %-I:%M %p PT")
    except Exception:
        return iso_utc[:16].replace("T", " ") + " UTC"


MAX_TOKENS_PER_FILE = 12_500
MAX_TOKENS_PER_PILLAR = 20_000
CHARS_PER_TOKEN = 4


def _chars_to_tokens(n_chars: int) -> int:
    """Convert a character count to a rough token estimate (4 chars per token)."""
    return n_chars // CHARS_PER_TOKEN


st.set_page_config(
    page_title="Rally",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    /* ── Fonts & base ── */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    html, body, [class*="css"] { font-family: 'Inter', system-ui, -apple-system, sans-serif; }

    /* ── Warm background with subtle diagonal weave ── */
    .stApp {
        background-color: #f5f0eb;
        background-image:
            repeating-linear-gradient(
                45deg,
                transparent,
                transparent 6px,
                rgba(180,165,148,0.09) 6px,
                rgba(180,165,148,0.09) 7px
            ),
            repeating-linear-gradient(
                -45deg,
                transparent,
                transparent 6px,
                rgba(180,165,148,0.09) 6px,
                rgba(180,165,148,0.09) 7px
            );
    }

    /* ── Nike black nav bar ── */
    header[data-testid="stHeader"] {
        background-color: #111111 !important;
        border-bottom: none !important;
        height: 3.75rem !important;
        min-height: 3.75rem !important;
    }
    header[data-testid="stHeader"]::before {
        content: 'NIKE  |  RALLY';
        position: absolute;
        left: 20px; top: 50%; transform: translateY(-50%);
        font-size: 11px; font-weight: 700; letter-spacing: 2.5px;
        color: #cccccc; text-transform: uppercase;
    }
    /* Hamburger/settings icon in white */
    header[data-testid="stHeader"] svg { fill: #ffffff !important; stroke: #ffffff !important; }
    /* Running indicator (text + spinner) forced white via filter */
    header[data-testid="stHeader"] [data-testid="stStatusWidget"] {
        filter: brightness(0) invert(1) !important;
    }

    /* Override: header elements stay white against black bar */
    header[data-testid="stHeader"] * { color: #ffffff !important; }

    /* Hide sidebar — logo and nav live on main page */
    [data-testid="stSidebar"],
    [data-testid="stSidebarCollapsedControl"],
    button[data-testid="stSidebarCollapsedControl"] {
        display: none !important;
    }
    section[data-testid="stMain"] > div {
        max-width: 100% !important;
    }
    section[data-testid="stMain"] .block-container {
        padding-top: 0 !important;
    }

    /* Page header row — no card chrome on header columns */
    section[data-testid="stMain"] .block-container > div:first-child {
        margin-bottom: 0.5rem;
    }
    section[data-testid="stMain"] .block-container > div:first-child [data-testid="stVerticalBlock"],
    section[data-testid="stMain"] .block-container > div:first-child [data-testid="column"] {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
    }
    /* Collapse the default vertical block gap for the logo/caption stack only */
    section[data-testid="stMain"] .block-container > div:first-child [data-testid="stVerticalBlock"] {
        gap: 0 !important;
    }

    /* Logo — below header, left-aligned, no extra spacing */
    [data-testid="stImage"] {
        width: 100% !important;
        margin-top: 0 !important;
        margin-bottom: 0 !important;
    }
    [data-testid="stImage"] img {
        display: block !important;
        height: 360px !important;
        width: 100% !important;
        max-width: 100% !important;
        object-fit: cover !important;
        object-position: center top !important;
        margin-left: 0 !important;
        margin-bottom: 0 !important;
    }
    /* Hide Streamlit's "Press Ctrl+Enter to apply" hint on text areas */
    [data-testid="InputInstructions"],
    [data-testid="stWidgetInstructions"] {
        display: none !important;
    }

    /* Remove Streamlit's hover fullscreen/zoom button on images */
    [data-testid="stImage"] button,
    [data-testid="stImage"] [data-testid="StyledFullScreenButton"],
    [data-testid="stImage"] [data-testid="stImageFullscreenButton"],
    [data-testid="stFullScreenFrame"] > button {
        display: none !important;
    }

    /* Caption beside logo */
    [data-testid="stImage"] + [data-testid="stCaptionContainer"],
    [data-testid="stCaptionContainer"] {
        margin-top: 0.2rem !important;
        margin-bottom: 0.4rem !important;
    }

    /* ── Section headers: orange left-border accent ── */
    h2 {
        border-left: 4px solid #f05a28 !important;
        padding-left: 12px !important;
        color: #1a1a1a !important;
        font-size: 20px !important;
        font-weight: 800 !important;
        letter-spacing: -0.2px !important;
        margin-top: 1.4rem !important;
        margin-bottom: 0.4rem !important;
    }
    h3 {
        border-left: 4px solid #f05a28 !important;
        padding-left: 12px !important;
        color: #1a1a1a !important;
        font-size: 18px !important;
        font-weight: 800 !important;
        letter-spacing: -0.2px !important;
        margin-top: 1rem !important;
        margin-bottom: 0.3rem !important;
    }
    h1 {
        color: #1a1a1a !important;
        font-weight: 800 !important;
    }

    /* ── White content cards (scoped — not page header) ── */
    [data-testid="stForm"],
    [data-testid="stExpander"] {
        background-color: #ffffff;
        border: 1px solid #e0ddd9;
        border-radius: 6px;
    }
    .stTabs div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlock"] {
        background-color: #ffffff;
        border: 1px solid #e0ddd9;
        border-radius: 6px;
    }

    /* ── Prevent button labels from wrapping mid-character ── */
    .stButton > button { white-space: nowrap; }

    /* ── Orange primary buttons ── */
    button[kind="primary"],
    .stButton > button[kind="primary"],
    button[data-testid="baseButton-primary"] {
        background-color: #f05a28 !important;
        border-color: #f05a28 !important;
        color: #ffffff !important;
        font-weight: 600 !important;
        border-radius: 4px !important;
    }
    button[kind="primary"]:hover,
    .stButton > button[kind="primary"]:hover {
        background-color: #d44e20 !important;
        border-color: #d44e20 !important;
    }

    /* ── Secondary & download buttons ── */
    button[kind="secondary"],
    .stButton > button[kind="secondary"],
    button[data-testid="baseButton-secondary"],
    button[data-testid="stDownloadButton"],
    [data-testid="stDownloadButton"] > button,
    .stDownloadButton > button {
        background-color: #ffffff !important;
        border: 1px solid #d8d3cc !important;
        color: #222222 !important;
        font-weight: 500 !important;
        border-radius: 4px !important;
    }
    button[kind="secondary"]:hover,
    .stButton > button[kind="secondary"]:hover,
    [data-testid="stDownloadButton"] > button:hover,
    .stDownloadButton > button:hover {
        border-color: #f05a28 !important;
        color: #f05a28 !important;
        background-color: #fff3ee !important;
    }

    /* ── Tabs: styled pill tabs matching the design ── */
    .stTabs [data-baseweb="tab-list"] {
        gap: 3px;
        flex-wrap: nowrap;
        justify-content: space-between;
        width: 100%;
        background: transparent !important;
        border-bottom: 1px solid #e0ddd9;
        padding-bottom: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        white-space: nowrap;
        background-color: #ffffff !important;
        border: 1px solid #e0ddd9 !important;
        border-radius: 4px !important;
        color: #111111 !important;
        font-size: 12px !important;
        font-weight: 500 !important;
        padding: 4px 8px !important;
    }
    .stTabs [aria-selected="true"] {
        background-color: #f05a28 !important;
        border-color: #f05a28 !important;
        color: #ffffff !important;
        font-weight: 600 !important;
    }
    .stTabs [data-baseweb="tab-highlight"] { display: none !important; }
    .stTabs [data-baseweb="tab-border"] { display: none !important; }

    /* "Ask Rally" — last tab of the 9-tab top-level strip, styled in light grey */
    .stTabs [data-baseweb="tab-list"]:has(> [data-baseweb="tab"]:nth-of-type(9)) > [data-baseweb="tab"]:last-of-type {
        background-color: #e5e5e5 !important;
        border-color: #cfcfcf !important;
        color: #222222 !important;
        font-weight: 600 !important;
    }
    .stTabs [data-baseweb="tab-list"]:has(> [data-baseweb="tab"]:nth-of-type(9)) > [data-baseweb="tab"]:last-of-type:hover {
        background-color: #d8d8d8 !important;
        border-color: #bfbfbf !important;
        color: #111111 !important;
    }
    .stTabs [data-baseweb="tab-list"]:has(> [data-baseweb="tab"]:nth-of-type(9)) > [data-baseweb="tab"]:last-of-type[aria-selected="true"] {
        background-color: #e5e5e5 !important;
        border-color: #f05a28 !important;
        color: #111111 !important;
    }

    /* ── Metric cards ── */
    [data-testid="stMetric"] {
        background-color: #ffffff;
        border: 1px solid #e0ddd9;
        border-radius: 6px;
        padding: 12px 16px;
    }
    [data-testid="stMetricValue"] { color: #111111 !important; font-weight: 700 !important; }
    [data-testid="stMetricLabel"] {
        color: #999999 !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        font-size: 10px !important;
    }

    /* ── Main block container ── */
    .block-container {
        max-width: 1100px;
        padding-left: 2rem;
        padding-right: 2rem;
    }

    /* ── Dividers ── */
    hr { border-color: #e0ddd9 !important; }

    /* ── Info/success/warning boxes ── */
    [data-testid="stAlert"] {
        border-radius: 4px !important;
        border-left-width: 3px !important;
    }

    /* ── Caption below logo ── */
    [data-testid="stCaptionContainer"] {
        margin-top: 0.05rem !important;
        margin-bottom: 0.75rem !important;
        color: #666666 !important;
        font-size: 12px !important;
    }

    /* ── All text inputs, textareas, select boxes ── */
    textarea,
    input[type="text"],
    input[type="number"],
    input[type="search"],
    input[type="email"],
    input[type="password"],
    [data-testid="stTextInput"] input,
    [data-testid="stTextArea"] textarea,
    [data-testid="stNumberInput"] input,
    [data-baseweb="textarea"],
    [data-baseweb="input"],
    [data-baseweb="base-input"] {
        background-color: #ffffff !important;
        color: #111111 !important;
        border-color: #d8d3cc !important;
        border-radius: 4px !important;
        font-family: 'Inter', system-ui, sans-serif !important;
        font-size: 13px !important;
    }
    /* Focus ring in orange */
    textarea:focus,
    input:focus,
    [data-baseweb="textarea"]:focus-within,
    [data-baseweb="input"]:focus-within,
    [data-baseweb="base-input"]:focus-within {
        border-color: #f05a28 !important;
        box-shadow: 0 0 0 2px rgba(240,90,40,0.15) !important;
        outline: none !important;
    }
    /* Label text above inputs */
    [data-testid="stTextInput"] label,
    [data-testid="stTextArea"] label,
    [data-testid="stNumberInput"] label,
    [data-testid="stSelectbox"] label,
    [data-testid="stMultiSelect"] label,
    .stTextArea label, .stTextInput label {
        color: #444444 !important;
        font-size: 11px !important;
        font-weight: 600 !important;
        letter-spacing: 0.5px !important;
        text-transform: uppercase !important;
    }
    /* Placeholder text */
    textarea::placeholder, input::placeholder {
        color: #b0a89e !important;
        font-style: italic;
    }

    /* ── Selectbox / dropdown ── */
    [data-testid="stSelectbox"] > div > div,
    [data-testid="stMultiSelect"] > div > div,
    [data-baseweb="select"] > div,
    [data-baseweb="select"] span,
    [data-baseweb="select"] input {
        background-color: #ffffff !important;
        border-color: #d8d3cc !important;
        border-radius: 4px !important;
        color: #111111 !important;
    }
    /* ── Radio buttons (History "Jump to submission") ── */
    [data-testid="stRadio"] > label {
        font-size: 11px !important;
        font-weight: 600 !important;
        letter-spacing: 0.5px !important;
        text-transform: uppercase !important;
        color: #444444 !important;
    }
    [data-testid="stRadio"] div[role="radiogroup"] label span {
        color: #111111 !important;
        font-size: 13px !important;
    }

    /* ── File uploader ── */
    [data-testid="stFileUploader"],
    [data-testid="stFileUploader"] section,
    [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"],
    [data-testid="stFileUploader"] > div,
    [data-testid="stFileUploader"] > div > div {
        background-color: #f5f0eb !important;
        border-radius: 6px !important;
    }
    [data-testid="stFileUploader"] section,
    [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"],
    [data-testid="stFileUploader"] > div > div {
        background-color: #ffffff !important;
        border: 1.5px dashed #d8d3cc !important;
        color: #444444 !important;
    }
    [data-testid="stFileUploader"] label {
        color: #444444 !important;
        font-size: 11px !important;
        font-weight: 600 !important;
        letter-spacing: 0.5px !important;
        text-transform: uppercase !important;
    }
    [data-testid="stFileUploader"] small,
    [data-testid="stFileUploader"] span,
    [data-testid="stFileUploader"] p,
    [data-testid="stFileUploader"] [data-testid="stMarkdownContainer"] p {
        color: #777777 !important;
        font-size: 12px !important;
    }
    [data-testid="stFileUploader"] svg {
        fill: #888888 !important;
        color: #888888 !important;
    }
    /* Browse Files button */
    [data-testid="stFileUploader"] button {
        background-color: #ffffff !important;
        color: #111111 !important;
        border: 1px solid #d8d3cc !important;
        border-radius: 4px !important;
        font-size: 12px !important;
        font-weight: 600 !important;
    }
    [data-testid="stFileUploader"] button:hover {
        border-color: #f05a28 !important;
        color: #f05a28 !important;
    }
    /* Hide help/tooltip question mark icons */
    [data-testid="stTooltipHoverTarget"],
    [data-testid="stFileUploader"] [data-testid="stTooltipHoverTarget"],
    [data-testid="stWidgetLabel"] [data-testid="stTooltipHoverTarget"] {
        display: none !important;
    }

    /* ── Global body text — unified size and color ── */
    p, li, div, label, span,
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stText"],
    .stMarkdown p {
        color: #222222 !important;
        font-family: 'Inter', system-ui, sans-serif !important;
        font-size: 14px !important;
        line-height: 1.6 !important;
    }
    /* Muted helper / caption text */
    small, .caption, [data-testid="stCaptionContainer"],
    [data-testid="stCaptionContainer"] p {
        color: #777777 !important;
        font-size: 12px !important;
        line-height: 1.4 !important;
    }

    /* ── Code/monospace blocks ── */
    code, pre {
        background-color: #f0ece6 !important;
        color: #333333 !important;
        border: 1px solid #e0ddd9 !important;
        border-radius: 3px !important;
    }

    /* ── Checkbox & radio ── */
    [data-testid="stCheckbox"] label,
    [data-testid="stRadio"] label {
        color: #222222 !important;
        font-size: 13px !important;
    }

    /* ── Tab content card — wraps all pillar/rollup content ── */
    [role="tabpanel"] {
        background-color: #ffffff !important;
        border: 1px solid #e0ddd9 !important;
        border-radius: 0 10px 10px 10px !important;
        padding: 1.75rem 2rem 2rem 2rem !important;
        box-shadow: 0 2px 10px rgba(0,0,0,0.06) !important;
        margin-top: 0.25rem !important;
    }

    /* ── Streamlit settings modal — dark text on light background ── */
    [role="dialog"],
    [data-testid="stModal"],
    .stModal {
        color: #1a1a1a !important;
    }
    [role="dialog"] p,
    [role="dialog"] span,
    [role="dialog"] label,
    [role="dialog"] div,
    [role="dialog"] h1,
    [role="dialog"] h2,
    [role="dialog"] h3 {
        color: #1a1a1a !important;
    }
    /* Settings modal header and body text */
    header[data-testid="stModalHeader"] *,
    [data-testid="stModalContent"] *,
    [data-testid="stSettings"] * {
        color: #1a1a1a !important;
    }
    /* BaseWeb modal used by Streamlit */
    [data-baseweb="modal"] p,
    [data-baseweb="modal"] span,
    [data-baseweb="modal"] label,
    [data-baseweb="modal"] div:not([data-testid="stCheckbox"] svg),
    [data-baseweb="modal"] h1,
    [data-baseweb="modal"] h2,
    [data-baseweb="modal"] h3 {
        color: #1a1a1a !important;
    }

    /* ── Admin buttons in the tab row (Status / Config / Prompts) ── */
    [data-testid="column"] button[kind="secondary"] {
        font-size: 12px !important;
        padding: 4px 6px !important;
        line-height: 1.3 !important;
    }

    /* ── BaseWeb dropdown — force option text to dark on white in all container variants ── */
    [data-baseweb="menu"],
    [data-baseweb="select-dropdown"],
    [data-baseweb="popover"] [role="listbox"] {
        background-color: #ffffff !important;
    }
    [data-baseweb="menu"] *,
    [data-baseweb="select-dropdown"] *,
    [data-baseweb="popover"] [role="listbox"] *,
    [role="option"],
    [role="option"] *,
    [role="option"] span,
    [role="option"] div {
        color: #1a1a1a !important;
        -webkit-text-fill-color: #1a1a1a !important;
    }
    [role="option"]:hover,
    [role="option"][aria-selected="true"] {
        background-color: rgba(240, 90, 40, 0.12) !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

settings = get_settings()

# Databricks Apps injects user identity via several possible headers.
# X-Forwarded-User often carries a numeric workspace ID rather than an email;
# try email-bearing headers first so admin comparisons work correctly.
_current_user: str = (
    st.context.headers.get("X-Forwarded-Email", "")
    or st.context.headers.get("x-forwarded-email", "")
    or st.context.headers.get("X-Forwarded-Preferred-Username", "")
    or st.context.headers.get("X-Forwarded-User-Name", "")
    or st.context.headers.get("X-Forwarded-User", "")
    or st.context.headers.get("x-forwarded-user", "")
    or "unknown"
)


@st.cache_resource
def _get_storage() -> MeetingStorage:
    return MeetingStorage(get_settings())


@st.cache_resource
def _get_summarizer() -> MeetingSummarizer:
    return MeetingSummarizer(get_settings())


storage = _get_storage()
summarizer = _get_summarizer()


@st.cache_data(ttl=60)
def _cached_latest_per_pillar() -> dict:
    return _get_storage().list_latest_per_pillar()


@st.cache_data(ttl=60, show_spinner=False)
def _cached_pillar_recent(pillar_slug: str) -> list[SummaryRecord]:
    return _get_storage().list_summaries(limit=1, pillar=pillar_slug)


@st.cache_data(ttl=60, show_spinner=False)
def _cached_pillar_week_notes(pillar_slug: str, week_key: str) -> list[NoteEntry]:
    return _get_storage().list_notes(pillar_slug, week_key=week_key)


@st.cache_data(ttl=60, show_spinner=False)
def _cached_pillar_all_notes(pillar_slug: str) -> list[NoteEntry]:
    return _get_storage().list_notes(pillar_slug)


@st.cache_data(ttl=60, show_spinner=False)
def _cached_pillar_history(pillar_slug: str, limit: int = 30) -> list[SummaryRecord]:
    return _get_storage().list_summaries(limit=limit, pillar=pillar_slug)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_prompt_text(name: str) -> str:
    return summarizer.load_prompt(name)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_prompt_meta(name: str) -> dict | None:
    return summarizer.prompt_meta(name)


_logo_bytes = (ROOT / "assets" / "rally_header.png").read_bytes()
_logo_b64 = base64.b64encode(_logo_bytes).decode("ascii")
st.markdown(
    f'<img src="data:image/png;base64,{_logo_b64}" '
    f'alt="Rally" '
    f'style="display:block; width:100%; height:auto; margin:0;" />',
    unsafe_allow_html=True,
)
st.markdown(
    "<div style='color:#777777; font-size:13px; margin-top:0.5rem; margin-bottom:0.5rem;'>"
    "Per-pillar work submissions, weekly tracker updates, and leadership rollups."
    "</div>",
    unsafe_allow_html=True,
)


if storage.backend == "local" and settings.databricks_host:
    st.error(
        "Storage fell back to local disk — submissions will be lost on the next deployment. "
        "Check that the UC Volume path is correct and accessible.",
        icon="🚨",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompts dialog (view / edit LLM prompts)
# ─────────────────────────────────────────────────────────────────────────────
@st.dialog("View / Edit Prompts", width="large")
def _prompts_dialog() -> None:
    _is_admin = settings.is_admin(_current_user)
    st.caption(
        "Admin editing enabled — changes apply to the next generation."
        if _is_admin
        else "Read-only view. Contact an admin to request changes."
    )

    _prompt_options = [
        ("extract", "Pillar Level"),
        ("rollup", "Team Level"),
        ("exec_rollup", "Org Level"),
        ("leadership_chat", "Leadership chat"),
    ]

    def _render_prompt(key: str) -> None:
        try:
            with st.spinner("Loading prompt…"):
                _ptext = _cached_prompt_text(key)
                _pmeta = _cached_prompt_meta(key)
        except Exception as exc:
            st.error(f"Could not load prompt: {exc}")
            return
        if _pmeta:
            st.caption(
                f"Last edited by {_pmeta.get('updated_by', 'unknown')} "
                f"on {(_pmeta.get('updated_at') or '')[:10]}"
            )
        else:
            st.caption("Using the default prompt shipped with the app.")

        if _is_admin:
            _edited = st.text_area(
                "Prompt body",
                value=_ptext,
                height=400,
                key=f"prompts_dialog_text_{key}",
                label_visibility="collapsed",
            )
            col_save, col_reset, col_dl = st.columns([1, 1, 1])
            with col_save:
                if st.button("Save", key=f"prompts_dialog_save_{key}", type="primary"):
                    if not _edited.strip():
                        st.warning("Prompt body cannot be empty.")
                    else:
                        try:
                            summarizer.save_prompt(
                                key, _edited, edited_by=_current_user or "unknown"
                            )
                            _cached_prompt_text.clear()
                            _cached_prompt_meta.clear()
                            st.success("Saved. Next generation will use the new prompt.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Save failed: {exc}")
            with col_reset:
                if _pmeta and st.button("Reset to default", key=f"prompts_dialog_reset_{key}"):
                    try:
                        summarizer.reset_prompt(key)
                        _cached_prompt_text.clear()
                        _cached_prompt_meta.clear()
                        st.success("Reverted to default.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Reset failed: {exc}")
            with col_dl:
                st.download_button(
                    "Download",
                    data=_edited,
                    file_name=f"{key}.md",
                    key=f"prompts_dialog_dl_{key}",
                )
        else:
            st.code(_ptext, language="markdown")
            st.download_button(
                "Download",
                data=_ptext,
                file_name=f"{key}.md",
                key=f"prompts_dialog_dl_{key}",
            )

    _tabs = st.tabs([label for _, label in _prompt_options])
    for _tab, (_key, _) in zip(_tabs, _prompt_options):
        with _tab:
            _render_prompt(_key)


def _render_prompts_button(key_suffix: str) -> None:
    if st.button("Prompts", key=f"prompts_btn_{key_suffix}"):
        _prompts_dialog()


# ─────────────────────────────────────────────────────────────────────────────
# Per-pillar renderer
# ─────────────────────────────────────────────────────────────────────────────
def _result_key(slug: str) -> str:
    return f"last_result_{slug}"


_EDIT_FORM_FIELDS = (
    "title",
    "at_a_glance",
    "decisions",
    "closures",
    "new_tracks",
    "risks",
    "next_steps",
)


def _clear_edit_form_state(slug: str) -> None:
    """Remove all per-pillar edit-form widget keys so the next Edit session
    starts from the current extract instead of stale leftovers."""
    for field in _EDIT_FORM_FIELDS:
        st.session_state.pop(f"edit_{field}_{slug}", None)


_TEAM_ROLLUP_EDIT_FIELDS = (
    "rollup_at_a_glance",
    "rollup_cross_dep",
    "rollup_blockers",
    "rollup_decisions",
    "rollup_capacity_risks",
    "rollup_next_steps",
)

_ORG_ROLLUP_EDIT_FIELDS = (
    "org_headline",
    "org_top_priorities",
    "org_critical_issues",
    "org_decisions",
    "org_body_text",
)


def _clear_rollup_edit_state(save_key: str, kind: str) -> None:
    """Remove all rollup edit-form widget keys for the given save_key."""
    fields = _TEAM_ROLLUP_EDIT_FIELDS if kind == "team" else _ORG_ROLLUP_EDIT_FIELDS
    for f in fields:
        st.session_state.pop(f"{f}_{save_key}", None)
    stale = [k for k in st.session_state if k.startswith(f"rollup_highlights_{save_key}_")]
    for k in stale:
        st.session_state.pop(k, None)


def _note_label_parts(note: NoteEntry) -> tuple[str, str]:
    """Return (author_display, preview) for a saved-note label.

    Author: strips @domain and replaces dots with spaces.
    Preview: for file-based notes shows the filename; for paste notes shows the
    first 80 characters of whitespace-collapsed text.
    """
    raw = note.author or ""
    local = raw.split("@", 1)[0]
    author_display = local.replace(".", " ").strip() or raw

    text = (note.text or "").strip()
    file_match = re.match(r"\[File:\s*([^\]]+)\]", text)
    if file_match:
        preview = f"File: {file_match.group(1).strip()}"
    else:
        flat = re.sub(r"\s+", " ", text).strip()
        preview = (flat[:80] + "\u2026") if len(flat) > 80 else flat
    return author_display, preview


def _render_note_card(
    note: NoteEntry,
    *,
    pillar_slug: str,
    location: str,
    use_expander: bool = True,
) -> bool:
    """Render a single saved note. Returns True if the note was deleted.

    When use_expander is False (e.g. already inside an expander), renders inline
    with a header row instead of wrapping in a nested expander.
    """
    ts = _fmt_pacific(note.created_at) if note.created_at else ""
    safe_text = html.escape(note.text)
    body_html = (
        f"<div style='background:#f5f0eb;border-radius:6px;"
        f"padding:12px 16px;white-space:pre-wrap;"
        f"font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        f"font-size:13px;color:#1a1a1a;'>{safe_text}</div>"
    )
    if use_expander:
        author_display, preview = _note_label_parts(note)
        safe_preview = html.escape(preview)
        label = f"{author_display}  ·  {ts}  ·  {safe_preview}"
        with st.expander(label, expanded=False):
            col_meta, col_del = st.columns([5, 1])
            with col_meta:
                st.caption(f"{len(note.text):,} characters")
            with col_del:
                with st.popover("🗑 Delete", use_container_width=True):
                    st.warning("This will permanently delete this note.")
                    if st.button(
                        "Yes, delete",
                        key=f"del_note_{note.id}_{pillar_slug}_{location}",
                        type="primary",
                    ):
                        storage.delete_note(note.id, pillar_slug)
                        _cached_pillar_week_notes.clear()
                        _cached_pillar_all_notes.clear()
                        return True
            st.markdown(body_html, unsafe_allow_html=True)
    else:
        author_display, _ = _note_label_parts(note)
        col_meta, col_del = st.columns([5, 1])
        with col_meta:
            st.markdown(
                f"**{html.escape(author_display)}** · {ts} · "
                f"<span style='color:#666;font-size:12px;'>{len(note.text):,} chars</span>",
                unsafe_allow_html=True,
            )
        with col_del:
            with st.popover("🗑 Delete", use_container_width=True):
                st.warning("This will permanently delete this note.")
                if st.button(
                    "Yes, delete",
                    key=f"del_note_{note.id}_{pillar_slug}_{location}",
                    type="primary",
                ):
                    storage.delete_note(note.id, pillar_slug)
                    _cached_pillar_week_notes.clear()
                    _cached_pillar_all_notes.clear()
                    return True
        st.markdown(body_html, unsafe_allow_html=True)
        st.markdown("<hr style='margin:8px 0;border:none;border-top:1px solid #e0d8d0;'>", unsafe_allow_html=True)
    return False


def render_pillar(pillar: Pillar) -> None:
    st.subheader(pillar.name)
    st.caption(
        "Upload meeting notes for this pillar to generate a one-pager. "
        "History below is scoped to this pillar only."
    )

    _recent = _cached_pillar_recent(pillar.slug)
    if _recent:
        _r = _recent[0]
        _eff = MeetingStorage._record_date(_r)
        _date_label = _eff.strftime("%B %d, %Y") if _eff else "unknown date"
        _last_title = _r.meeting_title or f"{pillar.name} Weekly Update"
        st.info(f"Last submission: **{_date_label}** — {_last_title}")
    else:
        st.info("No submissions yet for this pillar.")

    sub_gen, sub_hist = st.tabs(["Generate", "History"])

    # ── Generate ──────────────────────────────────────────────────────────
    with sub_gen:
        uploaded = st.file_uploader(
            "Meeting notes",
            type=["txt", "md", "docx", "pdf", "xlsx", "xls"],
            key=f"upload_{pillar.slug}_{st.session_state.get(f'upload_nonce_{pillar.slug}', 0)}",
            accept_multiple_files=True,
        )
        # ── Load this week's saved notes before rendering the text area so they
        #    are included in the live token total from the first page render ──
        _week_key = storage._current_week_key()
        _week_notes = _cached_pillar_week_notes(pillar.slug, _week_key)

        st.markdown("**Additional context / manual notes**")
        extra_notes = st.text_area(
            "Add context before generating",
            height=180,
            placeholder=(
                "Add anything Zoom may have missed:\n"
                "- Side conversations\n"
                "- Corrections to the notes\n"
                "- Pre-meeting context\n"
                "- Decisions made offline"
            ),
            label_visibility="collapsed",
            key=f"extra_{pillar.slug}_{st.session_state.get(f'extra_nonce_{pillar.slug}', 0)}",
        )

        # ── Compute live combined token estimate (used for progress bar and Generate button) ──
        _uploaded_chars = 0
        if uploaded:
            try:
                _uploaded_chars = sum(
                    len(read_uploaded_text(f.name, f.getvalue())) for f in uploaded
                )
            except Exception:
                _uploaded_chars = 0
        _combined_tokens_live = _chars_to_tokens(
            _uploaded_chars + len(extra_notes) + sum(len(n.text) for n in _week_notes)
        )
        _over_limit = _combined_tokens_live > MAX_TOKENS_PER_PILLAR

        _pct = min(_combined_tokens_live / MAX_TOKENS_PER_PILLAR, 1.0)
        _pct_display = round(100 * _combined_tokens_live / MAX_TOKENS_PER_PILLAR)
        st.caption(
            f"Input budget: {_combined_tokens_live:,} / {MAX_TOKENS_PER_PILLAR:,} tokens used ({_pct_display}%)"
            + (" — over limit" if _over_limit else "")
        )
        st.progress(_pct)

        # ── Action buttons ─────────────────────────────────────────────
        use_context = True
        use_registry = True

        _save_clicked = st.button(
            "💾 Save Note",
            key=f"save_note_{pillar.slug}",
        )
        _gen_clicked = st.button(
            "Generate one-pager",
            type="primary",
            key=f"gen_{pillar.slug}",
            disabled=_over_limit,
            help=(
                f"Combined input exceeds {MAX_TOKENS_PER_PILLAR:,}-token limit. "
                "Remove some saved notes or shorten the additional context."
                if _over_limit else None
            ),
        )

        if _save_clicked:
            if not (uploaded or extra_notes.strip()):
                st.warning("Please attach a file or add notes in the Additional context box before saving.")
            else:
                _author = _current_user
                _saved_count = 0
                if uploaded:
                    for f in uploaded:
                        raw = f.getvalue()
                        try:
                            _file_text = read_uploaded_text(f.name, raw)
                        except Exception as exc:
                            st.warning(f"Could not read {f.name}: {exc}")
                            continue
                        if _chars_to_tokens(len(_file_text)) > MAX_TOKENS_PER_FILE:
                            st.error(
                                f"**{f.name}** is too large: {_chars_to_tokens(len(_file_text)):,} tokens. "
                                f"Maximum allowed is {MAX_TOKENS_PER_FILE:,} tokens per file. "
                                "Please trim the transcript and re-upload."
                            )
                            continue
                        storage.save_upload(f.name, raw, pillar=pillar.slug)
                        storage.save_note(
                            _author,
                            f"[File: {f.name}]\n\n{_file_text}",
                            pillar=pillar.slug,
                        )
                        _saved_count += 1
                if extra_notes.strip():
                    storage.save_note(_author, extra_notes.strip(), pillar=pillar.slug)
                    _saved_count += 1
                _cached_pillar_week_notes.clear()
                _cached_pillar_all_notes.clear()
                st.session_state[f"upload_nonce_{pillar.slug}"] = (
                    st.session_state.get(f"upload_nonce_{pillar.slug}", 0) + 1
                )
                st.session_state[f"extra_nonce_{pillar.slug}"] = (
                    st.session_state.get(f"extra_nonce_{pillar.slug}", 0) + 1
                )
                st.success(f"Saved {_saved_count} item{'s' if _saved_count != 1 else ''}.")
                st.rerun()

        if _gen_clicked:
            if not (uploaded or extra_notes.strip() or _week_notes):
                st.warning("Please upload a file, add notes in the Additional context box, or save notes this week before generating.")
            elif _combined_tokens_live > MAX_TOKENS_PER_PILLAR:
                st.error(
                    f"Combined input is too large: {_combined_tokens_live:,} tokens. "
                    f"Maximum allowed is {MAX_TOKENS_PER_PILLAR:,} tokens per Generate call. "
                    "Remove some saved notes (delete with the trash icon below), "
                    "shorten the additional context, or remove one of the uploaded files."
                )
            else:
                try:
                    notes = ""
                    source_name = "uploaded_notes.txt"
                    upload_path = None

                    if uploaded:
                        file_parts = []
                        for f in uploaded:
                            raw = f.getvalue()
                            file_parts.append(read_uploaded_text(f.name, raw))
                            storage.save_upload(f.name, raw, pillar=pillar.slug)
                        notes = "\n\n".join(file_parts)
                        source_name = ", ".join(f.name for f in uploaded)
                        upload_path = None

                    if extra_notes.strip():
                        if notes.strip():
                            notes = notes + "\n\n## Additional context (manually added)\n\n" + extra_notes.strip()
                        else:
                            notes = extra_notes.strip()

                    if _week_notes:
                        weekly_block = "\n\n".join(
                            f"## Notes from {n.author} ({_fmt_pacific(n.created_at)[:12]})\n{n.text}"
                            for n in _week_notes
                        )
                        if notes.strip():
                            notes = notes + "\n\n## This week's saved notes\n\n" + weekly_block
                        else:
                            notes = weekly_block
                            source_name = f"weekly_notes_{_week_key}.txt"

                    if not notes.strip():
                        st.error("No text found in upload or additional context area.")
                        st.stop()

                    context_parts: list[str] = []
                    if use_context:
                        ctx = storage.load_recent_context(pillar=pillar.slug)
                        if ctx:
                            context_parts.append(ctx)
                    if use_registry:
                        reg_ctx = storage.registry_as_context(pillar=pillar.slug)
                        if reg_ctx:
                            context_parts.append(reg_ctx)
                    context = "\n\n".join(context_parts)

                    with st.spinner("Extracting meeting intelligence…"):
                        if settings.mock_llm:
                            st.error("MOCK_LLM is enabled. Disable MOCK_LLM for live runs.")
                            st.stop()
                        result = summarizer.summarize(notes, context=context, pillar_name=pillar.name)

                    st.success("One-pager generated. Review below and click Submit to History when ready.")
                    st.session_state[_result_key(pillar.slug)] = {
                        "markdown": result.markdown,
                        "html": result.html,
                        "extract": copy.deepcopy(result.extract),
                        "upload_path": upload_path,
                        "source_name": source_name,
                        "submitted": False,
                    }
                    _clear_edit_form_state(pillar.slug)
                    try:
                        storage.update_registry_from_extract(
                            result.extract, pillar=pillar.slug
                        )
                    except Exception as exc:
                        print(
                            f"[rally.registry_update] failed for pillar={pillar.slug}: {exc}",
                            flush=True,
                        )
                    st.session_state[f"extra_nonce_{pillar.slug}"] = (
                        st.session_state.get(f"extra_nonce_{pillar.slug}", 0) + 1
                    )
                    st.session_state[f"upload_nonce_{pillar.slug}"] = (
                        st.session_state.get(f"upload_nonce_{pillar.slug}", 0) + 1
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"**Error:** {exc}")

        last = st.session_state.get(_result_key(pillar.slug))
        if last:
            st.divider()

            # ── Edit / Submit action bar ──────────────────────────────────
            _edit_mode_key = f"edit_mode_{pillar.slug}"
            col_edit_btn, col_submit_btn, col_save_btn, col_spacer = st.columns([1.4, 1.4, 1.4, 5])
            with col_edit_btn:
                if st.button(
                    "✏️ Edit" if not st.session_state.get(_edit_mode_key) else "Cancel",
                    key=f"toggle_edit_{pillar.slug}",
                ):
                    _was_active = st.session_state.get(_edit_mode_key, False)
                    st.session_state[_edit_mode_key] = not _was_active
                    print(
                        f"[rally.edit_toggle] pillar={pillar.slug} "
                        f"was_active={_was_active} now={not _was_active}",
                        flush=True,
                    )
                    if _was_active:
                        _clear_edit_form_state(pillar.slug)
                    st.rerun()

            # Collect edits from the form (built below) before the submit handler runs.
            # These are only used if edit mode is active when ✅ Submit is clicked.
            _edit_mode_active = st.session_state.get(_edit_mode_key, False)
            _edited_extract: dict | None = None

            if _edit_mode_active:
                # ── Inline edit form ──────────────────────────────────────
                _edited_extract = copy.deepcopy(last["extract"])

                _edited_extract["meeting_title"] = st.text_input(
                    "Summary Title",
                    value=_edited_extract.get("meeting_title", ""),
                    key=f"edit_title_{pillar.slug}",
                )

                def _edit_list(label: str, field: str, current: list[str]) -> list[str]:
                    raw_text = st.text_area(
                        label,
                        value="\n".join(current),
                        height=100,
                        key=f"edit_{field}_{pillar.slug}",
                        help="One item per line.",
                    )
                    return [line.strip() for line in raw_text.splitlines() if line.strip()]

                _edited_extract["at_a_glance"] = _edit_list(
                    "At a glance (one bullet per line)", "at_a_glance", _edited_extract.get("at_a_glance", [])
                )
                # Decisions — pipe-delimited: "Decision | Context (optional)"
                def _split_pipe(line: str, n: int) -> list[str]:
                    parts = [p.strip() for p in line.split("|")]
                    while len(parts) < n:
                        parts.append("")
                    return parts[:n]

                _decisions_lines = [
                    " | ".join(filter(None, [d.get("decision", ""), d.get("context", "")]))
                    if isinstance(d, dict) else str(d)
                    for d in _edited_extract.get("decisions", [])
                ]
                _decisions_text = st.text_area(
                    "Decisions",
                    value="\n".join(_decisions_lines),
                    height=100,
                    key=f"edit_decisions_{pillar.slug}",
                    help="One item per line. Format: Decision | Context (optional)",
                )
                _edited_extract["decisions"] = [
                    {"decision": _split_pipe(l, 2)[0], "context": _split_pipe(l, 2)[1]}
                    for l in _decisions_text.splitlines()
                    if l.strip()
                ]

                # Wins / Closures — pipe-delimited: "Item | Owner | Summary (optional)"
                _closures_lines = [
                    " | ".join(filter(None, [c.get("item", ""), c.get("owner", ""), c.get("summary", "")]))
                    if isinstance(c, dict) else str(c)
                    for c in _edited_extract.get("closures", [])
                ]
                _closures_text = st.text_area(
                    "Wins / Closures",
                    value="\n".join(_closures_lines),
                    height=100,
                    key=f"edit_closures_{pillar.slug}",
                    help="One item per line. Format: Item | Owner | Summary (optional)",
                )
                _edited_extract["closures"] = [
                    {"item": _split_pipe(l, 3)[0], "owner": _split_pipe(l, 3)[1], "summary": _split_pipe(l, 3)[2]}
                    for l in _closures_text.splitlines()
                    if l.strip()
                ]

                # New Tracks — pipe-delimited: "Item | Owner | Summary (optional)"
                _tracks_lines = [
                    " | ".join(filter(None, [t.get("item", ""), t.get("owner", ""), t.get("summary", "")]))
                    if isinstance(t, dict) else str(t)
                    for t in _edited_extract.get("new_tracks", [])
                ]
                _tracks_text = st.text_area(
                    "New Tracks",
                    value="\n".join(_tracks_lines),
                    height=100,
                    key=f"edit_new_tracks_{pillar.slug}",
                    help="One item per line. Format: Item | Owner | Summary (optional)",
                )
                _edited_extract["new_tracks"] = [
                    {"item": _split_pipe(l, 3)[0], "owner": _split_pipe(l, 3)[1], "summary": _split_pipe(l, 3)[2]}
                    for l in _tracks_text.splitlines()
                    if l.strip()
                ]

                # Risks & Open Items — pipe-delimited: "Risk | Impact (optional)"
                # watch_list items are now dicts {risk, impact}; gaps remain plain strings
                _risks_lines = [
                    " | ".join(filter(None, [r.get("risk", ""), r.get("impact", "")]))
                    if isinstance(r, dict) else str(r)
                    for r in _edited_extract.get("watch_list", [])
                ] + [str(g) for g in _edited_extract.get("gaps", [])]
                _risks_text = st.text_area(
                    "Risks & Open Items",
                    value="\n".join(_risks_lines),
                    height=120,
                    key=f"edit_risks_{pillar.slug}",
                    help="One item per line. Format: Risk | Impact (optional)",
                )
                _edited_extract["watch_list"] = [
                    {"risk": _split_pipe(l, 2)[0], "impact": _split_pipe(l, 2)[1]}
                    for l in _risks_text.splitlines()
                    if l.strip()
                ]
                _edited_extract["gaps"] = []
                _ns_raw = _edited_extract.get("next_steps", [])
                _ns_lines = []
                for _ns in _ns_raw:
                    if isinstance(_ns, dict):
                        _owner = _ns.get("owner", "")
                        _action = _ns.get("action", "")
                        _due = _ns.get("due", "")
                        _ns_lines.append(f"{_owner} — {_action} (due: {_due})" if _owner else f"{_action} (due: {_due})" if _due else _action)
                    else:
                        _ns_lines.append(str(_ns))
                _ns_edited_text = st.text_area(
                    "Next steps",
                    value="\n".join(_ns_lines),
                    height=120,
                    key=f"edit_next_steps_{pillar.slug}",
                    help="One item per line. Format: Owner — Action (due: date)",
                )
                _edited_extract["next_steps"] = [
                    {"action": line.strip(), "owner": "", "due": ""}
                    for line in _ns_edited_text.splitlines()
                    if line.strip()
                ]

            with col_submit_btn:
                already_submitted = last.get("submitted", False)
                if st.button(
                    "Submitted" if already_submitted else "Submit",
                    type="primary",
                    disabled=already_submitted,
                    key=f"submit_history_{pillar.slug}",
                ):
                    try:
                        _final_extract = last["extract"]
                        _final_markdown = last["markdown"]
                        _final_html = last["html"]
                        _orig_title = (last["extract"] or {}).get("meeting_title", "")
                        _edited_title = (
                            (_edited_extract or {}).get("meeting_title", "")
                            if _edited_extract is not None
                            else None
                        )
                        print(
                            f"[rally.submit] pillar={pillar.slug} "
                            f"edit_mode_active={_edit_mode_active} "
                            f"edited_is_none={_edited_extract is None} "
                            f"orig_title={_orig_title!r} edited_title={_edited_title!r}",
                            flush=True,
                        )
                        if _edit_mode_active and _edited_extract is not None:
                            with st.spinner("Applying edits…"):
                                rerendered = summarizer.rerender_from_edited(_edited_extract)
                            _final_extract = _edited_extract
                            _final_markdown = rerendered.markdown
                            _final_html = rerendered.html
                        record = storage.save_summary(
                            meeting_title=(_final_extract.get("meeting_title") or "").strip() or f"{pillar.name} Weekly Update",
                            meeting_date=(_final_extract.get("meeting_date") or "").strip(),
                            markdown=_final_markdown,
                            html=_final_html,
                            extract=_final_extract,
                            upload_path=last.get("upload_path"),
                            source_filename=last.get("source_name", "notes.txt"),
                            pillar=pillar.slug,
                        )
                        _cached_latest_per_pillar.clear()
                        _cached_pillar_recent.clear()
                        _cached_pillar_history.clear()
                        st.session_state[_result_key(pillar.slug)]["markdown"] = _final_markdown
                        st.session_state[_result_key(pillar.slug)]["html"] = _final_html
                        st.session_state[_result_key(pillar.slug)]["extract"] = copy.deepcopy(_final_extract)
                        st.session_state[_result_key(pillar.slug)]["submitted"] = True
                        st.session_state[_edit_mode_key] = False
                        _clear_edit_form_state(pillar.slug)
                        print(
                            f"[rally.submit] pillar={pillar.slug} saved={record.id} "
                            f"final_title={_final_extract.get('meeting_title', '')!r}",
                            flush=True,
                        )
                        st.session_state[f"submit_flash_{pillar.slug}"] = True
                        st.rerun()
                    except Exception as exc:
                        st.error(f"**Error saving:** {exc}")

            if st.session_state.pop(f"submit_flash_{pillar.slug}", False):
                st.success("Submitted to History.")

            st.subheader("Preview")
            preview_tab_md, preview_tab_html = st.tabs(["Markdown", "HTML"])
            with preview_tab_md:
                st.markdown(last["markdown"])
                _dl_stem = last["extract"].get("meeting_title", "one-pager").replace(" ", "_")[:60]
                st.download_button(
                    "Download Markdown",
                    last["markdown"],
                    file_name=f"{_dl_stem}.md",
                    mime="text/markdown",
                    key=f"dl_md_{pillar.slug}",
                )
            with preview_tab_html:
                st.components.v1.html(last["html"], height=620, scrolling=True)
                st.caption(
                    "Click **Save as PDF** inside the preview above, or download the HTML "
                    "and open it in your browser to print to PDF."
                )
                st.download_button(
                    "Download HTML",
                    last["html"],
                    file_name=f"{_dl_stem}.html",
                    mime="text/html",
                    key=f"dl_html_{pillar.slug}",
                )

        # ── This week's saved notes ───────────────────────────────────────
        st.divider()
        st.markdown("**This week's saved notes**")
        if not _week_notes:
            st.caption("No notes saved this week yet. Use the Additional context box above and click 💾 Save Note.")
        else:
            for _note in _week_notes:
                if _render_note_card(_note, pillar_slug=pillar.slug, location="gen"):
                    st.rerun()


    # ── History ──────────────────────────────────────────────────────────
    with sub_hist:
        _all_notes = _cached_pillar_all_notes(pillar.slug)
        if _all_notes:
            st.markdown("**Saved notes**")
            _by_week: dict[str, list[NoteEntry]] = {}
            for _n in _all_notes:
                _by_week.setdefault(_n.week_key or "no-week", []).append(_n)
            for _wk in sorted(_by_week.keys(), reverse=True):
                _wk_label = f"Week of {_wk}" if _wk != "no-week" else "Undated"
                with st.expander(f"{_wk_label}  ·  {len(_by_week[_wk])} notes", expanded=False):
                    for _n in _by_week[_wk]:
                        if _render_note_card(_n, pillar_slug=pillar.slug, location=f"hist_{_wk}", use_expander=False):
                            st.rerun()
            st.divider()

        records = _cached_pillar_history(pillar.slug, 30)
        if not records:
            st.info(f"No archived summaries yet for {pillar.name}. Generate one from the Generate tab.")
        else:
            def _hist_date_label(r: SummaryRecord) -> str:
                eff = MeetingStorage._record_date(r)
                if eff:
                    return eff.strftime("%b %d, %Y")
                return r.meeting_date or "no date"

            _fallback_title = f"{pillar.name} Weekly Update"
            labels = [
                f"{r.meeting_title or _fallback_title} — {_hist_date_label(r)}"
                for r in records
            ]
            selected_label = st.radio(
                "Jump to submission",
                labels,
                key=f"hist_sel_{pillar.slug}",
                label_visibility="visible",
            )
            selected_idx = labels.index(selected_label) if selected_label else 0

            for i, record in enumerate(records):
                with st.expander(
                    f"{record.meeting_title or _fallback_title} · {_hist_date_label(record)}",
                    expanded=(i == selected_idx),
                ):
                    _, col_del = st.columns([8, 1])
                    with col_del:
                        with st.popover("🗑 Delete", use_container_width=True):
                            st.warning("This will permanently delete this submission. This cannot be undone.")
                            if st.button(
                                "Yes, delete",
                                key=f"del_yes_{pillar.slug}_{record.id}",
                                type="primary",
                            ):
                                storage.delete_summary(record)
                                _cached_latest_per_pillar.clear()
                                _cached_pillar_recent.clear()
                                _cached_pillar_history.clear()
                                st.success("Submission deleted.")
                                st.rerun()

                    try:
                        md = storage.read_file(record.markdown_path)
                        html = storage.read_file(record.html_path)
                        hist_md_tab, hist_html_tab = st.tabs(["Markdown", "HTML Preview"])
                        with hist_md_tab:
                            st.markdown(md)
                            st.download_button(
                                "Download MD",
                                md,
                                file_name=f"{record.id}.md",
                                key=f"hist_md_{pillar.slug}_{record.id}",
                            )
                        with hist_html_tab:
                            st.components.v1.html(html, height=600, scrolling=True)
                            st.caption(
                                "Click **Save as PDF** inside the preview, or download the HTML and open in browser."
                            )
                            st.download_button(
                                "Download HTML",
                                html,
                                file_name=f"{record.id}.html",
                                key=f"hist_html_{pillar.slug}_{record.id}",
                            )
                    except Exception as exc:
                        st.warning(f"Could not load: {exc}")

    _render_prompts_button(pillar.slug)


# ─────────────────────────────────────────────────────────────────────────────
# Leadership Rollup tab
# ─────────────────────────────────────────────────────────────────────────────
def render_rollup() -> None:
    st.subheader("Leadership Rollup")
    st.caption(
        "Select a date range and synthesize the most recent in-range summary from each "
        "pillar into a single cross-team executive one-pager for leadership."
    )

    sub_gen, sub_hist = st.tabs(["Generate", "History"])

    # ── Generate sub-tab ─────────────────────────────────────────────────────
    with sub_gen:
        today = date.today()
        _week_monday = today - timedelta(days=today.weekday())
        _week_friday = _week_monday + timedelta(days=4)
        col_from, col_to = st.columns(2)
        with col_from:
            range_start = st.date_input("From", value=_week_monday, key="rollup_start")
        with col_to:
            range_end = st.date_input("To", value=_week_friday, key="rollup_end")

        if range_start > range_end:
            st.error("'From' date must be on or before the 'To' date.")
        else:
            in_range = storage.latest_in_range_per_pillar(range_start, range_end)
            has_any = any(r is not None for r in in_range.values())

            if not has_any:
                st.info(
                    f"No pillar summaries found between {range_start.strftime('%b %d, %Y')} "
                    f"and {range_end.strftime('%b %d, %Y')}. Generate summaries in the pillar tabs first."
                )
            else:
                covered = sum(1 for r in in_range.values() if r is not None)
                total = len(PILLARS)
                missing_names_pre = [get_pillar(s).name for s, r in in_range.items() if r is None]

                col_ok, col_miss = st.columns(2)
                with col_ok:
                    st.metric("Pillars ready", f"{covered} / {total}")
                with col_miss:
                    st.metric("Missing", len(missing_names_pre))

                submitted_pillars = [p for p in PILLARS if in_range.get(p.slug)]
                missing_pillars = [p for p in PILLARS if not in_range.get(p.slug)]
                selections: dict[str, bool] = {}

                st.markdown("**Pillars with submissions — select to include:**")
                for p in submitted_pillars:
                    record = in_range[p.slug]
                    eff = MeetingStorage._record_date(record)
                    date_label = eff.strftime("%b %d, %Y") if eff else record.meeting_date or "no date"
                    label = f"**{p.name}** — {record.meeting_title} ({date_label})"
                    selections[p.slug] = st.checkbox(label, value=True, key=f"rollup_sel_{p.slug}")

                if missing_pillars:
                    st.markdown("---")
                    st.caption("No submission in selected range:")
                    for p in missing_pillars:
                        st.markdown(
                            f"<span style='color: gray;'>— {p.name}</span>",
                            unsafe_allow_html=True,
                        )
                    selections.update({p.slug: False for p in missing_pillars})

                selected_slugs = [slug for slug, picked in selections.items() if picked]
                missing_names = [get_pillar(s).name for s, r in in_range.items() if r is None]

                col_team_btn, col_org_btn = st.columns(2)
                with col_team_btn:
                    gen_team = st.button(
                        "Generate Team Level Rollup",
                        type="primary",
                        disabled=not selected_slugs,
                        key="rollup_gen_team",
                    )
                with col_org_btn:
                    gen_org = st.button(
                        "Generate Org Level Rollup",
                        type="primary",
                        disabled=not selected_slugs,
                        key="rollup_gen_org",
                    )

                def _build_pillar_summaries() -> list[dict]:
                    summaries = []
                    for slug in selected_slugs:
                        record = in_range[slug]
                        if record is None:
                            continue
                        md = storage.read_file(record.markdown_path)
                        summaries.append({"pillar": get_pillar(slug).name, "markdown": md})
                    return summaries

                if gen_team:
                    with st.spinner("Synthesizing team level rollup…"):
                        try:
                            rollup_extract = summarizer.generate_rollup(_build_pillar_summaries())
                            rollup_md, rollup_html = summarizer.render_rollup(rollup_extract)
                            st.session_state["rollup_result"] = {
                                "markdown": rollup_md,
                                "html": rollup_html,
                                "extract": rollup_extract,
                                "missing": missing_names,
                                "range_start": range_start,
                                "range_end": range_end,
                                "selected_slugs": selected_slugs,
                                "kind": "team",
                            }
                            st.success("Team Level Rollup generated.")
                        except Exception as exc:
                            st.error(f"**Error:** {exc}")

                if gen_org:
                    with st.spinner("Synthesizing org level rollup…"):
                        try:
                            org_extract = summarizer.generate_org_rollup(_build_pillar_summaries())
                            org_md, org_html = summarizer.render_org_rollup(org_extract)
                            st.session_state["org_rollup_result"] = {
                                "markdown": org_md,
                                "html": org_html,
                                "extract": org_extract,
                                "missing": missing_names,
                                "range_start": range_start,
                                "range_end": range_end,
                                "selected_slugs": selected_slugs,
                                "kind": "org",
                            }
                            st.success("Org Level Rollup generated.")
                        except Exception as exc:
                            st.error(f"**Error:** {exc}")

                def _render_rollup_preview(
                    rollup: dict,
                    label: str,
                    md_file: str,
                    html_file: str,
                    save_key: str,
                    kind: str,
                ) -> None:
                    st.divider()
                    _missing = rollup.get("missing", [])
                    _rs = rollup.get("range_start")
                    _re = rollup.get("range_end")
                    if _rs and _re:
                        _range_label = f"{_rs.strftime('%b %d, %Y')} – {_re.strftime('%b %d, %Y')}"
                        if _missing:
                            st.warning(
                                f"**Coverage note:** {len(_missing)} pillar(s) had no submission in "
                                f"{_range_label}: {', '.join(_missing)}"
                            )
                        else:
                            st.success(f"Full coverage — all 7 pillars submitted in {_range_label}.")

                    st.subheader(label)
                    roll_md_tab, roll_html_tab = st.tabs(["Markdown", "HTML"])
                    with roll_md_tab:
                        st.markdown(rollup["markdown"])
                        st.download_button(
                            "Download Markdown",
                            rollup["markdown"],
                            file_name=md_file,
                            mime="text/markdown",
                            key=f"dl_md_{save_key}",
                        )
                    with roll_html_tab:
                        st.components.v1.html(rollup["html"], height=680, scrolling=True)
                        st.caption(
                            "Click **Save as PDF** inside the preview, or download and open in browser."
                        )
                        st.download_button(
                            "Download HTML",
                            rollup["html"],
                            file_name=html_file,
                            mime="text/html",
                            key=f"dl_html_{save_key}",
                        )

                    st.divider()

                    # ── Edit / Save action row ─────────────────────────────────
                    _edit_mode_key = f"rollup_edit_mode_{save_key}"
                    _cols = st.columns([1.4, 1.4, 5])
                    with _cols[0]:
                        if st.button(
                            "✏️ Edit" if not st.session_state.get(_edit_mode_key) else "Cancel",
                            key=f"toggle_rollup_edit_{save_key}",
                        ):
                            _was_active = st.session_state.get(_edit_mode_key, False)
                            st.session_state[_edit_mode_key] = not _was_active
                            if _was_active:
                                _clear_rollup_edit_state(save_key, kind)
                            st.rerun()

                    _edit_mode_active = st.session_state.get(_edit_mode_key, False)
                    _edited_extract: dict | None = None

                    # ── Inline edit form ───────────────────────────────────────
                    if _edit_mode_active:
                        _edited_extract = copy.deepcopy(rollup["extract"])

                        def _edit_list_rollup(lbl: str, field: str, current: list[str]) -> list[str]:
                            raw = st.text_area(
                                lbl,
                                value="\n".join(current),
                                height=100,
                                key=f"{field}_{save_key}",
                                help="One item per line.",
                            )
                            return [ln.strip() for ln in raw.splitlines() if ln.strip()]

                        def _split_pipe3(line: str) -> list[str]:
                            parts = [p.strip() for p in line.split("|")]
                            while len(parts) < 3:
                                parts.append("")
                            return parts[:3]

                        if kind == "team":
                            _edited_extract["at_a_glance"] = _edit_list_rollup(
                                "At a glance", "rollup_at_a_glance",
                                _edited_extract.get("at_a_glance", []),
                            )
                            _edited_extract["cross_pillar_dependencies"] = _edit_list_rollup(
                                "Cross-pillar dependencies", "rollup_cross_dep",
                                _edited_extract.get("cross_pillar_dependencies", []),
                            )
                            _edited_extract["shared_blockers"] = _edit_list_rollup(
                                "Shared blockers", "rollup_blockers",
                                _edited_extract.get("shared_blockers", []),
                            )
                            _edited_extract["decisions_needed"] = _edit_list_rollup(
                                "Decisions needed from leadership", "rollup_decisions",
                                _edited_extract.get("decisions_needed", []),
                            )
                            _edited_extract["capacity_risks"] = _edit_list_rollup(
                                "Capacity risks", "rollup_capacity_risks",
                                _edited_extract.get("capacity_risks", []),
                            )

                            st.markdown("**Pillar highlights**")
                            _new_highlights: list[dict] = []
                            for _idx, _ph in enumerate(_edited_extract.get("pillar_highlights", [])):
                                _pname = _ph.get("pillar", f"Pillar {_idx + 1}")
                                _items = _ph.get("highlights", []) or []
                                _edited_hl = st.text_area(
                                    _pname,
                                    value="\n".join(_items),
                                    height=90,
                                    key=f"rollup_highlights_{save_key}_{_idx}",
                                    help="One highlight per line.",
                                )
                                _new_highlights.append({
                                    "pillar": _pname,
                                    "highlights": [ln.strip() for ln in _edited_hl.splitlines() if ln.strip()],
                                })
                            _edited_extract["pillar_highlights"] = _new_highlights

                            _ns_lines = []
                            for _s in _edited_extract.get("next_steps", []):
                                if isinstance(_s, dict):
                                    _parts = [_s.get("owner", ""), _s.get("action", ""), _s.get("due", "")]
                                    _ns_lines.append(" | ".join(p for p in _parts if p))
                                else:
                                    _ns_lines.append(str(_s))
                            _ns_raw = st.text_area(
                                "Next steps",
                                value="\n".join(_ns_lines),
                                height=120,
                                key=f"rollup_next_steps_{save_key}",
                                help="One per line. Format: Owner | Action | Due (any field optional)",
                            )
                            _edited_extract["next_steps"] = [
                                {
                                    "owner": _split_pipe3(ln)[0],
                                    "action": _split_pipe3(ln)[1],
                                    "due": _split_pipe3(ln)[2],
                                }
                                for ln in _ns_raw.splitlines()
                                if ln.strip()
                            ]

                        else:  # org
                            if _edited_extract.get("format") == "text":
                                _edited_body = st.text_area(
                                    "Org Rollup (free-form)",
                                    value=_edited_extract.get("body_text", ""),
                                    height=400,
                                    key=f"org_body_text_{save_key}",
                                    help="Edit the rollup body. Paste directly into Slack when done.",
                                )
                                _edited_extract["body_text"] = _edited_body
                            else:
                                _edited_extract["headline"] = st.text_input(
                                    "Headline",
                                    value=_edited_extract.get("headline", ""),
                                    key=f"org_headline_{save_key}",
                                )
                                _edited_extract["top_priorities"] = _edit_list_rollup(
                                    "Top priorities", "org_top_priorities",
                                    _edited_extract.get("top_priorities", []),
                                )
                                _edited_extract["critical_issues"] = _edit_list_rollup(
                                    "Critical issues", "org_critical_issues",
                                    _edited_extract.get("critical_issues", []),
                                )
                                _edited_extract["decisions_needed"] = _edit_list_rollup(
                                    "Decisions needed", "org_decisions",
                                    _edited_extract.get("decisions_needed", []),
                                )

                    # ── Save to History ────────────────────────────────────────
                    with _cols[1]:
                        _save_clicked = st.button(
                            "Save to History",
                            type="primary",
                            key=f"save_{save_key}",
                        )

                    if _save_clicked:
                        try:
                            _final_extract = rollup["extract"]
                            _final_md = rollup["markdown"]
                            _final_html = rollup["html"]
                            if _edit_mode_active and _edited_extract is not None:
                                with st.spinner("Applying edits…"):
                                    if kind == "team":
                                        _final_md, _final_html = summarizer.render_rollup(_edited_extract)
                                    else:
                                        _final_md, _final_html = summarizer.render_org_rollup(_edited_extract)
                                _final_extract = _edited_extract
                            _rs2 = rollup["range_start"]
                            _re2 = rollup["range_end"]
                            _kind_label = "Team" if kind == "team" else "Org"
                            _title = f"{_kind_label} Rollup {_rs2.strftime('%b %d')} – {_re2.strftime('%b %d, %Y')}"
                            rec = storage.save_rollup(
                                title=_title,
                                range_start=_rs2,
                                range_end=_re2,
                                markdown=_final_md,
                                html=_final_html,
                                extract=_final_extract,
                                pillars_included=[
                                    get_pillar(s).name for s in rollup.get("selected_slugs", [])
                                ],
                                pillars_missing=rollup.get("missing", []),
                                kind=kind,
                            )
                            _result_state_key = "rollup_result" if kind == "team" else "org_rollup_result"
                            st.session_state[_result_state_key]["markdown"] = _final_md
                            st.session_state[_result_state_key]["html"] = _final_html
                            st.session_state[_result_state_key]["extract"] = copy.deepcopy(_final_extract)
                            st.session_state[_edit_mode_key] = False
                            _clear_rollup_edit_state(save_key, kind)
                            st.success(f"Saved to History as `{rec.id}`.")
                        except Exception as exc:
                            st.error(f"Save failed: {exc}")

                rollup = st.session_state.get("rollup_result")
                if rollup:
                    _render_rollup_preview(
                        rollup,
                        label="Team Level Rollup Preview",
                        md_file="team_rollup.md",
                        html_file="team_rollup.html",
                        save_key="team",
                        kind="team",
                    )

                org_rollup = st.session_state.get("org_rollup_result")
                if org_rollup:
                    _render_rollup_preview(
                        org_rollup,
                        label="Org Level Rollup Preview",
                        md_file="org_rollup.md",
                        html_file="org_rollup.html",
                        save_key="org",
                        kind="org",
                    )

    # ── History sub-tab ───────────────────────────────────────────────────────
    with sub_hist:
        def _render_history_list(kind: str, key_prefix: str) -> None:
            rollup_records = storage.list_rollups(limit=30, kind=kind)
            if not rollup_records:
                st.info(
                    f"No saved {kind}-level rollups yet. "
                    "Generate one in the Generate tab and click 'Save to History'."
                )
                return
            labels = [
                f"{r.title} ({r.range_start[:10]} – {r.range_end[:10]})"
                for r in rollup_records
            ]
            selected_label = st.radio(
                "Jump to rollup",
                labels,
                key=f"rollup_hist_sel_{key_prefix}",
                label_visibility="visible",
            )
            selected_idx = labels.index(selected_label) if selected_label else 0

            for i, record in enumerate(rollup_records):
                label = labels[i]
                pillars_line = (
                    f"**Included:** {', '.join(record.pillars_included)}"
                    if record.pillars_included else ""
                )
                missing_line = (
                    f"  ·  **Missing:** {', '.join(record.pillars_missing)}"
                    if record.pillars_missing else ""
                )
                with st.expander(label, expanded=(i == selected_idx)):
                    if pillars_line or missing_line:
                        st.caption(pillars_line + missing_line)
                    try:
                        rmd = storage.read_file(record.markdown_path)
                        rhtml = storage.read_file(record.html_path)
                        hist_md_tab, hist_html_tab = st.tabs(["Markdown", "HTML Preview"])
                        with hist_md_tab:
                            st.markdown(rmd)
                            st.download_button(
                                "Download MD",
                                rmd,
                                file_name=f"{record.id}.md",
                                key=f"rollup_hist_md_{key_prefix}_{record.id}",
                            )
                        with hist_html_tab:
                            st.components.v1.html(rhtml, height=600, scrolling=True)
                            st.caption(
                                "Click **Save as PDF** inside the preview, or download the HTML and open in browser."
                            )
                            st.download_button(
                                "Download HTML",
                                rhtml,
                                file_name=f"{record.id}.html",
                                key=f"rollup_hist_html_{key_prefix}_{record.id}",
                            )
                    except Exception as exc:
                        st.warning(f"Could not load: {exc}")

                    st.divider()
                    confirm_key = f"rollup_del_confirm_{key_prefix}_{record.id}"
                    if st.session_state.get(confirm_key):
                        st.warning("Are you sure? This cannot be undone.")
                        col_yes, col_no = st.columns([1, 4])
                        with col_yes:
                            if st.button(
                                "Yes, delete",
                                key=f"rollup_del_yes_{key_prefix}_{record.id}",
                                type="primary",
                            ):
                                storage.delete_rollup(record)
                                st.session_state.pop(confirm_key, None)
                                st.success("Rollup deleted.")
                                st.rerun()
                        with col_no:
                            if st.button("Cancel", key=f"rollup_del_no_{key_prefix}_{record.id}"):
                                st.session_state.pop(confirm_key, None)
                                st.rerun()
                    else:
                        if st.button("Delete this rollup", key=f"rollup_del_{key_prefix}_{record.id}"):
                            st.session_state[confirm_key] = True
                            st.rerun()

        hist_team_tab, hist_org_tab = st.tabs(["Team Level", "Org Level"])
        with hist_team_tab:
            _render_history_list(kind="team", key_prefix="team")
        with hist_org_tab:
            _render_history_list(kind="org", key_prefix="org")

    _render_prompts_button("rollup")


# ─────────────────────────────────────────────────────────────────────────────
# Ask Rally tab
# ─────────────────────────────────────────────────────────────────────────────
def render_chat() -> None:
    st.subheader("Ask about your teams")
    st.caption(
        "Ask questions about current and past submissions from all 7 pillars. "
        "Answers are grounded only in archived one-pagers."
    )

    if "chat_context" not in st.session_state:
        with st.spinner("Loading pillar history…"):
            context, ctx_count = storage.load_all_history_context()
            st.session_state["chat_context"] = context
            st.session_state["chat_context_count"] = ctx_count

    ctx_count = st.session_state.get("chat_context_count", 0)
    if ctx_count:
        st.caption(f"Context loaded: {ctx_count} content block{'s' if ctx_count != 1 else ''} (summaries + saved notes) across all pillars.")
    else:
        st.caption("No archived summaries found. Generate summaries in the pillar tabs first.")

    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []

    _chat_box = st.container(height=500, border=True)

    with _chat_box:
        for msg in st.session_state["chat_history"]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    if ctx_count and (prompt := st.chat_input("Ask a question about your teams' work…")):
        st.session_state["chat_history"].append({"role": "user", "content": prompt})
        with _chat_box:
            with st.chat_message("user"):
                st.markdown(prompt)

        system_prompt = (
            LEADERSHIP_CHAT_PROMPT
            + "\n\n---\n\nSUMMARIES:\n\n"
            + st.session_state["chat_context"]
        )
        with _chat_box:
            with st.chat_message("assistant"):
                with st.spinner("Thinking…"):
                    try:
                        reply = summarizer.chat_with_history(
                            system=system_prompt,
                            messages=st.session_state["chat_history"],
                        )
                        st.markdown(reply)
                        st.session_state["chat_history"].append(
                            {"role": "assistant", "content": reply}
                        )
                    except Exception as exc:
                        st.error(f"**Error:** {exc}")
    elif not ctx_count:
        st.info("Generate at least one pillar summary to enable the chat assistant.")

    _render_prompts_button("chat")


# ─────────────────────────────────────────────────────────────────────────────
# Top-level tabs: 7 pillars + Leadership Rollup
# ─────────────────────────────────────────────────────────────────────────────
tab_labels = [p.short for p in PILLARS] + ["Leadership Rollup", "Ask Rally"]
tabs = st.tabs(tab_labels)

for tab, pillar in zip(tabs[: len(PILLARS)], PILLARS):
    with tab:
        render_pillar(pillar)

with tabs[-2]:
    render_rollup()

with tabs[-1]:
    render_chat()
