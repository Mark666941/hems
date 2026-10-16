"""情境比較（What-if）與投資回收年限（C4）。

做法：每個情境只改「設定」或「負載模型參數」（不改 core 的演算法），重新組裝系統推演一整年，
和目前的家比較年用電與年電費，再用投資金額算回收年限。
- 一年 12 個月：每月每隔 5 天取樣一天推演（一般習慣），依平日／假日平均估算整月；
  沒有天氣資料的月份（10–12 月）用最接近月份的天氣，電費依該月電價計算（與預測頁相同，見 ui/usage.py）
- 月電費依所選電價方案計算（含基本電費、累進級距），12 個月加總為年電費
- 回收年限 = 投資金額 ÷ 每年省下的電費；使用年限內淨效益 = 每年省 × 使用年限 − 投資金額
"""
from __future__ import annotations

import calendar
import copy
from dataclasses import dataclass
from datetime import date

import pandas as pd

from core.load_model import load_params
from ui import usage
from ui.settings import build_analyzer, build_house

YEAR = 2026
SAMPLE_STEP = 5


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    description: str
    cost_per_unit: float        # 預設投資金額（元／台或／間）
    unit: str
    lifetime_years: int


SCENARIOS = [
    Scenario("ac_upgrade", "冷氣換一級能效變頻機", "額定 COP 3.6 → 4.5（約 CSPF 一級）；冷房能力不變、耗電降低。",
             15000, "台（與一般機種的價差）", 10),
    Scenario("led", "照明全面換 LED", "照明功率減半（傳統燈管、省電燈泡 → LED）。", 800, "間", 10),
    Scenario("heat_pump", "電熱水器換熱泵熱水器", "加熱效率 0.85 → 3.0（熱泵 COP 約 3）。", 60000, "台", 12),
    Scenario("setpoint", "冷氣設定提高 1°C", "一般習慣的冷氣設定溫度 +1°C（不用花錢，只改習慣）。", 0, "—", 10),
]
SCENARIO_BY_ID = {s.id: s for s in SCENARIOS}


def units(settings: dict, scenario_id: str) -> int:
    """情境涉及的設備數量（算預設投資金額用）。"""
    house = build_house(settings)
    target = {"ac_upgrade": "air_conditioner", "led": "lighting", "heat_pump": "water_heater"}.get(scenario_id)
    if target is None:
        return 1 if scenario_id == "setpoint" else 0
    return sum(1 for _, a in house.all_appliances() if a.id == target and a.enabled)


def apply_scenario(settings: dict, params: dict, scenario_id: str) -> tuple[dict, dict]:
    """回傳情境的（設定, 負載模型參數），原本的不修改。"""
    s, p = copy.deepcopy(settings), copy.deepcopy(params)
    if scenario_id == "ac_upgrade":
        p["air_conditioner"]["rated_cop"] = 4.5
    elif scenario_id == "led":
        edits = s.setdefault("appliance_edits", {})
        for space, a in build_house(settings).all_appliances():
            if a.id == "lighting":
                key = f"{space.id}/{a.id}"
                edits[key] = {**edits.get(key, {}), "power_w": a.power_w * 0.5}
    elif scenario_id == "heat_pump":
        p["shiftable_tasks"]["water_heater"]["efficiency"] = 3.0
    elif scenario_id == "setpoint":
        s["baseline_setpoint_c"] = float(s["baseline_setpoint_c"]) + 1
    elif scenario_id != "current":
        raise KeyError(f"未知的情境：{scenario_id}")
    return s, p


def annual(settings: dict, params: dict, plan_id: str, available: set[date]) -> pd.DataFrame:
    """推演一整年（取樣），回傳 12 列：month、kwh、bill、proxy（是否用代替月份的天氣）。"""
    analyzer = build_analyzer(settings, params)
    tariff = analyzer.sim.tariff
    months = sorted({(d.year, d.month) for d in available})
    cache: dict = {}

    def profiles(year: int, month: int):
        src = usage.source_month(year, month, months, tariff)
        if src not in cache:
            prof = usage.simulate_month(analyzer, src[0], src[1], plan_id, available, step=SAMPLE_STEP)
            cache[src] = {**prof, "by_date": {}}      # 全部用平日／假日平均逐時用電 × 當日電價
        return cache[src], src

    rows = []
    for m in range(1, 13):
        days = [usage.estimate_day(date(YEAR, m, d), profiles, tariff, {}, {}, plan_id)
                for d in range(1, calendar.monthrange(YEAR, m)[1] + 1)]
        kwh, energy = sum(x["kwh"] for x in days), sum(x["cost"] for x in days)
        rows.append({"month": m, "kwh": kwh, "bill": usage.month_bill(tariff, plan_id, YEAR, m, kwh, energy),
                     "proxy": days[0]["proxy"]})
    return pd.DataFrame(rows)


def payback_table(current: pd.DataFrame, results: dict[str, pd.DataFrame], costs: dict[str, float]) -> pd.DataFrame:
    """各情境與目前相比：年用電、年電費、每年省、投資、回收年限、使用年限內淨效益。"""
    base_kwh, base_bill = float(current["kwh"].sum()), float(current["bill"].sum())
    rows = []
    for sid, df in results.items():
        sc = SCENARIO_BY_ID[sid]
        kwh, bill = float(df["kwh"].sum()), float(df["bill"].sum())
        saving, cost = base_bill - bill, float(costs.get(sid, 0.0))
        if cost <= 0:
            payback = 0.0 if saving > 0 else None
        else:
            payback = cost / saving if saving > 0 else None
        rows.append({"情境": sc.name, "id": sid, "年用電(kWh)": kwh, "年電費(元)": bill,
                     "每年省電(kWh)": base_kwh - kwh, "每年省(元)": saving, "投資(元)": cost,
                     "回收年限(年)": payback, "使用年限(年)": sc.lifetime_years,
                     "使用年限內淨效益(元)": saving * sc.lifetime_years - cost})
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("使用年限內淨效益(元)", ascending=False).reset_index(drop=True)
    return df


def verdict(row) -> str:
    """一句話結論。"""
    if row["每年省(元)"] <= 0:
        return "省不到電費，不建議"
    if row["投資(元)"] <= 0:
        return "不用花錢，立刻可以做"
    years = row["回收年限(年)"]
    if years <= row["使用年限(年)"] / 2:
        return f"約 {years:.1f} 年回收，划算"
    if years <= row["使用年限(年)"]:
        return f"約 {years:.1f} 年回收，使用年限內回本"
    return f"要 {years:.0f} 年才回收，超過使用年限，不划算"


def default_params() -> dict:
    return load_params()
