"""HEMS 居家能源管理系統：Streamlit 前端。

執行：streamlit run app.py（或 python run_app.py 開桌面視窗）

版面：
- 左側（可調整的選項）：日期與模擬時間、電價方案、模式排程、舒適偏好、格局與房間尺寸
- 右側分頁：
  🏠 智慧平面圖：房間尺寸標示、家電圖示（可拖曳）、即時用電、遙控器、時間地點溫度狀態列
  📊 總覽、⚖️ 模式比較、🗓️ 家電排程、🌡️ 室內環境、📅 整月分析與建議、⚙️ 進階設定、📖 方法說明
所有計算都呼叫 core/ 的模組；設定以 JSON 字串為 key 快取，相同設定不重算。
"""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, time

import pandas as pd
import streamlit as st

from core.data_source import CwaCsvWeatherSource, load_representative_days
from core.metrics import Analyzer
from core.models import load_space_templates
from core.optimizer import MODE_ORDER, ModeSchedule
from core.simulator import ControlPlan, SimulationResult
from ui import charts
from ui.floorplan import (ADDABLE, add_appliance, apply_remote, build_payload, default_spot, ensure_layout,
                          floorplan, move_appliance, move_room, remote_command, remove_appliance, room_dims,
                          set_room_size)
from ui.settings import (ROUTINE_LABELS, apply_routine_times, apply_spaces_table, build_analyzer, build_house,
                         default_settings, routine_times, sim_key, spaces_table)

st.set_page_config(page_title="HEMS 居家能源管理模擬", page_icon="⚡", layout="wide")

BASELINE = "基準（一般習慣）"
WEEKDAY_NAMES = "一二三四五六日"
# 左側選單：按鈕文字靠左，像清單一樣；摘要文字緊貼按鈕下方
SIDEBAR_CSS = """<style>
section[data-testid="stSidebar"] .stButton button { justify-content: flex-start; padding: 0.55rem 0.9rem;
  border-radius: 10px; font-weight: 600; }
section[data-testid="stSidebar"] .stButton button > div { justify-content: flex-start; width: 100%; text-align: left; }
section[data-testid="stSidebar"] .stButton button p { font-size: 0.95rem; }
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { margin: -0.5rem 0 0.4rem 0.9rem; }
</style>"""


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
                 by_hour: tuple[str, ...], name: str) -> tuple[ControlPlan, SimulationResult]:
    r = _analyzer.opt.optimize(day, ModeSchedule(list(by_hour), name), plan_id)
    return r.plan, r.result


@st.cache_data(max_entries=32, show_spinner=False)
def run_with_remote(_analyzer: Analyzer, _plan: ControlPlan, settings_key: str, day: date, plan_id: str,
                    by_hour: tuple[str, ...], remote_key: str):
    """最佳化結果 + 遙控器手動覆寫，重新推演一天（約 0.1 秒）。"""
    plan = apply_remote(_plan, json.loads(remote_key)) if remote_key != "{}" else _plan
    result = _analyzer.sim.run(day, plan_id, plan) if remote_key != "{}" else None
    return plan, result


@st.cache_data(max_entries=32, show_spinner=False)
def day_metrics(_analyzer: Analyzer, _result: SimulationResult, cache_key: str) -> dict:
    return _analyzer.day_metrics(_result)


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


def update_settings(new: dict, message: str | None = None) -> None:
    st.session_state.settings = new
    if message:
        st.session_state.flash = message      # 重新執行後再顯示提示
    st.rerun()


def room_comfort(analyzer: Analyzer, result: SimulationResult, hour: int) -> dict[str, str]:
    """各房間該小時的舒適狀態（ok／warm／cool）；沒人的房間不列入。"""
    opt = analyzer.opt
    season, prefs = opt.season(result.day)
    deadband = opt.comfort["evaluation_deadband_c"]
    climate = result.climate.set_index(["space_id", "hour"])["room_temp_c"]
    status = {}
    for sid, ctx in opt.contexts(result.day_type, season).items():
        if ctx.awake[hour] + ctx.asleep[hour] <= 0 or (sid, hour) not in climate.index:
            continue
        awake, asleep = opt._ideal(ctx, hour, prefs)
        ideal = asleep if ctx.is_sleep[hour] else awake
        diff = climate[(sid, hour)] - ideal
        status[sid] = "ok" if abs(diff) <= deadband else ("warm" if diff > 0 else "cool")
    return status


