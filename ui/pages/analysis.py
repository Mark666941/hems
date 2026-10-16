"""📊 分析：今日明細、模式比較、家電排程、室內環境、整月分析與建議、用電異常偵測。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ui import anomaly, charts, insights
from ui.context import BASELINE, AppContext, analyze_month, meter_comparison
from ui.theme import page_header, tips_card

METER_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "meter"


def render(ctx: AppContext) -> None:
    page_header("ANALYSIS · 分析", "用電明細與模式比較", [(f"📅 <b>{ctx.day}</b> {ctx.weekday}", ""),
                                                   (f"🎛️ {ctx.mode_schedule.name}", "")])
    tabs = st.tabs(["今日明細", "模式比較", "家電排程", "室內環境", "整月分析與建議", "異常偵測"])
    with tabs[0]:
        _today(ctx)
    with tabs[1]:
        _modes(ctx)
    with tabs[2]:
        _schedule(ctx)
    with tabs[3]:
        _climate(ctx)
    with tabs[4]:
        _month(ctx)
    with tabs[5]:
        _anomaly(ctx)


def _scenarios(ctx: AppContext) -> dict:
    _, sims = ctx.compare()
    return {ctx.mode_schedule.name: ctx.selected, **{n: r for n, r in sims.items() if n != ctx.mode_schedule.name}}


def _today(ctx: AppContext) -> None:
    view = st.segmented_control("顯示", [ctx.mode_schedule.name, BASELINE], default=ctx.mode_schedule.name,
                                key="today_view")
    shown = ctx.baseline() if view == BASELINE else ctx.selected
    st.markdown(f"**全屋逐時用電**（灰底為尖峰時段）｜共 {shown.total_kwh:.1f} 度、流動電費 {shown.energy_cost:.0f} 元")
    st.altair_chart(charts.hourly_stacked(shown), width="stretch")
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**各空間用電**（可移轉家電計入公共設備）")
        st.altair_chart(charts.space_breakdown(shown), width="stretch")
    with right:
        st.markdown("**用電最多的家電**")
        top = shown.by_appliance().sort_values("kwh", ascending=False).head(8)
        top["占比"] = top["kwh"] / shown.total_kwh
        st.dataframe(top.rename(columns={"space": "空間", "appliance": "家電", "kwh": "kWh"}), hide_index=True,
                     column_config={"kWh": st.column_config.NumberColumn(format="%.2f"),
                                    "占比": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)})


def _modes(ctx: AppContext) -> None:
    table, sims = ctx.compare()
    st.caption("每個模式都是同一套最佳化（電費 + 舒適權重 × 不舒適度）搭配不同參數；基準為一般家庭習慣。")
    st.altair_chart(charts.mode_lines(sims), width="stretch")
    st.altair_chart(charts.metric_bars(table, [
        ("流動電費(元)", "流動電費（元）"), ("用電(kWh)", "用電（kWh）"), ("尖峰占比(%)", "尖峰占比（%）"),
        ("最大需量(kW)", "最大需量（kW）"), ("舒適時間比例(%)", "舒適時間比例（%）"),
        ("平均PPD(%)", "平均 PPD（%，越低越好）")]))
    with st.expander("完整指標表"):
        st.dataframe(table.round(2), hide_index=True, width="stretch")
        st.download_button("下載比較表 CSV", table.to_csv(index=False).encode("utf-8-sig"),
                           f"modes_{ctx.day}.csv", "text/csv")


def _schedule(ctx: AppContext) -> None:
    options = _scenarios(ctx)
    scenario = st.selectbox("情境", list(options), key="gantt_scenario")
    result = options[scenario]
    st.markdown("**家電排程甘特圖**：每格代表該小時有用電，顏色深淺代表用電量")
    st.altair_chart(charts.appliance_gantt(result), width="stretch")
    left, right = st.columns(2)
    with left:
        st.markdown("**可移轉負載**")
        base_tasks = {t["appliance"]: t for t in ctx.baseline().tasks}
        st.dataframe(pd.DataFrame([{
            "家電": t["name"],
            "習慣時間": f"{base_tasks[t['appliance']]['start']:05.2f}" if t["appliance"] in base_tasks else "—",
            "排程時間": f"{t['start']:05.2f}", "運轉(h)": round(t["duration_h"], 2), "耗電(kWh)": round(t["energy_kwh"], 2),
        } for t in result.tasks]), hide_index=True, width="stretch")
        st.caption("時間以小時表示，例如 07.50 = 07:30。電熱水器提早燒會多出儲水桶散熱損失。")
    with right:
        st.markdown("**冷氣設定溫度（°C）**")
        climate = result.climate[result.climate["ac_on"] > 0]
        if climate.empty:
            st.info("這一天沒有開冷氣（室外涼爽時開窗通風）。")
        else:
            pivot = climate.pivot_table(index="hour", columns="space", values="setpoint_c").round(1)
            pivot.index = [f"{h:02d}:00" for h in pivot.index]
            st.dataframe(pivot, width="stretch")


def _climate(ctx: AppContext) -> None:
    opt = ctx.analyzer.opt
    options = _scenarios(ctx)
    ac_spaces = {s.id: s.name for s in opt.ac_spaces}
    c1, c2 = st.columns(2)
    space_id = c1.selectbox("空間", list(ac_spaces), format_func=ac_spaces.get)
    scenario = c2.selectbox("情境", list(options), key="climate_scenario")
    result = options[scenario]
    season, prefs = opt.season(ctx.day)
    room = opt.contexts(result.day_type, season)[space_id]
    ideal = []
    for h in range(24):
        if room.awake[h] + room.asleep[h] <= 0:
            ideal.append(None)
        else:
            awake_ideal, sleep_ideal = opt._ideal(room, h, prefs)
            ideal.append(sleep_ideal if room.is_sleep[h] else awake_ideal)
    deadband = opt.comfort["evaluation_deadband_c"]
    st.markdown(f"**{ac_spaces[space_id]} 逐時溫度**：灰色帶為有人時的舒適範圍（偏好溫度 ± {deadband}°C，"
                "睡眠時依睡眠曲線逐步放寬）")
    st.altair_chart(charts.room_temperature(result, space_id, ideal, deadband), width="stretch")
    m = ctx.analyzer.day_metrics(result)
    c = st.columns(4)
    c[0].metric("舒適時間比例", f"{m['舒適時間比例(%)']:.0f}%")
    c[1].metric("平均溫差", f"{m['平均溫差(°C)']:.2f}°C")
    c[2].metric("PMV 舒適比例", f"{m['PMV舒適比例(%)']:.0f}%", help="ISO 7730：|PMV| ≤ 0.5 的清醒人·時比例")
    c[3].metric("平均 PPD", f"{m['平均PPD(%)']:.1f}%", help="預測不滿意百分比，最低 5%")
    st.caption("以上舒適指標為全屋有冷氣空間的合計。冬季系統沒有暖氣，室溫主要受天氣影響。")


def _anomaly(ctx: AppContext) -> None:
    files = sorted(METER_DIR.glob("*.csv"))
    st.caption("把智慧插座或電表的實測用電和模擬（一般習慣）的預測逐時比較，誤差超出統計門檻的時段標為異常。"
               "門檻用穩健統計（中位數與 MAD），模型整體偏高或偏低不會被當成異常，只抓「突然不一樣」的時段。")
    if not files:
        st.info("data/meter/ 還沒有電表資料（格式見 data/meter/README.md）。", icon="🔌")
        return
    c1, c2 = st.columns([2, 1])
    path = c1.selectbox("電表資料", files, format_func=lambda p: p.name, key="meter_file")
    k = c2.slider("單一小時門檻（σ）", 2.0, 5.0, 3.0, 0.5, key="anomaly_k",
                  help="誤差偏離中位數超過幾個標準差算異常；越小越敏感、誤報也越多")
    if "synthetic" in path.name:
        st.warning("這是示範用合成資料（模擬值加上冷氣 +15%、熱水器時間飄移、待機 30 W 與隨機波動），不是實測資料。",
                   icon="🧪")
    detail = meter_comparison(ctx.analyzer, ctx.key, str(path))
    if detail.empty:
        st.info("電表資料的日期沒有對應的天氣資料，無法比對。")
        return
    names = anomaly.channel_names(ctx.house)
    scored, found = anomaly.detect(detail, k=k, names=names)
    counts = found["類型"].value_counts()
    c = st.columns(4)
    c[0].metric("比對天數", f"{detail['date'].nunique()} 天")
    c[1].metric("單一小時異常", f"{counts.get('單一小時', 0)} 筆")
    c[2].metric("持續偏離", f"{counts.get('持續偏離', 0)} 段")
    c[3].metric("整天偏離", f"{counts.get('整天偏離', 0)} 天")

    channels = list(dict.fromkeys(scored["channel"]))
    channel = st.selectbox("通道", channels, format_func=lambda ch: names.get(ch, ch), key="anomaly_channel")
    part = scored[scored["channel"] == channel]
    bias = float(part["center"].iloc[0])
    st.markdown(f"**{names.get(channel, channel)}：實測 − 模擬**（灰帶＝正常範圍 中位數 ± {k:g}σ，橘點＝異常）｜"
                f"系統性偏差 {bias:+.3f} kWh/時、σ = {float(part['scale'].iloc[0]):.3f} kWh")
    st.altair_chart(charts.anomaly_chart(part, k), width="stretch")
    if found.empty:
        st.success("沒有偵測到異常。", icon="✅")
    else:
        st.dataframe(found.drop(columns=["channel"]), hide_index=True, width="stretch", column_config={
            "實測(kWh)": st.column_config.NumberColumn(format="%.2f"),
            "模擬(kWh)": st.column_config.NumberColumn(format="%.2f"),
            "差異(kWh)": st.column_config.NumberColumn(format="%+.2f"),
            "偏離(σ)": st.column_config.NumberColumn(format="%+.1f")})
    if abs(bias) > 0.05:
        st.caption(f"這個通道的實測整體{'高' if bias > 0 else '低'}於模擬約 {abs(bias):.2f} kWh/時，屬於模型的系統性偏差，"
                   "可以用「預測」頁的帳單修正或調整家電功率校正，不算異常。")


def _month(ctx: AppContext) -> None:
    months = sorted({(d.year, d.month) for d in ctx.dates})
    c1, c2 = st.columns([1, 3])
    ym = c1.selectbox("月份", months, index=months.index((ctx.day.year, ctx.day.month)),
                      format_func=lambda x: f"{x[0]} 年 {x[1]} 月")
    c2.caption("整月分析會對每一天、每個模式各做一次最佳化，畫出每日用電、累積電費、溫度與冷氣用電的關係，"
               "並比較三種電價方案的月電費。")
    if st.button("執行整月分析", type="primary"):
        st.session_state.month_request = (ym, ctx.plan_id, ctx.key)
    request = st.session_state.get("month_request")
    if not (request and request[2] == ctx.key):
        return
    (year, month), req_plan, _ = request
    mtable, recs, report, daily = analyze_month(ctx.analyzer, ctx.key, year, month, req_plan)
    plan_col = f"{ctx.tariff.plans[req_plan]['name']}(元)"

    st.markdown(f"**各模式每日用電**｜{year} 年 {month} 月（滑鼠移到點上看當天日均溫）")
    st.altair_chart(charts.month_daily_lines(daily), width="stretch")
    left, right = st.columns(2)
    with left:
        st.markdown("**累積流動電費**（不含基本電費）")
        st.altair_chart(charts.month_daily_lines(daily, "cum_cost", "累積電費（元）"), width="stretch")
    with right:
        st.markdown("**室外溫度與冷氣用電**（每點一天，虛線為迴歸線）")
        st.altair_chart(charts.temp_vs_ac(daily), width="stretch")

    # 數據分析式建議：和目前選的模式比較；目前是白天／夜間切換時，用月電費最低的模式
    modes = mtable.iloc[1:]
    focus = (ctx.mode_schedule.name if ctx.mode_schedule.name in set(modes["模式"])
             else modes.sort_values(plan_col).iloc[0]["模式"])
    base_row = mtable.iloc[0]
    price = base_row[plan_col] / base_row["月用電(kWh)"] if base_row["月用電(kWh)"] else 0.0
    st.subheader("數據分析")
    found = insights.month_insights(daily, focus, price)
    tips_card([(i.icon, i.html()) for i in found] or [("✅", "這個月的用電沒有明顯的規律需要注意。")])

    with st.expander("各模式月指標比較"):
        st.altair_chart(charts.metric_bars(mtable, [
            (plan_col, "月電費（元）"), ("月用電(kWh)", "月用電（kWh）"), ("碳排放(kg)", "碳排放（kg CO2e）"),
            ("最大需量(kW)", "最大需量（kW）"), ("平均舒適時間比例(%)", "舒適時間比例（%）"),
            ("平均PPD(%)", "平均 PPD（%）")]))
    with st.expander("完整指標表"):
        st.dataframe(mtable.round(1), hide_index=True, width="stretch")
        st.download_button("下載每日資料 CSV", daily.to_csv(index=False).encode("utf-8-sig"),
                           f"daily_{year}-{month:02d}.csv", "text/csv")
    st.subheader("節省建議")
    for i, r in enumerate(recs):
        with st.container(border=True):
            st.markdown(f"**{i + 1}. {r.headline()}**")
            st.write(r.detail)
            if r.note:
                st.caption(f"※ {r.note}")
    st.download_button("下載分析報告（Markdown）", report.encode("utf-8"), f"report_{year}-{month:02d}.md",
                       "text/markdown")
