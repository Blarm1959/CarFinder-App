"""Shared modern presentation layer for CarFinder v3.

This intentionally changes presentation/navigation only. Search logic, database
logic and manufacturer adapters remain separate.
"""
from __future__ import annotations

import html
from typing import Any

_installed = False
_css_applied = False
_original_set_page_config = None
_original_markdown = None
_original_caption = None
_original_info = None


MODERN_CSS = r"""
<style>
:root {
  --cf-bg: #eef3f8;
  --cf-surface: #ffffff;
  --cf-surface-soft: #f6f8fb;
  --cf-text: #172331;
  --cf-muted: #667586;
  --cf-border: #d7e0e8;
  --cf-primary: #173b67;
  --cf-primary-dark: #12375f;
  --cf-primary-soft: #edf5ff;
  --cf-radius: 16px;
  --cf-shadow: 0 8px 30px rgba(24, 38, 70, .06);
}

/* Overall canvas */
.stApp {
  background:
    radial-gradient(circle at 10% 0%, rgba(61,111,163,.10), transparent 32rem),
    radial-gradient(circle at 100% 15%, rgba(197,93,75,.06), transparent 28rem),
    var(--cf-bg);
  color: var(--cf-text);
}
[data-testid="stAppViewContainer"] > .main {
  background: var(--cf-bg);
}
.block-container {
  max-width: 1440px;
  padding-top: 2.2rem;
  padding-bottom: 4rem;
}

/* Typography */
h1, h2, h3, h4 {
  color: var(--cf-text);
  letter-spacing: -0.025em;
}
h1 {
  font-size: clamp(2.25rem, 3.2vw, 3.4rem) !important;
  font-weight: 760 !important;
  line-height: 1.05 !important;
  margin-bottom: .35rem !important;
}
h2 {
  font-size: 1.65rem !important;
  font-weight: 720 !important;
}
h3 {
  font-size: 1.18rem !important;
  font-weight: 690 !important;
}
[data-testid="stCaptionContainer"] {
  color: var(--cf-muted);
  font-size: .94rem;
}

/* Primary and secondary buttons */
.stButton > button,
[data-testid="stBaseButton-primary"],
[data-testid="stBaseButton-secondary"] {
  border-radius: 12px !important;
  min-height: 2.8rem;
  padding: .55rem 1rem !important;
  font-weight: 650 !important;
  border: 1px solid var(--cf-border) !important;
  box-shadow: none !important;
  transition: transform .12s ease, box-shadow .12s ease, border-color .12s ease;
}
.stButton > button:hover {
  transform: translateY(-1px);
  box-shadow: 0 5px 14px rgba(25, 39, 72, .08) !important;
}
button[kind="primary"],
[data-testid="stBaseButton-primary"] {
  background: var(--cf-primary) !important;
  color: white !important;
  border-color: var(--cf-primary) !important;
}
button[kind="primary"]:hover,
[data-testid="stBaseButton-primary"]:hover {
  background: var(--cf-primary-dark) !important;
  border-color: var(--cf-primary-dark) !important;
}

/* Forms and controls */
[data-baseweb="input"] > div,
[data-baseweb="select"] > div,
[data-testid="stNumberInputContainer"] > div,
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea {
  border-radius: 11px !important;
}
[data-testid="stForm"] {
  background: var(--cf-surface);
  border: 1px solid var(--cf-border);
  border-radius: var(--cf-radius);
  padding: 1.25rem 1.35rem;
  box-shadow: var(--cf-shadow);
}

/* Tabs */
[data-baseweb="tab-list"] {
  gap: .4rem;
  background: transparent;
  border-bottom: 1px solid var(--cf-border);
}
button[data-baseweb="tab"] {
  padding-left: 1rem !important;
  padding-right: 1rem !important;
  font-weight: 650 !important;
}
button[data-baseweb="tab"][aria-selected="true"] {
  color: var(--cf-primary) !important;
}

/* Expanders become quiet cards */
[data-testid="stExpander"] {
  border: 1px solid var(--cf-border) !important;
  border-radius: 13px !important;
  background: var(--cf-surface);
  box-shadow: none;
}
[data-testid="stExpander"] summary {
  font-weight: 620;
}

/* Dataframes */
[data-testid="stDataFrame"] {
  border: 1px solid var(--cf-border);
  border-radius: 14px;
  overflow: hidden;
  background: var(--cf-surface);
  box-shadow: var(--cf-shadow);
}

/* Metrics */
[data-testid="stMetric"] {
  background: var(--cf-surface);
  border: 1px solid var(--cf-border);
  border-radius: 14px;
  padding: 1rem 1.05rem;
  box-shadow: var(--cf-shadow);
}
[data-testid="stMetricLabel"] {
  color: var(--cf-muted);
  font-size: .86rem;
}
[data-testid="stMetricValue"] {
  font-size: 1.65rem;
  font-weight: 720;
  color: var(--cf-text);
}

/* Alerts */
[data-testid="stAlert"] {
  border-radius: 13px;
  border: 1px solid var(--cf-border);
}

/* Dialogs */
div[role="dialog"] > div {
  border-radius: 18px !important;
}

/* Sidebar: quieter, less admin-tool-like */
[data-testid="stSidebar"] {
  background: #f0f2f6;
  border-right: 1px solid var(--cf-border);
}
[data-testid="stSidebar"] .stButton > button {
  background: rgba(255,255,255,.7);
}

/* Our shared hero/section cards */
.cf-hero {
  background: linear-gradient(135deg, #ffffff 0%, #f4f7ff 100%);
  border: 1px solid var(--cf-border);
  border-radius: 22px;
  padding: 2rem 2.1rem 1.8rem;
  margin: .7rem 0 1.4rem;
  box-shadow: var(--cf-shadow);
}
.cf-eyebrow {
  color: var(--cf-primary);
  font-weight: 700;
  font-size: .78rem;
  letter-spacing: .08em;
  text-transform: uppercase;
  margin-bottom: .65rem;
}
.cf-hero-title {
  color: var(--cf-text);
  font-size: clamp(1.8rem, 2.4vw, 2.6rem);
  line-height: 1.08;
  letter-spacing: -.03em;
  font-weight: 760;
  margin: 0 0 .55rem;
}
.cf-hero-copy {
  color: var(--cf-muted);
  font-size: 1.05rem;
  line-height: 1.55;
  max-width: 850px;
  margin: 0;
}
.cf-section-card {
  background: var(--cf-surface);
  border: 1px solid var(--cf-border);
  border-radius: 18px;
  padding: 1.35rem 1.45rem;
  margin: .6rem 0 1rem;
  box-shadow: var(--cf-shadow);
}
.cf-section-title {
  font-size: 1.12rem;
  font-weight: 720;
  margin: 0 0 .25rem;
}
.cf-section-copy {
  color: var(--cf-muted);
  margin: 0;
  line-height: 1.5;
}
.cf-kicker {
  color: var(--cf-muted);
  font-size: .92rem;
  margin-bottom: .35rem;
}
.cf-empty {
  background: var(--cf-surface);
  border: 1px solid var(--cf-border);
  border-radius: 20px;
  padding: 2rem;
  box-shadow: var(--cf-shadow);
  margin: 1rem 0;
}
.cf-empty h2 {
  margin-top: 0;
  margin-bottom: .35rem;
}
.cf-empty p {
  color: var(--cf-muted);
  font-size: 1rem;
  margin-bottom: .2rem;
}

/* Larger top-level journey buttons */
.cf-primary-actions .stButton > button,
div[data-testid="column"] .cf-primary-actions .stButton > button {
  min-height: 3.2rem !important;
  font-size: 1.02rem !important;
}

/* Hide Streamlit's default multipage page names visually when custom navigation is present. */
[data-testid="stSidebarNav"] {
  padding-top: .4rem;
}

/* Mobile */
@media (max-width: 900px) {
  .block-container {
    padding-left: 1rem;
    padding-right: 1rem;
    padding-top: 1rem;
  }
  .cf-hero {
    padding: 1.35rem;
    border-radius: 17px;
  }
}

/* v3.0.5 search-app layout */
.cf-hero-compact {
  padding: 1.55rem 1.7rem 1.45rem;
  margin-bottom: 1rem;
}
.cf-search-heading {
  display:flex;
  justify-content:space-between;
  align-items:flex-end;
  margin: 1.25rem 0 .8rem;
}
.cf-search-title {
  font-size: 1.55rem;
  font-weight: 760;
  letter-spacing: -.025em;
  color: var(--cf-text);
}
.cf-search-subtitle {
  color: var(--cf-muted);
  margin-top: .15rem;
}
.cf-inline-note {
  min-height: 2.75rem;
  display:flex;
  align-items:center;
  padding: .65rem .9rem;
  border-radius: 12px;
  background: var(--cf-surface-soft);
  color: var(--cf-muted);
  border:1px solid var(--cf-border);
}
.cf-form-divider {
  height:1px;
  background:var(--cf-border);
  margin:1rem 0 1.1rem;
}
.cf-results-heading {
  display:flex;
  align-items:center;
  justify-content:space-between;
  margin: 1.45rem 0 .65rem;
  font-size: 1.1rem;
  font-weight: 720;
}
.cf-results-heading strong {
  min-width:2rem;
  text-align:center;
  padding:.18rem .55rem;
  border-radius:999px;
  background:var(--cf-surface-soft);
  color:var(--cf-muted);
  font-size:.86rem;
}
.cf-empty-lite {
  border:1px dashed #ccd4e2;
  border-radius:16px;
  padding:1.25rem 1.35rem;
  color:var(--cf-muted);
  background:rgba(255,255,255,.55);
}
[data-testid="stVerticalBlockBorderWrapper"] {
  border-radius:18px !important;
  border-color:var(--cf-border) !important;
  background:var(--cf-surface);
  box-shadow:var(--cf-shadow);
}
[data-testid="stVerticalBlockBorderWrapper"] > div {
  padding: .25rem .35rem;
}
[data-testid="stToggle"] label {
  font-weight:650;
}
[data-testid="stMultiSelect"] [data-baseweb="tag"] {
  border-radius:999px !important;
}
div[data-testid="stSelectbox"] > div > div,
div[data-testid="stMultiSelect"] > div > div,
div[data-testid="stTextInput"] > div > div {
  min-height: 2.75rem;
}
[data-testid="stSidebarNav"] ul {
  gap:.25rem;
}
[data-testid="stSidebarNav"] a {
  border-radius:10px;
}
[data-testid="stSidebarNav"] a[aria-current="page"] {
  background:rgba(49,87,213,.09);
  font-weight:650;
}


/* v3.0.6 — Lipfty 14-inspired application language */
.block-container {
  max-width: 1500px;
  padding-top: 1.15rem;
}
.cf-hero {
  background: rgba(255,255,255,.94);
  border-color: rgba(212,222,232,.94);
  box-shadow: 0 12px 34px rgba(29,56,84,.08);
}
.cf-hero-title {
  color: #12375f;
}
.cf-eyebrow {
  color: #245b96;
}
.cf-page-heading {
  margin: 1rem 0 .75rem;
}
.cf-page-title {
  font-size: 1.65rem;
  font-weight: 800;
  letter-spacing: -.03em;
  color: #12375f;
}
.cf-page-subtitle {
  margin-top: .2rem;
  color: var(--cf-muted);
  font-size: .94rem;
}
.cf-panel-title {
  margin: 0 0 .85rem;
  color: #173d66;
  font-size: 1.05rem;
  font-weight: 800;
}
.cf-spacer {
  height: .55rem;
}
.cf-results-bar {
  display:flex;
  align-items:center;
  justify-content:space-between;
  margin: 1.25rem 0 .55rem;
}
.cf-results-bar > div {
  display:flex;
  align-items:center;
  gap:.65rem;
}
.cf-results-bar strong {
  font-size:1.08rem;
  color:#173d66;
}
.cf-results-bar span {
  display:inline-flex;
  align-items:center;
  justify-content:center;
  min-width:2.1rem;
  height:1.75rem;
  padding:0 .65rem;
  border-radius:999px;
  background:#edf5ff;
  color:#245b96;
  font-size:.82rem;
  font-weight:800;
}

/* Make Streamlit radio controls behave visually like Lipfty choice cards. */
div[role="radiogroup"] {
  display:flex !important;
  flex-wrap:wrap !important;
  gap:.5rem !important;
}
div[role="radiogroup"] > label {
  margin:0 !important;
  padding:.62rem .85rem !important;
  min-height:42px;
  border:2px solid #d7e0e6;
  border-radius:13px;
  background:#fff;
  cursor:pointer;
  transition: border-color .12s ease, background .12s ease, transform .12s ease;
}
div[role="radiogroup"] > label:hover {
  border-color:#9fb8d2;
  background:#f8fbff;
  transform:translateY(-1px);
}
div[role="radiogroup"] > label:has(input:checked) {
  border-color:#2165aa;
  background:#edf5ff;
  color:#173d66;
}
div[role="radiogroup"] [data-testid="stMarkdownContainer"] p {
  font-weight:750;
  font-size:.9rem;
}
div[role="radiogroup"] [data-baseweb="radio"] > div:first-child {
  display:none;
}

/* Lipfty-style field panels */
[data-testid="stVerticalBlockBorderWrapper"] {
  border:1px solid #cfdae2 !important;
  border-radius:16px !important;
  background:rgba(255,255,255,.95) !important;
  box-shadow:0 8px 24px rgba(29,56,84,.05) !important;
}
[data-testid="stVerticalBlockBorderWrapper"] > div {
  padding:.55rem .7rem .7rem !important;
}

/* Compact form controls */
[data-baseweb="input"] > div,
[data-baseweb="select"] > div,
[data-testid="stNumberInputContainer"] > div,
[data-testid="stTextInput"] input {
  background:#fff !important;
  border-color:#c9d6df !important;
}
[data-testid="stTextInput"] label p,
[data-testid="stSelectbox"] label p,
[data-testid="stMultiSelect"] label p {
  color:#314960;
  font-weight:750;
}

/* Strong Lipfty-style actions */
.stButton > button {
  min-height:40px !important;
  border-radius:10px !important;
  border-color:#b9c7d5 !important;
  color:#173b67 !important;
  font-weight:750 !important;
  background:#fff !important;
}
.stButton > button[kind="primary"],
button[kind="primary"] {
  background:#163f71 !important;
  color:#fff !important;
  border-color:#163f71 !important;
}
.stButton > button[kind="primary"]:hover,
button[kind="primary"]:hover {
  background:#12375f !important;
  border-color:#12375f !important;
}

/* Sidebar kept quiet, like a frame rather than the product itself. */
[data-testid="stSidebar"] {
  background:#eef2f6;
}
[data-testid="stSidebarNav"] a {
  min-height:38px;
  display:flex;
  align-items:center;
}
[data-testid="stSidebarNav"] a[aria-current="page"] {
  background:#dfe8f2 !important;
  color:#173b67 !important;
}

/* Results/data should feel like app content rather than a worksheet. */
[data-testid="stDataFrame"] {
  border:1px solid #cfdae2;
  border-radius:14px;
  box-shadow:0 8px 24px rgba(29,56,84,.05);
}

/* Modern target cards */
[data-testid="stToggle"] {
  padding-top:.25rem;
}
[data-testid="stToggle"] label {
  color:#173d66;
  font-weight:800;
}

</style>
"""