# ---------- 左側功能選單 ----------
# 每個功能只顯示「標題＋目前設定摘要」，點選後開啟該功能的設定視窗（st.dialog），按「套用」才生效。
# 介面選項存在 session_state.ui，關閉視窗後仍保留。

dates = available_dates()
rep = load_representative_days()
if "ui" not in st.session_state:
    st.session_state.ui = {
        "preset": "夏季代表日", "custom_day": rep["summer"], "hour": datetime.now().hour, "plan_id": "tou_2",
        "schedule_type": "single", "mode": "normal", "day_mode": "normal", "night_mode": "sleep",
        "day_start": 7, "day_end": 23,
    }
ui: dict = st.session_state.ui

try:
    analyzer = get_analyzer(sim_key(settings))
except (ValueError, KeyError) as e:
    st.error(f"設定有誤：{e}")
    if st.button("恢復預設設定"):
        update_settings(default_settings())
    st.stop()

tariff = analyzer.sim.tariff
modes = analyzer.opt.modes
templates = load_space_templates()
house = analyzer.sim.house
layout = ensure_layout(settings, house, templates)
cfg_by_id = {s["id"]: s for s in settings["house"]["spaces"]}

day = {"夏季代表日": rep["summer"], "冬季代表日": rep["winter"]}.get(ui["preset"], ui["custom_day"])
sim_hour = int(ui["hour"])
plan_id = ui["plan_id"]
day_type = "假日" if tariff.is_off_peak_day(day) else "平日"
if ui["schedule_type"] == "single":
    mode_schedule = ModeSchedule.single(modes[ui["mode"]])
else:
    mode_schedule = ModeSchedule.switch(modes, ui["day_mode"], ui["night_mode"],
                                        night_start=ui["day_end"], night_end=ui["day_start"])

PLAN_GUIDE = {
    "progressive": ("一般家庭預設的計費方式。不分時段，**每月用越多度、每度越貴**（分 6 級，夏月 1.78–8.86 元）。",
                    "晚上用電集中、無法把用電移到半夜的家庭（例如雙薪、晚上才開冷氣）。"),
    "tou_2": ("把一天分成**尖峰、離峰**兩段。夏月平日 9–24 時為尖峰（5.16 元），0–9 時與週末、國定假日為離峰（2.06 元）；"
              "每月另收基本費 75 元。",
              "白天有人在家用電、或能把熱水器、洗衣、烘衣移到半夜離峰的家庭。"),
    "tou_3": ("再把尖峰細分：夏月平日 **16–22 時最貴**（7.13 元），9–16 時與 22–24 時為半尖峰（4.69 元），其餘離峰（2.06 元）。",
              "傍晚到晚上用電很少的家庭；晚上開冷氣的家庭通常不划算。"),
}


@st.cache_data(max_entries=8, show_spinner="試算各電價方案的月電費…")
def month_bills(_analyzer: Analyzer, settings_key: str, year: int, month: int) -> pd.DataFrame:
    """以一般習慣（基準）推演整月，換算三種電價方案的月電費（約 1 秒）。"""
    return _analyzer.sim.run_month(year, month, "tou_2", _analyzer.opt.baseline_plan).bills()


def apply_ui(**changes) -> None:
    st.session_state.ui.update(changes)
    st.rerun()


@st.dialog("📅 日期與時間")
def dialog_datetime() -> None:
    preset = st.segmented_control("日期", ["夏季代表日", "冬季代表日", "自選日期"], default=ui["preset"])
    custom = ui["custom_day"]
    if preset == "自選日期":
        custom = st.date_input("選擇日期", value=ui["custom_day"], min_value=dates[0], max_value=dates[-1])
    st.caption("代表日：該月份中氣溫最接近月平均、沒有下雨的平日（中央氣象署臺中測站實測資料）。")
    hour = st.slider("模擬時間", 0, 23, value=sim_hour, format="%d:00",
                     help="平面圖顯示這個時刻的室溫與家電用電；遙控器操作從這個時刻起生效")
    if st.button("套用", type="primary", width="stretch"):
        apply_ui(preset=preset or ui["preset"], custom_day=custom, hour=hour)


