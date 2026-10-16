"""🏠 首頁：家中此刻。

進入介面第一眼只看到最重要的資訊：此刻用電、今日用電與電費、誰在家；
下方是唯讀小平面圖、今日用電曲線與今日建議；其他細節收在可展開的區塊裡。
"""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from ui import charts
from ui.context import AppContext, room_comfort
from ui.floorplan import floorplan
from ui.pages.plan import make_payload
from ui.theme import compare_bars, delta_badge, kpi_cards, page_header, section, tips_card


def _fmt_hour(h: float) -> str:
    return f"{int(h):02d}:{int(round(h % 1 * 60)):02d}"


def today_tips(ctx: AppContext, base) -> list[tuple[str, str]]:
    """依今天的模擬結果產生 2–4 則建議。"""
    r, h = ctx.selected, ctx.hour
    hourly = r.hourly.set_index("hour")
    tips = []
    saved = base.energy_cost - r.energy_cost
    if saved > 0.5:
        tips.append(("💰", f"今天預計電費 <b>{r.energy_cost:.0f} 元</b>，比一般習慣少 <b>{saved:.0f} 元</b>"
                           f"（−{saved / base.energy_cost * 100:.0f}%）。"))
    for t in r.tasks:
        if abs(t["start"] - t["default_start"]) > 0.01:
            tips.append(("🕑", f"{t['name']}排在 <b>{_fmt_hour(t['start'])}</b> 運轉（原本習慣 {_fmt_hour(t['default_start'])}），"
                               "避開尖峰電價。"))
    period = hourly.loc[h, "period_label"]
    upcoming = [x for x in range(h + 1, 24) if hourly.loc[x, "period_label"] != period]
    if period in ("尖峰", "半尖峰") and upcoming:
        tips.append(("⚡", f"目前是<b>{period}</b>電價（{hourly.loc[h, 'price']:.2f} 元/度），"
                           f"非必要的大功率家電建議延到 <b>{upcoming[0]:02d}:00</b> 之後。"))
    elif period == "離峰" and upcoming:
        tips.append(("⚡", f"<b>{upcoming[0]:02d}:00</b> 起進入{hourly.loc[upcoming[0], 'period_label']}"
                           f"（{hourly.loc[upcoming[0], 'price']:.2f} 元/度），洗衣、烘衣可趁現在完成。"))
    comfort = room_comfort(ctx.analyzer, r, h)
    names = {s.id: s.name for s in ctx.house.spaces}
    temps = r.climate[r.climate["hour"] == h].set_index("space_id")["room_temp_c"]
    hot = [sid for sid, state in comfort.items() if state == "warm"]
    if hot:
        sid = hot[0]
        tips.append(("🌡️", f"{names[sid]}有人且偏熱（{temps[sid]:.1f}°C），可到平面圖用遙控器調低冷氣，或改用效能模式。"))
    return tips[:4] or [("✅", "目前沒有需要注意的事項。")]


