"""家電使用紀錄與用電預測（第 5 批，預測頁使用；只呼叫 core 的推演，不改演算法）。

紀錄（存在資料庫 usage_records，每個設定檔各自一份）：
- habit：每週固定習慣，例如 {"space_id": "living_room", "appliance_id": "tv", "weekdays": [0..4], "start": 19, "end": 22}
- daily：某天實際使用時數，例如 {"date": "2026-07-20", "space_id": ..., "appliance_id": ..., "hours": 5}
- bill：台電帳單，例如 {"start": "2026-06-01", "end": "2026-07-31", "kwh": 820}

預測方法：
1. 物理模擬：以一般習慣（未最佳化）推演該月每一天的用電與電費；該月沒有天氣資料時，
   改用月份最接近的天氣（例如 10 月用 9 月），以平日／假日的平均逐時用電代替，電費依目標月份的電價計算。
2. 紀錄修正：有紀錄的家電，以「功率 × 紀錄時數」取代模擬的用電（每日紀錄優先於每週習慣）。
3. 帳單修正（使用者決定是否套用）：修正係數 = 帳單總度數 ÷ 同期間模擬度數。
4. 範圍：沒有帳單時 ±10%；有帳單時用帳單與模擬的誤差大小（至少 ±5%）；用代替月份天氣時再加 5%。
"""
from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import date, timedelta

import pandas as pd

from core.metrics import Analyzer
from core.schedule import day_type_for
from core.tariff import Tariff

WEEKDAY_NAMES = "一二三四五六日"
EXCLUDED = {"air_conditioner"}      # 冷氣耗電隨天氣與設定溫度變化，不能用「功率 × 時數」估算


def recordable_appliances(analyzer: Analyzer) -> dict[str, tuple[str, float]]:
    """可以記錄的家電：{"空間id/家電id": (顯示名稱, 功率 W)}。"""
    return {f"{s.id}/{a.id}": (f"{s.name}・{a.name}（{a.power_w:g} W）", a.power_w)
            for s, a in analyzer.sim.house.all_appliances() if a.enabled and a.id not in EXCLUDED}


def habit_hours(habit: dict) -> float:
    """每天使用時數；結束時間小於開始時間代表跨午夜（例如 22–2 時 = 4 小時）。"""
    start, end = int(habit["start"]), int(habit["end"])
    return float((end - start) % 24)


def describe_habit(habit: dict) -> str:
    days = habit["weekdays"]
    if sorted(days) == list(range(5)):
        when = "平日"
    elif sorted(days) == [5, 6]:
        when = "週末"
    elif len(days) == 7:
        when = "每天"
    else:
        when = "週" + "、".join(WEEKDAY_NAMES[d] for d in sorted(days))
    return f"{when} {int(habit['start']):02d}:00–{int(habit['end']):02d}:00（每天 {habit_hours(habit):g} 小時）"


# ---------- 物理模擬：每月概況 ----------

