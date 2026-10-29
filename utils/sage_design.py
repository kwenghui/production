"""Eight contrast-tested themes for the responsive Sage statistical UI.

Presentation only: theme changes never affect official data or AI decisions.

Used by: app.py, ui/answer_view.py, ui/components.py, ui/question_form.py,
ui/sidebar.py, ui/startup.py, ui/table_explorer.py,
tools/characterise_table_explorer.py.
Tested in: tests/test_code_style.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from functools import lru_cache
import base64
import re
from string import Template
from textwrap import dedent

import streamlit as st
import json

THEME_ORDER = [
    "sage_mist",
    "forest",
    "midnight",
    "blue_sky",
    "aubergine",
    "lilac",
    "copper",
    "berry",
]

# Each palette explicitly provides foreground, surface and graph colours.
THEMES = {
    "sage_mist": {
        'label': 'Sage Mist · Gentle green',
        'sidebar_caption': 'A calm, accessible Sage experience with clear charts and generous type.',
        'eyebrow': 'Discover Singapore through official statistics',
        'hero_note': 'A little curiosity can lead to a great discovery.',
        'side_eyebrow': 'SINGAPORE, THROUGH A CLEARER LENS',
        'brand_line': 'Smart Assistant for Government Data Analysis & Explanation',
        'bg': '#f2f7f1',
        'bg_soft': '#e9f1e8',
        'surface': '#ffffff',
        'surface_alt': '#ffffff',
        'surface_tint': '#e9f1e8',
        'text': '#233b32',
        'muted': '#465d51',
        'accent': '#246c55',
        'accent_2': '#397d63',
        'accent_soft': '#e7f3e9',
        'line': '#c7d8cc',
        'hero_from': '#255c4b',
        'hero_to': '#4f8b68',
        'hero_glow': 'rgba(255,255,255,.08)',
        'sidebar_bg': '#eef6ee',
        'sidebar_text': '#1f3b30',
        'sidebar_light': True,
        'shadow': '0 8px 24px rgba(0,0,0,.08)',
        'radius': '18px',
        'font': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'display': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'base_size': '19px',
        'hero_size': '4.4rem',
        'chart_colors': [
            '#126c55',
            '#264fa0',
            '#87519b',
            '#ad4e26',
            '#7a5c00',
            '#a02c65',
            '#486a19',
            '#555b9f',
        ],
        'dark': False,
    },
    "forest": {
        'label': 'Forest · Dark green',
        'sidebar_caption': 'A calm, accessible Sage experience with clear charts and generous type.',
        'eyebrow': 'Discover Singapore through official statistics',
        'hero_note': 'A little curiosity can lead to a great discovery.',
        'side_eyebrow': 'SINGAPORE, THROUGH A CLEARER LENS',
        'brand_line': 'Smart Assistant for Government Data Analysis & Explanation',
        'bg': '#10251f',
        'bg_soft': '#142f27',
        'surface': '#1a352b',
        'surface_alt': '#1a352b',
        'surface_tint': '#142f27',
        'text': '#f4faf5',
        'muted': '#c2d8c9',
        'accent': '#84d9ac',
        'accent_2': '#b4e9a5',
        'accent_soft': '#274b3a',
        'line': '#375e4c',
        'hero_from': '#173b2e',
        'hero_to': '#316a50',
        'hero_glow': 'rgba(255,255,255,.08)',
        'sidebar_bg': '#102c23',
        'sidebar_text': '#ffffff',
        'shadow': '0 8px 24px rgba(0,0,0,.2)',
        'radius': '18px',
        'font': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'display': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'base_size': '19px',
        'hero_size': '4.4rem',
        'chart_colors': [
            '#84e6c2',
            '#9cb8ff',
            '#ffd176',
            '#f7a6c2',
            '#cfb1ff',
            '#b7ec82',
            '#ffb28b',
            '#80d8ef',
        ],
        'dark': True,
    },
    "midnight": {
        'label': 'Midnight · Dark blue',
        'sidebar_caption': 'A calm, accessible Sage experience with clear charts and generous type.',
        'eyebrow': 'Discover Singapore through official statistics',
        'hero_note': 'A little curiosity can lead to a great discovery.',
        'side_eyebrow': 'SINGAPORE, THROUGH A CLEARER LENS',
        'brand_line': 'Smart Assistant for Government Data Analysis & Explanation',
        'bg': '#101f38',
        'bg_soft': '#142845',
        'surface': '#192e4b',
        'surface_alt': '#192e4b',
        'surface_tint': '#142845',
        'text': '#f5f9ff',
        'muted': '#c5d6ea',
        'accent': '#8bbdff',
        'accent_2': '#77d7db',
        'accent_soft': '#2b4667',
        'line': '#3a5474',
        'hero_from': '#1c3962',
        'hero_to': '#315c92',
        'hero_glow': 'rgba(255,255,255,.08)',
        'sidebar_bg': '#102d53',
        'sidebar_text': '#ffffff',
        'shadow': '0 8px 24px rgba(0,0,0,.2)',
        'radius': '18px',
        'font': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'display': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'base_size': '19px',
        'hero_size': '4.4rem',
        'chart_colors': [
            '#9ec8ff',
            '#7de6d3',
            '#ffc887',
            '#e7b0f9',
            '#f7a8af',
            '#a4e387',
            '#f4d96a',
            '#b3b6ff',
        ],
        'dark': True,
    },
    "blue_sky": {
        'label': 'Blue Sky · Light blue',
        'sidebar_caption': 'A calm, accessible Sage experience with clear charts and generous type.',
        'eyebrow': 'Discover Singapore through official statistics',
        'hero_note': 'A little curiosity can lead to a great discovery.',
        'side_eyebrow': 'SINGAPORE, THROUGH A CLEARER LENS',
        'brand_line': 'Smart Assistant for Government Data Analysis & Explanation',
        'bg': '#f0f8fe',
        'bg_soft': '#e2f2ff',
        'surface': '#ffffff',
        'surface_alt': '#ffffff',
        'surface_tint': '#e2f2ff',
        'text': '#233b52',
        'muted': '#4c667d',
        'accent': '#23649c',
        'accent_2': '#3d82c5',
        'accent_soft': '#e4f2fd',
        'line': '#c4d8e6',
        'hero_from': '#235a88',
        'hero_to': '#66a4d4',
        'hero_glow': 'rgba(255,255,255,.08)',
        'sidebar_bg': '#235781',
        'sidebar_text': '#ffffff',
        'shadow': '0 8px 24px rgba(0,0,0,.08)',
        'radius': '18px',
        'font': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'display': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'base_size': '19px',
        'hero_size': '4.4rem',
        'chart_colors': [
            '#145b9d',
            '#177769',
            '#8847a0',
            '#ab4c32',
            '#766000',
            '#a53073',
            '#426b28',
            '#525a99',
        ],
        'dark': False,
    },
    "aubergine": {
        'label': 'Aubergine · Dark purple',
        'sidebar_caption': 'A calm, accessible Sage experience with clear charts and generous type.',
        'eyebrow': 'Discover Singapore through official statistics',
        'hero_note': 'A little curiosity can lead to a great discovery.',
        'side_eyebrow': 'SINGAPORE, THROUGH A CLEARER LENS',
        'brand_line': 'Smart Assistant for Government Data Analysis & Explanation',
        'bg': '#21172e',
        'bg_soft': '#2b1d3c',
        'surface': '#352344',
        'surface_alt': '#352344',
        'surface_tint': '#2b1d3c',
        'text': '#fbf5ff',
        'muted': '#dfcce9',
        'accent': '#d9abef',
        'accent_2': '#cbb3ff',
        'accent_soft': '#4c335e',
        'line': '#654771',
        'hero_from': '#48255e',
        'hero_to': '#70468a',
        'hero_glow': 'rgba(255,255,255,.08)',
        'sidebar_bg': '#2d183e',
        'sidebar_text': '#ffffff',
        'shadow': '0 8px 24px rgba(0,0,0,.2)',
        'radius': '18px',
        'font': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'display': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'base_size': '19px',
        'hero_size': '4.4rem',
        'chart_colors': [
            '#f1b8ff',
            '#88d9ee',
            '#ffca87',
            '#b2e18e',
            '#ffa6bd',
            '#b6b2ff',
            '#f4e68c',
            '#8de2c3',
        ],
        'dark': True,
    },
    "lilac": {
        'label': 'Lilac · Light purple',
        'sidebar_caption': 'A calm, accessible Sage experience with clear charts and generous type.',
        'eyebrow': 'Discover Singapore through official statistics',
        'hero_note': 'A little curiosity can lead to a great discovery.',
        'side_eyebrow': 'SINGAPORE, THROUGH A CLEARER LENS',
        'brand_line': 'Smart Assistant for Government Data Analysis & Explanation',
        'bg': '#f9f4ff',
        'bg_soft': '#f0e8fa',
        'surface': '#ffffff',
        'surface_alt': '#ffffff',
        'surface_tint': '#f0e8fa',
        'text': '#39294c',
        'muted': '#655373',
        'accent': '#7548a1',
        'accent_2': '#946fc3',
        'accent_soft': '#f0e7fa',
        'line': '#dcd0ea',
        'hero_from': '#5c3789',
        'hero_to': '#a083c8',
        'hero_glow': 'rgba(255,255,255,.08)',
        'sidebar_bg': '#4a3168',
        'sidebar_text': '#ffffff',
        'shadow': '0 8px 24px rgba(0,0,0,.08)',
        'radius': '18px',
        'font': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'display': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'base_size': '19px',
        'hero_size': '4.4rem',
        'chart_colors': [
            '#6f3e9b',
            '#206b88',
            '#936000',
            '#ae456b',
            '#28704f',
            '#375da0',
            '#9c4030',
            '#655499',
        ],
        'dark': False,
    },
    "copper": {
        'label': 'Copper · Warm amber',
        'sidebar_caption': 'A calm, accessible Sage experience with clear charts and generous type.',
        'eyebrow': 'Discover Singapore through official statistics',
        'hero_note': 'A little curiosity can lead to a great discovery.',
        'side_eyebrow': 'SINGAPORE, THROUGH A CLEARER LENS',
        'brand_line': 'Smart Assistant for Government Data Analysis & Explanation',
        'bg': '#fff5eb',
        'bg_soft': '#ffe9d6',
        'surface': '#ffffff',
        'surface_alt': '#ffffff',
        'surface_tint': '#ffe9d6',
        'text': '#4a3022',
        'muted': '#765840',
        'accent': '#99420f',
        'accent_2': '#be5d21',
        'accent_soft': '#fff0e0',
        'line': '#e7cbb8',
        'hero_from': '#8b3819',
        'hero_to': '#bb6a38',
        'hero_glow': 'rgba(255,255,255,.08)',
        'sidebar_bg': '#73351d',
        'sidebar_text': '#ffffff',
        'shadow': '0 8px 24px rgba(0,0,0,.08)',
        'radius': '18px',
        'font': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'display': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'base_size': '19px',
        'hero_size': '4.4rem',
        'chart_colors': [
            '#9b4514',
            '#246a63',
            '#34549d',
            '#8d3a87',
            '#786000',
            '#8c3657',
            '#35652e',
            '#6260a2',
        ],
        'dark': False,
    },
    "berry": {
        'label': 'Berry · Dark pink',
        'sidebar_caption': 'A calm, accessible Sage experience with clear charts and generous type.',
        'eyebrow': 'Discover Singapore through official statistics',
        'hero_note': 'A little curiosity can lead to a great discovery.',
        'side_eyebrow': 'SINGAPORE, THROUGH A CLEARER LENS',
        'brand_line': 'Smart Assistant for Government Data Analysis & Explanation',
        'bg': '#2c1427',
        'bg_soft': '#3d1c35',
        'surface': '#48263f',
        'surface_alt': '#48263f',
        'surface_tint': '#3d1c35',
        'text': '#fff4fb',
        'muted': '#f0cde2',
        'accent': '#f3a4cc',
        'accent_2': '#e3b1f0',
        'accent_soft': '#623752',
        'line': '#7a5268',
        'hero_from': '#692950',
        'hero_to': '#a34779',
        'hero_glow': 'rgba(255,255,255,.08)',
        'sidebar_bg': '#4c1c39',
        'sidebar_text': '#ffffff',
        'shadow': '0 8px 24px rgba(0,0,0,.2)',
        'radius': '18px',
        'font': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'display': '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
        'base_size': '19px',
        'hero_size': '4.4rem',
        'chart_colors': [
            '#ffb5d8',
            '#9de2da',
            '#ffd691',
            '#aabbff',
            '#bef0a0',
            '#e5b5ff',
            '#ffae95',
            '#8dcbef',
        ],
        'dark': True,
    },
}


def theme_keys() -> list[str]:
    """Theme keys in display order."""
    return list(THEME_ORDER)


def theme_label(key: str) -> str:
    """Human-readable name of a theme (default theme if unknown)."""
    return THEMES.get(key, THEMES["sage_mist"])["label"]


def current_theme_key() -> str:
    # Deliberately one coherent, high-contrast brand. Old widget/session values
    # must not silently turn charts dark while the interface remains light.
    """The session's chosen theme key, or the default if unset/unknown."""
    selected = st.session_state.get("sage_theme_choice", "sage_mist")
    return selected if selected in THEMES else "sage_mist"


