"""科技感視覺：全域樣式與共用元件（KPI 卡、比較條、狀態標籤）。

原則：用留白與層次表現科技感，不堆特效——
- 深藍黑底、細線框卡片、藍青色點綴，只有「狀態燈」有呼吸動畫
- 數字用等寬字型、單位縮小，一眼看到數值
- 顏色只用來表達意義（省／多花、尖峰／離峰、舒適／偏熱），亮暗主題都可讀
"""
from __future__ import annotations

import html

import streamlit as st

ACCENT = "#3ba7ff"
GOOD = "#22c55e"
BAD = "#f97316"

GLOBAL_CSS = """<style>
:root { --hems-accent: #3ba7ff; --hems-accent2: #22d3ee; --hems-good: #22c55e; --hems-bad: #f97316;
        --hems-line: rgba(120,160,255,.18); --hems-card: rgba(59,167,255,.045); --hems-muted: rgba(150,165,190,.95); }
.block-container { padding-top: 3.6rem; max-width: 1400px; }
h1, h2, h3 { letter-spacing: .02em; }
.mono { font-family: "JetBrains Mono", "Cascadia Mono", Consolas, ui-monospace, monospace; font-variant-numeric: tabular-nums; }

/* 頁首 */
.hems-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 14px; }
.hems-head .title { font-size: 1.65rem; font-weight: 800; line-height: 1.2; }
.hems-head .title small { display: block; font-size: .8rem; font-weight: 600; color: var(--hems-accent);
  letter-spacing: .18em; margin-bottom: 4px; }
.hems-chips { display: flex; gap: 8px; flex-wrap: wrap; }
.hems-chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 11px; border-radius: 999px;
  border: 1px solid var(--hems-line); background: var(--hems-card); font-size: .82rem; white-space: nowrap; }
.hems-chip b { font-weight: 700; }
.hems-chip.peak { border-color: rgba(249,115,22,.45); color: #fb923c; }
.hems-chip.off { border-color: rgba(34,197,94,.45); color: #4ade80; }

/* KPI 卡 */
.hems-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin: 6px 0 18px; }
@media (max-width: 900px) { .hems-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.hems-kpi { position: relative; padding: 16px 18px 14px; border-radius: 16px; border: 1px solid var(--hems-line);
  background: linear-gradient(160deg, rgba(59,167,255,.10), rgba(59,167,255,.015) 60%); overflow: hidden; }
.hems-kpi::after { content: ""; position: absolute; left: 0; top: 0; width: 100%; height: 2px;
  background: linear-gradient(90deg, var(--hems-accent), var(--hems-accent2), transparent); opacity: .8; }
.hems-kpi .label { font-size: .8rem; color: var(--hems-muted); letter-spacing: .06em; display: flex; align-items: center; gap: 7px; }
.hems-kpi .value { font-size: 2.15rem; font-weight: 700; line-height: 1.15; margin-top: 6px; }
.hems-kpi .value .unit { font-size: .95rem; font-weight: 600; color: var(--hems-muted); margin-left: 4px; }
.hems-kpi .sub { font-size: .8rem; color: var(--hems-muted); margin-top: 6px; }
.hems-delta { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: .76rem; font-weight: 700; margin-right: 6px; }
.hems-delta.good { background: rgba(34,197,94,.14); color: #4ade80; }
.hems-delta.bad { background: rgba(249,115,22,.14); color: #fb923c; }
.hems-delta.flat { background: rgba(150,165,190,.14); color: var(--hems-muted); }
.pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--hems-good); display: inline-block;
  box-shadow: 0 0 0 0 rgba(34,197,94,.6); animation: hems-pulse 2s infinite; }
@keyframes hems-pulse { 0% { box-shadow: 0 0 0 0 rgba(34,197,94,.55); } 70% { box-shadow: 0 0 0 8px rgba(34,197,94,0); }
  100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); } }

/* 區塊標題與卡片 */
.hems-section { font-size: .78rem; font-weight: 700; letter-spacing: .16em; color: var(--hems-accent); margin: 4px 0 8px; }
.hems-card { border: 1px solid var(--hems-line); background: var(--hems-card); border-radius: 16px; padding: 14px 16px; }
.hems-tip { display: flex; gap: 10px; padding: 9px 0; border-bottom: 1px dashed var(--hems-line); font-size: .92rem; line-height: 1.55; }
.hems-tip:last-child { border-bottom: 0; }
.hems-tip .icon { flex-shrink: 0; width: 26px; height: 26px; border-radius: 8px; display: grid; place-items: center;
  background: rgba(59,167,255,.12); }

/* 比較條：基準 vs 目前 */
.hems-cmp { display: grid; grid-template-columns: 7.5rem 1fr 6.5rem; gap: 6px 12px; align-items: center; font-size: .88rem; }
.hems-cmp .name { color: var(--hems-muted); }
.hems-cmp .bars { display: grid; gap: 4px; }
.hems-cmp .bar { height: 9px; border-radius: 6px; }
.hems-cmp .bar.base { background: rgba(150,165,190,.45); }
.hems-cmp .bar.now { background: linear-gradient(90deg, var(--hems-accent), var(--hems-accent2)); }
.hems-cmp .vals { text-align: right; font-size: .8rem; line-height: 1.3; }
.hems-legend { display: flex; gap: 16px; font-size: .78rem; color: var(--hems-muted); margin-bottom: 8px; }
.hems-legend i { display: inline-block; width: 18px; height: 8px; border-radius: 4px; margin-right: 6px; vertical-align: middle; }

/* 左側選單 */
section[data-testid="stSidebar"] .stButton button { justify-content: flex-start; padding: .55rem .9rem;
  border-radius: 10px; font-weight: 600; border-color: var(--hems-line); }
section[data-testid="stSidebar"] .stButton button > div { justify-content: flex-start; width: 100%; text-align: left; }
section[data-testid="stSidebar"] .stButton button:hover { border-color: var(--hems-accent); color: var(--hems-accent); }
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { margin: -.5rem 0 .45rem .9rem; }

/* 展開區塊 */
[data-testid="stExpander"] details { border-radius: 14px; border-color: var(--hems-line); }
</style>"""