def _render_top_actions(st) -> None:
    if st.session_state.get("_cf_top_actions_rendered"):
        return
    st.session_state["_cf_top_actions_rendered"] = True

    st.markdown(
        """
        <div class="cf-kicker">Search, shortlist and compare used cars across supported manufacturers.</div>
        """,
        unsafe_allow_html=True,
    )
    c1, c2, c3 = st.columns([1.25, 1.05, 3.2])
    with c1:
        if st.button(
            "Start Discovery Search",
            type="primary",
            use_container_width=True,
            key="_cf_start_discovery",
        ):
            st.session_state["find_cars_section"] = "Discovery"
            st.switch_page("pages/1_Find_Cars.py")
    with c2:
        if st.button(
            "My Car List",
            use_container_width=True,
            key="_cf_open_my_cars",
        ):
            st.session_state["find_cars_section"] = "My Car List"
            st.switch_page("pages/1_Find_Cars.py")


def _empty_home(st) -> None:
    st.markdown(
        """
        <div class="cf-empty">
          <div class="cf-eyebrow">Getting started</div>
          <h2>Start with a broad search</h2>
          <p>Choose the manufacturers and the kind of car you want. From the results,
          add the car types worth following to My Car List.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    c1, c2, c3 = st.columns([1.35, 1.05, 3])
    with c1:
        if st.button(
            "Start Discovery Search",
            type="primary",
            use_container_width=True,
            key="_cf_empty_discovery",
        ):
            st.session_state["find_cars_section"] = "Discovery"
            st.switch_page("pages/1_Find_Cars.py")
    with c2:
        if st.button(
            "Open My Car List",
            use_container_width=True,
            key="_cf_empty_list",
        ):
            st.session_state["find_cars_section"] = "My Car List"
            st.switch_page("pages/1_Find_Cars.py")


def apply_css(st) -> None:
    # Streamlit rebuilds the page on every rerun, so the CSS must be emitted
    # every time set_page_config is called.
    _original_markdown(MODERN_CSS, unsafe_allow_html=True)


def install_global_ui() -> None:
    """Install once before each Streamlit page calls set_page_config()."""
    global _installed, _original_set_page_config, _original_markdown
    global _original_caption, _original_info

    if _installed:
        return

    import streamlit as st

    _installed = True
    _original_set_page_config = st.set_page_config
    _original_markdown = st.markdown
    _original_caption = st.caption
    _original_info = st.info

    def modern_set_page_config(*args: Any, **kwargs: Any):
        # Per-run UI marker: the main header appears once per normal render.
        st.session_state["_cf_top_actions_rendered"] = False
        result = _original_set_page_config(*args, **kwargs)
        apply_css(st)
        return result

    def modern_markdown(body: Any, *args: Any, **kwargs: Any):
        text = str(body) if body is not None else ""
        # Remove decorative car emoji from the product heading.
        if "<h1" in text and "🚗 CarFinder" in text:
            body = text.replace("🚗 CarFinder", "CarFinder")
        result = _original_markdown(body, *args, **kwargs)

        # On the logged-in main page, put the actual v3 journey directly below
        # the product heading instead of hiding it in the sidebar.
        if (
            "<h1" in str(body)
            and "CarFinder" in str(body)
            and st.session_state.get("user")
            and "Find Cars" not in str(body)
        ):
            _render_top_actions(st)
        return result

    def modern_caption(body: Any, *args: Any, **kwargs: Any):
        text = str(body)
        lower = text.lower()
        if "no car searches yet" in lower or "add up to 10" in lower:
            who = text.split("·", 1)[0].strip()
            body = f"{who} · start with Discovery, then build My Car List."
        elif text.startswith("Searchable makes:"):
            body = "32 manufacturers available for Discovery."
        return _original_caption(body, *args, **kwargs)

    def modern_info(body: Any, *args: Any, **kwargs: Any):
        text = str(body)
        if text.startswith("No cars yet."):
            _empty_home(st)
            return None
        return _original_info(body, *args, **kwargs)

    st.set_page_config = modern_set_page_config
    st.markdown = modern_markdown
    st.caption = modern_caption
    st.info = modern_info