@st.dialog("💰 電價方案", width="large")
def dialog_tariff() -> None:
    names = {pid: p["name"] for pid, p in tariff.plans.items()}
    choice = st.radio("選擇方案", list(names), index=list(names).index(plan_id), format_func=names.get,
                      horizontal=True)
    what, who = PLAN_GUIDE[choice]
    st.markdown(f"**怎麼計費**：{what}\n\n**適合誰**：{who}")
    prices = tariff.hourly_prices(day, choice)
    prices = prices.assign(時段=prices["period"].map(lambda p: p.label)).drop(columns="period")
    st.markdown(f"**{day}（{day_type}）的每小時電價**")
    st.altair_chart(charts.price_bars(prices), width="stretch")

    st.markdown(f"**哪個方案最適合我家？**（以目前房型與一般習慣試算 {day.year} 年 {day.month} 月）")
    bills = month_bills(analyzer, sim_key(settings), day.year, day.month)
    best = bills.iloc[0]["plan_id"]
    show = bills[["方案", "月電費", "平均每度"]].copy()
    show["方案"] = [("⭐ " if pid == best else "") + n for pid, n in zip(bills["plan_id"], show["方案"])]
    st.dataframe(show, hide_index=True, width="stretch",
                 column_config={"月電費": st.column_config.NumberColumn(format="%.0f 元"),
                                "平均每度": st.column_config.NumberColumn(format="%.2f 元")})
    st.caption("試算以一般習慣、未最佳化的用電為準；改用省電或正常模式後，時間電價的電費還會再下降。"
               "完整比較請看「整月分析與建議」。")
    if st.button("套用", type="primary", width="stretch"):
        apply_ui(plan_id=choice)


@st.dialog("🎛️ 運轉模式", width="large")
def dialog_mode() -> None:
    kind = st.radio("排程方式", ["single", "switch"], index=0 if ui["schedule_type"] == "single" else 1,
                    format_func={"single": "整天同一模式", "switch": "白天／夜間不同模式"}.get, horizontal=True)
    for mid in MODE_ORDER:
        m = modes[mid]
        lo, hi = m.params.setpoint_range_c
        st.markdown(f"**{m.name}**　<span style='color:gray'>冷氣 {lo:g}–{hi:g}°C</span>  \n{m.description}",
                    unsafe_allow_html=True)
    st.divider()
    if kind == "single":
        mode = st.selectbox("模式", MODE_ORDER, index=MODE_ORDER.index(ui["mode"]), format_func=lambda m: modes[m].name)
        changes = {"schedule_type": "single", "mode": mode}
    else:
        c1, c2 = st.columns(2)
        day_mode = c1.selectbox("白天模式", MODE_ORDER, index=MODE_ORDER.index(ui["day_mode"]),
                                format_func=lambda m: modes[m].name)
        night_mode = c2.selectbox("夜間模式", MODE_ORDER, index=MODE_ORDER.index(ui["night_mode"]),
                                  format_func=lambda m: modes[m].name)
        start, end = st.select_slider("白天時段", options=list(range(24)), value=(ui["day_start"], ui["day_end"]),
                                      format_func=lambda h: f"{h:02d}:00",
                                      help="其餘時間為夜間，例如 07:00–23:00 表示 23:00 自動切換夜間模式")
        changes = {"schedule_type": "switch", "day_mode": day_mode, "night_mode": night_mode,
                   "day_start": start, "day_end": end}
    if st.button("套用", type="primary", width="stretch"):
        apply_ui(**changes)


@st.dialog("🌡️ 舒適偏好")
def dialog_comfort() -> None:
    seasons = settings["modes"]["comfort"]["seasons"]
    st.caption("所有模式都會以這裡的溫度為目標做最佳化，也是比較舒適度的基準。")
    st.markdown("**夏季（5–10 月，短袖）**")
    c1, c2 = st.columns(2)
    s_awake = c1.number_input("清醒時 °C", 16.0, 30.0, float(seasons["cooling"]["preferred_awake_c"]), 0.5, format="%.1f")
    s_sleep = c2.number_input("睡眠時 °C", 16.0, 30.0, float(seasons["cooling"]["preferred_sleep_c"]), 0.5, format="%.1f")
    st.markdown("**冬季（11–4 月，長袖、睡覺蓋棉被）**")
    c1, c2 = st.columns(2)
    w_awake = c1.number_input("清醒時 °C ", 16.0, 30.0, float(seasons["heating"]["preferred_awake_c"]), 0.5, format="%.1f")
    w_sleep = c2.number_input("睡眠時 °C ", 16.0, 30.0, float(seasons["heating"]["preferred_sleep_c"]), 0.5, format="%.1f")
    st.caption("室溫偏離偏好溫度 ±1.5°C 以內算舒適（約對應 ASHRAE 55 的 PMV ±0.5）。")
    if st.button("套用", type="primary", width="stretch"):
        new = copy.deepcopy(settings)
        new["modes"]["comfort"]["seasons"]["cooling"].update(preferred_awake_c=s_awake, preferred_sleep_c=s_sleep)
        new["modes"]["comfort"]["seasons"]["heating"].update(preferred_awake_c=w_awake, preferred_sleep_c=w_sleep)
        update_settings(new, "已更新舒適偏好")


