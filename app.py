"""HEMS 居家能源管理系統：Streamlit 前端。

執行：streamlit run app.py

版面：
- 側邊欄（快速設定）：日期、電價方案、模式排程、舒適偏好
- 分頁（結果區）：總覽、模式比較、家電排程、室內環境、整月分析與建議
- 分頁（設定區）：客製化房型、家電功率、作息時間
所有計算都呼叫 core/ 的模組；設定以 JSON 字串為 key 快取，相同設定不重算。
"""
from __future__ import annotations

import copy
import json
from datetime import date, time

import pandas as pd
import streamlit as st

from core.data_source import CwaCsvWeatherSource, load_representative_days
from core.metrics import Analyzer
from core.models import load_space_templates
from core.optimizer import MODE_ORDER, ModeSchedule
from core.simulator import SimulationResult
from ui import charts
from ui.settings import (ROUTINE_LABELS, apply_routine_times, apply_spaces_table, build_analyzer, build_house,
                         default_settings, routine_times, spaces_table, to_key)

st.set_page_config(page_title="HEMS 居家能源管理模擬", page_icon="⚡", layout="wide")

BASELINE = "基準（一般習慣）"
WEEKDAY_NAMES = "一二三四五六日"


# ---------- 快取 ----------

@st.cache_resource(max_entries=4, show_spinner="建立模型中…")
def get_analyzer(settings_key: str) -> Analyzer:
    return build_analyzer(json.loads(settings_key))


@st.cache_data(max_entries=16, show_spinner="四種模式最佳化中…")
def compare_day(_analyzer: Analyzer, settings_key: str, day: date, plan_id: str):
    table, results = _analyzer.compare_day(day, plan_id)
    sims = {BASELINE if k == "基準" else _analyzer.opt.modes[k].name: (r if isinstance(r, SimulationResult) else r.result)
            for k, r in results.items()}
    return table, sims


@st.cache_data(max_entries=16, show_spinner="依模式排程最佳化中…")
def run_schedule(_analyzer: Analyzer, settings_key: str, day: date, plan_id: str,
                 by_hour: tuple[str, ...], name: str):
    result = _analyzer.opt.optimize(day, ModeSchedule(list(by_hour), name), plan_id).result
    return result, _analyzer.day_metrics(result)


@st.cache_data(max_entries=4, show_spinner="整月分析中（每天各模式都要最佳化，約 15 秒）…")
def analyze_month(_analyzer: Analyzer, settings_key: str, year: int, month: int, plan_id: str):
    table, scenarios = _analyzer.compare_month(year, month, plan_id)
    recs = _analyzer.recommendations(year, month, plan_id, table, scenarios)
    report = _analyzer.report_markdown(year, month, plan_id, table, recs)
    return table, recs, report


@st.cache_data
def available_dates() -> list[date]:
    return CwaCsvWeatherSource().available_dates()


# ---------- 設定狀態 ----------

if "settings" not in st.session_state:
    st.session_state.settings = default_settings()
settings: dict = st.session_state.settings


def update_settings(new: dict) -> None:
    st.session_state.settings = new
    st.rerun()


# ---------- 側邊欄：快速設定 ----------