def apply_theme() -> None:
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)


def esc(text) -> str:
    return html.escape(str(text))


def page_header(kicker: str, title: str, chips: list[tuple[str, str]] | None = None) -> None:
    """頁首：小標（英文代號）＋大標題，右側為狀態標籤。chips：[(文字 HTML, 樣式 class)]。"""
    chip_html = "".join(f'<span class="hems-chip {cls}">{text}</span>' for text, cls in (chips or []))
    st.markdown(f'<div class="hems-head"><div class="title"><small>{esc(kicker)}</small>{esc(title)}</div>'
                f'<div class="hems-chips">{chip_html}</div></div>', unsafe_allow_html=True)


def delta_badge(value: float, base: float, lower_is_better: bool = True, fmt: str = "{:+.0f}%") -> str:
    """與基準相比的變化標籤：省（綠）、多花（橘）、持平（灰）。"""
    if not base:
        return ""
    pct = (value - base) / abs(base) * 100
    if abs(pct) < 0.5:
        return '<span class="hems-delta flat">持平</span>'
    good = (pct < 0) == lower_is_better
    return f'<span class="hems-delta {"good" if good else "bad"}">{fmt.format(pct)}</span>'


def kpi_cards(cards: list[dict]) -> None:
    """大數字卡片。每張：label、value、unit、sub（HTML）、live（是否顯示呼吸燈）。"""
    items = []
    for c in cards:
        dot = '<span class="pulse"></span>' if c.get("live") else ""
        items.append(f'<div class="hems-kpi"><div class="label">{dot}{esc(c["label"])}</div>'
                     f'<div class="value mono">{esc(c["value"])}<span class="unit">{esc(c.get("unit", ""))}</span></div>'
                     f'<div class="sub">{c.get("sub", "")}</div></div>')
    st.markdown(f'<div class="hems-kpis">{"".join(items)}</div>', unsafe_allow_html=True)


def compare_bars(rows: list[dict]) -> None:
    """「基準 vs 目前」比較條。每列：name、base、now、unit、fmt、lower_is_better。"""
    out = ['<div class="hems-legend"><span><i style="background:rgba(150,165,190,.45)"></i>一般習慣</span>'
           '<span><i style="background:linear-gradient(90deg,#3ba7ff,#22d3ee)"></i>目前模式</span></div>',
           '<div class="hems-cmp">']
    for r in rows:
        top = max(abs(r["base"]), abs(r["now"])) or 1
        fmt = r.get("fmt", "{:.1f}")
        out.append(
            f'<div class="name">{esc(r["name"])}</div>'
            f'<div class="bars"><div class="bar base" style="width:{abs(r["base"]) / top * 100:.1f}%"></div>'
            f'<div class="bar now" style="width:{abs(r["now"]) / top * 100:.1f}%"></div></div>'
            f'<div class="vals mono">{delta_badge(r["now"], r["base"], r.get("lower_is_better", True))}<br>'
            f'{fmt.format(r["base"])} → <b>{fmt.format(r["now"])}</b> {esc(r.get("unit", ""))}</div>')
    out.append("</div>")
    st.markdown("".join(out), unsafe_allow_html=True)


def section(title: str) -> None:
    st.markdown(f'<div class="hems-section">{esc(title)}</div>', unsafe_allow_html=True)


def tips_card(tips: list[tuple[str, str]]) -> None:
    """今日建議卡：[(圖示, 文字 HTML)]。"""
    body = "".join(f'<div class="hems-tip"><span class="icon">{icon}</span><span>{text}</span></div>' for icon, text in tips)
    st.markdown(f'<div class="hems-card">{body}</div>', unsafe_allow_html=True)