def render(ctx: AppContext) -> None:
    r, h = ctx.selected, ctx.hour
    hourly = r.hourly.set_index("hour")
    base = ctx.baseline()
    period, price = hourly.loc[h, "period_label"], float(hourly.loc[h, "price"])
    page_header("HOME · 家中此刻", ctx.house.name, [
        (f"📅 <b>{ctx.day}</b> {ctx.weekday}・{ctx.day_type}", ""),
        (f"🕑 模擬時間 <b>{h:02d}:00</b>", ""),
        ("📍 臺中市", ""),
        (f"🌡️ 室外 <b>{hourly.loc[h, 'temp_c']:.1f}°C</b>・{hourly.loc[h, 'rh_pct']:.0f}%", ""),
        (f"⚡ {period} <b>{price:.2f}</b> 元/度", "peak" if period in ("尖峰", "半尖峰") else "off"),
    ])

    people = ctx.people_home()
    ac_rooms = ctx.ac_rooms_on()
    now_kw = float(hourly.loc[h, "kwh"])
    kpi_cards([
        {"label": "此刻用電", "value": f"{now_kw:.2f}", "unit": "kW", "live": True,
         "sub": f"這一小時約 {now_kw * price:.1f} 元（{period}）"},
        {"label": "今日用電", "value": f"{r.total_kwh:.1f}", "unit": "度",
         "sub": f"{delta_badge(r.total_kwh, base.total_kwh)}一般習慣 {base.total_kwh:.1f} 度"},
        {"label": "今日電費", "value": f"{r.energy_cost:.0f}", "unit": "元",
         "sub": f"{delta_badge(r.energy_cost, base.energy_cost)}一般習慣 {base.energy_cost:.0f} 元"},
        {"label": "在家", "value": f"{people:g}", "unit": "人",
         "sub": f"冷氣運轉 {len(ac_rooms)} 間：{'、'.join(ac_rooms)}" if ac_rooms else "冷氣未開"},
    ])

    left, right = st.columns([11, 9], gap="large")
    with left:
        section("即時平面圖")
        floorplan(make_payload(ctx), key="home_plan", compact=True)
        st.page_link(ctx.pages["plan"], label="進入平面圖：拖曳家電、遙控器操作", icon="🗺️")
    with right:
        section("今日用電曲線")
        st.altair_chart(charts.today_curve(r, base, h), width="stretch")
        st.markdown('<div class="hems-legend"><span><i style="background:linear-gradient(90deg,#3ba7ff,#22d3ee)"></i>'
                    f'{ctx.mode_schedule.name}</span><span><i style="background:#8a94a6"></i>一般習慣（虛線）</span>'
                    '<span><i style="background:rgba(138,138,138,.35)"></i>尖峰時段</span></div>', unsafe_allow_html=True)
        section("今日建議")
        tips_card(today_tips(ctx, base))

    st.write("")
    with st.expander("📊 和一般習慣比較"):
        m_now, m_base = ctx.metrics(r, "selected"), ctx.metrics(base, "baseline")
        compare_bars([
            {"name": "用電", "base": m_base["用電(kWh)"], "now": m_now["用電(kWh)"], "unit": "度"},
            {"name": "流動電費", "base": m_base["流動電費(元)"], "now": m_now["流動電費(元)"], "unit": "元", "fmt": "{:.0f}"},
            {"name": "尖峰占比", "base": m_base["尖峰占比(%)"], "now": m_now["尖峰占比(%)"], "unit": "%", "fmt": "{:.0f}"},
            {"name": "最大需量", "base": m_base["最大需量(kW)"], "now": m_now["最大需量(kW)"], "unit": "kW", "fmt": "{:.2f}"},
            {"name": "舒適時間", "base": m_base["舒適時間比例(%)"], "now": m_now["舒適時間比例(%)"], "unit": "%",
             "fmt": "{:.0f}", "lower_is_better": False},
            {"name": "碳排放", "base": m_base["碳排放(kg)"], "now": m_now["碳排放(kg)"], "unit": "kg"},
        ])
        st.caption(f"目前模式：{ctx.mode_schedule.name}；一般習慣：有人就開冷氣、整天 "
                   f"{ctx.analyzer.opt.baseline_plan.default_setpoint:g}°C、洗衣與熱水在晚間習慣時段。")
    with st.expander("🏠 各房間用電"):
        st.altair_chart(charts.space_breakdown(r), width="stretch")
    with st.expander("🔌 耗電最多的家電"):
        top = r.by_appliance().sort_values("kwh", ascending=False).head(8)
        top["占比"] = top["kwh"] / r.total_kwh
        st.dataframe(top.rename(columns={"space": "空間", "appliance": "家電", "kwh": "kWh"}), hide_index=True,
                     width="stretch",
                     column_config={"kWh": st.column_config.NumberColumn(format="%.2f"),
                                    "占比": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)})

    if st.session_state.get("_now_hint"):
        st.caption(st.session_state.pop("_now_hint"))


def back_to_now() -> None:
    """「回到現在」：模擬時間設為現在的小時（天氣資料只到 2026 年 9 月，日期維持目前選擇）。"""
    st.session_state.ui["hour"] = datetime.now().hour
    st.session_state["_now_hint"] = "已回到現在的時刻。目前天氣資料只到 2026 年 9 月，接上即時天氣後日期也會同步到今天。"
