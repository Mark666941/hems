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
import threading
from datetime import date, datetime

import streamlit as st

from core.data_source import WeatherUnavailable, load_representative_days
from core.models import load_space_templates
from core.optimizer import MODE_ORDER, ModeSchedule
from ui import charts
from ui.account import account_dialog, autosave, ensure_session, migrate
from ui.context import (
    WEEKDAY_NAMES,
    AppContext,
    available_dates,
    get_analyzer,
    month_bills,
    run_baseline,
    run_schedule,
    run_with_remote,
    update_settings,
)
from ui.family import apply_person_overrides
from ui.floorplan import ensure_layout, room_dims, set_room_size
from ui.logs import get_logger, guarded
from ui.pages import about, analysis, forecast, home, plan, settings as settings_page
from ui.settings import (
    WEATHER_SOURCES,
    default_settings,
    load_towns,
    location_key,
    location_label,
    location_of,
    sim_key,
    weather_source,
)
from ui.theme import apply_theme
from ui.wizard import open_wizard, show_wizard_if_open

st.set_page_config(page_title="HEMS 居家能源管理", page_icon="⚡", layout="wide")
apply_theme()

# ---------- 背景預先計算 ----------

@st.cache_resource(show_spinner=False)
def start_warmup() -> threading.Thread:
    """伺服器啟動後第一次有人開網頁時（每個伺服器程序只做一次），在背景先算好「預設房型、夏季代表日、
    正常模式」的首頁結果。訪客在登入畫面的這段時間計算就在進行，按下「示範帳號」時直接取用快取。
    雲端免費主機單核心又慢，示範帳號第一次進入首頁原本要約 27 秒。參數必須與 app 實際呼叫的完全相同。"""
    def work() -> None:
        try:
            settings = migrate(default_settings())          # 示範帳號的設定載入時也會經過 migrate
            key = sim_key(settings)
            an = get_analyzer(key)
            day0, normal = load_representative_days()["summer"], an.opt.modes["normal"]
            schedule = ModeSchedule.single(normal)
            run_schedule(an, key, day0, "tou_2", tuple(schedule.by_hour), schedule.name, "coordinate")
            run_baseline(an, key, day0, "tou_2")
        except Exception:   # noqa: BLE001  預先計算失敗不影響使用，之後照常計算
            get_logger("app").exception("背景預先計算失敗")

    thread = threading.Thread(target=work, name="hems-warmup", daemon=True)
    thread.start()
    return thread


start_warmup()

# ---------- 狀態 ----------

rep = load_representative_days()
DEFAULT_UI = {
    "preset": "夏季代表日", "custom_day": rep["summer"], "hour": datetime.now().hour, "plan_id": "tou_2",
    "schedule_type": "single", "mode": "normal", "day_mode": "normal", "night_mode": "sleep",
    "day_start": 7, "day_end": 23, "forecast_correction": False,
    "onboarded": False, "opt_method": "coordinate",
}
# 登入並載入最後使用的設定檔（桌面版自動以本機使用者登入）；之後每次修改都會自動存檔
store = ensure_session(DEFAULT_UI)
settings: dict = st.session_state.settings
ui: dict = st.session_state.ui
location = location_of(settings)
live_weather = location["source"] == "openmeteo"
dates = available_dates(location_key(settings))

try:
    analyzer = get_analyzer(sim_key(settings))
except (ValueError, KeyError) as e:
    get_logger("app").warning("設定有誤（設定檔 %s）：%s", st.session_state.get("profile_id"), e)
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
if day not in set(dates):   # 換天氣來源後，原本選的日期可能沒有資料（例如今天只有 Open-Meteo 有）
    day = min(dates, key=lambda d: abs((d - day).days))
    st.session_state.ui.update(preset="自選日期", custom_day=day)
    st.toast(f"所選天氣來源沒有原本日期的資料，已改為 {day}", icon="📅")
try:   # Open-Meteo 需要網路：先確認這一天取得到天氣，取不到就提示並提供改回測站資料
    analyzer.sim.weather.get_hourly_weather(day)
except WeatherUnavailable as e:
    get_logger("app").warning("天氣取得失敗：%s", e)
    st.error(str(e))
    if st.button("改用中央氣象署臺中測站資料"):
        new = copy.deepcopy(settings)
        new["location"] = {**location, "source": "cwa"}
        update_settings(new, "已改用中央氣象署臺中測站資料")
        st.rerun()
    st.stop()