@st.dialog("🏠 格局與房間")
def dialog_layout() -> None:
    sizable = [s for s in house.spaces if s.area_ping > 0]
    room_id = st.selectbox("房間", [s.id for s in sizable], format_func=lambda i: house.get_space(i).name)
    w_now, d_now = room_dims(cfg_by_id[room_id], layout["rooms"][room_id], templates)
    c1, c2 = st.columns(2)
    width = c1.number_input("寬（m）", 1.0, 20.0, round(w_now, 2), 0.1, key=f"w_{room_id}")
    depth = c2.number_input("深（m）", 1.0, 20.0, round(d_now, 2), 0.1, key=f"d_{room_id}")
    st.caption(f"面積 {width * depth:.1f} m² ＝ {width * depth / 3.3058:.2f} 坪（冷房負載依新面積重算）")
    if st.button("套用尺寸", type="primary", width="stretch"):
        update_settings(set_room_size({**settings, "layout": layout}, room_id, width, depth),
                        f"{house.get_space(room_id).name}已改為 {width:.2f} × {depth:.2f} m")
    st.divider()
    st.caption("在平面圖切換到「調整格局」可以拖曳房間；新增或刪除房間請到「進階設定」。")
    if st.button("重新自動排列格局", width="stretch", help="清除手動調整的房間與家電位置"):
        new = copy.deepcopy(settings)
        new["layout"] = {"rooms": {}, "appliances": {}}
        update_settings(new, "已重新自動排列格局")


def menu_item(title: str, summary: str, dialog, key: str) -> None:
    """左側選單的一個項目：按鈕（標題）＋目前設定摘要。"""
    if st.button(f"{title}　›", key=f"menu_{key}", width="stretch"):
        dialog()
    st.caption(summary)


cooling = settings["modes"]["comfort"]["seasons"]["cooling"]
with st.sidebar:
    st.markdown(SIDEBAR_CSS, unsafe_allow_html=True)
    st.markdown("### ⚡ HEMS 控制台")
    st.caption("點選項目開啟設定")
    menu_item("📅 日期與時間", f"{day}（週{WEEKDAY_NAMES[day.weekday()]}・{day_type}）{sim_hour:02d}:00",
              dialog_datetime, "datetime")
    c1, c2 = st.columns(2)
    if c1.button("◀ 前 1 小時", width="stretch", disabled=sim_hour == 0):
        apply_ui(hour=sim_hour - 1)
    if c2.button("後 1 小時 ▶", width="stretch", disabled=sim_hour == 23):
        apply_ui(hour=sim_hour + 1)
    menu_item("💰 電價方案", tariff.plans[plan_id]["name"], dialog_tariff, "tariff")
    menu_item("🎛️ 運轉模式", mode_schedule.name, dialog_mode, "mode")
    menu_item("🌡️ 舒適偏好", f"夏季清醒 {cooling['preferred_awake_c']:g}°C、睡眠 {cooling['preferred_sleep_c']:g}°C",
              dialog_comfort, "comfort")
    menu_item("🏠 格局與房間", f"{len(house.spaces)} 個空間，總面積 {house.total_area_ping:.1f} 坪", dialog_layout, "layout")

key = sim_key(settings)
table, sims = compare_day(analyzer, key, day, plan_id)
base_plan, base_result = run_schedule(analyzer, key, day, plan_id, tuple(mode_schedule.by_hour), mode_schedule.name)

# 遙控器的手動覆寫只屬於「這一天」；換日期時清除
if st.session_state.get("remote_day") != str(day):
    st.session_state.remote, st.session_state.remote_day = {}, str(day)
remote: dict = st.session_state.remote
remote_key = json.dumps(remote, sort_keys=True)
effective_plan, remote_result = run_with_remote(analyzer, base_plan, key, day, plan_id,
                                                tuple(mode_schedule.by_hour), remote_key)
selected = remote_result if remote_result is not None else base_result
selected_metrics = day_metrics(analyzer, selected, f"{key}|{day}|{plan_id}|{mode_schedule.by_hour}|{remote_key}")
baseline_metrics = table.iloc[0]
if remote:
    mode_schedule = ModeSchedule(mode_schedule.by_hour, f"{mode_schedule.name}＋遙控")