def simulate_month(analyzer: Analyzer, year: int, month: int, plan_id: str,
                   available: set[date], step: int = 1) -> dict:
    """推演某月有天氣資料的每一天（一般習慣），整理成可快取的概況。step > 1 時每隔 step 天取樣一天（較快）。

    回傳 {"by_date": {日期: {"kwh", "cost"}}, "mean": {日別: {"kwh", "cost"}},
          "appliance": {日別: {"空間id/家電id": kWh}}, "hourly": {日別: [24 小時平均 kWh]}}
    """
    by_date, rows, appl, hourly = {}, [], [], []
    plan = analyzer.opt.baseline_plan
    for d in range(1, calendar.monthrange(year, month)[1] + 1, step):
        day = date(year, month, d)
        if day not in available:
            continue
        try:
            r = analyzer.sim.run(day, plan_id, plan)
        except ValueError:          # 該日逐時資料不完整
            continue
        by_date[day] = {"kwh": r.total_kwh, "cost": r.energy_cost}
        rows.append({"day_type": r.day_type, "kwh": r.total_kwh, "cost": r.energy_cost})
        a = r.records.groupby(["space_id", "appliance_id"])["kwh"].sum()
        appl.append({"day_type": r.day_type, **{f"{s}/{i}": v for (s, i), v in a.items()}})
        hourly.append({"day_type": r.day_type, **{h: float(v) for h, v in enumerate(r.hourly["kwh"])}})
    if not rows:
        return {"by_date": {}, "mean": {}, "appliance": {}, "hourly": {}}
    df = pd.DataFrame(rows)
    hourly_df = pd.DataFrame(hourly)
    hourly_mean = hourly_df.groupby("day_type").mean()
    mean = df.groupby("day_type")[["kwh", "cost"]].mean()
    # 只有平日或只有假日的資料時，另一種用全部平均
    mean_all = df[["kwh", "cost"]].mean()
    appl_df = pd.DataFrame(appl).fillna(0.0)
    appl_mean = appl_df.groupby("day_type").mean()
    out_mean, out_appl, out_hourly = {}, {}, {}
    for t in ("weekday", "holiday"):
        out_mean[t] = (mean.loc[t] if t in mean.index else mean_all).to_dict()
        out_appl[t] = (appl_mean.loc[t] if t in appl_mean.index else appl_df.drop(columns="day_type").mean()).to_dict()
        h = hourly_mean.loc[t] if t in hourly_mean.index else hourly_df.drop(columns="day_type").mean()
        out_hourly[t] = [float(h[i]) for i in range(24)]
    return {"by_date": by_date, "mean": out_mean, "appliance": out_appl, "hourly": out_hourly}


def source_month(year: int, month: int, months: list[tuple[int, int]], tariff: Tariff) -> tuple[int, int] | None:
    """推演用的天氣月份：有資料就用該月；沒有就用月份最接近的（例如 10 月用 9 月、12 月用 1 月），
    距離相同時優先同一電價季節（夏月／非夏月）。電費仍依目標月份的電價計算（見 estimate_day）。"""
    if (year, month) in months:
        return year, month
    if not months:
        return None
    summer = tariff.is_summer(date(year, month, 1))

    def distance(ym: tuple[int, int]) -> tuple:
        gap = abs(ym[1] - month)
        return (min(gap, 12 - gap), tariff.is_summer(date(ym[0], ym[1], 1)) != summer, abs(ym[0] - year))
    return min(months, key=distance)


# ---------- 紀錄修正 ----------

def record_adjustment(day: date, day_type: str, records: dict, powers: dict, sim_appliance: dict) -> float:
    """這一天有紀錄的家電：紀錄用電 −模擬用電（kWh）。"""
    hours: dict[str, float] = {}
    for h in records.get("habit", []):
        key = f"{h['space_id']}/{h['appliance_id']}"
        hours.setdefault(key, 0.0)            # 有每週習慣的家電：沒列到的星期視為不使用
        if day.weekday() in h["weekdays"]:
            hours[key] += habit_hours(h)
    # 每日紀錄覆蓋每週習慣（同一天同一家電有多筆時加總）
    daily = {}
    for d in records.get("daily", []):
        if d["date"] == day.isoformat():
            key = f"{d['space_id']}/{d['appliance_id']}"
            daily[key] = daily.get(key, 0.0) + float(d["hours"])
    hours.update(daily)
    adj = 0.0
    for key, h in hours.items():
        if key in powers:
            adj += powers[key] * h / 1000 - sim_appliance.get(day_type, {}).get(key, 0.0)
    return adj


# ---------- 預測與帳單比對 ----------

Profiles = Callable[[int, int], tuple[dict, tuple[int, int] | None]]   # (年, 月) → (月概況, 實際使用的天氣月份)