dates = available_dates()
rep = load_representative_days()
with st.sidebar:
    st.header("⚡ 快速設定")
    preset = st.segmented_control("快速選擇", ["夏季代表日", "冬季代表日", "自選日期"], default="夏季代表日")
    if preset == "夏季代表日":
        day = rep["summer"]
    elif preset == "冬季代表日":
        day = rep["winter"]
    else:
        day = st.date_input("日期", value=rep["summer"], min_value=dates[0], max_value=dates[-1])

    try:
        analyzer = get_analyzer(to_key(settings))
    except (ValueError, KeyError) as e:
        st.error(f"設定有誤：{e}")
        if st.button("恢復預設設定"):
            update_settings(default_settings())
        st.stop()
    tariff = analyzer.sim.tariff
    day_type = "假日" if tariff.is_off_peak_day(day) else "平日"
    season = "夏月" if tariff.is_summer(day) else "非夏月"
    st.caption(f"{day}（週{WEEKDAY_NAMES[day.weekday()]}，{day_type}、電價{season}）")

    plan_names = {pid: p["name"] for pid, p in tariff.plans.items()}
    plan_id = st.selectbox("電價方案", list(plan_names), format_func=plan_names.get)

    st.subheader("模式排程")
    modes = analyzer.opt.modes
    mode_ids = MODE_ORDER
    schedule_type = st.radio("排程方式", ["整天同一模式", "白天／夜間不同模式"], horizontal=True,
                             label_visibility="collapsed")
    if schedule_type == "整天同一模式":
        chosen = st.selectbox("模式", mode_ids, index=1, format_func=lambda m: modes[m].name)
        mode_schedule = ModeSchedule.single(modes[chosen])
    else:
        day_mode = st.selectbox("白天模式", mode_ids, index=1, format_func=lambda m: modes[m].name)
        night_mode = st.selectbox("夜間模式", mode_ids, index=3, format_func=lambda m: modes[m].name)
        day_start, day_end = st.select_slider(
            "白天時段", options=list(range(24)), value=(7, 23),
            format_func=lambda h: f"{h:02d}:00", help="其餘時間為夜間，例如 07:00–23:00 表示 23:00 自動切換夜間模式")
        mode_schedule = ModeSchedule.switch(modes, day_mode, night_mode, night_start=day_end, night_end=day_start)
    st.caption(modes[mode_schedule.by_hour[12]].description)

    st.subheader("舒適偏好")
    comfort = settings["modes"]["comfort"]["seasons"]
    c1, c2 = st.columns(2)
    awake = c1.number_input("夏季清醒 °C", 22.0, 30.0, float(comfort["cooling"]["preferred_awake_c"]), 0.5)
    asleep = c2.number_input("夏季睡眠 °C", 22.0, 30.0, float(comfort["cooling"]["preferred_sleep_c"]), 0.5)
    if (awake, asleep) != (comfort["cooling"]["preferred_awake_c"], comfort["cooling"]["preferred_sleep_c"]):
        new = copy.deepcopy(settings)
        new["modes"]["comfort"]["seasons"]["cooling"].update(preferred_awake_c=awake, preferred_sleep_c=asleep)
        update_settings(new)

key = to_key(settings)
table, sims = compare_day(analyzer, key, day, plan_id)
selected, selected_metrics = run_schedule(analyzer, key, day, plan_id, tuple(mode_schedule.by_hour),
                                          mode_schedule.name)
baseline_metrics = table.iloc[0]

st.title("居家能源管理系統（HEMS）模擬與最佳化")
st.caption(f"{analyzer.sim.house.name}｜{len(analyzer.sim.schedule.members)} 人｜"
           f"天氣：中央氣象署臺中測站逐時觀測｜電價：台電 {tariff.source['effective_date']} 起實施費率")

tabs = st.tabs(["📊 總覽", "⚖️ 模式比較", "🗓️ 家電排程", "🌡️ 室內環境", "📅 整月分析與建議", "⚙️ 客製化設定", "📖 方法說明"])

# ---------- 總覽 ----------

with tabs[0]:
    st.subheader(f"{mode_schedule.name} vs. {BASELINE}")
    kpis = [("用電", "用電(kWh)", "{:.1f} kWh", "inverse"), ("流動電費", "流動電費(元)", "{:.0f} 元", "inverse"),
            ("尖峰占比", "尖峰占比(%)", "{:.0f}%", "inverse"), ("舒適時間", "舒適時間比例(%)", "{:.0f}%", "normal"),
            ("平均 PPD", "平均PPD(%)", "{:.1f}%", "inverse"), ("碳排放", "碳排放(kg)", "{:.1f} kg", "inverse")]
    for col, (label, field, fmt, delta_color) in zip(st.columns(len(kpis)), kpis):
        value, base = selected_metrics[field], baseline_metrics[field]
        col.metric(label, fmt.format(value), f"{value - base:+.1f}", delta_color=delta_color,
                   help=f"基準：{fmt.format(base)}")

    view = st.segmented_control("顯示", [mode_schedule.name, BASELINE], default=mode_schedule.name,
                                key="overview_view")
    shown = selected if view != BASELINE else sims[BASELINE]
    st.markdown(f"**全屋逐時用電**（灰底為尖峰時段）｜共 {shown.total_kwh:.1f} kWh、流動電費 {shown.energy_cost:.0f} 元")
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