def current_theme() -> dict:
    """The session's theme settings (colours, fonts, ...)."""
    return THEMES[current_theme_key()]


def theme_sidebar_caption(key: str | None = None) -> str:
    """Short sidebar caption describing a theme."""
    meta = THEMES.get(key or current_theme_key(), THEMES["sage_mist"])
    return meta["sidebar_caption"]


# This SVG is decoration, not a factual time series. Coordinates are deliberately
# non-data-bearing and labels do not imply any actual observed result.
#: The header illustration. Kept as a plain SVG file so designers can edit it;
#: theme_eureka_art() recolours three of its colours for the chosen theme.
_EUREKA_ART_FILE = Path(__file__).resolve().parent.parent / "assets" / "eureka_art.svg"
EUREKA_ART = _EUREKA_ART_FILE.read_text(encoding="utf-8")


# Eight coherent editorial variations, each preserving the same clear eureka
# narrative while giving the explorer's surroundings a distinct personality.
_ART_TONES = {
    "sage_mist": ("#ffdf81", "#a7cecb", "#253a54", "✦"),
    "forest": ("#c8f6a9", "#74cda7", "#1d3b33", "❋"),
    "midnight": ("#9bdaff", "#b2b7ff", "#24346c", "✧"),
    "blue_sky": ("#ffe3a6", "#8acbed", "#284b77", "☼"),
    "aubergine": ("#ffd1ef", "#be9bea", "#49316b", "✳"),
    "lilac": ("#ffe6a8", "#dbc6ff", "#604d91", "✦"),
    "copper": ("#f9d49e", "#e1a889", "#794838", "❈"),
    "berry": ("#ffd7a4", "#eaa8cd", "#622c59", "✺"),
}