st.title("居家能源管理系統（HEMS）模擬與最佳化")
st.caption(f"{analyzer.sim.house.name}｜{len(analyzer.sim.schedule.members)} 人｜"
           f"天氣：中央氣象署臺中測站逐時觀測｜電價：台電 {tariff.source['effective_date']} 起實施費率")

if "flash" in st.session_state:
    st.toast(st.session_state.pop("flash"), icon="✅")

tabs = st.tabs(["🏠 智慧平面圖", "📊 總覽", "⚖️ 模式比較", "🗓️ 家電排程", "🌡️ 室內環境", "📅 整月分析與建議",
                "⚙️ 進階設定", "📖 方法說明"])

# ---------- 智慧平面圖 ----------

with tabs[0]:
    hourly = selected.hourly.set_index("hour")
    status = {
        "date": str(day), "weekday": f"週{WEEKDAY_NAMES[day.weekday()]}・{day_type}", "time": f"{sim_hour:02d}:00",
        "location": "臺中市｜中央氣象署臺中測站", "outdoor_temp": float(hourly.loc[sim_hour, "temp_c"]),
        "rh": float(hourly.loc[sim_hour, "rh_pct"]), "house_kw": float(hourly.loc[sim_hour, "kwh"]),
        "period": hourly.loc[sim_hour, "period_label"], "price": float(hourly.loc[sim_hour, "price"]),
        "today_kwh": selected.total_kwh, "today_cost": selected.energy_cost, "mode": mode_schedule.name,
    }
    payload = build_payload(settings, house, layout, templates, selected, effective_plan, remote, sim_hour,
                            status, room_comfort(analyzer, selected, sim_hour))
    event = floorplan(payload)

    # 處理平面圖送回來的操作（每個操作有唯一的 nonce，避免重新整理時重複執行）
    if event and event.get("nonce") != st.session_state.get("last_nonce"):
        st.session_state.last_nonce = event["nonce"]
        working = {**settings, "layout": layout}
        action = event.get("action")
        if action == "move_appliance":
            new, msg = move_appliance(working, house, layout, templates, event["key"], event["x"], event["y"])
            moved_key = event["key"]
            if msg and msg.startswith("已將") and moved_key in remote:   # 搬到別的房間：取消原本的遙控設定
                st.session_state.remote.pop(moved_key)
            update_settings(new, msg)
        elif action == "move_room":
            update_settings(move_room(working, layout, event["id"], event["x"], event["y"]))
        elif action == "remote":
            state = next(a for a in payload["appliances"] if a["key"] == event["key"])
            st.session_state.remote = remote_command(remote, event["key"], event["command"], event.get("value"),
                                                     state, sim_hour)
            st.rerun()
        elif action == "remove":
            space_id, appliance_id = event["key"].split("/")
            st.session_state.remote.pop(event["key"], None)
            update_settings(*remove_appliance(working, house, space_id, appliance_id))
        elif action == "add":
            room = layout["rooms"][event["room"]]
            w, d = room_dims(cfg_by_id[event["room"]], room, templates)
            spot = default_spot(event["type"], room["x"], room["y"], w, d)
            new, msg = add_appliance(working, house, event["room"], event["type"], spot=spot)
            if new is working:
                st.toast(msg, icon="⚠️")
            else:
                update_settings(new, msg)

    with st.expander("這張圖怎麼用？"):
        st.markdown(f"""
- **房間**：牆線內標示寬 × 深（公尺）、坪數與這個時刻的室溫（綠＝舒適、橘＝偏熱、藍＝偏冷、灰＝無人）。
  房間尺寸在左側「格局與房間尺寸」修改，冷房負載會依新面積重算；切到「調整格局」可以拖曳房間排成實際格局。
- **家電圖示**：可新增{ "、".join(ADDABLE.values()) }。右上綠點＝運轉中，左上橘點＝遙控器手動控制，下方為這個時刻的用電（W）。
  拖到別的房間，家電就會搬過去，模擬（例如冷氣負責哪個房間）隨之重算。
- **遙控器**：點一下家電圖示開啟。操作從左側「模擬時間」那一小時起生效到當天結束，
  例如把時間調到 17:00、遠端打開主臥冷氣，就能看到「下班前預冷」對用電與室溫的影響。
- **狀態列**：日期、模擬時間、地點、室外溫濕度、目前電價時段、全屋即時用電、今日累計用電與電費。
""")

# ---------- 總覽 ----------

with tabs[1]:
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

with tabs[2]:
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

with tabs[3]:
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

with tabs[4]:
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

with tabs[5]:
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

with tabs[6]:
    st.subheader("進階設定")
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

with tabs[7]:
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
