"""HEMS 居家能源管理系統：Streamlit 前端入口。

執行：streamlit run app.py（或 python run_app.py 開桌面視窗）

版面：
- 頂端導覽列：🏠 首頁（家中此刻）、🗺️ 平面圖、📊 分析、📈 預測、⚙️ 設定、ℹ️ 說明（ui/pages/）
- 左側功能選單：日期與時間、電價方案、運轉模式、舒適偏好、格局與房間；點選開啟設定視窗，按「套用」才生效
這個檔案只負責：主題、介面狀態、左側選單、建立各頁共用的 AppContext、頁面導覽。
所有計算呼叫 core/ 的模組；設定以 JSON 字串為 key 快取，相同設定不重算。
"""
from __future__ import annotations

import copy
import json
from datetime import datetime

import streamlit as st

from core.data_source import load_representative_days
from core.models import load_space_templates
from core.optimizer import MODE_ORDER, ModeSchedule
from ui import charts
from ui.context import (WEEKDAY_NAMES, AppContext, available_dates, get_analyzer, month_bills, run_schedule,
                        run_with_remote, update_settings)
from ui.floorplan import ensure_layout, room_dims, set_room_size
from ui.pages import about, analysis, forecast, home, plan, settings as settings_page
from ui.settings import default_settings, sim_key
from ui.theme import apply_theme

st.set_page_config(page_title="HEMS 居家能源管理", page_icon="⚡", layout="wide")
apply_theme()

# ---------- 狀態 ----------

if "settings" not in st.session_state:
    st.session_state.settings = default_settings()
settings: dict = st.session_state.settings

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

# ---------- 左側功能選單：每個功能一個設定視窗 ----------

PLAN_GUIDE = {
    "progressive": ("一般家庭預設的計費方式。不分時段，**每月用越多度、每度越貴**（分 6 級，夏月 1.78–8.86 元）。",
                    "晚上用電集中、無法把用電移到半夜的家庭（例如雙薪、晚上才開冷氣）。"),
    "tou_2": ("把一天分成**尖峰、離峰**兩段。夏月平日 9–24 時為尖峰（5.16 元），0–9 時與週末、國定假日為離峰（2.06 元）；"
              "每月另收基本費 75 元。",
              "白天有人在家用電、或能把熱水器、洗衣、烘衣移到半夜離峰的家庭。"),
    "tou_3": ("再把尖峰細分：夏月平日 **16–22 時最貴**（7.13 元），9–16 時與 22–24 時為半尖峰（4.69 元），其餘離峰（2.06 元）。",
              "傍晚到晚上用電很少的家庭；晚上開冷氣的家庭通常不划算。"),
}


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
               "完整比較請看「分析 → 整月分析與建議」。")
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
    st.caption("在平面圖切換到「調整格局」可以拖曳房間；新增或刪除房間請到「設定」頁。")
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
    st.markdown("### ⚡ HEMS 控制台")
    st.caption("點選項目開啟設定")
    menu_item("📅 日期與時間", f"{day}（週{WEEKDAY_NAMES[day.weekday()]}・{day_type}）{sim_hour:02d}:00",
              dialog_datetime, "datetime")
    c1, c2, c3 = st.columns([1, 1, 1])
    if c1.button("◀", width="stretch", disabled=sim_hour == 0, help="前 1 小時"):
        apply_ui(hour=sim_hour - 1)
    if c2.button("現在", width="stretch", help="模擬時間回到現在的小時"):
        home.back_to_now()
        st.rerun()
    if c3.button("▶", width="stretch", disabled=sim_hour == 23, help="後 1 小時"):
        apply_ui(hour=sim_hour + 1)
    menu_item("💰 電價方案", tariff.plans[plan_id]["name"], dialog_tariff, "tariff")
    menu_item("🎛️ 運轉模式", mode_schedule.name, dialog_mode, "mode")
    menu_item("🌡️ 舒適偏好", f"夏季清醒 {cooling['preferred_awake_c']:g}°C、睡眠 {cooling['preferred_sleep_c']:g}°C",
              dialog_comfort, "comfort")
    menu_item("🏠 格局與房間", f"{len(house.spaces)} 個空間，總面積 {house.total_area_ping:.1f} 坪", dialog_layout, "layout")

# ---------- 共用推演結果 ----------

key = sim_key(settings)
base_plan, base_result = run_schedule(analyzer, key, day, plan_id, tuple(mode_schedule.by_hour), mode_schedule.name)
# 遙控器的手動覆寫只屬於「這一天」；換日期時清除
if st.session_state.get("remote_day") != str(day):
    st.session_state.remote, st.session_state.remote_day = {}, str(day)
remote: dict = st.session_state.remote
effective_plan, remote_result = run_with_remote(analyzer, base_plan, key, day, plan_id, tuple(mode_schedule.by_hour),
                                                json.dumps(remote, sort_keys=True))
if remote:
    mode_schedule = ModeSchedule(mode_schedule.by_hour, f"{mode_schedule.name}＋遙控")

ctx = AppContext(settings=settings, key=key, analyzer=analyzer, templates=templates, layout=layout, day=day,
                 day_type=day_type, hour=sim_hour, plan_id=plan_id, mode_schedule=mode_schedule, base_plan=base_plan,
                 plan=effective_plan, selected=remote_result if remote_result is not None else base_result,
                 remote=remote, dates=dates)

if "flash" in st.session_state:
    st.toast(st.session_state.pop("flash"), icon="✅")

# ---------- 頂端導覽 ----------

PAGES = [("home", home, "首頁", ":material/home:"), ("plan", plan, "平面圖", ":material/floor:"),
         ("analysis", analysis, "分析", ":material/monitoring:"), ("forecast", forecast, "預測", ":material/trending_up:"),
         ("settings", settings_page, "設定", ":material/tune:"), ("about", about, "說明", ":material/info:")]
start = st.session_state.get("_test_page", "home")   # 預設首頁；自動測試可指定其他頁
ctx.pages = {name: st.Page(lambda m=module: m.render(ctx), title=title, icon=icon, url_path=name, default=name == start)
             for name, module, title, icon in PAGES}
st.navigation(list(ctx.pages.values()), position="top").run()