sim_hour = int(ui["hour"])
plan_id = ui["plan_id"]
day_type = "假日" if tariff.is_off_peak_day(day) else "平日"
if ui["schedule_type"] == "single":
    mode_schedule = ModeSchedule.single(modes[ui["mode"]])
else:
    mode_schedule = ModeSchedule.switch(modes, ui["day_mode"], ui["night_mode"],
                                        night_start=ui["day_end"], night_end=ui["day_start"])

# ---------- 左側功能選單：每個功能一個設定視窗 ----------

OPT_METHODS = {"coordinate": "座標下降（預設）", "milp": "混合整數規劃 MILP（全屋、逐時）"}

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
    custom = min(max(ui["custom_day"], dates[0]), dates[-1])
    if preset == "自選日期":
        custom = st.date_input("選擇日期", value=custom, min_value=dates[0], max_value=dates[-1])
    st.caption("代表日：該月份中氣溫最接近月平均、沒有下雨的平日（中央氣象署臺中測站實測資料）。"
               + (f"目前使用 Open-Meteo，可選 {dates[0]} 到 {dates[-1]}（今天之後為預報）。" if live_weather else ""))
    hour = st.slider("模擬時間", 0, 23, value=sim_hour, format="%d:00",
                     help="平面圖顯示這個時刻的室溫與家電用電；遙控器操作從這個時刻起生效")
    if st.button("套用", type="primary", width="stretch"):
        apply_ui(preset=preset or ui["preset"], custom_day=custom, hour=hour)


@st.dialog("📍 地區與天氣")
def dialog_location() -> None:
    towns = load_towns()
    source = st.radio("天氣來源", list(WEATHER_SOURCES), index=list(WEATHER_SOURCES).index(location["source"]),
                      format_func=WEATHER_SOURCES.get)
    if source == "cwa":
        st.caption("中央氣象署臺中測站（467490）2026 年 1–9 月逐時實測，不需要網路，結果可重現（預設）。")
        new_loc = {**location, "source": "cwa"}
    else:
        counties = list(towns)
        c1, c2 = st.columns(2)
        county = c1.selectbox("縣市", counties, index=counties.index(location["county"]))
        names = list(towns[county])
        town = c2.selectbox("鄉鎮市區", names,
                            index=names.index(location["town"]) if location["town"] in names else 0)
        lat, lon = towns[county][town]
        st.caption(f"中心點：北緯 {lat:.3f}°、東經 {lon:.3f}°（內政部鄉鎮市區界線計算）。"
                   "天氣：過去用再分析資料、近期與未來 7 天用預報（約 9–25 km 網格，與單一測站略有差異；"
                   "市區測站受熱島效應影響，通常比網格值高約 1°C）。太陽位置也依此地點計算。")
        new_loc = {"source": "openmeteo", "county": county, "town": town}
    if st.button("套用", type="primary", width="stretch"):
        if new_loc["source"] == "openmeteo":
            try:
                with st.spinner("下載天氣資料…"):
                    weather_source({"location": new_loc}).get_hourly_weather(date.today())
            except WeatherUnavailable as e:
                st.error(str(e))
                return
        new = copy.deepcopy(settings)
        new["location"] = new_loc
        if new_loc["source"] == "openmeteo" and location["source"] != "openmeteo":
            st.session_state.ui.update(preset="自選日期", custom_day=date.today(), hour=datetime.now().hour)
        update_settings(new, f"天氣改為 {location_label(new)}")
        st.rerun()


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
    st.divider()
    method = st.radio("最佳化方法", list(OPT_METHODS), index=list(OPT_METHODS).index(ui.get("opt_method", "coordinate")),
                      format_func=OPT_METHODS.get,
                      help="座標下降：逐房搜尋、每個時段一個設定溫度（預設，0.1 秒）。"
                           "MILP：全屋聯合混合整數規劃，每房每小時各自調整，舒適度通常較好（約 0.4 秒）。"
                           "比較結果見 docs/reports/optimizer_comparison.md")
    if st.button("套用", type="primary", width="stretch"):
        apply_ui(**changes, opt_method=method)


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
        if key != "wizard":
            st.session_state.wizard_open = False   # 同一次執行只能開一個視窗
        dialog()
    st.caption(summary)