def theme_eureka_art(theme_key: str) -> str:
    """The header illustration (SVG) recoloured for a theme."""
    bulb, shirt, hair, _ = _ART_TONES.get(theme_key, _ART_TONES["sage_mist"])
    art = EUREKA_ART.replace("#fff9c6", bulb).replace("#ecf9f5", shirt).replace("#073f37", hair)
    return art


def _rgb(hex_colour: str) -> tuple:
    """(red, green, blue) as 0-1 floats from a "#RRGGBB" colour."""
    return tuple(int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))


def _luminance(hex_colour: str) -> float:
    """Relative luminance of a colour (WCAG formula), for contrast checks."""

    def linear(value: float) -> float:
        """A colour channel converted to linear light (sRGB formula)."""
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return sum(
        weight * linear(value) for weight, value in zip((0.2126, 0.7152, 0.0722), _rgb(hex_colour))
    )


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two hex colours (1 to 21)."""
    x, y = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def on_accent(theme: dict) -> str:
    """Legible button lettering on saturated AND pastel primary buttons."""
    accent = theme['accent']
    light, dark = '#ffffff', '#12241d'
    return light if contrast_ratio(accent, light) >= contrast_ratio(accent, dark) else dark


def theme_swatch_html() -> str:
    """Purely decorative palette chips; actual selection is the accessible selectbox."""
    selected = current_theme_key()
    chips = ''.join(
        '<span class="sage-theme-chip' + (' is-current' if key == selected else '') + '"'
        ' style="background:'
        + THEMES[key]['accent']
        + '" title="'
        + escape(THEMES[key]['label'], quote=True)
        + '"'
        ' aria-label="' + escape(THEMES[key]['label'], quote=True) + '"></span>'
        for key in THEME_ORDER
    )
    return (
        '<div class="sage-theme-swatches" aria-label="Available colour palettes">'
        + chips
        + '</div>'
    )


_THEME_CSS_FILE = Path(__file__).resolve().parent.parent / "assets" / "sage_theme.css"


@lru_cache(maxsize=1)
def _theme_css_template() -> Template:
    """The page CSS as a string.Template, read once (see assets/sage_theme.css)."""
    text = _THEME_CSS_FILE.read_text(encoding="utf-8")
    return Template(text[text.index("*/") + 3 :])  # skip the explanatory header comment


def _style_for(theme: dict) -> str:
    """The <style> block for one theme: the CSS template filled with its values.

    Besides the theme's own keys (colours, fonts, radius ...), the template
    uses base_size and hero_size (with defaults), a fixed button height and
    button_ink, the readable text colour on the accent (see on_accent).
    """
    values = dict(
        theme,
        base_size=theme.get("base_size", "19px"),
        hero_size=theme.get("hero_size", "4.75rem"),
        button_height="52px",
        button_ink=on_accent(theme),
    )
    return "\n<style>\n" + _theme_css_template().substitute(values) + "</style>\n"


_LIGHT_SIDEBAR_CSS = """
<style>
[data-testid="stSidebar"] { background:#eef6ee !important; border-right:1px solid #d3e3d6 !important; }
[data-testid="stSidebar"] :is(p,label,li,h1,h2,h3,small,span,div) { color:#1f3b30 !important; }
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p { color:#4a6558 !important; }
[data-testid="stSidebar"] hr { border-color:#d3e3d6 !important; }
[data-testid="stSidebar"] .sage-sidebrand__mark { background:#dcefe2; border-color:#bcd8c6; color:#1d6a4e !important; }
[data-testid="stSidebar"] .sage-sidebrand__line { background:linear-gradient(90deg,#9fc6ae,rgba(159,198,174,0)); }
[data-testid="stSidebar"] [data-testid="stButton"] button {
    background:#ffffff !important; color:#1f3b30 !important; border:1px solid #bcd3c3 !important;
}
[data-testid="stSidebar"] [data-testid="stButton"] button:hover:not(:disabled) { background:#e3f1e6 !important; }
[data-testid="stSidebar"] .st-key-sage_random_question button {
    background:#0a6b45 !important; color:#ffffff !important; border-color:transparent !important;
}
[data-testid="stSidebar"] .st-key-sage_random_question button :is(p,span) { color:#ffffff !important; }
[data-testid="stSidebar"] .st-key-sage_random_question button:hover:not(:disabled) {
    background:#08573a !important; color:#ffffff !important; border-color:transparent !important; filter:none;
}
[data-testid="stSidebar"] .st-key-sage_random_question button:hover:not(:disabled) :is(p,span) { color:#ffffff !important; }
[data-testid="stMain"] [class*="st-key-sage_redraw_"] button { background:#0a6b45 !important; }
[data-testid="stMain"] [class*="st-key-sage_redraw_"] button:hover:not(:disabled) { background:#08573a !important; filter:none; }
[data-testid="stSidebar"] [data-testid="stToggle"] label, [data-testid="stSidebar"] [data-testid="stCheckbox"] label { color:#1f3b30 !important; }

/* ---- Final sidebar polish (keep LAST; uses theme variables so every theme matches) ---- */
/* Dropdowns: white field, theme border and text -- never the browser's dark default. */
[data-testid="stSidebar"] [data-baseweb="select"] > div,
[data-testid="stSidebar"] [data-baseweb="select"] > div > div {
    background:var(--sage-surface) !important; color:var(--sage-text) !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] > div {
    border:1px solid var(--sage-line) !important; border-radius:12px !important;
    box-shadow:none !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] > div:hover,
[data-testid="stSidebar"] [data-baseweb="select"] > div:focus-within {
    border-color:var(--sage-accent) !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] :is(span,input,div) { color:var(--sage-text) !important; }
[data-testid="stSidebar"] [data-baseweb="select"] svg { color:var(--sage-accent) !important; fill:var(--sage-accent) !important; }
/* Checkboxes: a clear square box, white when off, accent green with a white tick when on. */
[data-testid="stSidebar"] [data-testid="stCheckbox"] [role="checkbox"] {
    background:var(--sage-surface) !important;
    border:2px solid var(--sage-muted) !important;
    min-width:0 !important; min-height:0 !important;
    width:1.15rem !important; height:1.15rem !important;
    border-radius:5px !important; box-shadow:none !important;
}
[data-testid="stSidebar"] [data-testid="stCheckbox"] [role="checkbox"][aria-checked="true"] {
    background:var(--sage-accent) !important; border-color:var(--sage-accent) !important;
}
[data-testid="stSidebar"] [data-testid="stCheckbox"] [role="checkbox"] svg {
    color:#ffffff !important; fill:#ffffff !important;
}
/* "Advanced settings": a rounded card instead of a square white slab. */
[data-testid="stSidebar"] [data-testid="stExpander"] details {
    background:var(--sage-surface) !important;
    border:1px solid var(--sage-line) !important; border-radius:14px !important;
    overflow:hidden;
}
[data-testid="stSidebar"] [data-testid="stExpander"] summary { background:transparent !important; }
/* Inline code (e.g. the operator command): soft tint, readable text, wraps cleanly. */
[data-testid="stSidebar"] :not(pre) > code {
    background:var(--sage-accent-soft) !important; color:var(--sage-text) !important;
    border:1px solid var(--sage-line); border-radius:6px; padding:.05rem .3rem;
    font-size:.82em; word-break:break-all;
}
</style>
"""


# the Singapore Open Data Licence version named in its attribution sentence
ODL_VERSION = "1.0"
ODL_URL = "https://data.gov.sg/open-data-licence"
# Singapore has no daylight saving; avoids needing tz data on Windows
_SINGAPORE = timezone(timedelta(hours=8))
_OFFICIAL_TABLE_URL = re.compile(
    r"^https://tablebuilder\.singstat\.gov\.sg/table/(?:TS|CT|DC)/[A-Za-z0-9]+$"
)


def singapore_date_text(now: datetime | None = None) -> str:
    """Today's date in Singapore as 21 October 2026 (no leading zero, no platform-specific format
    codes)."""
    moment = (now or datetime.now(timezone.utc)).astimezone(_SINGAPORE)
    return f"{moment.day} {moment:%B} {moment.year}"


def attribution_html(title: str, source_url: str, now: datetime | None = None) -> str:
    """The Singapore Open Data Licence's attribution sentence for one table.

    Names the dataset, the date it was accessed, the source and the licence version, with a link to
    the licence. Everything that comes from data (title, URL) is escaped, and only an official
    SingStat Table Builder address is ever turned into a link.
    """
    name = escape(" ".join(str(title or "this table").split()))
    when = escape(singapore_date_text(now))
    source = "SingStat Table Builder"
    if _OFFICIAL_TABLE_URL.match(str(source_url or "")):
        source = (
            f'<a href="{escape(source_url, quote=True)}" target="_blank" rel="noopener '
            f'noreferrer">SingStat Table Builder</a>'
        )
    return (
        f'<div class="sage-attribution">Contains information from {name} accessed on {when} '
        f'from {source}, '
        f'which is made available under the terms of the '
        f'<a href="{ODL_URL}" target="_blank" rel="noopener noreferrer">'
        f'Singapore Open Data Licence version {ODL_VERSION}</a>.</div>'
    )


def render_data_attribution(title: str, source_url: str) -> None:
    """Show the licence attribution for the table on screen (called once per explored table)."""
    st.markdown(attribution_html(title, source_url), unsafe_allow_html=True)


DISCLAIMER_HTML = (
    '<div class="sage-disclaimer">'
    '<p><strong>Data source and attribution.</strong> Statistical data are sourced from the '
    'Singapore Department of '
    'Statistics (SingStat), through SingStat Table Builder, and remain subject to SingStat\'s '
    'terms of use and the '
    '<a href="https://data.gov.sg/open-data-licence" target="_blank" rel="noopener noreferrer">'
    'Singapore Open Data Licence</a>.</p>'
    '<p><strong>Disclaimer.</strong> This information, including AI-generated summaries and '
    'explanations, is provided '
    'for general reference only. While every care is taken to ensure that the data presented is '
    'accurate and reliable, '
    'no warranty or representation, express or implied, is given as to its accuracy, completeness '
    'or fitness for any '
    'particular purpose. Where accuracy matters, please verify the figures against the official '
    'source of the data '
    'provided for public use, '
    '<a href="https://tablebuilder.singstat.gov.sg" target="_blank" rel="noopener '
    'noreferrer">SingStat Table Builder</a>'
    ' or <a href="https://www.singstat.gov.sg" target="_blank" rel="noopener '
    'noreferrer">singstat.gov.sg</a>, '
    'before relying on them. To the fullest extent permitted by law, the operators of this service '
    'accept no liability '
    'for any loss or damage arising from the use of, or reliance on, this information.</p>'
    '</div>'
)


def render_disclaimer() -> None:
    """A small data-source note at the foot of the page. Fixed text; nothing user-supplied is
    inserted."""
    st.markdown(DISCLAIMER_HTML, unsafe_allow_html=True)


@lru_cache(maxsize=None)
def _cached_stylesheet(theme_key: str) -> str:
    """The stylesheet depends only on the (constant) theme, so build it once per theme."""
    return _style_for(THEMES[theme_key])


def install_design_system() -> None:
    """Inject the selected theme stylesheet once per rerun."""
    theme = current_theme()
    st.markdown(_cached_stylesheet(current_theme_key()), unsafe_allow_html=True)
    if theme.get("sidebar_light"):
        st.markdown(_LIGHT_SIDEBAR_CSS, unsafe_allow_html=True)


def render_sidebar_brand() -> None:
    """Render the sidebar wordmark and current design pill."""
    theme = current_theme()
    st.markdown(
        dedent(
            f"""
            <div class="sage-sidebrand" aria-label="Sage brand block">
              <div class="sage-sidebrand__eyebrow">{escape(theme['side_eyebrow'])}</div>
              <div class="sage-sidebrand__top">
                <div class="sage-sidebrand__mark" aria-hidden="true">S</div>
                <div class="sage-sidebrand__name">Sage<span style="opacity:.82">.</span></div>
              </div>
              <div class="sage-sidebrand__line"></div>
              <div class="sage-sidebrand__caption">{escape(theme['brand_line'])}</div>
              </div>
            """
        ),
        unsafe_allow_html=True,
    )


@lru_cache(maxsize=1)
def _eureka_banner_data_uri() -> str:
    """Small bundled image from the approved hero reference, cached per worker."""
    image_file = Path(__file__).resolve().parent.parent / "assets" / "sage_singapore_eureka.webp"
    try:
        return "data:image/webp;base64," + base64.b64encode(image_file.read_bytes()).decode("ascii")
    except OSError:
        return ""  # Hero remains useful when the asset is missing.


def cycle_theme() -> None:
    """Button callback: move to the next theme, wrapping round (round robin)."""
    keys = theme_keys()
    current = current_theme_key()
    index = keys.index(current) if current in keys else -1
    st.session_state["sage_theme_choice"] = keys[(index + 1) % len(keys)]


def render_hero() -> None:
    """Render the approved Singapore skyline artwork; controls and statistics stay live."""
    theme = current_theme()
    banner = _eureka_banner_data_uri()
    image_html = (
        (
            f'<div class="sage-new-hero-art" aria-hidden="true" '
            f'style="background-image:url(\'{banner}\')"></div>'
        )
        if banner
        else ""
    )
    with st.container(key="sage_hero_wrap"):
        st.markdown(
            dedent(
                f"""
                <section class="sage-hero sage-new-hero" aria-labelledby="sage-hero-title">
                  {image_html}
                  <div class="sage-new-hero-content">
                    <div class="sage-hero__eyebrow">Sage · Singapore official statistics</div>
                    <h1 id="sage-hero-title">Ask. <span>Discover.</span> Understand<br>Singapore through data.</h1>
                    <p class="sage-hero__sub">Ask about Singapore and explore trusted SingStat data<br>through clear charts and plain-language explanations.</p>
                  </div>
                </section>
                """
            ),
            unsafe_allow_html=True,
        )
        st.button("Change theme", key="sage_hero_cycle", on_click=cycle_theme)


def render_landing_cards() -> None:
    """Three static, lightweight onboarding cards. No data or tracking."""
    st.markdown(
        """<section class="sage-landing-section" aria-label="Three things Sage helps you do">
          <p class="sage-onboarding-label">HOW SAGE HELPS · A QUICK GUIDE</p>
          <div class="sage-landing-cards">
            <div class="sage-landing-card"><span class="sage-landing-card__icon" aria-hidden="true">✦</span><div>
              <p class="sage-landing-card__title">1 · Ask</p><p class="sage-landing-card__desc">Try anything you have wondered about Singapore.</p>
            </div></div>
            <div class="sage-landing-card"><span class="sage-landing-card__icon" aria-hidden="true">↗</span><div>
              <p class="sage-landing-card__title">2 · Explore</p><p class="sage-landing-card__desc">Readable charts, with the sources beside them.</p>
            </div></div>
            <div class="sage-landing-card"><span class="sage-landing-card__icon" aria-hidden="true">☼</span><div>
              <p class="sage-landing-card__title">3 · Understand</p><p class="sage-landing-card__desc">Useful explanations without the jargon.</p>
            </div></div>
          </div>
        </section>""",
        unsafe_allow_html=True,
    )


def style_altair_chart(chart):
    """Make every label visible on a chart with the selected Sage palette.

    Streamlit normally applies its own chart theme. Every call site MUST pass
    theme=None to st.altair_chart so this configuration wins, even when the
    user's Streamlit/OS preference is dark but Sage Mist has a light surface.
    This function changes presentation only; not official observations.
    """
    # Charts deliberately retain the verified light, high-contrast styling
    # even while the surrounding page changes theme.
    theme = THEMES['sage_mist']
    ink = theme['text']
    # Encoding-level colour beats every Vega default, including a globally
    # dark Streamlit theme or old cached chart configuration.
    encoding = getattr(chart, 'encoding', None)
    if encoding is not None:
        for channel_name in ('x', 'y'):
            channel = getattr(encoding, channel_name, None)
            axis = getattr(channel, 'axis', None)
            if axis is not None and hasattr(axis, "to_dict"):
                axis.labelColor = ink
                axis.titleColor = ink
                axis.labelFontSize = 17
                axis.titleFontSize = 18
        colour = getattr(encoding, 'color', None)
        legend = getattr(colour, 'legend', None)
        if legend is not None and legend is not False and hasattr(legend, "to_dict"):
            legend.labelColor = ink
            legend.titleColor = ink
            legend.labelFontSize = 17
    return (
        chart.properties(padding={'left': 18, 'right': 22, 'top': 20, 'bottom': 16})
        .configure_axis(
            labelColor=ink,
            titleColor=ink,
            gridColor=theme['line'],
            domainColor=theme['muted'],
            tickColor=theme['muted'],
            labelFontSize=17,
            titleFontSize=18,
            labelFontWeight='bold',
            titleFontWeight='bold',
            labelFont='Segoe UI',
            titleFont='Segoe UI',
            labelLimit=350,
            titleLimit=450,
            labelPadding=9,
            titlePadding=14,
        )
        .configure_legend(
            labelColor=ink,
            titleColor=ink,
            labelFontSize=17,
            titleFontSize=18,
            labelFontWeight='bold',
            titleFontWeight='bold',
            labelFont='Segoe UI',
            titleFont='Segoe UI',
            labelLimit=340,
            titleLimit=400,
            symbolSize=160,
        )
        .configure_title(
            color=ink,
            fontSize=22,
            fontWeight='bold',
            font='Segoe UI',
            limit=850,
            offset=14,
        )
        .configure_header(labelColor=ink, titleColor=ink)
        .configure_view(strokeWidth=0)
        .configure_range(category=theme['chart_colors'])
        .configure(background="#ffffff")
    )


_NO_GROUP_COLUMNS = re.compile(
    r"(?i)^(year|period|date|time|month|quarter|id|code|rank|order|no|index|series id|table id)$"
)
_PLAIN_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def format_cell_number(column, value) -> str:
    """Add thousand separators (1,234,567.89) to numeric cells; leave text, IDs and years alone."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return str(value)
        raw = repr(value) if isinstance(value, float) else str(value)
        if "e" in raw.lower():
            raw = f"{value:.6f}".rstrip("0").rstrip(".")
        from_text = False
    elif isinstance(value, str) and _PLAIN_NUMBER.match(value.strip()):
        raw, from_text = value.strip(), True
    else:
        return str(value)
    if _NO_GROUP_COLUMNS.match(str(column).strip()):
        return str(value)
    sign = "-" if raw.startswith("-") else ""
    whole, _, fraction = raw.lstrip("+-").partition(".")
    if from_text and (
        (len(whole) > 1 and whole.startswith("0"))
        or (not fraction and len(whole) == 4 and 1800 <= int(whole) <= 2100)
    ):
        return str(value)  # codes like 007, and year-looking labels such as "2020"
    if fraction.endswith("0") and not from_text and isinstance(value, float):
        fraction = fraction.rstrip("0")
    return sign + f"{int(whole):,}" + ("." + fraction if fraction else "")


#: Cell text this short and without spaces (a code, a number, a single word such as
#: "embeddings" or "text-embedding-3-small") is never broken across lines.
_KEEP_WHOLE_MAX = 28
#: Cell text longer than this is prose and keeps a readable column width.
_PROSE_MIN_CHARS = 40


def _cell_html(column, value) -> str:
    """One escaped table cell; short single-word values are kept on one line."""
    text = format_cell_number(column, value)
    keep_whole = len(text) <= _KEEP_WHOLE_MAX and not any(ch.isspace() for ch in text)
    # Long text gets a readable minimum width instead of a tall one-word-per-line strip.
    css_class = (
        ' class="sage-nowrap"'
        if keep_whole
        else ' class="sage-prose"'
        if len(text) > _PROSE_MIN_CHARS
        else ''
    )
    return (
        '<td'
        + css_class
        + ' data-label="'
        + escape(str(column), quote=True)
        + '">'
        + escape(text)
        + '</td>'
    )


def readable_table_html(records: list[dict], *, max_columns: int | None = None) -> str:
    """Safely escape and wrap every cell. No untrusted HTML/JS is interpreted.

    Long text wraps between words; short single-word values (codes, numbers,
    names like "embeddings") stay whole on one line in every browser.
    """
    if not records:
        return '<p class="sage-readmore">No rows to display.</p>'
    for item in records:
        if not isinstance(item, dict):
            raise ValueError('Expected dictionary rows')
    # Union of all column names: later rows may contain additional columns.
    columns = list(dict.fromkeys(str(key) for item in records for key in item))
    if max_columns is not None:
        columns = columns[:max_columns]
    head = ''.join('<th scope="col">' + escape(str(k)) + '</th>' for k in columns)
    body = ''.join(
        '<tr>' + ''.join(_cell_html(k, item.get(k)) for k in columns) + '</tr>' for item in records
    )
    return (
        '<div class="sage-readable-table-scroller"><table class="sage-readable-table">'
        '<thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>'
    )


def render_readable_table(rows, *, key: str, page_size: int = 20) -> None:
    """Display a bounded, text-first table with complete wrapping and pagination."""
    records = [dict(row) for row in rows if isinstance(row, dict)]
    if not records:
        st.caption('No observations to display.')
        return
    page_count = (len(records) + page_size - 1) // page_size
    page = (
        st.selectbox(
            f'Table rows · {len(records):,} available',
            options=list(range(page_count)),
            format_func=lambda n: f'{n * page_size + 1:,}–{min(len(records), (n + 1) * page_size):,} '
            f'of {len(records):,}',
            key=f'sage_table_page_{key}',
            label_visibility='visible' if page_count > 1 else 'collapsed',
        )
        if page_count > 1
        else 0
    )
    st.markdown(
        readable_table_html(records[page * page_size : (page + 1) * page_size]),
        unsafe_allow_html=True,
    )


def render_debug_json_text(value, *, key: str, page_size: int = 18000) -> None:
    """Render raw diagnostic JSON as searchable, selectable, fully wrapping text.

    Pagination bounds the HTML size for very large technical responses without
    altering or discarding any of the diagnostic content.
    """
    raw = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if not raw:
        st.caption("No diagnostic output to display.")
        return
    pages = [raw[i : i + page_size] for i in range(0, len(raw), page_size)]
    if len(pages) > 1:
        page = st.selectbox(
            f"Diagnostic text · {len(raw):,} characters",
            options=list(range(len(pages))),
            format_func=lambda i: f"Part {i+1:,} of {len(pages):,}",
            key=f"sage_json_page_{key}",
        )
    else:
        page = 0
    st.markdown(
        '<pre class="sage-diagnostic-text">' + escape(pages[page]) + '</pre>',
        unsafe_allow_html=True,
    )
