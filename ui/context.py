"""各頁面共用的資料與快取。

app.py 每次執行時建立一個 AppContext（目前設定、日期時間、模擬結果……），
所有頁面都從這裡拿資料，不各自重算；昂貴的計算（四種模式比較、整月分析）用 st.cache_data 快取，
需要時才算（例如首頁不需要四種模式比較，就不會觸發）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from core.data_source import CsvMeterSource
from core.metrics import Analyzer, compare_with_meter
from core.optimizer import ModeSchedule
from core.simulator import ControlPlan, SimulatedMeterSource, SimulationResult
from ui import insights, usage, whatif
from ui.floorplan import apply_remote
from ui.settings import build_analyzer, location_key, location_label, weather_source

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
                 by_hour: tuple[str, ...], name: str, method: str = "coordinate") -> tuple[ControlPlan, SimulationResult]:
    r = _analyzer.opt.optimize(day, ModeSchedule(list(by_hour), name), plan_id, method=method)
    return r.plan, r.result


@st.cache_data(max_entries=16, show_spinner=False)
def run_baseline(_analyzer: Analyzer, settings_key: str, day: date, plan_id: str) -> SimulationResult:
    """一般習慣（未最佳化）的推演，首頁比較用（約 0.05 秒）。"""
    return _analyzer.sim.run(day, plan_id, _analyzer.opt.baseline_plan)


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


@st.cache_data(max_entries=4, show_spinner="整月分析中（每天各模式都要最佳化，多核心平行計算，約 2–3 秒）…")
def analyze_month(_analyzer: Analyzer, settings_key: str, year: int, month: int, plan_id: str):
    table, scenarios = _analyzer.compare_month(year, month, plan_id)
    recs = _analyzer.recommendations(year, month, plan_id, table, scenarios)
    report = _analyzer.report_markdown(year, month, plan_id, table, recs)
    return table, recs, report, insights.daily_table(scenarios)


@st.cache_data(max_entries=4, show_spinner="產生 PDF 報告…")
def month_pdf(_analyzer: Analyzer, settings_key: str, year: int, month: int, plan_id: str, focus: str) -> bytes:
    """整月分析報告 PDF（focus：數據分析要和基準比較的模式）。"""
    from ui.pdf_report import month_report_pdf  # matplotlib 較慢載入，需要時才匯入

    table, recs, _, daily = analyze_month(_analyzer, settings_key, year, month, plan_id)
    plan_name = _analyzer.sim.tariff.plans[plan_id]["name"]
    base = table.iloc[0]
    price = base[f"{plan_name}(元)"] / base["月用電(kWh)"] if base["月用電(kWh)"] else 0.0
    return month_report_pdf(year, month, _analyzer.sim.house.name, plan_name, table, recs, daily,
                            insights.month_insights(daily, focus, price),
                            weather_label=location_label(json.loads(settings_key)).replace("｜", " "))


@st.cache_data(max_entries=8, show_spinner="試算各電價方案的月電費…")
def month_bills(_analyzer: Analyzer, settings_key: str, year: int, month: int) -> pd.DataFrame:
    """以一般習慣（基準）推演整月，換算三種電價方案的月電費（約 1 秒）。"""
    return _analyzer.sim.run_month(year, month, "tou_2", _analyzer.opt.baseline_plan).bills()


@st.cache_data(ttl=3600)
def available_dates(location: str | None = None) -> list[date]:
    """所選天氣來源有資料的日期（location 為 ui.settings.location_key；Open-Meteo 每小時更新到今天＋7 天）。"""
    settings = {"location": json.loads(location)} if location else {}
    return weather_source(settings).available_dates()


def _dates_for(settings_key: str) -> list[date]:
    return available_dates(location_key(json.loads(settings_key)))


@st.cache_data(max_entries=16, show_spinner="推演一整年（每個情境約 3 秒）…")
def annual_scenario(settings_key: str, scenario_id: str, plan_id: str) -> pd.DataFrame:
    """情境比較：某個情境（或目前 current）的全年逐月用電與電費。"""
    settings, params = whatif.apply_scenario(json.loads(settings_key), whatif.default_params(), scenario_id)
    return whatif.annual(settings, params, plan_id, set(_dates_for(settings_key)))


@st.cache_data(max_entries=4, show_spinner="比對實測與模擬…")
def meter_comparison(_analyzer: Analyzer, settings_key: str, path: str) -> pd.DataFrame:
    """異常偵測：實測電表 CSV 與模擬（一般習慣）的逐時對照表；只比對有天氣資料的日子。"""
    measured = CsvMeterSource(Path(path))
    simulated = SimulatedMeterSource(_analyzer.sim, control=_analyzer.opt.baseline_plan)
    weather_days = set(_dates_for(settings_key))
    days = [d for d in measured.available_dates() if d in weather_days]
    detail, _ = compare_with_meter(measured, simulated, days, _analyzer.cfg["calibration"]["hourly"])
    return detail


@st.cache_data(max_entries=24, show_spinner="推演整月用電（預測用，約 2 秒）…")
def month_profile(_analyzer: Analyzer, settings_key: str, year: int, month: int, plan_id: str):
    """預測頁：某月一般習慣的每日用電概況；沒有天氣資料的月份改用最接近的月份（回傳實際使用的月份）。"""
    dates = set(_dates_for(settings_key))
    months = sorted({(d.year, d.month) for d in dates})
    src = usage.source_month(year, month, months, _analyzer.sim.tariff)
    if src is None:
        return {"by_date": {}, "mean": {}, "appliance": {}, "hourly": {}}, None
    prof = usage.simulate_month(_analyzer, src[0], src[1], plan_id, dates)
    if src != (year, month):
        prof = {**prof, "by_date": {}}      # 代替月份只用平日／假日平均
    return prof, src


# ---------- 設定 ----------

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


# ---------- 共用情境 ----------

@dataclass
class AppContext:
    """一次執行中所有頁面共用的狀態與結果。"""

    settings: dict
    key: str                          # 模擬快取 key（不含平面圖位置）
    analyzer: Analyzer
    templates: dict
    layout: dict
    day: date
    day_type: str
    hour: int
    plan_id: str
    mode_schedule: ModeSchedule
    base_plan: ControlPlan            # 所選模式最佳化後的控制策略
    plan: ControlPlan                 # 加上遙控器覆寫後的控制策略
    selected: SimulationResult        # 所選模式（含遙控）的推演結果
    remote: dict
    dates: list[date] = field(default_factory=list)
    pages: dict = field(default_factory=dict)       # 頁面物件（st.page_link 用）
    person_overrides: dict = field(default_factory=dict)   # 人物臨時變動（拖曳到別的房間）

    @property
    def location_label(self) -> str:
        return location_label(self.settings)

    @property
    def sim(self):
        return self.analyzer.sim

    @property
    def house(self):
        return self.analyzer.sim.house

    @property
    def tariff(self):
        return self.analyzer.sim.tariff

    @property
    def cfg_by_id(self) -> dict:
        return {s["id"]: s for s in self.settings["house"]["spaces"]}

    @property
    def weekday(self) -> str:
        return f"週{WEEKDAY_NAMES[self.day.weekday()]}"

    def baseline(self) -> SimulationResult:
        return run_baseline(self.analyzer, self.key, self.day, self.plan_id)

    def compare(self):
        """四種模式比較（較慢，只在分析頁使用）。"""
        return compare_day(self.analyzer, self.key, self.day, self.plan_id)

    def metrics(self, result: SimulationResult, tag: str) -> dict:
        return day_metrics(self.analyzer, result,
                           f"{self.key}|{self.day}|{self.plan_id}|{tag}|{json.dumps(self.remote, sort_keys=True)}")

    def month_profile(self, year: int, month: int):
        return month_profile(self.analyzer, self.key, year, month, self.plan_id)

    def people_home(self, hour: int | None = None) -> float:
        h = self.hour if hour is None else hour
        return float(self.sim.schedule.people_at_home(self.selected.day_type)[h])

    def ac_rooms_on(self, hour: int | None = None) -> list[str]:
        h = self.hour if hour is None else hour
        records = self.selected.records
        on = records[(records["hour"] == h) & (records["appliance_id"] == "air_conditioner") & (records["kwh"] > 0)]
        return list(on["space"].unique())