cooling = settings["modes"]["comfort"]["seasons"]["cooling"]
with st.sidebar:
    st.markdown("### ⚡ HEMS 控制台")
    st.caption("點選項目開啟設定")
    menu_item("👤 帳號與設定檔", f"{st.session_state.profile_name}・自動存檔", lambda: account_dialog(DEFAULT_UI),
              "account")
    menu_item("🧭 新手引導", "房型、成員作息、電價、熱水器一次設定", open_wizard, "wizard")
    menu_item("📅 日期與時間", f"{day}（週{WEEKDAY_NAMES[day.weekday()]}・{day_type}）{sim_hour:02d}:00",
              dialog_datetime, "datetime")
    c1, c2, c3 = st.columns([1, 1, 1])
    if c1.button("◀", width="stretch", disabled=sim_hour == 0, help="前 1 小時"):
        apply_ui(hour=sim_hour - 1)
    if c2.button("現在", width="stretch", help="模擬時間（使用 Open-Meteo 時連同日期）回到現在"):
        home.back_to_now(live_weather)
        st.rerun()
    if c3.button("▶", width="stretch", disabled=sim_hour == 23, help="後 1 小時"):
        apply_ui(hour=sim_hour + 1)
    menu_item("📍 地區與天氣", location_label(settings), dialog_location, "location")
    menu_item("💰 電價方案", tariff.plans[plan_id]["name"], dialog_tariff, "tariff")
    menu_item("🎛️ 運轉模式", mode_schedule.name + ("・MILP 最佳化" if ui.get("opt_method") == "milp" else ""),
              dialog_mode, "mode")
    menu_item("🌡️ 舒適偏好", f"夏季清醒 {cooling['preferred_awake_c']:g}°C、睡眠 {cooling['preferred_sleep_c']:g}°C",
              dialog_comfort, "comfort")
    menu_item("🏠 格局與房間", f"{len(house.spaces)} 個空間，總面積 {house.total_area_ping:.1f} 坪", dialog_layout, "layout")
show_wizard_if_open()   # 新手引導開啟中（左側選單或首頁開啟）

# ---------- 共用推演結果 ----------

# 遙控器的手動覆寫、人物的臨時變動都只屬於「這一天」；換日期時清除
if st.session_state.get("remote_day") != str(day):
    st.session_state.remote, st.session_state.remote_day = {}, str(day)
    st.session_state.person_overrides = {}
remote: dict = st.session_state.remote
person_overrides: dict = st.session_state.setdefault("person_overrides", {})

# 人物臨時變動：疊加到作息上再模擬（不寫入設定檔）
sim_settings = settings
if person_overrides:
    sim_settings = apply_person_overrides(settings, person_overrides, "holiday" if day_type == "假日" else "weekday")
    try:
        analyzer = get_analyzer(sim_key(sim_settings))
    except (ValueError, KeyError):
        st.session_state.person_overrides, sim_settings = {}, settings
key = sim_key(sim_settings)
base_plan, base_result = run_schedule(analyzer, key, day, plan_id, tuple(mode_schedule.by_hour), mode_schedule.name,
                                     ui.get("opt_method", "coordinate"))
effective_plan, remote_result = run_with_remote(analyzer, base_plan, key, day, plan_id, tuple(mode_schedule.by_hour),
                                                json.dumps(remote, sort_keys=True))
if remote:
    mode_schedule = ModeSchedule(mode_schedule.by_hour, f"{mode_schedule.name}＋遙控")

ctx = AppContext(settings=settings, key=key, analyzer=analyzer, templates=templates, layout=layout, day=day,
                 day_type=day_type, hour=sim_hour, plan_id=plan_id, mode_schedule=mode_schedule, base_plan=base_plan,
                 plan=effective_plan, selected=remote_result if remote_result is not None else base_result,
                 remote=remote, dates=dates, person_overrides=person_overrides)

if "flash" in st.session_state:
    st.toast(st.session_state.pop("flash"), icon="✅")

# ---------- 頂端導覽 ----------

PAGES = [("home", home, "首頁", ":material/home:"), ("plan", plan, "平面圖", ":material/floor:"),
         ("analysis", analysis, "分析", ":material/monitoring:"), ("forecast", forecast, "預測", ":material/trending_up:"),
         ("settings", settings_page, "設定", ":material/tune:"), ("about", about, "說明", ":material/info:")]
start = st.session_state.get("_test_page", "home")   # 預設首頁；自動測試可指定其他頁
# 每個頁面包在 guarded() 裡：頁面出錯時記錄並顯示錯誤訊息，左側選單與其他頁面照常可用
ctx.pages = {name: st.Page(lambda m=module, t=title: guarded(f"{t}頁", lambda: m.render(ctx)), title=title, icon=icon,
                           url_path=name, default=name == start)
             for name, module, title, icon in PAGES}
st.navigation(list(ctx.pages.values()), position="top").run()
autosave(store)   # 這次執行中若有修改設定或介面選項，寫回資料庫