def estimate_day(day: date, profiles: Profiles, tariff: Tariff, records: dict, powers: dict,
                 plan_id: str = "tou_2") -> dict:
    """一天的用電估計：模擬值、加上紀錄修正後的值（kWh）與流動電費（元）。

    有該日推演結果就直接用；否則用平日／假日的平均逐時用電 × 該日的逐時電價（夏月／非夏月、尖離峰依目標日期）。"""
    prof, src = profiles(day.year, day.month)
    t = day_type_for(day, tariff.is_off_peak_day)
    if day in prof["by_date"]:
        kwh, cost = prof["by_date"][day]["kwh"], prof["by_date"][day]["cost"]
    elif t in prof["hourly"]:
        hourly = prof["hourly"][t]
        prices = tariff.hourly_prices(day, plan_id)["price"]
        kwh, cost = float(sum(hourly)), float(sum(k * p for k, p in zip(hourly, prices)))
    else:
        kwh, cost = 0.0, 0.0
    adjusted = max(0.0, kwh + record_adjustment(day, t, records, powers, prof["appliance"]))
    price = cost / kwh if kwh else 0.0
    return {"date": day, "day_type": t, "sim_kwh": kwh, "kwh": adjusted, "cost": adjusted * price,
            "proxy": src != (day.year, day.month)}


def month_bill(tariff: Tariff, plan_id: str, year: int, month: int, kwh: float, energy_cost: float) -> float:
    plan = tariff.plans[plan_id]
    if plan["type"] == "time_of_use":
        return tariff.tou_monthly_bill(plan_id, energy_cost, kwh)["total"]
    return tariff.progressive_bill(kwh, tariff.is_summer(date(year, month, 1)))


def compare_bills(bills: list[dict], profiles: Profiles, tariff: Tariff, records: dict,
                  powers: dict) -> tuple[pd.DataFrame, float | None]:
    """每張帳單：帳單度數 vs 同期間模擬（含紀錄修正）度數。回傳（比對表, 修正係數）。"""
    rows = []
    for b in bills:
        start, end = date.fromisoformat(b["start"]), date.fromisoformat(b["end"])
        days = [start + timedelta(n) for n in range((end - start).days + 1)]
        sim = sum(estimate_day(d, profiles, tariff, records, powers)["kwh"] for d in days)
        rows.append({"期間": f"{start}～{end}", "天數": len(days), "帳單度數": float(b["kwh"]),
                     "模擬度數": sim, "誤差": (float(b["kwh"]) - sim) / sim if sim else 0.0})
    df = pd.DataFrame(rows, columns=["期間", "天數", "帳單度數", "模擬度數", "誤差"])
    if df.empty or df["模擬度數"].sum() <= 0:
        return df, None
    return df, float(df["帳單度數"].sum() / df["模擬度數"].sum())


def uncertainty(bill_table: pd.DataFrame, factor: float | None, applied: bool, proxy: bool) -> float:
    """預測範圍（±比例）。"""
    if bill_table.empty or factor is None:
        u = 0.10
    else:
        scale = factor if applied else 1.0
        residual = bill_table["帳單度數"] / (bill_table["模擬度數"] * scale) - 1
        u = max(0.05, float(residual.abs().max()))
    return u + (0.05 if proxy else 0.0)


def forecast_month(year: int, month: int, plan_id: str, profiles: Profiles, tariff: Tariff, records: dict,
                   powers: dict, factor: float = 1.0, spread: float = 0.10) -> dict:
    """預測某月用電與電費（含範圍）。"""
    days = [date(year, month, d) for d in range(1, calendar.monthrange(year, month)[1] + 1)]
    daily = pd.DataFrame([estimate_day(d, profiles, tariff, records, powers, plan_id) for d in days])
    daily["kwh"] *= factor
    daily["cost"] *= factor
    kwh, energy = float(daily["kwh"].sum()), float(daily["cost"].sum())
    low, high = kwh * (1 - spread), kwh * (1 + spread)
    _, src = profiles(year, month)
    return {
        "year": year, "month": month, "daily": daily, "kwh": kwh, "low": low, "high": high,
        "sim_kwh": float(daily["sim_kwh"].sum()), "record_kwh": float((daily["kwh"] - daily["sim_kwh"] * factor).sum()),
        "bill": month_bill(tariff, plan_id, year, month, kwh, energy),
        "bill_low": month_bill(tariff, plan_id, year, month, low, energy * (1 - spread)),
        "bill_high": month_bill(tariff, plan_id, year, month, high, energy * (1 + spread)),
        "source": src, "proxy": bool(daily["proxy"].any()),
    }


def next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)