# ---------- 模式比較 ----------

with tabs[1]:
    st.subheader(f"{day} 四種模式比較")
    st.caption("每個模式都是同一套最佳化（電費 + 舒適權重 × 不舒適度）搭配不同參數；基準為一般家庭習慣。")
    st.altair_chart(charts.mode_lines(sims), width="stretch")
    st.altair_chart(charts.metric_bars(table, [
        ("流動電費(元)", "流動電費（元）"), ("用電(kWh)", "用電（kWh）"), ("尖峰占比(%)", "尖峰占比（%）"),
        ("最大需量(kW)", "最大需量（kW）"), ("舒適時間比例(%)", "舒適時間比例（%）"),
        ("平均PPD(%)", "平均 PPD（%，越低越好）")]))
    st.markdown("**完整指標**")
    st.dataframe(table.round(2), hide_index=True, width="stretch")
    st.download_button("下載比較表 CSV", table.to_csv(index=False).encode("utf-8-sig"),
                       f"modes_{day}.csv", "text/csv")

# ---------- 家電排程 ----------

scenario_options = {mode_schedule.name: selected, **{n: r for n, r in sims.items() if n != mode_schedule.name}}

with tabs[2]:
    scenario = st.selectbox("情境", list(scenario_options), key="gantt_scenario")
    result = scenario_options[scenario]
    st.markdown("**家電排程甘特圖**：每格代表該小時有用電，顏色深淺代表用電量")
    st.altair_chart(charts.appliance_gantt(result), width="stretch")
    left, right = st.columns(2)
    with left:
        st.markdown("**可移轉負載**")
        base_tasks = {t["appliance"]: t for t in sims[BASELINE].tasks}
        st.dataframe(pd.DataFrame([{
            "家電": t["name"], "習慣時間": f"{base_tasks[t['appliance']]['start']:05.2f}" if t["appliance"] in base_tasks else "—",
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

# ---------- 室內環境 ----------

with tabs[3]:
    opt = analyzer.opt
    ac_spaces = {s.id: s.name for s in opt.ac_spaces}
    c1, c2 = st.columns(2)
    space_id = c1.selectbox("空間", list(ac_spaces), format_func=ac_spaces.get)
    scenario = c2.selectbox("情境", list(scenario_options), key="climate_scenario")
    result = scenario_options[scenario]
    season, prefs = opt.season(day)
    ctx = opt.contexts(result.day_type, season)[space_id]
    ideal = []
    for h in range(24):
        if ctx.awake[h] + ctx.asleep[h] <= 0:
            ideal.append(None)
        else:
            awake_ideal, sleep_ideal = opt._ideal(ctx, h, prefs)
            ideal.append(sleep_ideal if ctx.is_sleep[h] else awake_ideal)
    deadband = opt.comfort["evaluation_deadband_c"]
    st.markdown(f"**{ac_spaces[space_id]} 逐時溫度**：灰色帶為有人時的舒適範圍（偏好溫度 ± {deadband}°C，"
                "睡眠時依睡眠曲線逐步放寬）")
    st.altair_chart(charts.room_temperature(result, space_id, ideal, deadband), width="stretch")
    m = analyzer.day_metrics(result)
    c = st.columns(4)
    c[0].metric("舒適時間比例", f"{m['舒適時間比例(%)']:.0f}%")
    c[1].metric("平均溫差", f"{m['平均溫差(°C)']:.2f}°C")
    c[2].metric("PMV 舒適比例", f"{m['PMV舒適比例(%)']:.0f}%", help="ISO 7730：|PMV| ≤ 0.5 的清醒人·時比例")
    c[3].metric("平均 PPD", f"{m['平均PPD(%)']:.1f}%", help="預測不滿意百分比，最低 5%")
    st.caption("以上舒適指標為全屋有冷氣空間的合計。冬季系統沒有暖氣，室溫主要受天氣影響。")

# ---------- 整月分析與建議 ----------

with tabs[4]:
    months = sorted({(d.year, d.month) for d in dates})
    c1, c2 = st.columns([1, 3])
    ym = c1.selectbox("月份", months, index=months.index((day.year, day.month)),
                      format_func=lambda x: f"{x[0]} 年 {x[1]} 月")
    c2.caption("整月分析會對每一天、每個模式各做一次最佳化，並比較三種電價方案的月電費。")
    if st.button("執行整月分析", type="primary"):
        st.session_state.month_request = (ym, plan_id, key)
    request = st.session_state.get("month_request")
    if request and request[2] == key:
        (year, month), req_plan, _ = request
        mtable, recs, report = analyze_month(analyzer, key, year, month, req_plan)
        plan_col = f"{tariff.plans[req_plan]['name']}(元)"
        st.altair_chart(charts.metric_bars(mtable, [
            (plan_col, "月電費（元）"), ("月用電(kWh)", "月用電（kWh）"), ("碳排放(kg)", "碳排放（kg CO2e）"),
            ("最大需量(kW)", "最大需量（kW）"), ("平均舒適時間比例(%)", "舒適時間比例（%）"),
            ("平均PPD(%)", "平均 PPD（%）")]))
        st.dataframe(mtable.round(1), hide_index=True, width="stretch")
        st.subheader("節省建議")
        for i, r in enumerate(recs):
            with st.container(border=True):
                st.markdown(f"**{i + 1}. {r.headline()}**")
                st.write(r.detail)
                if r.note:
                    st.caption(f"※ {r.note}")
        st.download_button("下載分析報告（Markdown）", report.encode("utf-8"), f"report_{year}-{month:02d}.md",
                           "text/markdown")

# ---------- 客製化設定 ----------

with tabs[5]:
    st.subheader("客製化設定")
    st.caption("修改後按「套用」，所有結果會依新設定重新計算。設定只存在這次瀏覽，不會改到預設檔案。")
    templates = load_space_templates()

    with st.expander("🏠 房型：空間範本與面積", expanded=True):
        st.caption("可新增或刪除列。新增的空間依範本帶入預設家電；家庭成員使用的臥室、浴室若被刪除會提示錯誤。")
        edited = st.data_editor(
            pd.DataFrame(spaces_table(settings)), num_rows="dynamic", hide_index=True, key="spaces_editor",
            column_config={
                "id": st.column_config.TextColumn("代碼", disabled=True, help="系統自動產生"),
                "範本": st.column_config.SelectboxColumn(options=list(templates), required=True,
                                                       help="、".join(f"{k}={v['name']}" for k, v in templates.items())),
                "坪數": st.column_config.NumberColumn(min_value=0.0, max_value=50.0, step=0.5),
            })
        if st.button("套用房型"):
            update_settings(apply_spaces_table(settings, edited.to_dict("records")))

    with st.expander("🔌 家電功率與啟用"):
        house = build_house(settings)
        rows = [{"key": f"{s.id}/{a.id}", "空間": s.name, "家電": a.name, "類型": a.load_type.label,
                 "功率(W)": a.power_w, "啟用": a.enabled} for s, a in house.all_appliances()]
        edited_app = st.data_editor(pd.DataFrame(rows), hide_index=True, key="appliance_editor",
                                    disabled=["key", "空間", "家電", "類型"], column_order=["空間", "家電", "類型", "功率(W)", "啟用"],
                                    column_config={"功率(W)": st.column_config.NumberColumn(min_value=0, step=10)})
        st.caption("冷氣的功率為額定耗電，冷房能力 = 功率 × 額定 COP。熱水器改用瓦斯時，取消勾選電熱水器。")
        if st.button("套用家電設定"):
            new = copy.deepcopy(settings)
            new["appliance_edits"] = {r["key"]: {"power_w": r["功率(W)"], "enabled": bool(r["啟用"])}
                                      for r in edited_app.to_dict("records")}
            update_settings(new)

    with st.expander("🕒 作息時間"):
        st.caption("調整起床、就寢、外出時間；盥洗、三餐、洗澡等時段沿用預設。")

        def to_time(text: str | None, fallback: str) -> time:
            h, m = map(int, (text or fallback).replace("24:00", "23:59").split(":"))
            return time(h, m)

        with st.form("routine_form"):
            new = copy.deepcopy(settings)
            for rid, label in ROUTINE_LABELS.items():
                t = routine_times(settings["schedule"]["routines"][rid])
                st.markdown(f"**{label}**")
                c = st.columns(4)
                wake = c[0].time_input("起床", to_time(t["wake"], "07:00"), key=f"{rid}_wake", step=900)
                bed = c[1].time_input("就寢", to_time(t["bed"], "23:00"), key=f"{rid}_bed", step=900)
                out_s = c[2].time_input("外出", to_time(t["out_start"], "08:00"), key=f"{rid}_out_s", step=900)
                out_e = c[3].time_input("回家", to_time(t["out_end"], "18:00"), key=f"{rid}_out_e", step=900)
                new = apply_routine_times(new, rid, {"wake": wake.strftime("%H:%M"), "bed": bed.strftime("%H:%M"),
                                                     "out_start": out_s.strftime("%H:%M"),
                                                     "out_end": out_e.strftime("%H:%M")})
            st.markdown("**成員的臥室與浴室**")
            space_names = {s["id"]: s.get("name", s["id"]) for s in settings["house"]["spaces"]}
            for i, member in enumerate(new["schedule"]["members"]):
                c = st.columns(3)
                c[0].markdown(f"{member['name']}")
                member["bedroom"] = c[1].selectbox("臥室", list(space_names), index=list(space_names).index(member["bedroom"])
                                                   if member["bedroom"] in space_names else 0,
                                                   format_func=space_names.get, key=f"m{i}_bed")
                member["bathroom"] = c[2].selectbox("浴室", list(space_names), index=list(space_names).index(member["bathroom"])
                                                    if member["bathroom"] in space_names else 0,
                                                    format_func=space_names.get, key=f"m{i}_bath")
            if st.form_submit_button("套用作息"):
                update_settings(new)

    with st.expander("📏 比較基準"):
        base_sp = st.number_input("一般習慣的冷氣設定溫度（°C）", 20.0, 30.0, float(settings["baseline_setpoint_c"]), 0.5,
                                  help="基準情境：有人就開冷氣，整天固定這個溫度；可移轉負載在晚間習慣時段運轉。")
        if base_sp != settings["baseline_setpoint_c"]:
            new = copy.deepcopy(settings)
            new["baseline_setpoint_c"] = base_sp
            update_settings(new)

    if st.button("恢復全部預設設定"):
        update_settings(default_settings())

# ---------- 方法說明 ----------

with tabs[6]:
    st.markdown("""
### 系統架構（五層）
1. **輸入層**：房型（空間範本 × 數量）、家庭作息、中央氣象署逐時天氣、台電電價
2. **模擬層**：依作息與天氣推演每個空間每小時的冷房負載與家電耗電
3. **最佳化層**：四種模式＝同一套最佳化（電費 + 舒適權重 × 不舒適度）＋不同參數
4. **分析層**：用電、電費、尖峰、需量、碳排、舒適度（溫度範圍、ISO 7730 PMV/PPD）、節省建議
5. **介面層**：本頁面

### 冷房負載與冷氣耗電
- 冷房負載 = 外牆（等效日射溫度 + 時滯衰減）+ 窗戶傳熱與日射 + 人體顯潛熱 + 家電發熱 + 外氣滲透顯潛熱 + 蓄熱移除
- 冷氣耗電 = 冷房負載 ÷ COP；COP 隨室外溫度、設定溫度、部分負載率（變頻與啟停）修正
- 冷氣沒開時，室溫以一階熱響應（1R1C）計算

### 最佳化
- **冷氣設定溫度**：座標下降法，每次以負載模型推演整天評估（室溫有蓄熱，前後小時互相影響）
- **可移轉負載**：熱水器 × 洗衣機 × 烘衣機開始時間聯合窮舉（全域最佳），
  限制：洗澡前燒好熱水、烘衣接續洗衣、睡眠時段安靜、需量懲罰讓大功率家電錯開

### 資料來源
- 天氣：中央氣象署 CODiS 臺中測站（467490）逐時觀測，2026 年 1–9 月
- 電價：台電「各類電價表及計算範例」（114 年 10 月 1 日起實施）
- 人體發熱：ASHRAE Fundamentals；熱舒適：ISO 7730；碳排係數：經濟部能源署 113 年度電力排碳係數

### 限制
- 系統只模擬冷氣，沒有暖氣；冬季舒適度主要受天氣影響
- 各房間獨立計算，未考慮房間之間的熱交換
- 參數為台灣常見 RC 公寓典型值，可用智慧插座／電表資料依 ASHRAE Guideline 14 校正
""")
